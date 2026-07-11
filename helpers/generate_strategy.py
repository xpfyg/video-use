#!/usr/bin/env python3
"""Generate editing strategy / shot-list from TTS metadata.

The strategy is **audio-first**: the dubbing track decides the structure and
runtime. For each phrase separated by comma we produce a shot entry with timing,
beat label, and a visual prompt describing what kind of footage should accompany it.

Usage:
    python helpers/generate_strategy.py test_materials/metadata.json --edit-dir ./edit_test

Output: edit_test/strategy.json with shot list.

每个镜头的 `visual_prompt` 会由 Ollama 模型从指定「大分类」的 labels 数组中判定的素材标签开头
（labels 来自 `labels.json`，用 `--category` 指定大分类；`--labels` 可显式覆盖）。
调用约定与 classify_materials.py 一致：从 `.env` 读取 `OLLAMA_URL`/`OLLAMA_API_KEY`。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import requests

# Ollama 模型（文本/多模态均可；这里做「口播台词 → beat/动作标签/画面」的文本推理）
DEFAULT_MODEL = "qwen3-vl:8b"
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "")
DEFAULT_OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")

# 分类标签配置文件（大分类 → 标签列表 映射），默认位于 skill 根目录
DEFAULT_LABELS_FILE = (Path(__file__).resolve().parents[1] / "labels.json")
DEFAULT_CATEGORY = "冷饮"

# 内置默认素材标签（labels.json 不可用时兜底）
DEFAULT_LABELS = ["拿起产品", "放下产品", "开盖展示", "饮料气泡特写", "配料表", "倒饮品动作"]


def load_labels_for_category(path: Path, category: str) -> list[str]:
    """从 labels 配置（大分类→标签列表 映射）按 category 取出标签列表。

    兼容三种格式：纯 JSON 数组 / {"labels": [...]} / {"大分类": [...], ...}。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        if "labels" in data and isinstance(data["labels"], list):
            return [str(x) for x in data["labels"]]
        if category in data:
            return [str(x) for x in data[category]]
        cats = [k for k, v in data.items() if isinstance(v, list)]
        if cats:
            raise KeyError(f"大分类「{category}」不在 labels 配置中，可用：{', '.join(cats)}")
        raise KeyError(f"labels 配置 {path} 中未找到可用的标签列表")
    raise KeyError(f"labels 配置 {path} 格式不支持")


def resolve_labels(labels_file: Path | None, category: str, explicit: list[str] | None) -> list[str]:
    """确定本次使用的素材标签列表，优先级：--labels > labels.json[category] > 内置默认。"""
    if explicit:
        return explicit
    src = labels_file if (labels_file and labels_file.exists()) else DEFAULT_LABELS_FILE
    if src and src.exists():
        try:
            return load_labels_for_category(src, category)
        except KeyError as e:
            print(f"[error] {e}", file=sys.stderr)
            raise SystemExit(2)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 读取 labels 配置失败，回退默认标签：{e}", file=sys.stderr)
            return list(DEFAULT_LABELS)
    print(f"[warn] 未找到 labels 配置（{src}），使用内置默认标签", file=sys.stderr)
    return list(DEFAULT_LABELS)


def _normalize_action_label(raw: str, labels: list[str]) -> str:
    """把模型返回的素材标签归一到候选集合；匹配不上则保留原样。"""
    raw = (raw or "").strip().strip("。.!！?？\"'“”‘’[]【】()（）")
    if not raw:
        return ""
    if raw in labels:
        return raw
    norm = lambda s: re.sub(r"[\s\-_:：、，。.！!？?]+", "", s)
    rn = norm(raw)
    for lab in labels:
        if norm(lab) == rn:
            return lab
    for lab in labels:
        if lab in raw or raw in lab:
            return lab
    return raw


# Offline fallback: only used when the LLM is unavailable. This keeps the
# pipeline usable without a local model, but the primary path is model-driven.
_BEAT_KEYWORDS: dict[str, list[str]] = {
    "HOOK": ["终于", "找到", "心心念念", "绝了", "巨好吃", "救命"],
    "PRODUCT": ["就是", "酱", "品牌", "虎邦", "泰式", "打抛", "包装", "瓶"],
    "TASTE": ["肉香", "罗勒", "酸辣", "鲜爽", "口感", "味道", "香"],
    "USAGE": ["米饭", "拌", "炒", "吃", "下饭", "配", "正宗"],
    "DETAIL": ["猪肉", "辣椒", "罗勒叶", "香菜", "配料", "一小勺"],
    "CTA": ["快", "买", "囤", "链接", "下单", "赶紧", "别错过"],
}

_BEAT_PROMPT = """你是一名{category}带货短视频剪辑师。请为下面这句口播台词判断镜头类型并给出画面建议。

台词：「{text}」

可选 beat 类型（选一个）：
- HOOK：吸引注意力的开头
- PRODUCT：产品/包装展示
- TASTE：味道、口感、食欲感
- USAGE：食用/烹饪/搭配场景
- DETAIL：配料、食材、细节
- CTA：引导购买/行动号召

素材标签,选一个作为该镜头找素材的标准：
{labels}

判断要求：
1. beat：从上方 beat 类型中选一个。
2. 素材标签：从上方「素材标签」数组中选一个最贴合该镜头的（必须是指定标签之一，不可自创）。
3. visual_prompt：一段中文画面描述，必须以所选 label 开头，具体描写该动作/画面的机位、主体与动作。



请严格输出 JSON，不要 markdown 代码块，不要解释：
{{"beat": "", "素材标签": "", "visual_prompt": ""}}"""


def classify_beat_rule(text: str) -> str:
    """Simple rule-based beat classification for offline use."""
    scores: dict[str, int] = {k: 0 for k in _BEAT_KEYWORDS}
    for beat, keywords in _BEAT_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[beat] += 1
    if max(scores.values(), default=0) > 0:
        return max(scores, key=scores.get)  # type: ignore[arg-type]
    return "DETAIL"


def visual_prompt_rule(beat: str, text: str, labels: list[str] | None = None) -> str:
    """Offline fallback visual prompt."""
    base = {
        "HOOK": "手持产品入镜或开箱动作，画面有吸引力，能抓住注意力",
        "PRODUCT": "产品正面/包装特写，品牌 logo 和瓶身清晰可见，光线明亮",
        "TASTE": "食物质感特写，酱汁光泽、蒸汽或食欲感强的画面",
        "USAGE": "拌饭、食用或烹饪场景，动作自然，展示使用方式",
        "DETAIL": "配料、食材、包装细节或文字说明类画面",
        "CTA": "产品 prominently 展示，适合结尾引导购买",
    }
    prompt = base.get(beat, "与产品相关的清晰画面")
    return f"{prompt}。台词：「{text}」"


def parse_json_response(text: str) -> dict | None:
    """从模型返回里稳健地解析出 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    # 去掉 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    # 直接整体解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 截取首个 {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def call_ollama_text(
    prompt: str,
    model: str,
    ollama_url: str,
    ollama_key: str,
    temperature: float = 0.0,
    max_tokens: int = 256,
) -> tuple[dict | None, str | None, str | None]:
    """调用 Ollama 文本模型（无需图片），返回 (解析后的dict, 原始文本, 错误信息)。

    与 classify_materials.py 共用同一套 Ollama 约定：
    - OLLAMA_URL 为完整 /api/chat 端点；
    - 请求头带 ``Authorization: Bearer $OLLAMA_API_KEY``；
    - ``format: "json"`` 强制结构化输出。
    """
    if not ollama_url:
        return None, None, "OLLAMA_URL 未配置"
    payload = {
        "model": model,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "format": "json",
        "messages": [
            {"role": "user", "content": prompt.strip()},
        ],
    }
    headers = {
        "Authorization": f"Bearer {ollama_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(ollama_url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
    except Exception as e:  # noqa: BLE001
        return None, None, f"Ollama 调用失败: {e}"

    parsed = parse_json_response(content)
    if parsed is None:
        return None, content, f"无法解析模型返回: {content[:200]}"
    return parsed, content, None


def llm_beat_and_prompt(
    text: str,
    model: str,
    labels: list[str],
    category: str,
    ollama_url: str,
    ollama_key: str,
    temperature: float = 0.0,
    max_tokens: int = 256,
) -> tuple[str, str, str]:
    """Ask the Ollama model for beat + action label + visual prompt.

    失败时静默回退到规则兜底，保证流程不中断。
    """
    labels_text = "\n".join(f"- {x}" for x in labels) if labels else "（无）"
    prompt = _BEAT_PROMPT.format(category=category, labels=labels_text, text=text)

    parsed, raw_text, err = call_ollama_text(
        prompt, model, ollama_url, ollama_key, temperature, max_tokens
    )
    if parsed is None:
        print(f"  [ollama warn] {err}，回退规则兜底")
        beat = classify_beat_rule(text)
        return beat, "", visual_prompt_rule(beat, text, labels)

    beat = str(parsed.get("beat", "DETAIL")).upper()
    if beat not in _BEAT_KEYWORDS:
        beat = classify_beat_rule(text)
    raw_label = str(parsed.get("素材标签", ""))
    label = _normalize_action_label(raw_label, labels)
    visual = str(parsed.get("visual_prompt", "")) or visual_prompt_rule(beat, text, labels)
    return beat, label, visual


def load_metadata_sentences(metadata_path: Path) -> list[dict]:
    """Load metadata and split sentences by commas."""
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    all_words: list[dict] = []
    
    # Collect all words from all sentences
    for event in data.get("events", []):
        sent = event.get("sentence")
        if not sent:
            continue
        words = sent.get("words", [])
        if words:
            all_words.extend(words)
    
    if not all_words:
        return []
    
    # Build full text from words and collect all start/end times
    full_text = ""
    word_positions = []  # (start_char, end_char, word_data)
    for word in all_words:
        word_text = word.get("word", "")
        if not word_text:
            continue
        start_char = len(full_text)
        full_text += word_text
        end_char = len(full_text)
        word_positions.append((start_char, end_char, word))
    
    # Split by commas (both Chinese and English commas)
    split_pattern = r"([，,])"
    parts = re.split(split_pattern, full_text)
    
    # Reconstruct segments with their commas
    segments: list[tuple[str, int, int]] = []
    current_start = 0
    for i in range(0, len(parts), 2):
        text_part = parts[i]
        comma_part = parts[i+1] if i+1 < len(parts) else ""
        
        if text_part or comma_part:
            segment_text = text_part + comma_part
            if segment_text.strip():
                segment_start = current_start
                segment_end = current_start + len(segment_text)
                segments.append((segment_text.strip(), segment_start, segment_end))
            current_start += len(text_part) + len(comma_part)
    
    # Map each segment to word timings
    sentences: list[dict] = []
    for segment_text, seg_start, seg_end in segments:
        # Find all words that overlap with this segment
        segment_words = []
        for start_char, end_char, word in word_positions:
            # Check if word overlaps with segment
            if not (end_char <= seg_start or start_char >= seg_end):
                segment_words.append(word)
        
        if segment_words:
            sentences.append({
                "start": float(segment_words[0].get("startTime", 0)),
                "end": float(segment_words[-1].get("endTime", 0)),
                "text": segment_text,
            })
    
    return sentences


def _srt_timestamp(seconds: float) -> str:
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_subtitles_from_metadata(metadata_path: Path, out_path: Path) -> None:
    """Generate SRT subtitles from TTS metadata JSON, grouped by commas (same as shots)."""
    # Reuse the same logic as load_metadata_sentences to group by commas
    sentences = load_metadata_sentences(metadata_path)
    
    if not sentences:
        out_path.write_text("", encoding="utf-8")
        return
    
    # Generate SRT with one cue per shot/sentence
    srt_lines: list[str] = []
    for idx, sent in enumerate(sentences, start=1):
        text = sent["text"]
        # Strip trailing punctuation for cleaner uppercase look
        text = text.rstrip(".,!?;:，。！？；：")
        text = text.upper()
        
        srt_lines.append(str(idx))
        srt_lines.append(f"{_srt_timestamp(sent['start'])} --> {_srt_timestamp(sent['end'])}")
        srt_lines.append(text)
        srt_lines.append("")
    
    out_path.write_text("\n".join(srt_lines), encoding="utf-8")


def build_shots(
    sentences: list[dict],
    model: str | None,
    labels: list[str],
    category: str,
    ollama_url: str,
    ollama_key: str,
    temperature: float = 0.0,
    max_tokens: int = 256,
) -> list[dict]:
    """Build shots from sentences with beat, action label and visual prompt."""
    shots = []
    for i, sent in enumerate(sentences):
        if model:
            beat, label, visual = llm_beat_and_prompt(
                sent["text"], model, labels, category, ollama_url, ollama_key,
                temperature, max_tokens,
            )
        else:
            beat = classify_beat_rule(sent["text"])
            label = ""
            visual = visual_prompt_rule(beat, sent["text"], labels)
        start = round(sent["start"], 3)
        if i > 0:
            start = shots[i-1]["end"]
        end = round(sent["end"], 3)
        shots.append({
            "shot_id": f"S{i+1:02d}",
            "start": start,
            "end": end,
            "duration": round(end - start, 3),
            "text": sent["text"],
            "beat": beat,
            "label": label,
            "visual_prompt": visual,
        })
    return shots


def build_strategy(
    metadata_path: Path,
    edit_dir: Path,
    model: str | None,
    labels: list[str],
    category: str,
    ollama_url: str,
    ollama_key: str,
    temperature: float = 0.0,
    max_tokens: int = 256,
) -> dict:
    """Build editing strategy from metadata JSON."""
    sentences = load_metadata_sentences(metadata_path)
    total_duration = sentences[-1]["end"] if sentences else 0

    # Generate subtitles
    subs_path = edit_dir / "subtitles.srt"
    generate_subtitles_from_metadata(metadata_path, subs_path)

    return {
        "version": 1,
        "source": str(metadata_path.resolve()),
        "category": category,
        "model": model or "rule-based(offline)",
        "labels": labels,
        "total_duration_s": round(total_duration, 3),
        "subtitles": str(subs_path.resolve()),
        "shots": build_shots(
            sentences, model, labels, category, ollama_url, ollama_key,
            temperature, max_tokens,
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate editing strategy from metadata")
    ap.add_argument("script", type=Path, help="metadata.json file")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Output dir")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Ollama 模型（如 qwen3-vl:8b）；加 --offline 可跳过模型用规则兜底")
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama API 地址（默认从 .env 的 OLLAMA_URL 读取）")
    ap.add_argument("--ollama-api-key", default=DEFAULT_OLLAMA_API_KEY, help="Ollama API Key（默认从 .env 的 OLLAMA_API_KEY 读取）")
    ap.add_argument("--offline", action="store_true",
                    help="Use rule-based fallback instead of calling the model")
    ap.add_argument("--temperature", type=float, default=0.0, help="模型采样温度")
    ap.add_argument("--max-tokens", type=int, default=256, help="模型最大输出 token 数")
    ap.add_argument("--category", default=DEFAULT_CATEGORY,
                    help="大分类（决定从 labels.json 取哪组素材标签，也用于提示词）")
    ap.add_argument("--labels-file", type=Path, default=DEFAULT_LABELS_FILE,
                    help="分类标签配置文件（大分类→标签列表 映射）；默认用本 skill 根目录 labels.json")
    ap.add_argument("--labels", type=str, default=None,
                    help="显式指定候选素材标签（逗号分隔），优先级高于 labels 配置文件")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    edit_dir.mkdir(parents=True, exist_ok=True)

    model = None if args.offline else args.model
    explicit = [x.strip() for x in args.labels.split(",") if x.strip()] if args.labels else None
    labels = resolve_labels(args.labels_file, args.category, explicit)
    print(f"[labels] 大分类「{args.category}」→ {labels}")
    strategy = build_strategy(
        args.script, edit_dir, model, labels, args.category,
        args.ollama_url, args.ollama_api_key, args.temperature, args.max_tokens,
    )
    out_path = edit_dir / "strategy.json"
    out_path.write_text(json.dumps(strategy, indent=2, ensure_ascii=False), encoding="utf-8")

    # Also write a human-readable summary
    md_lines = [f"# 剪辑策略（{strategy['total_duration_s']:.2f}s · 大分类：{strategy.get('category','')}）", ""]
    md_lines.extend([
        f"共 {len(strategy['shots'])} 个镜头",
        f"素材标签候选：{'、'.join(strategy.get('labels', []))}",
        "",
        "| 镜头 | 时间 | 时长 | 类型 | 素材标签 | 台词 | 建议画面 |",
        "|------|------|------|------|---------|------|----------|",
    ])
    for s in strategy["shots"]:
        md_lines.append(
            f"| {s['shot_id']} | {s['start']:.2f}-{s['end']:.2f} | "
            f"{s['duration']:.2f}s | {s['beat']} | {s.get('label','')} | {s['text']} | {s['visual_prompt']} |"
        )
    md_lines.append("")

    md_path = edit_dir / "strategy.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"strategy → {out_path}")
    print(f"summary  → {md_path}")
    print(f"subtitles → {edit_dir / 'subtitles.srt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

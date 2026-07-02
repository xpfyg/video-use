#!/usr/bin/env python3
"""Generate editing strategy / shot-list from TTS metadata.

The strategy is **audio-first**: the dubbing track decides the structure and
runtime. For each phrase separated by comma we produce a shot entry with timing,
beat label, and a visual prompt describing what kind of footage should accompany it.

Usage:
    python helpers/generate_strategy.py test_materials/metadata.json --edit-dir ./edit_test

Output: edit_test/strategy.json with shot list.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_MODEL = "mlx-community/Qwen2-VL-2B-Instruct-4bit"

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

_BEAT_PROMPT = """你是一名零食带货短视频剪辑师。请为下面这句口播台词判断镜头类型并给出画面建议。
从以下标签中选择一个最贴合的 beat：
- HOOK：吸引注意力的开头
- PRODUCT：产品/包装展示
- TASTE：味道、口感、食欲感
- USAGE：食用/烹饪/搭配场景
- DETAIL：配料、食材、细节
- CTA：引导购买/行动号召

并用简短中文（30字以内）写出该镜头应该呈现什么样的画面,
比如提到配料的话就建议特写配料表的镜头,
提到口味就建议吃或者展示食物的镜头
提到包装和和产品就展示开箱或者产品特写镜头

台词：「{text}」

请严格输出JSON，不要markdown代码块，不要解释：
{{"beat": "", "visual_prompt": ""}}"""


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


def visual_prompt_rule(beat: str, text: str) -> str:
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


def llm_beat_and_prompt(text: str, model: str) -> tuple[str, str]:
    """Ask the local text model for beat label + visual prompt."""
    prompt = _BEAT_PROMPT.format(text=text)
    cmd = [
        sys.executable, "-m", "mlx_vlm.generate",
        "--model", model,
        "--max-tokens", "128",
        "--temp", "0.2",
        "--prompt", prompt,
    ]
    env = {**__import__("os").environ, "HF_HUB_OFFLINE": "1"}
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        print(f"  llm warning: {result.stderr[:200]}")
        beat = classify_beat_rule(text)
        return beat, visual_prompt_rule(beat, text)

    raw = result.stdout
    marker = "<|im_start|>assistant\n"
    if marker in raw:
        raw = raw.split(marker, 1)[1]
    raw = raw.replace("```json", "").replace("```", "").strip()

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        beat = classify_beat_rule(text)
        return beat, visual_prompt_rule(beat, text)

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        beat = classify_beat_rule(text)
        return beat, visual_prompt_rule(beat, text)

    beat = str(parsed.get("beat", "DETAIL")).upper()
    if beat not in _BEAT_KEYWORDS:
        beat = classify_beat_rule(text)
    visual = str(parsed.get("visual_prompt", visual_prompt_rule(beat, text)))
    return beat, visual


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


def build_shots(sentences: list[dict], model: str | None) -> list[dict]:
    """Build shots from sentences with beat and visual prompt."""
    shots = []
    for i, sent in enumerate(sentences):
        beat, visual = llm_beat_and_prompt(sent["text"], model) if model else (
            classify_beat_rule(sent["text"]),
            visual_prompt_rule(classify_beat_rule(sent["text"]), sent["text"]),
        )
        shots.append({
            "shot_id": f"S{i+1:02d}",
            "start": round(sent["start"], 3),
            "end": round(sent["end"], 3),
            "duration": round(sent["end"] - sent["start"], 3),
            "text": sent["text"],
            "beat": beat,
            "visual_prompt": visual,
        })
    return shots


def build_strategy(metadata_path: Path, edit_dir: Path, model: str | None) -> dict:
    """Build editing strategy from metadata JSON."""
    sentences = load_metadata_sentences(metadata_path)
    total_duration = sentences[-1]["end"] if sentences else 0

    return {
        "version": 1,
        "source": str(metadata_path.resolve()),
        "total_duration_s": round(total_duration, 3),
        "shots": build_shots(sentences, model),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate editing strategy from metadata")
    ap.add_argument("script", type=Path, help="metadata.json file")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Output dir")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Local mlx-vlm model for beat/prompt generation; use --offline to skip")
    ap.add_argument("--offline", action="store_true",
                    help="Use rule-based fallback instead of calling the model")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    edit_dir.mkdir(parents=True, exist_ok=True)

    model = None if args.offline else args.model
    strategy = build_strategy(args.script, edit_dir, model)
    out_path = edit_dir / "strategy.json"
    out_path.write_text(json.dumps(strategy, indent=2, ensure_ascii=False), encoding="utf-8")

    # Also write a human-readable summary
    md_lines = [f"# 剪辑策略（{strategy['total_duration_s']:.2f}s）", ""]
    md_lines.extend([
        f"共 {len(strategy['shots'])} 个镜头",
        "",
        "| 镜头 | 时间 | 时长 | 类型 | 台词 | 建议画面 |",
        "|------|------|------|------|------|----------|",
    ])
    for s in strategy["shots"]:
        md_lines.append(
            f"| {s['shot_id']} | {s['start']:.2f}-{s['end']:.2f} | "
            f"{s['duration']:.2f}s | {s['beat']} | {s['text']} | {s['visual_prompt']} |"
        )
    md_lines.append("")

    md_path = edit_dir / "strategy.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"strategy → {out_path}")
    print(f"summary  → {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

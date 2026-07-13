#!/usr/bin/env python3
"""对中间产物 cache 中的素材目录抽帧并做动作分类，生成素材分类标签文件。

本脚本服务于「带货短视频」素材动作分类工作流（默认以冷饮为例，标签可由 labels.json 按大分类配置）：
1. 遍历中间产物目录的 cache（默认 ``<edit-dir>/cache/frames/<素材>/``），
   每个素材目录对应一条原始拍摄素材（clip）。
2. 对每个素材目录**抽取 N 张代表性图片**（默认最多 6 张，按视频时长缩放：
   2–6 张，时长约每 5 秒 +1 张），按时间轴均匀取样，
   拷贝到 ``<cache>/selected/<素材>/`` 便于人工复核。
3. 将这 N 张图发给 Ollama 视觉模型（默认 ``qwen3-vl:8b``），
   让其从给定动作标签里**选一个**作为该素材的分类标签。
4. 产出机器可读的 ``material_labels.json`` 与人工可读的 ``material_labels.md``。

帧布局与 ``helpers/analyze_visual.py`` 保持一致：
    cache/frames/<素材>/frame_0000_<t>s.jpg

用法：
    # 标准：基于 edit 目录的 cache 进行分类（默认用 labels.json 的「冷饮」标签组）
    python helpers/classify_materials.py --edit-dir ./edit

    # 通过「大分类」选择 labels.json 中的标签组
    python helpers/classify_materials.py --edit-dir ./edit --category 冷饮
    python helpers/classify_materials.py --edit-dir ./edit --category 商品

    # 指定自己的 labels 配置文件（大分类→标签列表 映射）
    python helpers/classify_materials.py --edit-dir ./edit --labels-file ./my_labels.json --category 商品

    # 直接显式给出候选标签（逗号分隔，优先级最高，忽略配置文件）
    python helpers/classify_materials.py --edit-dir ./edit --labels 拿起产品,放下产品,开盖展示

    # 指定素材目录（任意包含「素材子目录」的目录）
    python helpers/classify_materials.py --materials-dir ./edit/cache/frames

    # 强制重新分类（忽略已有标签文件）
    python helpers/classify_materials.py --edit-dir ./edit --force

    # 没有视觉模型时先跑一遍，仅抽帧 + 标记待复核
    python helpers/classify_materials.py --edit-dir ./edit --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import os
import requests

# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "qwen3-vl:8b"
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "")
DEFAULT_OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
DEFAULT_CATEGORY = "冷饮"

# 分类标签配置文件（大分类 → 标签列表 映射），默认位于 skill 根目录
DEFAULT_LABELS_FILE = (Path(__file__).resolve().parents[1] / "labels.json")

# 冷饮产品带货短视频常见镜头动作标签（从其中选一个；labels.json 不可用时兜底）
DEFAULT_LABELS = [
    "拿起产品",
    "放下产品",
    "开盖展示",
    "饮料气泡特写",
    "配料表",
    "倒饮品动作",
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}

# 每个标签的识别要点，喂给视觉模型做判别
LABEL_HINTS = {
    "拿起产品": "手部将饮料/冷饮产品从桌面、包装或货架中拿起，主体动作是「拿起/抓取」商品。",
    "放下产品": "手部将饮料/冷饮产品放回桌面/货架/包装，主体动作是「放下/放置」商品。",
    "开盖展示": "拧开瓶盖、拉开拉环、撕开封口等开盖动作，并展示瓶口/内部，主体动作是「开盖/开封展示」。",
    "饮料气泡特写": "近景微距拍摄杯中/瓶中饮料液体、上升的气泡、冰块的特写镜头，主体是被摄液体/气泡本身。",
    "配料表": "镜头对准产品包装上的配料表、营养成分表、规格文字区域，静态展示包装印刷文字。",
    "倒饮品动作": "向杯中/容器中倾倒饮料液体，液体流入、注水的连续动作，主体动作是「倒/倾倒」。",
    # 商品分类标签识别要点
    "拆包装": "手部撕开/剪开商品的包装（盒、袋、塑封等），主体动作是「拆/开封包装」。",
    "倒出零食": "将包装里的零食、颗粒、干货等倒入容器或手中，主体动作是「倒出/倾倒内容物」。",
    "拆袋展示": "拆开包装袋并展示袋内商品/内容物，露出产品本身，主体是「拆袋 + 展示」。",
    "食物细节": "近景特写拍摄食物/零食的切面、纹理、色泽、颗粒等细节，主体是食物本身而非动作。",
}


# ---------------------------------------------------------------------------
# 帧发现与抽取
# ---------------------------------------------------------------------------
def parse_frame_time(path: Path) -> float:
    """从 ``frame_0000_12.340s.jpg`` 这样的文件名里解析时间戳（秒）。"""
    m = re.search(r"_(\d+(?:\.\d+)?)s\.", path.name)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # 退路：frame_0000_... 取中间数字段
    parts = path.stem.split("_")
    for p in parts:
        try:
            return float(p)
        except ValueError:
            continue
    return 0.0


def gather_images(material_dir: Path) -> list[Path]:
    """递归收集素材目录下的图片文件。"""
    out: list[Path] = []
    for p in sorted(material_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            out.append(p)
    return out


def find_video(material_dir: Path) -> Path | None:
    """若素材目录里没有图片但有一个视频，返回它。"""
    videos = [p for p in sorted(material_dir.iterdir())
              if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    return videos[0] if videos else None


def select_representative(frames: list[Path], n: int) -> list[Path]:
    """按时间轴均匀取 N 帧；不足 N 张则全取。"""
    if len(frames) <= n:
        return list(frames)
    # 先按时间戳排序
    ordered = sorted(frames, key=parse_frame_time)
    step = (len(ordered) - 1) / (n - 1)
    idxs = [round(i * step) for i in range(n)]
    # 去重保序
    seen, picked = set(), []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            picked.append(ordered[i])
    return picked


def extract_frames_from_video(video: Path, n: int, out_dir: Path) -> list[Path]:
    """用 ffmpeg 从视频里按时间轴均匀抽 N 张图（最长边 480）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    # 取时长
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        duration = float(json.loads(probe.stdout)["format"]["duration"] or 0)
    except Exception:
        duration = 0.0
    if duration <= 0:
        return []
    times = [duration * (i + 0.5) / n for i in range(n)]
    saved: list[Path] = []
    for i, t in enumerate(times):
        dst = out_dir / f"frame_{i:04d}_{t:.3f}s.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-nostats", "-ss", f"{t:.3f}",
             "-i", str(video), "-frames:v", "1", "-q:v", "3", str(dst)],
            capture_output=True, text=True, check=False,
        )
        if dst.exists():
            saved.append(dst)
    return saved


def frames_for_duration(duration: float | None, cap: int) -> int:
    """按视频时长缩放每素材取帧数：约每 5 秒 +1 张，夹取到 [2, cap]。

    - 极短视频（<5s）→ 2 张
    - 10s → 3 张；18s → 4 张；30s → 6 张
    - 长视频封顶 cap（默认 6）
    """
    cap = max(2, int(cap))
    if not duration or duration <= 0:
        return cap
    n = int(duration // 5) + 1
    return max(2, min(cap, n))


def copy_selected(srcs: list[Path], selected_dir: Path, material: str) -> list[dict]:
    """把选中的帧拷贝到 selected/<material>/，返回带相对路径的记录。"""
    dest_root = selected_dir / material
    dest_root.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for i, src in enumerate(srcs):
        dst = dest_root / f"sel_{i:02d}_{src.name}"
        try:
            shutil.copy2(src, dst)
        except Exception:
            dst = src  # 拷贝失败则直接引用原图
        records.append({
            "time": round(parse_frame_time(src), 3),
            "file": str(dst),
        })
    return records


# ---------------------------------------------------------------------------
# Ollama 视觉分类
# ---------------------------------------------------------------------------
def build_prompt(n: int, category: str, labels: list[str]) -> str:
    hints = "\n".join(f"- {lab}：{LABEL_HINTS.get(lab, '')}" for lab in labels)
    label_list = "、".join(labels)
    return f"""你是一名{ category }带货短视频的镜头动作标注员。
下面这 {n} 张图片来自同一条{ category }带货短视频素材的连续画面（已按时间顺序排列）。
请判断这条素材整体最匹配下列哪一个「动作标签」，只能从给定列表中选择一个最贴切的。

可选标签与识别要点：
{hints}

严格要求：
1. 只能从以下标签中选择一个，禁止自创标签：{label_list}
2. 综合全部图片判断，选最能代表该素材主体动作的那一个标签。
3. 输出严格 JSON，不要任何额外文字、不要 markdown 代码块：
{{"label": "所选标签", "confidence": 0.0到1.0之间的小数, "reason": "不超过40字的中文理由"}}
"""


def encode_image_to_base64(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_ollama_vlm(
    image_paths: list[Path],
    labels: list[str],
    model: str,
    ollama_url: str,
    ollama_key: str,
    category: str,
    temperature: float = 0.0,
    max_tokens: int = 256,
) -> tuple[dict | None, str | None, str | None]:
    """调用 Ollama 视觉模型，返回 (解析后的dict, 原始文本, 错误信息)。"""
    if not image_paths:
        return None, None, "no images"
    if not ollama_url:
        return None, None, "OLLAMA_URL 未配置"

    img_b64 = [encode_image_to_base64(str(p)) for p in image_paths]
    prompt = build_prompt(len(image_paths), category, labels)
    payload = {
        "model": model,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "format": "json",
        "messages": [
            {"role": "user", "content": prompt.strip(), "images": img_b64}
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
    except Exception as e:
        return None, None, f"Ollama 调用失败: {e}"

    parsed = parse_json_response(content)
    if parsed is None:
        return None, content, f"无法解析模型返回: {content[:200]}"
    return parsed, content, None


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


def normalize_label(raw: str, labels: list[str]) -> str | None:
    """把模型返回的标签归一到候选集合；匹配不上返回 None。"""
    if not raw:
        return None
    raw = raw.strip().strip("。.!！?？\"'“”‘’[]【】()（）")
    if not raw:
        return None
    if raw in labels:
        return raw
    # 去标点/空格后比对
    norm = lambda s: re.sub(r"[\s\-_:：、，。.！!？?]+", "", s)
    rn = norm(raw)
    for lab in labels:
        if norm(lab) == rn:
            return lab
    # 子串包含（双向）
    for lab in labels:
        if lab in raw or raw in lab:
            return lab
    return None


def recover_label_from_text(text: str | None, labels: list[str]) -> str | None:
    """标签字段缺失/非法时，从模型原始文本（含 reason）里搜候选标签。"""
    if not text:
        return None
    # 整段文本里出现最多的候选标签胜出
    hits = [lab for lab in labels if lab in text]
    if hits:
        # 优先出现在「理由/reason」附近的标签：简单取首个命中
        return hits[0]
    return None


# ---------------------------------------------------------------------------
# 标签解析（大分类 → 标签列表）
# ---------------------------------------------------------------------------
def load_labels_for_category(path: Path, category: str) -> list[str]:
    """从 labels 配置（大分类→标签列表 映射）按 category 取出标签列表。

    兼容三种格式：
      - 纯 JSON 数组：直接作为标签列表返回（忽略 category）。
      - {"labels": [...]}：单分类旧格式，直接返回。
      - {"大分类": [...], ...}：按 category 取；category 不存在则抛 KeyError。
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
    """确定本次使用的候选标签列表，优先级：
    1. --labels 显式列表；
    2. --labels-file（默认 skill 根目录 labels.json）按 category 取；
    3. 兜底硬编码 DEFAULT_LABELS（仅当配置文件不可用）。
    """
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def discover_materials(frames_root: Path, materials_dir: Path | None) -> list[tuple[str, Path]]:
    """返回 [(素材名, 素材目录), ...]。"""
    root = materials_dir if materials_dir else frames_root
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir():
            out.append((p.name, p))
    return out


def load_existing(output: Path) -> dict:
    if output.exists():
        try:
            return json.loads(output.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="素材动作分类标签生成（Ollama 视觉模型）")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="中间产物目录")
    ap.add_argument("--cache-dir", type=Path, default=None, help="cache 目录（默认 <edit-dir>/cache）")
    ap.add_argument("--frames-root", type=Path, default=None, help="素材目录根（默认 <cache>/frames）")
    ap.add_argument("--materials-dir", type=Path, default=None, help="直接指定素材目录根（覆盖 frames-root）")
    ap.add_argument("--output", type=Path, default=None, help="标签 JSON 输出（默认 <edit-dir>/material_labels.json）")
    ap.add_argument("--output-md", type=Path, default=None, help="标签 MD 输出（默认 <edit-dir>/material_labels.md）")
    ap.add_argument("--selected-dir", type=Path, default=None, help="选中帧拷贝目录（默认 <cache>/selected）")
    ap.add_argument("--num-frames", type=int, default=6, help="每素材最多抽取图片数（实际按视频时长缩放为 2–6 张）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Ollama 视觉模型")
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama API 地址")
    ap.add_argument("--ollama-api-key", default=DEFAULT_OLLAMA_API_KEY, help="Ollama API Key")
    ap.add_argument("--category", default=DEFAULT_CATEGORY, help="大分类（决定从 labels.json 取哪组标签，也用于提示词）")
    ap.add_argument("--labels-file", type=Path, default=DEFAULT_LABELS_FILE, help="分类标签配置文件（大分类→标签列表 映射）；默认用本 skill 根目录 labels.json")
    ap.add_argument("--labels", type=str, default=None, help="显式指定候选标签（逗号分隔），优先级高于 labels 配置文件")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--force", action="store_true", help="忽略已有标签文件，全部重分类")
    ap.add_argument("--allow-unclassified", action="store_true", help="模型不可用时仍写出 null 标签并标记 needs_review")
    ap.add_argument("--dry-run", action="store_true", help="仅抽帧，不调用模型")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    cache_dir = (args.cache_dir or (edit_dir / "cache")).resolve()
    frames_root = (args.frames_root or (cache_dir / "frames")).resolve()
    selected_dir = (args.selected_dir or (cache_dir / "selected")).resolve()
    output = (args.output or (edit_dir / "material_labels.json")).resolve()
    output_md = (args.output_md or (edit_dir / "material_labels.md")).resolve()

    # 候选标签：按 --category（大分类）从 labels.json 取对应的一组标签
    explicit = [x.strip() for x in args.labels.split(",") if x.strip()] if args.labels else None
    labels = resolve_labels(args.labels_file, args.category, explicit)

    selected_dir.mkdir(parents=True, exist_ok=True)

    # 已存在的标签（缓存）
    existing = load_existing(output)
    existing_materials = existing.get("materials", {}) if isinstance(existing, dict) else {}

    materials = discover_materials(frames_root, args.materials_dir)
    if not materials:
        print(f"[warn] 在 {frames_root} 下未找到任何素材目录")
        return 1

    # 读取 visual_report 获取每素材时长，用于按时长缩放取帧数
    vr_clips = {}
    vr = load_json(edit_dir / "visual_report.json")
    if isinstance(vr, dict):
        vr_clips = vr.get("clips", {})

    results: dict[str, dict] = {}
    # 保留已有但本次未覆盖的条目（避免 --force 之外误删）
    for name, entry in existing_materials.items():
        results[name] = entry

    print(f"[discover] 找到 {len(materials)} 个素材目录")

    for name, mdir in materials:
        # 缓存命中：已分类且无需复核，跳过
        if not args.force and name in existing_materials:
            prev = existing_materials[name]
            if isinstance(prev, dict) and prev.get("label") and not prev.get("needs_review"):
                print(f"[cache] {name} → {prev.get('label')}")
                continue

        dur = (vr_clips.get(name, {}) or {}).get("duration")
        n = frames_for_duration(dur, args.num_frames)

        images = gather_images(mdir)
        if not images:
            video = find_video(mdir)
            if video:
                print(f"[extract] {name} 从视频抽帧（时长 {dur}s → 取 {n} 张）")
                images = extract_frames_from_video(video, n, selected_dir / name)
            if not images:
                print(f"[skip] {name} 无图片也无视频")
                results[name] = {
                    "label": None, "confidence": None, "reason": "无可用帧",
                    "needs_review": True, "source_dir": str(mdir), "selected_frames": [],
                }
                continue

        selected = select_representative(images, n)
        frame_records = copy_selected(selected, selected_dir, name)
        print(f"[frames] {name}: 时长 {dur}s → 从 {len(images)} 张中取 {len(selected)} 张")

        if args.dry_run:
            results[name] = {
                "label": None, "confidence": None, "reason": "dry-run 未调用模型",
                "needs_review": True, "source_dir": str(mdir.relative_to(edit_dir) if mdir.is_relative_to(edit_dir) else mdir),
                "selected_frames": frame_records,
            }
            continue

        parsed, raw_text, err = call_ollama_vlm(
            selected, labels, args.model, args.ollama_url, args.ollama_api_key,
            args.category, args.temperature, args.max_tokens,
        )
        if parsed is None:
            if args.allow_unclassified:
                print(f"[unclassified] {name}: {err}")
                results[name] = {
                    "label": None, "confidence": None, "reason": err,
                    "needs_review": True,
                    "source_dir": str(mdir.relative_to(edit_dir) if mdir.is_relative_to(edit_dir) else mdir),
                    "selected_frames": frame_records,
                }
                continue
            else:
                print(f"[error] {name}: {err}", file=sys.stderr)
                return 2

        raw_label = str(parsed.get("label", ""))
        label = normalize_label(raw_label, labels)
        # 标签字段为空/非法时，尝试从原始文本（含 reason）里找回
        if label is None:
            label = recover_label_from_text(raw_text, labels)
        reason = str(parsed.get("reason", ""))
        try:
            conf = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
            if label is not None:
                conf = 0.5  # 文本找回时给一个默认中等置信
        needs_review = label is None
        if label is None:
            reason = f"模型返回不在候选集: {raw_label}。" + reason
        print(f"[classify] {name} → {label} (conf={conf:.2f})")

        results[name] = {
            "label": label,
            "confidence": round(conf, 2),
            "reason": reason,
            "needs_review": needs_review,
            "source_dir": str(mdir.relative_to(edit_dir) if mdir.is_relative_to(edit_dir) else mdir),
            "selected_frames": frame_records,
        }

    # 写 JSON
    report = {
        "version": 1,
        "category": args.category,
        "model": args.model,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_labels": labels,
        "num_frames_per_material": args.num_frames,
        "materials": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # 写 MD
    write_markdown(output_md, report)

    print(f"\n标签文件 → {output}")
    print(f"可读文档 → {output_md}")
    return 0


def write_markdown(path: Path, report: dict) -> None:
    lines = []
    lines.append(f"# 素材分类标签 · {report['category']}带货\n")
    lines.append(f"- 模型：`{report['model']}`")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 候选标签：{'、'.join(report['candidate_labels'])}")
    lines.append(f"- 每素材抽帧数：{report['num_frames_per_material']}\n")

    mats = report["materials"]
    lines.append("## 汇总\n")
    lines.append("| 素材 | 标签 | 置信度 | 复核 | 理由 |")
    lines.append("|------|------|--------|------|------|")
    for name, e in mats.items():
        label = e.get("label") or "—"
        conf = e.get("confidence")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
        rev = "⚠️" if e.get("needs_review") else "✅"
        reason = (e.get("reason") or "").replace("|", "/")
        lines.append(f"| {name} | {label} | {conf_s} | {rev} | {reason} |")
    lines.append("")

    lines.append("## 明细\n")
    for name, e in mats.items():
        label = e.get("label") or "（未分类）"
        lines.append(f"### {name} → {label}\n")
        lines.append(f"- 置信度：{e.get('confidence')}")
        lines.append(f"- 需复核：{'是' if e.get('needs_review') else '否'}")
        lines.append(f"- 理由：{e.get('reason')}")
        lines.append(f"- 素材目录：{e.get('source_dir')}")
        fr = e.get("selected_frames") or []
        if fr:
            lines.append(f"- 抽取帧（{len(fr)} 张）：")
            for f in fr:
                lines.append(f"  - {f.get('time')}s → {f.get('file')}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

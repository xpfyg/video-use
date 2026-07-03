#!/usr/bin/env python3
"""Generate natural-language content descriptions for video clips.

Reads ``edit/visual_report.json`` (which now contains every sampled frame at
1fps with timestamps), calls a local vision-language model for each 3-second
window, and writes ``edit/content_report.json`` plus a human-readable
``edit/visual_catalog.md``.

The goal is a time-stamped material document: for every 1-3 seconds of footage
we know what the visual model sees, so a text-only model can later match clips
to dubbing sentences at the right timestamp.

Usage:
    python helpers/analyze_content.py --edit-dir ./edit_test
    python helpers/analyze_content.py --edit-dir ./edit_test --model mlx-community/Qwen2-VL-2B-Instruct-4bit
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import requests
import subprocess
import sys
import os
from pathlib import Path
from dotenv import load_dotenv


# 加载 .env 文件
load_dotenv()

DEFAULT_MODEL = "mlx-community/Qwen2-VL-2B-Instruct-4bit"
DEFAULT_OLLAMA_MODEL = "qwen3-vl:8b"
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
WINDOW_SIZE = 3.0  # seconds

WINDOW_PROMPT = """
硬性前置规则（最高优先级，违反无效）：
1.仅描述三张图片里肉眼可见像素内容，**绝对禁止推测、联想、脑补、预判后续动作**，没出现的动作一律不提；
2.严格区远景,中景,近景，
3.输出格式,字数≤60字，格式：(远景/中景/近景)+ 镜头分类 +客观画面描述，无多余解释。
这{count}张图片来自同一条零食带货短视频素材的连续画面，时间范围约为第{start:.1f}秒到第{end:.1f}秒。
请用中文总结这{count}秒内展示了什么内容，
下面是一些关键镜头标签参考提示：
标签1. 展示配料表、产品包装 提示
画面主体为食品完整外包装盒 / 密封包装袋，镜头对准包装印刷区域，清晰展示品牌 logo、配料表、营养成分表、产品规格，静态静物拍摄，焦点固定在包装文字与外观，属于产品外包装合规展示镜头。
标签2. 开箱拆袋
第一人称 / 第三人称手部出镜，动作是撕开产品外层塑封、撕开食品密封袋、打开外包装纸盒，属于拆封开箱动态镜头，原始未拆新品拆解过程，核心识别动作：撕袋、开封、拆包装。
标签3. 拿出来、摆出来 提示
手部从包装袋 / 包装盒内取出食品原料，将取出食材规整摆放在桌面，整理陈列产品，属于产品取出 + 桌面摆盘展示镜头，识别动作：取物、摆放、整理陈列。
标签4. 包装内食物展示 提示
包装袋半撑开状态，镜头向内拍摄袋内原装未加工食物、粉料、干货、食材原料，不取出外包装，仅展示包装内部原生食材样貌，微距近距离拍摄袋内食材细节。
标签5. 倒水、搅拌 提示
向容器内倾倒清水 / 热水，手持搅拌勺进行顺时针搅拌冲泡，液体融合、粉料溶解动态过程，识别连续动作：注水、倾倒液体、搅拌、冲泡调和。
6. 吃一口试吃 提示
人物进食咀嚼，试吃体验镜头，核心行为：入口食用、品尝食物，可识别面部食用神态、食材入口状态。

远景识别提示:远景全景镜头，完整收录整、全套产品、整体拍摄环境，交代整体拍摄场景环境。
中景识别提示:标准中景镜头，手部操作动作与产品主体，兼顾手部动作和商品展示，操作+产品同框核心互动镜头。
近景识别提示:近景局部特写镜头，聚焦手部、食材、包装细节，背景轻微虚化，仅展示局部主体细节，无大范围环境画面，突出食材纹理、文字、动作细节。
"""

OLLAMA_PROMPT = """
这{count}张图片来自同一条零食带货短视频素材的连续画面，时间范围约为第{start:.1f}秒到第{end:.1f}秒。
硬性前置规则（最高优先级，违反无效）：
1.仅描述所有图片里肉眼可见像素内容，绝对禁止推测、联想、脑补、预判后续动作，没出现的动作一律不提；
2.严格区分远景,中景,近景
3.如果画面素材不佳,虚焦,无意义的镜头,没有拍摄到商品,标记为废片
4.描述每一秒的画面内容,输出格式,字数≤60字，格式：第几秒+(远景/中景/近景)+ 镜头标签 + 素材质量+ 客观画面描述,不要联想。
5.输出总结画面内容,给出有废片的时间范围,没有则不用格式：第几秒到第几秒
镜头标签有如下:
展示产品包装
展示配料表
开箱
拆袋展示食物
拿出食物
倒水
搅拌
试吃
---
远景识别提示:远景全景镜头，完整收录整、全套产品、整体拍摄环境，交代整体拍摄场景环境。
中景识别提示:标准中景镜头，手部操作动作与产品主体，兼顾手部动作和商品展示，操作+产品同框核心互动镜头。
近景识别提示:近景局部特写镜头，聚焦手部、食材、包装细节，背景轻微虚化，仅展示局部主体细节，无大范围环境画面，突出食材纹理、文字、动作
"""


def encode_image_to_base64(file_path: str) -> str:
    """本地图片转base64字符串"""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def describe_clip_with_ollama(
    image_paths: list[Path],
    model: str,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    temperature: float = 0.1,
    max_tokens: int = 512,
) -> str:
    """Run Ollama VLM on all frames of a clip and return description."""
    if not image_paths:
        return ""
    
    # 批量转base64
    img_b64_list = [encode_image_to_base64(str(p)) for p in image_paths]
    
    # 构建提示词
    count = len(image_paths)
    start = 0.0
    # 从文件名提取最后一个时间
    end = 0.0
    try:
        last_path = image_paths[-1]
        # 解析文件名如 frame_0013_13.000s.jpg
        parts = str(last_path.stem).split('_')
        if len(parts) >= 3:
            end = float(parts[2].replace('s', ''))
    except:
        pass
    
    prompt = OLLAMA_PROMPT.format(
        count=count,
        start=start,
        end=end,
    )
    
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt.strip(),
                "images": img_b64_list
            }
        ]
    }
    
    headers = {
        "Authorization": f"Bearer {OLLAMA_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        print(f"  calling Ollama with {len(img_b64_list)} images...")
        resp = requests.post(ollama_url, json=payload,headers=headers)
        resp.raise_for_status()
        res_data = resp.json()
        return res_data["message"]["content"]
    except Exception as e:
        print(f"      Ollama error: {e}")
        return ""


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def resolve_path(rel: str, edit_dir: Path) -> Path | None:
    p = Path(rel)
    if not p.is_absolute():
        p = edit_dir / p
    return p if p.exists() else None


def describe_window(
    image_paths: list[Path],
    start: float,
    end: float,
    model: str,
    max_tokens: int = 128,
) -> str:
    """Run mlx-vlm on 1-3 frames from a time window and return a description."""
    prompt = WINDOW_PROMPT.format(
        count=len(image_paths),
        start=start,
        end=end,
    )
    cmd = [
        sys.executable, "-m", "mlx_vlm.generate",
        "--model", model,
        "--max-tokens", str(max_tokens),
        "--temp", "0.0",
        "--prompt", prompt,
        *[arg for path in image_paths for arg in ("--image", str(path))],
    ]
    env = {**__import__("os").environ, "HF_HUB_OFFLINE": "1"}
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        print(f"      mlx-vlm error [{start:.1f}-{end:.1f}s]: {result.stderr[:200]}")
        return ""

    text = result.stdout
    marker = "<|im_start|>assistant\n"
    if marker in text:
        text = text.split(marker, 1)[1]
    # mlx-vlm appends "==========\nPrompt: ..." after the generated text.
    text = text.split("==========", 1)[0]
    text = text.replace("<|im_end|>", "").strip()
    return text


def build_windows(frames: list[dict], window_size: float) -> list[tuple[float, float, list[dict]]]:
    """Group frames into contiguous time windows."""
    if not frames:
        return []

    duration = frames[-1]["time"]
    windows: list[tuple[float, float, list[dict]]] = []
    start = 0.0
    while start <= duration + 0.01:
        end = start + window_size
        window_frames = [f for f in frames if start <= f["time"] < end]
        if window_frames:
            windows.append((start, min(end, duration + window_size), window_frames))
        start = end
    return windows


def summarize_windows(windows: list[dict]) -> str:
    """Merge window descriptions into a concise clip-level summary."""
    descriptions = [w["description"] for w in windows if w.get("description")]
    if not descriptions:
        return ""
    if len(descriptions) == 1:
        return descriptions[0]

    # Keep one clause from each window to preserve temporal progression.
    clauses: list[str] = []
    for d in descriptions:
        for part in re.split(r"[，。；!！?？]", d):
            part = part.strip()
            if not part or part in clauses:
                continue
            if any(part in existing or existing in part for existing in clauses):
                continue
            clauses.append(part)

    merged = "，".join(clauses)
    if len(merged) > 150:
        merged = "，".join(clauses[:4])
    return merged + "。" if not merged.endswith(("。", "！", "？")) else merged


def analyze_clip_content(
    clip_name: str,
    entry: dict,
    edit_dir: Path,
    model: str,
    use_ollama: bool = False,
    ollama_url: str = DEFAULT_OLLAMA_URL,
) -> dict:
    """Return content descriptions for a clip, using either mlx-vlm or Ollama."""
    # print(f"[content] {clip_name}")
    frames = entry.get("frames", [])
    if not frames:
        print(f"  warning: no sampled frames found for {clip_name}")
        return {"clip_name": clip_name, "summary": "", "windows": [], "model": model}

    if use_ollama:
        # 使用 Ollama，一次处理所有图片
        print(f"  using Ollama mode, processing all {len(frames)} frames...")
        image_paths = [resolve_path(f["file"], edit_dir) for f in frames]
        image_paths = [p for p in image_paths if p]
        
        # if image_paths:
        #     print(f"    image paths: {[str(p) for p in image_paths]}")
        
        desc = describe_clip_with_ollama(
            image_paths, 
            model, 
            ollama_url=ollama_url
        )
        
        # Ollama 模式下，创建一个包含所有帧的 window
        if image_paths:
            start = 0.0
            end = frames[-1]["time"] if frames else 0.0
            enriched_windows = [{
                "start": round(start, 3),
                "end": round(end, 3),
                "description": desc,
                "frames": [f["time"] for f in frames],
            }]
            summary = desc
        else:
            enriched_windows = []
            summary = ""
        
        return {
            "clip_name": clip_name,
            "clip_path": entry.get("file", ""),
            "summary": summary,
            "windows": enriched_windows
        }
    else:
        # 使用原有的 mlx-vlm 窗口模式
        windows = build_windows(frames, WINDOW_SIZE)
        enriched_windows: list[dict] = []

        for start, end, window_frames in windows:
            # Use up to 3 representative frames (roughly one per second).
            selected = window_frames[:3]
            image_paths = [resolve_path(f["file"], edit_dir) for f in selected]
            image_paths = [p for p in image_paths if p]
            if image_paths:
                print(f"    image paths: {[str(p) for p in image_paths]}")
            if not image_paths:
                continue

            print(f"  window {start:.1f}-{end:.1f}s ({len(image_paths)} frame(s))")
            desc = describe_window(image_paths, start, end, model)
            enriched_windows.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "description": desc,
                "frames": [f["time"] for f in selected],
            })

        summary = summarize_windows(enriched_windows)
        return {
            "clip_name": clip_name,
            "summary": summary,
            "windows": enriched_windows,
            "model": model,
            "window_size": WINDOW_SIZE,
            "use_ollama": False,
        }


def build_catalog(report: dict, visual_report: dict | None = None) -> str:
    lines = [
        "# 素材内容文档",
        "",
        "由本地视觉模型（mlx-vlm）按 3 秒窗口自动分析生成，用于后续文本模型匹配口播片段。",
        "",
    ]
    visual_clips = (visual_report or {}).get("clips", {})
    for clip_name, data in report.get("clips", {}).items():
        vinfo = visual_clips.get(clip_name, {})
        scores = vinfo.get("visual_scores", {})
        tier = vinfo.get("quality_tier", "B")
        warnings = vinfo.get("warnings", [])
        duration = vinfo.get("duration", 0)

        lines.append(f"## {clip_name}")
        lines.append(
            f"**质量**：{tier}级 | **时长**：{duration:.1f}s | "
            f"**清晰度**：{scores.get('sharpness_mean', 0):.1f} | "
            f"**稳定度**：{scores.get('stability_score', 0):.2f} | "
            f"**曝光**：{scores.get('exposure_mean', 0):.2f}"
        )
        if warnings:
            lines.append(f"**警告**：{', '.join(warnings)}")
        lines.append(f"**素材总述**：{data.get('summary', '')}")
        lines.append("")
        lines.append("| 时间窗口 | 画面内容 |")
        lines.append("|----------|----------|")
        for w in data.get("windows", []):
            lines.append(f"| {w['start']:.1f}s-{w['end']:.1f}s | {w.get('description', '')} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate clip content descriptions with VLM")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Project edit dir")
    ap.add_argument("--model", help="Model identifier (default: uses Ollama model)")
    ap.add_argument("--window-size", type=float, default=WINDOW_SIZE, help="Seconds per analysis window (mlx-vlm only)")
    ap.add_argument("--max-frame-tokens", type=int, default=128, help="Max tokens for mlx-vlm")
    ap.add_argument("--force", action="store_true", help="Force re-generation")
    ap.add_argument("--use-ollama", action=argparse.BooleanOptionalAction, default=True, help="Use Ollama for analysis (default: True)")
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama API URL")
    ap.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL, help="Ollama model name")
    args = ap.parse_args()

    # Determine which model to use
    if args.use_ollama:
        model = args.model if args.model else args.ollama_model
    else:
        model = args.model if args.model else DEFAULT_MODEL

    edit_dir = args.edit_dir.resolve()
    visual_report = load_json(edit_dir / "visual_report.json")
    if not visual_report:
        print(f"error: {edit_dir / 'visual_report.json'} not found. Run analyze_visual.py first.")
        return 1

    existing_content = load_json(edit_dir / "content_report.json") or {}
    existing_clips = existing_content.get("clips", {})

    report = {
        "version": 1,
        "model": model,
        "use_ollama": args.use_ollama,
        "clips": {},
    }
    if not args.use_ollama:
        report["window_size"] = args.window_size

    for clip_name, entry in visual_report.get("clips", {}).items():
        cached = existing_clips.get(clip_name)
        cached_model = (cached or {}).get("model")
        cached_use_ollama = (cached or {}).get("use_ollama", False)
        
        # Check if cache is valid
        cache_valid = (
            not args.force 
            and cached 
            and cached_model == model 
            and cached_use_ollama == args.use_ollama
        )
        if not args.use_ollama:
            cached_window_size = (cached or {}).get("window_size")
            cache_valid = cache_valid and (cached_window_size == args.window_size)
        
        if cache_valid:
            print(f"[cache] {clip_name}")
            report["clips"][clip_name] = cached
            continue

        report["clips"][clip_name] = analyze_clip_content(
            clip_name, 
            entry, 
            edit_dir, 
            model,
            use_ollama=args.use_ollama,
            ollama_url=args.ollama_url
        )
        # print(report)
    out_path = edit_dir / "content_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\ncontent report → {out_path}")

    catalog_path = edit_dir / "visual_catalog.md"
    catalog_path.write_text(build_catalog(report, visual_report), encoding="utf-8")
    print(f"catalog        → {catalog_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

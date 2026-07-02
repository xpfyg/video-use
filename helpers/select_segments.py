#!/usr/bin/env python3
"""Use a local multimodal LLM (or CV fallback) to select the best ~15s segments.

Reads ``edit/visual_report.json`` and writes ``edit/edl.json``.

Usage:
    # Local multimodal LLM via mlx-vlm
    python helpers/select_segments.py --edit-dir ./edit \
        --backend mlx-vlm \
        --model mlx-community/Qwen2-VL-2B-Instruct-4bit

    # Local multimodal LLM via Ollama REST API
    python helpers/select_segments.py --edit-dir ./edit \
        --backend ollama --model qwen2-vl:2b

    # Deterministic CV-only mode (no LLM, fastest, for testing)
    python helpers/select_segments.py --edit-dir ./edit --backend cv
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests


DEFAULT_PROMPT_TEMPLATE = """你是一名零食带货短视频剪辑师。下面提供了一段素材的关键帧，以及该素材的视觉质量指标。
请判断这段素材中哪一段最适合剪进 15 秒成片，并从以下候选窗口中选择最佳的一个。

素材名：{clip_name}
质量等级：{tier}
清晰度：{sharpness:.1f}
曝光均值：{exposure:.3f}
稳定度：{stability:.3f}
警告：{warnings}

关键帧（对应时间，单位：秒）：
{keyframes}

候选窗口（单位：秒）：
{windows}

请输出严格的 JSON，不要带 markdown 代码块：
{{"start": 浮点数, "end": 浮点数, "score": 0-10 整数, "reason": "简短理由"}}

规则：
- 选择画面最清晰、产品展示最完整、动作最稳定的窗口。
- 优先展示产品正面、包装细节、开袋/试吃等带货关键动作。
- 避免选择模糊、过曝、抖动或空镜头的窗口。
- 窗口长度建议 2-8 秒，节奏快时可用 0.5-2 秒短镜头补充。
"""


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def resolve_thumb_path(thumb: str, edit_dir: Path) -> Path | None:
    p = Path(thumb)
    if not p.is_absolute():
        p = edit_dir / p
    return p if p.exists() else None


def _windows_for_clip(entry: dict, min_dur: float = 0.5, max_dur: float = 8.0) -> list[tuple[float, float]]:
    """Generate candidate windows from shot boundaries.

    Falls back to a single 0.5s--max_dur window when no shot boundaries exist.
    """
    boundaries = entry.get("shot_boundaries", [])
    duration = entry.get("duration", 0)
    if len(boundaries) < 2:
        boundaries = [0.0, duration]

    windows = []
    for i in range(len(boundaries)):
        for j in range(i + 1, len(boundaries)):
            start = boundaries[i]
            end = boundaries[j]
            dur = end - start
            if min_dur <= dur <= max_dur:
                windows.append((start, end))
    if not windows and duration > 0:
        end = min(duration, max_dur)
        if end >= min_dur:
            windows.append((0.0, end))
    return windows


def _describe_windows(windows: list[tuple[float, float]]) -> str:
    lines = []
    for i, (s, e) in enumerate(windows, 1):
        lines.append(f"  {i}. [{s:.2f} - {e:.2f}]，时长 {e - s:.2f}s")
    return "\n".join(lines)


def _describe_keyframes(keyframes: list[dict]) -> str:
    lines = []
    for i, kf in enumerate(keyframes[:6], 1):
        lines.append(f"  {i}. {kf.get('time', 0):.2f}s")
    return "\n".join(lines) if lines else "  无"


def _extract_json(text: str) -> dict | None:
    """Extract the first {...} block that looks like valid JSON."""
    # Remove markdown code fences
    text = re.sub(r"```json\s*|```\s*", "", text)
    # Find JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _score_by_cv(entry: dict, windows: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Fallback deterministic scorer using CV metrics."""
    scores = entry.get("visual_scores", {})
    sharp = scores.get("sharpness_mean", 0)
    exp = scores.get("exposure_mean", 0.5)
    stab = scores.get("stability_score", 1.0)

    # Best window is simply the longest within bounds (CV already filters quality)
    best = max(windows, key=lambda w: w[1] - w[0])
    # Composite score
    exp_penalty = abs(exp - 0.5) * 100
    score = min(10, max(1, (sharp / 20) + (stab * 5) - exp_penalty))
    return best[0], best[1], round(score)


class Backend:
    def score_clip(
        self,
        clip_name: str,
        entry: dict,
        windows: list[tuple[float, float]],
        edit_dir: Path,
    ) -> tuple[float, float, float, str]:
        raise NotImplementedError


class CVBackend(Backend):
    """Deterministic fallback; no LLM call."""

    def score_clip(
        self,
        clip_name: str,
        entry: dict,
        windows: list[tuple[float, float]],
        edit_dir: Path,
    ) -> tuple[float, float, float, str]:
        start, end, score = _score_by_cv(entry, windows)
        reason = f"CV fallback: tier {entry.get('quality_tier', '?')}, sharpness {entry.get('visual_scores', {}).get('sharpness_mean', 0):.1f}"
        return start, end, float(score), reason


class MlxVlmBackend(Backend):
    def __init__(self, model: str, max_tokens: int = 512, temperature: float = 0.2):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._checked = False

    def _check(self):
        if self._checked:
            return
        if shutil.which("python") is None:
            raise RuntimeError("python executable not found on PATH")
        self._checked = True

    def score_clip(
        self,
        clip_name: str,
        entry: dict,
        windows: list[tuple[float, float]],
        edit_dir: Path,
    ) -> tuple[float, float, float, str]:
        self._check()
        scores = entry.get("visual_scores", {})
        keyframes = entry.get("keyframes", [])
        prompt = DEFAULT_PROMPT_TEMPLATE.format(
            clip_name=clip_name,
            tier=entry.get("quality_tier", "?"),
            sharpness=scores.get("sharpness_mean", 0),
            exposure=scores.get("exposure_mean", 0),
            stability=scores.get("stability_score", 0),
            warnings=", ".join(entry.get("warnings", [])) or "无",
            keyframes=_describe_keyframes(keyframes),
            windows=_describe_windows(windows),
        )

        # Collect keyframe thumbnails
        image_args: list[str] = []
        for kf in keyframes[:6]:  # max 6 frames
            thumb_path = resolve_thumb_path(kf.get("thumb", ""), edit_dir)
            if thumb_path:
                image_args.extend(["--image", str(thumb_path)])

        cmd = [
            sys.executable, "-m", "mlx_vlm.generate",
            "--model", self.model,
            "--max-tokens", str(self.max_tokens),
            "--temp", str(self.temperature),
            "--prompt", prompt,
            *image_args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"  mlx-vlm error for {clip_name}: {result.stderr[:500]}")
            # Fallback to CV
            s, e, sc = _score_by_cv(entry, windows)
            return s, e, sc, "LLM failed, fell back to CV"

        parsed = _extract_json(result.stdout)
        if not parsed or "start" not in parsed or "end" not in parsed:
            s, e, sc = _score_by_cv(entry, windows)
            return s, e, sc, f"LLM output unparseable: {result.stdout[:200]}"

        start = float(parsed.get("start", windows[0][0]))
        end = float(parsed.get("end", windows[0][1]))
        score = float(parsed.get("score", 5))
        reason = str(parsed.get("reason", "LLM selected"))
        return start, end, score, reason


class OllamaBackend(Backend):
    def __init__(self, model: str, url: str = "http://localhost:11434/api/generate"):
        self.model = model
        self.url = url

    def _encode_image(self, path: Path) -> str:
        return base64.b64encode(path.read_bytes()).decode("utf-8")

    def score_clip(
        self,
        clip_name: str,
        entry: dict,
        windows: list[tuple[float, float]],
        edit_dir: Path,
    ) -> tuple[float, float, float, str]:
        scores = entry.get("visual_scores", {})
        keyframes = entry.get("keyframes", [])
        prompt = DEFAULT_PROMPT_TEMPLATE.format(
            clip_name=clip_name,
            tier=entry.get("quality_tier", "?"),
            sharpness=scores.get("sharpness_mean", 0),
            exposure=scores.get("exposure_mean", 0),
            stability=scores.get("stability_score", 0),
            warnings=", ".join(entry.get("warnings", [])) or "无",
            keyframes=_describe_keyframes(keyframes),
            windows=_describe_windows(windows),
        )

        images = []
        for kf in keyframes[:6]:
            thumb_path = resolve_thumb_path(kf.get("thumb", ""), edit_dir)
            if thumb_path:
                images.append(self._encode_image(thumb_path))

        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": images,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 512},
        }
        try:
            resp = requests.post(self.url, json=payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "")
        except Exception as e:
            print(f"  ollama error for {clip_name}: {e}")
            s, e, sc = _score_by_cv(entry, windows)
            return s, e, sc, "Ollama failed, fell back to CV"

        parsed = _extract_json(text)
        if not parsed or "start" not in parsed or "end" not in parsed:
            s, e, sc = _score_by_cv(entry, windows)
            return s, e, sc, f"Ollama output unparseable: {text[:200]}"

        start = float(parsed.get("start", windows[0][0]))
        end = float(parsed.get("end", windows[0][1]))
        score = float(parsed.get("score", 5))
        reason = str(parsed.get("reason", "Ollama selected"))
        return start, end, score, reason


def build_backend(args: argparse.Namespace) -> Backend:
    if args.backend == "cv":
        return CVBackend()
    if args.backend == "mlx-vlm":
        return MlxVlmBackend(model=args.model)
    if args.backend == "ollama":
        return OllamaBackend(model=args.model, url=args.ollama_url)
    raise ValueError(f"Unknown backend: {args.backend}")


def select_segments(
    report: dict,
    backend: Backend,
    edit_dir: Path,
    target_duration: float,
    min_seg: float,
    max_seg: float,
) -> list[dict]:
    clips = report.get("clips", {})
    scored_candidates: list[dict] = []

    for clip_name in sorted(clips.keys()):
        entry = clips[clip_name]
        windows = _windows_for_clip(entry, min_seg, max_seg)
        if not windows:
            continue

        print(f"[score] {clip_name}")
        start, end, score, reason = backend.score_clip(clip_name, entry, windows, edit_dir)
        # Clamp to valid range
        duration = entry.get("duration", end)
        start = max(0.0, min(start, duration - 0.5))
        end = max(start + 0.5, min(end, duration))

        scored_candidates.append({
            "source": clip_name,
            "start": round(start, 2),
            "end": round(end, 2),
            "score": score,
            "reason": reason,
            "tier": entry.get("quality_tier", "C"),
            "duration": round(end - start, 2),
        })

    # Greedy select by score until we hit target duration.
    scored_candidates.sort(key=lambda x: (x["score"], {"A": 2, "B": 1, "C": 0}.get(x["tier"], 0)), reverse=True)

    selected: list[dict] = []
    remaining = target_duration
    used_sources: set[str] = set()

    for cand in scored_candidates:
        if remaining <= 0.5:
            break
        if cand["source"] in used_sources:
            continue
        seg_dur = cand["duration"]
        if seg_dur > remaining + 0.5:
            # Trim the segment to fit.
            cand["end"] = round(cand["start"] + remaining, 2)
            cand["duration"] = round(cand["end"] - cand["start"], 2)
            seg_dur = cand["duration"]
        selected.append(cand)
        used_sources.add(cand["source"])
        remaining -= seg_dur

    # Sort selected by source filename for a deterministic order.
    selected.sort(key=lambda x: x["source"])
    return selected


def build_edl(selected: list[dict], report: dict, args: argparse.Namespace) -> dict:
    sources = {}
    ranges = []
    beat_map = {
        0: "OPENING",
        1: "PRODUCT_REVEAL",
        2: "DETAIL",
        3: "ACTION",
        4: "CLOSE",
    }
    for i, seg in enumerate(selected):
        clip_name = seg["source"]
        entry = report["clips"].get(clip_name, {})
        sources[clip_name] = entry.get("file", str(Path.cwd() / f"{clip_name}.MP4"))
        ranges.append({
            "source": clip_name,
            "start": seg["start"],
            "end": seg["end"],
            "beat": beat_map.get(i, "SHOT"),
            "quote": "",
            "reason": f"score {seg['score']:.1f}/10: {seg['reason']}",
        })

    total = sum(r["end"] - r["start"] for r in ranges)
    return {
        "version": 1,
        "workflow": "snack_visual",
        "sources": sources,
        "ranges": ranges,
        "grade": args.grade,
        "overlays": [],
        "subtitles": None,
        "audio_track": args.audio_track,
        "total_duration_s": round(total, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Select best ~15s segments using local multimodal LLM")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Project edit dir")
    ap.add_argument("--backend", choices=["cv", "mlx-vlm", "ollama"], default="cv",
                    help="Scoring backend (cv = no LLM)")
    ap.add_argument("--model", default="mlx-community/Qwen2-VL-2B-Instruct-4bit",
                    help="Model name for mlx-vlm or ollama")
    ap.add_argument("--ollama-url", default="http://localhost:11434/api/generate",
                    help="Ollama API endpoint")
    ap.add_argument("--target-duration", type=float, default=15.0, help="Target final duration")
    ap.add_argument("--min-seg", type=float, default=2.5, help="Minimum segment length")
    ap.add_argument("--max-seg", type=float, default=8.0, help="Maximum segment length")
    ap.add_argument("--grade", default="auto", help="Grade preset or 'auto'")
    ap.add_argument("--audio-track", default=None, help="Optional external audio track path")
    ap.add_argument("-o", "--output", type=Path, default=None, help="EDL output path")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    report = load_json(edit_dir / "visual_report.json")
    if not report:
        print(f"error: {edit_dir / 'visual_report.json'} not found. Run analyze_visual.py first.")
        return 1

    backend = build_backend(args)
    selected = select_segments(report, backend, edit_dir, args.target_duration, args.min_seg, args.max_seg)

    if not selected:
        print("error: no usable segments found")
        return 1

    total = sum(s["duration"] for s in selected)
    print(f"\nselected {len(selected)} segment(s), total {total:.2f}s")
    for s in selected:
        print(f"  {s['source']} [{s['start']:.2f}-{s['end']:.2f}] {s['duration']:.2f}s score={s['score']:.1f} tier={s['tier']}")

    edl = build_edl(selected, report, args)
    out_path = args.output or (edit_dir / "edl.json")
    out_path.write_text(json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nedl → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

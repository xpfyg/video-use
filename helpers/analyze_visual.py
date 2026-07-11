#!/usr/bin/env python3
"""Analyze video clips for visual quality metrics.

Extracts downsampled frames at a fixed sampling rate and computes:
  - sharpness (Laplacian variance)
  - exposure mean / std (Y channel, 0-1)
  - saturation mean (HSV S channel, 0-1)
  - contrast (gray std)
  - stability (frame-diff MSE, inverted so 1.0 = perfectly stable)
  - shot boundaries (histogram Bhattacharyya distance)

Outputs ``edit/visual_report.json`` with per-clip scores and keyframe
thumbnails. Results are cached by file mtime + size so re-runs are cheap.

Usage:
    python helpers/analyze_visual.py raw/*.MP4 --edit-dir ./edit
    python helpers/analyze_visual.py raw/*.MP4 --edit-dir ./edit --force
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np


def run(cmd: list[str], capture_output: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture_output, text=True, check=True)


def get_video_info(video_path: Path) -> dict:
    """Return duration, width, height, fps via ffprobe.

    Width/height are display dimensions (honors rotation metadata).
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate:stream_side_data=rotation",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ]
    result = run(cmd)
    data = json.loads(result.stdout)
    stream = data.get("streams", [{}])[0]
    fmt = data.get("format", {})

    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))

    # Honor rotation side-data for display dimensions.
    rotation = 0
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"])
            break
    if abs(rotation) in (90, 270):
        width, height = height, width

    fps = 0.0
    for key in ("r_frame_rate", "avg_frame_rate"):
        rate = stream.get(key, "0/1")
        if "/" in rate:
            num, den = rate.split("/")
            den = int(den) if den else 1
            if den:
                fps = int(num) / den
                break

    duration = float(fmt.get("duration", 0) or 0)
    return {
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "duration": round(duration, 2),
    }


def extract_frames(
    video_path: Path,
    sample_fps: float,
    max_size: int,
) -> tuple[list[np.ndarray], list[float]]:
    """Extract frames at ``sample_fps`` downscaled so longest edge <= max_size.

    Total frames are capped at 10 per video. For long videos the effective
    sampling rate is lowered automatically so we never exceed the cap.
    """
    info = get_video_info(video_path)
    duration = info["duration"]

    # Short videos keep the requested rate; long videos are throttled so
    # the total frame count never exceeds 10.
    max_frames = 10
    if duration > 0 and duration * sample_fps > max_frames:
        sample_fps = max_frames / duration

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        scale = f"scale='min({max_size},iw)':-2"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(video_path),
            "-vf", f"fps={sample_fps:.2f},{scale}",
            "-pix_fmt", "bgr24",
            str(tmp_path / "frame_%06d.png"),
        ]
        run(cmd, capture_output=False)

        files = sorted(tmp_path.glob("frame_*.png"))
        frames = []
        times = []
        for i, f in enumerate(files):
            t = i / sample_fps
            if t > duration + 0.05:
                break
            img = cv2.imread(str(f))
            if img is not None:
                frames.append(img)
                times.append(round(t, 3))

    if not frames:
        raise RuntimeError(f"No frames could be extracted from {video_path}")
    return frames, times


def _gray(frame: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def compute_sharpness(frame: np.ndarray) -> float:
    return float(cv2.Laplacian(_gray(frame), cv2.CV_64F).var())


def compute_exposure(frame: np.ndarray) -> tuple[float, float]:
    """Return (Y mean, Y std) normalized to 0..1."""
    yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
    y = yuv[:, :, 0].astype(np.float32) / 255.0
    return float(y.mean()), float(y.std())


def compute_saturation(frame: np.ndarray) -> float:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1].astype(np.float32) / 255.0
    return float(s.mean())


def compute_contrast(frame: np.ndarray) -> float:
    gray = _gray(frame).astype(np.float32) / 255.0
    return float(gray.std())


def compute_stability(frames: list[np.ndarray]) -> float:
    """Return stability score 0..1, where 1.0 is perfectly static."""
    if len(frames) < 2:
        return 1.0
    diffs: list[float] = []
    prev = _gray(frames[0]).astype(np.float32)
    for frame in frames[1:]:
        curr = _gray(frame).astype(np.float32)
        mse = float(((curr - prev) ** 2).mean())
        diffs.append(mse)
        prev = curr
    mean_diff = sum(diffs) / len(diffs)
    # Normalise roughly: 0-255^2 = 65025 max MSE. Scale so 5000 MSE -> 0.9 stable.
    stability = max(0.0, 1.0 - mean_diff / 5000.0)
    return round(stability, 3)


def detect_shot_boundaries(
    frames: list[np.ndarray],
    times: list[float],
    threshold: float = 0.35,
) -> list[float]:
    """Detect shot boundaries using grayscale histogram Bhattacharyya distance."""
    if len(frames) < 2:
        return [0.0]

    boundaries = [0.0]
    prev_hist = cv2.calcHist([_gray(frames[0])], [0], None, [64], [0, 256])
    prev_hist = cv2.normalize(prev_hist, prev_hist).flatten()

    for i in range(1, len(frames)):
        curr_hist = cv2.calcHist([_gray(frames[i])], [0], None, [64], [0, 256])
        curr_hist = cv2.normalize(curr_hist, curr_hist).flatten()
        diff = cv2.compareHist(prev_hist, curr_hist, cv2.HISTCMP_BHATTACHARYYA)
        if diff > threshold:
            boundaries.append(times[i])
        prev_hist = curr_hist

    # Deduplicate very close boundaries.
    cleaned = [boundaries[0]]
    for b in boundaries[1:]:
        if b - cleaned[-1] >= 0.5:
            cleaned.append(b)
    return cleaned


def compute_quality_tier(scores: dict) -> str:
    sharp = scores["sharpness_mean"]
    exp = scores["exposure_mean"]
    stab = scores["stability_score"]

    if sharp < 50 or exp < 0.25 or exp > 0.8 or stab < 0.6:
        return "C"
    if sharp >= 100 and 0.35 <= exp <= 0.65 and stab >= 0.85:
        return "A"
    return "B"


def save_all_frames(
    frames: list[np.ndarray],
    times: list[float],
    cache_dir: Path,
    clip_name: str,
) -> list[dict]:
    """Save every sampled frame so later content analysis knows exact timestamps."""
    frames_dir = cache_dir / "frames" / clip_name
    frames_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    for i, (frame, t) in enumerate(zip(frames, times)):
        path = frames_dir / f"frame_{i:04d}_{t:.3f}s.jpg"
        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        saved.append({
            "time": round(t, 3),
            "file": str(path.relative_to(cache_dir.parent)),
        })
    return saved





def analyze_clip(
    video_path: Path,
    cache_dir: Path,
    sample_fps: float,
    max_size: int,
) -> dict:
    clip_name = video_path.stem
    info = get_video_info(video_path)
    frames, times = extract_frames(video_path, sample_fps, max_size)

    sharpnesses = [compute_sharpness(f) for f in frames]
    exposures = [compute_exposure(f) for f in frames]
    saturations = [compute_saturation(f) for f in frames]
    contrasts = [compute_contrast(f) for f in frames]

    boundaries = detect_shot_boundaries(frames, times)
    stability = compute_stability(frames)

    scores = {
        "sharpness_mean": round(sum(sharpnesses) / len(sharpnesses), 2),
        "sharpness_min": round(min(sharpnesses), 2),
        "exposure_mean": round(sum(e[0] for e in exposures) / len(exposures), 3),
        "exposure_std": round(sum(e[1] for e in exposures) / len(exposures), 3),
        "saturation_mean": round(sum(saturations) / len(saturations), 3),
        "contrast_mean": round(sum(contrasts) / len(contrasts), 3),
        "stability_score": stability,
        "stability_method": "frame_diff_mse",
    }

    warnings: list[str] = []
    if scores["exposure_mean"] < 0.3:
        warnings.append("underexposure")
    elif scores["exposure_mean"] > 0.7:
        warnings.append("overexposure")
    if scores["sharpness_mean"] < 60:
        warnings.append("significant_blur")
    if scores["stability_score"] < 0.7:
        warnings.append("unstable_motion")

    frame_files = save_all_frames(frames, times, cache_dir, clip_name)

    return {
        "file": str(video_path.resolve()),
        "duration": info["duration"],
        "resolution": [info["width"], info["height"]],
        "fps": info["fps"],
        "visual_scores": scores,
        "shot_boundaries": [round(b, 2) for b in boundaries],
        "frames": frame_files,
        "quality_tier": compute_quality_tier(scores),
        "warnings": warnings,
    }


def load_cached_report(edit_dir: Path) -> dict | None:
    path = edit_dir / "visual_report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def should_reanalyze(video_path: Path, cached_entry: dict | None) -> bool:
    if cached_entry is None:
        return True
    try:
        stat = video_path.stat()
    except OSError:
        return True
    return (
        cached_entry.get("file_mtime") != stat.st_mtime
        or cached_entry.get("file_size") != stat.st_size
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze video clips for visual quality")
    ap.add_argument("videos", nargs="+", type=Path, help="Video files to analyze")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Output dir")
    ap.add_argument("--sample-fps", type=float, default=1.0, help="Analysis frame rate")
    ap.add_argument("--max-size", type=int, default=480, help="Longest analysis edge")
    ap.add_argument("--shot-threshold", type=float, default=0.35, help="Shot boundary threshold")
    ap.add_argument("--force", action="store_true", help="Force re-analysis")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    cache_dir = edit_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    existing = load_cached_report(edit_dir) or {}
    existing_clips = existing.get("clips", {})

    report = {
        "version": 1,
        "sample_fps": args.sample_fps,
        "max_size": args.max_size,
        "clips": {},
    }

    for video_path in args.videos:
        clip_name = video_path.stem
        cached = existing_clips.get(clip_name)

        if not args.force and not should_reanalyze(video_path, cached):
            print(f"[cache] {clip_name}")
            report["clips"][clip_name] = cached
            continue

        print(f"[analyze] {clip_name}")
        stat = video_path.stat()
        entry = analyze_clip(video_path, cache_dir, args.sample_fps, args.max_size)
        entry["file_mtime"] = stat.st_mtime
        entry["file_size"] = stat.st_size
        report["clips"][clip_name] = entry

    report_path = edit_dir / "visual_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nreport → {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

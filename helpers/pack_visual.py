#!/usr/bin/env python3
"""Pack visual_report.json (and optional duplicate_groups.json) into a compact,
LLM-readable markdown report: ``edit/visual_packed.md``.

Usage:
    python helpers/pack_visual.py --edit-dir ./edit
    python helpers/pack_visual.py --edit-dir ./edit --target-duration 15.0
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(edit_dir: Path, name: str) -> dict | None:
    path = edit_dir / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def load_duplicate_groups(edit_dir: Path) -> tuple[list[dict], list[str]]:
    data = load_json(edit_dir, "duplicate_groups.json")
    if not data:
        return [], []
    return data.get("groups", []), data.get("ungrouped", [])


def format_scores(scores: dict) -> str:
    return (
        f"{scores['sharpness_mean']:6.1f} | "
        f"{scores['exposure_mean']:5.3f} | "
        f"{scores['saturation_mean']:5.3f} | "
        f"{scores['stability_score']:5.3f}"
    )


def pick_candidate_windows(
    report: dict,
    groups: list[dict],
    ungrouped: list[str],
    target_duration: float,
) -> list[dict]:
    """Greedy pre-selection of non-overlapping, high-quality segments."""
    clips = report.get("clips", {})

    # Build one representative clip per group (best tier, then sharpest).
    representative: dict[str, str] = {}
    for g in groups:
        members = g.get("clips", [])
        if not members:
            continue
        ranked = sorted(
            members,
            key=lambda c: (
                {"A": 2, "B": 1, "C": 0}.get(clips.get(c, {}).get("quality_tier", "C"), 0),
                clips.get(c, {}).get("visual_scores", {}).get("sharpness_mean", 0),
            ),
            reverse=True,
        )
        representative[g["group_id"]] = ranked[0]

    # All selectable clips: one per group + ungrouped.
    selectable = list(representative.values()) + ungrouped

    # Sort by tier then sharpness.
    def score_key(c: str) -> tuple:
        entry = clips.get(c, {})
        tier = {"A": 2, "B": 1, "C": 0}.get(entry.get("quality_tier", "C"), 0)
        sharp = entry.get("visual_scores", {}).get("sharpness_mean", 0)
        return (tier, sharp)

    selectable.sort(key=score_key, reverse=True)

    # Greedy pick segments from shot boundaries until we approach target.
    picked: list[dict] = []
    remaining = target_duration
    used_sources: set[str] = set()

    for clip_name in selectable:
        if remaining <= 0.5:
            break
        if clip_name in used_sources:
            continue
        entry = clips.get(clip_name)
        if not entry:
            continue

        boundaries = entry.get("shot_boundaries", [0.0])
        duration = entry.get("duration", 0)
        if len(boundaries) < 2:
            boundaries = [0.0, min(duration, remaining + 1.0)]

        # Pick the longest shot that fits in remaining budget.
        best_segment = None
        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            seg_dur = end - start
            if 0.5 <= seg_dur <= remaining + 0.5:
                if best_segment is None or seg_dur > (best_segment["end"] - best_segment["start"]):
                    best_segment = {"source": clip_name, "start": start, "end": end}

        if best_segment:
            # Clamp to remaining budget.
            seg_dur = best_segment["end"] - best_segment["start"]
            if seg_dur > remaining:
                best_segment["end"] = best_segment["start"] + remaining
            picked.append(best_segment)
            used_sources.add(clip_name)
            remaining -= (best_segment["end"] - best_segment["start"])

    return picked


def build_markdown(
    report: dict,
    groups: list[dict],
    ungrouped: list[str],
    target_duration: float,
) -> str:
    clips = report.get("clips", {})
    total_duration = sum(c.get("duration", 0) for c in clips.values())

    lines: list[str] = [
        "# 视觉质量报告",
        "",
        "## 摘要",
        f"- {len(clips)} 条素材，总时长 {total_duration:.1f}s",
        f"- 重复分组 {len(groups)} 个，独立镜头 {len(ungrouped)} 个",
        f"- 目标成片时长：{target_duration:.1f}s",
        "",
    ]

    # Duplicate groups
    if groups:
        lines.extend(["## 重复分组（每组只选 1 条）", ""])
        for g in groups:
            gid = g.get("group_id", "G?")
            label = g.get("label", "未命名场景")
            members = g.get("clips", [])
            lines.append(f"### {gid}: {label} — {' / '.join(members)}")
            lines.append("")
            lines.append("| 素材 | 等级 | 清晰度 | 曝光 | 饱和度 | 稳定度 | 警告 |")
            lines.append("|------|------|--------|------|--------|--------|------|")
            for c in members:
                entry = clips.get(c, {})
                scores = entry.get("visual_scores", {})
                warnings = entry.get("warnings", [])
                lines.append(
                    f"| {c} | {entry.get('quality_tier', '?')} | "
                    f"{scores.get('sharpness_mean', 0):.1f} | "
                    f"{scores.get('exposure_mean', 0):.3f} | "
                    f"{scores.get('saturation_mean', 0):.3f} | "
                    f"{scores.get('stability_score', 0):.3f} | "
                    f"{', '.join(warnings) if warnings else '-'} |"
                )
            lines.append("")

    # Ungrouped clips
    if ungrouped:
        lines.extend(["## 独立镜头", ""])
        lines.append("| 素材 | 等级 | 清晰度 | 曝光 | 饱和度 | 稳定度 | 警告 |")
        lines.append("|------|------|--------|------|--------|--------|------|")
        for c in ungrouped:
            entry = clips.get(c, {})
            scores = entry.get("visual_scores", {})
            warnings = entry.get("warnings", [])
            lines.append(
                f"| {c} | {entry.get('quality_tier', '?')} | "
                f"{scores.get('sharpness_mean', 0):.1f} | "
                f"{scores.get('exposure_mean', 0):.3f} | "
                f"{scores.get('saturation_mean', 0):.3f} | "
                f"{scores.get('stability_score', 0):.3f} | "
                f"{', '.join(warnings) if warnings else '-'} |"
            )
        lines.append("")

    # Pre-selected candidate segments
    candidates = pick_candidate_windows(report, groups, ungrouped, target_duration)
    if candidates:
        lines.extend(["## 预筛选候选窗口", ""])
        total = 0.0
        for seg in candidates:
            dur = seg["end"] - seg["start"]
            total += dur
            lines.append(f"- {seg['source']} [{seg['start']:.2f}-{seg['end']:.2f}] = {dur:.2f}s")
        lines.append(f"\n**预筛选合计：{total:.2f}s**")
        lines.append("")

    # LLM instructions
    lines.extend(
        [
            "## LLM 选段指令",
            "",
            "你是零食带货短视频剪辑助手。请根据上方的视觉质量报告，为最终 15 秒成片挑选最佳片段。",
            "",
            "规则：",
            "1. 最终总时长必须控制在 14.5–15.5s 之间。",
            "2. 每个重复分组最多选 1 条素材。",
            "3. 优先选择：A 等级 > B 等级 > C 等级；同等级优先清晰度高、稳定度高的窗口。",
            "4. 切割点尽量落在 shot boundary 上。",
            "5. 输出严格 JSON 数组，每个元素包含 source / start / end / beat / reason。",
            "",
            "输出示例：",
            "```json",
            '[{"source": "C0103", "start": 4.2, "end": 11.8, "beat": "PRODUCT_REVEAL", "reason": "最清晰、曝光稳定"}]',
            "```",
            "",
        ]
    )

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pack visual report into LLM-readable markdown")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Project edit dir")
    ap.add_argument("--target-duration", type=float, default=15.0, help="Target final duration")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    report = load_json(edit_dir, "visual_report.json")
    if not report:
        print(f"error: {edit_dir / 'visual_report.json'} not found")
        return 1

    groups, ungrouped = load_duplicate_groups(edit_dir)

    md = build_markdown(report, groups, ungrouped, args.target_duration)
    out_path = edit_dir / "visual_packed.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"packed → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

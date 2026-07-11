#!/usr/bin/env python3
"""优化 content_report：把 material_labels.json 中每个素材的动作标签回填进 content_report，
并去掉冗余的 summary 字段。

工作流位置：
- ``content_report.json`` 由 ``analyze_content.py`` 产出，按素材（clip）记录分镜窗口与画面描述，
  每个 clip 形如 ``{clip_name, clip_path, summary, windows, use_ollama}``。
- ``material_labels.json`` 由 ``classify_materials.py`` 产出，按素材给出动作标签
  ``{label, confidence, needs_review, ...}``。

本脚本做两件事：
1. 用 material_labels 的 label 回填到 content_report 每个 clip（同时带上 confidence / needs_review），
   便于下游 match_shots 直接按标签匹配镜头。
2. 删除每个 clip 里体积大且无用的 summary 字段，得到更轻量的 content_report。

clip 名称（IMG_7658 / PMR00868 ...）在两份文件里一一对应。

用法：
    # 基于 edit 目录（同目录下的 content_report.json / material_labels.json）
    python helpers/optimize_content_report.py --edit-dir ./edit

    # 显式指定两个输入文件，输出到新文件（不覆盖原 content_report）
    python helpers/optimize_content_report.py \
        --content-report ./edit/content_report.json \
        --material-labels ./edit/material_labels.json \
        --output ./edit/content_report_labeled.json

    # 先预览改动，不落盘
    python helpers/optimize_content_report.py --edit-dir ./edit --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    if not path.exists():
        print(f"[error] 找不到文件：{path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[error] 解析 JSON 失败 {path}: {e}", file=sys.stderr)
        raise SystemExit(2)


def optimize(
    content_report: dict,
    material_labels: dict,
    drop_summary: bool = True,
    keep_confidence: bool = True,
    keep_needs_review: bool = True,
) -> tuple[dict, dict]:
    """返回 (优化后的 content_report, 统计信息)。

    - 对每个 clip，按 clip_name 在 material_labels.materials 里查 label 并回填；
    - 可选删除 summary 字段。
    """
    clips = content_report.get("clips")
    if not isinstance(clips, dict):
        print("[error] content_report.json 缺少 'clips' 字段或格式不对", file=sys.stderr)
        raise SystemExit(2)

    materials = material_labels.get("materials", {})
    if not isinstance(materials, dict):
        print("[error] material_labels.json 缺少 'materials' 字段或格式不对", file=sys.stderr)
        raise SystemExit(2)

    stats = {"filled": 0, "missing": 0, "summary_removed": 0, "total": len(clips)}

    for name, clip in clips.items():
        if not isinstance(clip, dict):
            continue

        # 1) 回填动作标签
        mat = materials.get(name)
        if mat and isinstance(mat, dict) and mat.get("label"):
            clip["label"] = mat["label"]
            if keep_confidence and "confidence" in mat:
                try:
                    clip["confidence"] = round(float(mat["confidence"]), 2)
                except (TypeError, ValueError):
                    clip["confidence"] = mat["confidence"]
            if keep_needs_review and "needs_review" in mat:
                clip["needs_review"] = bool(mat["needs_review"])
            stats["filled"] += 1
        else:
            clip["label"] = None
            stats["missing"] += 1

        # 2) 删除 summary
        if drop_summary and "summary" in clip:
            clip.pop("summary", None)
            stats["summary_removed"] += 1

    return content_report, stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description="优化 content_report：回填 material_labels 的标签，并去掉 summary 字段"
    )
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="中间产物目录；默认从此读取 content_report.json 与 material_labels.json")
    ap.add_argument("--content-report", type=Path, default=None,
                    help="content_report.json 路径（默认 <edit-dir>/content_report.json）")
    ap.add_argument("--material-labels", type=Path, default=None,
                    help="material_labels.json 路径（默认 <edit-dir>/material_labels.json）")
    ap.add_argument("--output", type=Path, default=None,
                    help="优化后输出路径（默认覆盖输入的 content_report.json）")
    ap.add_argument("--keep-summary", action="store_true",
                    help="保留 summary 字段（默认删除）")
    ap.add_argument("--no-confidence", action="store_true",
                    help="不回填 confidence 字段")
    ap.add_argument("--no-needs-review", action="store_true",
                    help="不回填 needs_review 字段")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印改动统计，不写文件")
    args = ap.parse_args()

    # 解析默认路径
    edit_dir = args.edit_dir.resolve() if args.edit_dir else None
    content_report_path = args.content_report or (edit_dir / "content_report.json" if edit_dir else None)
    material_labels_path = args.material_labels or (edit_dir / "material_labels.json" if edit_dir else None)
    if not content_report_path or not material_labels_path:
        print("[error] 必须通过 --edit-dir 或同时给出 --content-report / --material-labels", file=sys.stderr)
        return 2
    content_report_path = Path(content_report_path).resolve()
    material_labels_path = Path(material_labels_path).resolve()
    output_path = (args.output or content_report_path).resolve()

    content_report = load_json(content_report_path)
    material_labels = load_json(material_labels_path)

    optimized, stats = optimize(
        content_report,
        material_labels,
        drop_summary=not args.keep_summary,
        keep_confidence=not args.no_confidence,
        keep_needs_review=not args.no_needs_review,
    )

    print(f"[stat] 素材总数: {stats['total']}")
    print(f"[stat] 已回填标签: {stats['filled']}")
    print(f"[stat] 缺失标签(无对应 material): {stats['missing']}")
    print(f"[stat] 移除 summary 字段: {stats['summary_removed']}")
    if stats["missing"]:
        missing = [n for n, c in optimized["clips"].items() if c.get("label") is None]
        print(f"[warn] 以下 clip 在 material_labels 中找不到标签：{missing}")

    if args.dry_run:
        print("[dry-run] 未写入任何文件")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(optimized, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[done] 优化后的 content_report → {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""确定性 EDL 生成器（无大模型）。

取代 ``match_shots.py`` 的 LLM 选片环节：不再调用 Ollama/DeepSeek，也不再依赖
``content_report.json``（内容理解）。直接基于：

- ``strategy.json``        每镜头的 ``visual_prompt`` / ``text`` / ``duration``
- ``visual_report.json``   每素材的源视频路径(file)与时长(duration)
- ``material_labels.json`` 每素材的动作标签 + selected_frames（好切片锚点）

生成逻辑（复用「一键优化」的选片思想）：

1. 从每个 shot 的 ``visual_prompt`` 判断该镜头属于「产品展示 / 食物展示」。
2. 把已分类素材按动作标签归到 产品池 / 食物池。
3. 每个镜头从其所属类别的素材池里**轮询取一个素材**（保证相邻镜头换素材），
   再在该素材的 ``selected_frames`` 里**轮询取一个锚点**，以锚点为起点、截取
   该镜头时长(d=end-start)的切片。
4. 锚点越界则夹取到素材可用时长内。
5. 本地组装 EDL（sources 来自 visual_report），写出 edl.json。

优点：快（纯本地计算，秒级）、稳定、可控、多样；后续仍可用「一键优化 / 精剪台」
对单个镜头手工替换切片。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# 类别判定
# ---------------------------------------------------------------------------
# 已知动作标签 → 类别（产品 / 食物）。覆盖 labels.json 中冷饮、商品两组。
LABEL_CATEGORY: dict[str, str] = {
    # 产品相关（展示包装 / 产品本体）
    "拿起产品": "product",
    "放下产品": "product",
    "开盖展示": "product",
    "配料表": "product",
    "展示产品": "product",
    "产品特写": "product",
    # 食物 / 饮用相关
    "饮料气泡特写": "food",
    "倒饮品动作": "food",
    "拆包装": "food",
    "倒出零食": "food",
    "拆袋展示": "food",
    "食物细节": "food",
    "试吃": "food",
    "入口": "food",
}

# 标签兜底关键词
_PRODUCT_KW = ("产品", "包装", "开盖", "配料", "拿起", "放下", "整箱", "箱", "罐体", "瓶身", "logo")
_FOOD_KW = ("饮品", "饮料", "气泡", "倒", "零食", "食物", "拆", "袋", "细节", "入口", "食用", "试吃", "调配")


def category_of_label(label: str | None) -> str | None:
    if not label:
        return None
    if label in LABEL_CATEGORY:
        return LABEL_CATEGORY[label]
    if any(k in label for k in _PRODUCT_KW):
        return "product"
    if any(k in label for k in _FOOD_KW):
        return "food"
    return None


def category_of_shot(shot: dict) -> str | None:
    """从 shot 的 visual_prompt 判断 产品 / 食物 类别。"""
    vp = (shot.get("visual_prompt") or "") + " " + (shot.get("beat") or "")
    if "产品展示" in vp or "产品" in vp:
        return "product"
    if "食物展示" in vp or "食物" in vp or "饮品" in vp or "零食" in vp:
        return "food"
    return None


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def resolve_source_path(edl_path: Path, mat_id: str, existing: dict) -> str | None:
    """把 material_id 解析为 素材/ 下的视频绝对路径（文件名 stem 匹配，忽略大小写）。"""
    if mat_id in existing:
        return existing[mat_id]
    base = os.path.dirname(os.path.dirname(str(edl_path)))  # 中间产物 -> 产品根
    sp = os.path.join(base, "素材")
    if os.path.isdir(sp):
        for f in os.listdir(sp):
            if os.path.splitext(f)[0].lower() == str(mat_id).lower():
                return os.path.join(sp, f)
    return None


def assemble_edl(strategy, visual_report, material_labels, edit_dir,
                 grade, subtitles, audio_track) -> dict:
    shots = strategy.get("shots")
    if not shots:
        tmpls = strategy.get("templates")
        if isinstance(tmpls, dict) and tmpls:
            first = next(iter(tmpls.values()))
            shots = first.get("shots") if isinstance(first, dict) else None
    if not shots:
        raise SystemExit("error: strategy.json 中找不到 shots")

    # sources + 时长 来自 visual_report
    sources_map: dict[str, str] = {}
    clip_durations: dict[str, float] = {}
    for name, entry in (visual_report or {}).get("clips", {}).items():
        sources_map[name] = entry.get("file", str(edit_dir / f"{name}.MOV"))
        d = entry.get("duration")
        if isinstance(d, (int, float)):
            clip_durations[name] = float(d)

    # 素材分池
    mats = (material_labels or {}).get("materials", {})
    pools: dict[str, list[str]] = {"product": [], "food": []}
    all_labeled: list[str] = []
    for mid, m in mats.items():
        if not isinstance(m, dict):
            continue
        if m.get("needs_review") or not m.get("label"):
            continue
        all_labeled.append(mid)
        cat = category_of_label(m.get("label"))
        if cat in pools:
            pools[cat].append(mid)

    def frames_of(mid: str) -> list[float]:
        m = mats.get(mid, {})
        return sorted(fr.get("time", 0) for fr in m.get("selected_frames", []) if fr.get("time") is not None) or [0.0]

    def span_of(mid: str) -> float:
        d = clip_durations.get(mid)
        if d:
            return d
        ts = frames_of(mid)
        return (max(ts) + 3.0) if ts else 30.0

    ranges: list[dict] = []
    sources: dict[str, str] = {}
    used_idx = {"product": 0, "food": 0}  # 每池轮询指针

    for i, shot in enumerate(shots):
        cat = category_of_shot(shot)
        pool = pools.get(cat) if (cat and pools.get(cat)) else None
        if not pool:
            pool = all_labeled
        if not pool:
            print(f"[warn] shot {shot.get('shot_id','?')} 无可用素材，跳过")
            continue

        # 轮询选素材（相邻镜头换素材）
        mid = pool[used_idx[cat] % len(pool)] if cat in used_idx else pool[i % len(pool)]
        if cat in used_idx:
            used_idx[cat] += 1
        else:
            # 用全局 i 轮询（无类别时）
            mid = pool[i % len(pool)]

        frames = frames_of(mid)
        anchor = frames[i % len(frames)] if frames else 0.0

        d = shot.get("duration")
        if not d:
            d = (shot.get("end", 0) or 0) - (shot.get("start", 0) or 0)
        d = float(d or 2.0)

        span = span_of(mid)
        if anchor + d > span:
            anchor = max(0.0, span - d)
        anchor = max(0.0, anchor)

        ranges.append({
            "source": mid,
            "start": round(anchor, 3),
            "end": round(anchor + d, 3),
            "beat": shot.get("beat", ""),
            "quote": shot.get("text", ""),
            "reason": f"确定性生成：{cat or '通用'}池轮询选「{mats.get(mid, {}).get('label', '?')}」@ {anchor:.1f}s",
        })
        if mid not in sources:
            p = sources_map.get(mid) or resolve_source_path(edit_dir / "edl.json", mid, sources)
            if p:
                sources[mid] = p

    total = round(sum(r["end"] - r["start"] for r in ranges), 3)
    edl = {
        "version": 1,
        "sources": sources,
        "ranges": ranges,
        "grade": grade,
        "overlays": [],
        "subtitles": subtitles,
        "total_duration_s": total,
    }
    if audio_track:
        edl["audio_track"] = str(audio_track.resolve() if hasattr(audio_track, "resolve") else audio_track)
    return edl


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性 EDL 生成（无大模型，基于素材分类帧）")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="中间产物目录")
    ap.add_argument("--grade", default="auto", help="调色预设")
    ap.add_argument("--audio-track", type=Path, default=None, help="外部配音文件")
    ap.add_argument("--strategy", type=Path, default=None, help="strategy.json（默认 <edit-dir>/strategy.json）")
    ap.add_argument("--visual-report", type=Path, default=None, help="visual_report.json（默认 <edit-dir>/visual_report.json）")
    ap.add_argument("--material-labels", type=Path, default=None, help="material_labels.json（默认 <edit-dir>/material_labels.json）")
    ap.add_argument("-o", "--output", type=Path, default=None, help="EDL 输出（默认 <edit-dir>/edl.json）")
    ap.add_argument("--force", action="store_true", help="（兼容接口）始终重新生成，覆盖已有 EDL")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    strategy = load_json(args.strategy or (edit_dir / "strategy.json"))
    visual_report = load_json(args.visual_report or (edit_dir / "visual_report.json"))
    mat_labels_path = (args.material_labels or (edit_dir / "material_labels.json")).resolve()
    material_labels = load_json(mat_labels_path)

    if not strategy:
        print(f"error: 缺少 strategy.json（先跑 generate_strategy.py）")
        return 1
    if not visual_report:
        print(f"error: 缺少 visual_report.json（先跑 analyze_visual.py）")
        return 1
    if not material_labels:
        print(f"error: 缺少 material_labels.json（先跑 classify_materials.py）")
        return 1

    subtitles = strategy.get("subtitles") or str(edit_dir / "subtitles.srt")
    edl = assemble_edl(strategy, visual_report, material_labels, edit_dir,
                       args.grade, subtitles, args.audio_track)

    out_path = args.output or (edit_dir / "edl.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nedl → {out_path}")
    print(f"total video duration: {edl['total_duration_s']:.2f}s")
    print(f"ranges: {len(edl['ranges'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

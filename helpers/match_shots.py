#!/usr/bin/env python3
"""Match strategy shots to video clips: one model call per shot, then local EDL assembly.

Each shot (beat) in strategy.json carries a `label` (action tag, e.g. 拿起产品). We filter
materials by that label (via material_labels.json) and call the model once per shot with only
that shot's 台词 + the matching materials' content. After all shots are processed, the full
EDL is assembled locally (no giant single prompt).
"""

from __future__ import annotations

import argparse
import json
import os
import requests
from pathlib import Path
from dotenv import load_dotenv


# Load .env file
load_dotenv()

# DEFAULT_ARK_MODEL = "ep-20260702134855-4jqlj"
DEFAULT_ARK_MODEL = "deepseek-v4-flash"
# DEFAULT_ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
DEFAULT_ARK_URL = "https://api.deepseek.com/chat/completions"
ARK_API_KEY = os.getenv("ARK_API_KEY", "")

# Ollama (远程服务, 默认后端)
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3-vl:8b")
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")

PER_SHOT_PROMPT = """你是一名专业的零食带货短视频剪辑师。下面只有一个镜头（shot）需要你从候选素材里挑选一段最合适的切片。

【本镜头信息 - Shot】
- shot_id: {shot_id}
- 阶段(beat): {beat}
- 动作标签(label): {label}
- 口播台词(quote): {text}
- 需要覆盖的时长(秒): {required_duration}

【候选素材内容 - Candidates】
（已按本镜头 label 预筛选；若为空说明无可用素材）
{candidates_json}

【要求】
1. 只从上面「候选素材」里挑，source 必须是给出的素材名之一。
2. 选一段 [start, end]，其长度要尽可能接近「需要覆盖的时长」，画面内容要贴合台词与动作标签。
3. 不要选被标记为废片/质量差的区间；确保所选区间在素材时长范围内。
4. 严格只输出 JSON，不要任何额外文字、不要 markdown 代码块：
{{"source": "素材名", "start": 数字, "end": 数字, "reason": "不超过40字的中文理由"}}"""


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def run_deepseek(prompt: str, model: str, api_url: str = DEFAULT_ARK_URL, max_tokens: int = 32768) -> str:
    """Run DeepSeek API (OpenAI-compatible) and return the raw generated text."""
    payload = {
        "model": model,
        "stream": False,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": prompt.strip()
            }
        ]
    }

    headers = {
        "Authorization": f"Bearer {ARK_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        print("[deepseek] calling model...")
        resp = requests.post(api_url, json=payload, headers=headers)
        resp.raise_for_status()
        res_data = resp.json()
        return res_data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[deepseek] error: {e}")
        if 'resp' in locals():
            print(f"[deepseek] response status: {resp.status_code}")
            print(f"[deepseek] response: {resp.text}")
        return ""


def run_ollama(prompt: str, model: str, ollama_url: str = DEFAULT_OLLAMA_URL, temperature: float = 0.1, max_tokens: int = 32768) -> str:
    """Run Ollama chat (/api/chat) and return the raw generated text."""
    if not ollama_url:
        print("[ollama] error: OLLAMA_URL is not set. Check your .env file.")
        return ""

    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt.strip(),
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {OLLAMA_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        print(f"[ollama] calling model {model}...")
        resp = requests.post(ollama_url, json=payload, headers=headers)
        resp.raise_for_status()
        res_data = resp.json()
        return res_data["message"]["content"].strip()
    except Exception as e:
        print(f"[ollama] error: {e}")
        if 'resp' in locals():
            print(f"[ollama] response status: {resp.status_code}")
            print(f"[ollama] response: {resp.text}")
        return ""


def extract_json(text: str) -> str:
    """Extract JSON from text that may have extra content."""
    # Find first { and last }
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]
    return text


def _char_overlap(a: str, b: str) -> int:
    """Number of shared characters between two label strings (closeness proxy)."""
    return len(set(a) & set(b))


def load_material_labels(path: Path) -> dict | None:
    """Load material_labels.json; returns None if missing/unparseable."""
    data = load_json(path)
    if not data or "materials" not in data:
        return None
    return data


def filter_candidates(shot_label: str, material_labels: dict, no_filter: bool = False):
    """Return (list[clip_name], is_fallback).

    - Exact: materials whose label == shot_label and not needs_review.
    - Fallback (no exact match / empty label / --no-filter-label): use all labeled
      materials, ranked by label closeness to shot_label.
    """
    mats = (material_labels or {}).get("materials", {})

    if no_filter:
        return [c for c, m in mats.items() if not (m or {}).get("needs_review")], False

    if shot_label:
        exact = [c for c, m in mats.items()
                 if (m or {}).get("label") == shot_label and not (m or {}).get("needs_review")]
        if exact:
            return exact, False

    # Fallback: rank labeled materials by closeness to shot_label
    labeled = [(c, (m or {}).get("label") or "") for c, m in mats.items()
               if (m or {}).get("label") and not (m or {}).get("needs_review")]
    if not labeled:
        # Nothing labeled at all -> fall back to everything
        return [c for c, _ in mats.items()], True
    if not shot_label:
        # Empty label -> use all labeled materials
        return [c for c, _ in labeled], True

    ranked = sorted(labeled, key=lambda cm: _char_overlap(shot_label, cm[1]), reverse=True)
    best = _char_overlap(shot_label, ranked[0][1])
    if best == 0:
        # No shared characters anywhere -> take a few closest as candidates
        return [c for c, _ in ranked[:3]], True
    return [c for c, lab in ranked if _char_overlap(shot_label, lab) == best], True


def build_candidate_content(clip_names: list[str], content_report: dict) -> dict:
    """Build a {clip_name: {summary, windows}} dict for the given clips."""
    clips = (content_report or {}).get("clips", {})
    out: dict = {}
    for name in clip_names:
        c = clips.get(name)
        if not c:
            continue
        out[name] = {
            "summary": c.get("summary", ""),
            "windows": [
                {
                    "start": w.get("start"),
                    "end": w.get("end"),
                    "description": w.get("description", ""),
                }
                for w in c.get("windows", [])
            ],
        }
    return out


def match_one_shot(shot: dict, candidates_content: dict, backend: str, model: str,
                   ollama_url: str, ark_url: str, max_tokens: int = 2048) -> dict | None:
    """Call the model for a single shot and return its parsed range dict, or None on failure."""
    shot_id = shot.get("shot_id", "")
    beat = shot.get("beat", "")
    label = shot.get("label", "")
    text = shot.get("text", "")
    dur = shot.get("duration")
    if not dur:
        dur = (shot.get("end", 0) or 0) - (shot.get("start", 0) or 0)
    required = round(float(dur or 0), 3)

    prompt = PER_SHOT_PROMPT.format(
        shot_id=shot_id,
        beat=beat,
        label=label,
        text=text,
        required_duration=required,
        candidates_json=json.dumps(candidates_content, indent=2, ensure_ascii=False),
    )

    if backend == "ollama":
        resp = run_ollama(prompt, model, ollama_url, max_tokens=max_tokens)
    else:
        resp = run_deepseek(prompt, model, ark_url, max_tokens=max_tokens)

    if not resp:
        return None
    try:
        obj = json.loads(extract_json(resp))
    except Exception as e:
        print(f"[shot] {shot_id} parse error: {e}")
        return None

    src = obj.get("source")
    if src not in candidates_content:
        print(f"[shot] {shot_id} model returned unknown source: {src}")
        return None
    return obj


def dedup_ranges(ranges: list[dict], clip_durations: dict) -> list[dict]:
    """Light local de-duplication: on the same source, if a new range overlaps an already
    used interval, try to shift its start forward; if no room, keep and warn."""
    used: dict[str, list[tuple[float, float]]] = {}
    out: list[dict] = []
    for r in ranges:
        s = r["source"]
        a = float(r["start"])
        b = float(r["end"])
        length = b - a
        intervals = used.setdefault(s, [])
        overlap = any(not (b <= ia or a >= ib) for ia, ib in intervals)
        if overlap:
            new_a = max(ib for ia, ib in intervals if not (b <= ia or a >= ib))
            new_b = new_a + length
            dur = clip_durations.get(s)
            if dur is None or new_b <= dur + 0.05:
                a, b = new_a, new_b
                print(f"[dedup] {s}: 区间重叠，后移到 {a:.2f}-{b:.2f}s")
            else:
                print(f"[dedup] warn: {s} 重叠且无空间后移，保留原区间")
        intervals.append((a, b))
        rr = dict(r)
        rr["start"] = round(a, 3)
        rr["end"] = round(b, 3)
        out.append(rr)
    return out


def assemble_edl(shots: list[dict], per_shot_results: list, sources_map: dict,
                 subtitles, grade: str, audio_track, clip_durations: dict) -> dict:
    """Assemble the final EDL locally from per-shot results."""
    ranges: list[dict] = []
    sources: dict = {}
    for shot, res in zip(shots, per_shot_results):
        if res is None:
            continue
        name = res.get("source")
        if name and name in sources_map:
            sources[name] = sources_map[name]
        ranges.append({
            "source": name,
            "start": round(float(res.get("start", 0)), 3),
            "end": round(float(res.get("end", 0)), 3),
            "beat": shot.get("beat", ""),
            "quote": shot.get("text", ""),
            "reason": res.get("reason", ""),
        })

    ranges = dedup_ranges(ranges, clip_durations)
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
        edl["audio_track"] = str(audio_track.resolve())
    return edl


def main() -> int:
    ap = argparse.ArgumentParser(description="Match strategy shots to video clips (per-shot model call, Ollama default), outputting EDL format")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Project edit dir")
    ap.add_argument("--use-ollama", action=argparse.BooleanOptionalAction, default=True, help="Use Ollama for matching (default: True)")
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama API URL")
    ap.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL, help="Ollama model name")
    ap.add_argument("--ark-url", default=DEFAULT_ARK_URL, help="ARK API URL")
    ap.add_argument("--ark-model", default=DEFAULT_ARK_MODEL, help="ARK model name")
    ap.add_argument("--grade", default="auto", help="Grade preset")
    ap.add_argument("--audio-track", type=Path, default=None, help="External audio/dubbing file")
    ap.add_argument("--material-labels", type=Path, default=None, help="material_labels.json path (default <edit-dir>/material_labels.json)")
    ap.add_argument("--no-filter-label", action="store_true", help="Ignore label filtering; use all materials for every shot")
    ap.add_argument("-o", "--output", type=Path, default=None, help="EDL output path")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    strategy = load_json(edit_dir / "strategy.json")
    visual_report = load_json(edit_dir / "visual_report.json")
    content_report = load_json(edit_dir / "content_report.json")

    mat_labels_path = (args.material_labels or (edit_dir / "material_labels.json")).resolve()
    material_labels = load_material_labels(mat_labels_path)

    if not strategy:
        print(f"error: {edit_dir / 'strategy.json'} not found. Run generate_strategy.py first.")
        return 1
    if not visual_report:
        print(f"error: {edit_dir / 'visual_report.json'} not found. Run analyze_visual.py first.")
        return 1
    if not content_report:
        print(f"error: {edit_dir / 'content_report.json'} not found. Run analyze_content.py first.")
        return 1
    if not material_labels:
        print(f"error: {mat_labels_path} not found. Run classify_materials.py first.")
        return 1

    # Resolve shots (support both top-level `shots` and legacy `templates[...].shots`)
    shots = strategy.get("shots")
    if not shots:
        tmpls = strategy.get("templates")
        if isinstance(tmpls, dict) and tmpls:
            first = next(iter(tmpls.values()))
            shots = first.get("shots") if isinstance(first, dict) else None
    if not shots:
        print("error: no shots found in strategy.json")
        return 1

    # Build source path + duration maps from visual report
    sources_map: dict = {}
    clip_durations: dict = {}
    for name, entry in visual_report.get("clips", {}).items():
        sources_map[name] = entry.get("file", str(edit_dir / f"{name}.MOV"))
        d = entry.get("duration")
        if isinstance(d, (int, float)):
            clip_durations[name] = float(d)

    subtitles = strategy.get("subtitles") or str(edit_dir / "subtitles.srt")
    strategy_total = strategy.get("total_duration_s")

    backend = "ollama" if args.use_ollama else "ark"
    model = args.ollama_model if args.use_ollama else args.ark_model

    per_shot_results: list = []
    fallback_shots: list = []
    failed_shots: list = []

    for shot in shots:
        label = shot.get("label", "")
        cands, is_fb = filter_candidates(label, material_labels, no_filter=args.no_filter_label)
        cand_content = build_candidate_content(cands, content_report)
        res = match_one_shot(
            shot, cand_content, backend, model,
            args.ollama_url, args.ark_url, max_tokens=2048,
        )
        sid = shot.get("shot_id", "?")
        if res is None:
            failed_shots.append(sid)
            per_shot_results.append(None)
            print(f"[shot] {sid} label={label or '(空)'} candidates={len(cands)} -> FAIL")
        else:
            if is_fb:
                fallback_shots.append(sid)
                res["label_fallback"] = True
            per_shot_results.append(res)
            print(f"[shot] {sid} label={label or '(空)'} candidates={len(cands)} -> {res.get('source')} "
                  f"{res.get('start')}-{res.get('end')}s{f' (fallback)' if is_fb else ''}")

    edl = assemble_edl(shots, per_shot_results, sources_map, subtitles, args.grade, args.audio_track, clip_durations)

    if strategy_total and abs(edl["total_duration_s"] - float(strategy_total)) > 2.0:
        print(f"[warn] 总时长 {edl['total_duration_s']}s 与 strategy {strategy_total}s 偏差较大")

    out_path = args.output or (edit_dir / "edl.json")
    out_path.write_text(json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nedl → {out_path}")
    print(f"backend: {backend}")
    print(f"total video duration: {edl['total_duration_s']:.2f}s")
    print(f"matched {len(edl['ranges'])}/{len(shots)} shot(s)")
    if fallback_shots:
        print(f"[info] 走 label 近似 fallback 的镜头: {', '.join(fallback_shots)}")
    if failed_shots:
        print(f"[warn] 未匹配到素材的镜头(需人工复核): {', '.join(failed_shots)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

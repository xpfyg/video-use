#!/usr/bin/env python3
"""Detect near-duplicate clips across a project using keyframes.

Combines perceptual hashing (fast) with ORB feature matching (verification) on
the keyframes written by ``analyze_visual.py``. Outputs
``edit/duplicate_groups.json``.

Usage:
    python helpers/dedup_clips.py --edit-dir ./edit
    python helpers/dedup_clips.py --edit-dir ./edit --hash-threshold 8 --orb-threshold 25
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import imagehash
from PIL import Image


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def resolve_thumb(thumb: str, edit_dir: Path) -> Path | None:
    p = Path(thumb)
    if not p.is_absolute():
        p = edit_dir / p
    return p if p.exists() else None


def compute_hashes(image_path: Path) -> dict[str, str]:
    img = Image.open(image_path).convert("RGB")
    return {
        "ahash": str(imagehash.average_hash(img)),
        "dhash": str(imagehash.dhash(img)),
        "phash": str(imagehash.phash(img)),
    }


def hamming_distance(h1: str, h2: str) -> int:
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def orb_match_score(path1: Path, path2: Path, max_features: int = 500) -> int:
    """Return number of good ORB matches between two images."""
    img1 = cv2.imread(str(path1), cv2.IMREAD_GRAYSCALE)
    img2 = cv2.imread(str(path2), cv2.IMREAD_GRAYSCALE)
    if img1 is None or img2 is None:
        return 0

    orb = cv2.ORB_create(max_features)
    kp1, des1 = orb.detectAndCompute(img1, None)
    kp2, des2 = orb.detectAndCompute(img2, None)
    if des1 is None or des2 is None:
        return 0

    # Use k-NN + ratio test for robustness
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    raw_matches = bf.knnMatch(des1, des2, k=2)
    good = 0
    for m_n in raw_matches:
        if len(m_n) != 2:
            continue
        m, n = m_n
        if m.distance < 0.75 * n.distance:
            good += 1
    return good


def clips_are_duplicate(
    entry_a: dict,
    entry_b: dict,
    edit_dir: Path,
    hash_threshold: int,
    orb_threshold: int,
) -> tuple[bool, float]:
    """Return (is_duplicate, confidence)."""
    keyframes_a = entry_a.get("keyframes", [])
    keyframes_b = entry_b.get("keyframes", [])

    best_hash_sim = 0.0
    best_orb = 0
    comparisons = 0

    for kf_a in keyframes_a:
        thumb_a = resolve_thumb(kf_a.get("thumb", ""), edit_dir)
        if not thumb_a:
            continue
        hashes_a = compute_hashes(thumb_a)

        for kf_b in keyframes_b:
            thumb_b = resolve_thumb(kf_b.get("thumb", ""), edit_dir)
            if not thumb_b:
                continue
            hashes_b = compute_hashes(thumb_b)
            comparisons += 1

            # Perceptual hash distance (0 = identical, 64 = completely different)
            dist = min(
                hamming_distance(hashes_a["ahash"], hashes_b["ahash"]),
                hamming_distance(hashes_a["dhash"], hashes_b["dhash"]),
                hamming_distance(hashes_a["phash"], hashes_b["phash"]),
            )
            if dist <= hash_threshold:
                orb = orb_match_score(thumb_a, thumb_b)
                if orb >= orb_threshold:
                    return True, min(0.99, 0.6 + 0.4 * (orb / max(orb_threshold * 3, 1)))
                best_orb = max(best_orb, orb)
            best_hash_sim = max(best_hash_sim, 1.0 - dist / 64.0)

    # Weak duplicate signal: very similar hashes but not enough ORB matches
    if comparisons > 0 and best_hash_sim >= 0.85 and best_orb >= orb_threshold // 2:
        return True, 0.75

    return False, 0.0


class UnionFind:
    def __init__(self, items: list[str]):
        self.parent = {item: item for item in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[ry] = rx


def auto_label(group_clips: list[str], clips: dict) -> str:
    """Generate a simple scene label from dominant colors or clip names."""
    # Heuristic: if all clips share a common prefix, use it.
    if not group_clips:
        return "unknown_scene"
    prefix = group_clips[0]
    for c in group_clips[1:]:
        while not c.startswith(prefix) and prefix:
            prefix = prefix[:-1]
    if prefix and len(prefix) >= 3:
        return f"scene_{prefix}"
    return f"scene_{group_clips[0]}"


def find_duplicate_groups(
    report: dict,
    edit_dir: Path,
    hash_threshold: int = 8,
    orb_threshold: int = 25,
) -> tuple[list[dict], list[str]]:
    clips = report.get("clips", {})
    names = sorted(clips.keys())
    uf = UnionFind(names)

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            is_dup, _conf = clips_are_duplicate(
                clips[a], clips[b], edit_dir, hash_threshold, orb_threshold
            )
            if is_dup:
                uf.union(a, b)

    # Build groups
    roots: dict[str, list[str]] = {}
    for name in names:
        root = uf.find(name)
        roots.setdefault(root, []).append(name)

    groups: list[dict] = []
    ungrouped: list[str] = []
    for root, members in roots.items():
        if len(members) == 1:
            ungrouped.append(members[0])
            continue
        # Pick best candidate by tier then sharpness
        ranked = sorted(
            members,
            key=lambda c: (
                {"A": 2, "B": 1, "C": 0}.get(clips[c].get("quality_tier", "C"), 0),
                clips[c].get("visual_scores", {}).get("sharpness_mean", 0),
            ),
            reverse=True,
        )
        groups.append({
            "group_id": f"G{len(groups) + 1:02d}",
            "label": auto_label(members, clips),
            "clips": members,
            "best_candidate": ranked[0],
            "confidence": 0.9,
            "method": "perceptual_hash+orb",
        })

    return groups, sorted(ungrouped)


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect duplicate clips from visual report")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Project edit dir")
    ap.add_argument("--hash-threshold", type=int, default=8, help="Max perceptual hash Hamming distance")
    ap.add_argument("--orb-threshold", type=int, default=25, help="Min good ORB matches to confirm duplicate")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    report = load_json(edit_dir / "visual_report.json")
    if not report:
        print(f"error: {edit_dir / 'visual_report.json'} not found")
        return 1

    print(f"[dedup] {len(report.get('clips', {}))} clips, hash_threshold={args.hash_threshold}, orb_threshold={args.orb_threshold}")
    groups, ungrouped = find_duplicate_groups(report, edit_dir, args.hash_threshold, args.orb_threshold)

    out = {
        "version": 1,
        "groups": groups,
        "ungrouped": ungrouped,
    }
    out_path = edit_dir / "duplicate_groups.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  {len(groups)} duplicate group(s), {len(ungrouped)} ungrouped")
    for g in groups:
        print(f"  {g['group_id']}: {g['label']} → {', '.join(g['clips'])} (best: {g['best_candidate']})")
    print(f"\ngroups → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

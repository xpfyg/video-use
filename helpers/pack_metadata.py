"""Pack TTS metadata.json into lightweight, readable transcript text.

Optimized for Chinese TTS output with word-level timestamps.
Produces a clean, phrase-level markdown transcript with time ranges
— designed for LLM reading (editor decision-making, content review, etc.).

This is a lighter alternative to going through Scribe-format JSON +
pack_transcripts.py. It preserves sentence boundaries from the TTS
metadata and outputs natural Chinese (no spaces between characters).

Output: <edit_dir>/takes_packed.md

Usage:
    python helpers/pack_metadata.py <metadata.json>
    python helpers/pack_metadata.py <metadata.json> --edit-dir /custom/edit
    python helpers/pack_metadata.py <metadata.json> --speaker S0
    python helpers/pack_metadata.py <metadata.json> --group-by sentence
    python helpers/pack_metadata.py <metadata.json> --group-by auto --max-chars 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def format_time(seconds: float) -> str:
    """Format seconds as "NNN.NN" with fixed 6-char width for alignment."""
    return f"{seconds:06.2f}"


def format_duration(seconds: float) -> str:
    """Format a duration as "Ms" or "Mm SSs"."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}m {s:04.1f}s"


def extract_sentences(metadata: dict) -> list[dict]:
    """Extract sentences from metadata with word timestamps.

    Returns list of {text, start, end, word_count}.
    Skips sentences with empty words arrays.
    """
    sentences: list[dict] = []
    events = metadata.get("events", [])

    for event in events:
        sentence = event.get("sentence")
        if not sentence:
            continue
        words = sentence.get("words", [])
        if not words:
            continue  # Skip summary sentences without word timestamps

        start = words[0].get("startTime", 0)
        end = words[-1].get("endTime", 0)
        text = sentence.get("text", "")

        sentences.append({
            "text": text,
            "start": start,
            "end": end,
            "word_count": len(words),
        })

    return sentences


def group_sentences_auto(
    sentences: list[dict],
    max_chars: int = 40,
    max_duration: float = 8.0,
) -> list[dict]:
    """Group short sentences into longer phrases for readability.

    Combines consecutive sentences until we hit max_chars or max_duration.
    Keeps sentence boundaries visible via punctuation.

    Args:
        sentences: List of sentence dicts with start/end/text.
        max_chars: Approximate max characters per group.
        max_duration: Max duration per group in seconds.

    Returns:
        List of grouped phrase dicts.
    """
    groups: list[dict] = []
    current_text_parts: list[str] = []
    current_start: float | None = None
    current_end: float | None = None
    current_chars = 0

    def flush() -> None:
        nonlocal current_text_parts, current_start, current_end, current_chars
        if not current_text_parts:
            return
        text = "".join(current_text_parts)
        groups.append({
            "text": text,
            "start": current_start,
            "end": current_end,
            "sentence_count": len(current_text_parts),
        })
        current_text_parts = []
        current_start = None
        current_end = None
        current_chars = 0

    for sent in sentences:
        text = sent["text"]
        text_len = len(text)
        duration = sent["end"] - sent["start"]

        # Check if adding this sentence would exceed limits
        if current_text_parts:
            new_chars = current_chars + text_len
            new_duration = sent["end"] - current_start
            if new_chars > max_chars or new_duration > max_duration:
                flush()

        if current_start is None:
            current_start = sent["start"]

        current_text_parts.append(text)
        current_end = sent["end"]
        current_chars += text_len

    flush()
    return groups


def render_markdown(
    phrases: list[dict],
    name: str,
    speaker: str,
    group_mode: str,
) -> str:
    """Render packed transcript as markdown.

    Args:
        phrases: List of {text, start, end, ...} phrases.
        name: Name of the source file.
        speaker: Speaker label (e.g., "S0").
        group_mode: Grouping mode used (for documentation).

    Returns:
        Markdown string.
    """
    lines: list[str] = []
    lines.append("# Packed transcripts")
    lines.append("")
    lines.append(f"Source: TTS metadata ({group_mode} grouping)")
    lines.append("Phrase-level with time ranges. Use `[start-end]` to address cuts.")
    lines.append("")

    if phrases:
        duration = phrases[-1]["end"] - phrases[0]["start"]
    else:
        duration = 0.0

    lines.append(f"## {name}  (duration: {format_duration(duration)}, {len(phrases)} phrases)")

    if not phrases:
        lines.append("  _no speech detected_")
        lines.append("")
        return "\n".join(lines)

    for p in phrases:
        spk_tag = f" {speaker}" if speaker else ""
        lines.append(f"  [{format_time(p['start'])}-{format_time(p['end'])}]{spk_tag} {p['text']}")

    lines.append("")

    # Add stats footer
    total_chars = sum(len(p["text"]) for p in phrases)
    lines.append(f"_Total: {total_chars} characters, {len(phrases)} phrases_")
    lines.append("")

    return "\n".join(lines)


def pack_metadata(
    metadata_path: Path,
    edit_dir: Path,
    speaker: str = "S0",
    group_by: str = "sentence",
    max_chars: int = 40,
    max_duration: float = 8.0,
) -> Path:
    """Pack TTS metadata into a lightweight transcript markdown file.

    Args:
        metadata_path: Path to input metadata.json.
        edit_dir: Edit output directory.
        speaker: Speaker label (e.g., "S0").
        group_by: 'sentence' = one phrase per sentence; 'auto' = smart grouping.
        max_chars: Max chars per phrase (auto mode only).
        max_duration: Max seconds per phrase (auto mode only).

    Returns:
        Path to the generated takes_packed.md.
    """
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    sentences = extract_sentences(metadata)

    if not sentences:
        sys.exit(f"no sentences with word timestamps found in {metadata_path}")

    if group_by == "auto":
        phrases = group_sentences_auto(sentences, max_chars, max_duration)
        mode_label = f"auto (≤{max_chars} chars, ≤{max_duration}s)"
    else:
        phrases = sentences
        mode_label = "sentence"

    markdown = render_markdown(
        phrases=phrases,
        name=metadata_path.stem,
        speaker=speaker,
        group_mode=mode_label,
    )

    # Write output
    edit_dir.mkdir(parents=True, exist_ok=True)
    out_path = edit_dir / "takes_packed.md"
    out_path.write_text(markdown, encoding='utf-8')

    total_duration = phrases[-1]["end"] - phrases[0]["start"] if phrases else 0
    total_chars = sum(len(p["text"]) for p in phrases)
    kb = out_path.stat().st_size / 1024

    print(f"packed: {metadata_path.name} → {out_path}")
    print(f"  {len(phrases)} phrases, {format_duration(total_duration)} total")
    print(f"  {total_chars} characters, {kb:.1f} KB")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pack TTS metadata.json into lightweight transcript markdown"
    )
    ap.add_argument("metadata", type=Path, help="Path to metadata.json file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <metadata_parent>/edit)",
    )
    ap.add_argument(
        "--speaker",
        type=str,
        default="S0",
        help="Speaker label (default: S0).",
    )
    ap.add_argument(
        "--group-by",
        type=str,
        choices=["sentence", "auto"],
        default="sentence",
        help="Phrase grouping mode: sentence (one per sentence) or auto (smart grouping). Default: sentence",
    )
    ap.add_argument(
        "--max-chars",
        type=int,
        default=40,
        help="Max characters per phrase (auto mode only). Default: 40",
    )
    ap.add_argument(
        "--max-duration",
        type=float,
        default=8.0,
        help="Max duration per phrase in seconds (auto mode only). Default: 8.0",
    )
    args = ap.parse_args()

    metadata_path = args.metadata.resolve()
    if not metadata_path.exists():
        sys.exit(f"metadata file not found: {metadata_path}")

    edit_dir = (args.edit_dir or (metadata_path.parent / "edit")).resolve()

    pack_metadata(
        metadata_path=metadata_path,
        edit_dir=edit_dir,
        speaker=args.speaker,
        group_by=args.group_by,
        max_chars=args.max_chars,
        max_duration=args.max_duration,
    )


if __name__ == "__main__":
    main()

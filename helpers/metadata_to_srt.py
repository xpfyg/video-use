"""Convert TTS metadata.json (word-level timestamps) to SRT subtitles.

Input format: ByteDance TTS metadata with events[].sentence.words[]
Each word has startTime, endTime, and word text.

Output: Standard SRT subtitle file. Subtitles are grouped by sentence
or by configurable max duration per cue.

Usage:
    python helpers/metadata_to_srt.py <metadata.json>
    python helpers/metadata_to_srt.py <metadata.json> -o output.srt
    python helpers/metadata_to_srt.py <metadata.json> --max-duration 5.0
    python helpers/metadata_to_srt.py <metadata.json> --mode sentence
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def format_srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    whole_secs = int(secs)
    millis = int((secs - whole_secs) * 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_secs:02d},{millis:03d}"


def extract_sentences(metadata: dict) -> list[dict]:
    """Extract sentences with word data from metadata.

    Returns list of {text, words: [{word, startTime, endTime}]}.
    Skips sentences with empty words list.
    """
    sentences: list[dict] = []
    events = metadata.get("events", [])

    for event in events:
        sentence = event.get("sentence")
        if not sentence:
            continue
        words = sentence.get("words", [])
        if not words:
            continue  # Skip sentences without word-level timestamps
        sentences.append({
            "text": sentence.get("text", ""),
            "words": words,
        })

    return sentences


def group_words_into_cues(
    all_words: list[dict],
    max_duration: float = 5.0,
    max_chars: int = 40,
) -> list[dict]:
    """Group word-level entries into SRT cues.

    Tries to keep sentences intact. If a sentence is too long, splits on
    punctuation or at word boundaries.

    Args:
        all_words: Flat list of all word entries with startTime/endTime.
        max_duration: Maximum duration per subtitle cue (seconds).
        max_chars: Approximate max characters per line (Chinese).

    Returns:
        List of {start, end, text} cues.
    """
    cues: list[dict] = []
    current_words: list[dict] = []
    current_start: float | None = None

    def flush() -> None:
        nonlocal current_words, current_start
        if not current_words:
            return
        text = "".join(w["word"] for w in current_words)
        end_time = current_words[-1]["endTime"]
        cues.append({
            "start": current_start,
            "end": end_time,
            "text": text,
        })
        current_words = []
        current_start = None

    for word in all_words:
        word_text = word.get("word", "")
        start = word.get("startTime", 0)
        end = word.get("endTime", 0)

        if current_start is None:
            current_start = start

        current_duration = end - current_start
        current_chars = sum(len(w["word"]) for w in current_words) + len(word_text)

        # Check if we need to split before adding this word
        should_split = False
        if current_words and current_duration > max_duration:
            should_split = True
        elif current_words and current_chars > max_chars:
            # Split before punctuation if possible, otherwise just split
            prev_word = current_words[-1]["word"]
            if any(p in prev_word for p in "，。！？、；："):
                should_split = True

        if should_split:
            flush()
            current_start = start

        current_words.append(word)

    flush()
    return cues


def generate_srt(cues: list[dict]) -> str:
    """Generate SRT format string from cues."""
    lines: list[str] = []
    for i, cue in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{format_srt_time(cue['start'])} --> {format_srt_time(cue['end'])}")
        lines.append(cue["text"])
        lines.append("")  # Blank line between cues
    return "\n".join(lines)


def convert_metadata_to_srt(
    metadata_path: Path,
    output_path: Path | None = None,
    max_duration: float = 5.0,
    mode: str = "sentence",
) -> Path:
    """Convert metadata.json to SRT subtitles.

    Args:
        metadata_path: Path to input metadata.json.
        output_path: Path to output SRT file. Defaults to same name with .srt.
        max_duration: Max seconds per subtitle cue.
        mode: 'sentence' = one cue per sentence; 'auto' = split by duration/chars.

    Returns:
        Path to the generated SRT file.
    """
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    sentences = extract_sentences(metadata)

    if not sentences:
        sys.exit(f"no sentences with word timestamps found in {metadata_path}")

    if mode == "sentence":
        # One SRT cue per sentence
        cues: list[dict] = []
        for sent in sentences:
            words = sent["words"]
            if not words:
                continue
            cues.append({
                "start": words[0]["startTime"],
                "end": words[-1]["endTime"],
                "text": sent["text"],
            })
    else:
        # Auto mode: flatten all words and split by duration/chars
        all_words: list[dict] = []
        for sent in sentences:
            all_words.extend(sent["words"])
        cues = group_words_into_cues(all_words, max_duration)

    srt_content = generate_srt(cues)

    if output_path is None:
        output_path = metadata_path.with_suffix(".srt")

    output_path.write_text(srt_content, encoding='utf-8')

    total_duration = cues[-1]["end"] - cues[0]["start"] if cues else 0
    print(f"converted: {metadata_path.name} → {output_path.name}")
    print(f"  cues: {len(cues)}, duration: {total_duration:.1f}s")

    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert TTS metadata.json to SRT subtitles"
    )
    ap.add_argument("metadata", type=Path, help="Path to metadata.json file")
    ap.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output SRT file path (default: same name with .srt)",
    )
    ap.add_argument(
        "--max-duration",
        type=float,
        default=5.0,
        help="Max duration per subtitle in seconds (auto mode only). Default: 5.0",
    )
    ap.add_argument(
        "--mode",
        type=str,
        choices=["sentence", "auto"],
        default="sentence",
        help="Subtitle grouping mode: sentence (one per sentence) or auto (split by duration). Default: sentence",
    )
    args = ap.parse_args()

    metadata_path = args.metadata.resolve()
    if not metadata_path.exists():
        sys.exit(f"metadata file not found: {metadata_path}")

    output_path = args.output.resolve() if args.output else None

    convert_metadata_to_srt(
        metadata_path=metadata_path,
        output_path=output_path,
        max_duration=args.max_duration,
        mode=args.mode,
    )


if __name__ == "__main__":
    main()

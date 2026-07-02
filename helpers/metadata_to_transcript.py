"""Convert TTS metadata.json directly to Scribe-compatible transcript JSON.

This preserves word-level timestamp precision — better than going through
SRT (which loses per-word timing). The output is compatible with
pack_transcripts.py and the rest of the video-use toolchain.

Input format: ByteDance TTS metadata with events[].sentence.words[]
Each word has startTime, endTime, confidence, and word text.

Output format: Scribe-compatible JSON with words[] array containing
type='word' | 'spacing' entries, each with start/end/speaker_id.

Usage:
    python helpers/metadata_to_transcript.py <metadata.json>
    python helpers/metadata_to_transcript.py <metadata.json> --edit-dir /custom/edit
    python helpers/metadata_to_transcript.py <metadata.json> --speaker S0
    python helpers/metadata_to_transcript.py <metadata.json> --audio-source audio.mp3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def extract_all_words(metadata: dict) -> list[dict]:
    """Extract all word entries from metadata, preserving order.

    Returns flat list of {word, startTime, endTime, confidence}.
    Skips sentences with empty words arrays.
    """
    all_words: list[dict] = []
    events = metadata.get("events", [])

    for event in events:
        sentence = event.get("sentence")
        if not sentence:
            continue
        words = sentence.get("words", [])
        if not words:
            continue  # Skip summary sentences without word timestamps
        all_words.extend(words)

    return all_words


def build_transcript_words(
    words: list[dict],
    speaker_id: str = "speaker_0",
    min_silence_for_spacing: float = 0.05,
) -> list[dict]:
    """Build Scribe-format words array with word and spacing entries.

    Args:
        words: Flat list of word entries with startTime/endTime.
        speaker_id: Speaker ID string.
        min_silence_for_spacing: Minimum silence gap to create a spacing entry.

    Returns:
        List of Scribe-format word entries with type, start, end, text, speaker_id.
    """
    result: list[dict] = []
    prev_end: float | None = None

    for w in words:
        start = w.get("startTime", 0)
        end = w.get("endTime", 0)
        text = w.get("word", "")

        # Add spacing entry if there's a gap from previous word
        if prev_end is not None and start > prev_end:
            gap = start - prev_end
            if gap >= min_silence_for_spacing:
                result.append({
                    "type": "spacing",
                    "start": round(prev_end, 3),
                    "end": round(start, 3),
                    "text": "",
                    "speaker_id": None,
                })

        # Add the word entry
        result.append({
            "type": "word",
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "speaker_id": speaker_id,
        })

        prev_end = end

    return result


def convert_metadata_to_transcript(
    metadata_path: Path,
    edit_dir: Path,
    speaker_id: str = "speaker_0",
    audio_source: str | None = None,
) -> Path:
    """Convert TTS metadata.json to Scribe-format transcript JSON.

    Args:
        metadata_path: Path to input metadata.json.
        edit_dir: Edit output directory.
        speaker_id: Speaker ID for the transcript.
        audio_source: Optional name of the source audio file.

    Returns:
        Path to the generated transcript JSON.
    """
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    words = extract_all_words(metadata)

    if not words:
        sys.exit(f"no words with timestamps found in {metadata_path}")

    transcript_words = build_transcript_words(words, speaker_id)

    # Build full text from words
    full_text = "".join(w["word"] for w in words)

    # Build Scribe-compatible response
    transcript = {
        "text": full_text,
        "words": transcript_words,
        "source": "tts_metadata",
        "metadata_file": metadata_path.name,
        "word_count": len(words),
        "note": "Generated from TTS metadata.json. Word-level timestamps are precise.",
    }

    if audio_source:
        transcript["audio_source"] = audio_source

    # Get speaker info from metadata if available
    summary = metadata.get("summary", {})
    if summary.get("speaker"):
        transcript["tts_voice"] = summary["speaker"]

    # Write output
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    output_name = metadata_path.stem
    out_path = transcripts_dir / f"{output_name}.json"

    out_path.write_text(
        json.dumps(transcript, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )

    duration = words[-1]["endTime"] - words[0]["startTime"] if words else 0
    print(f"converted: {metadata_path.name} → {out_path.name}")
    print(f"  words: {len(words)}, duration: {duration:.1f}s")
    print(f"  spacing entries: {len([w for w in transcript_words if w['type'] == 'spacing'])}")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert TTS metadata.json to Scribe-format transcript JSON"
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
        help="Speaker ID label (default: S0). Use S0, S1, etc.",
    )
    ap.add_argument(
        "--audio-source",
        type=str,
        default=None,
        help="Optional: name of the source audio file",
    )
    args = ap.parse_args()

    metadata_path = args.metadata.resolve()
    if not metadata_path.exists():
        sys.exit(f"metadata file not found: {metadata_path}")

    edit_dir = (args.edit_dir or (metadata_path.parent / "edit")).resolve()

    # Normalize speaker ID
    speaker_id = args.speaker
    if not speaker_id.startswith("speaker_"):
        if re.match(r'^S\d+$', speaker_id, re.IGNORECASE):
            speaker_id = f"speaker_{speaker_id[1:]}"
        else:
            speaker_id = f"speaker_{speaker_id}"

    convert_metadata_to_transcript(
        metadata_path=metadata_path,
        edit_dir=edit_dir,
        speaker_id=speaker_id,
        audio_source=args.audio_source,
    )


if __name__ == "__main__":
    main()

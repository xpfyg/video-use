"""Convert SRT subtitle files to Scribe-compatible transcript JSON.

This allows using video-use without ElevenLabs: provide your own SRT
subtitles + audio/video files, and we pack them into the same
`takes_packed.md` format for the editor to work with.

Word-level timestamps are estimated proportionally from character count
within each SRT cue. They're approximate — good enough for phrase-level
editing decisions, not for frame-perfect cuts.

Usage:
    python helpers/srt_to_transcript.py <srt_file>
    python helpers/srt_to_transcript.py <srt_file> --edit-dir /custom/edit
    python helpers/srt_to_transcript.py <srt_file> --speaker S0
    python helpers/srt_to_transcript.py <srt_file> --video-source video.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_srt_time(time_str: str) -> float:
    """Parse SRT timestamp like "00:01:23,456" to seconds."""
    time_str = time_str.strip().replace(',', '.')
    h, m, s = time_str.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(srt_path: Path) -> list[dict]:
    """Parse an SRT file into a list of cues.

    Each cue: {index, start, end, text}
    """
    content = srt_path.read_text(encoding='utf-8-sig')
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    blocks = re.split(r'\n\s*\n', content.strip())

    cues: list[dict] = []
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue

        idx = 0
        time_line_idx = 0
        if re.match(r'^\d+$', lines[0].strip()):
            idx = int(lines[0].strip())
            time_line_idx = 1

        if time_line_idx >= len(lines):
            continue

        time_line = lines[time_line_idx]
        time_match = re.match(
            r'(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)',
            time_line
        )
        if not time_match:
            continue

        start = parse_srt_time(time_match.group(1))
        end = parse_srt_time(time_match.group(2))
        text_lines = lines[time_line_idx + 1:]
        text = ' '.join(line.strip() for line in text_lines if line.strip())

        if text:
            cues.append({
                'index': idx,
                'start': start,
                'end': end,
                'text': text,
            })

    return cues


def split_into_words(text: str) -> list[str]:
    """Split text into word units. CJK chars become individual units."""
    tokens = text.split()
    words: list[str] = []

    for token in tokens:
        has_cjk = bool(re.search(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', token))
        if has_cjk:
            words.extend(list(token))
        else:
            words.append(token)

    return words


def estimate_word_timestamps(
    cues: list[dict],
    speaker_id: str = 'speaker_0',
) -> list[dict]:
    """Convert SRT cues to word-level entries with estimated timestamps.

    Returns a list compatible with Scribe's "words" array format.
    """
    words: list[dict] = []
    prev_end: float | None = None

    for cue in cues:
        cue_start = cue['start']
        cue_end = cue['end']
        cue_text = cue['text']
        cue_duration = cue_end - cue_start

        if prev_end is not None and cue_start > prev_end:
            words.append({
                'type': 'spacing',
                'start': prev_end,
                'end': cue_start,
                'text': '',
                'speaker_id': None,
            })

        word_list = split_into_words(cue_text)
        if not word_list:
            prev_end = cue_end
            continue

        total_weight = sum(len(w) for w in word_list)
        if total_weight == 0:
            total_weight = len(word_list)

        num_gaps = len(word_list) - 1
        inter_word_gap = 0.05
        total_gap_time = num_gaps * inter_word_gap
        available_time = max(cue_duration - total_gap_time, 0.1)

        current_time = cue_start

        for i, word in enumerate(word_list):
            weight = len(word) if len(word) > 0 else 1
            word_duration = (weight / total_weight) * available_time

            words.append({
                'type': 'word',
                'start': round(current_time, 3),
                'end': round(current_time + word_duration, 3),
                'text': word,
                'speaker_id': speaker_id,
            })

            current_time += word_duration

            if i < num_gaps:
                words.append({
                    'type': 'spacing',
                    'start': round(current_time, 3),
                    'end': round(current_time + inter_word_gap, 3),
                    'text': '',
                    'speaker_id': None,
                })
                current_time += inter_word_gap

        prev_end = cue_end

    return words


def convert_srt_to_transcript(
    srt_path: Path,
    edit_dir: Path,
    speaker_id: str = 'speaker_0',
    video_source: str | None = None,
) -> Path:
    """Convert an SRT file to a Scribe-format transcript JSON."""
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    output_name = srt_path.stem
    out_path = transcripts_dir / f"{output_name}.json"

    cues = parse_srt(srt_path)
    if not cues:
        sys.exit(f"no cues found in {srt_path}")

    words = estimate_word_timestamps(cues, speaker_id)

    transcript = {
        'text': ' '.join(cue['text'] for cue in cues),
        'words': words,
        'srt_source': srt_path.name,
        'word_timestamps': 'estimated (proportional to character count)',
        'note': 'Generated from SRT subtitles. Timestamps are approximate.',
    }

    if video_source:
        transcript['video_source'] = video_source

    out_path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False))

    print(f"converted: {srt_path.name} → {out_path.name}")
    word_count = len([w for w in words if w['type'] == 'word'])
    print(f"  cues: {len(cues)}, words: {word_count}")
    if cues:
        duration = cues[-1]['end'] - cues[0]['start']
        print(f"  duration: {duration:.1f}s")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert SRT subtitles to Scribe-format transcript JSON"
    )
    ap.add_argument("srt", type=Path, help="Path to SRT subtitle file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <srt_parent>/edit)",
    )
    ap.add_argument(
        "--speaker",
        type=str,
        default="S0",
        help="Speaker ID label (default: S0). Use S0, S1, etc.",
    )
    ap.add_argument(
        "--video-source",
        type=str,
        default=None,
        help="Optional: name of the source video file",
    )
    args = ap.parse_args()

    srt_path = args.srt.resolve()
    if not srt_path.exists():
        sys.exit(f"SRT file not found: {srt_path}")

    edit_dir = (args.edit_dir or (srt_path.parent / "edit")).resolve()

    speaker_id = args.speaker
    if not speaker_id.startswith("speaker_"):
        if re.match(r'^S\d+$', speaker_id, re.IGNORECASE):
            speaker_id = f"speaker_{speaker_id[1:]}"
        else:
            speaker_id = f"speaker_{speaker_id}"

    convert_srt_to_transcript(
        srt_path=srt_path,
        edit_dir=edit_dir,
        speaker_id=speaker_id,
        video_source=args.video_source,
    )


if __name__ == "__main__":
    main()

"""Convert SRT/VTT subtitle files to Scribe-style transcript JSON.

This allows using video-use without ElevenLabs API — bring your own subtitles.

Supports:
  - SRT format (.srt)
  - WebVTT format (.vtt)

Output format matches ElevenLabs Scribe JSON structure expected by
pack_transcripts.py and render.py:
  {
    "words": [
      {"type": "word", "text": "Hello", "start": 0.5, "end": 0.8, "speaker_id": "speaker_0"},
      {"type": "spacing", "start": 0.8, "end": 0.9},
      ...
    ]
  }

Note: SRT/VTT only have phrase-level timestamps. We estimate word-level
timestamps by distributing duration evenly across words. This is good enough
for editing decisions; for frame-perfect cuts you'll want word-level Scribe.

Usage:
    python helpers/subtitle_to_transcript.py <subtitle_file>
    python helpers/subtitle_to_transcript.py <subtitle_file> --edit-dir /custom/edit
    python helpers/subtitle_to_transcript.py <subtitle_file> --speaker speaker_0
    python helpers/subtitle_to_transcript.py folder/ --edit-dir folder/edit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_srt_time(time_str: str) -> float:
    """Parse SRT timestamp like '00:00:01,234' to seconds."""
    time_str = time_str.strip().replace(',', '.')
    parts = time_str.split(':')
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def parse_vtt_time(time_str: str) -> float:
    """Parse VTT timestamp like '00:00:01.234' or '00:01.234' to seconds."""
    time_str = time_str.strip()
    parts = time_str.split(':')
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
        return h * 3600 + m * 60 + s
    elif len(parts) == 2:
        m, s = int(parts[0]), float(parts[1])
        return m * 60 + s
    else:
        return float(time_str)


def parse_srt(content: str) -> list[dict]:
    """Parse SRT content into list of {start, end, text} entries."""
    entries = []
    blocks = re.split(r'\n\s*\n', content.strip())
    
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        
        # Skip numeric index line if present
        idx = 0
        if lines[0].strip().isdigit():
            idx = 1
        
        if idx >= len(lines):
            continue
        
        # Parse timestamp line
        time_line = lines[idx]
        time_match = re.search(
            r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})',
            time_line
        )
        if not time_match:
            continue
        
        start = parse_srt_time(time_match.group(1))
        end = parse_srt_time(time_match.group(2))
        
        # Rest is text
        text_lines = lines[idx + 1:]
        text = ' '.join(line.strip() for line in text_lines if line.strip())
        text = re.sub(r'<[^>]+>', '', text)  # strip HTML/formatting tags
        
        if text:
            entries.append({"start": start, "end": end, "text": text})
    
    return entries


def parse_vtt(content: str) -> list[dict]:
    """Parse VTT content into list of {start, end, text} entries."""
    entries = []
    lines = content.strip().split('\n')
    
    i = 0
    # Skip WEBVTT header
    while i < len(lines) and not lines[i].strip().startswith('WEBVTT'):
        i += 1
    if i < len(lines):
        i += 1  # skip WEBVTT line
    
    while i < len(lines):
        line = lines[i].strip()
        
        # Skip empty lines and cue identifiers
        if not line:
            i += 1
            continue
        
        # Check if this is a timestamp line
        time_match = re.search(
            r'(\d{2}:\d{2}(?::\d{2})?[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}(?::\d{2})?[,\.]\d{3})',
            line
        )
        
        if time_match:
            start = parse_vtt_time(time_match.group(1))
            end = parse_vtt_time(time_match.group(2))
            
            # Collect text lines
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip():
                text_parts.append(lines[i].strip())
                i += 1
            
            text = ' '.join(text_parts)
            text = re.sub(r'<[^>]+>', '', text)  # strip tags
            
            if text:
                entries.append({"start": start, "end": end, "text": text})
        else:
            # Probably a cue identifier or note, skip
            i += 1
    
    return entries


def parse_subtitle(file_path: Path) -> list[dict]:
    """Parse a subtitle file (SRT or VTT) based on extension."""
    content = file_path.read_text(encoding='utf-8-sig')  # BOM tolerant
    ext = file_path.suffix.lower()
    
    if ext == '.srt':
        return parse_srt(content)
    elif ext == '.vtt':
        return parse_vtt(content)
    else:
        # Try both
        try:
            return parse_srt(content)
        except Exception:
            return parse_vtt(content)


def phrases_to_scribe_words(
    phrases: list[dict],
    speaker_id: str = "speaker_0",
    default_gap: float = 0.15,
) -> list[dict]:
    """Convert phrase-level subtitles to Scribe-style word list.
    
    Distributes each phrase's duration evenly across its words.
    Adds spacing entries between phrases.
    """
    words: list[dict] = []
    prev_end: float | None = None
    
    for phrase in phrases:
        start = phrase["start"]
        end = phrase["end"]
        text = phrase["text"]
        
        # Add spacing if there's a gap from previous phrase
        if prev_end is not None and start > prev_end:
            gap = start - prev_end
            if gap > 0.01:  # only add meaningful gaps
                words.append({
                    "type": "spacing",
                    "start": prev_end,
                    "end": start,
                })
        
        # Split text into words (simple whitespace split)
        text_words = re.findall(r"\S+", text)
        if not text_words:
            continue
        
        duration = end - start
        if duration < 0:
            duration = 0
        
        # Distribute duration evenly across words
        if len(text_words) == 1:
            word_duration = duration
        else:
            # Leave small gaps between words
            total_gap = default_gap * (len(text_words) - 1)
            word_duration = max(0.05, (duration - total_gap) / len(text_words))
        
        current_time = start
        for i, w in enumerate(text_words):
            word_start = current_time
            word_end = word_start + word_duration
            
            # Clean up word (remove trailing punctuation for cleaner display)
            clean_word = w.rstrip('.,!?;:')
            
            words.append({
                "type": "word",
                "text": clean_word,
                "start": round(word_start, 3),
                "end": round(word_end, 3),
                "speaker_id": speaker_id,
            })
            
            current_time = word_end + default_gap
        
        prev_end = end
    
    return words


def convert_one(
    subtitle_path: Path,
    edit_dir: Path,
    speaker_id: str = "speaker_0",
    verbose: bool = True,
) -> Path:
    """Convert a single subtitle file to Scribe JSON. Returns output path."""
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{subtitle_path.stem}.json"
    
    phrases = parse_subtitle(subtitle_path)
    if not phrases:
        print(f"warning: no subtitle entries found in {subtitle_path}")
    
    words = phrases_to_scribe_words(phrases, speaker_id)
    
    transcript = {
        "text": " ".join(p["text"] for p in phrases),
        "words": words,
        "speaker_count": 1,
        "source": "subtitle_import",
        "subtitle_file": str(subtitle_path.name),
    }
    
    out_path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False))
    
    if verbose:
        kb = out_path.stat().st_size / 1024
        print(f"  converted: {subtitle_path.name} → {out_path.name}")
        print(f"    {len(phrases)} phrases → {len(words)} words, {kb:.1f} KB")
    
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert SRT/VTT subtitles to Scribe-style transcript JSON"
    )
    ap.add_argument(
        "input",
        type=Path,
        help="Path to subtitle file (.srt/.vtt) or a folder of subtitles",
    )
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <input_parent>/edit)",
    )
    ap.add_argument(
        "--speaker",
        type=str,
        default="speaker_0",
        help="Speaker ID to assign (default: speaker_0)",
    )
    ap.add_argument(
        "--silence-threshold",
        type=float,
        default=0.5,
        help="Silence threshold for packing (seconds). Default 0.5.",
    )
    ap.add_argument(
        "--pack",
        action="store_true",
        help="Also run pack_transcripts.py after conversion",
    )
    args = ap.parse_args()
    
    input_path = args.input.resolve()
    if not input_path.exists():
        sys.exit(f"input not found: {input_path}")
    
    # Determine edit_dir
    if args.edit_dir:
        edit_dir = args.edit_dir.resolve()
    else:
        if input_path.is_dir():
            edit_dir = input_path / "edit"
        else:
            edit_dir = input_path.parent / "edit"
    
    # Collect subtitle files
    subtitle_files: list[Path] = []
    if input_path.is_dir():
        for ext in ['.srt', '.vtt']:
            subtitle_files.extend(input_path.glob(f"*{ext}"))
        subtitle_files.sort()
    else:
        subtitle_files = [input_path]
    
    if not subtitle_files:
        sys.exit(f"no subtitle files found in {input_path}")
    
    print(f"Converting {len(subtitle_files)} subtitle file(s)...")
    
    for sf in subtitle_files:
        convert_one(sf, edit_dir, args.speaker)
    
    print(f"\nTranscripts saved to: {edit_dir / 'transcripts'}")
    
    if args.pack:
        print("\nPacking into takes_packed.md...")
        # Import and run pack_transcripts
        sys.path.insert(0, str(Path(__file__).parent))
        from pack_transcripts import pack_one_file, render_markdown
        
        transcripts_dir = edit_dir / "transcripts"
        json_files = sorted(transcripts_dir.glob("*.json"))
        
        entries = [pack_one_file(p, args.silence_threshold) for p in json_files]
        markdown = render_markdown(entries, args.silence_threshold)
        
        out_path = edit_dir / "takes_packed.md"
        out_path.write_text(markdown, encoding="utf-8")
        
        total_phrases = sum(len(e[2]) for e in entries)
        total_duration = sum(e[1] for e in entries)
        kb = out_path.stat().st_size / 1024
        print(f"  packed → {out_path}")
        print(f"  {total_phrases} phrases, {total_duration:.1f}s total, {kb:.1f} KB")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Match strategy shots to video clips using ARK API, outputting direct EDL format."""

from __future__ import annotations

import argparse
import json
import requests
import os
from pathlib import Path
from dotenv import load_dotenv


# Load .env file
load_dotenv()

DEFAULT_ARK_MODEL = "ep-20260702134855-4jqlj"
DEFAULT_ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
ARK_API_KEY = os.getenv("ARK_API_KEY", "")

PLAN_PROMPT = """你是一名专业的零食带货短视频剪辑师。请根据下面的策略模板和候选素材内容，直接生成 EDL (Edit Decision List) 格式的 JSON。

【策略模板 - Strategy】
{strategy}

【候选素材内容 - Content Report】
{content_report}


【要求】
1. 输出严格的 JSON 格式，不要有任何额外文字
2. 不要重复使用类似的素材切片,而是在类似的素材切片挑选质量最好的（非常重要）
3. 尽量让每个素材都至少出现一次
4. 切记不要使素材指定有废片的切片（非常重要）。
4. 选择画面内容最贴合台词的素材片段（非常重要）
6. 好的成片要覆盖远景,中景,镜像
5. 确保所选片段有足够的时长覆盖该镜头
6. 使用素材的原始文件名作为 source 键
7. 给每个选择提供合理的 reason
8. total_duration_s 一定是等于 Strategy 最后一个end时间, 输出的 ranges 中的end-start 的和要等于total_duration_s

【输出格式示例】
{{
  "version": 1,
  "sources": {{
    "C0103": "/absolute/path/to/C0103.MP4",
    "C0108": "/absolute/path/to/C0108.MP4"
  }},
  "ranges": [
    {{
      "source": "C0103",
      "start": 2.42,
      "end": 6.85,
      "beat": "HOOK",
      "quote": "这里的零食真的超好吃！",
      "reason": "画面清晰展示产品，动作自然"
    }},
    {{
      "source": "C0108",
      "start": 14.30,
      "end": 28.90,
      "beat": "SOLUTION",
      "quote": "大家一定要试试！",
      "reason": "模特试吃表情真实"
    }}
  ],
  "grade": "auto",
  "overlays": [],
  "subtitles": null,
  "total_duration_s": 87.4
}}

请直接输出完整的 JSON，不要添加任何 Markdown 格式或额外解释！"""


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def run_ark_text(prompt: str, model: str, ark_url: str = DEFAULT_ARK_URL, max_tokens: int = 32768) -> str:
    """Run ARK API model and return the raw generated text."""
    payload = {
        "model": model,
        "stream": False,
        "max_output_tokens": max_tokens,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt.strip()
                    }
                ]
            }
        ]
    }
   
    headers = {
        "Authorization": f"Bearer {ARK_API_KEY}",
        "Content-Type": "application/json",
    }
    
    try:
        print("[ark] calling model...")
        resp = requests.post(ark_url, json=payload, headers=headers)
        resp.raise_for_status()
        res_data = resp.json()
        
        # Find the message output and extract text
        for output_item in res_data.get("output", []):
            if output_item.get("type") == "message" and output_item.get("role") == "assistant":
                for content_item in output_item.get("content", []):
                    if content_item.get("type") == "output_text":
                        return content_item.get("text", "").strip()
        
        return ""
    except Exception as e:
        print(f"[ark] error: {e}")
        if 'resp' in locals():
            print(f"[ark] response status: {resp.status_code}")
            print(f"[ark] response: {resp.text}")
        return ""


def extract_json(text: str) -> str:
    """Extract JSON from text that may have extra content."""
    # Find first { and last }
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="Match strategy shots to video clips using ARK API, outputting EDL format")
    ap.add_argument("--edit-dir", type=Path, default=Path("./edit"), help="Project edit dir")
    ap.add_argument("--template", default="default", help="Strategy template name")
    ap.add_argument("--ark-url", default=DEFAULT_ARK_URL, help="ARK API URL")
    ap.add_argument("--ark-model", default=DEFAULT_ARK_MODEL, help="ARK model name")
    ap.add_argument("--grade", default="auto", help="Grade preset")
    ap.add_argument("--audio-track", type=Path, default=None, help="External audio/dubbing file")
    ap.add_argument("-o", "--output", type=Path, default=None, help="EDL output path")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    strategy = load_json(edit_dir / "strategy.json")
    visual_report = load_json(edit_dir / "visual_report.json")
    content_report = load_json(edit_dir / "content_report.json")

    if not strategy:
        print(f"error: {edit_dir / 'strategy.json'} not found. Run generate_strategy.py first.")
        return 1
    if not visual_report:
        print(f"error: {edit_dir / 'visual_report.json'} not found. Run analyze_visual.py first.")
        return 1
    if not content_report:
        print(f"error: {edit_dir / 'content_report.json'} not found. Run analyze_content.py first.")
        return 1

    # Get the selected template
    tmpl = strategy
    if not tmpl:
        raise ValueError(f"Unknown template '{args.template}'. Available: {list(strategy['templates'].keys())}")

    # Build sources mapping from visual report
    sources = {}
    for name, entry in visual_report.get("clips", {}).items():
        sources[name] = entry.get("file", str(edit_dir / f"{name}.MOV"))

    # Prepare prompt content
    prompt = PLAN_PROMPT.format(
        strategy=json.dumps({"templates": {args.template: tmpl}, "total_duration_s": strategy.get("total_duration_s")}, indent=2, ensure_ascii=False),
        content_report=json.dumps(content_report, indent=2, ensure_ascii=False)
    )

    # Get response from ARK
    response = run_ark_text(prompt, args.ark_model, args.ark_url, max_tokens=32768)
    if not response:
        print("error: no response from model")
        return 1

    # Extract and parse JSON
    json_str = extract_json(response)
    try:
        edl = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"error: failed to parse model response as JSON: {e}")
        print("\nResponse from model:")
        print(response)
        return 1

    # Ensure required fields exist
    edl.setdefault("version", 1)
    edl.setdefault("sources", sources)
    edl.setdefault("ranges", [])
    edl.setdefault("grade", args.grade)
    edl.setdefault("overlays", [])
    edl.setdefault("subtitles", None)
    
    if args.audio_track:
        edl["audio_track"] = str(args.audio_track.resolve())
    
    # Calculate total duration if not provided
    if "total_duration_s" not in edl:
        total = sum(r.get("end", 0) - r.get("start", 0) for r in edl.get("ranges", []))
        edl["total_duration_s"] = round(total, 3)

    # Write output
    out_path = args.output or (edit_dir / "edl.json")
    out_path.write_text(json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")
    
    print(f"\nedl → {out_path}")
    print(f"total video duration: {edl.get('total_duration_s', 0):.2f}s")
    print(f"matched {len(edl.get('ranges', []))} shot(s)")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

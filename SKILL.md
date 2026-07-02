---
name: video-use
description: 通过对话编辑任意视频。针对使用 byte-dance TTS metadata.json（字符级时间戳）的 TTS 生成内容进行了优化。同时支持基于 Apple Silicon 的本地视觉模型驱动的零食带货短视频工作流。支持剪辑、调色、生成 overlay 动画、烧录字幕 —— 适用于口播、蒙太奇、教程、产品演示、短视频。无预设，无菜单。先问问题，确认方案，执行，迭代，持久化。生产正确性规则是硬性的；其他都是艺术自由。
---

## 工作流程

本技能采用**5步流水线**处理视频素材：

1. **策略生成** - 根据音频 metadata 生成多个剪辑策略模板
2. **视觉分析** - 抽帧并分析每条素材的视觉质量指标
3. **内容理解** - 使用视觉模型描述素材内容，生成素材目录
4. **片段匹配** - AI 根据内容描述和策略自动匹配合适的视频片段
5. **渲染成片** - 生成最终视频并烧录字幕

**物料流：**
```
TTS metadata ──> strategy.json ──> match_shots.py ──> edl.json
                                              ↑
视频素材 ──> visual_report.json ──> content_report.json ──┘
```

**素材：**
- 配音：`test_materials/hubang_beef_sauce.mp3`
- 字幕/时间戳：`test_materials/hubang_beef_sauce_metadata.json`
- 视频素材：`test_materials/*.MOV`

**执行步骤：**

```bash
# 1. 生成剪辑策略 基于 metadata.json 在edit-dir 输出 strategy.json 以及 strategy.md  还有 subtitles.srt
python helpers/generate_strategy.py test_materials/hubang_beef_sauce_metadata.json --edit-dir ./edit_test

# 2. 抽帧以及视觉质量分析（首次运行后会命中缓存） 基于视频素材 在edit-dir 输出 visual_report.json
python helpers/analyze_visual.py test_materials/*.MOV --edit-dir ./edit_test

# 3. 基于本地模型描述素材内容（首次运行后会命中缓存）基于 visual_report.json 在edit-dir 输出 content_report.json 以及 visual_catalog.md
python helpers/analyze_content.py --edit-dir ./edit_test

# 4. 模型匹配片段 基于 content_report.json 以及 strategy.json 在edit-dir 输出 edl.json
python helpers/match_shots.py --edit-dir ./edit_test 

# 5. 渲染成片 基于 edl.json 以及音频文件 在edit-dir 输出 final.mp4
python helpers/render.py edit_test/edl_hubang.json -o edit_test/final.mp4 \
  --audio-track test_materials/hubang_beef_sauce.mp3
```

**输出文件：**
- `edit_test/strategy.json` / `strategy.md` —— 剪辑策略（多模板）
- `edit_test/visual_report.json` —— 每条素材的 CV 质量指标
- `edit_test/content_report.json` —— VLM 生成的素材内容描述
- `edit_test/visual_catalog.md` —— 给 LLM/人工复核的素材文档
- `edit_test/edl_hubang.json` —— 剪辑决策
- `edit_test/final_hubang.mp4` —— 最终成片

*** EDL format:**
```
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```
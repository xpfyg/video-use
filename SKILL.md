---
name: video-use
description: 通过对话编辑任意视频。针对使用 byte-dance TTS metadata.json（字符级时间戳）的 TTS 生成内容进行了优化。同时支持基于 Apple Silicon 的本地视觉模型驱动的零食带货短视频工作流。支持剪辑、调色、生成 overlay 动画、烧录字幕 —— 适用于口播、蒙太奇、教程、产品演示、短视频。无预设，无菜单。先问问题，确认方案，执行，迭代，持久化。生产正确性规则是硬性的；其他都是艺术自由。
---

**素材：**
- 配音：`test_materials/hubang_beef_sauce.mp3`
- 字幕/时间戳：`test_materials/hubang_beef_sauce_metadata.json`
- 视频素材：`test_materials/*.MOV`

**执行步骤：**

```bash
# 1. 生成剪辑策略
python helpers/generate_strategy.py test_materials/hubang_beef_sauce_metadata.json --edit-dir ./edit_test

# 2. 视觉质量分析（首次运行后会命中缓存）
python helpers/analyze_visual.py test_materials/*.MOV --edit-dir ./edit_test

# 3. 内容文档生成（首次运行后会命中缓存）
python helpers/analyze_content.py --edit-dir ./edit_test

# 4. 模型匹配片段
python helpers/match_shots.py --edit-dir ./edit_test --template default \
  --audio-track test_materials/hubang_beef_sauce.mp3 -o edit_test/edl_hubang.json

# 5. 渲染成片
python helpers/render.py edit_test/edl_hubang.json -o edit_test/final_hubang.mp4 \
  --audio-track test_materials/hubang_beef_sauce.mp3
```

**输出文件：**
- `edit_test/strategy.json` / `strategy.md` —— 剪辑策略（多模板）
- `edit_test/visual_report.json` —— 每条素材的 CV 质量指标
- `edit_test/content_report.json` —— VLM 生成的素材内容描述
- `edit_test/visual_catalog.md` —— 给 LLM/人工复核的素材文档
- `edit_test/edl_hubang.json` —— 剪辑决策
- `edit_test/final_hubang.mp4` —— 最终成片

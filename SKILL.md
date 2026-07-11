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

**执行步骤（运行前检查）：**

> ⚠️ 智能体必须遵循的缓存原则：在执行任何脚本之前，先检查目标输出文件是否已存在。如果文件已存在且内容非空，**默认跳过该步骤**，不要重复运行。仅在用户明确要求重新生成、或已知参数/素材发生变化时，才添加 `--force` 重新执行。

各步骤对应检查文件：
- 步骤1：`edit-dir/strategy.json` 或 `edit-dir/strategy.md`
- 步骤2：`edit-dir/visual_report.json`
- 步骤3：`edit-dir/content_report.json` 或 `edit-dir/visual_catalog.md`
- 步骤4：`edit-dir/edl.json`
- 步骤5：`edit-dir/final.mp4`

```bash
# 1. 生成剪辑策略 基于 metadata.json 在edit-dir 输出 strategy.json 以及 strategy.md  还有 subtitles.srt
python helpers/generate_strategy.py test_materials/hubang_beef_sauce_metadata.json --edit-dir ./edit_test

# 2. 抽帧以及视觉质量分析（如 visual_report.json 已存在则跳过）基于视频素材 在edit-dir 输出 visual_report.json
python helpers/analyze_visual.py test_materials/*.MOV --edit-dir ./edit_test

# 3. 基于本地模型描述素材内容（如 content_report.json 已存在则跳过）基于 visual_report.json 在edit-dir 输出 content_report.json 以及 visual_catalog.md
# 对素材分析本身很费时间,这个脚本执行比较费事时间，可能长达十分钟到半小时左右,可检查日志或者后台运行,不要钱轻易判定为超时,每隔几分钟检查是否有文件生成,生成成功且不为空则说明分析完成。
python helpers/analyze_content.py --edit-dir ./edit_test

# 4. 模型匹配片段（如 edl.json 已存在则跳过）基于 content_report.json 以及 strategy.json 在edit-dir 输出 edl.json
python helpers/match_shots.py --edit-dir ./edit_test 

# 5. 渲染成片（如 final.mp4 已存在则跳过）基于 edl.json 以及音频文件 在edit-dir 输出 final.mp4
python helpers/render.py edit_test/edl_hubang.json -o edit_test/final.mp4 \
  --audio-track test_materials/hubang_beef_sauce.mp3
```

**输出文件：**
- `edit_test/strategy.json` / `strategy.md` —— 剪辑策略
- `edit_test/subtitles.srt` —— 字幕文件（按逗号分组，每个镜头一条字幕）
- `edit_test/visual_report.json` —— 每条素材的 CV 质量指标
- `edit_test/content_report.json` —— VLM 生成的素材内容描述
- `edit_test/visual_catalog.md` —— 给 LLM/人工复核的素材文档
- `edit_test/edl.json` —— 剪辑决策
- `edit_test/final.mp4` —— 最终成片

---

## 脚本命令行参数

### 1. generate_strategy.py — 策略生成

基于 TTS metadata.json 生成剪辑策略、人类可读摘要和字幕 SRT 文件。

```
python helpers/generate_strategy.py <metadata.json> [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `script` | **(必选)** metadata.json 文件路径 | — |
| `--edit-dir` | 输出目录 | `./edit` |
| `--model` | 本地 mlx-vlm 模型，用于生成 beat 标签和画面建议 | `mlx-community/Qwen2-VL-2B-Instruct-4bit` |
| `--offline` | 跳过模型调用，使用基于关键词的规则 fallback | 关闭 |

**输出：** `strategy.json`、`strategy.md`、`subtitles.srt`

---

### 2. analyze_visual.py — 视觉质量分析

抽帧并分析每条素材的视觉质量指标（清晰度、曝光、饱和度、对比度、稳定度、镜头边界）。

```
python helpers/analyze_visual.py <videos...> [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `videos` | **(必选，多文件)** 视频素材文件路径 | — |
| `--edit-dir` | 输出目录 | `./edit` |
| `--sample-fps` | 抽帧帧率（帧/秒） | `1.0` |
| `--max-size` | 分析用最长边像素 | `480` |
| `--shot-threshold` | 镜头边界检测阈值 | `0.35` |
| `--force` | 强制重新分析，忽略缓存 | 关闭 |

**输出：** `visual_report.json`（含 `cache/frames/` 抽帧图片）

**缓存策略：** 根据文件 mtime + size 判断是否需要重新分析，已分析过的素材会命中缓存直接跳过。

---

### 3. analyze_content.py — 内容理解

使用视觉模型描述素材内容，生成自然语言摘要和素材目录。 对素材分析本身很费时间,这个脚本执行比较费事时间，可能长达十分钟到半小时左右,可检查日志或者后台运行,不要钱轻易判定为超时,每隔几分钟检查是否有文件生成,生成成功且不为空则说明分析完成。

```
python helpers/analyze_content.py [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--edit-dir` | 项目目录（需含 `visual_report.json`） | `./edit` |
| `--model` | 模型标识符（覆盖默认模型） | Ollama 模式(默认)：`qwen3-vl:8b`；本地模式：`mlx-community/Qwen2-VL-2B-Instruct-4bit` |
| `--window-size` | 每窗口秒数（仅 mlx-vlm 模式） | `3.0` |
| `--max-frame-tokens` | mlx-vlm 最大 token 数 | `128` |
| `--use-ollama` / `--no-use-ollama` | 使用 Ollama 还是本地 mlx-vlm 进行分析 | 默认开启 Ollama |
| `--ollama-url` | Ollama API 地址 | 从 `.env` 的 `OLLAMA_URL` 读取 |
| `--ollama-model` | Ollama 模型名称 | `qwen3-vl:8b` |
| `--force` | 强制重新生成，忽略缓存 | 关闭 |

**输出：** `content_report.json`、`visual_catalog.md`

**缓存策略：** 按 clip_name 缓存，同一模型+相同参数下命中缓存则跳过。

---

### 4. match_shots.py — 片段匹配

根据策略模板和素材内容描述，通过 ARK API 自动生成 EDL。

```
python helpers/match_shots.py [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--edit-dir` | 项目目录（需含 `strategy.json`、`visual_report.json`、`content_report.json`） | `./edit` |
| `--ark-url` | ARK API 地址 | `https://ark.cn-beijing.volces.com/api/v3/responses` |
| `--ark-model` | ARK 模型名称 | `ep-20260702134855-4jqlj` |
| `--grade` | 色彩分级预设 | `auto` |
| `--audio-track` | 外部配音音频文件路径 | 无 |
| `-o`, `--output` | EDL 输出路径 | `./edit/edl.json` |

**输出：** `edl.json`

---

### 5. render.py — 渲染成片

根据 EDL 渲染最终视频，支持调色、字幕烧录和响度归一化。视频片段不含原素材音频，仅使用外部音频轨。

```
python helpers/render.py <edl.json> [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `edl` | **(必选)** EDL JSON 文件路径 | — |
| `-o`, `--output` | **(必选)** 输出视频路径 | — |
| `--preview` | 预览模式：1080p, medium, CRF 22 | 关闭 |
| `--draft` | 草稿模式：720p, ultrafast, CRF 28（仅校验剪辑点） | 关闭 |
| `--build-subtitles` | 从转录文件 + EDL 偏移生成 master.srt | 关闭 |
| `--no-subtitles` | 跳过字幕（即使 EDL 中配置了） | 关闭 |
| `--no-loudnorm` | 跳过响度归一化 | 关闭（默认 -14 LUFS / -1 dBTP / LRA 11） |
| `--audio-track` | 外部配音音频文件（替换最终音轨） | 无 |

**输出：** 最终视频文件

**画质梯度：**
| 模式 | 分辨率 | preset | CRF | 用途 |
|------|--------|--------|-----|------|
| 默认（final） | 1080p | fast | 20 | 最终成片 |
| `--preview` | 1080p | medium | 22 | QC 评估 |
| `--draft` | 720p | ultrafast | 28 | 剪辑点校验 |

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
  "grade": "snack_vivid",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

---

### 6. classify_materials.py — 素材动作分类标签

对中间产物目录 `cache/frames/<素材>/` 下的每个素材目录，抽取 N 张代表性图片（默认 3 张，按时间轴均匀取样），发给 Ollama 视觉模型（默认 `qwen3-vl:8b`），让其从给定动作标签里**选一个**作为该素材分类标签，最终生成机器可读的 `material_labels.json` 与人工可读的 `material_labels.md`。

帧布局与 `analyze_visual.py` 保持一致（`frame_0000_<t>s.jpg`），抽出的帧会拷贝到 `cache/selected/<素材>/` 便于复核。

```
python helpers/classify_materials.py --edit-dir ./edit
python helpers/classify_materials.py --edit-dir ./edit --category 商品
python helpers/classify_materials.py --edit-dir ./edit --labels-file ./my_labels.json --category 商品
python helpers/classify_materials.py --edit-dir ./edit --labels 拿起产品,放下产品,开盖展示
python helpers/classify_materials.py --edit-dir ./edit --force
```

**标签由 `labels.json`（大分类 → 标签列表 映射）驱动**：用 `--category` 指定大分类，脚本自动取该分类对应的标签组。默认读取 skill 根目录的 `labels.json`；`--labels-file` 可换文件；`--labels` 可显式逗号分隔覆盖（优先级最高）。大分类不存在时报错并列出可用分类。

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--edit-dir` | 中间产物目录（含 `cache/`） | `./edit` |
| `--cache-dir` | cache 目录 | `<edit-dir>/cache` |
| `--frames-root` | 素材目录根（其下每个子目录 = 一个素材） | `<cache>/frames` |
| `--materials-dir` | 直接指定素材目录根（覆盖 frames-root） | 无 |
| `--output` | 标签 JSON 输出 | `<edit-dir>/material_labels.json` |
| `--output-md` | 标签 MD 输出 | `<edit-dir>/material_labels.md` |
| `--num-frames` | 每素材抽取图片数 | `3` |
| `--model` | Ollama 视觉模型 | `qwen3-vl:8b` |
| `--ollama-url` | Ollama API 地址 | 从 `.env` 的 `OLLAMA_URL` 读取 |
| `--ollama-api-key` | Ollama API Key | 从 `.env` 的 `OLLAMA_API_KEY` 读取 |
| `--category` | 大分类（决定从 `labels.json` 取哪组标签，也用于提示词） | `冷饮` |
| `--labels-file` | 分类标签配置文件（大分类→标签列表 映射） | skill 根目录 `labels.json` |
| `--labels` | 显式候选标签（逗号分隔），优先级高于配置文件 | 无 |
| `--force` | 忽略已有标签文件，全部重分类 | 关闭 |
| `--allow-unclassified` | 模型不可用时仍写出 null 标签并标记 `needs_review` | 关闭 |
| `--dry-run` | 仅抽帧，不调用模型 | 关闭 |

**默认标签组（来自 `labels.json`）：**
- `冷饮`：拿起产品、放下产品、开盖展示、饮料气泡特写、配料表、倒饮品动作
- `商品`：拆包装、倒出零食、拆袋展示、食物细节、配料表

**缓存策略：** 与技能其它步骤一致——已分类且无需复核的素材命中缓存直接跳过，`--force` 才重跑。

---

## 调色使用指南

调色通过在 EDL (`edl.json`) 中设置 `grade` 字段控制，支持三种方式。`match_shots.py` 生成 EDL 时可通过 `--grade` 参数指定默认值。

### 预设名称

直接使用预设名，如 `"grade": "snack_vivid"`，可选的预设：

| 预设 | 效果 | 适用场景 |
|------|------|----------|
| `snack_vivid` | 对比度 +8%, 饱和度 +12%, S 曲线 | **零食带货**首选，增强食欲感 |
| `neutral_punch` | 对比度 +6%, 轻微 S 曲线 | 通用画面提亮 |
| `subtle` | 对比度 +3%, 饱和度 -2% | 极小干预的底线校正 |
| `warm_cinematic` | 暖阴影、冷高光、电影感曲线 | 复古/氛围感，日常不推荐 |
| `none` | 无调色 | 跳过调色 |

### 自动调色 (`"grade": "auto"`)

渲染时对每个片段单独分析帧统计，自动生成针对性的轻微校正。推荐作为默认值。

### 原始 FFmpeg 滤镜

直接写 FFmpeg 滤镜串，如：

```json
"grade": "eq=contrast=1.1:saturation=1.15,curves=master='0/0 0.5/0.55 1/1'"
```

含 `=` 或 `,` 的字符串会被当作原始滤镜处理，短标识符则匹配预设。
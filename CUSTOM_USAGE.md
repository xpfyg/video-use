# video-use 定制版使用指南（无 ElevenLabs）

基于 TTS metadata.json 的视频编辑工作流，无需 ElevenLabs API。

## 核心变化

- **移除 ElevenLabs 依赖**：不需要 Scribe 转录服务
- **输入改为 TTS metadata**：直接使用字节 TTS 生成的字级别时间戳 JSON
- **新增转换工具**：支持 metadata.json → SRT / transcript / 打包文本

## 工作流

### 第一步：准备素材

```
你的项目目录/
├── dubbing.mp3       # 音频文件（或视频文件）
└── metadata.json     # 字节 TTS 输出的字级别时间戳
```

### 第二步：生成轻量化文本（LLM 阅读视图）

**推荐方式：直接打包（最简洁）**

```bash
python helpers/pack_metadata.py metadata.json
```

输出：`edit/takes_packed.md`

可选参数：
- `--edit-dir <dir>`：指定输出目录（默认：`./edit`）
- `--speaker S0`：说话人标签（默认：S0）
- `--group-by sentence`：按句子分组（默认）
- `--group-by auto --max-chars 30`：自动分组，每段最多 30 字

**示例输出：**
```
## metadata  (duration: 17.0s, 7 phrases)
  [000.35-002.81] S0 我终于找到心心念念的泰国下饭酱了，
  [003.06-004.64] S0 就是这个虎邦泰式打抛酱，
  [004.96-008.14] S0 它是肉香混着浓浓的罗勒香，一口下去酸辣鲜爽，
  ...
```

### 其他转换方式

**方式二：生成 SRT 字幕**

```bash
python helpers/metadata_to_srt.py metadata.json
```

输出：`metadata.srt`

可选参数：
- `-o output.srt`：指定输出文件
- `--mode sentence`：每句一条字幕（默认）
- `--mode auto --max-duration 5.0`：自动分割，每条最多 5 秒

**方式三：生成 Scribe 格式 transcript JSON**

如果你需要使用原始的 `pack_transcripts.py` 或其他 Scribe 兼容工具：

```bash
python helpers/metadata_to_transcript.py metadata.json
python helpers/pack_transcripts.py --edit-dir ./edit --silence-threshold 0.2
```

注意：中文 TTS 的句子停顿较短，建议用 `--silence-threshold 0.2`（默认 0.5 太大）。

### 第三步：编辑决策

LLM 阅读 `takes_packed.md`，进行：
- 内容理解
- 剪辑点选择
- 策略制定

### 第四步：视频渲染

使用原有工具链：
- `timeline_view.py` - 时间轴可视化
- `render.py` - 视频渲染
- `grade.py` - 调色

## 新增工具一览

| 工具 | 用途 | 输入 | 输出 |
|------|------|------|------|
| `helpers/pack_metadata.py` | **推荐**：直接打包轻量化文本 | metadata.json | takes_packed.md |
| `helpers/metadata_to_srt.py` | 转 SRT 字幕 | metadata.json | .srt 文件 |
| `helpers/metadata_to_transcript.py` | 转 Scribe 格式 transcript | metadata.json | transcripts/*.json |
| `helpers/srt_to_transcript.py` | SRT 转 transcript（估算时间戳） | .srt 文件 | transcripts/*.json |

## 输入格式说明

### metadata.json（字节 TTS 格式）

```json
{
  "summary": {
    "output": "音频文件路径",
    "speaker": "音色ID",
    "duration": "总时长"
  },
  "events": [
    {
      "sentence": {
        "text": "完整句子文本",
        "words": [
          {
            "word": "我",
            "startTime": 0.355,
            "endTime": 0.475,
            "confidence": 0.958
          }
        ]
      }
    }
  ]
}
```

关键点：
- 字级别的精确时间戳（startTime, endTime）
- 每个事件（event）可能包含一个句子（sentence）
- 有些 sentence 的 words 数组为空（摘要句），会自动跳过

## 与原始版本的对比

| 特性 | 原始版本（ElevenLabs） | 定制版本（TTS metadata） |
|------|----------------------|------------------------|
| 转录方式 | ElevenLabs Scribe API | 直接使用 TTS 字级时间戳 |
| 时间戳精度 | 词级别（ASR 识别） | 字级别（精确到毫秒） |
| 中文支持 | 一般 | 优秀（原生中文） |
| 费用 | 按用量付费 | 免费 |
| 速度 | 取决于 API | 本地即时转换 |
| 字间空格 | 有（词级别） | 无（更自然的中文） |

## 硬规则（生产正确性，不可违反）

以下规则与原始版本一致，必须遵守：

1. **字幕最后应用**：在所有叠加层之后，否则叠加层会遮挡字幕
2. **分段提取 + 无损拼接**：避免二次编码
3. **每段边界 30ms 音频淡入淡出**：避免切割处的爆音
4. **叠加层使用 setpts 时间偏移**
5. **主 SRT 使用输出时间线偏移**
6. **不在词中间切割**：切割点必须对齐到字/词边界
7. **切割边界留余量**：30-200ms 工作窗口
8. **所有输出在 `<项目目录>/edit/` 中**

## 快速开始

```bash
# 1. 进入你的项目目录
cd /path/to/your/video/project

# 2. 直接打包生成轻量化文本
python /path/to/video-use/helpers/pack_metadata.py metadata.json

# 3. 查看结果
cat edit/takes_packed.md
```

## 常见问题

**Q: 为什么推荐用 pack_metadata.py 而不是 pack_transcripts.py？**

A: pack_metadata.py 专门针对中文 TTS 优化，输出更自然的中文（无字间空格），并且直接利用 metadata 中的句子结构，分组更合理。

**Q: 我需要 SRT 字幕文件怎么办？**

A: 使用 `metadata_to_srt.py`，支持 sentence 和 auto 两种模式。

**Q: 我只有 SRT 字幕，没有 metadata.json 怎么办？**

A: 使用 `srt_to_transcript.py` 将 SRT 转为 transcript JSON（时间戳为估算值，精度较低），然后用 `pack_transcripts.py` 打包。

---

## 零食带货工作流

适用于零食带货短视频。核心原则：**先让视觉模型分析素材内容和质量，生成素材文档；再用文本模型根据口播/分镜需求去匹配画面**。不依赖手写关键词规则。

目前实现的是 **音频优先（audio-first）** 工作流：用户提供配音和字幕，系统生成剪辑策略，然后在素材中自动匹配每个口播片段最合适的画面。

### 准备工作

```
你的项目目录/
├── raw/                  # 多条 1 分钟以内的素材
├── dubbing.mp3           # 已处理好的配音音轨
├── metadata.json         # 字节 TTS 输出的字级时间戳（或 subtitles.srt）
└── edit/                 # 输出目录（自动创建）
```

### 安装依赖

```bash
pip install -e ".[snack_visual]"
```

### 完整流程

```bash
# 1. 进入项目目录
cd /path/to/your/snack_project

# 2. 根据配音/字幕生成剪辑策略（多模板）
python /path/to/video-use/helpers/generate_strategy.py metadata.json --edit-dir ./edit
# 或只有 SRT 字幕时：
# python /path/to/video-use/helpers/generate_strategy.py subtitles.srt --edit-dir ./edit

# 3. 视觉质量分析（480p@2fps，按文件 mtime+size 缓存）
python /path/to/video-use/helpers/analyze_visual.py raw/*.MP4 --edit-dir ./edit

# 4. 用本地视觉大模型生成素材内容文档
python /path/to/video-use/helpers/analyze_content.py --edit-dir ./edit \
    --model mlx-community/Qwen2-VL-2B-Instruct-4bit

# 5. 文本模型根据口播语义匹配最佳素材
python /path/to/video-use/helpers/match_shots.py --edit-dir ./edit \
    --template default --audio-track dubbing.mp3

# 6. 渲染成片
python /path/to/video-use/helpers/render.py edit/edl.json \
    -o edit/final.mp4 --audio-track dubbing.mp3

# 7. 快速预览（720p）
python /path/to/video-use/helpers/render.py edit/edl.json \
    -o edit/preview.mp4 --preview --audio-track dubbing.mp3
```

### 输出文件

- `edit/strategy.json` / `strategy.md` — 剪辑策略（多个模板）
- `edit/visual_report.json` — 每条素材的 CV 质量指标
- `edit/content_report.json` — 视觉模型生成的素材内容描述
- `edit/visual_catalog.md` — 给 LLM/人工复核看的素材文档
- `edit/edl.json` — 剪辑决策
- `edit/final.mp4` — 最终成片

### 关键参数

- `--template default|fast|closeup`：选择剪辑节奏模板
- `--offline`：跳过模型调用，使用规则兜底（仅测试/无模型环境）
- `--model`：指定本地 mlx-vlm 模型，默认 `mlx-community/Qwen2-VL-2B-Instruct-4bit`
- `--grade snack_vivid`：食物食欲感调色（可在 `render.py` 使用）

### 注意事项

- 分析阶段默认采样 **480p @ 2fps**，M1 Pro 16GB 下峰值内存约 300MB/条。
- 内容描述默认使用 **Qwen2-VL-2B 4bit**，约 3GB 内存。
- 语义匹配也使用同系列模型纯文本模式，约 1.5GB 内存。
- 如果素材全程模糊/过曝，`quality_tier` 会标记为 C，匹配时会优先避开。
- `render.py --audio-track` 会用配音替换素材原声。

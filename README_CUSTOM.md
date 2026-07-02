# video-use 定制版使用说明（无需 ElevenLabs）

## 概述

这是 video-use 的定制版本，支持直接使用你自己的字幕文件（SRT/VTT），无需调用 ElevenLabs Scribe API。

## 环境要求

### 必需
- **ffmpeg** + **ffprobe**：已安装在 `/opt/homebrew/bin/`
- **Python 3.10+**：已安装
- **Python 依赖**：requests, librosa, matplotlib, pillow, numpy — 已安装

### 可选
- **yt-dlp**：用于下载在线视频源（未安装，可通过 `brew install yt-dlp` 安装）

## 快速开始

### 1. 准备你的素材

创建一个文件夹，放入你的视频/音频文件和对应的字幕文件：

```
my_video_project/
├── interview_take1.mp4
├── interview_take1.srt      # 对应的字幕文件
├── interview_take2.mp4
└── interview_take2.srt
```

### 2. 转换字幕为转录格式

进入你的项目文件夹，运行字幕转换脚本：

```bash
# 转换单个字幕文件
python3 ~/Developer/video-use/helpers/subtitle_to_transcript.py interview_take1.srt

# 转换整个文件夹的字幕
python3 ~/Developer/video-use/helpers/subtitle_to_transcript.py .

# 转换并直接打包成 takes_packed.md（推荐）
python3 ~/Developer/video-use/helpers/subtitle_to_transcript.py . --pack
```

输出会生成在 `<项目文件夹>/edit/` 目录下：
- `edit/transcripts/*.json` — 转换后的转录 JSON 文件
- `edit/takes_packed.md` — 打包后的轻量化转录文本（LLM 阅读用）

### 3. 开始编辑

有了 `takes_packed.md` 之后，就可以进行视频编辑了。

**典型工作流：**
1. 查看 `takes_packed.md` 了解所有素材内容
2. 制定剪辑策略（哪些片段保留，哪些剪掉）
3. 创建 EDL（编辑决策列表）JSON 文件
4. 使用 `render.py` 渲染最终视频

## 命令参考

### subtitle_to_transcript.py

将 SRT/VTT 字幕转换为 Scribe 格式的转录 JSON。

```bash
python3 ~/Developer/video-use/helpers/subtitle_to_transcript.py <输入文件或文件夹> [选项]
```

**选项：**
- `--edit-dir <目录>`：指定输出目录（默认：输入文件同级的 edit/）
- `--speaker <ID>`：指定说话人 ID（默认：speaker_0）
- `--pack`：转换后自动打包生成 takes_packed.md
- `--silence-threshold <秒>`：打包时的静音阈值（默认：0.5 秒）

### pack_transcripts.py

将转录 JSON 打包成可读的 takes_packed.md。

```bash
python3 ~/Developer/video-use/helpers/pack_transcripts.py --edit-dir <edit目录>
```

### render.py

根据 EDL 渲染最终视频。

```bash
python3 ~/Developer/video-use/helpers/render.py <edl.json> -o final.mp4
```

### timeline_view.py

生成时间轴预览图（胶片条 + 波形 + 文字标签）。

```bash
python3 ~/Developer/video-use/helpers/timeline_view.py <视频文件> <开始时间> <结束时间>
```

## 注意事项

### 1. ffmpeg 路径

如果脚本找不到 ffmpeg，请先设置 PATH：

```bash
export PATH="/opt/homebrew/bin:$PATH"
```

或者在运行脚本前确保 ffmpeg 在 PATH 中。

### 2. 字幕精度说明

SRT/VTT 字幕只有**短语级**时间戳，转换后会估算词级时间戳（按词数平均分配时长）。

- ✅ 足够用于：剪辑决策、粗剪、内容浏览
- ⚠️ 不够精确：帧级精剪、字幕对齐
- 🔧 如需更高精度：建议使用 ElevenLabs Scribe 或其他词级转录工具

### 3. 多说话人

如果你的字幕有多个说话人，可以分多个字幕文件，分别指定 speaker ID：

```bash
python3 ~/Developer/video-use/helpers/subtitle_to_transcript.py speakerA.srt --speaker speaker_0
python3 ~/Developer/video-use/helpers/subtitle_to_transcript.py speakerB.srt --speaker speaker_1
```

### 4. 字幕格式支持

- ✅ SRT 格式 (.srt)
- ✅ WebVTT 格式 (.vtt)
- 自动检测格式（根据文件扩展名）

## 目录结构

```
~/Developer/video-use/
├── SKILL.md              # 主 skill 文档
├── install.md            # 安装说明
├── pyproject.toml        # Python 依赖
├── helpers/              # 辅助脚本
│   ├── subtitle_to_transcript.py  # 【新增】字幕转转录脚本
│   ├── transcribe.py              # 原始 ElevenLabs 转录脚本
│   ├── pack_transcripts.py        # 打包转录为 markdown
│   ├── render.py                  # 渲染视频
│   ├── timeline_view.py           # 时间轴预览
│   └── grade.py                   # 颜色分级
└── skills/               # 子 skill（如 manim-video）
```

## 更新 skill

```bash
cd ~/Developer/video-use && git pull --ff-only
```

如果依赖有变化，重新安装：
```bash
pip3 install -e .
```

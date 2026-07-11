# 项目长期记忆 · video-use skill

## 运行环境要点（重要，跨会话复用）
- **Python 运行**：`cv2`/`numpy`/`PIL`/`requests`/`dotenv` 仅在 miniconda python
  (`/Users/chihuan/miniconda3/bin/python3`) 可用；受管 venv (`binaries/python/envs/default`)
  没有 cv2。所有 helpers/*.py（依赖 cv2）应用 miniconda python 运行。
- **Ollama 是远程服务**：`OLLAMA_URL` 在 `.env` 中指向远程地址（非 localhost），
  调用需带 `Authorization: Bearer $OLLAMA_API_KEY`。可用视觉模型含
  `qwen3-vl:8b`（默认）、`qwen2.5vl:3b`、`gemma3:4b`。
- Ollama 调用约定（见 `analyze_content.py` / `classify_materials.py`）：
  POST `OLLAMA_URL`，payload 顶层放 `model`/`temperature`/`max_tokens`/`format:"json"`/
  `messages:[{role:user, content, images:[base64]}]`。

## 缓存目录约定
- `analyze_visual.py` 抽帧输出：`cache/frames/<clip>/frame_0000_<t>s.jpg`（文件名含时间戳）。
- `classify_materials.py` 选中帧：`cache/selected/<素材>/sel_XX_<原文件名>`。
- 各步骤遵循「已生成则跳过」缓存原则，重跑用 `--force`。

## 标签体系
- 冷饮带货默认动作标签：拿起产品、放下产品、开盖展示、饮料气泡特写、配料表、倒饮品动作。
  由 `classify_materials.py` 的 `DEFAULT_LABELS` 定义，可用 `--labels-file` 覆盖。

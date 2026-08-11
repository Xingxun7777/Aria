# Aria 数据与隐私说明

本页说明 Aria 1.5 默认保存什么、什么情况下会联网，以及如何关闭或清理。

## 默认结论

- 语音识别默认在本机完成。
- AI 润色和云端识别救援默认关闭。
- 屏幕 OCR 默认在本机运行；屏幕感知增强默认关闭。
- 自动热词学习与审查默认关闭。
- 显式纠正规则只有在用户说出“纠正 A 为 B”或在管理界面添加后才产生，始终只在本机应用。
- 调试录音默认不保存。
- 完整窗口标题、OCR 正文和屏幕截图默认不写入日志或图片文件。
- GPU 引擎的 `llama-server` 只监听 `127.0.0.1`。
- API Key 使用当前 Windows 用户的 DPAPI 加密，不能直接复制到另一台机器使用。

## 本地保存的数据

| 数据 | 典型位置 | 用途与保留 |
|---|---|---|
| 设置、热词、替换规则 | `config/` | 持久保存；更新时保留 |
| 识别历史 | `data/history/` | 默认保留 90 天，可在界面清理 |
| 提醒与记录 | `data/` | 用户主动创建 |
| 调试日志 | `DebugLog/` | 默认 14 天 / 500 MB 上限 |
| OCR 短期缓存 | 内存 | 30 秒作为刷新与 ASR 新鲜度窗口；同窗口缓存可保留到被替换或退出，少量跨窗口上下文最多约 3 分钟 |
| 自动热词候选 | `data/auto_hotwords.json` | 仅 `auto_hotword.enabled=true` 时产生；用户可在设置中审查/清理 |
| 显式纠正规则 | `data/explicit_corrections.jsonl` | 仅保存用户明确提交的原词、新词和时间；可在“纠正规则”窗口查看或停用 |
| OCR 原文样本 | `data/ocr_samples/` | 仅 `auto_hotword.sample_logging.enabled=true` 时按每日上限保存 |
| ASR/OCR/润色模型 | `models/` 或包内模型目录 | 本地推理资产 |
| API Key | 配置文件中的 `dpapi:v1:` 密文 | 仅当前 Windows 用户可解密 |

运行时 JSON、历史、日志和模型均被 Git 忽略，不属于公开源码。

### 显式纠正规则

“小助手纠正 A 为 B”与“编辑 A 为 B”是两条独立路径。前者把原词和正确写法写入本机 `data/explicit_corrections.jsonl`，从下一次语音识别起应用；后者只修改当前可安全寻址的文本，不会学习。纠正规则不保存周围句子、窗口标题、文档名、应用名或文件路径，也不会发送给润色或热词审查 API。

右键 Aria 后打开“纠正规则”，可以查看、手动添加或停用规则；也可以说“小助手撤销上一次纠正”。公开源码同步和发布包净化都会排除已有的 `data/explicit_corrections.jsonl`，新安装包不会携带开发机上的个人规则。

## 默认不会发生的事情

- 不会持续把麦克风音频上传到服务器。
- 不会把屏幕截图上传到项目维护者或润色 API；可选联网功能传递的是文字或音频，不是截图图片。
- 不会把 API Key 写进公开仓库、更新包或发布说明。
- 不会在后台启用 AI 润色、自动热词学习或云端救援；这三项都需要用户配置并开启。

## 联网能力

### 1. AI 润色

配置字段：`polish.enabled`，默认 `false`。

开启后，会把本次识别文本发送到用户配置的兼容 API。若 `screen_context_enabled=true`（默认），请求还会包含当前前台进程的应用名称和推断出的场景类别，用于调整润色语气；关闭该项即可省略这部分应用上下文，它不包含窗口标题或截图。若同时显式开启 `vad.screen_ocr_polish`，请求还会包含用于纠错的屏幕 OCR 文字摘要。返回结果只用于本次文字修正。缺少 `screen_ocr_polish` 字段按关闭处理。

默认推荐模型为 `deepseek-v4-flash`，但用户可以改成其他兼容接口和模型。

### 2. 自动热词审查

配置字段：`auto_hotword.enabled`，默认 `false`。

开启后，Aria 会在本地提取和持久化 OCR 候选词。达到审查阈值且 API Key 可用时，会把候选词、出现次数和最多少量窗口标题样本发送给用户配置的热词审查 API；未配置专用端点时可能复用主润色 API。缺少 `auto_hotword.enabled` 字段按关闭处理。

`auto_hotword.sample_logging.enabled` 默认也是 `false`。只有显式开启后，才把受限数量的 OCR 原文保存到 `data/ocr_samples/`。

### 3. 云端识别救援

配置字段：`asr_rescue.cloud_enabled`，默认 `false`。

只有本地最终转写超时、异常或为空，且用户已经配置救援 API 时，失败语段才会发送到对应服务进行二次转写。

### 4. 自动更新

Aria 会从 GitHub 获取 `release-manifest.json`，并在有新版本时下载经过大小和 SHA256 校验的 source ZIP。更新只替换程序文件，配置、历史和模型继续保留。

个人构建带有 `PERSONAL_BUILD.txt`，会跳过公开更新通道。

### 5. GPU 资产获取

用户在悬浮窗右键菜单确认安装 GPU 加速时，Aria 会调用包内 Python，从 GitHub Releases 和 Hugging Face 兼容端点下载固定版本的 llama.cpp CUDA 文件与公开 GGUF 模型；包根的 `Install_GPU.cmd` 是 Aria 关闭时的备用入口。下载支持断点续传，对四个下载件执行固定 SHA256 校验，不上传本地数据。校验后会在本机回环地址短暂启动模型服务，并调用本机 `nvidia-smi` 确认 GPU 实际生效；全部通过后才切换运行配置。

### 6. 本机 GPU 服务

`qwen3_llamacpp` 会启动 `llama-server.exe` 并绑定 `127.0.0.1`。识别音频只在本机进程之间传递。默认端口为 `18539`，可在配置中修改。

该端口没有对局域网或公网监听。若端口已被不匹配的进程占用，Aria 会停止启动 GPU 引擎并回退，而不是结束该进程。

## 麦克风与敏感调试数据

麦克风只在用户触发听写、持续监听或明确开启的唤醒流程中使用。内存中的音频在识别后释放。

只有设置环境变量 `ARIA_DEBUG_SAVE_AUDIO=1` 时，调试 WAV 才会写入 `DebugLog/`。这些文件可能包含真实语音，不应分享。

OCR 截图默认只在内存中处理，不保存图片。常规 OCR 与屏幕上下文日志只记录后端、耗时、长度、哈希和通用场景类别，不记录前台应用名称、窗口标题或 OCR 正文；只有设置 `ARIA_DEBUG_SAVE_SCREEN_TEXT=1` 时，前台应用名称、窗口标题预览和完整 OCR 上下文才会进入诊断日志，OCR 正文还会写入 `DebugLog/screen_text_dump.log`。这些内容可能包含私人消息、文档、账号名或路径，排查结束后应关闭该环境变量并删除日志。

屏幕 OCR 的完整分层、缓存和路由说明见 [屏幕 OCR 上下文模块](OCR.md)。

## 彻底关闭联网功能

1. 保持 `polish.enabled = false`。
2. 保持 `vad.screen_ocr_polish = false`。
3. 保持 `auto_hotword.enabled = false`。
4. 保持 `asr_rescue.cloud_enabled = false`。
5. 不运行 `Install_GPU.cmd`。
6. 如不希望检查更新，在设置中关闭自动更新。
7. 使用标准版 `qwen3_sherpa`，或确保 GPU 引擎仍只绑定默认回环地址。

## 清理本地数据

- 在设置/历史界面清理历史记录。
- 正常退出 Aria 后，可删除 `DebugLog/` 中的日志。
- 关闭自动热词学习后，可删除 `data/auto_hotwords.json` 和 `data/ocr_samples/`。
- 可在“纠正规则”窗口逐条停用；正常退出 Aria 后也可删除 `data/explicit_corrections.jsonl` 清空全部显式纠正规则。
- 删除不再需要的 `data/` 内容前先备份自己的提醒和记录。
- 删除模型只会释放空间；下次使用对应引擎时需要重新准备资产。

不要把完整 `config/`、`DebugLog/`、`data/`、OCR dump 或录音文件直接附到公开 Issue。报告问题时先删除密钥、个人文本、路径和窗口内容。

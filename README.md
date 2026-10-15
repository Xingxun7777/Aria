<div align="center">

# Aria

**Windows 上的本地语音输入法：按一下反引号，说话就出字；说错了不用删，喊一声「小助手」让它改。**

识别全程在你自己的电脑上完成，不需要账号，没有显卡也能用。

[![Version](https://img.shields.io/badge/version-1.7.1-blue.svg)](https://github.com/Xingxun7777/Aria/releases/latest)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#系统要求)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

[English](README_EN.md) · [下载](#下载) · [三分钟上手](#三分钟上手) · [能做什么](#能做什么) · [隐私边界](#默认状态与联网边界) · [工作原理](#工作原理) · [参与开发](CONTRIBUTING.md)

</div>

<!--
演示 GIF 位：录一段「按反引号 → 说一句话自动上屏 → 说“小助手，把刚才那句改得自然一点”原地改写」，
把 GIF 拖进任意 GitHub Issue / Release 评论框，得到 github.com/user-attachments 链接后替换本注释：
![Aria 演示](https://github.com/user-attachments/assets/xxxxxxxx)
-->

---

## 下载

[**前往最新 Release 下载 `Aria-v1.7.1-Windows.zip`**](https://github.com/Xingxun7777/Aria/releases/latest)（约 1.2 GB，解压即用）

| 文件 | 用途 |
|---|---|
| `Aria-v1.7.1-Windows.zip` | 唯一的新安装包；CPU 解压即用，包内含 GPU 一键安装器 |
| `Aria-source-1.7.1.zip` | 应用内自动更新载荷；普通新安装不需要下载 |

- 没有 NVIDIA 显卡：解压后直接双击 `Aria.exe`，默认使用 CPU 轻量引擎。
- 有 NVIDIA 显卡：先正常启动 Aria，再从悬浮窗右键菜单点击“GPU 加速”。未安装时会提示下载约 3.1 GB 的固定 GPU 资产并持续显示进度；逐项校验和显卡实机验证全部通过后，Aria 会自动切换到 GPU，无需重启。GPU 安装失败不会把 CPU 配置改坏，修好网络、驱动或磁盘空间后重跑即可，下载支持断点续传；应用内入口无法使用时，也可以在 Aria 关闭后双击包根的 `Install_GPU.cmd`。
- GitHub 直连很慢时，可以把完整下载链接粘在 `https://ghfast.top/` 或 `https://gh-proxy.com/` 后面试试加速。应用内的更新检查和 GPU 资产下载在直连失败时会自动走这些镜像与 `hf-mirror.com`，下载件仍要通过固定 SHA256 校验。
<!-- 国内网盘镜像位：上传 Aria-v1.7.1-Windows.zip 后在此追加一行「- 国内镜像：<链接>（与 GitHub 同一文件，SHA256 …）」 -->

维护者另有约 4.8 GB 的完整 GPU 目录做发行验收，其单个归档超过 GitHub Release 的单资产限制，不作为公开附件。旧版只用于历史追溯，不再作为新安装入口。

## 三分钟上手

1. 下载 `Aria-v1.7.1-Windows.zip`，完整解压到普通可写目录；不要在压缩包预览窗口内直接运行。
2. 双击 `Aria.exe`，默认先以 CPU 模式启动。首次运行如遇 Windows SmartScreen 提示「已保护你的电脑」，点「更多信息 → 仍要运行」即可——这是未签名开源程序的正常提示，仅首次出现，此后的应用内更新不会再弹。
3. 把光标放到任何输入框，按反引号键 `` ` `` 开始录音，正常说话——每说完一句稍作停顿（约 1.5 秒），文字自动上屏，中途不用按任何键；不再输入时再按一次关闭。
4. NVIDIA GPU 用户可在悬浮窗右键菜单点击“GPU 加速”，按提示完成安装并自动切换，识别质量更高。
5. 想让它帮你改字、翻译、总结，先配好 AI 接口（见下方[开启 AI 润色](#开启-ai-润色可选)）。之后录音开着直接说「小助手，帮我把刚才那句话改得更自然一点」，刚才上屏的内容会被原地改写。

不配置 API Key 也能完成本地识别、热词纠错、文字输入和本地类语音指令（截图、提醒、打开程序等）；语音修改与翻译、总结、回复这类 AI 指令需要先配好 API Key。

## 能做什么

- **说话出字，全程本地**：标准版无需显卡；GPU 形态使用本机 llama.cpp 服务。录音不上传、不落盘。
- **说错了直接让它改**：刚说上屏的内容，喊一声唤醒词直接让它改，不用删掉重打（用法见下一节）。
- **上屏找回**：一句话因为切了窗口没落到想要的位置，把光标放好后说「小助手，上屏」，刚才那句会重新输入到光标处；不会替你发送，剪贴板里原有的图片或文件也不会被冲掉。
- **语音指令**：截图、提醒、打开程序或路径本地即可用（在资源管理器或 Cursor / VS Code 文件树里选中文件后说「打开目录」同样有效）；配好 API 后支持翻译、总结、回复。
- **热词与同音纠错**：支持权重、替换规则、拼音近似和屏幕专名学习，专业词、人名不再反复打错。
- **屏幕 OCR 上下文**：读取当前窗口标题、UI Automation 文本和本地截图识别结果作为纠错证据，默认只用最轻的一层，原文不会直接插入输出。
- **三档润色**（可选）：逐字保真 / 顺畅口语 / 结构化文档。
- **声学防线与识别自愈**：VAD、能量、峰值与置信度联合判断，减少低音量吞字和安静环境幻觉；超时、异常或空结果会触发引擎重建和救援策略。
- **剪贴板保护**：恢复文字、图片、文件列表和空剪贴板；退出前冲刷等待中的恢复任务。
- **自动更新**：校验更新载荷后只替换程序文件，保留配置、历史和模型。

### 语音修改：说错了直接让它改

三步：① 按一次反引号开始录音，正常说话——每说完一句稍作停顿（约 1.5 秒），文字自动上屏，中途不用按键；② 想改刚上屏的内容，录音开着就直接说，句首喊默认唤醒词「小助手」加上你的要求，稍作停顿它就原地改写，不用选中文字（已经关了录音的话，再按反引号打开后说也行，刚上屏的内容约 3 分钟内都能改）；③ 全部说完，再按一次反引号关闭录音。微信 / QQ 聊天框、Word / WPS 文档、终端里刚输入的内容都能改。

可以这样说（均为真实可用句式）：

- 「小助手，这句话太啰嗦，精简一点」
- 「小助手，刚刚说的那段帮我润色一下」
- 「小助手，把上面的内容写得更自然一点」
- 「小助手，帮我把上一句话里天气挺好改成天气挺坏」
- 改完不满意，接着说：「小助手，把上一句话再精简一点」——三分钟内都会在上一版的基础上继续调
- 只要意见、不动原文：「小助手，看看这句话哪里不好」

说明：语音修改走 AI 改写，需要先配置 API Key（见下方[开启 AI 润色](#开启-ai-润色可选)），未配置时 Aria 只会提示、不会动原文。唤醒词要说在句首；改写期间切了窗口、或原文被手动编辑过，Aria 会自动放弃这次覆盖，不会把内容写错地方。唤醒词可在 设置 → 语音指令 里改成自己喜欢的叫法，改完默认的「小助手」依然有效。选中一段文字后还可以说「小助手翻译成英文 / 总结一下 / 帮我回复」。

### 开启 AI 润色（可选）

1. 右键托盘图标 → 设置 → API。
2. 使用 DeepSeek 推荐配置或填写兼容接口。
3. 粘贴自己的 API Key，测试成功后保存。
4. 按需开启“屏幕感知增强”。

开启后，识别文本以及必要的屏幕上下文会发送给所配置的 API。具体边界见 [数据与隐私说明](docs/PRIVACY_DATA.md)。

## 系统要求

| 项目 | 标准版 | GPU 形态 |
|---|---|---|
| 操作系统 | Windows 10/11 64 位 | Windows 10/11 64 位 |
| CPU | x64，建议 4 核以上 | x64 |
| 内存 | 8 GB，建议 16 GB | 建议 16 GB |
| 显卡 | 不需要 | NVIDIA CUDA 显卡，建议至少 6 GB 可用显存 |
| Python | 便携包不需要；源码开发使用 3.12 | 同左 |

## 默认状态与联网边界

| 场景 | 默认值 | 是否联网 | 说明 |
|---|---|---|---|
| 标准版 | `qwen3_sherpa` | 否 | sherpa-onnx int8 + Qwen3-ASR 0.6B，纯 CPU |
| GPU 形态 | `qwen3_llamacpp` | 仅本机回环 | llama.cpp CUDA + Qwen3-ASR 1.7B Q8；识别请求只发往 `127.0.0.1` |
| 源码模板 | `qwen3` | 否 | 可选 PyTorch 运行时；开发者也可切换到另外两个运行时 |
| AI 润色 | 关闭 | 开启后会联网 | 默认模型 `deepseek-v4-flash`；没有 Key 时不调用 |
| 云端识别救援 | 关闭 | 开启后会联网 | 只有显式配置并启用后才上传失败语段 |
| 屏幕 OCR 采集 | 关闭 | 否 | 可在“自动学习的热词”中显式开启；截图仅在本地内存处理 |
| 屏幕感知增强 | 关闭 | 开启后随润色联网 | 只有与 AI 润色同时显式开启，才把文字摘要交给用户配置的 API |
| 自动热词学习 | 关闭 | 开启审查后会联网 | 默认不持久化 OCR 候选词，也不调用热词审查 API |
| 润色风格 | 顺畅口语 | 取决于润色开关 | 默认整理口头重复与语气词，不擅自改写意思 |
| 收音模式 | 正常 | 否 | 可切换嘈杂 / 轻语 |
| 调试录音 | 不保存 | 否 | 只有设置 `ARIA_DEBUG_SAVE_AUDIO=1` 才落盘 |

> Qwen3-ASR 官方模型覆盖 30 种语言与 22 种中文方言。不同 Aria 运行时使用不同量化和推理后端，能力与速度仍以实际硬件和音频为准。

### 数据与隐私

- 麦克风音频默认只进入本地 ASR，处理完成后不保存原始 WAV。
- OCR 窗口截图默认只在内存中处理，不保存图片；默认日志只写后端、耗时、长度和哈希，不写完整页面文字。
- 热词、替换规则、历史记录、提醒和日志只存于当前安装目录。
- API Key 使用 Windows DPAPI 加密；发布包不携带维护者或构建机器配置。
- AI 润色、云端救援、更新检查和 GPU 资产下载是独立联网能力，说明和关闭方式见 [数据与隐私说明](docs/PRIVACY_DATA.md)。
- `llama-server` 的 HTTP 端口只监听本机回环地址，不是公网服务。

安全问题请按 [安全策略](SECURITY.md) 私下报告。

## 工作原理

Aria 的目标不是把语音识别单独做成演示，而是把“按下热键—说话—纠错—输入—恢复现场”做成可长期使用的完整链路：识别默认在本机完成；热词、屏幕证据和近期上下文只用于纠正专名与同音词，不改变原意；AI 润色与云端救援都是可选能力，默认关闭；借用剪贴板后必须恢复原来的文字、图片、文件或空状态；配置、历史和模型属于用户数据，更新程序不能覆盖它们。

```text
前台窗口 ── 标题 / UI Automation / 窗口截图
                         │
                         ▼
              本地 ScreenOCR 上下文缓存
                         ├── fast：过滤后的短关键词 → ASR context
                         ├── quality：显式开启后 → AI 润色证据
                         └── 可选自动热词学习（默认关）

热键 / 持续监听 / 唤醒词
            │
音频采集 → DSP / VAD / 声学判定
            │
┌─────────────────────────────────────────────┐
│ qwen3_sherpa │ qwen3_llamacpp │ qwen3      │
│ CPU 0.6B     │ GPU 1.7B Q8    │ PyTorch    │
└─────────────────────────────────────────────┘
            │
            ├── 最终转写失败 → 引擎自愈 / 可选云端救援
            ▼
热词纠错 + 近期上下文 + 受控的屏幕证据
            ├── 可选 AI 润色
            ▼
输出注入 + 剪贴板恢复 + 历史记录 + UI 状态
```

四个 ASR 类型共用同一条后处理、输出和历史链路：

| `asr_engine` | 运行时 | 用途 |
|---|---|---|
| `qwen3_sherpa` | sherpa-onnx int8 | 标准版默认；无显卡也能运行 |
| `qwen3_llamacpp` | llama.cpp CUDA + GGUF | GPU 形态默认；常驻本机 `llama-server` 子进程 |
| `qwen3` | PyTorch | 源码/legacy 环境兼容 |
| `funasr` | FunASR Paraformer | 中文备用引擎 |

详细切换、文件布局和诊断方法见 [引擎指南](docs/ENGINES.md)。

### 屏幕 OCR 上下文模块

OCR 在 Aria 中不是附属截图功能，而是与 ASR、热词、润色并列的上下文子系统：

- **三层取证**：窗口标题立即可用；原生应用优先读取 UI Automation 文档文本；浏览器、终端和自绘界面由窗口截图 OCR 补齐。
- **本地多级回退**：`PP-OCRv5 DirectML → PP-OCRv5 CPU → RapidOCR v4 CPU → Windows OCR → 仅标题`。
- **崩溃隔离**：DirectML 跑在独立 OCR worker；驱动或 ONNX Runtime native 崩溃只会结束 worker，主程序自动切 CPU。
- **延迟受控**：OCR 后台运行并按窗口缓存；fast 模式永不等待，quality 模式只在确有屏幕证据需求时做预测式有界等待。
- **证据而非输入**：OCR 原文不会直接插入输出。屏幕采集、屏幕增强、自动热词学习和完整文字诊断日志都需要显式开启。

完整数据流、默认值、缓存策略、后端诊断和隐私边界见 [屏幕 OCR 模块说明](docs/OCR.md)。

### 运行进程

任务管理器里会看到这几个进程：

| 进程 | 来源与职责 |
|---|---|
| `Aria.exe` | 启动器；负责定位便携运行时并启动应用 |
| `AriaRuntime.exe` | Aria 使用的嵌入式 CPython 运行时；写入 Aria Project 的文件说明、版本和图标，避免任务管理器显示成匿名 `pythonw.exe` |
| `llama-server.exe` | llama.cpp 上游组件；仅在 GPU 引擎启用时运行，保留上游文件名和身份，不伪装成 Aria |

GPU 服务只绑定 `127.0.0.1`。Aria 只管理自己启动且资产匹配的子进程；发现端口被其他程序占用时会停止切换，不会结束无关进程。

## 从源码运行

```powershell
git clone https://github.com/Xingxun7777/Aria.git
cd Aria
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config\hotwords.template.json config\hotwords.json
.\.venv\Scripts\python.exe launcher.py
```

源码模板默认使用 `qwen3` PyTorch CUDA 运行时，首次加载模型时需要联网下载模型资产；上面的 CUDA 12.8 安装行与仓库锁定版本一致。只想直接使用 CPU 版时，请优先下载 Release 的标准版，而不是安装整套开发依赖。

运行时配置文件已被 `.gitignore` 排除。不要提交 `config/hotwords.json`、`wakeword.json`、日志、历史、模型或 API Key。

开发环境、测试命令与目录职责见 [贡献指南](CONTRIBUTING.md)。

## 项目结构

公开仓库是每次发布生成的运行源码快照，目录只包含应用源码、出厂模板、运行资产和用户文档：

```text
Aria/
├── app.py                    # 应用状态机、录音与 ASR 编排
├── launcher.py               # 单实例、启动画面、更新恢复
├── aria/                     # 稳定的 Python 包入口
├── core/
│   ├── asr/                  # 三个 Qwen3 运行时、FunASR、救援与声学策略
│   ├── audio/                # 音频采集、DSP、VAD、增益
│   ├── context/              # 三层屏幕 OCR、独立 DML worker、缓存与上下文路由
│   ├── hotword/              # 热词、纠错、润色
│   ├── history/              # 历史记录
│   ├── selection/            # 选区操作
│   ├── trigger/              # 热键/按住说话状态机
│   └── wakeword/             # 唤醒词与语音指令
├── system/                   # 全局热键、输出、剪贴板与权限
├── ui/qt/                    # PySide6 界面
├── config/                   # 可公开模板；运行时 JSON 不入库
├── assets/                   # 图标、VAD 与界面运行资产
├── docs/                     # 配置、引擎、OCR、数据与隐私说明
├── .github/                  # Issue / PR 模板
├── CONTRIBUTING.md           # 贡献约定
└── SECURITY.md               # 安全报告与支持边界
```

发布构建、维护者测试、一次性诊断和本机配置不进入公开快照。公开仓的每个版本仍可直接检查完整运行源码，并可针对当前快照提交 Issue 或 Pull Request。

## 配置速查

| 字段 | 默认值 |
|---|---|
| `general.hotkey` | 反引号键 `` ` `` |
| 语音唤醒词（`wakeword.json`） | 小助手（设置 → 语音指令 可改） |
| `polish.enabled` | `false` |
| `polish.model` | `deepseek-v4-flash` |
| `polish_style` | `smooth` |
| `asr_rescue.enabled` | `true` |
| `asr_rescue.cloud_enabled` | `false` |
| `vad.screen_ocr` | `false` |
| `vad.screen_ocr_polish` | `false` |
| `vad.screen_ocr_use_dml` | `true` |
| `vad.screen_ocr_force_cpu` | `false` |
| `auto_hotword.enabled` | `false` |
| `audio.capture_mode` | `standard` |
| `output.typewriter_mode` | `true` |

完整字段见 [配置参考](docs/CONFIGURATION.md)。

## 许可证与致谢

本项目采用 [Apache License 2.0](LICENSE)。

主要上游组件：

- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) — Apache 2.0
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) — Apache 2.0
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — MIT
- [FunASR](https://github.com/modelscope/FunASR) — MIT
- [Silero VAD](https://github.com/snakers4/silero-vad) — MIT
- [PySide6](https://www.qt.io/) — LGPL v3
- [RapidOCR](https://github.com/RapidAI/RapidOCR) — Apache 2.0

项目依赖与第三方许可证以各组件随附许可为准。

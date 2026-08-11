<div align="center">

# Aria — Windows 本地 AI 语音输入

**本地语音识别 · 本地屏幕 OCR 上下文 · 热词纠错 · 可选 AI 润色 · CPU / GPU 双运行形态**

[![Version](https://img.shields.io/badge/version-1.6.0-blue.svg)](https://github.com/Xingxun7777/Aria/releases/latest)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#系统要求)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

[下载](#下载与选择) · [快速开始](#快速开始) · [默认状态](#当前默认状态) · [架构](#当前架构) · [OCR 模块](docs/OCR.md) · [隐私](docs/PRIVACY_DATA.md) · [参与开发](CONTRIBUTING.md)

</div>

---

## 项目目的

Aria 是一个面向 Windows 桌面输入场景的本地语音输入项目。它的目标不是把语音识别单独做成演示，而是把“按下热键—说话—纠错—输入—恢复现场”做成可长期使用的完整链路：

- 识别默认在本机完成，不配置账号也能使用；
- CPU 机器有开箱即用的轻量运行时，NVIDIA GPU 可按需启用高质量运行时；
- 屏幕 OCR 通过窗口标题、UI Automation 和本地截图识别提供专名证据，同时限制脏页面文字对 ASR 的干扰；
- 热词、屏幕证据和近期上下文用于纠正专名与同音词，但不能改变用户原意；
- AI 润色、云端识别救援均为可选能力，默认关闭；
- 语音输入借用剪贴板时，必须恢复原来的文字、图片、文件或空状态；
- 配置、历史和模型属于用户数据，更新程序不能覆盖它们。

当前稳定线是 **1.6**。它延续三运行时架构，并重点收口语音修改、AI 配置、GPU 安装和日常输入体验。

## 当前默认状态

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

## 当前架构

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

## 屏幕 OCR 上下文模块

OCR 在 Aria 中不是附属截图功能，而是与 ASR、热词、润色并列的上下文子系统：

- **三层取证**：窗口标题立即可用；原生应用优先读取 UI Automation 文档文本；浏览器、终端和自绘界面由窗口截图 OCR 补齐。
- **本地多级回退**：`PP-OCRv5 DirectML → PP-OCRv5 CPU → RapidOCR v4 CPU → Windows OCR → 仅标题`。
- **崩溃隔离**：DirectML 跑在独立 OCR worker；驱动或 ONNX Runtime native 崩溃只会结束 worker，主程序自动切 CPU。
- **延迟受控**：OCR 后台运行并按窗口缓存；fast 模式永不等待，quality 模式只在确有屏幕证据需求时做预测式有界等待。
- **证据而非输入**：OCR 原文不会直接插入输出。屏幕采集、屏幕增强、自动热词学习和完整文字诊断日志都需要显式开启。

完整数据流、默认值、缓存策略、后端诊断和隐私边界见 [屏幕 OCR 模块说明](docs/OCR.md)。

## 下载与选择

公开 Release 固定提供两个文件：

| 文件 | 用途 |
|---|---|
| `Aria-v1.6.0-Windows.zip` | 唯一的新安装包；CPU 解压即用，包内含 GPU 一键安装器 |
| `Aria-source-1.6.0.zip` | 应用内自动更新载荷；普通新安装不需要下载 |

[前往最新 Release](https://github.com/Xingxun7777/Aria/releases/latest)

**怎么选：**

- 没有 NVIDIA 显卡：解压后直接双击 `Aria.exe`，默认使用 CPU 轻量引擎。
- 有 NVIDIA 显卡：先正常启动 Aria，再从悬浮窗右键菜单点击“GPU 加速”。未安装时会提示下载约 3.1 GB 的固定 GPU 资产并持续显示进度；逐项校验和显卡实机验证全部通过后，Aria 会自动切换到 GPU，无需重启。
- GPU 安装失败不会把 CPU 配置改坏；按窗口里的明确错误修复驱动、网络或磁盘空间后重跑即可，下载支持断点续传。
- 如果应用内入口无法使用，也可以在 Aria 关闭后双击包根的 `Install_GPU.cmd` 重新安装。
- 维护者仍会构建约 4.8 GB 的完整 GPU 目录做发行验收，但其单个归档超过 GitHub Release 的单资产限制，不作为公开附件。
- 旧版只用于历史追溯，不再作为新安装入口。

## 快速开始

1. 下载 `Aria-v1.6.0-Windows.zip`，完整解压到普通可写目录；不要在压缩包预览窗口内直接运行。
2. 双击 `Aria.exe`，默认先以 CPU 模式启动。
3. NVIDIA GPU 用户可在悬浮窗右键菜单点击“GPU 加速”，按提示完成安装并自动切换。
4. 按反引号键 `` ` `` 开始说话，再按一次结束。

不配置 API Key 也能完成本地识别、热词纠错、语音指令和文字输入。

### 可选：开启 AI 润色

1. 右键托盘图标 → 设置 → API。
2. 使用 DeepSeek 推荐配置或填写兼容接口。
3. 粘贴自己的 API Key，测试成功后保存。
4. 按需开启“屏幕感知增强”。

开启后，识别文本以及必要的屏幕上下文会发送给所配置的 API。具体边界见 [数据与隐私说明](docs/PRIVACY_DATA.md)。

## 主要能力

- **本地语音识别**：标准版无需显卡；GPU 形态使用本机 llama.cpp 服务。
- **声学防线**：VAD、能量、峰值与置信度联合判断，减少低音量吞字和安静环境幻觉。
- **识别自愈**：超时、异常或空结果会触发引擎重建和救援策略。
- **热词与同音纠错**：支持权重、替换规则、拼音近似和屏幕专名学习。
- **屏幕 OCR 上下文**：标题、UI Automation、RapidOCR/Windows OCR 多级采集；独立 DML worker、CPU 回退、短期缓存和受控上下文路由。
- **三档润色**：逐字保真 / 顺畅口语 / 结构化文档。
- **语音指令**：翻译、总结、回复、截图、提醒、打开程序或路径。
- **剪贴板保护**：恢复文字、图片、文件列表和空剪贴板；退出前冲刷等待中的恢复任务。
- **自动更新**：校验更新载荷后只替换程序文件，保留配置、历史和模型。

## 运行进程说明

这部分是运行时身份说明，不是产品功能宣传：

| 进程 | 来源与职责 |
|---|---|
| `Aria.exe` | 启动器；负责定位便携运行时并启动应用 |
| `AriaRuntime.exe` | Aria 使用的嵌入式 CPython 运行时；写入 Aria Project 的文件说明、版本和图标，避免任务管理器显示成匿名 `pythonw.exe` |
| `llama-server.exe` | llama.cpp 上游组件；仅在 GPU 引擎启用时运行，保留上游文件名和身份，不伪装成 Aria |

GPU 服务只绑定 `127.0.0.1`。Aria 只管理自己启动且资产匹配的子进程；发现端口被其他程序占用时会停止切换，不会结束无关进程。

## 数据与隐私

- 麦克风音频默认只进入本地 ASR，处理完成后不保存原始 WAV。
- OCR 窗口截图默认只在内存中处理，不保存图片；默认日志只写后端、耗时、长度和哈希，不写完整页面文字。
- 热词、替换规则、历史记录、提醒和日志只存于当前安装目录。
- API Key 使用 Windows DPAPI 加密；发布包不携带维护者或构建机器配置。
- AI 润色、云端救援、更新检查和 GPU 资产下载是独立联网能力，说明和关闭方式见 [数据与隐私说明](docs/PRIVACY_DATA.md)。
- `llama-server` 的 HTTP 端口只监听本机回环地址，不是公网服务。

安全问题请按 [安全策略](SECURITY.md) 私下报告。

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
├── CONTRIBUTING.md           # 贡献约定
└── SECURITY.md               # 安全报告与支持边界
```

发布构建、维护者测试、一次性诊断和本机配置不进入公开快照。公开仓的每个版本仍可直接检查完整运行源码，并可针对当前快照提交 Issue 或 Pull Request。

## 配置速查

| 字段 | 默认值 |
|---|---|
| `general.hotkey` | 反引号键 `` ` `` |
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
| `output.typewriter_mode` | `false` |

完整字段见 [配置参考](docs/CONFIGURATION.md)。

## 系统要求

| 项目 | 标准版 | GPU 形态 |
|---|---|---|
| 操作系统 | Windows 10/11 64 位 | Windows 10/11 64 位 |
| CPU | x64，建议 4 核以上 | x64 |
| 内存 | 8 GB，建议 16 GB | 建议 16 GB |
| 显卡 | 不需要 | NVIDIA CUDA 显卡，建议至少 6 GB 可用显存 |
| Python | 便携包不需要；源码开发使用 3.12 | 同左 |

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

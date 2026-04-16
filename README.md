<div align="center">

# Aria — Local AI Voice Typing for Windows

**Windows 本地 AI 语音输入法 | Offline Speech-to-Text | 离线语音转文字**

[![Version](https://img.shields.io/badge/version-1.0.3.18-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#系统要求)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

[下载](#下载) · [快速开始](#快速开始) · [功能介绍](#功能介绍) · [配置说明](#配置) · [常见问题](#常见问题)

</div>

---

Aria 是一个 Windows 本地语音输入工具。基于 Qwen3-ASR，所有语音处理在本地完成。支持热词纠错、屏幕上下文感知、语音指令操作选中文字等功能。

## 特点

- **屏幕感知上下文** — 说话时自动 OCR 读取当前屏幕内容，提取关键词注入 ASR，提高专业术语和人名的识别准确率
- **热词纠错** — 在热词表中添加你常用的专有名词、人名、术语，ASR 识别时自动偏向这些词，同音字错误通过拼音匹配自动纠正，可选接 API 润色进一步修正
- **语音操作选区** — 选中文字后语音触发翻译、润色、总结、扩写、改写、AI 回复等操作，结果弹窗展示或直接替换
- **语音提醒和记录** — 语音设置定时提醒（如"提醒我五分钟后喝水"），语音记录想法和灵感
- **持续监听模式** — 按一次快捷键开启，Aria 持续监听并自动识别输入，再按一次关闭，无需按住说话
- **便携免安装** — 解压即用，支持 GPU 加速，无 GPU 自动回退 CPU

## 下载

| 版本 | 大小 | 说明 | 链接 |
|------|------|------|------|
| **精简版 (Lite)** | ~2 GB | 首次启动自动下载 ASR 模型 | [GitHub Releases](../../releases) |
| **完整版 (Full)** | ~6.4 GB | 内置 ASR 模型，解压即用 | 网盘下载（见 Releases 页面） |

## 快速开始

1. 下载并解压
2. 双击 **`Aria.cmd`**
3. 按 **`` ` ``**（反引号键）开启语音输入
4. 对着麦克风说话，文字自动输入到当前光标位置
5. 再按一次 **`` ` ``** 关闭

### 从源码运行

```bash
git clone https://github.com/Xingxun7777/Aria.git
cd Aria
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config\hotwords.template.json config\hotwords.json
Aria_debug.bat
```

## 功能介绍

### 语音识别

- **ASR 引擎** — 默认 Qwen3-ASR（支持 52 种语言），备用 FunASR（中文）
- **屏幕上下文** — 说话时自动 OCR 当前窗口，提取关键词注入 ASR 上下文
- **近期上下文** — 最近 10 条识别结果作为跨语段上下文，提升连续对话准确率
- **热词纠错** — 正则替换 + 拼音模糊匹配 + 可选 AI 润色，每个热词可设权重
- **实时字幕** — 说话时毛玻璃面板显示中间识别结果
- **噪声过滤** — 自动过滤环境噪声产生的无意义文字

### 智能润色（可选，需配置 API）

- 通过 API 调用大语言模型修正错别字、标点和同音字
- 自动去除口语填充词
- 长段口述自动整理为结构化文本
- 支持个性化润色规则
- 根据当前应用类型自动调整润色风格
- 高级用户可配置 GGUF 模型实现离线润色

### 选区指令

选中文字后，说唤醒词 + 命令：

| 命令 | 效果 |
|------|------|
| 润色 / 优化 | 提升文字表达质量 |
| 翻译成英文 / 中文 / 日文 | 替换为对应语言翻译 |
| 扩写 / 展开 | 增加细节和深度 |
| 缩写 / 精简 | 保留核心信息 |
| 重写 / 改写 | 换种表达方式 |
| 什么意思 | 弹窗翻译（不替换原文）|
| 总结一下 | 弹窗显示摘要 |
| 帮我回复 | AI 生成回复建议 |
| 问 AI | 打开 AI 对话窗口 |
| 帮我打开 | 打开选中的路径或 URL |
| 记一下 + 内容 | 记录想法 |
| 提醒我 + 时间 + 内容 | 设置定时提醒 |

弹窗支持拖拽移动、固定、一键复制、一键插入。

### 唤醒词控制

| 命令 | 效果 |
|------|------|
| [唤醒词] 开启/关闭自动发送 | 控制识别后是否自动按回车 |
| [唤醒词] 休眠 / 醒来 | 暂停/恢复语音监听 |
| [唤醒词] 深度休眠 | 卸载 ASR 模型，释放显存 |

> 唤醒词可在设置中自定义。

### 历史记录

- 所有语音输入、翻译、润色结果自动存储
- 历史浏览器：按日期、类型、关键词查找
- 支持导出为 Markdown
- 自动清理过期记录（默认 90 天）

### 其他

- **备用 API 轮询** — 配置主/备 API，连续慢响应自动切换
- **Typewriter 模式** — 逐字输入，兼容游戏和管理员权限应用
- **配置热重载** — 修改配置后自动生效，无需重启
- **开机自启** — 注册表方式，支持便携模式
- **HF 镜像** — 中国用户默认使用 hf-mirror.com 下载模型

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| 操作系统 | Windows 10 64 位 | Windows 11 |
| Python | 3.12 | 3.12 |
| 内存 | 8 GB | 16 GB |
| 显卡 | 无（CPU 可用）| NVIDIA GTX 16xx+（4 GB 显存）|

内置 CUDA 12.8 的 PyTorch，无兼容 GPU 时自动回退 CPU 模式。

<details>
<summary>GPU 兼容性详情</summary>

| GPU 系列 | 架构 | 支持 |
|----------|------|------|
| RTX 40xx / 50xx | Ada / Blackwell | 完全支持 |
| RTX 30xx | Ampere | 完全支持 |
| RTX 20xx / GTX 16xx | Turing | 支持 |
| GTX 10xx | Pascal | 自动 CPU 回退 |
| 无 NVIDIA GPU | — | CPU 模式 |

</details>

## 配置

配置文件：`config/hotwords.json`（首次运行自动从模板创建，保存后 2 秒自动热重载）。

**常用配置项：**

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `asr_engine` | 识别引擎 | `qwen3` |
| `general.hotkey` | 全局热键 | `` ` `` |
| `polish_mode` | 润色模式 | `quality` |
| `filter_filler_words` | 口语过滤 | `true` |
| `auto_structure` | 自动结构化 | `false` |
| `vad.screen_ocr` | 屏幕感知 | `true` |
| `vad.threshold` | VAD 灵敏度 (0-1) | `0.2` |
| `output.typewriter_mode` | 逐字输入模式 | `false` |

> 完整配置说明：[docs/CONFIGURATION.md](docs/CONFIGURATION.md)

## 常见问题

<details>
<summary><b>识别准确率不高？</b></summary>

1. 添加专有名词到 `hotwords` 列表，设置合适权重
2. 配置 `replacements` 替换规则处理固定误识别
3. 填写 `domain_context` 描述使用场景（如 "编程技术讨论"）
4. 启用 API 润色（`polish_mode: "quality"` + 配置 API key）
5. 确保屏幕感知已开启（`vad.screen_ocr: true`）

</details>

<details>
<summary><b>GPU 加速不工作？</b></summary>

1. 确认 `nvidia-smi` 正常运行
2. GTX 16xx 及以上自动启用，10xx 及更旧自动回退 CPU
3. 更新显卡驱动到最新版本

</details>

<details>
<summary><b>无法在某些应用输入文字？</b></summary>

1. 启用 Typewriter 模式：`output.typewriter_mode: true`
2. 如目标程序以管理员运行，以管理员启动 Aria

</details>

<details>
<summary><b>首次启动很慢？</b></summary>

- **精简版**：首次使用需自动下载 ASR 模型（约 1.2-3.4 GB），下载后离线可用
- **完整版**：已内置模型，解压即用
- 也可在设置中切换到 FunASR 备用引擎

</details>

<details>
<summary><b>环境噪声导致误输入？</b></summary>

1. 适当提高 VAD 阈值（`vad.threshold`，默认 0.2，嘈杂环境可调至 0.4）
2. 适当提高能量阈值（`vad.energy_threshold`，默认 0.003）

</details>

## 项目结构

<details>
<summary>展开查看</summary>

```
Aria/
├── launcher.py              # 入口：单例检查 + 启动画面 + 模型预加载
├── app.py                   # 主应用：状态机 + ASR 编排
├── core/                    # 核心模块
│   ├── asr/                 # 语音识别引擎
│   ├── audio/               # 音频捕获 + VAD
│   ├── context/             # 屏幕上下文感知 + OCR
│   ├── history/             # 历史记录存储
│   ├── hotword/             # 热词纠错 + AI 润色
│   ├── selection/           # 选区指令
│   ├── wakeword/            # 唤醒词检测 + 命令执行
│   └── command/             # 语音键盘命令
├── ui/qt/                   # PySide6 界面
├── system/                  # 系统集成（热键、文本输出、权限检测）
├── config/                  # 配置文件
└── build_portable/          # 便携版打包
```

</details>

## 许可证

[Apache License 2.0](LICENSE)

## 致谢

| 项目 | 许可证 | 说明 |
|------|--------|------|
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) | Apache 2.0 | 通义千问语音识别 |
| [FunASR](https://github.com/alibaba-damo-academy/FunASR) | MIT | 阿里达摩院语音识别 |
| [Silero-VAD](https://github.com/snakers4/silero-vad) | MIT | 语音活动检测 |
| [PySide6](https://www.qt.io/) | LGPL v3 | Qt6 Python 绑定 |
| [PyTorch](https://pytorch.org/) | BSD-3-Clause | 深度学习框架 |
| [pypinyin](https://github.com/mozillazg/python-pinyin) | MIT | 汉字拼音转换 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache 2.0 | PaddleOCR ONNX 推理 |

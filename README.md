<div align="center">

# Aria — Local AI Voice Typing for Windows

**Windows 本地 AI 语音输入法 | Offline Speech-to-Text | 离线语音转文字**

[![Version](https://img.shields.io/badge/version-1.0.3.2-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#系统要求)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![CUDA](https://img.shields.io/badge/CUDA-GPU%20加速-76B900.svg)](#系统要求)

离线语音识别 · GPU 加速 · 智能纠错 · 屏幕感知 · 语音指令 · 翻译润色

[下载](#下载) · [快速开始](#快速开始) · [功能介绍](#功能介绍) · [配置说明](#配置) · [常见问题](#常见问题)

</div>

---

## 为什么选择 Aria？

市面上的语音输入工具要么需要联网、要么把音频传到云端、要么中文支持拉胯。Aria 不一样：

- **100% 本地私密** — 所有语音识别在你自己电脑上运行，音频永远不会上传到任何服务器
- **双引擎架构** — Qwen3-ASR（52 种语言）+ FunASR（中文极速），GPU 自动加速
- **四层智能纠错** — ASR 引导 → 正则替换 → 拼音模糊匹配 → AI 润色，专有名词识别准确率大幅提升
- **屏幕感知** — 自动读取当前屏幕内容，提取关键词注入 ASR 上下文，领域术语和人名更准
- **语音指令** — 翻译、总结、润色、改写选中文字，全部用语音完成
- **万能输入** — 能往任何 Windows 应用输入文字，包括游戏和管理员权限窗口

> **隐私说明**：所有语音数据在本地处理。可选的 AI 润色功能需要 API（可配置），但原始音频绝不外传。

## 下载

| 版本 | 大小 | 说明 | 链接 |
|------|------|------|------|
| **精简版 (Lite)** | ~2 GB | 首次启动自动下载 ASR 模型（约 1.2-3.4 GB） | [GitHub Releases](../../releases) |
| **完整版 (Full)** | ~6.4 GB | 内置 Qwen3-ASR 0.6B + 1.7B 双模型，解压即用 | 网盘下载（见 Releases 页面） |

两个版本都是便携版 — 解压即用，无需安装。

## 快速开始

1. 下载并解压 **完整版** 或 **精简版**
2. 双击 **`Aria.cmd`**（或 `Aria.vbs` 静默启动）
3. 按 **`` ` ``**（反引号键）开启语音输入 — Aria 开始持续监听
4. 对着麦克风说话 — 文字自动输入到当前光标位置
5. 再按一次 **`` ` ``** 关闭

就这么简单。不用注册账号，不用联网（AI 润色除外）。

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

- **双引擎** — Qwen3-ASR（默认，52 种语言）/ FunASR（中文，极速），GPU 自动加速
- **四层热词纠错** — ASR 引导 → 正则替换 → 拼音模糊 → AI 润色，每个热词可设 0-1.0 权重
- **切换模式** — 按快捷键开启，Aria 持续监听并自动识别，再按一次关闭
- **实时流式字幕** — 说话时毛玻璃面板显示中间识别结果
- **噪声过滤** — 自动丢弃环境噪声产生的无意义文字（嗯、啊、呃等），不影响正常短回复
- **屏幕感知** — 自动读取当前窗口内容，提取关键词注入 ASR 上下文，提高专业术语和人名识别准确率
- **近期上下文** — 最近 10 条识别结果作为跨语段上下文，提升连续对话准确率

### 智能润色

- **高质量模式** — 通过 API 调用大语言模型（DeepSeek / Gemini 等）修正错别字、标点和同音字
- **口语过滤** — 自动去除"就是"、"然后的话"、"嗯"等口语填充词
- **自动结构化** — 长段口述自动整理为带换行、编号的结构化文本
- **个性化规则** — 自然语言描述你的润色偏好（如"英文专有名词保留原始大小写"）
- **场景识别** — 聊天场景保留口语感，文档场景偏书面化
- **本地润色** — 高级用户可配置 GGUF 模型实现完全离线润色

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
| 帮我回复 | AI 生成回复建议，支持指定风格 |
| 问 AI | 打开 AI 对话窗口 |
| 帮我打开 | 选中路径/URL，语音打开文件或网页 |
| 记一下 + 内容 | 记录想法 |
| 提醒我 + 时间 + 内容 | 设置定时提醒 |

### 弹窗交互

翻译、总结、回复弹窗支持：
- 拖拽移动
- 固定（Pin）防止自动消失
- 一键复制
- 一键插入（自动切回原窗口粘贴）

### 唤醒词控制

| 命令 | 效果 |
|------|------|
| [唤醒词] 开启/关闭自动发送 | 控制识别后是否自动按回车 |
| [唤醒词] 休眠 / 醒来 | 暂停/恢复语音监听 |
| [唤醒词] 深度休眠 | 完全卸载 ASR 模型，释放全部显存 |

> 唤醒词可在设置中自定义。

### 历史记录

- 所有语音输入、翻译、润色结果自动存储
- 历史浏览器：按日期翻、按类型筛、搜索关键词
- 支持导出为 Markdown
- 自动清理过期记录（默认 90 天）

### 其他特性

- **备用 API 智能轮询** — 配置主/备 API，连续慢响应自动切换
- **Typewriter 模式** — 逐字输入，兼容游戏和管理员权限应用
- **回复风格** — 在设置中定义 AI 回复的风格偏好
- **配置热重载** — 修改配置后 2 秒自动生效，无需重启
- **开机自启** — 注册表方式，支持便携模式
- **HF 镜像加速** — 中国用户默认使用 hf-mirror.com 下载模型

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| 操作系统 | Windows 10 64 位 | Windows 11 |
| Python | 3.12 | 3.12 |
| 内存 | 8 GB | 16 GB |
| 显卡 | 无（CPU 可用）| NVIDIA GTX 16xx+（4 GB 显存）|

内置 CUDA 12.8 的 PyTorch。无 GPU 或显卡不兼容时**自动回退 CPU 模式**，无需任何配置。

<details>
<summary>GPU 兼容性详情</summary>

| GPU 系列 | 架构 | 加速支持 |
|----------|------|----------|
| RTX 40xx / 50xx | Ada / Blackwell | 完全支持 |
| RTX 30xx | Ampere | 完全支持 |
| RTX 20xx / GTX 16xx | Turing | 支持 |
| GTX 10xx | Pascal | 自动 CPU 回退 |
| 无 NVIDIA GPU | — | CPU 模式 |

CPU 模式下所有功能正常，识别速度约为 GPU 的 2-5 倍时间。

</details>

## 识别引擎

| 引擎 | 速度 | 语言 | 特点 |
|------|------|------|------|
| **Qwen3-ASR**（默认）| 中等 | 52 种 | 上下文增强 + 防幻觉 + 屏幕 OCR 辅助，根据显存自动选模型大小 |
| **FunASR** | 最快 | 中文 | 首次使用自动下载模型（~700MB），之后离线可用 |

<details>
<summary>四层热词纠错详解</summary>

| 层级 | 说明 |
|------|------|
| L1 ASR 引导 | initial_prompt 偏向领域词汇 |
| L2 正则替换 | 规则映射（如 scale → skill）|
| L2.5 拼音匹配 | 同音字纠正（如 "星循" → "星巡"）|
| L3 AI 润色 | API 调用大语言模型修正语法和同音字 |

每个热词可设置 0-1.0 权重，精细控制各层参与度。详见 [配置参考](docs/CONFIGURATION.md)。

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
| `reply_style` | 回复风格 | `""` |
| `vad.noise_filter` | 噪声过滤 | `true` |
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
5. 确保屏幕识别已开启（`vad.screen_ocr: true`）

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

- **精简版**：首次使用需自动下载 Qwen3-ASR 模型（约 1.2-3.4 GB），下载后离线可用
- **完整版**：已内置模型，解压即用
- 也可切换到 FunASR（首次下载约 700MB，之后离线可用）

</details>

<details>
<summary><b>环境噪声导致误输入？</b></summary>

1. 确保噪声过滤已开启（`vad.noise_filter: true`，默认开启）
2. 适当提高 VAD 阈值（`vad.threshold`，默认 0.2，嘈杂环境可调至 0.4）
3. 适当提高能量阈值（`vad.energy_threshold`，默认 0.003）

</details>

## 项目结构

<details>
<summary>展开查看</summary>

```
Aria/
├── launcher.py              # 入口：单例检查 + 启动画面 + 模型预加载
├── app.py                   # 主应用：状态机 + ASR 编排
├── core/                    # 核心模块
│   ├── asr/                 # 语音识别引擎（Qwen3-ASR / FunASR）
│   ├── audio/               # 音频捕获 + VAD（Silero）
│   ├── context/             # 屏幕上下文感知 + OCR
│   ├── history/             # 历史记录存储
│   ├── hotword/             # 四层热词纠错 + AI 润色
│   ├── selection/           # 选区指令（润色/翻译/扩写等）
│   ├── wakeword/            # 唤醒词检测 + 命令执行
│   └── command/             # 语音键盘命令
├── ui/qt/                   # PySide6 界面
│   ├── floating_ball.py     # 浮动球 + 流式字幕
│   ├── settings.py          # 设置面板
│   ├── translation_popup.py # 翻译/总结/回复弹窗
│   └── workers/             # 后台任务
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

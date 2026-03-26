<div align="center">

# Aria

**Windows 本地 AI 语音输入工具**

[![Version](https://img.shields.io/badge/version-1.0.2-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#系统要求)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

离线语音识别 · 智能润色 · 屏幕感知 · 语音指令 · 历史记录

[快速开始](#快速开始) · [功能介绍](#功能介绍) · [配置说明](#配置) · [常见问题](#常见问题)

</div>

---

> 所有语音数据在本地处理，不上传任何服务器。润色和翻译功能需配置 API。

## 功能介绍

### 语音识别

- **双引擎** — Qwen3-ASR（默认，多语言）/ FunASR（中文，极速），GPU 自动加速
- **四层热词纠错** — ASR 引导 → 正则替换 → 拼音模糊 → AI 润色，每个热词可设 0-1.0 权重
- **切换模式** — 按快捷键开启语音输入，Aria 持续监听并自动识别，再按一次关闭
- **实时流式字幕** — 说话时毛玻璃面板显示中间识别结果
- **噪声过滤** — 自动丢弃环境噪声产生的无意义文字（嗯、啊、呃等），不影响正常短回复
- **屏幕感知** — 自动读取当前窗口标题和页面内容，提取关键词注入 ASR 上下文，提高专业术语和人名识别准确率
- **近期上下文** — 保留最近 10 条识别结果作为跨语段上下文，提升连续对话的识别准确率

### 智能润色

- **高质量模式** — 通过 API 调用大语言模型（DeepSeek / Gemini 等）修正错别字、标点和同音字
- **口语过滤** — 自动去除"就是"、"然后的话"、"嗯"等口语填充词
- **自动结构化** — 长段口述自动整理为带换行、编号的结构化文本
- **个性化规则** — 自然语言描述你的润色偏好（如"英文专有名词保留原始大小写"）
- **屏幕感知** — 自动识别当前应用类型，聊天场景保留口语感，文档场景偏书面化
- **本地润色** — 高级用户可配置 GGUF 模型实现离线润色（需额外安装 `llama-cpp-python`）

### 选区指令

选中文字后，说唤醒词 + 命令：

| 命令 | 效果 |
|------|------|
| 润色 / 优化 | 提升文字表达质量 |
| 翻译成英文 / 中文 / 日文 | 替换为对应语言翻译 |
| 扩写 / 展开 | 增加细节和深度 |
| 缩写 / 精简 | 保留核心信息 |
| 重写 / 改写 | 不同表达方式 |
| 什么意思 | 弹窗翻译（不替换原文）|
| 总结一下 | 弹窗显示摘要 |
| 帮我回复 | AI 生成回复建议，支持指定风格（如"帮我回复，语气轻松一点"）|
| 问 AI | 打开 AI 对话窗口 |
| 帮我打开 | 选中路径/URL，语音打开文件或网页 |
| 记一下 + 内容 | 记录想法 |
| 提醒我 + 时间 + 内容 | 设置定时提醒（如"提醒我五分钟后喝水"）|

### 弹窗交互

翻译、总结、回复弹窗支持：
- 拖拽移动
- 固定（Pin）防止自动消失
- 一键复制
- 一键插入（自动切回原窗口粘贴）

### 语音键盘命令

说 `[唤醒词] + 命令` 模拟键盘操作：

**发送** (Enter) · **换行** · **删除** (Backspace) · **撤销** (Ctrl+Z) · **重做** (Ctrl+Y) · **复制** · **粘贴** · **全选** · **保存** · **剪切**

### 唤醒词控制

| 命令 | 效果 |
|------|------|
| [唤醒词] 开启/关闭自动发送 | 控制识别后是否自动按回车 |
| [唤醒词] 休眠 / 醒来 | 暂停/恢复语音监听 |
| [唤醒词] 记一下 + 内容 | 记录想法 |

> 唤醒词可在设置中自定义。内置可选：遥遥、瑶瑶、小朋友、小溪、助手、小白。

### 历史记录

- 所有语音输入、翻译、润色、回复结果自动存储
- 历史浏览器：按日期翻、按类型筛、搜索关键词
- 支持导出为 Markdown
- 自动清理过期记录（默认 90 天）

### 翻译输出

翻译结果支持两种输出方式（在弹出菜单或设置中切换）：
- **弹窗显示**（默认）— 悬浮弹窗展示翻译结果
- **复制到剪贴板** — 翻译后自动复制，托盘通知

### 其他

- **备用 API 智能轮询** — 配置主/备 API，连续 2 次慢响应（> 3 秒）自动切换
- **Typewriter 模式** — 逐字输入，兼容游戏和管理员权限应用
- **回复风格** — 在设置中定义 AI 回复的风格偏好（如"回复简短，像朋友聊天"）
- **配置热重载** — 修改配置后 2 秒自动生效，无需重启
- **开机自启** — 注册表方式，支持开发和便携模式
- **HF 镜像加速** — 中国用户默认使用 hf-mirror.com 下载模型

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| OS | Windows 10 64 位 | Windows 11 |
| Python | 3.12 | 3.12 |
| RAM | 8 GB | 16 GB |
| GPU | 无（CPU 可用）| NVIDIA GTX 16xx+（4 GB VRAM）|

内置 CUDA 12.8 的 PyTorch。无 GPU 或不兼容时**自动回退 CPU 模式**，无需任何配置。

<details>
<summary>GPU 兼容性详情</summary>

| GPU 系列 | 架构 | 加速 |
|----------|------|------|
| RTX 40xx / 50xx | Ada / Blackwell | 完全支持 |
| RTX 30xx | Ampere | 完全支持 |
| RTX 20xx / GTX 16xx | Turing | 支持 |
| GTX 10xx | Pascal | 自动 CPU 回退 |
| 无 NVIDIA GPU | — | CPU 模式 |

CPU 模式下所有功能正常，识别速度约为 GPU 的 2-5 倍时间。

</details>

## 快速开始

### 便携版（推荐）

1. 从 [Releases](../../releases) 下载 **Lite 版**，或从网盘链接下载 **Full 版**
2. 解压到任意目录
3. 双击 `Aria.exe`
4. 按 `` ` ``（反引号）键开启语音输入，Aria 开始持续监听
5. 对着麦克风说话，识别结果自动输入到当前光标位置
6. 再按一次 `` ` `` 键关闭语音输入

> - **Lite**：GitHub 发布，不含模型；首次运行自动下载 Qwen3-ASR（约 1.2-3.4 GB）
> - **Full**：网盘发布，内置 Qwen3-ASR 0.6B + 1.7B，解压即用

### 从源码运行

```bash
git clone https://github.com/Xingxun7777/Aria.git
cd Aria

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt

copy config\hotwords.template.json config\hotwords.json

# 带控制台启动（调试用）
Aria_debug.bat

# 无控制台启动（日常使用）
Aria.bat
```

## 使用指南

### 基础操作

| 操作 | 说明 |
|------|------|
| `` ` `` 键（默认） | 开启/关闭语音输入 |
| 左键浮动球 | 开启/关闭语音输入 |
| 右键浮动球 | 快捷菜单（润色模式切换、翻译模式等）|
| 中键浮动球 | 锁定位置（锁定后隐藏流式字幕）|
| 系统托盘 | 单击查看历史 · 双击打开热词设置 · 右键更多选项 |

### 工作流程

```
按快捷键开启 → Aria 持续监听麦克风
  ↓
说话 → VAD 检测语音 → 屏幕 OCR（后台）
  ↓
语音转文字（Qwen3-ASR + 热词/近期/OCR 上下文）
  ↓
噪声过滤 → 四层纠错 → AI 润色（可选）
  ↓
文字自动输入到当前光标位置
```

## 识别引擎

| 引擎 | 速度 | 语言 | 特点 |
|------|------|------|------|
| **Qwen3-ASR**（默认）| 中等 | 52 语言 | 上下文增强 + 三级防幻觉 + 屏幕 OCR 辅助，VRAM 自动选模型 |
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

## 构建便携版

```bash
build_portable\release-lite.bat
build_portable\release-full.bat
```

输出目录：

- `dist_portable/Aria_release_lite/`：GitHub 发布用 Lite 包
- `dist_portable/Aria_release_full/`：网盘/云盘发布用 Full 包

兼容入口：

```bash
build_portable\release.bat lite
build_portable\release.bat full
```

详见 [build_portable/RELEASE_GUIDE.md](build_portable/RELEASE_GUIDE.md)。

## 常见问题

<details>
<summary><b>识别准确率不高？</b></summary>

1. 添加专有名词到 `hotwords` 列表，设置合适权重
2. 配置 `replacements` 替换规则处理固定误识别
3. 填写 `domain_context` 描述使用场景（如 "编程技术讨论"）
4. 启用 API 润色（`polish_mode: "quality"` + 配置 API key）
5. 确保屏幕识别辅助已开启（`vad.screen_ocr: true`）

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

- **Lite 版**：Qwen3-ASR 首次使用需自动下载模型（约 1.2-3.4 GB），下载后离线可用
- **Full 版**：已内置模型，通常不会触发首次下载
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
├── launcher.py              # 入口：单例检查 + splash + 模型预加载
├── app.py                   # 主应用：状态机 + ASR 编排
├── core/                    # 核心模块
│   ├── asr/                 # 语音识别引擎（Qwen3-ASR / FunASR）
│   ├── audio/               # 音频捕获 + VAD（Silero）
│   ├── context/             # 屏幕上下文感知 + OCR
│   ├── history/             # 历史记录存储 + 迁移
│   ├── hotword/             # 四层热词纠错 + AI 润色
│   ├── selection/           # 选区指令（润色/翻译/扩写等）
│   ├── wakeword/            # 唤醒词检测 + 命令执行
│   └── command/             # 语音键盘命令
├── ui/qt/                   # PySide6 界面
│   ├── main.py              # 主窗口 + 托盘 + 信号连接
│   ├── floating_ball.py     # 浮动球 + 流式字幕
│   ├── popup_menu.py        # 右键菜单
│   ├── settings.py          # 设置面板
│   ├── history_browser.py   # 历史记录浏览器
│   ├── translation_popup.py # 翻译/总结/回复弹窗
│   └── workers/             # 后台任务（翻译/总结/回复）
├── system/                  # 系统集成
│   ├── hotkey.py            # 全局热键
│   ├── output.py            # 文本输出 + 窗口信息获取
│   └── admin.py             # 权限检测
├── config/                  # 配置文件
│   ├── hotwords.template.json  # 配置模板
│   ├── wakeword.json        # 唤醒词定义
│   └── commands.json        # 键盘命令定义
└── build_portable/          # 便携版打包
    ├── build.py             # 打包脚本
    ├── release-lite.bat     # Lite 打包
    ├── release-full.bat     # Full 打包
    └── RELEASE_GUIDE.md     # 发布指南
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
| [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) | Apache 2.0 | Windows UI Automation |
| [winocr](https://github.com/GitHub30/winocr) | MIT | Windows OCR 绑定 |

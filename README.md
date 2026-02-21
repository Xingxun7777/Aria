<div align="center">

# Aria

**Windows 本地 AI 语音输入工具**

[![Version](https://img.shields.io/badge/version-1.1.2-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#系统要求)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

离线语音识别 · 四层智能纠错 · 按住说话 · 语音指令 · 选区命令

[快速开始](#快速开始) · [功能介绍](#功能介绍) · [配置说明](#配置) · [常见问题](#常见问题)

</div>

---

> 所有语音数据在本地处理，不上传任何服务器。

## 功能介绍

- **四引擎语音识别** — Qwen3-ASR（默认）/ FunASR / Whisper / FireRedASR，GPU 自动加速
- **四层热词纠错** — ASR 引导 → 正则替换 → 拼音模糊 → AI 润色，每个热词可设权重
- **双输入模式** — 切换模式（按一下开始/再按停止）和按住说话（PTT，右 Ctrl）
- **实时流式字幕** — 录音中毛玻璃面板显示中间识别结果
- **选区指令** — 选中文字后语音执行：润色 · 翻译 · 扩写 · 缩写 · 重写 · 问 AI
- **语音键盘命令** — 语音模拟发送、删除、撤销、复制等键盘操作
- **翻译弹窗 & AI 对话** — 选中文字说"什么意思"翻译，说"问 AI"对话
- **唤醒词控制** — 语音控制休眠/唤醒/自动发送/记笔记
- **Typewriter 模式** — 逐字输入，兼容游戏和管理员权限应用

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

1. 从 [Releases](../../releases) 下载最新版压缩包
2. 解压到任意目录
3. 双击 `Aria.exe`
4. 按 `` ` `` 键开始说话，再按一次结束 → 文字自动输入到当前窗口

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
| `` ` `` 键 | 开始/停止录音（切换模式）|
| 按住右 Ctrl | 持续录音，松开自动识别（PTT 模式）|
| 左键浮动球 | 开始/停止录音 |
| 右键浮动球 | 快捷菜单 |
| 中键浮动球 | 锁定位置（锁定后隐藏流式字幕）|
| 系统托盘 | 单击查看历史 · 双击打开热词设置 · 右键更多选项 |

### 输入模式

在右键菜单中切换：

| 模式 | 操作 | 适用场景 |
|------|------|----------|
| **切换模式**（默认）| 按热键开始，再按停止 | 长段落、边想边说 |
| **按住说话 (PTT)** | 按住右 Ctrl，松开识别 | 短句快速输入 |

PTT 保护机制：< 0.3 秒误触过滤、60 秒最大时长、模式切换安全停止。

### 选区指令

选中文字 → 按热键 → 说命令：

| 命令 | 效果 |
|------|------|
| 润色 / 优化 | 提升文字表达质量 |
| 翻译成英文 / 中文 / 日文 | 替换为对应语言翻译 |
| 扩写 / 展开 | 增加细节和深度 |
| 缩写 / 精简 | 保留核心信息 |
| 重写 / 改写 | 不同表达方式 |
| 什么意思 | 弹窗翻译（不替换原文）|
| 问 AI | 打开 AI 对话窗口 |

### 语音键盘命令

说 `[唤醒词] + 命令` 模拟键盘操作：

**发送** (Enter) · **换行** · **删除** (Backspace) · **撤销** (Ctrl+Z) · **重做** (Ctrl+Y) · **复制** · **粘贴** · **全选** · **保存** · **剪切**

> 唤醒词默认 "遥遥"，可在设置中修改。命令可在 `config/commands.json` 自定义。

### 唤醒词控制

| 命令 | 效果 |
|------|------|
| [唤醒词] 开启/关闭自动发送 | 控制识别后是否自动输入 |
| [唤醒词] 休眠 / 醒来 | 暂停/恢复语音监听 |
| [唤醒词] 记一下 + 内容 | 记录想法到笔记 |

## 识别引擎

| 引擎 | 速度 | 语言 | 特点 |
|------|------|------|------|
| **Qwen3-ASR**（默认）| 中等 | 多语言 | 上下文热词增强 + 三级防幻觉系统 |
| **FunASR** | 最快 | 中文 | 离线即用，无需下载模型 |
| **Whisper** | 中等 | 多语言 | large-v3-turbo，精度最高 |
| **FireRedASR** | 中等 | 中文 | 需外部仓库 |

<details>
<summary>四层热词纠错详解</summary>

| 层级 | 说明 |
|------|------|
| L1 ASR 引导 | initial_prompt 偏向领域词汇 |
| L2 正则替换 | 规则映射（如 scale → skill）|
| L2.5 拼音匹配 | 同音字纠正（如 "星循" → "星巡"）|
| L3 AI 润色 | API 或本地 LLM 修正语法和同音字 |

每个热词可设置 0-1.0 权重，精细控制各层参与度。详见 [配置参考](docs/CONFIGURATION.md)。

</details>

## 配置

配置文件：`config/hotwords.json`（首次运行自动从模板创建，保存后 2 秒自动热重载）。

**常用配置项：**

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `asr_engine` | 识别引擎 | `qwen3` |
| `general.hotkey` | 全局热键 | `` ` `` |
| `general.input_mode` | 输入模式 (`toggle` / `ptt`) | `toggle` |
| `vad.threshold` | VAD 灵敏度 (0-1) | `0.3` |
| `polish.enabled` | 启用 API 润色 | `false` |
| `output.typewriter_mode` | 逐字输入模式 | `false` |

> 完整配置说明：[docs/CONFIGURATION.md](docs/CONFIGURATION.md)

## 构建便携版

```bash
build_portable\release.bat
```

输出到 `dist_portable/Aria/`（含完整 Python 运行环境和 CUDA 依赖，约 9 GB）。

打包流程：下载嵌入式 Python → 复制源码 → 清理敏感数据 → 复制 site-packages → 编译 EXE 启动器 → 验证。

详见 [build_portable/RELEASE_GUIDE.md](build_portable/RELEASE_GUIDE.md)。

## 常见问题

<details>
<summary><b>识别准确率不高？</b></summary>

1. 添加专有名词到 `hotwords` 列表，设置合适权重
2. 配置 `replacements` 替换规则处理固定误识别
3. 填写 `domain_context` 描述使用场景（如 "编程技术讨论"）
4. 启用 API 润色（`polish.enabled: true` + 配置 API key）

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
<summary><b>PTT 模式没反应？</b></summary>

1. 确认已在快捷菜单切换到 "按住说话" 模式
2. 默认 PTT 按键是右 Ctrl
3. 按住时间需超过 0.3 秒（防误触保护）

</details>

<details>
<summary><b>首次启动很慢？</b></summary>

Qwen3-ASR 首次使用需下载模型（1.2-3.4 GB），下载后离线可用。切换到 FunASR 可免下载立即使用。

</details>

## 项目结构

<details>
<summary>展开查看</summary>

```
Aria/
├── launcher.py              # 入口：单例检查 + splash + 模型预加载
├── app.py                   # 主应用：状态机 + ASR 编排
├── core/                    # 核心模块
│   ├── asr/                 # 语音识别引擎
│   ├── audio/               # 音频捕获 + VAD
│   ├── hotword/             # 四层热词纠错 + AI 润色
│   ├── selection/           # 选区指令
│   └── wakeword/            # 唤醒词检测 + 命令执行
├── ui/qt/                   # PySide6 界面
│   ├── main.py              # 主窗口 + 托盘 + 信号连接
│   ├── floating_ball.py     # 浮动球 + 流式字幕
│   ├── popup_menu.py        # 右键菜单
│   └── settings.py          # 设置面板
├── system/                  # 系统集成
│   ├── hotkey.py            # 全局热键 + PTT Handler
│   ├── output.py            # 文本输出
│   └── admin.py             # 权限检测
├── config/                  # 配置文件
│   ├── hotwords.template.json  # 配置模板
│   ├── wakeword.json        # 唤醒词定义
│   └── commands.json        # 键盘命令定义
└── build_portable/          # 便携版打包
    ├── build.py             # 打包脚本
    ├── release.bat          # 一键打包
    └── RELEASE_GUIDE.md     # 发布指南
```

</details>

## 许可证

[Apache License 2.0](LICENSE)

## 致谢

| 项目 | 许可证 | 说明 |
|------|--------|------|
| [FunASR](https://github.com/alibaba-damo-academy/FunASR) | MIT | 阿里达摩院语音识别 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Whisper 优化实现 |
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) | Apache 2.0 | 通义千问语音识别 |
| [Silero-VAD](https://github.com/snakers4/silero-vad) | MIT | 语音活动检测 |
| [PySide6](https://www.qt.io/) | LGPL v3 | Qt6 Python 绑定 |
| [PyTorch](https://pytorch.org/) | BSD-3-Clause | 深度学习框架 |
| [pypinyin](https://github.com/mozillazg/python-pinyin) | MIT | 汉字拼音转换 |
| [pynput](https://github.com/moses-palmer/pynput) | LGPL v3 | 键盘监听 |

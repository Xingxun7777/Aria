# Aria v1.1.1 - AI 智能语音输入工具

> Windows 本地 AI 语音听写 + 智能指令工具

Aria 是一款运行在 Windows 上的本地语音输入工具，支持多引擎离线语音识别、四层热词纠错、选区指令和语音命令控制。所有语音数据在本地处理，保护隐私。

## 核心功能

### 语音识别
- **多引擎支持** - FunASR Paraformer (默认/最快)、Whisper (高精度)、FireRedASR、Qwen3-ASR
- **流式识别** - 录音过程中实时显示中间结果
- **GPU 加速** - NVIDIA GPU 自动加速，无 GPU 自动回退 CPU
- **智能防幻觉** - Pre-ASR 声学门控 + 三级能量感知泄漏检测 + 无上下文重试机制

### 四层热词纠错
- **Layer 1** - ASR initial_prompt 引导领域词汇
- **Layer 2** - 正则规则替换 (中英文映射)
- **Layer 2.5** - 拼音模糊匹配 (纠正同音字)
- **Layer 3** - AI 润色 (API 或本地 LLM)

### 智能交互
- **选区指令** - 选中文字后语音执行：润色、翻译、扩写、缩写、问 AI
- **翻译弹窗** - 选中文字说"什么意思"，浮窗显示翻译
- **AI 对话** - 选中文字说"问 AI"，打开对话窗口
- **唤醒词命令** - 语音控制休眠/唤醒/自动发送

### 系统集成
- **全局热键** - 可自定义 (默认 `` ` `` 键)
- **浮动球 UI** - 状态指示 + 右键菜单
- **系统托盘** - 最小化到后台
- **文本输出** - 剪贴板+Ctrl+V 或 typewriter 逐字输入 (游戏兼容)
- **管理员提权** - 检测目标应用权限，自动提示

## 系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| OS | Windows 10 64位 | Windows 11 |
| Python | 3.12 | 3.12 |
| RAM | 8GB | 16GB |
| GPU | 无 (CPU 可用) | NVIDIA GTX 16xx 及以上 |
| VRAM | - | 4GB+ |

### GPU 兼容性说明

Aria 内置 CUDA 12.8 版本的 PyTorch，GPU 加速支持情况如下：

| GPU 系列 | 架构 | GPU 加速 | 说明 |
|----------|------|----------|------|
| RTX 40xx / 50xx | Ada / Blackwell | ✅ 完全支持 | 最佳性能 |
| RTX 30xx | Ampere (sm_80) | ✅ 完全支持 | - |
| RTX 20xx | Turing (sm_75) | ✅ 支持 | - |
| GTX 16xx | Turing (sm_75) | ✅ 支持 | - |
| GTX 10xx | Pascal (sm_61) | ❌ 自动回退 CPU | CUDA 12.8 不支持此架构 |
| 无 NVIDIA GPU | - | ❌ CPU 模式 | 功能完全可用，速度稍慢 |

> **CPU 模式说明**: 没有支持的 GPU 时，Aria 会自动检测并回退到 CPU 模式。所有功能均可正常使用，识别速度约为 GPU 模式的 2-5 倍时间。无需任何手动配置。

## 快速开始

### 便携版

1. 下载 Aria 便携版压缩包
2. 解压到任意目录
3. 双击 `Aria.exe` 启动
4. 按 `` ` `` 键开始录音，再按一次结束

### 开发版

```bash
git clone <repo-url>
cd voicetype-v1.1-dev

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 复制配置模板
copy config\hotwords.template.json config\hotwords.json

# 启动 (带控制台)
Aria_debug.bat

# 启动 (无控制台)
Aria.bat
```

## 使用指南

### 基础操作

| 操作 | 说明 |
|------|------|
| 按 `` ` `` 键 | 开始录音 |
| 再按 `` ` `` 键 | 结束录音，识别并输入文字 |
| 左键浮动球 | 开始/停止录音 |
| 右键浮动球 | 打开快捷菜单 |
| 中键浮动球 | 锁定/解锁位置 |
| 拖拽浮动球 | 移动位置 (未锁定时) |
| 系统托盘单击 | 显示历史记录 (Ctrl+1-9 快速复制) |
| 系统托盘双击 | 打开热词设置 |
| 系统托盘右键 | 设置 / 静音 / 自动发送 / 退出 |

### 选区指令

1. 选中任意文字
2. 按热键说出命令：

| 命令 | 效果 |
|------|------|
| 润色 | 优化文字表达 |
| 翻译成英文/中文/日文 | 翻译选中内容 |
| 扩写 / 缩写 | 调整文字长度 |
| 什么意思 | 弹窗显示翻译 |
| 问 AI | 发起 AI 对话 |

### 唤醒词命令

说出唤醒词后跟命令 (唤醒词可在设置中自定义)：

| 命令 | 效果 |
|------|------|
| [唤醒词] 开启自动发送 | 识别完自动输入 |
| [唤醒词] 关闭自动发送 | 手动确认后输入 |
| [唤醒词] 休眠 | 暂停语音监听 |
| [唤醒词] 醒来 | 恢复语音监听 |

### 快捷菜单功能

右键浮动球打开菜单，包含：

| 功能 | 说明 |
|------|------|
| 开关 | 启用/禁用语音输入 |
| 润色模式 | 高质量 (API) / 快速 (本地) |
| 翻译输出 | 弹窗显示 / 复制到剪贴板 |
| 锁定位置 | 防止误拖拽 |
| 休眠模式 | 暂停监听，唤醒词可唤醒 |
| 实时字幕 | 显示/隐藏录音中间结果 |
| 高级设置 | 打开完整设置面板 |

## 配置

配置文件位于 `config/hotwords.json`，首次运行从 `hotwords.template.json` 复制。支持热重载（保存后 2 秒内自动生效）。

### 关键配置项

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `asr_engine` | 识别引擎 | `funasr` |
| `hotwords` | 热词列表 | `[]` |
| `replacements` | 替换规则 | `{}` |
| `polish.enabled` | API 润色 | `false` |
| `polish.api_key` | OpenRouter API Key | 需配置 |
| `local_polish.enabled` | 本地 LLM 润色 | `false` |
| `general.hotkey` | 热键 | `` ` `` |
| `vad.threshold` | VAD 灵敏度 (0-1) | `0.3` |
| `output.typewriter_mode` | 逐字输入模式 | `false` |

### 切换 ASR 引擎

```json
{ "asr_engine": "funasr" }
```

| 引擎 | 速度 | 精度 | 特点 |
|------|------|------|------|
| `funasr` | 最快 | 高 | 推荐，离线，中文优化 |
| `whisper` | 中等 | 最高 | 多语言，需下载模型 |
| `fireredasr` | 中等 | 高 | 需外部仓库 |
| `qwen3` | 中等 | 高 | 最新，多语言，上下文热词增强 |

### Qwen3-ASR 说明

Qwen3-ASR 引擎支持上下文热词增强 (Context Biasing)，可显著提升领域专有名词识别率。首次使用需下载模型 (1.2GB-3.4GB)。

配置 `model_name` 为 `"auto"` 时，Aria 会根据 GPU 显存自动选择模型：
- VRAM >= 4GB → Qwen3-ASR-1.7B (更高精度)
- VRAM < 4GB 或 CPU 模式 → Qwen3-ASR-0.6B (更快速度)

Aria 内置三级声学感知防幻觉系统，有效防止 Qwen3 在噪音环境下输出上下文热词产生的幻觉文本。

## 项目结构

```
voicetype-v1.1-dev/
├── launcher.py            # 入口: 单例检查 + splash + 模型预加载
├── app.py                 # 主应用: 状态机 + ASR 编排
├── core/                  # 核心模块
│   ├── asr/               # 语音识别引擎 (4个)
│   ├── audio/             # 音频捕获 + VAD
│   ├── hotword/           # 四层热词纠错
│   ├── selection/         # 选区指令
│   ├── wakeword/          # 唤醒词系统
│   └── command/           # 语音命令
├── ui/qt/                 # PySide6 界面
│   ├── main.py            # 主窗口 + 托盘
│   ├── floating_ball.py   # 浮动球
│   ├── settings.py        # 设置面板
│   ├── translation_popup.py
│   └── ai_chat_window.py
├── system/                # 系统集成
│   ├── hotkey.py          # 全局热键 (low-level hook)
│   ├── output.py          # 文本输出 (剪贴板/typewriter)
│   └── admin.py           # 管理员权限检测
├── config/                # 配置文件
│   ├── hotwords.template.json  # 配置模板
│   ├── wakeword.json      # 唤醒词配置
│   └── commands.json      # 语音命令定义
└── build_portable/        # 便携版打包
    ├── build.py           # 主打包脚本
    ├── release.bat        # 一键打包
    └── RELEASE_GUIDE.md   # 发布指南
```

## 构建便携版

```bash
cd voicetype-v1.1-dev
build_portable\release.bat
```

输出到 `dist_portable/Aria/`，包含完整运行环境，无需安装 Python。

## 常见问题

**Q: 识别准确率不高？**
1. 添加常用词到 `hotwords` 列表
2. 配置 `replacements` 替换规则
3. 启用 API 润色 (`polish.enabled: true` + 配置 API key)

**Q: GPU 加速不工作？**
1. 确认 `nvidia-smi` 正常运行
2. Aria 需要 NVIDIA GTX 16xx 及以上 GPU (Turing 架构+)
3. 如果 GPU 不兼容，Aria 会自动切换到 CPU 模式，无需手动干预
4. 确认显卡驱动已更新到最新版本

**Q: GTX 1060 / 1080 能用吗？**

可以使用。GTX 10xx 系列无法使用 GPU 加速（CUDA 12.8 不支持 Pascal 架构），但 Aria 会自动回退到 CPU 模式，所有功能正常工作，识别速度稍慢。

**Q: 无法在某些应用输入文字？**
1. 尝试启用 `output.typewriter_mode`（逐字输入模式，兼容游戏等应用）
2. 如目标程序以管理员运行，需要以管理员启动 Aria

**Q: 热键冲突？**

在设置面板中修改热键，或编辑 `config/hotwords.json` 的 `general.hotkey`。Aria 支持任意按键作为热键。

**Q: 首次启动很慢？**

首次使用 Whisper 或 Qwen3-ASR 引擎时，需要下载模型文件 (1-3GB)。后续启动不需要重新下载。FunASR 引擎内置模型，无需额外下载。

**Q: 没有声音提示？**

检查系统托盘右键菜单中的"静音"选项是否开启。

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

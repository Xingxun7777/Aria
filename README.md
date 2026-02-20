# Aria - AI 智能语音输入工具

> Windows 本地 AI 语音听写 · 智能纠错 · 语音指令 · 选区命令

Aria 是一款 Windows 本地语音输入工具。支持四引擎离线识别、四层智能纠错、按住说话 (PTT)、语音键盘命令和选区指令。所有语音数据在本地处理，不上传任何服务器。

---

## 功能总览

| 功能 | 说明 |
|------|------|
| 多引擎语音识别 | FunASR / Whisper / FireRedASR / Qwen3-ASR，GPU 自动加速 |
| 四层热词纠错 | ASR 引导 → 正则替换 → 拼音模糊 → AI 润色 |
| 两种输入模式 | 切换模式 (按一次开始/再按停止) 和 按住说话 (PTT) |
| 实时流式字幕 | 录音过程中浮动显示中间识别结果 |
| 选区指令 | 选中文字后语音执行润色、翻译、扩写、缩写等 |
| 语音键盘命令 | 语音说"发送""删除""撤销""复制"等模拟键盘操作 |
| 翻译弹窗 | 选中文字说"什么意思"，浮窗翻译 |
| AI 对话 | 选中文字说"问 AI"，发起上下文对话 |
| 唤醒词控制 | 语音控制休眠/唤醒/自动发送 |
| 文本输出兼容 | 剪贴板粘贴模式 + 逐字输入 (Typewriter) 模式 |
| 管理员提权 | 自动检测目标应用权限，智能提示 |
| 浮动球 UI | 状态指示 + 语音活动动画 + 右键快捷菜单 |

---

## 系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| OS | Windows 10 64位 | Windows 11 |
| Python | 3.12 | 3.12 |
| RAM | 8GB | 16GB |
| GPU | 无 (CPU 可用) | NVIDIA GTX 16xx 及以上 |
| VRAM | — | 4GB+ |

### GPU 兼容性

Aria 内置 CUDA 12.8 的 PyTorch。无 GPU 或不兼容 GPU 时**自动回退 CPU 模式**，无需手动配置。

| GPU 系列 | 架构 | GPU 加速 |
|----------|------|----------|
| RTX 40xx / 50xx | Ada / Blackwell | ✅ 完全支持 |
| RTX 30xx | Ampere (sm_80) | ✅ 完全支持 |
| RTX 20xx / GTX 16xx | Turing (sm_75) | ✅ 支持 |
| GTX 10xx | Pascal (sm_61) | ❌ 自动 CPU 回退 |
| 无 NVIDIA GPU | — | ❌ CPU 模式 |

> CPU 模式下所有功能正常工作，识别速度约为 GPU 的 2-5 倍时间。

---

## 快速开始

### 便携版 (推荐)

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

# 启动 (带控制台，用于调试)
Aria_debug.bat

# 启动 (无控制台，日常使用)
Aria.bat
```

---

## 使用指南

### 基础操作

| 操作 | 说明 |
|------|------|
| 按 `` ` `` 键 | 开始/停止录音 (切换模式) |
| 按住右 Ctrl | 持续录音，松开自动识别 (PTT 模式) |
| 左键浮动球 | 开始/停止录音 |
| 右键浮动球 | 打开快捷菜单 |
| 中键浮动球 | 锁定/解锁位置 (锁定后隐藏流式字幕) |
| 拖拽浮动球 | 移动位置 (未锁定时) |
| 系统托盘单击 | 显示历史记录 (Ctrl+1-9 快速复制) |
| 系统托盘双击 | 打开热词设置 |
| 系统托盘右键 | 设置 / 静音 / 自动发送 / 退出 |

### 输入模式

Aria 支持两种输入模式，可在快捷菜单中切换：

| 模式 | 操作方式 | 适用场景 |
|------|----------|----------|
| **切换模式** (默认) | 按热键开始，再按停止 | 长段落听写，边想边说 |
| **按住说话 (PTT)** | 按住右 Ctrl 说话，松开自动识别 | 短句快速输入，聊天场景 |

**PTT 模式特点：**
- 默认按键：右 Ctrl (可在配置中修改)
- 松开后自动合并所有音频段并识别
- 短于 0.3 秒的误触自动忽略
- 超过 60 秒自动停止 (安全保护)
- 可在快捷菜单中一键切换回切换模式

### 流式字幕

录音过程中，浮动球上方会实时显示识别中间结果。采用半透明毛玻璃面板，在任何桌面背景下都清晰可读。

- 右键菜单 → "实时字幕" 开关可控制显示
- 锁定浮动球 (中键) 后字幕自动隐藏
- PTT 模式下不显示流式字幕 (松开后直接输出完整结果)

### 选区指令

1. 选中任意文字
2. 按热键后说出命令

| 命令 | 效果 |
|------|------|
| 润色 / 优化 | 提升文字表达质量 |
| 翻译成英文 | 替换为英文翻译 |
| 翻译成中文 | 替换为中文翻译 |
| 翻译成日文 | 替换为日文翻译 |
| 扩写 / 展开 | 增加更多细节和深度 |
| 缩写 / 精简 | 保留核心信息 |
| 重写 / 改写 | 使用不同表达方式 |
| 什么意思 | 弹窗翻译 (不替换原文) |
| 问 AI | 打开 AI 对话窗口 |
| 总结一下 | 弹窗显示总结 |

### 语音键盘命令

说出唤醒词 + 命令词，Aria 会模拟对应的键盘操作：

| 命令 | 模拟按键 | 效果 |
|------|----------|------|
| [唤醒词] 发送 | Enter | 发送消息/确认 |
| [唤醒词] 换行 | Enter | 换行 |
| [唤醒词] 删除 | Backspace | 删除一个字符 |
| [唤醒词] 撤销 | Ctrl+Z | 撤销操作 |
| [唤醒词] 重做 | Ctrl+Y | 重做操作 |
| [唤醒词] 复制 | Ctrl+C | 复制选中内容 |
| [唤醒词] 粘贴 | Ctrl+V | 粘贴 |
| [唤醒词] 全选 | Ctrl+A | 全选内容 |
| [唤醒词] 保存 | Ctrl+S | 保存文件 |
| [唤醒词] 剪切 | Ctrl+X | 剪切选中内容 |

> 语音键盘命令可在 `config/commands.json` 中自定义。

### 唤醒词命令

| 命令 | 效果 |
|------|------|
| [唤醒词] 开启自动发送 | 识别完毕自动输入 |
| [唤醒词] 关闭自动发送 | 手动确认后输入 |
| [唤醒词] 休眠 | 暂停语音监听 |
| [唤醒词] 醒来 | 恢复语音监听 |
| [唤醒词] 记一下 + 内容 | 记录想法到笔记 |

> 唤醒词默认为 "遥遥"，可在设置中修改。可选唤醒词：瑶瑶、小朋友、小溪、助手、小白。

### 快捷菜单

右键浮动球打开菜单，包含以下功能：

| 功能 | 说明 |
|------|------|
| Aria 开关 | 启用/禁用语音输入 |
| 引擎信息 | 显示当前 ASR 引擎名称 |
| 输入模式 | 切换"切换模式"或"按住说话" |
| 润色模式 | 高质量 (API) / 快速 (本地 LLM) |
| 翻译输出 | 弹窗显示 / 复制到剪贴板 |
| 高级设置 | 打开完整设置面板 |
| 锁定位置 | 防止误拖拽，同时隐藏流式字幕 |
| 休眠模式 | 暂停监听，唤醒词可唤醒 |
| 实时字幕 | 开关录音中间结果显示 |

---

## 语音识别引擎

| 引擎 | 速度 | 精度 | 语言 | 特点 |
|------|------|------|------|------|
| **FunASR** (默认) | ⚡ 最快 | 高 | 中文 | 推荐，离线，内置模型无需下载 |
| **Whisper** | 中等 | 最高 | 多语言 | large-v3-turbo，首次需下载模型 |
| **FireRedASR** | 中等 | 高 | 中文 | 需外部仓库 |
| **Qwen3-ASR** | 中等 | 高 | 多语言 | 上下文热词增强，防幻觉系统 |

### Qwen3-ASR 特别说明

- 支持上下文热词增强 (Context Biasing)，显著提升专有名词识别率
- 首次使用需下载模型 (1.2GB-3.4GB)
- `model_name: "auto"` 自动选择：VRAM >= 4GB → 1.7B，否则 → 0.6B
- 内置三级声学感知防幻觉系统，防止噪音环境下输出热词幻觉

### 四层热词纠错

| 层级 | 名称 | 说明 |
|------|------|------|
| Layer 1 | ASR initial_prompt | 引导识别引擎偏向领域词汇 |
| Layer 2 | 正则规则替换 | 基于规则的文字映射 (如 scale → skill) |
| Layer 2.5 | 拼音模糊匹配 | 利用拼音纠正同音字 (如 "星循" → "星巡") |
| Layer 3 | AI 润色 | API 或本地 LLM 润色，修正语法和同音字 |

### 热词权重系统

每个热词可设置权重，控制在各层级的参与程度：

| 权重 | ASR 分数 | 正则替换 | 拼音匹配 | LLM 提示 |
|------|----------|----------|----------|----------|
| 0 | 跳过 | ❌ | ❌ | 禁用 |
| 0.3 | 15 (提示) | ❌ | ❌ | 仅提示 |
| 0.5 | 50 (标准) | ✅ | ❌ | 参考 |
| 0.7 | 70 (强) | ✅ | ❌ | 强参考 |
| 0.9 | 85 (很强) | ✅ | ✅ | 强参考 |
| 1.0 | 100 (锁定) | ✅ | ✅ | 必须 |

---

## 文本输出

### 剪贴板模式 (默认)

识别完成后自动通过 Ctrl+V 粘贴到当前活动窗口。粘贴后恢复原始剪贴板内容。

### Typewriter 模式

逐字符模拟键盘输入。兼容以下场景：
- 游戏内聊天框
- 以管理员运行的应用
- 不支持 Ctrl+V 粘贴的应用

配置 `output.typewriter_mode: true` 启用，`output.typewriter_delay_ms` 控制逐字间隔。

### 管理员提权

当目标应用以管理员运行而 Aria 未提权时，Aria 会弹窗提示。可勾选"不再提醒"后自动禁用（按热键可重新启用）。

---

## 配置

配置文件 `config/hotwords.json`，首次运行自动从模板创建。**保存后 2 秒内自动热重载**。

### 关键配置项

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `asr_engine` | 识别引擎 (`funasr` / `whisper` / `fireredasr` / `qwen3`) | `funasr` |
| `hotwords` | 热词列表 | `[]` |
| `hotword_weights` | 热词权重映射 | `{}` |
| `replacements` | 正则替换规则 | `{}` |
| `domain_context` | 领域上下文描述 (提升特定领域识别率) | `""` |
| `polish.enabled` | API 润色开关 | `false` |
| `polish.api_key` | OpenRouter API Key | 需配置 |
| `polish.model` | 润色模型 | `deepseek/deepseek-chat-v3.1:free` |
| `local_polish.enabled` | 本地 LLM 润色 | `false` |
| `local_polish.model_path` | 本地 GGUF 模型路径 | `models/qwen2.5-1.5b-instruct-q4_k_m.gguf` |
| `general.hotkey` | 全局热键 | `` ` `` |
| `general.input_mode` | 输入模式 (`toggle` / `ptt`) | `toggle` |
| `general.ptt_key` | PTT 按键 | `right_ctrl` |
| `general.audio_device` | 音频设备 (空字符串=自动) | `""` |
| `vad.threshold` | VAD 灵敏度 (0-1) | `0.3` |
| `vad.min_silence_ms` | 静默判定阈值 (ms) | `1200` |
| `output.typewriter_mode` | 逐字输入模式 | `false` |
| `output.typewriter_delay_ms` | 逐字间隔 (ms) | `15` |
| `output.check_elevation` | 管理员权限检测 | `true` |
| `translation.output_mode` | 翻译输出 (`popup` / `clipboard`) | `popup` |

### 配置示例：启用 API 润色

```json
{
  "polish": {
    "enabled": true,
    "api_url": "https://openrouter.ai/api",
    "api_key": "sk-or-v1-xxxx",
    "model": "deepseek/deepseek-chat-v3.1:free",
    "timeout": 10
  }
}
```

### 配置示例：切换到 PTT 模式

```json
{
  "general": {
    "input_mode": "ptt",
    "ptt_key": "right_ctrl"
  }
}
```

---

## 项目结构

```
voicetype-v1.1-dev/
├── launcher.py              # 入口: 单例检查 + splash + 模型预加载
├── app.py                   # 主应用: 状态机 + ASR 编排 (~2300行)
├── core/                    # 核心模块
│   ├── asr/                 # 语音识别引擎 (FunASR, Whisper, FireRedASR, Qwen3)
│   ├── audio/               # 音频捕获 + Silero-VAD 语音检测
│   ├── hotword/             # 四层热词纠错 + AI 润色
│   ├── selection/           # 选区指令 (润色/翻译/扩写/缩写/问AI)
│   └── wakeword/            # 唤醒词检测 + 命令执行
├── ui/qt/                   # PySide6 界面
│   ├── main.py              # 主窗口 + 托盘 + 信号连接
│   ├── floating_ball.py     # 浮动球 + 流式字幕标签
│   ├── popup_menu.py        # 右键快捷菜单
│   ├── settings.py          # 高级设置面板
│   ├── translation_popup.py # 翻译弹窗
│   ├── ai_chat_window.py    # AI 对话窗口
│   ├── history.py           # 历史记录窗口
│   └── elevation_dialog.py  # 管理员提权对话框
├── system/                  # 系统集成
│   ├── hotkey.py            # 全局热键 + PTT Handler (pynput)
│   ├── output.py            # 文本输出 (剪贴板/typewriter)
│   └── admin.py             # 管理员权限检测
├── config/                  # 配置文件
│   ├── hotwords.template.json  # 配置模板 (分发用)
│   ├── wakeword.json        # 唤醒词 + 语音命令定义
│   └── commands.json        # 语音键盘命令定义
└── build_portable/          # 便携版打包
    ├── build.py             # 主打包脚本
    ├── release.bat          # 一键打包
    └── RELEASE_GUIDE.md     # 发布指南
```

---

## 构建便携版

```bash
cd voicetype-v1.1-dev
build_portable\release.bat
```

输出到 `dist_portable/Aria/` (含完整运行环境，无需安装 Python)。

---

## 常见问题

**Q: 识别准确率不高？**
1. 添加领域专有名词到 `hotwords` 列表，设置合适权重
2. 配置 `replacements` 替换规则（处理固定的误识别）
3. 填写 `domain_context` 描述使用场景（如"编程技术讨论"）
4. 启用 API 润色 (`polish.enabled: true` + 配置 API key)

**Q: GPU 加速不工作？**
1. 确认 `nvidia-smi` 正常运行
2. GTX 16xx 及以上 GPU 自动启用加速
3. GTX 10xx 或更旧显卡自动回退 CPU，无需手动干预
4. 确认显卡驱动已更新到最新版本

**Q: 无法在某些应用输入文字？**
1. 启用 `output.typewriter_mode`（兼容游戏等应用）
2. 如目标程序以管理员运行，以管理员启动 Aria

**Q: 热键冲突？**

在设置面板中修改热键，或编辑 `config/hotwords.json` 的 `general.hotkey`。

**Q: 首次启动很慢？**

Whisper 和 Qwen3-ASR 首次使用需下载模型 (1-3GB)。FunASR 内置模型，无需额外下载。

**Q: PTT 模式没有反应？**
1. 确认已在快捷菜单切换到"按住说话"模式
2. 默认 PTT 按键是右 Ctrl，确认按的是正确按键
3. 按住时间需超过 0.3 秒（防误触保护）

**Q: 流式字幕不显示？**
1. 检查快捷菜单中"实时字幕"是否开启
2. 锁定状态下字幕会自动隐藏
3. PTT 模式下不显示流式字幕（设计如此）

---

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
| [pynput](https://github.com/moses-palmer/pynput) | LGPL v3 | 键盘监听 (PTT) |

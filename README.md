# Aria - AI 智能语音输入工具

Aria 是一款 Windows 本地语音输入工具，支持离线语音识别、智能热词纠错和选区指令处理。

## 功能特点

- **本地离线语音识别** - 支持 FunASR (Paraformer-zh) 和 Whisper 双引擎
- **三层热词纠错系统** - initial_prompt 引导 + 规则替换 + 拼音模糊匹配 + API 润色
- **选区指令** - 选中文字后语音执行润色、翻译、扩写等操作
- **唤醒词控制** - 语音命令控制 ("瑶瑶开启自动发送")
- **翻译弹窗** - 选中文字直接翻译显示 (v1.1 新功能)
- **AI 对话窗口** - 选中文字发起 AI 对话 (v1.1 新功能)
- **全局热键** - CapsLock 或 ` 键快速启动录音
- **现代界面** - Qt6 浮动球 + 系统托盘

## 系统要求

- Windows 10/11 64位
- Python 3.10+
- NVIDIA GPU (推荐，CUDA 12.x)
- 4GB+ 显存 (使用 GPU 加速时)

## 快速开始

### 便携版

1. 下载 Aria 便携版
2. 解压到任意目录
3. 双击 `Aria.vbs` 启动
4. 按 `` ` `` 键开始录音，松开结束

### 开发版

```bash
# 克隆仓库
git clone https://github.com/yourname/aria.git
cd aria

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 复制配置模板
copy config\hotwords.template.json config\hotwords.json

# 启动
python launcher.py
```

## 使用指南

### 基础用法

| 操作 | 说明 |
|------|------|
| 按住 `` ` `` 键 | 开始录音 |
| 松开 `` ` `` 键 | 结束录音，识别并输入文字 |
| 按住 CapsLock | 备用热键 |
| 点击浮动球 | 切换激活状态 |
| 右键托盘图标 | 打开设置/退出 |

### 热词配置

编辑 `config/hotwords.json`：

```json
{
  "hotwords": ["Claude", "GitHub", "Codex"],
  "replacements": {
    "克劳德": "Claude",
    "吉他": "GitHub"
  }
}
```

### 选区指令

1. 选中任意文字
2. 按热键说出命令
3. 支持的命令:
   - **润色** - 优化文字表达
   - **翻译成英文/中文/日文** - 翻译选中内容
   - **扩写/缩写** - 调整文字长度
   - **什么意思** - 弹窗显示翻译
   - **问AI** - 发起 AI 对话

### 唤醒词命令

说出 "瑶瑶" 或其他唤醒词后跟命令：

| 命令 | 效果 |
|------|------|
| 瑶瑶开启自动发送 | 自动发送识别结果 |
| 瑶瑶关闭自动发送 | 手动确认后发送 |
| 瑶瑶休眠 | 暂停语音监听 |
| 瑶瑶醒来 | 恢复语音监听 |

## 配置说明

### hotwords.json

| 字段 | 说明 |
|------|------|
| `asr_engine` | 识别引擎: `funasr` 或 `whisper` |
| `hotwords` | 热词列表，提高识别准确率 |
| `replacements` | 替换规则表 |
| `polish.enabled` | 启用 API 润色 |
| `polish.api_key` | OpenRouter API Key |
| `local_polish.enabled` | 启用本地 LLM 润色 |
| `general.hotkey` | 热键设置: `grave` 或 `capslock` |

### wakeword.json

| 字段 | 说明 |
|------|------|
| `enabled` | 启用唤醒词 |
| `wakeword` | 当前唤醒词 |
| `commands` | 命令定义 |

## 技术架构

```
aria/
├── launcher.py          # 启动器 (单例检查、预加载)
├── app.py               # 主应用入口
├── core/
│   ├── asr/             # 语音识别引擎
│   │   ├── funasr_engine.py
│   │   └── whisper_engine.py
│   ├── audio/           # 音频处理
│   │   ├── capture.py   # 麦克风捕获
│   │   └── vad.py       # 静音检测 (Silero-VAD)
│   ├── hotword/         # 热词纠错
│   │   ├── manager.py   # L1: initial_prompt
│   │   ├── processor.py # L2: 规则替换
│   │   ├── fuzzy_matcher.py # L2.5: 拼音匹配
│   │   └── polish.py    # L3: API 润色
│   ├── selection/       # 选区处理
│   └── wakeword/        # 唤醒词识别
├── ui/qt/               # Qt6 界面
│   ├── floating_ball.py # 浮动球
│   ├── settings.py      # 设置面板
│   ├── translation_popup.py # 翻译弹窗
│   └── ai_chat_window.py # AI 对话窗口
└── system/              # 系统集成
    ├── hotkey.py        # 全局热键
    └── output.py        # 文本输出
```

## 常见问题

### Q: 识别准确率不高怎么办？

1. 检查麦克风是否正确设置
2. 添加常用词到 `hotwords` 列表
3. 配置 `replacements` 替换规则
4. 启用 API 润色 (`polish.enabled: true`)

### Q: GPU 加速不工作？

1. 确认安装了 CUDA 12.x
2. 检查 `nvidia-smi` 是否正常
3. 在配置中设置 `device: "cuda"`

### Q: 热键冲突？

编辑 `config/hotwords.json`，修改 `general.hotkey`:
- `grave` - `` ` `` 键
- `capslock` - CapsLock 键

### Q: 如何切换 ASR 引擎？

修改 `config/hotwords.json`:
```json
{
  "asr_engine": "whisper"
}
```

FunASR 更快，Whisper 准确率更高。

## 更新日志

查看 [CHANGELOG.md](CHANGELOG.md)

## 许可证

MIT License

## 致谢

- [FunASR](https://github.com/alibaba-damo-academy/FunASR) - 阿里达摩院语音识别
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) - OpenAI Whisper 优化实现
- [Silero-VAD](https://github.com/snakers4/silero-vad) - 语音活动检测
- [PySide6](https://www.qt.io/) - Qt6 Python 绑定

# VoiceType - Local AI Voice Dictation

本地AI语音听写工具，按CapsLock键即可将语音转为文字并自动输入。

## 功能特性

- **本地离线运行**：基于Whisper ASR，无需联网
- **GPU加速**：支持CUDA，4090/5090可实时转录
- **中文优化**：使用medium模型，language="zh"
- **智能VAD**：Silero-VAD过滤静音，只处理语音
- **全局热键**：CapsLock一键开始/停止

## 快速开始

```bash
# 安装依赖
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install openai-whisper silero-vad sounddevice

# 运行
python -m voicetype
```

## 使用方法

1. 启动后等待模型加载（约10秒）
2. 按 **CapsLock** 开始录音
3. 说话
4. 再按 **CapsLock** 停止
5. 识别结果自动粘贴到光标位置

## 命令行参数

```bash
python -m voicetype --hotkey capslock  # 默认热键
python -m voicetype --hotkey "ctrl+shift+space"  # 自定义热键
python -m voicetype --list-devices  # 列出音频设备
```

## 技术架构

```
[Hotkey] → [Audio Capture] → [VAD Filter] → [Whisper ASR] → [Text Paste]
   ↓            ↓                ↓              ↓              ↓
CapsLock   sounddevice      Silero-VAD    medium/CUDA    Ctrl+V paste
```

## 配置

当前配置（app.py）：
- 模型：`medium`（1.42GB，准确率最高）
- 设备：`cuda`（GPU加速）
- 语言：`zh`（中文）
- VAD阈值：0.5
- 最小语音：250ms
- 最小静音：300ms

## 开发进度

### Phase 1: 核心功能 ✅
- [x] 音频捕获 + VAD集成
- [x] Whisper ASR引擎
- [x] 全局热键（CapsLock）
- [x] 文本自动粘贴
- [x] GPU加速（CUDA）
- [x] 修复：重复发送问题
- [x] 修复：热键线程问题

### Phase 2: 热词系统（规划中）
- [ ] initial_prompt 领域词汇
- [ ] 发音相似词替换表
- [ ] 可选：AI智能纠错

### Phase 3: 增强功能（未开始）
- [ ] 系统托盘图标
- [ ] 配置文件
- [ ] 多语言支持
- [ ] 流式显示

## 文件结构

```
voicetype/
├── app.py              # 主应用入口
├── core/
│   ├── audio/
│   │   ├── capture.py  # 音频捕获+VAD
│   │   └── vad.py      # Silero-VAD封装
│   ├── asr/
│   │   └── whisper_engine.py  # Whisper引擎
│   └── logging.py      # 日志系统
├── system/
│   ├── hotkey.py       # 全局热键
│   └── output.py       # 文本输出（Ctrl+V）
└── ui/
    └── streaming_display.py  # 流式显示（预留）
```

## 依赖

- Python 3.10+
- PyTorch 2.4+ (CUDA)
- openai-whisper
- silero-vad
- sounddevice

## License

MIT

# Aria 项目文档

> Windows 本地 AI 语音听写工具
> 版本: v1.0 (2025-12)
> 路径: `G:\AIBOX\aria`

---

## 快速概览

按热键说话 → 本地 AI 识别 → 智能纠错润色 → 自动输入到光标位置

**核心特性:**
- FunASR (Paraformer) 本地中文语音识别
- Silero-VAD 智能语音活动检测
- 三层热词纠错系统 + AI 润色
- 选区指令（选中文字后说"润色"/"翻译"）
- 唤醒词控制（"瑶瑶开启自动发送"）
- PySide6 浮动球 UI

---

## 目录结构

```
aria/
├── app.py                 # 主应用 (69KB) - 核心逻辑、状态机、ASR流程
├── launcher.py            # 启动器 - 环境检测、splash、异常处理
├── config/
│   ├── hotwords.json      # 用户配置 (热词、API、模型设置)
│   └── settings.py        # 配置加载器
│
├── core/                  # 核心模块
│   ├── asr/               # 语音识别引擎
│   │   ├── base.py        # ASR 基类接口
│   │   ├── funasr_engine.py   # FunASR Paraformer (当前使用)
│   │   ├── whisper_engine.py  # Whisper (备选)
│   │   └── fireredasr_engine.py # FireRedASR (实验性)
│   │
│   ├── audio/             # 音频处理
│   │   ├── capture.py     # 音频捕获 (sounddevice)
│   │   └── vad.py         # Silero-VAD 语音检测
│   │
│   ├── hotword/           # 热词纠错系统 (三层)
│   │   ├── manager.py     # Layer1: initial_prompt 领域词汇
│   │   ├── processor.py   # Layer2: 规则替换
│   │   ├── fuzzy_matcher.py  # Layer2.5: 拼音模糊匹配
│   │   ├── polish.py      # Layer3: AI 在线润色 (Gemini/DeepSeek)
│   │   └── local_polish.py   # Layer3-alt: 本地 LLM 润色 (llama.cpp)
│   │
│   ├── selection/         # 选区指令系统
│   │   ├── detector.py    # 检测选中文字 (Ctrl+C)
│   │   ├── commands.py    # 指令解析 (润色/翻译/总结...)
│   │   └── processor.py   # LLM 处理选中内容
│   │
│   ├── wakeword/          # 唤醒词系统 ("瑶瑶")
│   │   ├── detector.py    # 唤醒词检测
│   │   └── executor.py    # 命令执行
│   │
│   ├── action/            # UI 动作类型
│   │   └── types.py       # TranslationAction, ChatAction
│   │
│   ├── debug.py           # 调试信息收集
│   ├── logging.py         # 日志系统
│   ├── insight_store.py   # 历史记录存储
│   └── model_manager.py   # 模型下载管理
│
├── system/                # 系统交互
│   ├── hotkey.py          # 全局热键 (Windows API)
│   ├── output.py          # 文本输出 (剪贴板 + Ctrl+V)
│   └── platform/          # 平台特定代码
│
├── ui/                    # 用户界面
│   ├── qt/                # PySide6 Qt UI
│   │   ├── main.py        # 主窗口
│   │   ├── floating_ball.py  # 浮动球 (核心 UI)
│   │   ├── settings.py    # 设置面板
│   │   ├── ai_chat_window.py  # AI 对话窗口
│   │   ├── translation_popup.py # 翻译弹窗
│   │   ├── history.py     # 历史记录
│   │   ├── overlay.py     # 录音状态浮层
│   │   ├── bridge.py      # 后端信号桥
│   │   └── workers/       # 后台线程
│   └── streaming_display.py  # 流式显示 (预留)
│
├── models/                # 本地模型
│   └── qwen2.5-1.5b-instruct-q4_k_m.gguf  # 本地润色模型 (1.1GB)
│
├── DebugLog/              # 调试日志 (gitignore)
├── docs/                  # 文档
├── tests/                 # 测试
└── tools/                 # 工具脚本
```

---

## 处理流程

```
┌─────────────────────────────────────────────────────────────────┐
│                      Aria 处理流水线                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  [热键触发] ──► [录音] ──► [VAD检测] ──► [ASR识别]              │
│      │            │            │            │                    │
│   CapsLock   sounddevice   Silero-VAD   FunASR/Paraformer       │
│                                                                  │
│  ──► [唤醒词检测] ──► [选区指令检测] ──► [热词纠错]             │
│          │                │                  │                   │
│      "瑶瑶xxx"        "润色/翻译"      Layer1+2+2.5             │
│                                                                  │
│  ──► [AI润色] ──► [文本输出] ──► [自动发送?]                    │
│         │            │              │                            │
│    DeepSeek     Ctrl+V paste    Enter (可选)                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 热词纠错系统 (三层)

| 层级 | 名称 | 作用 | 示例 |
|------|------|------|------|
| Layer 1 | initial_prompt | ASR 提示词，引导识别方向 | domain_context: "AI工具、编程" |
| Layer 2 | 规则替换 | 固定替换规则 | "scale" → "skill" |
| Layer 2.5 | 拼音模糊 | 相似发音匹配 | "克劳德" → "Claude" |
| Layer 3 | AI 润色 | LLM 智能纠错+标点 | "吉他" → "GitHub" |

---

## 配置文件 (config/hotwords.json)

```json
{
  "hotwords": ["claude", "github", "aria", ...],
  "replacements": {"scale": "skill"},
  "domain_context": "AI工具、编程",

  "polish": {
    "enabled": true,
    "api_url": "https://openrouter.ai/api",
    "model": "deepseek/deepseek-chat"
  },

  "general": {
    "hotkey": "grave",
    "start_active": true
  },

  "funasr": {
    "model_name": "paraformer-zh",
    "device": "cuda"
  },

  "vad": {
    "threshold": 0.2,
    "min_silence_ms": 1200
  }
}
```

---

## 选区指令

选中文字后按热键说话，支持以下指令：

| 指令词 | 功能 | 行为 |
|--------|------|------|
| "润色" / "优化" | 润色文字 | 替换选中内容 |
| "翻译" / "翻译成英文" | 翻译弹窗 | 显示翻译结果 |
| "总结" / "帮我总结" | 总结内容 | 替换选中内容 |
| "问AI" / "解释一下" | AI对话 | 打开对话窗口 |

---

## 唤醒词命令 ("瑶瑶")

| 命令 | 功能 |
|------|------|
| "瑶瑶开启自动发送" | 输入后自动按 Enter |
| "瑶瑶关闭自动发送" | 关闭自动发送 |
| "瑶瑶睡觉" | 进入休眠模式 |
| "瑶瑶醒来" | 退出休眠模式 |

---

## 版本历史

### v1.0 (2025-12)
- FunASR Paraformer 中文识别
- 三层热词纠错系统
- AI 在线润色 (DeepSeek/Gemini)
- 选区指令 (润色/翻译成英文/总结) - 替换选中文字
- 唤醒词控制 ("瑶瑶")
- PySide6 浮动球 UI
- 系统托盘
- 调试日志系统

### v1.1 (2025-12) - 当前版本
**Action-driven 架构升级：**
- [x] 翻译弹窗 - 选中文字说"翻译"，显示弹窗而非替换原文
- [x] AI 对话窗口 - 选中文字说"问AI"，打开对话窗口
- [x] TranslationAction / ChatAction 动作类型
- [x] QtBridge 信号机制
- [x] TranslationWorker 异步翻译

**待修复/优化：**
- [ ] AI 润色过于精简问题 (已修复 prompt，待验证)
- [ ] 新录音可能清空之前文字的问题 (待复现定位)

### v1.2 (计划中)
- [ ] 流式 ASR 显示 (边说边显示)
- [ ] 多语言支持
- [ ] 语音输入历史搜索
- [ ] 自定义选区指令模板

---

## 启动方式

```bash
# 方式1: 双击启动
Aria.bat

# 方式2: Python 启动
python -m aria

# 方式3: 调试模式
Aria_debug.bat
```

---

## 环境要求

- Windows 10/11
- Python 3.10+ (建议 3.12)
- CUDA 12.4+ (RTX 5090 支持)
- 独立 conda 环境: `aria`

---

## 相关文件说明

| 文件 | 用途 |
|------|------|
| `Aria.bat` | 主启动脚本 |
| `launcher.py` | Python 启动器 |
| `app.py` | 主应用逻辑 |
| `config/hotwords.json` | 用户配置 |
| `DebugLog/` | 调试日志 |
| `docs/RELEASE_CHECKLIST.md` | 发布检查清单 |

---

*最后更新: 2025-12-17*

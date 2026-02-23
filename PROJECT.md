# Aria v1.1 - 项目技术文档

> Windows 本地 AI 语音听写 + 智能指令工具
> 版本: v1.1.2 (2026-02)
> Python: 3.12.4 | Qt: PySide6 | GPU: CUDA 12.x

---

## 启动链路

### 开发模式
```
Aria.bat → .venv\Scripts\pythonw.exe launcher.py
  └→ launcher.py: 单例检查(Named Mutex) → splash屏幕 → from aria.ui.qt.main import main → main()
       └→ aria/__init__.py: __path__ = [项目根] → 所有 from aria.xxx import 重定向到根目录
            └→ main() → AriaApp() → start() → hotkey/ASR/UI 全部就绪
```

### 便携版
```
Aria.exe → launcher_stub.py (PyInstaller EXE)
  └→ _internal/AriaRuntime.exe -s -m aria.launcher
       └→ aria 是普通包 (根 __init__.py，无 __path__ 重定向)
```

### 启动脚本

| 文件 | 用途 |
|------|------|
| `Aria.bat` | 生产启动 (pythonw, 无控制台) |
| `Aria_debug.bat` | 调试启动 (python, 有控制台) |
| `run_debug.bat` | 高级调试 (PATH 隔离, Torch DLL 优先) |

---

## 目录结构

```
Aria/
├── launcher.py            # 入口: 单例 + splash + 环境检测
├── app.py                 # 主应用 (~2300行): 状态机 + ASR 编排 + 所有流程
├── progress_ipc.py        # splash 进程间通信
├── __init__.py            # 根包元数据 (__version__)
├── __main__.py            # python -m aria 入口
│
├── aria/                  # 包别名 (__path__ 重定向到项目根)
│   ├── __init__.py        # __path__ = [parent_dir] 实现重定向
│   └── __main__.py        # module 入口
│
├── core/                  # 核心模块
│   ├── asr/               # 语音识别引擎 (2个)
│   │   ├── __init__.py    # ASR 基类接口
│   │   ├── qwen3_engine.py   # Qwen3-ASR (默认)
│   │   └── funasr_engine.py   # FunASR Paraformer
│   ├── audio/
│   │   ├── capture.py     # 音频捕获 (sounddevice, 有界队列)
│   │   └── vad.py         # Silero-VAD (阈值0.3, 线程安全)
│   ├── hotword/           # 4层热词纠错
│   │   ├── manager.py     # 配置管理 + Layer1 initial_prompt
│   │   ├── processor.py   # Layer2: 规则替换
│   │   ├── fuzzy_matcher.py # Layer2.5: 拼音模糊匹配
│   │   ├── polish.py      # Layer3: API 润色 (OpenRouter)
│   │   └── local_polish.py # Layer3: 本地 LLM 润色 (llama.cpp)
│   ├── selection/         # 选区指令 (润色/翻译/扩写/问AI)
│   ├── wakeword/          # 唤醒词系统
│   ├── command/           # 语音键盘命令 (检测+执行)
│   ├── command/           # 语音命令检测
│   ├── action/            # UI 动作类型 (Translation/Chat Action)
│   ├── debug.py           # DebugConfig + DebugSession (JSON 日志)
│   ├── logging.py         # 系统日志
│   ├── insight_store.py   # 历史记录
│   └── utils/             # 工具函数
│
├── system/                # 系统交互
│   ├── hotkey.py          # 全局热键 (Win32 RegisterHotKey)
│   ├── output.py          # 文本输出 (剪贴板+Ctrl+V / typewriter模式)
│   └── admin.py           # 管理员权限检测
│
├── ui/                    # 用户界面
│   ├── qt/
│   │   ├── main.py        # 主窗口 + 系统托盘
│   │   ├── floating_ball.py # 浮动球 UI
│   │   ├── popup_menu.py  # 右键菜单
│   │   ├── settings.py    # 设置面板 (84KB)
│   │   ├── bridge.py      # QtBridge 线程安全信号桥
│   │   ├── translation_popup.py # 翻译弹窗
│   │   ├── ai_chat_window.py   # AI 对话窗口
│   │   ├── elevation_dialog.py # 管理员提权对话框
│   │   ├── splash.py      # 启动画面
│   │   ├── history.py     # 历史记录面板
│   │   ├── styles.py      # 样式定义
│   │   └── workers/       # 后台线程 (翻译等)
│   └── streaming_display.py # 流式显示缓冲区
│
├── config/
│   ├── hotwords.json      # 用户配置 (gitignored, 含 API key)
│   ├── hotwords.template.json # 配置模板 (入库)
│   ├── wakeword.json      # 唤醒词配置
│   └── commands.json      # 语音命令定义
│
├── build_portable/        # 便携版打包系统
│   ├── build.py           # 主打包脚本 (嵌入式Python + 代码 + 依赖)
│   ├── build_launcher_exe.py  # 编译 Aria.exe 启动器
│   ├── launcher_stub.py   # EXE 源码 (最小启动器)
│   ├── release.bat        # 一键打包 (build + exe + 验证)
│   └── RELEASE_GUIDE.md   # 发布指南
│
├── assets/aria.ico        # 应用图标
├── models/                # 本地模型 (GGUF)
├── DebugLog/              # 调试日志 (gitignored)
├── data/insights/         # 用户历史 (gitignored)
├── tests/                 # 测试
├── tools/                 # 开发工具
└── docs/                  # 文档
    └── DEBUG_LESSONS.md   # 调试经验库
```

---

## 核心处理流水线

```
热键触发 → 状态 RECORDING
  │
  ├→ sounddevice 音频回调 → VAD 检测 (Silero)
  │    └→ 语音开始 → ASR 队列 (maxsize=5)
  │    └→ 语音结束 → ASR 队列
  │
  ├→ 流式显示 (1.5s 间隔中间识别)
  │
  └→ 热键再次 → 状态 TRANSCRIBING
       │
       ├→ ASR Worker 线程消费队列
       │    └→ 引擎识别 (FunASR/Whisper/Qwen3)
       │
       ├→ Layer -1: 唤醒词检测 → 语音命令 (sleep/wake/auto-send)
       ├→ Layer  0: 选区指令检测 → AI 处理
       ├→ Layer  1: initial_prompt (ASR 引导)
       ├→ Layer  2: 规则替换 (regex)
       ├→ Layer 2.5: 拼音模糊匹配
       ├→ Layer  3: AI 润色 (API/本地LLM)
       │
       └→ 文本输出 (剪贴板+Ctrl+V / typewriter) → 状态 IDLE
```

---

## 线程模型

| 线程 | 用途 | 同步机制 |
|------|------|----------|
| UI 主线程 | PySide6 事件循环 | QueuedConnection |
| 音频回调 | sounddevice 采集 | _buffer_lock |
| ASR Worker | 识别+热词+输出 | _asr_lock, _stop_event |
| 热键监听 | Windows hook | _lock (状态切换) |
| 配置监视 | 2s 轮询热重载 | _stop_event |
| 流式定时器 | 中间识别 | generation token |
| Bridge | 后端→UI 信号 | QMetaObject.invokeMethod |

---

## 便携版打包

### 命令
```powershell
cd Aria
.\build_portable\release.bat
```

### 打包流程
1. 下载 Python 3.12.4 嵌入式 (缓存复用)
2. 创建 `dist_portable/Aria/_internal/` 结构
3. 配置 `python312._pth` (stdlib → site-packages → app → app\aria)
4. 复制源码到 `_internal/app/aria/`
5. **清理敏感数据** (用 template 替换用户配置, 日志/录音 → 删除)
6. 复制 `.venv/Lib/site-packages/` (~8.3GB)
7. 创建启动脚本 (Aria.cmd, Aria.vbs, Aria_debug.bat)
8. 编译 Aria.exe (PyInstaller, launcher_stub.py)
9. 验证 API key 已清理

### 输出
```
dist_portable/Aria/
├── Aria.exe           # 主入口 (双击启动)
├── Aria.cmd           # 命令行启动
├── Aria.vbs           # 静默启动
├── Aria_debug.bat     # 调试模式
├── CreateShortcut.cmd # 创建桌面快捷方式
├── aria.ico           # 图标
└── _internal/
    ├── python.exe / pythonw.exe / AriaRuntime.exe
    ├── python312.dll
    ├── python312._pth
    ├── app/aria/      # 源码
    └── Lib/site-packages/  # 依赖
```

---

## 配置热重载

config watcher 每 2 秒检查 `hotwords.json` 修改时间，变更时自动更新：
- Layer 2 替换规则
- Layer 2.5 拼音词表
- Layer 3 polisher 实例 (旧实例会被 close)
- 唤醒词
- VAD 参数

---

## 环境要求

| 项目 | 要求 |
|------|------|
| OS | Windows 10/11 64位 |
| Python | 3.12.4 (venv) |
| GPU | NVIDIA CUDA 12.x (推荐) |
| VRAM | 4GB+ |
| 关键依赖 | PySide6, torch, sounddevice, silero-vad, qwen-asr, funasr |

---

*最后更新: 2026-02-20*

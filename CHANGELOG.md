# Changelog

All notable changes to Aria will be documented in this file.

## [1.1.0] - 2025-12-27

### Added
- **翻译弹窗** - 选中文字说"什么意思"，直接显示翻译结果
- **AI 对话窗口** - 选中文字说"问AI"，发起上下文对话
- **PID 创建时间校验** - 防止 PID 复用导致的锁文件误判
- **安全锁文件删除** - WinError 32 重试机制
- **项目 Python 路径检测** - 子进程启动更可靠

### Changed
- 锁文件格式升级为 `PID:CREATION_TIME`
- 改进单例检查的 stale lock 处理
- 优化 splash 屏幕启动流程

### Fixed
- 修复进程崩溃后锁文件未清理的问题
- 修复 JSON 解析失败导致的 NameError
- 修复子进程使用错误 Python 环境的问题

## [1.0.0] - 2025-12-15

### Added
- **双 ASR 引擎** - FunASR (Paraformer-zh) 和 Whisper (faster-whisper)
- **三层热词纠错**
  - L1: initial_prompt 领域词汇引导
  - L2: 规则替换表
  - L2.5: 拼音模糊匹配
  - L3: API 润色 (OpenRouter)
- **选区指令系统** - 润色、翻译、扩写、缩写
- **唤醒词控制** - "瑶瑶" + 命令
- **Qt6 现代界面**
  - 浮动球 UI
  - 系统托盘
  - 设置面板
  - 启动画面
- **全局热键** - CapsLock / ` 键
- **单例检查** - Windows Named Mutex + 文件锁

### Technical
- Silero-VAD 语音活动检测
- CUDA 12.x GPU 加速
- 便携版构建 (build.py)

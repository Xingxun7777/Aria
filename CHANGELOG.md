# Changelog

All notable changes to Aria will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

*无*

## [1.1.2] - 2026-02-24

### Added
- **Qwen3-ASR 设为默认引擎** — 替代 FunASR 成为首选，支持多语言 + 上下文热词
- **流式字幕毛玻璃面板** — 录音中间结果以半透明毛玻璃面板显示，适配任意桌面背景
- **锁定隐藏字幕** — 锁定浮动球时自动隐藏流式字幕
- **三级防幻觉系统** — Pre-ASR 能量门控 + 三档能量泄漏检测 + retry-without-context
- **热词权重精细控制** — 每个热词可独立设置 0-1.0 权重，控制各纠错层参与度
- **开机自启动** — Registry HKCU\Run 方式，支持路径自修复

### Changed
- 输入模式简化为单一切换模式（按 `` ` `` 键开始/停止）
- 热词默认权重 1.0 → 0.5（减少误识别）
- Qwen3 context 阈值 0.3 → 0.5（排除低权重词进入 ASR 上下文）
- 自启动方式从 Startup .lnk → Registry HKCU\Run（更可靠）

### Fixed
- 修复浮动球动画在录音中因文字插入完成而意外缩小
- 修复流式字幕在 WA_TranslucentBackground 窗口下背景不渲染
- 修复 QGraphicsOpacityEffect 与透明窗口不兼容导致淡入淡出失效
- **[稳定性 R1]** 修复 14 处默认值链不一致（Qwen3 torch_dtype、polish_mode、hotkey 等）
- **[稳定性 R2]** 修复 SelectionProcessor 在本地润色模式下崩溃（类型守卫缺失）
- **[稳定性 R3]** 修复 main.py 4 处翻译/摘要/对话在本地润色模式下 AttributeError
- **[稳定性 R3]** 修复配置热重载 mtime 不同步导致双重重载

### Removed
- **移除 Whisper 引擎** — 不再维护，Qwen3-ASR 替代
- **移除 FireRedASR 引擎** — 不再维护
- 移除 pynput 依赖
- 移除 faster-whisper 依赖

## [1.1.1] - 2026-02-12

### Added
- **Qwen3-ASR 引擎** - 新增 Qwen/Qwen3-ASR-1.7B 支持 (`asr_engine: "qwen3"`)
- **Typewriter 模式** - 逐字符输入，兼容游戏和管理员权限应用
- **管理员提权检测** - 自动检测目标应用权限，提示以管理员运行
- **提权对话框** - 可选择"不再提醒"
- **配置热重载优化** - Polisher 资源正确回收

### Fixed
- 修复 `reload_config()` 旧 polisher 资源泄露
- 修复 `audio_capture.start()` 返回值未检查导致的静默失败
- 修复 `stop()` 缺少 `_stop_interim_timer()` 导致的残留回调
- 修复 JSON 配置写入非原子性（改用 tmp+fsync+os.replace）
- 修复 `_pipeline_log` 生产环境下无条件磁盘写入
- 修复 `hotwords.template.json` 缺少多个配置键
- 修复 VAD 默认参数不一致（统一运行时 fallback 为 0.2，与模板一致）

### Changed
- 构建系统: Python 嵌入版本 3.10.11 → 3.12.4 (匹配开发环境)
- 构建系统: 分发配置改用 template 而非用户配置
- 构建系统: 移除死代码目录 (features/, scheduler/)
- 清理 17+ 冗余启动脚本和旧文件
- 更新 .gitignore 覆盖 AI agent 和构建产物
- 重写 PROJECT.md 技术文档
- 重写 README.md 用户文档

### Removed
- 删除未使用模块: overlay.py, tray.py, mock_backend.py, model_download_dialog.py
- 删除未使用模块: core/model_manager.py, core/hotword/polish_prompt_backup.py
- 删除死代码: scheduler/ (未连接的任务队列), features/ (空目录)
- 删除冗余启动脚本: run.py, run_gui.pyw, Aria_env.bat, 等 10+ 文件

## [1.1.0] - 2025-12-27

### Added
- **翻译弹窗** - 选中文字说"什么意思"，浮窗显示翻译
- **AI 对话窗口** - 选中文字说"问AI"，发起上下文对话
- **PID 创建时间校验** - 防止 PID 复用导致的锁文件误判
- **安全锁文件删除** - WinError 32 重试机制
- **项目 Python 路径检测** - 子进程启动更可靠

### Changed
- 锁文件格式升级为 `PID:CREATION_TIME`
- 改进单例检查的 stale lock 处理
- 优化 splash 屏幕启动流程

### Fixed
- 修复进程崩溃后锁文件未清理
- 修复 JSON 解析失败导致的 NameError
- 修复子进程使用错误 Python 环境

## [1.0.0] - 2025-12-15

### Added
- **ASR 引擎** — FunASR (Paraformer-zh) + Whisper (faster-whisper)
- **四层热词纠错** - initial_prompt + 规则替换 + 拼音模糊 + AI 润色
- **选区指令系统** - 润色、翻译、扩写、缩写
- **唤醒词控制** - 语音命令
- **Qt6 现代界面** - 浮动球、系统托盘、设置面板、启动画面
- **全局热键** - CapsLock / `` ` `` 键
- **单例检查** - Windows Named Mutex + 文件锁
- **便携版构建** - 嵌入式 Python + 自动打包

### Technical
- Silero-VAD 语音活动检测
- CUDA 12.x GPU 加速
- QtBridge 线程安全信号

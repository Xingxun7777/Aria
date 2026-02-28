# Changelog

All notable changes to Aria will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

*无*

## [1.0.0] - 2026-02-28

首个公开发布版本。

### 语音识别
- **双引擎架构** — Qwen3-ASR（默认，多语言 + 上下文热词）和 FunASR（Paraformer-zh，轻量备选）
- **Silero-VAD 语音检测** — 自动区分语音与静默，带能量门控 fallback
- **三级防幻觉系统** — Pre-ASR 能量门控 + 三档能量泄漏检测 + retry-without-context
- **CUDA GPU 加速** — 自动检测显卡，不兼容时平滑回退 CPU

### 热词纠错
- **四层纠错管线** — initial_prompt → 正则替换 → 拼音模糊匹配 → AI 润色
- **热词权重系统** — 每个热词可独立设置 0-1.0 权重，精细控制各层参与度
- **AI 润色层** — 支持 OpenRouter 等 API，可选开启

### 语音控制
- **唤醒词指令** — 语音键盘命令（发送、删除、撤销、复制等）
- **模式切换** — 开启/关闭自动发送、休眠/唤醒
- **Typewriter 模式** — 逐字符输入，兼容游戏和管理员权限应用

### 选区功能
- **选区指令** — 润色、翻译、扩写、缩写选中文本
- **翻译弹窗** — 选中文字说"什么意思"，浮窗显示翻译
- **AI 对话窗口** — 选中文字说"问AI"，发起上下文对话

### 界面
- **浮动球** — 可拖动状态指示器，左键切换录音，右键菜单
- **流式字幕面板** — 毛玻璃半透明面板实时显示识别中间结果
- **系统托盘** — 后台运行，右键快捷操作
- **设置面板** — 图形化全功能配置（引擎、热词、快捷键、音频设备等）
- **启动画面** — 加载进度展示

### 系统集成
- **全局热键** — 默认 `` ` `` 键切换录音
- **开机自启动** — Registry HKCU\Run 方式，支持路径变更自修复
- **管理员提权检测** — 目标应用需高权限时自动提示
- **单例检查** — Windows Named Mutex + 文件锁 + PID 创建时间校验

### 构建与分发
- **便携版打包** — 嵌入式 Python，开箱即用，无需安装
- **桌面快捷方式生成器** — 一键创建快捷方式
- **配置热重载** — 修改配置文件后自动生效，无需重启

### 技术架构
- QtBridge 线程安全信号机制
- 原子性 JSON 写入（tmp + fsync + os.replace）
- ASR 队列有界（maxsize=5）+ 丢弃策略
- 配置五层 fallback 链（widget → config → app → template → dataclass）

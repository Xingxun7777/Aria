# VoiceType 发布审核清单

## 三方会谈审核结果 (2025-12-16)
审核人: Claude + Codex + Gemini

---

## P0 - 阻断性问题 (必须修复)

### 1. API 密钥泄露
- **位置**: `config/hotwords.json`, `config/hotwords.json.bak`
- **问题**: 包含真实 API 密钥
- **修复**: build.py 排除 `.bak` 文件，config 使用占位符模板

### 2. 用户隐私数据
- **位置**: `DebugLog/` 目录
- **问题**: 包含用户音频录音和转录文本
- **修复**: build.py 排除 `DebugLog/` 目录

### 3. 硬编码路径
- **位置**:
  - `launcher.py:136,175,176,232,245`
  - `ui/qt/splash_runner.py:16,17`
  - `core/asr/fireredasr_engine.py:36,58`
- **问题**: 包含 `G:\AIBOX` 绝对路径，其他机器无法运行
- **修复**: 使用相对路径或运行时检测

### 4. 日志文件泄露
- **位置**: `launch_error.log`, `splash_error.log`
- **问题**: 包含开发环境本地路径
- **修复**: build.py 排除 `*_error.log` 文件

---

## P1 - 高优先级问题

### 1. 调试系统默认启用
- **位置**: `core/debug_system.py`
- **问题**: 生产环境应默认关闭
- **修复**: 发布版本禁用调试模式

### 2. 静默大文件下载
- **问题**: 首次启动可能下载 1GB+ 模型文件无提示
- **修复**: 添加下载进度提示或预置模型

---

## P2 - 建议优化

### 1. 热键冲突
- **问题**: 某些热键可能与系统或其他软件冲突
- **建议**: 添加热键冲突检测

### 2. FireRedASR 路径
- **问题**: 硬编码外部依赖路径
- **建议**: 支持配置或自动检测

---

## Build.py 排除清单

```python
# 源代码复制时排除
ignore_patterns = [
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".git",
    "*.bak",           # 备份文件
    "DebugLog",        # 调试日志目录
    "*_error.log",     # 错误日志
    "*.log",           # 所有日志
    ".env",            # 环境变量
]

# 完全排除的目录
EXCLUDE_DIRS = ["DebugLog", "logs", ".git", "__pycache__"]

# 需要清理的配置文件
CONFIG_CLEAN = {
    "config/hotwords.json": ["api_key", "api_base"],
}
```

---

## 验证检查项

- [ ] 无 API 密钥在发布包中
- [ ] 无用户音频/转录数据
- [ ] 无绝对路径 `G:\AIBOX`
- [ ] 无 `.bak` 备份文件
- [ ] 无 `*_error.log` 日志文件
- [ ] pythonw.exe 来自官方 Python
- [ ] VirusTotal 检测 0/70+

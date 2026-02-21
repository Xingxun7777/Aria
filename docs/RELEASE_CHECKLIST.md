# Aria 发布审核清单

> 最后更新: 2026-02-20 (v1.1.2)

## 发布前检查

### 安全检查
- [x] API 密钥不在发布包中 (build.py 使用 template 替换)
- [x] 用户音频/转录数据已清理 (DebugLog/ 排除)
- [x] 无绝对路径泄露 (build.py 清理 `[A-Z]:\` 路径)
- [x] 无 .bak 备份文件 (EXCLUDE_PATTERNS 覆盖)
- [x] 无 .log 日志文件
- [x] audio_device 已清空 (自动检测)
- [x] hotwords/replacements 使用模板默认值

### 构建检查
- [x] Python 版本匹配 (build.py 3.12.4 = .venv 3.12.4)
- [x] Aria.exe 编译成功 (PyInstaller)
- [x] AriaRuntime.exe 存在
- [x] python312.dll 存在
- [x] site-packages 完整 (torch, PySide6, FunASR, numpy, sounddevice)
- [x] _pth 文件配置正确

### 功能检查
- [ ] Aria_debug.bat 能正常启动
- [ ] Aria.exe 能正常启动
- [ ] Qwen3-ASR 引擎正常识别
- [ ] 热键触发录音正常
- [ ] 浮动球显示正常
- [ ] 设置面板能打开
- [ ] 托盘图标正常

### 分发检查
- [ ] 7z 压缩包创建成功
- [ ] 压缩包可在干净系统解压运行
- [ ] VirusTotal 检测 Aria.exe (应 0 检出)

## Build 命令

```bash
build_portable\release.bat
```

## 压缩命令

```bash
7z a Aria-v1.1.2.7z dist_portable\Aria\
```

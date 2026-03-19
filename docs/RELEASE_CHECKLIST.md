# Aria 发布审核清单

> 最后更新: 2026-03-19 (v1.0.2)

## 发布前检查

### 安全检查
- [x] API 密钥不在发布包中 (build.py 使用 template 替换)
- [x] 用户音频/转录数据已清理 (DebugLog/ 排除)
- [x] InsightStore 数据已清理 (data/insights/)
- [x] HistoryStore 数据已清理 (data/history/)
- [x] OCR 调试日志已清理 (ocr_debug.log)
- [x] 无绝对路径泄露 (build.py 清理 `[A-Z]:\` 路径)
- [x] 无 .bak 备份文件 (EXCLUDE_PATTERNS 覆盖)
- [x] 无 .log 日志文件
- [x] audio_device 已清空 (自动检测)
- [x] hotwords/replacements 使用模板默认值 (replacements 为空)
- [x] wakeword 重置为 "小助手" (非开发者个人唤醒词)
- [x] commands.json prefix 重置为 "小助手"
- [x] reply_style 为空 (模板默认)
- [x] personalization_rules 为空 (模板默认)

### 构建检查
- [x] Python 版本匹配 (build.py 3.12.4 = .venv 3.12.4)
- [x] Aria.exe 编译成功 (PyInstaller)
- [x] AriaRuntime.exe 存在
- [x] python312.dll 存在
- [x] site-packages 完整 (torch, PySide6, FunASR, numpy, sounddevice, winocr)
- [x] _pth 文件配置正确
- [x] Smoke test 通过 (含 v1.0.2 新模块: ScreenOCR, HistoryStore, ReplyWorker)

### 功能检查
- [ ] Aria_debug.bat 能正常启动
- [ ] Aria.exe 能正常启动
- [ ] Qwen3-ASR 引擎正常识别
- [ ] 热键触发录音正常
- [ ] 浮动球显示正常
- [ ] 设置面板能打开 (含润色偏好、回复风格、噪声过滤、屏幕识别开关)
- [ ] 托盘图标正常
- [ ] 历史记录浏览器能打开
- [ ] 弹窗交互正常 (翻译/总结/回复 + 拖拽/固定/插入)
- [ ] 屏幕 OCR 正常触发 (检查 ocr_debug.log)

### 分发检查
- [ ] 7z 压缩包创建成功
- [ ] 压缩包可在干净系统解压运行
- [ ] VirusTotal 检测 Aria.exe (应 0 检出)

## Build 命令

```bash
build_portable\release-lite.bat
build_portable\release-full.bat
```

## 压缩命令

```bash
7z a Aria-v1.0.2-lite.7z dist_portable\Aria_release_lite\
7z a Aria-v1.0.2-full.7z dist_portable\Aria_release_full\
```

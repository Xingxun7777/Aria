# VoiceType v1.1-dev Session Log

## Session: 2025-12-23 15:30

### Completed
- [x] 修复翻译弹窗闪退问题 (pythonw.exe stdout=None 导致 print() 崩溃)
- [x] 修复重点记录功能 (InsightStore.add() 缺少 entry_type/attributes 参数)
- [x] 添加翻译弹窗关闭按钮 (右上角 ✕)
- [x] 全面审计并修复 main.py 中 40+ 个未保护的 print() 调用

### Key Changes
| File | Change |
|------|--------|
| `ui/qt/main.py` | 40+ print() → _log() 安全替换 |
| `ui/qt/translation_popup.py` | 添加关闭按钮 + 详细调试日志 |
| `core/insight_store.py` | add() 方法新增 entry_type, attributes 参数 |
| `core/wakeword/executor.py` | _save_highlight() 中 print → _debug |

### Key Decisions
- **调试策略**: 使用 [RAW] 原始文件写入追踪崩溃点，绕过可能有问题的日志函数
- **保留调试代码**: 用户要求不删除调试日志，便于后续不稳定时排查

### Technical Findings
1. **pythonw.exe 环境**: sys.stdout 为 None，任何 print() 调用都会导致崩溃
2. **崩溃定位方法**: 在关键函数入口添加直接文件写入 `[RAW]` 日志
3. **Qt showEvent 时序**: self.show() 触发 showEvent → _apply_win32_styles → 动画

### Pending Tasks
1. [x] 调查历史对话框复制失败问题 (框外点击复制不了) - 已修复
2. [x] 调查翻译弹窗复制失败问题 (经常性失败) - 已修复
3. [ ] 清理调试代码 (用户确认稳定后)
4. [ ] 验证复制功能修复效果

### Session: 2025-12-23 15:40

### Completed
- [x] 分析剪贴板复制失败问题
- [x] TranslationPopup: 添加直接剪贴板操作（不依赖信号槽）
- [x] HistoryWindow: 修复删除按钮点击导致意外复制的问题
- [x] HistoryWindow: 添加错误处理和重试逻辑

### Key Changes
| File | Change |
|------|--------|
| `ui/qt/translation_popup.py` | mousePressEvent 直接写剪贴板 + 重试逻辑 + event.accept() |
| `ui/qt/history.py` | _hlog 安全日志函数, _delete_pending 初始化, 空值检查, 重试逻辑 |

### Code Review Fixes (2025-12-23 15:50)
- [x] history.py: 添加 `_hlog()` pythonw.exe 安全日志函数
- [x] history.py: `_delete_pending` 在 `__init__` 中初始化
- [x] history.py: `_on_copy` 添加 null/empty 检查
- [x] history.py: 重试逻辑不再静默吞异常
- [x] history.py: 所有 `print()` 替换为 `_hlog()`
- [x] history.py: `mousePressEvent` 添加 `event.accept()`
- [x] translation_popup.py: 添加 whitespace 检查
- [x] translation_popup.py: 添加重试逻辑
- [x] translation_popup.py: 添加 `event.accept()`
- [x] 日志不再暴露完整文本，只记录长度

### Known Issues
- ~~历史对话框: 点击复制经常失败~~ → 已修复 (2025-12-23)
- ~~翻译弹窗复制: 复制功能不稳定~~ → 已修复 (2025-12-23)
- 待验证: 需要用户测试确认修复效果

---

## Architecture Notes

### Translation Popup Flow
```
Wakeword Detected → executor._selection_process()
    → bridge.emit_action(SHOW_TRANSLATION)
    → main.py on_action_triggered()
    → translation_popup.show_loading()
    → TranslationWorker (thread pool)
    → on_translation_finished()
    → translation_popup.show_result()
```

### Highlight Save Flow
```
Wakeword "遥遥记一下" + content
    → detector captures following_text
    → executor._save_highlight()
    → InsightStore.add(entry_type="highlight", attributes={...})
    → bridge.emit_highlight_saved()
    → floating_ball.on_highlight_saved() (gold flash)
```

# VoiceType Debug Lessons

记录调试过程中的经验教训，避免重复踩坑。

---

## Issue: pythonw.exe 环境下 print() 导致闪退

**Date**: 2025-12-23

**Symptom**:
- 翻译功能触发后程序立即闪退
- 日志停在 `[MAIN] Calling show_loading` 之后
- 无任何错误信息

**Root Cause**:
- Windows 下使用 `pythonw.exe` 运行时，`sys.stdout` 和 `sys.stderr` 为 `None`
- 任何 `print()` 调用都会导致 `AttributeError` 或直接崩溃
- 崩溃发生在 Qt 事件循环内部，无法被常规 try/except 捕获

**Solution**:
1. 创建安全的日志函数：
```python
def _log(msg: str):
    if sys.stdout is not None:
        print(msg)
    # 同时写入文件日志
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")
```

2. 全局替换所有 `print()` 为 `_log()`

**Key Learning**:
- Windows GUI 程序（.pyw 或 pythonw.exe）无控制台输出
- **必须**在所有 print() 前检查 `sys.stdout is not None`
- 推荐使用文件日志作为主要调试手段

---

## Issue: 调试崩溃点定位技巧

**Date**: 2025-12-23

**Symptom**:
- 程序崩溃但不知道具体在哪一行
- 日志函数本身可能有问题，无法信任

**Root Cause**:
- 崩溃发生在日志写入完成之前
- 或日志函数内部有未捕获的异常

**Solution**:
使用"原始调试"方法 - 直接写文件，绕过所有封装：

```python
# 在可疑代码前添加
try:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[RAW] Checkpoint 1: {variable}\n")
except Exception:
    pass

# 可疑代码
suspicious_function()

# 可疑代码后
try:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[RAW] Checkpoint 2: success\n")
except Exception:
    pass
```

**Key Learning**:
- 当常规日志失效时，用最原始的文件写入
- `[RAW]` 前缀便于在日志中识别调试点
- try/except 包裹避免调试代码本身崩溃

---

## Issue: InsightStore.add() 参数不匹配

**Date**: 2025-12-23

**Symptom**:
```
InsightStore.add() got an unexpected keyword argument 'entry_type'
```

**Root Cause**:
- `_save_highlight()` 调用了 `add(entry_type=..., attributes=...)`
- 但 `InsightStore.add()` 原签名不支持这些参数

**Solution**:
扩展 `InsightStore.add()` 签名：

```python
def add(
    self,
    text: str,
    timestamp: str,
    duration_s: float = 0.0,
    session_id: int = 0,
    entry_type: str = "transcription",  # 新增
    attributes: dict = None,             # 新增
) -> bool:
```

**Key Learning**:
- 新增功能前先检查被调用方法的签名
- 使用默认参数保持向后兼容

---

## Quick Reference

### pythonw.exe 安全 print
```python
if sys.stdout is not None:
    print(msg)
```

### 崩溃点定位
```python
with open("debug.log", "a") as f:
    f.write(f"[RAW] checkpoint\n")
```

### 日志文件位置
- Wakeword/Main: `DebugLog/wakeword_debug.log`
- Insights: `data/insights/YYYY-MM.json`

---

## Issue: Qt 非激活窗口剪贴板操作不可靠

**Date**: 2025-12-23

**Symptom**:
- TranslationPopup 点击复制经常失败
- HistoryWindow 复制有时不工作

**Root Cause**:
1. **TranslationPopup**: 使用 `Qt.WindowDoesNotAcceptFocus` + Win32 `WS_EX_NOACTIVATE` 样式
   - 信号/槽模式 (`copyRequested.emit()`) 增加了间接层
   - 非激活窗口的事件处理在 Windows 上可能不可靠

2. **HistoryWindow**:
   - 删除按钮的 `clicked` 信号触发后，事件继续冒泡到父级 `mousePressEvent`
   - 可能导致意外触发复制操作

**Solution**:

1. **TranslationPopup** - 直接在 mousePressEvent 中写剪贴板：
```python
def mousePressEvent(self, event):
    if event.button() == Qt.LeftButton:
        if self._translated_text:
            # 直接写剪贴板，不依赖信号槽
            clipboard = QApplication.clipboard()
            clipboard.setText(self._translated_text)
            # 同时发射信号供其他处理器使用
            self.copyRequested.emit(self._translated_text)
        self.dismiss()
```

2. **HistoryWindow** - 防止删除按钮事件冒泡：
```python
def _on_delete_clicked(self):
    self._delete_pending = True  # 标记删除操作
    self.deleteClicked.emit(self.index)

def mousePressEvent(self, event):
    if getattr(self, '_delete_pending', False):
        self._delete_pending = False
        return  # 跳过复制
    # ... 正常复制逻辑
```

**Key Learning**:
- 非激活窗口 (`WindowDoesNotAcceptFocus`) 的剪贴板操作应直接执行，不要依赖信号槽
- Qt 按钮点击事件会冒泡到父级，需要手动阻止
- 剪贴板操作应添加 try/except 和重试逻辑

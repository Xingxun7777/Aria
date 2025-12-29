# Aria v1.1 调试会话归档

**日期**: 2024-12-19
**分支**: dev/v1.1-voice-commands
**提交**: 571d420

---

## 问题概述

Aria 在 pythonw.exe 下运行时出现多个致命问题：
1. 热键按下后无任何反应（波形动画正常但无文字输出）
2. "翻译成日文"等翻译命令导致程序闪退

---

## 问题1：热键注册被意外清除

### 症状
- 热键回调日志显示 `Registered bindings: []`
- 热键从未被注册

### 根因分析
**文件**: `ui/qt/main.py` - `_ensure_active_state()` 函数

```python
# 问题代码
ball._popup_menu.toggle.setChecked(False)
ball._popup_menu.toggle.setChecked(True)  # 触发 toggled 信号
```

这个 False→True 切换会触发 `toggled` 信号，导致：
1. `setChecked(False)` → 调用 `stop()` → 清除所有热键绑定
2. `setChecked(True)` → 调用 `start()` → 但 start() 不会重新注册热键！

### 修复方案
```python
# 修复后
ball._popup_menu.toggle.blockSignals(True)   # 阻止信号发射
ball._popup_menu.toggle.setChecked(True)
ball._popup_menu.toggle.blockSignals(False)  # 恢复信号
```

### 经验教训
> **Qt 信号陷阱**: 在代码中修改 UI 状态时，必须考虑是否会触发信号。
> 使用 `blockSignals(True/False)` 来安全地更新 UI 而不触发副作用。

---

## 问题2：FunASR 在 pythonw.exe 下崩溃

### 症状
- 热键工作后，ASR 识别无输出
- 错误：`'NoneType' object has no attribute 'write'`

### 根因分析
**文件**: `core/asr/funasr_engine.py`

pythonw.exe 是无控制台的 Python 解释器：
```python
import sys
print(sys.stdout)  # 在 pythonw.exe 下输出: None
```

FunASR 的 `generate()` 方法内部有 print 语句，当 `sys.stdout` 为 None 时崩溃。

### 修复方案
```python
import sys
import io

old_stdout = sys.stdout
old_stderr = sys.stderr

# 创建临时 dummy 流
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

try:
    result = self._model.generate(**gen_kwargs)
finally:
    # 恢复原始流
    sys.stdout = old_stdout
    sys.stderr = old_stderr
```

### 经验教训
> **pythonw.exe 兼容性**: 任何可能在 pythonw.exe 下运行的代码，
> 都必须假设 `sys.stdout/stderr` 可能为 None。
> 第三方库的内部 print 也需要防护。

---

## 问题3：TranslationWorker 崩溃

### 症状
- 说"翻译成日文"后程序闪退
- 日志显示 `TranslationWorker started` 后无任何输出

### 根因分析
**文件**: `ui/qt/workers/translation_worker.py:186-189`

```python
print(
    f"[TranslationWorker] Completed in {elapsed:.2f}s: "
    f"{self.source_text[:30]}... -> {translated[:30]}..."
)
```

同样的 pythonw.exe stdout=None 问题。

### 修复方案
```python
import sys
if sys.stdout is not None:
    print(...)
```

---

## 调试基础设施

为解决 pythonw.exe 无控制台的问题，建立了文件日志系统：

### 日志文件位置
```
aria-v1.1-dev/DebugLog/
├── funasr_debug.log      # FunASR 引擎日志
├── hotkey_debug.log      # 热键注册/触发日志
├── pipeline_debug.log    # 完整流水线日志
└── wakeword_debug.log    # 唤醒词检测日志
```

### 日志函数模板
```python
from pathlib import Path
import datetime

_LOG_FILE = Path(__file__).parent.parent / "DebugLog" / "xxx_debug.log"

def _log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
```

---

## pythonw.exe 兼容性检查清单

在使用 pythonw.exe 运行的代码中，检查：

- [ ] 所有 `print()` 语句是否有 `if sys.stdout is not None` 保护
- [ ] 第三方库调用是否可能内部 print（需要临时重定向）
- [ ] logging 配置是否依赖 StreamHandler（需要 FileHandler 替代）
- [ ] 任何写入 sys.stdout/stderr 的操作

---

## 修改文件清单

| 文件 | 修改内容 |
|------|----------|
| `ui/qt/main.py` | blockSignals 防止 toggle 触发 stop() |
| `core/asr/funasr_engine.py` | stdout/stderr 临时重定向 |
| `ui/qt/workers/translation_worker.py` | print() 保护 |
| `system/hotkey.py` | 详细调试日志 |
| `app.py` | 流水线日志 |

---

## 后续建议

1. **全局 print 审计**: 搜索所有 `print(` 并评估 pythonw 兼容性
2. **统一日志系统**: 考虑用 FileHandler 替代所有 StreamHandler
3. **自动化测试**: 添加 pythonw.exe 下的启动测试

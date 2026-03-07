# Aria Debug Lessons

记录调试过程中的经验教训，避免重复踩坑。

---

## Issue: 便携版放在中文路径时，Qwen3-ASR 启动即崩（nagisa / dyNET Unicode 路径问题）

**Date**: 2026-03-07

**Symptom**:
- 便携版放在 `E:\语音\Aria\...` 之类的中文目录下时，启动弹窗报错：
  `Could not read model from ...nagisa/data/nagisa_v001.model`
- 英文路径正常，中文路径失败
- 错误在应用真正启动前就发生，导致用户感觉是“便携版打不开”

**Root Cause**:
1. `launcher.py` / `qwen3_engine.py` / `settings.py` 会触发 `qwen_asr` 导入
2. `qwen_asr` 顶层又导入 `qwen3_forced_aligner`
3. `qwen3_forced_aligner` 顶层 `import nagisa`
4. `nagisa/__init__.py` 顶层立刻 `Tagger()`
5. `Tagger()` 最终走到 `dyNET ParameterCollection.populate(params)`
6. dyNET 读取 `nagisa_v001.model` 时**不兼容 Windows 非 ASCII 路径**

**关键坑点**:
- 不是“漏打包模型文件”，而是**文件存在但 native 库读不了 Unicode 路径**
- Windows `Path.resolve()` 会把 Unicode junction 折叠回原始 ASCII 路径，**会掩盖问题**
- 只在英文路径做 smoke test 会误判“可交付”

**Solution**:
1. 新增 `core/utils/import_workarounds.py`
2. 在导入 `qwen_asr` 前，检测 `nagisa` 是否位于非 ASCII 路径
3. 若是，则把 `nagisa/` 和 `nagisa_utils*.pyd` 镜像到 ASCII-only 缓存目录，并把该目录 prepend 到 `sys.path`
4. 用 `check_qwen3_installation()` 统一导入入口，避免各处直接 `import qwen_asr`
5. 在 `build_portable/build.py` 中增加 **Unicode 路径探针**（中文 junction + embedded python）

**Verification**:
- ASCII 路径：`nagisa` 正常导入
- Unicode 路径：旧版本稳定复现 `Could not read model...`
- 修复后：`build_portable/build.py` 的 smoke test + Unicode probe 均通过

**Key Learning**:
- Windows 便携版验证必须包含 **中文路径 / 非 ASCII 路径**
- 遇到 native 库读模型失败，先区分“缺文件”还是“Unicode 路径不兼容”
- `importlib`/`Path.resolve()` 可能把真实运行路径“洗白”，排查时要保留原始 import 路径

---

## Issue: 第一句话必出问题（重复/截断）- Prompt Shock

**Date**: 2025-12-28

**Symptom**:
- 启动后第一句话总是有问题
- Whisper 返回重复句子：`"测试。测试。"` 而不是 `"测试。"`
- 幻觉检测触发，回退到不完整的 interim 结果 → 截断
- 后续句子正常，只有第一句有问题

**Root Cause** (来自 Gemini 三方会谈分析):
- GPU warmup 在 `initial_prompt` 设置**之前**运行
- 第一次真正识别是模型首次看到 hotword prompt
- 同时处理 prompt KV cache + 音频编码 → 注意力机制"循环" → 重复输出
- 这被称为 **"Prompt Shock"** - warmup 状态和 production 状态不一致

**原代码问题**:
```python
# 错误顺序！
warmup_audio = np.random.randn(8000)  # 随机噪声也有问题
_ = self.asr_engine.transcribe(warmup_audio)  # 此时 initial_prompt 还未设置！
# ... 后面才设置 initial_prompt
```

**Solution**:
1. 将 warmup 移到 `initial_prompt` 设置**之后**
2. 用静音（zeros）替代随机噪声（Whisper 没见过白噪声）
3. 延长到 1 秒（16000 samples）

```python
# 正确顺序：先设置 initial_prompt，再 warmup
self.asr_engine.set_initial_prompt(initial_prompt)
# ...
# GPU Warmup - MUST run AFTER initial_prompt is set
warmup_audio = np.zeros(16000, dtype=np.float32)  # 静音，不是噪声
_ = self.asr_engine.transcribe(warmup_audio)  # 现在有 initial_prompt 了
```

**Key Learning**:
- Warmup 必须模拟真实推理环境（包括 prompt、VAD 设置等）
- 白噪声可能扰乱 LayerNorm 统计，用静音更安全
- 第一句话问题通常是初始化顺序问题，检查 warmup 时机

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

---

## Issue: Whisper 冷启动幻觉导致第一句话丢失

**Date**: 2025-12-28

**Symptom**:
- 程序空闲一段时间后，用户说的第一句话不被识别
- 第二句话开始才能正常工作
- 日志显示 `[ASR] Filtered hallucination: '重复的句子...'`

**Root Cause**:
1. GPU 长时间空闲后，CUDA 内核需要"预热"
2. Whisper 第一次推理容易产生幻觉（典型特征：重复句子）
3. 幻觉过滤器 `_is_hallucination()` 的 Pattern 5（重复句子检测）正确识别并过滤
4. 但用户的真实语音也被一起丢弃了

**日志示例**:
```
[INTERIM] 以及能不能发育工具的设计  ← 实时识别正确
[ASR] raw: '以及能不能防御攻击者的恶意攻击。 以及能不能防御攻击者的恶意攻击。' ← 最终结果是幻觉
[ASR] Filtered hallucination: '...'
[WARN] No speech recognized  ← 用户语音丢失
```

**Solution**:
在 `app.py` 的 ASR worker 中实现双重恢复机制：

1. **重试机制**：检测到幻觉时，立即重试一次转录（GPU 已预热，第二次通常正常）
2. **Interim 回退**：如果重试仍失败，使用之前的 interim 结果

```python
if self._is_hallucination(text):
    print(f"[ASR] Detected hallucination: '{text}'")

    # Strategy 1: Retry transcription
    retry_result = self.asr_engine.transcribe(audio)
    retry_text = retry_result.text.strip()

    if retry_text and not self._is_hallucination(retry_text):
        text = retry_text  # 重试成功
    else:
        # Strategy 2: Fallback to interim
        if self._last_interim_text and not self._is_hallucination(self._last_interim_text):
            text = self._last_interim_text  # 使用 interim
        else:
            text = ""  # 两种策略都失败
```

**Key Learning**:
- Whisper 空闲后第一次推理容易幻觉，这是 CUDA/GPU warmup 问题
- Interim 结果（实时识别）通常比最终结果更稳定
- 遇到幻觉时重试一次是低成本高回报的策略
- 幻觉典型特征：相同句子重复 2+ 次

---

## Issue: 会话黏连 - 语音累积不输出 (Sticky Session)

**Date**: 2025-12-28

**Symptom**:
- 悬浮窗显示 interim 文字，但不插入到目标应用
- 只有开始说下一句话时，上一句才突然输出
- 日志显示超长音频段：258048 samples（16秒！正常应该 2-5 秒）
- ASR 处理时间极长（46秒处理 16秒音频）

**Root Cause** (来自三方会谈分析):
1. **VAD 阈值过低 (0.2)**：
   - Silero-VAD 返回 0-1 的语音概率
   - 0.2 阈值太敏感，环境噪声/呼吸都会被识别为语音
   - 导致 silence_samples 永远达不到 min_silence_samples
   - speech_end 事件永不触发

2. **max_speech_ms 未被正确应用**：
   - 默认 15 秒限制应该触发强制分割
   - 但 app.py 没有传递 max_speech_ms 给 VADConfig

**日志证据**:
```
[17:36:59.359] Got audio: 258048 samples (16.1秒)
[17:37:45.360] Transcription done (46秒处理时间)
```

**Solution**:
1. 提高 VAD 阈值：0.2 → 0.35（减少误报）
2. 降低最大语音时长：15s → 8s（作为安全网）
3. 缩短静音检测时间：600ms → 500ms（更快响应停顿）
4. 在 app.py 中读取并传递 max_speech_ms 配置

```python
# hotwords.json
"vad": {
    "threshold": 0.35,      # 原来 0.2
    "min_silence_ms": 500,  # 原来 600
    "max_speech_ms": 8000   # 新增，8秒强制分割
}

# app.py - 读取并传递 max_speech_ms
vad_max_speech = max(3000, min(30000, vad_cfg.get("max_speech_ms", 8000)))
VADConfig(
    threshold=vad_threshold,
    min_speech_ms=vad_min_speech,
    min_silence_ms=vad_min_silence,
    max_speech_ms=vad_max_speech,  # 新增
)
```

**Key Learning**:
- VAD 阈值对实时语音识别至关重要：太低→累积，太高→截断
- 推荐阈值范围：0.3-0.5（根据环境噪声调整）
- 必须有 max_speech 安全网，防止无限累积
- 超长音频会导致 Whisper 处理时间指数增长
- 添加日志打印 VAD 配置，方便排查配置未生效问题

---

## Issue: Whisper 无限循环幻觉（处理时间暴增）

**Date**: 2025-12-28

**Symptom**:
- 正常音频（2-4秒）处理时间突然从 1-2 秒暴增到 18-50 秒
- 输出是重复字符："嘟嘟嘟嘟嘟..."、"哒哒哒哒..."
- 或完全乱码的输出
- 导致后续音频在队列中排队，恢复后"一下输出一大段"

**Root Cause**:
- Whisper 模型偶尔陷入 **无限序列生成（infinite sequence generation）**
- 特别是在音频质量差、背景噪声、或 GPU 压力大时
- 模型不断生成重复 token，直到达到最大长度限制

**日志证据**:
```
samples=67072 (4.2秒) → 18661ms (18秒处理！)
samples=31232 (1.9秒) → 31432ms (31秒处理！)
结果: "嘟嘟嘟嘟嘟嘟嘟嘟嘟嘟嘟嘟..."
```

**Solution**:
在 `whisper_engine.py` 的 `transcribe()` 方法中添加防护参数：

```python
transcribe_kwargs = {
    # ... 原有参数 ...
    # Anti-infinite-loop parameters
    "compression_ratio_threshold": 2.4,  # 检测重复输出
    "log_prob_threshold": -1.0,          # 过滤低置信度
    "no_speech_threshold": 0.6,          # 跳过静音段
    # 注意：不要使用 max_new_tokens！会和 initial_prompt 冲突
}
```

**Key Learning**:
- Whisper 的 decoder 可能陷入重复生成循环
- `compression_ratio_threshold` 是检测 "嘟嘟嘟..." 这类重复的关键
- **不要使用 `max_new_tokens`**：Whisper max_length=448，initial_prompt 可能占用 196 tokens，设置 max_new_tokens 会导致超限报错

---

## Issue: Claude Code 编辑 hotwords.json 时崩溃

**Date**: 2025-12-28

**Symptom**:
- 使用 Claude Code 的 Edit 工具编辑 `config/hotwords.json` 时，整个 Claude Code 崩溃
- Rust panic 错误：`byte index X is not a char boundary; it is inside '点' (bytes X..Y)`
- 只有编辑这个包含大量中文的 JSON 文件时发生

**Root Cause**:
- Claude Code 的 Rust 实现在处理多字节 UTF-8 字符（中文）时有 bug
- Edit 工具在计算字符串边界时，使用字节索引而非字符索引
- 中文字符占 3 字节（UTF-8），索引计算错误会落在字符中间
- 这是 Claude Code 本身的 bug，无法在用户代码中修复

**Workaround**:
**永远不要用 Edit 工具编辑 hotwords.json！使用 Python 代替：**

```python
# 安全的 hotwords.json 编辑方法
import json

with open('config/hotwords.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

# 修改配置...
config['some_key'] = 'new_value'

with open('config/hotwords.json', 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
```

**Key Learning**:
- Claude Code Edit 工具对包含大量中文的 JSON 文件不安全
- 遇到中文密集的配置文件，优先使用 Python/Bash 间接编辑
- 这个问题已报告给 Anthropic，等待官方修复
- 在 CLAUDE.md 中添加规则提醒未来会话避免此操作

---

## Issue: Whisper 中文 YouTube 字幕幻觉（训练数据污染）

**Date**: 2025-12-29

**Symptom**:
- Whisper 输出与用户语音完全无关的固定文本
- 典型幻觉：`请不吝点赞 订阅 转发 打赏支持明镜与点点栏目`
- 其他变体：`字幕志愿者 杨茜茜优优独播剧场`、`欢迎订阅我的频道`
- 还有怪字符：`．﹏﹏﹏﹏`（全角句号+波浪线）
- 通常在音频质量差或背景噪声时触发

**Root Cause**:
- **Whisper 训练数据污染**：模型训练数据大量来自 YouTube 视频字幕
- 中文 YouTube 视频常见片尾语被模型"记住"
- 当音频不清晰时，模型倾向于输出这些高频训练模式
- 这是 OpenAI 官方已知问题：https://huggingface.co/openai/whisper-large-v3/discussions/165

**日志证据**:
```
[14:31:19.856] Transcription done: '请不吝点赞 订阅 转发 打赏支持明镜与点点栏目' (2202ms)
[14:31:26.852] Transcription done: '请不吝点赞 订阅 转发 打赏支持明镜与点点栏目' (2200ms)
```

**Solution**:
在 `whisper_engine.py` 添加幻觉黑名单过滤器：

```python
# Known Whisper hallucination patterns (Chinese YouTube subtitle contamination)
HALLUCINATION_PATTERNS = [
    r"请不吝点赞.*?订阅.*?转发.*?打赏",
    r"明镜与点点栏目",
    r"字幕志愿者.*",
    r"[．。][﹏~～]{2,}",  # 全角波浪符
]

_hallucination_regex = re.compile("|".join(HALLUCINATION_PATTERNS), re.IGNORECASE)

def is_hallucination(text: str) -> bool:
    return bool(_hallucination_regex.search(text))

# 在 transcribe() 中：
if is_hallucination(full_text):
    logger.warning(f"Hallucination detected and filtered: '{full_text[:50]}...'")
    return ASRResult(text="", ...)  # 返回空结果
```

**Key Learning**:
- Whisper 中文幻觉是训练数据问题，无法通过参数调优解决
- 必须用黑名单正则表达式后处理过滤
- 常见幻觉模式都是 YouTube 视频的片尾语/字幕署名
- 幻觉通常在音频质量差时触发，也可能是 VAD 阈值问题（噪声被当成语音）

---

## Issue: 启动时出现双 Python 进程（Aria Dev + base Python）

**Date**: 2025-12-31  
**Author**: Codex (GPT-5)

**Symptom**:
- 启动后任务管理器显示两个进程（AriaDevRuntime.exe + Python）
- Python 进程路径指向 `G:\AIBOX\Python310\tools\pythonw.exe`
- 两个进程同时启动，父进程退出后子进程同步结束

**Root Cause**:
- venv 的 `pyvenv.cfg` 指向 NuGet/嵌入式 Python（`home = G:\AIBOX\Python310\tools`）
- 该启动器实现会在启动时再拉起 base Python
- venv 的 `python.exe/pythonw.exe` 是启动器而非解释器本体，导致双进程

**Solution**:
1. 备份 venv 解释器：`python.exe.bak` / `pythonw.exe.bak`
2. 用 base Python 替换 venv 解释器，避免二次拉起
3. 重新生成 `AriaDevRuntime.exe` 与桌面快捷方式

```powershell
Copy-Item G:\AIBOX\Python310\tools\python.exe  G:\AIBOX\voicetype-v1.1-dev\.venv\Scripts\python.exe
Copy-Item G:\AIBOX\Python310\tools\pythonw.exe G:\AIBOX\voicetype-v1.1-dev\.venv\Scripts\pythonw.exe
```

**Key Learning**:
- venv home 指向 NuGet/嵌入式 Python 时，可能出现"启动器再拉起 base Python"的双进程
- 任务管理器看到 base Python 路径时，优先检查 `pyvenv.cfg` 的 home
- 用 base 解释器替换 venv 解释器可恢复单进程行为

---

## Issue: LLM 润色层过度替换（热词过拟合）

**Date**: 2026-01-02
**Author**: Claude + Codex + Gemini (三方会谈)

**Symptom**:
- ASR 识别正确，但 LLM 润色后变成完全无关的热词
- 典型错误：
  - `OPUS四点五` → `ComfyUI四点五`
  - `米薯` → `ComfyUI`
  - `tram上` → `GitHub上`
  - `鬼车站` → `GitHub上面`
- 英文被强制替换为中文热词
- 语义完全不同的词被替换

**Root Cause** (三方会谈共识):
1. **Prompt 权限过度**：原 prompt 包含"专业术语谐音必须修复"，给 LLM 过度权限
2. **热词权重形同虚设**：所有词默认权重 1.0，没有真正的分层
3. **热词传给 LLM 导致过拟合**：LLM 视热词表为"必须使用的词"，激进替换

**日志证据**:
```
[ASR] raw: 'OPUS四点五'
[POLISH] output: 'ComfyUI四点五'  ← 完全无关替换
```

**Solution**:
**三层防护策略：权重分层 + LLM 隔离 + 保守替换**

1. **设计权重分层系统**（控制热词在各层的作用）：

| 权重 | Layer 1 (ASR) | Layer 2 (正则) | Layer 2.5 (拼音) |
|------|---------------|----------------|------------------|
| 0    | ❌ | ❌ | ❌ |
| 0.3  | ✅ score=20 (hint) | ❌ | ❌ |
| 0.5  | ✅ score=50 (standard) | ✅ | ❌ |
| 1.0  | ✅ score=80 (lock) | ✅ | ✅ |

2. **LLM 完全不接收热词列表**：
   - 移除 prompt 中的 `{hotwords}` 占位符
   - LLM 只负责：同音字纠错 + 标点修正
   - 热词替换交给 Layer 1 (ASR) 和 Layer 2 (正则)

3. **实现代码**：

```python
# manager.py - 新增方法
def get_hotwords_by_layer(self) -> Dict[str, List[str]]:
    """根据权重返回各层热词"""
    weights = self._load_weights()
    layer1_asr = [w for w in words if weights.get(w, 0.5) >= 0.3]
    layer2_regex = [w for w in words if weights.get(w, 0.5) >= 0.5]
    layer2_5_pinyin = [w for w in words if weights.get(w, 0.5) >= 1.0]
    return {"layer1_asr": layer1_asr, "layer2_regex": layer2_regex, "layer2_5_pinyin": layer2_5_pinyin}

def get_asr_hotwords_with_score(self) -> List[tuple]:
    """返回 FunASR 格式的 (word, score) 列表"""
    # 0.3 → score=20, 0.5 → score=50, 1.0 → score=80
    ...

# funasr_engine.py - 新增方法
def set_hotwords_with_score(self, hotwords_with_score: List[tuple]) -> None:
    """设置带分数的热词，格式：'word score\\n...'"""
    self.config.hotwords = [f"{word} {score}" for word, score in hotwords_with_score]
```

**Key Learning**:
- **LLM + 热词 = 过拟合**：LLM 会把热词当成"必须出现的词"，疯狂替换
- **分层是关键**：让不同置信度的热词在不同层生效
- **ASR 原生 hotword 更安全**：FunASR 的 hotword 是解码时的偏置，不会强制替换
- **保守策略**：默认权重 0.5，只有明确需要的词才设为 1.0
- **拼音匹配最危险**：只对 weight=1.0 的词开启，否则容易误匹配

---

## Issue: 删除文件后残留 import 导致启动崩溃
**Date**: 2026-02-12
**Symptom**: Aria 启动时立即 ImportError 崩溃，完全无法运行
**Root Cause**: 在项目清理时删除了 `mock_backend.py`, `overlay.py`, `tray.py`，但 `ui/qt/__init__.py` 仍然 `from .mock_backend import MockBackend` 等。`main.py` 也有 `from .mock_backend import MockBackend` 在 demo 模式和 error fallback 中。
**Solution**:
1. 从 `ui/qt/__init__.py` 移除 3 个死导入及其 `__all__` 条目
2. 从 `main.py` 移除 `--demo` 参数和整个 demo 模式分支
3. 将 MockBackend fallback 改为 `sys.exit(1)` + 中文错误提示
**Key Learning**:
- **删除文件后必须全项目 grep `from .{filename}`** — 不能只删文件不清 import
- 命令: `Grep "from.*mock_backend|from.*overlay|from.*tray" --glob "*.py"`
- `__init__.py` 的导入尤其容易遗漏，因为它不直接使用这些类

---

## Issue: launcher.py 裸 print() 在便携版崩溃
**Date**: 2026-02-12
**Symptom**: 便携版 (AriaRuntime.exe = pythonw.exe) 启动时随机崩溃，无任何错误信息
**Root Cause**: launcher.py 有 20+ 处裸 `print()` 调用。AriaRuntime.exe 是 pythonw.exe 的副本，sys.stdout 为 None，任何 print() 都会 `AttributeError: 'NoneType' has no attribute 'write'`
**Solution**: 在 launcher.py imports 之后添加全局 stdout/stderr null 保护：
```python
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
```
**Key Learning**:
- 这比逐个替换 print()→log() 更安全，不会遗漏
- 只影响 pythonw.exe 环境，python.exe (debug mode) 不受影响
- 之前已在 main.py、vad.py 修复过相同问题，但 launcher.py 遗漏了
- **教训**: 新增 print() 前搜索 "pythonw" 或 "stdout" 确认环境安全

---

## Issue: 模板配置键名不匹配导致 FunASR 默认值回退
**Date**: 2026-02-12
**Symptom**: hotwords.template.json 中设置的 FunASR 完整模型路径不生效
**Root Cause**: 模板用 `"model": "iic/speech_seaco_paraformer..."` 但代码读 `funasr_cfg.get("model_name", "paraformer-zh")`。键名不匹配，代码总是用默认值 "paraformer-zh"。
**Solution**: 模板改为 `"model_name": "paraformer-zh"` 匹配代码读取的键名
**Key Learning**:
- **配置模板的键名必须和代码读取的键名完全一致** — 拼错不报错，只是静默用默认值
- 排查方法: Grep 代码中 `funasr_cfg.get` 或 `config.get` 看实际读取的键名

---

## Issue: WA_TranslucentBackground 窗口 stylesheet background-color 不渲染
**Date**: 2026-02-20
**Symptom**: 独立顶层 QLabel 窗口设置了 `WA_TranslucentBackground` + stylesheet `background-color: rgba(20,20,25,160)`，在深色桌面背景上文字看起来正常（白色文字直接浮在深色桌面上），但在白色桌面上文字完全不可见 — 深色底板根本没有被渲染。
**Root Cause**: Windows 上 `WA_TranslucentBackground` 可能导致 QLabel 的 stylesheet `background-color` 不被绘制。此外，`QGraphicsOpacityEffect` 与 `WA_TranslucentBackground` 组合时更严重 — 中间 pixmap 渲染丢失背景。
**Solution**: 子类化 QLabel，在 `paintEvent` 中用 QPainter 手动绘制圆角矩形背景（`drawRoundedRect`），然后调用 `super().paintEvent()` 让 QLabel 绘制文字。QPainter 直接绘制 100% 可靠，不受 stylesheet 渲染管线影响。
**Key Learning**:
- **WA_TranslucentBackground + stylesheet background = 不可靠**（Windows 上）
- 任何需要背景的 translucent 窗口，都应该用 QPainter 手动画背景
- QGraphicsOpacityEffect 与 translucent 窗口不兼容，用 `setWindowOpacity()` 替代
- 排查方法: 如果文字在深色背景可见但白色背景不可见 → 说明背景没渲染，文字直接浮在桌面上

---

## Issue: 小字号文字描边（subtitle outline）在 13px 以下非常丑陋
**Date**: 2026-02-20
**Symptom**: 尝试用字幕式 8 方向 1px 描边让 13px 流式文字在任何背景上可读，在白色背景下文字边缘出现 muddy halo，CJK 字符尤为严重。
**Root Cause**: 字幕描边技术（QPainter 多 pass drawText + 偏移）适用于大字号（24px+）。小字号下 1px 偏移占字符宽度比例过大，抗锯齿叠加形成脏灰色光晕。
**Solution**: 放弃描边方案，改用精心设计的半透明面板（"Frosted Glass Lite"）：暖紫色调背景 `rgba(28,25,38,130)` + 顶部高光渐变 + 极细亮边。视觉效果优于纯黑扁平矩形。
**Key Learning**:
- **字幕描边仅适合大字号** — 13px 以下不要用
- 半透明面板的 "设计感" 不在于 alpha 高低，而在于 **色调暖度 + 高光层次 + 边框细节**
- 纯黑（rgb 0,0,0）半透明 = debug overlay 感；暖紫色调（rgb 28,25,38）= 设计意图感

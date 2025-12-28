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

---

## Session: 2025-12-26 12:00

### Completed
- [x] 完成 ASR 引擎切换功能审计（FunASR ↔ Whisper）
- [x] 修复 settings.py 硬编码 `asr_engine = "funasr"` 导致切换无效的 bug
- [x] 添加 HuggingFace 国内镜像加速（hf-mirror.com）
- [x] 实现 faster-whisper 动态安装（用户切换时自动安装依赖）
- [x] 添加 CUDA fallback 机制（GPU 不可用时自动切换 CPU）
- [x] 添加磁盘空间预检测（下载前检查空间是否充足）
- [x] 添加首次使用 Whisper 弹窗提醒
- [x] 更新热词表（添加 FunASR、Whisper 等术语）
- [x] 优化 DeepSeek polish prompt（更敢于纠正 ASR 错误）

### Key Changes
| File | Change |
|------|--------|
| `ui/qt/settings.py` | 动态安装 faster-whisper + 磁盘空间检测 + 首次使用提醒弹窗 |
| `launcher.py` | HF_ENDPOINT 镜像 + 进度文案优化 + 依赖检测 |
| `core/asr/whisper_engine.py` | CUDA fallback（GPU→CPU 自动切换） |
| `config/hotwords.json` | 新增 FunASR/Whisper 热词 + 优化 polish prompt |
| `config/hotwords_prompt_backup.txt` | 原始 prompt 备份 |

### Key Decisions
- **动态安装 vs 预装**: 选择动态安装 faster-whisper，避免增加 300MB 安装包体积
- **镜像策略**: 使用 hf-mirror.com 公益镜像，对中国用户友好
- **Polish Prompt**: 从"最小改动"改为"必须修复"语气，让 DeepSeek 更敢纠错

### Technical Findings
1. **FunASR/Whisper 切换**: HotWord 适配层已存在（app.py:633-650），但 settings.py 硬编码了引擎
2. **faster-whisper 安装**: 使用 `subprocess.run([sys.executable, "-m", "pip", ...])` 动态安装
3. **CUDA fallback**: 捕获 RuntimeError，检查 "CUDA" 或 "cudnn" 关键词后切换 CPU+int8
4. **HuggingFace 缓存路径**: `~/.cache/huggingface/hub/models--Systran--faster-whisper-{model}`
5. **DeepSeek 保守问题**: 原 prompt "能不改就不改" 导致很多 ASR 错误不敢修正

### Pending Tasks
1. [ ] 测试新 polish prompt 效果（需要用户实际使用验证）
2. [ ] 考虑离线版（预打包 Whisper 模型）作为可选方案
3. [ ] 清理调试代码（用户确认稳定后）

### Known Issues
- Polish 对同音字纠错（如"整齐→整体"）仍需依赖上下文判断，可能不够准确

---

---

## Session: 2025-12-28 17:30

### Completed
- [x] 调查 Whisper 冷启动幻觉问题（第一句话丢失）
- [x] 实现双重恢复机制：重试 + Interim 回退
- [x] 便携版 launcher.py 单例锁机制修复

### Key Changes
| File | Change |
|------|--------|
| `app.py:1016-1064` | 幻觉检测后添加重试机制 + interim 回退 |
| `launcher.py` | PID 创建时间校验、stale lock 清理优化 |

### Key Decisions
- **幻觉恢复策略**: 优先重试（GPU 已预热），其次用 interim 结果

### Technical Findings
1. **冷启动幻觉原因**: GPU 空闲后 CUDA kernel 需预热，导致第一次推理不稳定
2. **幻觉特征**: Whisper 输出重复句子（`_is_hallucination` Pattern 5 检测）
3. **Interim 价值**: 实时识别通常比最终结果稳定，可作为回退

### Pending Tasks
1. [ ] **修复启动页面缺失问题** ← 当前优先
2. [ ] 验证冷启动幻觉修复效果（用户反馈）
3. [ ] 清理调试代码

### Known Issues
- 启动页面（Splash Screen）不显示了 → 待调查

---

## Session: 2025-12-28 18:15

### Completed
- [x] 修复 VAD 会话黏连问题（语音积累不输出）
- [x] 修复 vad.py 中 pythonw.exe 不安全的 print() 调用
- [x] 修复 Whisper 无限循环幻觉（处理时间暴增到 18-50 秒）
- [x] 修复 max_new_tokens 与 initial_prompt 冲突问题
- [x] 深度审查并验证所有修复

### Key Changes
| File | Change |
|------|--------|
| `config/hotwords.json` | VAD: threshold 0.2→0.35, min_silence_ms 600→700, max_speech_ms=10000 |
| `app.py:532-560` | 读取并传递 max_speech_ms 到 VADConfig |
| `core/audio/vad.py:93-104,269-280` | 添加 `if sys.stdout is not None:` print 保护 |
| `core/asr/whisper_engine.py:166-171` | 添加 compression_ratio_threshold, log_prob_threshold, no_speech_threshold |
| `docs/DEBUG_LESSONS.md` | 记录 VAD 积累 + Whisper 幻觉问题解决方案 |

### Key Decisions
- **VAD threshold 0.35**: 平衡灵敏度和误报，允许自然停顿
- **max_speech_ms 10000**: 10秒安全网，防止无限累积
- **不使用 max_new_tokens**: 与 initial_prompt (196 tokens) 冲突，超过 Whisper max_length=448

### Technical Findings
1. **VAD 积累根因**: threshold=0.2 太低，环境噪声被误判为语音，silence 检测永不触发
2. **Whisper 幻觉特征**: 输出 "嘟嘟嘟..." 重复字符，处理时间暴增到 40-50 秒
3. **compression_ratio_threshold=2.4**: 关键参数，检测重复输出并提前终止
4. **FunASR vs Whisper**: 之前用 FunASR 没遇到这些问题，Whisper 需要更多防护参数

### Pending Tasks
1. [ ] 便携版打包测试
2. [ ] 长期使用验证 VAD/Whisper 参数稳定性

### Known Issues
- Whisper 偶发性处理变慢（可能是模型状态问题，但 compression_ratio 等参数已提供防护）

## Warmup Hints
<!-- 预热系统读取此区块 -->
focus: build.py
mode: standard
pending_research: 无

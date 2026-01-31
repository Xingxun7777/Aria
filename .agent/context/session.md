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

---

## Session: 2025-12-29 17:45

### Completed
- [x] 清理便携版用户数据（InsightStore、Session 文件、Debug 日志、音频、API keys）
- [x] Codex 安全审计（发现并清理残留日志：launch_error.log, splash_error.log）
- [x] 创建 README.txt 完整使用说明（含 SmartScreen 绕过指南）
- [x] 增强 build.py step_clean_sensitive_data()（自动清理所有敏感数据）
- [x] 创建 EXE 启动器（launcher_stub.py + build_launcher_exe.py）
- [x] 更换唤醒词：遥遥 → 小助手
- [x] 更换图标：托盘风格（深色圆 + 橙色声波条）
- [x] 清理 __pycache__ 目录（14个）
- [x] 最终验证：所有敏感数据已清除，便携版可分发

### Key Changes
| File | Change |
|------|--------|
| `dist_portable/VoiceType/README.txt` | 创建完整使用说明 |
| `build_portable/build.py` | step_clean_sensitive_data() 增强 - 清理所有用户数据 |
| `build_portable/launcher_stub.py` | EXE 启动器源码（调用 pythonw.exe） |
| `build_portable/build_launcher_exe.py` | PyInstaller 构建脚本 |
| `config/wakeword.json` (portable) | wakeword: 遥遥 → 小助手 |
| `assets/voicetype.ico` | 新图标（托盘样式：深色圆 + 橙色声波） |

### Key Decisions
- **EXE vs VBS**: 选择 EXE 启动器，VBS 容易被安全软件/企业策略拦截
- **图标一致性**: 使用托盘图标样式，保持视觉统一
- **唤醒词**: "遥遥"改"小助手"，对普通用户更友好

### Technical Findings
1. **托盘图标是代码生成的**: `main.py:create_tray_icon()` 用 QPainter 动态绘制
2. **ICO 转换**: PySide6 QPixmap → PIL Image → 多尺寸 ICO
3. **Windows 图标缓存**: 必须删除快捷方式重建才能刷新
4. **PyInstaller**: launcher_stub.py 编译后 ~6.1MB，带图标

### 便携版最终状态
```
dist_portable/VoiceType/ (6.8GB)
├── VoiceType.exe ★       推荐启动
├── VoiceType.cmd          备选
├── VoiceType.vbs          静默启动
├── VoiceType_debug.bat    调试
├── README.txt             使用说明
├── voicetype.ico          新图标
└── _internal/             程序文件
    - API Key: YOUR_API_KEY_HERE ✓
    - DebugLog: 空 ✓
    - InsightStore: 空 ✓
    - __pycache__: 已清理 ✓
    - 唤醒词: 小助手 ✓
```

### Pending Tasks
1. [ ] 分发测试（让真实用户使用）
2. [ ] 收集用户反馈
3. [ ] 考虑购买代码签名证书（消除 SmartScreen 警告）

### Known Issues
- Windows SmartScreen 首次运行会警告（未签名 EXE，需用户点"更多信息→仍要运行"）

---

## Session: 2025-12-29 19:30

### Completed
- [x] 审查热加载系统（API Key、唤醒词、替换规则等）
- [x] 验证 reload_config() 流程（manager.reload() → polisher 重建）
- [x] 创建并执行热加载验证脚本（_verify_hot_reload.py）
- [x] 确认所有热加载测试通过

### Key Changes
| File | Change |
|------|--------|
| (审查，无修改) | 确认热加载系统已正确实现 |

### Key Decisions
- **热加载验证策略**: 创建独立测试脚本，模拟配置修改 → reload → 验证新实例

### Technical Findings
1. **API Key 热加载流程**:
   - UI 保存 → hotwords.json 写入
   - settingsSaved 信号 → on_settings_saved()
   - backend.reload_config() → hotword_manager.reload()
   - reload() 设置 _polisher = None → 强制重建
   - get_active_polisher() 用新配置创建新实例

2. **热加载覆盖范围**:
   - ✅ API Key/URL/Model/Timeout
   - ✅ 润色提示词 (prompt_template)
   - ✅ 热词列表 + 权重
   - ✅ 替换规则
   - ✅ 唤醒词
   - ✅ VAD 参数
   - ✅ 热键
   - ❌ ASR 引擎/模型（需重启）
   - ❌ 音频设备（需重启）

3. **验证结果**: 所有热加载测试通过
   - Polisher 实例 ID 确实变化（强制重建）
   - 新 Polisher 使用新 API Key

### Pending Tasks
1. [ ] 分发测试（让真实用户使用）
2. [ ] 收集用户反馈
3. [ ] 考虑购买代码签名证书

### Known Issues
- 无新问题

---

## Session: 2026-01-02 (热词权重分层系统)

### Completed
- [x] 诊断 LLM 润色层过度替换问题（OPUS→ComfyUI, 米薯→ComfyUI, tram上→GitHub上）
- [x] 三方会谈（Codex + Gemini）设计热词权重分层系统
- [x] 实现 `manager.py` 新方法：`get_hotwords_by_layer()`, `get_asr_hotwords_with_score()`
- [x] 实现 `funasr_engine.py` 新方法：`set_hotwords_with_score()`
- [x] 修改 `app.py` 使用新的分层 API（初始化 + 热重载两处）
- [x] 验证 FunASR hotword 参数确实生效（generate() 调用成功）

### Key Changes
| File | Change |
|------|--------|
| `core/hotword/manager.py` | 添加 `get_hotwords_by_layer()` + `get_asr_hotwords_with_score()` |
| `core/asr/funasr_engine.py` | 添加 `set_hotwords_with_score()` 方法 |
| `app.py:668-677` | FunASR 初始化使用 score 映射 |
| `app.py:713-720` | 拼音匹配只用 weight>=1.0 热词 |
| `app.py:1728-1740` | 热重载同步更新 |
| `config/hotwords.json` | prompt_template 移除 {hotwords}，默认权重 0.5 |

### Key Decisions
- **权重分层设计**（三方会谈共识）:
  | 权重 | Layer 1 (ASR) | Layer 2 (正则) | Layer 2.5 (拼音) |
  |------|---------------|----------------|------------------|
  | 0    | ❌ | ❌ | ❌ |
  | 0.3  | ✅ score=20 (hint) | ❌ | ❌ |
  | 0.5  | ✅ score=50 (standard) | ✅ | ❌ |
  | 1.0  | ✅ score=80 (lock) | ✅ | ✅ |

- **LLM 不再接收热词列表**：避免过拟合，只做同音字纠错和标点

### Technical Findings
1. **过度替换根因**：所有热词默认权重 1.0 → LLM 视为"必须使用" → 激进替换
2. **FunASR hotword 格式**：换行分隔的 "word score" 对，score 影响解码偏置
3. **seaco_paraformer**：FunASR v1.2.7 的 paraformer-zh 自动使用 seaco 变体（原生支持 hotword）

### Pending Tasks
1. [ ] 实际运行测试识别效果
2. [ ] 根据测试结果调整个别热词权重
3. [ ] 便携版重新打包

### Known Issues
- 当前没有 weight=1.0 的热词，拼音匹配层为空（预期行为，保守策略）

---

## Session: 2026-01-02 (FunASR 热词分数优化)

### Completed
- [x] 终极分析 FunASR 热词系统参数配置
- [x] 诊断 weight=0.5 热词完全无法识别问题（FunASR score=30 太低）
- [x] 重新设计 weight→score 映射（Aggressive v2.0）
- [x] 添加 LLM Polish 三层系统（必须/强参考/参考）
- [x] 更新前端 UI 权重选项（添加 0.7、0.9）

### Key Changes
| File | Change |
|------|--------|
| `core/hotword/manager.py` | weight_to_score() 分数提升到 50/70/85/100；添加 strong tier |
| `core/hotword/polish.py` | PolishConfig 添加 hotwords_strong；_build_prompt() 支持【强参考】 |
| `ui/qt/settings.py` | 权重选项：0/0.3/0.5/0.7/0.9/1.0（移除 1.5/2.0） |

### Key Decisions
- **Aggressive 分数策略**：FunASR baseline=20 但实测 <40 几乎无效，新策略用 50-100
- **移除 1.5/2.0 权重**：过高权重导致过度校正（ultrathink 事件），1.0 已是最大值
- **三层 LLM 提示**：【必须】≥1.0 / 【强参考】0.7-0.99 / 【参考】0.5-0.69

### Technical Findings
1. **根因**：FunASR score=30（weight 0.5）低于有效激活阈值，SeACo-Paraformer 需要更高分数
2. **新映射**：
   | Weight | FunASR Score | LLM Tier |
   |--------|--------------|----------|
   | 0 | skip | 禁用 |
   | 0.3 | 15 | 仅提示 |
   | 0.5 | 50 | 【参考】 |
   | 0.7 | 70 | 【强参考】 |
   | 0.9 | 85 | 【强参考】 |
   | 1.0 | 100 | 【必须】 |

### Pending Tasks
1. [ ] 测试新分数是否修复 0.5 权重热词识别问题
2. [ ] 根据测试结果微调个别热词权重

### Known Issues
- 待验证：新 Aggressive 分数是否导致误激活

---

## Session: 2026-01-25 (权限弹窗优化)

### Completed
- [x] 权限弹窗添加"下次不再提醒"勾选框
- [x] 按钮从"关闭输入法"改为"暂时禁用"（不退出程序）
- [x] 实现禁用后按热键可重新启用功能
- [x] 勾选"不再提醒"后自动保存到配置，下次自动禁用不弹窗

### Key Changes
| File | Change |
|------|--------|
| `ui/qt/elevation_dialog.py` | 添加 QCheckBox "下次不再提醒" + disableRequested 信号 + 配置读写 |
| `ui/qt/main.py` | 连接 disableRequested 信号 + 处理 "enabled" 设置变更 |
| `app.py` | 添加 _is_disabled 标志 + set_enabled() 不停止热键 + _on_hotkey() 禁用状态重新启用 |

### Key Decisions
- **热键不停止**: 禁用时设置标志而非停止 hotkey_manager，允许热键重新启用
- **配置存储**: "不再提醒"设置保存到 hotwords.json 的 elevation_dialog.dont_remind

### Technical Findings
1. **原问题**: set_enabled(False) 会调用 hotkey_manager.stop()，热键完全无法使用
2. **解决方案**: 用 _is_disabled 标志替代停止热键，_on_hotkey() 检测到禁用状态时自动重新启用
3. **UI 同步**: bridge.emit_setting_changed("enabled", True) 通知 popup menu 更新开关状态

### Pending Tasks
1. [ ] 测试权限弹窗新功能
2. [ ] 验证"不再提醒"配置持久化

### Known Issues
- 无

---

## Warmup Hints
<!-- 预热系统读取此区块 -->
focus: ui/qt/elevation_dialog.py
mode: standard
pending_research: 无
note: 权限弹窗优化完成，添加了"暂时禁用"和"下次不再提醒"功能

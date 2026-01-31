# Draft: Fun-ASR-Nano Integration

## Requirements (confirmed)
- Add Fun-ASR-Nano as THIRD engine option (alongside Paraformer-zh and Whisper)
- Keep Paraformer-zh as default (fast, stable)
- Allow user selection in settings
- Preserve existing hotword system

## Technical Decisions
- Dual/triple architecture: low risk, user choice
- Nano API is similar to Paraformer (same funasr.AutoModel pattern)
- Hotword format compatible ("魔搭 阿里" space-separated)

## Research Findings (from user)
- Model: FunAudioLLM/Fun-ASR-Nano-2512
- VRAM: ~3.5GB FP16, ~1.8GB GGUF quantized
- RTF: 0.05-0.08 (still real-time, but slower than Paraformer 0.01)
- Streaming latency: 200-400ms (vs Paraformer ~50ms)
- Requires: trust_remote_code=True
- Known issue: LLM hallucination on silence/noise

## Codebase Analysis (from explore)

### ASR Architecture
- **Base class**: `ASREngine` in `core/asr/base.py`
  - Required methods: `load()`, `unload()`, `transcribe(audio)`, `is_loaded`
- **FunASR impl**: `FunASREngine` in `core/asr/funasr_engine.py`
  - Uses `FunASRConfig` for init, supports paraformer-zh and SenseVoiceSmall
  - `get_optimal_device()` with cuda→cpu fallback

### Hotword System
- **Storage**: `config/hotwords.json`
- **Manager**: `HotWordManager` in `core/hotword/manager.py`
- **Weight→Score mapping**: 0.3→30, 0.5→60, 1.0→100
- **Format for FunASR**: newline-separated "word score" pairs
- **Injection**: `set_hotwords_with_score(list_of_tuples)`

### Engine Selection
- `AriaApp._init_components()` reads `asr_engine` from config
- Checks `aria._preloaded_asr_engine` for launcher pre-load
- Dynamic instantiation based on engine string

### Test Infrastructure
- Custom script-based (no formal pytest config)
- Files: `test_v4_flow.py`, `tests/test_streaming_integration.py`
- Pattern: standalone scripts with print/manual assertions

## Open Questions
- [x] Extend funasr_engine.py or create new file? → **CREATE NEW** (cleaner)
- [x] UI framework being used? → **PySide6 (Qt6)**, settings in `ui/qt/settings.py`
- [x] Test infrastructure exists? → **Yes, informal scripts**
- [x] Fallback behavior if Nano fails to load? → **Fall back to Paraformer-zh**
- [x] Should model auto-download or require manual setup? → **先跑通流程，后续再考虑分发**
- [x] Nano hotword format same as Paraformer? → **Yes, same funasr API**
- [x] Display name? → **"Fun-ASR-Nano (高精度)"**
- [x] Test strategy? → **Manual verification only**

## User Decisions (confirmed 2026-01-29)
1. **Project directory**: `G:\AIBOX\voicetype-v1.1-dev` (ignore aria-release)
2. **Model loading**: HuggingFace auto-download for now, distribution strategy later
3. **Fallback**: Silent fallback to Paraformer-zh on failure
4. **UI name**: "Fun-ASR-Nano (高精度)"
5. **Tests**: Manual verification, follow existing test_v4_flow.py pattern

## Metis Review Findings (addressed)

### API Differences (CRITICAL)
| Aspect | Paraformer | Nano | Resolution |
|--------|-----------|------|------------|
| Hotword format | "word score" newline | List of strings | **Adapt in Nano engine** - map weights internally |
| Return format | `[{"text": ...}]` | `[{"text": ..., "text_tn": ...}]` | **Use text_tn** (normalized) |
| VAD handling | Optional | **Required** (fsmn-vad) | **Both VADs** - external Silero + internal fsmn-vad |

### Fallback Strategy
- Trigger: **On load() failure only** (not transcription errors)
- Method: Cold load Paraformer (3-5s latency acceptable for rare failure case)
- UI indication: Log warning, no popup

### Guardrails (from Metis)
**MUST DO:**
1. Inherit from `ASREngine` exactly like existing engines
2. Follow `FunASRConfig` pattern (dataclass with defaults)
3. Add "funasr-nano" to engine selection chain in app.py
4. Add nano_group QGroupBox in settings.py
5. Update _on_engine_changed() for 3 engines

**MUST NOT DO:**
1. Modify existing funasr_engine.py
2. Add new hotword layers or polish logic
3. Create factory pattern (keep if/elif)
4. Modify base.py
5. Add automatic runtime engine switching

### Scope Lock
- ❌ No streaming ASR for Nano
- ❌ No multi-engine comparison
- ❌ No model manager UI
- ❌ No benchmark features
- ❌ No language detection toggle

### Framework
- **PySide6 (Qt6)**
- Settings file: `ui/qt/settings.py`
- ASR settings in "Advanced" (高级) tab

### Engine Selection Widget
- `QComboBox` (`self.engine_combo`)
- Current options: "FunASR (推荐，中文优化)", "Whisper (多语言支持)"
- `_on_engine_changed(index)` toggles visibility of engine-specific groups
- Engine-specific settings in `QGroupBox` (funasr_group, whisper_group)

### Persistence
- Config: `config/hotwords.json`
- Key: `asr_engine` ("funasr" | "whisper")
- Restart required on engine change

### IMPORTANT DISCOVERY
- `fireredasr_engine.py` exists but this is **FireRedASR**, NOT Fun-ASR-Nano
- Fun-ASR-Nano is a DIFFERENT model from FunAudioLLM team (LLM-enhanced)
- Need to create NEW engine file for Nano

## Scope Boundaries
- INCLUDE: Config, engine file, UI settings, integration
- EXCLUDE: Streaming mode changes (Nano latency is higher)

## Session 2026-01-29 Confirmation

All requirements from previous session appear complete. Ready for plan generation.

### Clearance Checklist (Final Review)
- [x] Core objective: Add Fun-ASR-Nano as selectable ASR engine
- [x] Scope boundaries: INCLUDE engine/UI/config, EXCLUDE streaming
- [x] Technical approach: Create new `funasr_nano_engine.py`, extend settings
- [x] Test strategy: Manual verification (no formal pytest)
- [x] Blocking questions: None remaining

**STATUS: READY FOR PLAN GENERATION**

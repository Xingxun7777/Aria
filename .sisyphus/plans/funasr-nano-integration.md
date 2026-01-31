# Fun-ASR-Nano Integration

## TL;DR

> **Quick Summary**: Add Fun-ASR-Nano as third ASR engine option (alongside Paraformer-zh and Whisper) for higher accuracy on industrial/mixed Chinese data, while preserving existing stability.
> 
> **Deliverables**:
> - `core/asr/funasr_nano_engine.py` - New engine implementation
> - Updated `ui/qt/settings.py` - 3-engine dropdown + nano settings group
> - Updated `app.py` - Engine selection logic for "funasr-nano"
> - Updated `core/asr/__init__.py` - Export new classes
> 
> **Estimated Effort**: Medium (5 tasks, ~2-3 hours)
> **Parallel Execution**: YES - 2 waves
> **Critical Path**: Task 1 (Engine) -> Task 3 (App Integration) -> Task 5 (Manual Test)

---

## Context

### Original Request
Add Fun-ASR-Nano as a selectable ASR engine option:
1. Keep existing Paraformer-zh as default (fast, stable)
2. Add Fun-ASR-Nano as optional engine for higher accuracy
3. Let users choose in settings
4. Preserve all existing hotword system layers

### Interview Summary
**Key Discussions**:
- **API Compatibility**: Fun-ASR-Nano uses SAME `funasr.AutoModel` API - just change model name
- **Hardware**: RTX 5090 (32GB VRAM) - no memory concerns
- **Approach**: Incremental - Nano as optional, don't break existing stability
- **Config Constraint**: hotwords.json MUST be edited via Python (UTF-8 Edit tool bug)

**Research Findings**:
| Attribute | Paraformer-zh | Fun-ASR-Nano |
|-----------|---------------|--------------|
| Model | `paraformer-zh` | `FunAudioLLM/Fun-ASR-Nano-2512` |
| Parameters | 220M | 0.8B (3.6x larger) |
| VRAM | ~1.2GB | ~3.5GB |
| Latency | ~50ms | 200-400ms |
| Accuracy | Baseline | 15-25% better on industrial data |
| Hotword Format | `"word score"` newline | Same format |

### Metis Review
**Identified Gaps** (addressed):
- **launcher.py preload**: Uses `aria._preloaded_asr_engine` attribute. No launcher.py changes needed - preload happens elsewhere.
- **text_tn handling**: Nano may return `text_tn` (normalized) - plan includes handling both
- **Config key naming**: Use "funasr-nano" to match existing "funasr", "whisper" pattern

---

## Work Objectives

### Core Objective
Add Fun-ASR-Nano as a user-selectable ASR engine in Aria, accessible via Settings > Advanced, without modifying or breaking existing Paraformer-zh or Whisper functionality.

### Concrete Deliverables
- `core/asr/funasr_nano_engine.py` - New engine implementation (~150 lines)
- `core/asr/__init__.py` - Updated exports
- `ui/qt/settings.py` - Extended engine dropdown (3 options) + nano settings group
- `app.py` - Engine initialization branch for "funasr-nano"

### Definition of Done
- [ ] User can select "Fun-ASR-Nano" in Settings > Advanced > ASR Engine
- [ ] Selecting Nano and restarting app loads Nano model
- [ ] Hotwords work with Nano (transcription contains boosted terms)
- [ ] Fallback to Paraformer-zh on load failure works
- [ ] Saving settings persists "funasr-nano" to config
- [ ] Existing Paraformer and Whisper still work (no regressions)

### Must Have
- Same hotword injection interface as FunASREngine
- Graceful fallback on load failure
- Clear UI labels with accuracy/latency tradeoffs
- Config key "funasr-nano" for engine selection

### Must NOT Have (Guardrails)
- Modifications to existing `funasr_engine.py` (preserve stability)
- Modifications to `base.py` (no interface changes)
- Streaming mode for Nano (latency too high)
- Automatic runtime engine switching
- Factory pattern refactoring (keep simple if/elif)
- Model benchmark or comparison features

---

## Verification Strategy (MANDATORY)

### Test Decision
- **Infrastructure exists**: YES (informal scripts in tests/)
- **User wants tests**: Manual verification only
- **Framework**: None (script-based manual testing)

### Manual Execution Verification

Each TODO includes detailed verification procedures using:
- **interactive_bash (tmux)**: For running Python scripts
- **Python REPL**: For engine verification

**Evidence Required:**
- Terminal output from test commands
- Config file content after save
- Model loading logs

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately - Independent):
├── Task 1: Create funasr_nano_engine.py [no dependencies]
└── Task 2: Update settings.py UI [no dependencies]

Wave 2 (After Wave 1):
├── Task 3: Update app.py integration [depends: 1]
└── Task 4: Update __init__.py exports [depends: 1]

Wave 3 (After Wave 2):
└── Task 5: Manual integration test [depends: 2, 3, 4]

Critical Path: Task 1 → Task 3 → Task 5
Parallel Speedup: ~40% faster than sequential
```

### Dependency Matrix

| Task | Depends On | Blocks | Can Parallelize With |
|------|------------|--------|---------------------|
| 1 | None | 3, 4 | 2 |
| 2 | None | 5 | 1 |
| 3 | 1 | 5 | 4 |
| 4 | 1 | 5 | 3 |
| 5 | 2, 3, 4 | None | None (final) |

### Agent Dispatch Summary

| Wave | Tasks | Recommended Approach |
|------|-------|---------------------|
| 1 | 1, 2 | `run_in_background=true`, parallel agents |
| 2 | 3, 4 | Sequential after Wave 1 |
| 3 | 5 | Manual verification |

---

## TODOs

- [ ] 1. Create Fun-ASR-Nano Engine File

  **What to do**:
  - Create `core/asr/funasr_nano_engine.py`
  - Define `FunASRNanoConfig` dataclass with:
    - `model_name: str = "FunAudioLLM/Fun-ASR-Nano-2512"`
    - `device: str = "cuda"`
    - `vad_model: str = "fsmn-vad"`
    - `hotwords: List[str] = field(default_factory=list)`
    - `batch_size_s: int = 300`
  - Implement `FunASRNanoEngine` class:
    - Inherit pattern from FunASREngine (same structure)
    - Use same `funasr.AutoModel` API
    - `load()`: Initialize model with `trust_remote_code=True`, `hub="hf"`
    - `transcribe()`: Handle both `text` and `text_tn` in result (prefer text_tn if available)
    - `set_hotwords_with_score()`: Same interface as FunASREngine
    - `unload()`: Clean up model
  - Add fallback mechanism: If load() fails, log warning and raise exception (caller handles fallback)
  - Include CUDA check with `get_optimal_device()` pattern from funasr_engine.py

  **Must NOT do**:
  - Do NOT modify existing funasr_engine.py
  - Do NOT add streaming support
  - Do NOT add language detection

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Python file creation with specific API patterns, moderate complexity
  - **Skills**: [`deep-analysis`]
    - `deep-analysis`: Multi-layer verification for API compatibility

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Task 2)
  - **Blocks**: Tasks 3, 4
  - **Blocked By**: None (can start immediately)

  **References**:
  - `core/asr/funasr_engine.py:131-156` - FunASRConfig dataclass pattern to follow
  - `core/asr/funasr_engine.py:159-280` - FunASREngine.load() pattern with device detection
  - `core/asr/funasr_engine.py:306-465` - transcribe() pattern with hotword injection
  - `core/asr/funasr_engine.py:467-497` - set_hotwords_with_score() interface
  - `core/asr/funasr_engine.py:52-109` - get_optimal_device() CUDA check pattern
  - `core/asr/base.py` - ASREngine interface (do not modify, just reference)

  **Acceptance Criteria**:
  - [ ] File created at `core/asr/funasr_nano_engine.py`
  - [ ] Syntax check: `python -m py_compile core/asr/funasr_nano_engine.py` → No errors
  - [ ] Import check: `python -c "from core.asr.funasr_nano_engine import FunASRNanoEngine, FunASRNanoConfig"` → No errors
  - [ ] Class has methods: `load()`, `unload()`, `transcribe()`, `set_hotwords_with_score()`
  - [ ] Config has fields: `model_name`, `device`, `vad_model`, `hotwords`

  **Commit**: YES
  - Message: `feat(asr): add Fun-ASR-Nano engine for high-accuracy Chinese ASR`
  - Files: `core/asr/funasr_nano_engine.py`
  - Pre-commit: `python -m py_compile core/asr/funasr_nano_engine.py`

---

- [ ] 2. Update Settings UI for 3-Engine Selection

  **What to do**:
  - Edit `ui/qt/settings.py` to add third engine option
  - Update `engine_combo` dropdown in `_create_advanced_tab()`:
    - Index 0: "FunASR (推荐，中文优化)"
    - Index 1: "Fun-ASR-Nano (高精度，较慢)"
    - Index 2: "Whisper (多语言支持)"
  - Create `self.nano_group` QGroupBox for Nano-specific settings:
    - Device selection (cuda/cpu) - `self.nano_device = QComboBox()`
    - Info label: "高精度模式，需要约3.5GB显存，延迟200-400ms"
  - Update `_on_engine_changed(index)` to handle 3 states:
    - 0: Show funasr_group, hide nano_group, hide whisper_group
    - 1: Hide funasr_group, show nano_group, hide whisper_group
    - 2: Hide funasr_group, hide nano_group, show whisper_group
  - Update `load_config()` to load "funasr-nano" engine setting (index 1)
  - Update `save_config()` to save "funasr-nano" engine setting (index 1 -> "funasr-nano")

  **Must NOT do**:
  - Do NOT change existing FunASR or Whisper settings groups structure
  - Do NOT add model download UI (HuggingFace auto-download)

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: PySide6/Qt UI modifications
  - **Skills**: [`frontend-ui-ux`]
    - `frontend-ui-ux`: UI/UX implementation patterns

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Task 1)
  - **Blocks**: Task 5
  - **Blocked By**: None (can start immediately)

  **References**:
  - `ui/qt/settings.py:906-943` - Current ASR engine selection UI pattern
  - `ui/qt/settings.py:921-943` - funasr_group QGroupBox structure (template for nano_group)
  - `ui/qt/settings.py:945-974` - whisper_group QGroupBox structure
  - `ui/qt/settings.py:875-879` - `_on_engine_changed()` visibility toggle logic
  - `ui/qt/settings.py:1143-1166` - Engine config loading in `load_config()`
  - `ui/qt/settings.py:1290-1400` - Engine config saving in `save_config()`

  **Acceptance Criteria**:
  - [ ] Syntax check: `python -m py_compile ui/qt/settings.py` → No errors
  - [ ] Import check: `python -c "from ui.qt.settings import SettingsWindow"` → No errors
  - [ ] Manual verification:
    - Launch app with `python launcher.py`
    - Open Settings > Advanced tab
    - Engine dropdown shows 3 options
    - Selecting each option toggles correct settings group visibility
    - Save settings and verify config file updated

  **Commit**: YES
  - Message: `feat(ui): add Fun-ASR-Nano engine option in settings`
  - Files: `ui/qt/settings.py`
  - Pre-commit: `python -m py_compile ui/qt/settings.py`

---

- [ ] 3. Integrate Nano Engine in app.py

  **What to do**:
  - Edit `app.py` to add "funasr-nano" engine branch in `_init_components()`
  - Add import at top (near line 69): 
    ```python
    from .core.asr.funasr_nano_engine import FunASRNanoEngine, FunASRNanoConfig
    ```
  - Add engine initialization branch (after "funasr" block around line 647, before "whisper"):
    ```python
    elif engine_type == "funasr-nano":
        self._asr_engine_type = "funasr-nano"
        import aria
        preloaded = getattr(aria, "_preloaded_asr_engine", None)
        if preloaded is not None and isinstance(preloaded, FunASRNanoEngine):
            print("Using pre-loaded Fun-ASR-Nano engine")
            self.asr_engine = preloaded
        else:
            print("Loading Fun-ASR-Nano model (this may take a minute)...")
            nano_cfg = asr_cfg.get("funasr-nano", {})
            try:
                asr_config = FunASRNanoConfig(
                    device=nano_cfg.get("device", "cuda"),
                )
                self.asr_engine = FunASRNanoEngine(asr_config)
                self.asr_engine.load()
                print("Fun-ASR-Nano ready!")
            except Exception as e:
                logger.warning(f"Fun-ASR-Nano failed to load: {e}, falling back to Paraformer-zh")
                self._asr_engine_type = "funasr"
                funasr_cfg = asr_cfg.get("funasr", {})
                asr_config = FunASRConfig(
                    model_name=funasr_cfg.get("model_name", "paraformer-zh"),
                    device=funasr_cfg.get("device", "cuda"),
                )
                self.asr_engine = FunASREngine(asr_config)
                self.asr_engine.load()
                print("Fallback to Paraformer-zh complete")
    ```
  - Add hotword injection for Nano (after line 730, in hotword setup section):
    ```python
    elif engine_type == "funasr-nano" and hasattr(self.asr_engine, "set_hotwords_with_score"):
        hotwords_with_score = self.hotword_manager.get_asr_hotwords_with_score()
        self.asr_engine.set_hotwords_with_score(hotwords_with_score)
        print(f"[HOTWORD] Fun-ASR-Nano hotwords: {len(hotwords_with_score)} words")
    ```

  **Must NOT do**:
  - Do NOT modify existing FunASR or Whisper branches
  - Do NOT add automatic engine switching
  - Do NOT create factory pattern

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Core application logic modification with fallback handling
  - **Skills**: []
    - No special skills needed - straightforward Python integration

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Task 4, after Task 1)
  - **Parallel Group**: Wave 2 (with Task 4)
  - **Blocks**: Task 5
  - **Blocked By**: Task 1

  **References**:
  - `app.py:66-69` - Import pattern for ASR engines
  - `app.py:624-714` - Engine initialization if/elif chain in `_init_components()`
  - `app.py:627-648` - FunASR block pattern (template to follow)
  - `app.py:649-670` - Whisper block pattern
  - `app.py:720-741` - Hotword injection pattern per engine type

  **Acceptance Criteria**:
  - [ ] Syntax check: `python -m py_compile app.py` → No errors
  - [ ] Import check: `python -c "from app import AriaApp"` → No errors
  - [ ] grep verification: `grep "funasr-nano" app.py` shows initialization code

  **Commit**: YES
  - Message: `feat(app): integrate Fun-ASR-Nano engine in initialization`
  - Files: `app.py`
  - Pre-commit: `python -m py_compile app.py`

---

- [ ] 4. Update ASR Module Exports

  **What to do**:
  - Edit `core/asr/__init__.py` to export new classes
  - Add exports: `FunASRNanoEngine`, `FunASRNanoConfig`
  - Follow existing export pattern

  **Must NOT do**:
  - Do NOT change existing exports

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Simple file modification (< 5 lines)
  - **Skills**: []
    - No special skills needed

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Task 3, after Task 1)
  - **Parallel Group**: Wave 2 (with Task 3)
  - **Blocks**: Task 5
  - **Blocked By**: Task 1

  **References**:
  - `core/asr/__init__.py` - Current exports (read full file first)

  **Acceptance Criteria**:
  - [ ] Import check: `python -c "from core.asr import FunASRNanoEngine, FunASRNanoConfig"` → No errors

  **Commit**: Groups with Task 3
  - Message: (included in Task 3 commit)
  - Files: `core/asr/__init__.py`

---

- [ ] 5. Manual Integration Test

  **What to do**:
  - Test the complete flow manually
  - Verify all components work together

  **IMPORTANT**: Do NOT use Edit tool on hotwords.json (UTF-8 bug per CLAUDE.md). Use Python script to modify config.

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Manual verification steps
  - **Skills**: [`playwright`]
    - `playwright`: If UI verification needed

  **Parallelization**:
  - **Can Run In Parallel**: NO (final integration test)
  - **Parallel Group**: Wave 3 (sequential)
  - **Blocks**: None (final task)
  - **Blocked By**: Tasks 2, 3, 4

  **References**:
  - `test_v4_flow.py` - Existing test pattern
  - `config/hotwords.json` - Config structure
  - `CLAUDE.md` - hotwords.json editing constraint

  **Acceptance Criteria**:

  **Step 1: Update config to use Nano (via Python)**
  ```bash
  python -c "
import json
with open('config/hotwords.json', 'r', encoding='utf-8') as f:
    config = json.load(f)
config['asr_engine'] = 'funasr-nano'
config['funasr-nano'] = {'device': 'cuda'}
with open('config/hotwords.json', 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
print('Config updated to funasr-nano')
"
  ```
  - [ ] Verify: `python -c "import json; print(json.load(open('config/hotwords.json', encoding='utf-8'))['asr_engine'])"` → "funasr-nano"

  **Step 2: Test engine loading**
  ```bash
  python -c "
from core.asr.funasr_nano_engine import FunASRNanoEngine, FunASRNanoConfig
config = FunASRNanoConfig()
engine = FunASRNanoEngine(config)
engine.load()
print('Engine loaded:', engine.is_loaded)
print('Engine name:', engine.name)
engine.unload()
print('SUCCESS')
"
  ```
  - [ ] Expected: "Engine loaded: True", "SUCCESS"

  **Step 3: Test hotword injection**
  ```bash
  python -c "
from core.asr.funasr_nano_engine import FunASRNanoEngine, FunASRNanoConfig
config = FunASRNanoConfig()
engine = FunASRNanoEngine(config)
engine.set_hotwords_with_score([('Claude', 80), ('Gemini', 50)])
print('Hotwords set:', engine.config.hotwords)
"
  ```
  - [ ] Expected: Hotwords list shows formatted entries

  **Step 4: Test settings UI (manual)**
  - [ ] Start app: `python launcher.py`
  - [ ] Open Settings > Advanced tab
  - [ ] Verify dropdown shows 3 options
  - [ ] Select "Fun-ASR-Nano (高精度，较慢)"
  - [ ] Verify nano_group appears with device selector
  - [ ] Save settings
  - [ ] Verify config file has "funasr-nano"

  **Step 5: Restore config (cleanup)**
  ```bash
  python -c "
import json
with open('config/hotwords.json', 'r', encoding='utf-8') as f:
    config = json.load(f)
config['asr_engine'] = 'funasr'
with open('config/hotwords.json', 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
print('Config restored to funasr')
"
  ```

  **Commit**: NO (test task, no code changes)

---

## Commit Strategy

| After Task | Message | Files | Verification |
|------------|---------|-------|--------------|
| 1 | `feat(asr): add Fun-ASR-Nano engine for high-accuracy Chinese ASR` | funasr_nano_engine.py | py_compile |
| 2 | `feat(ui): add Fun-ASR-Nano engine option in settings` | settings.py | py_compile |
| 3+4 | `feat(app): integrate Fun-ASR-Nano engine in initialization` | app.py, __init__.py | py_compile |

---

## Success Criteria

### Verification Commands
```bash
# Syntax check all modified files
python -m py_compile core/asr/funasr_nano_engine.py
python -m py_compile ui/qt/settings.py
python -m py_compile app.py

# Import check
python -c "from core.asr import FunASRNanoEngine, FunASRNanoConfig"

# Engine load test (requires GPU, first run downloads model)
python -c "
from core.asr.funasr_nano_engine import FunASRNanoEngine, FunASRNanoConfig
e = FunASRNanoEngine(FunASRNanoConfig())
e.load()
print('SUCCESS: Engine loaded')
e.unload()
"
```

### Final Checklist
- [ ] All "Must Have" present:
  - [ ] 3-engine dropdown in settings
  - [ ] Config key "funasr-nano" persists
  - [ ] Hotword injection works
  - [ ] Fallback to Paraformer on load failure
- [ ] All "Must NOT Have" absent:
  - [ ] No funasr_engine.py changes
  - [ ] No base.py changes
  - [ ] No streaming mode
  - [ ] No runtime engine switching
- [ ] Settings UI shows 3 engine options
- [ ] Selecting Nano saves "funasr-nano" to config
- [ ] App loads Nano engine when configured
- [ ] Existing Paraformer and Whisper still work (no regressions)

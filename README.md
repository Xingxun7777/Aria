<div align="center">

# Aria — Local AI Voice Typing for Windows

**Windows 本地 AI 语音输入法 | Offline Speech-to-Text with Smart Correction**

[![Version](https://img.shields.io/badge/version-1.0.3.2-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#system-requirements)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![CUDA](https://img.shields.io/badge/CUDA-GPU%20accelerated-76B900.svg)](#system-requirements)

Offline voice recognition with GPU acceleration, AI-powered text correction, screen-aware context, voice commands, and history tracking.

离线语音识别 · GPU 加速 · 智能纠错 · 屏幕感知 · 语音指令 · 翻译润色

[Download](#download) · [Quick Start](#quick-start) · [Features](#features) · [Configuration](#configuration) · [FAQ](#faq)

</div>

---

## Why Aria?

Most voice typing tools require an internet connection, send your audio to the cloud, or lack proper Chinese support. Aria is different:

- **100% Local & Private** — All speech recognition runs on your machine. No audio leaves your computer. Ever.
- **Dual ASR Engine** — Qwen3-ASR (52 languages) + FunASR (fastest Chinese), with automatic GPU acceleration
- **Smart Correction** — 4-layer hotword pipeline: ASR guidance → regex → pinyin fuzzy match → AI polish
- **Screen-Aware** — Reads your current screen via OCR, injects keywords into ASR for better domain-specific accuracy
- **Voice Commands** — Translate, summarize, polish, rewrite selected text — all by voice
- **Works Everywhere** — Types into any Windows application, including games and admin-elevated windows

> **Privacy**: All voice data is processed locally. The optional AI polish feature uses an API (configurable), but raw audio never leaves your machine.

## Download

| Edition | Size | Description | Link |
|---------|------|-------------|------|
| **Lite** | ~2 GB | Downloads ASR model on first run (~1.2-3.4 GB) | [GitHub Releases](../../releases) |
| **Full** | ~6.4 GB | Includes Qwen3-ASR 0.6B + 1.7B, ready to use | Cloud drive (see Releases page) |

Both editions are portable — extract and run, no installation needed.

## Quick Start

1. Download and extract the **Full** or **Lite** edition
2. Double-click **`Aria.cmd`** (or `Aria.vbs` for silent launch)
3. Press **`` ` ``** (backtick) to start voice input — Aria listens continuously
4. Speak naturally — text appears at your cursor position
5. Press **`` ` ``** again to stop

That's it. No setup, no account, no internet required (except optional AI polish).

### From Source

```bash
git clone https://github.com/Xingxun7777/Aria.git
cd Aria
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config\hotwords.template.json config\hotwords.json
Aria_debug.bat
```

## Features

### Speech Recognition

- **Dual Engine** — Qwen3-ASR (default, 52 languages) / FunASR (Chinese, ultra-fast), automatic GPU acceleration
- **4-Layer Hotword Correction** — ASR prompt → regex replace → pinyin fuzzy → AI polish, each word with 0-1.0 weight
- **Toggle Mode** — Press hotkey to start, Aria listens and transcribes continuously, press again to stop
- **Real-time Streaming Subtitles** — Frosted glass panel shows interim recognition results
- **Noise Filtering** — Auto-discards filler words (um, uh, etc.) without affecting short valid replies
- **Screen Context** — Reads current window content via OCR, extracts keywords for ASR context, improving proper noun accuracy
- **Rolling Context** — Last 10 recognition results serve as cross-segment context

### AI Text Polish

- **Quality Mode** — API-powered LLM (DeepSeek / Gemini / OpenRouter) fixes typos, punctuation, homophones
- **Filler Removal** — Strips conversational fillers ("so basically", "you know", etc.)
- **Auto Structure** — Long spoken text auto-formatted with line breaks and numbered lists
- **Personalization** — Natural language rules (e.g., "keep English proper nouns in original case")
- **Scene Detection** — Chat apps keep casual tone; documents get formal style
- **Local Polish** — Advanced users can run GGUF models for fully offline polish

### Voice Commands for Selected Text

Select text, then say the wake word + command:

| Command | Effect |
|---------|--------|
| Polish / Optimize | Improve writing quality |
| Translate to English / Chinese / Japanese | Replace with translation |
| Expand | Add detail and depth |
| Summarize / Condense | Keep core information |
| Rewrite | Different expression |
| What does this mean | Popup translation (keeps original) |
| Summarize this | Popup summary |
| Help me reply | AI-generated reply with optional tone |
| Ask AI | Open AI chat window |
| Open this | Open selected path/URL |
| Remind me + time + content | Set timed reminder |

### Popup Interactions

Translation, summary, and reply popups support:
- Drag to move
- Pin to prevent auto-dismiss
- One-click copy
- One-click insert (auto-switches back to original window)

### Wake Word Control

| Command | Effect |
|---------|--------|
| [wake word] enable/disable auto-send | Toggle auto-Enter after recognition |
| [wake word] sleep / wake up | Pause/resume voice listening |
| [wake word] deep sleep | Fully unload ASR model, release all VRAM |

> Wake word is customizable in Settings.

### History

- All voice inputs, translations, polish results auto-saved
- History browser: browse by date, filter by type, keyword search
- Export as Markdown
- Auto-cleanup after 90 days

### Other

- **Backup API Failover** — Configure primary + backup API, auto-switch after 2 slow responses
- **Typewriter Mode** — Character-by-character input, compatible with games and admin apps
- **Reply Style** — Define AI reply personality in settings
- **Hot Reload** — Config changes take effect in 2 seconds, no restart needed
- **Auto Start** — Registry-based startup, supports portable mode
- **HF Mirror** — Chinese users default to hf-mirror.com for model downloads

## System Requirements

| Item | Minimum | Recommended |
|------|---------|-------------|
| OS | Windows 10 64-bit | Windows 11 |
| Python | 3.12 | 3.12 |
| RAM | 8 GB | 16 GB |
| GPU | None (CPU works) | NVIDIA GTX 16xx+ (4 GB VRAM) |

Includes PyTorch with CUDA 12.8. Automatically falls back to CPU mode if no compatible GPU is detected.

<details>
<summary>GPU Compatibility</summary>

| GPU Series | Architecture | Acceleration |
|------------|-------------|--------------|
| RTX 40xx / 50xx | Ada / Blackwell | Full support |
| RTX 30xx | Ampere | Full support |
| RTX 20xx / GTX 16xx | Turing | Supported |
| GTX 10xx | Pascal | Auto CPU fallback |
| No NVIDIA GPU | — | CPU mode |

CPU mode: all features work, recognition takes 2-5x longer.

</details>

## ASR Engines

| Engine | Speed | Languages | Highlights |
|--------|-------|-----------|------------|
| **Qwen3-ASR** (default) | Medium | 52 | Context-enhanced + anti-hallucination + screen OCR assist, auto-selects model size by VRAM |
| **FunASR** | Fastest | Chinese | Auto-downloads model (~700 MB) on first use, fully offline after |

<details>
<summary>4-Layer Hotword Correction Details</summary>

| Layer | Description |
|-------|-------------|
| L1 ASR Guidance | initial_prompt biases toward domain vocabulary |
| L2 Regex Replace | Rule mapping (e.g., "scale" → "skill") |
| L2.5 Pinyin Match | Homophone correction (e.g., "星循" → "星巡") |
| L3 AI Polish | LLM API call for grammar and homophone fixes |

Each hotword has 0-1.0 weight for fine-grained control. See [Configuration Reference](docs/CONFIGURATION.md).

</details>

## Configuration

Config file: `config/hotwords.json` (auto-created from template on first run, hot-reloads 2s after save).

**Common Settings:**

| Field | Description | Default |
|-------|-------------|---------|
| `asr_engine` | Recognition engine | `qwen3` |
| `general.hotkey` | Global hotkey | `` ` `` |
| `polish_mode` | Polish mode | `quality` |
| `filter_filler_words` | Filler word filter | `true` |
| `auto_structure` | Auto structure | `false` |
| `reply_style` | Reply personality | `""` |
| `vad.noise_filter` | Noise filter | `true` |
| `vad.screen_ocr` | Screen awareness | `true` |
| `vad.threshold` | VAD sensitivity (0-1) | `0.2` |
| `output.typewriter_mode` | Character input mode | `false` |

> Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md)

## FAQ

<details>
<summary><b>Recognition accuracy is low?</b></summary>

1. Add proper nouns to `hotwords` list with appropriate weights
2. Configure `replacements` for known mis-recognitions
3. Set `domain_context` to describe your use case (e.g., "programming discussion")
4. Enable API polish (`polish_mode: "quality"` + configure API key)
5. Ensure screen OCR is enabled (`vad.screen_ocr: true`)

</details>

<details>
<summary><b>GPU acceleration not working?</b></summary>

1. Verify `nvidia-smi` runs successfully
2. GTX 16xx+ auto-enables GPU; older GPUs auto-fallback to CPU
3. Update GPU drivers to latest version

</details>

<details>
<summary><b>Can't type into certain applications?</b></summary>

1. Enable Typewriter mode: `output.typewriter_mode: true`
2. If target app runs as admin, run Aria as admin too

</details>

<details>
<summary><b>First launch is slow?</b></summary>

- **Lite**: Qwen3-ASR downloads on first use (~1.2-3.4 GB), offline after that
- **Full**: Models pre-bundled, no download needed
- Or switch to FunASR (first download ~700 MB, offline after)

</details>

<details>
<summary><b>Noisy environment causes false inputs?</b></summary>

1. Ensure noise filter is on (`vad.noise_filter: true`, default)
2. Increase VAD threshold (`vad.threshold`, default 0.2, try 0.4 for noisy rooms)
3. Increase energy threshold (`vad.energy_threshold`, default 0.003)

</details>

## Project Structure

<details>
<summary>Expand</summary>

```
Aria/
├── launcher.py              # Entry: singleton check + splash + model preload
├── app.py                   # Main app: state machine + ASR orchestration
├── core/                    # Core modules
│   ├── asr/                 # ASR engines (Qwen3-ASR / FunASR)
│   ├── audio/               # Audio capture + VAD (Silero)
│   ├── context/             # Screen context + OCR
│   ├── history/             # History storage + migration
│   ├── hotword/             # 4-layer hotword correction + AI polish
│   ├── selection/           # Selection commands (polish/translate/expand)
│   ├── wakeword/            # Wake word detection + command execution
│   └── command/             # Voice keyboard commands
├── ui/qt/                   # PySide6 UI
│   ├── main.py              # Main window + tray + signal routing
│   ├── floating_ball.py     # Floating ball + streaming subtitles
│   ├── popup_menu.py        # Right-click menu
│   ├── settings.py          # Settings panel
│   ├── history_browser.py   # History browser
│   ├── translation_popup.py # Translation/summary/reply popup
│   └── workers/             # Background tasks
├── system/                  # System integration
│   ├── hotkey.py            # Global hotkey
│   ├── output.py            # Text output + window detection
│   └── admin.py             # Privilege detection
├── config/                  # Configuration
│   ├── hotwords.template.json  # Config template
│   ├── wakeword.json        # Wake word definitions
│   └── commands.json        # Keyboard command definitions
└── build_portable/          # Portable packaging
    ├── build.py             # Build script
    ├── release-lite.bat     # Lite packaging
    └── release-full.bat     # Full packaging
```

</details>

## License

[Apache License 2.0](LICENSE)

## Acknowledgments

| Project | License | Description |
|---------|---------|-------------|
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) | Apache 2.0 | Qwen speech recognition |
| [FunASR](https://github.com/alibaba-damo-academy/FunASR) | MIT | Alibaba DAMO speech recognition |
| [Silero-VAD](https://github.com/snakers4/silero-vad) | MIT | Voice activity detection |
| [PySide6](https://www.qt.io/) | LGPL v3 | Qt6 Python binding |
| [PyTorch](https://pytorch.org/) | BSD-3-Clause | Deep learning framework |
| [pypinyin](https://github.com/mozillazg/python-pinyin) | MIT | Chinese pinyin conversion |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache 2.0 | PaddleOCR ONNX inference |

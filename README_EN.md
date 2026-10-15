<div align="center">

# Aria

**Local voice typing for Windows: press the backtick key, talk, and the text lands wherever your cursor is. Said it wrong? Say "小助手, make that sound more natural" and it rewrites the last sentence in place.**

Recognition runs entirely on your own machine with Qwen3-ASR. No account, no upload, and it works without a GPU.

[![Version](https://img.shields.io/badge/version-1.7.1-blue.svg)](https://github.com/Xingxun7777/Aria/releases/latest)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#requirements)

[中文说明](README.md) · [Download](#download) · [Quick start](#quick-start) · [Features](#what-it-does) · [Privacy](docs/PRIVACY_DATA.md) · [Contributing](CONTRIBUTING.md)

</div>

---

## Download

[**Get `Aria-v1.7.1-Windows.zip` from the latest Release**](https://github.com/Xingxun7777/Aria/releases/latest) (about 1.2 GB, portable, no installer).

| File | Purpose |
|---|---|
| `Aria-v1.7.1-Windows.zip` | The only install package. Runs on CPU out of the box; includes the one-click GPU installer |
| `Aria-source-1.7.1.zip` | Payload for the in-app auto-updater; fresh installs do not need it |

- No NVIDIA GPU: unzip and double-click `Aria.exe`. The default CPU engine is sherpa-onnx int8 running Qwen3-ASR 0.6B.
- NVIDIA GPU: start Aria, right-click the floating window and choose "GPU 加速" (GPU acceleration). It downloads about 3.1 GB of pinned assets (llama.cpp CUDA + Qwen3-ASR 1.7B Q8 GGUF), verifies every file by size and SHA256, runs a real GPU smoke test, then switches engines without a restart. A failed install never breaks the CPU setup; `Install_GPU.cmd` in the package root is the fallback entry when Aria is closed.

## Quick start

1. Unzip the package to a normal writable folder. Do not run it from inside the archive preview.
2. Double-click `Aria.exe`. On first launch Windows SmartScreen may warn about an unsigned app; choose "More info → Run anyway". This only happens once; in-app updates do not trigger it again.
3. Put the cursor in any text field and press the backtick key `` ` `` to start listening. Speak normally: each sentence is typed automatically after a pause of about 1.5 seconds, no extra key presses. Press `` ` `` again to stop.
4. Optional: configure an AI endpoint (tray icon → Settings → API; DeepSeek or any compatible API with your own key). With that in place you can keep the microphone open and say the wake word followed by an instruction, for example "小助手，把刚才那句话改得更自然一点" ("assistant, make the last sentence sound more natural"). The text you just dictated is rewritten in place, in chat apps, Word / WPS documents and terminals alike.

Everything local (recognition, hotword correction, typing, local voice commands such as screenshot, reminder, open app) works without any API key. Voice editing, translation, summarizing and reply drafting are AI features and need a key.

## What it does

- **Local speech to text**: CPU by default, optional llama.cpp CUDA engine on NVIDIA GPUs. Audio is never uploaded or written to disk.
- **Fix mistakes by voice**: say the wake word plus what you want changed; Aria rewrites the sentence it just typed. Follow-up requests within three minutes keep refining the previous version.
- **Recover a lost sentence**: if a sentence landed in the wrong window, place the cursor and say "小助手，上屏" to type it again at the cursor. It never presses Enter for you and leaves images or files in your clipboard untouched.
- **Voice commands**: screenshot, reminders, open programs or paths locally (works on a file selected in Explorer or the Cursor / VS Code file tree); translate, summarize and reply once an API key is set.
- **Hotwords and homophone correction**: weighted hotwords, replacement rules, pinyin similarity and on-screen proper-noun learning.
- **Screen OCR context**: window title, UI Automation text and local screenshot OCR are used as evidence for correcting names; the raw text is never inserted into your output, and the heavier layers are off by default.
- **Three polish styles** (optional AI): verbatim, smooth speech, structured document.
- **Acoustic guard rails and self-healing**: VAD, energy, peak and confidence checks reduce swallowed words and silence hallucinations; timeouts or empty results trigger engine rebuilds and rescue paths.
- **Clipboard protection**: text, images, file lists and an empty clipboard are all restored after typing.
- **Auto-update**: verified payloads replace program files only; settings, history and models stay.

Qwen3-ASR covers 30 languages and 22 Chinese dialects. Aria's UI, wake word and voice commands are currently Chinese-first; the default wake word can be changed in Settings → 语音指令.

## Requirements

| | CPU package | GPU mode |
|---|---|---|
| OS | Windows 10/11 64-bit | Windows 10/11 64-bit |
| CPU | x64, 4+ cores recommended | x64 |
| RAM | 8 GB (16 GB recommended) | 16 GB recommended |
| GPU | not required | NVIDIA CUDA GPU, 6 GB+ free VRAM recommended |
| Python | not needed for the package; 3.12 for source | same |

## Privacy and network use

- Default engines (`qwen3_sherpa` on CPU, `qwen3_llamacpp` on GPU) run offline; the GPU server binds to `127.0.0.1` only.
- AI polish, cloud ASR rescue, screen-aware polish and automatic hotword learning are all off by default and only contact the API endpoint you configure.
- Update checks read the release manifest from GitHub-owned hosts only; downloads may fall back to accelerator mirrors but must match the manifest SHA256. GPU assets fall back from `huggingface.co` to `hf-mirror.com` with pinned hashes.
- API keys are encrypted with Windows DPAPI. Debug recordings are never saved unless `ARIA_DEBUG_SAVE_AUDIO=1` is set.

Details: [data and privacy notes](docs/PRIVACY_DATA.md), [engine guide](docs/ENGINES.md), [screen OCR module](docs/OCR.md), [configuration reference](docs/CONFIGURATION.md). Report security issues privately as described in [SECURITY.md](SECURITY.md).

## Running from source

```powershell
git clone https://github.com/Xingxun7777/Aria.git
cd Aria
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config\hotwords.template.json config\hotwords.json
.\.venv\Scripts\python.exe launcher.py
```

The source template defaults to the `qwen3` PyTorch CUDA runtime and downloads model weights on first run. If you just want the CPU build, download the Release package instead of installing the development stack. The public repository is a sanitized source snapshot of each release; see [CONTRIBUTING.md](CONTRIBUTING.md) for what is and is not included.

## License

Apache License 2.0. Upstream components: [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR), [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), [llama.cpp](https://github.com/ggml-org/llama.cpp), [FunASR](https://github.com/modelscope/FunASR), [Silero VAD](https://github.com/snakers4/silero-vad), [PySide6](https://www.qt.io/), [RapidOCR](https://github.com/RapidAI/RapidOCR).

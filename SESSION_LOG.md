# VoiceType v1.1 Session Log

## 2025-12-20 Session

### Completed Tasks

| Task | File | Changes |
|------|------|---------|
| History panel click fix | `ui/qt/history.py` | Removed `TextSelectableByMouse`, added `WA_TransparentForMouseEvents`, entire item clickable |
| Shortcut hint optimization | `ui/qt/history.py` | Removed Ctrl+1/2/3 display, changed to "点击复制" |
| Process residual issue | `launcher.py` | Disabled aggressive orphan process cleanup (was killing parent processes) |
| Exit cleanup enhancement | `ui/qt/main.py` | Improved `cleanup_and_quit()` with exception protection, thread detection, force exit |
| _flog error fix | `ui/qt/floating_ball.py` | Changed 2x `_flog()` to `self._log()` |
| AI chat window | `ui/qt/ai_chat_window.py` | Added "New Chat" + "Save Chat" buttons |

### Key Technical Decisions

1. **Singleton mechanism**: Named Mutex + file lock, disabled orphan cleanup (too risky)
2. **History panel interaction**: Chose "entire item clickable" over "text selectable" - better UX
3. **Qt event passing**: Used `WA_TransparentForMouseEvents` to pass mouse events to parent

### Modified Files

```
ui/qt/history.py        - Click-to-copy logic fix
ui/qt/main.py           - Exit cleanup enhancement
ui/qt/floating_ball.py  - _flog error fix
ui/qt/ai_chat_window.py - New chat/save features
launcher.py             - Disabled orphan process cleanup
```

### Remaining Optional Tasks

| Priority | Feature | Effort |
|----------|---------|--------|
| P0 | Sound system (3 wav files) | ~20 lines + audio generation |
| P2 | Floating ball press physics | ~60 lines |
| P2 | voiceActivity signal connection | ~5 lines |
| P2 | Translation popup TTS | Low priority |

### Next Session Entry Point

- Core features stable and working
- If continuing, start with P0 sound system
- Plan file: `C:\Users\84238\.claude\plans\eager-pondering-valiant.md`

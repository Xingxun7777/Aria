#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test voicetype environment."""
import sys

print('=== 测试voicetype环境 ===')

print('1. PyTorch...')
try:
    import torch
    print(f'   torch {torch.__version__} OK')
except Exception as e:
    print(f'   FAILED: {e}')
    sys.exit(1)

print('2. Silero-VAD...')
try:
    from silero_vad import load_silero_vad
    model = load_silero_vad()
    audio = torch.randn(512)
    prob = model(audio, 16000)
    print(f'   VAD OK')
except Exception as e:
    print(f'   FAILED: {e}')
    sys.exit(1)

print('3. faster-whisper (CPU)...')
try:
    from faster_whisper import WhisperModel
    import numpy as np
    model = WhisperModel('tiny', device='cpu', compute_type='int8')
    audio = np.random.randn(16000).astype(np.float32) * 0.01
    segments, info = model.transcribe(audio, language='zh')
    list(segments)
    print('   faster-whisper OK')
except Exception as e:
    print(f'   FAILED: {e}')
    sys.exit(1)

print('4. PySide6...')
try:
    from PySide6.QtCore import QCoreApplication
    print('   PySide6 OK')
except Exception as e:
    print(f'   FAILED: {e}')
    sys.exit(1)

print('\n=== 全部通过！ ===')

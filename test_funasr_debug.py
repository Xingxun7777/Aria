# -*- coding: utf-8 -*-
"""
FunASR Debug Test Script
Tests the FunASR engine to diagnose transcription issues.
"""
import sys
import os
import traceback

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))


def test_funasr():
    print("=" * 60)
    print("FunASR Debug Test")
    print("=" * 60)

    # Step 1: Check if FunASR can be imported
    print("\n[1] Importing FunASR engine...")
    try:
        from core.asr.funasr_engine import FunASREngine, FunASRConfig

        print("    OK: FunASR engine imported successfully")
    except Exception as e:
        print(f"    FAIL: Import error: {e}")
        traceback.print_exc()
        return

    # Step 2: Create config and engine
    print("\n[2] Creating FunASR config...")
    try:
        config = FunASRConfig(
            model_name="paraformer-zh",
            device="cuda",
            language="auto",
        )
        print(
            f"    OK: Config created - model={config.model_name}, device={config.device}"
        )
    except Exception as e:
        print(f"    FAIL: Config error: {e}")
        traceback.print_exc()
        return

    # Step 3: Create engine instance
    print("\n[3] Creating FunASR engine instance...")
    try:
        engine = FunASREngine(config)
        print("    OK: Engine instance created")
    except Exception as e:
        print(f"    FAIL: Engine creation error: {e}")
        traceback.print_exc()
        return

    # Step 4: Check if model is already loaded (preloaded)
    print("\n[4] Checking model state...")
    print(f"    Model is None: {engine._model is None}")
    print(f"    Model type: {type(engine._model)}")

    # Step 5: Load model if not loaded
    if engine._model is None:
        print("\n[5] Loading model...")
        try:
            engine.load()
            print(f"    OK: Model loaded - type={type(engine._model)}")
        except Exception as e:
            print(f"    FAIL: Model load error: {e}")
            traceback.print_exc()
            return
    else:
        print("\n[5] Model already loaded (skipping load)")

    # Step 6: Load test audio file
    print("\n[6] Loading test audio file...")
    audio_path = os.path.join(os.path.dirname(__file__), "DebugLog", "audio_4.wav")

    if not os.path.exists(audio_path):
        # Try any audio file
        debug_dir = os.path.join(os.path.dirname(__file__), "DebugLog")
        audio_files = [f for f in os.listdir(debug_dir) if f.endswith(".wav")]
        if audio_files:
            audio_path = os.path.join(debug_dir, audio_files[-1])
            print(f"    Using: {audio_path}")
        else:
            print("    FAIL: No audio files found in DebugLog/")
            return
    else:
        print(f"    Using: {audio_path}")

    try:
        import wave
        import numpy as np

        with wave.open(audio_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            audio_int16 = np.frombuffer(frames, dtype=np.int16)
            audio = audio_int16.astype(np.float32) / 32768.0

        print(f"    OK: Audio loaded - {len(audio)} samples ({len(audio)/16000:.2f}s)")
        print(
            f"    Audio stats: min={audio.min():.4f}, max={audio.max():.4f}, mean={audio.mean():.6f}"
        )
    except Exception as e:
        print(f"    FAIL: Audio load error: {e}")
        traceback.print_exc()
        return

    # Step 7: Run transcription with detailed error handling
    print("\n[7] Running transcription...")
    import time

    try:
        start = time.time()
        result = engine.transcribe(audio)
        elapsed = (time.time() - start) * 1000

        print(f"    Transcription time: {elapsed:.2f}ms")
        print(f"    Result text: '{result.text}'")
        print(f"    Result confidence: {result.confidence}")
        print(f"    Result type: {result.type}")

        if result.text:
            print("\n    >>> SUCCESS: Transcription worked!")
        else:
            if elapsed < 10:
                print("\n    >>> FAIL: Empty result with suspiciously fast timing")
                print("    This usually means an exception was silently caught.")
            else:
                print("\n    >>> PARTIAL: Empty result but timing is normal")
                print("    Audio may not contain recognizable speech.")

    except Exception as e:
        print(f"    EXCEPTION during transcription: {e}")
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("Test complete")
    print("=" * 60)


if __name__ == "__main__":
    test_funasr()

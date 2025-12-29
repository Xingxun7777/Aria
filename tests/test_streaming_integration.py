"""
Streaming Transcription Integration Test
=========================================
Tests the full pipeline: AudioCapture + VAD + WhisperEngine + StreamingDisplay
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import numpy as np
import time
from typing import List


# Test imports
def test_imports():
    """Test that all modules import correctly."""
    print("=" * 60)
    print("Test 1: Module Imports")
    print("=" * 60)

    try:
        from aria.core.audio.vad import VADProcessor, VADConfig

        print("  [OK] VADProcessor imported")

        from aria.core.audio.capture import AudioCapture, AudioConfig

        print("  [OK] AudioCapture imported")

        from aria.core.asr.base import ASREngine, ASRResult, TranscriptType

        print("  [OK] ASR base classes imported")

        from aria.core.asr.whisper_engine import WhisperEngine, WhisperConfig

        print("  [OK] WhisperEngine imported")

        from aria.ui.streaming_display import (
            DisplayBuffer,
            DisplayState,
            TranscriptSegment,
            StreamingTranscriptionManager,
        )

        print("  [OK] StreamingDisplay imported")

        return True
    except ImportError as e:
        print(f"  [FAIL] Import error: {e}")
        return False


def test_display_buffer():
    """Test DisplayBuffer state transitions and callbacks."""
    print("\n" + "=" * 60)
    print("Test 2: DisplayBuffer State Machine")
    print("=" * 60)

    from aria.ui.streaming_display import DisplayBuffer, DisplayState

    buffer = DisplayBuffer()
    updates: List[tuple] = []
    states: List[DisplayState] = []

    # Set callbacks
    buffer.set_callbacks(
        on_update=lambda text, is_final: updates.append((text, is_final)),
        on_state_change=lambda state: states.append(state),
    )

    # Test state transitions
    assert buffer.state == DisplayState.IDLE, "Initial state should be IDLE"
    print("  [OK] Initial state is IDLE")

    # Start listening
    buffer.start_listening()
    assert buffer.state == DisplayState.LISTENING, "Should be LISTENING"
    print("  [OK] State changed to LISTENING")

    # Add interim result
    buffer.add_interim("Hello wor", confidence=0.7)
    assert buffer.state == DisplayState.SHOWING_INTERIM, "Should show interim"
    assert buffer.get_full_text() == "Hello wor"
    print("  [OK] Interim text added: 'Hello wor'")

    # Update interim (replaces previous)
    buffer.add_interim("Hello world", confidence=0.85)
    assert buffer.get_full_text() == "Hello world"
    print("  [OK] Interim text updated: 'Hello world'")

    # Add final result
    buffer.add_final("Hello world!")
    assert buffer.state == DisplayState.SHOWING_FINAL, "Should show final"
    assert buffer.get_final_text() == "Hello world!"
    assert buffer.is_complete
    print("  [OK] Final text set: 'Hello world!'")

    # Verify callbacks fired
    assert len(updates) >= 3, "Should have multiple update callbacks"
    assert len(states) >= 3, "Should have state change callbacks"
    print(
        f"  [OK] Callbacks fired: {len(updates)} updates, {len(states)} state changes"
    )

    # Test clear
    buffer.clear()
    assert buffer.state == DisplayState.IDLE
    assert not buffer.has_content
    print("  [OK] Buffer cleared")

    return True


def test_transcript_segment():
    """Test TranscriptSegment display styles."""
    print("\n" + "=" * 60)
    print("Test 3: TranscriptSegment Display Styles")
    print("=" * 60)

    from aria.ui.streaming_display import TranscriptSegment

    # Final segment
    final = TranscriptSegment(text="Hello", is_final=True, confidence=1.0)
    assert final.display_style == "final"
    print("  [OK] Final segment style: 'final'")

    # High confidence interim
    interim_high = TranscriptSegment(text="Hel", is_final=False, confidence=0.85)
    assert interim_high.display_style == "interim-high"
    print("  [OK] High confidence interim style: 'interim-high'")

    # Low confidence interim
    interim_low = TranscriptSegment(text="H", is_final=False, confidence=0.5)
    assert interim_low.display_style == "interim-low"
    print("  [OK] Low confidence interim style: 'interim-low'")

    return True


def test_asr_result():
    """Test ASRResult properties."""
    print("\n" + "=" * 60)
    print("Test 4: ASRResult Properties")
    print("=" * 60)

    from aria.core.asr.base import ASRResult, TranscriptType

    # Interim result
    interim = ASRResult(text="Testing", type=TranscriptType.INTERIM, confidence=0.8)
    assert interim.is_interim
    assert not interim.is_final
    print("  [OK] Interim result properties correct")

    # Final result
    final = ASRResult(
        text="Testing complete",
        type=TranscriptType.FINAL,
        confidence=1.0,
        start_time=0.0,
        end_time=2.5,
    )
    assert final.is_final
    assert not final.is_interim
    assert final.duration == 2.5
    print("  [OK] Final result properties correct")

    return True


def test_whisper_engine_init():
    """Test WhisperEngine initialization (without loading model)."""
    print("\n" + "=" * 60)
    print("Test 5: WhisperEngine Configuration")
    print("=" * 60)

    from aria.core.asr.whisper_engine import WhisperEngine, WhisperConfig

    # Default config
    config = WhisperConfig()
    assert config.model_name == "base"
    assert config.sample_rate == 16000
    print("  [OK] Default config: model='base', sr=16000")

    # Custom config
    config = WhisperConfig(model_name="small", language="zh", chunk_length_s=3.0)
    engine = WhisperEngine(config)
    assert not engine.is_loaded
    assert engine.config.language == "zh"
    print("  [OK] Custom config: model='small', language='zh'")

    return True


def test_streaming_manager_setup():
    """Test StreamingTranscriptionManager setup (without starting)."""
    print("\n" + "=" * 60)
    print("Test 6: StreamingTranscriptionManager Setup")
    print("=" * 60)

    from aria.ui.streaming_display import StreamingTranscriptionManager, DisplayState

    manager = StreamingTranscriptionManager()

    # Initial state
    assert not manager.is_running
    assert manager.current_text == ""
    print("  [OK] Initial state: not running, empty text")

    # Set callbacks
    text_updates = []
    state_changes = []

    manager.set_on_text_update(lambda t, f: text_updates.append((t, f)))
    manager.set_on_state_change(lambda s: state_changes.append(s))
    print("  [OK] Callbacks configured")

    # Verify display buffer has callbacks
    assert manager.display._on_update is not None
    assert manager.display._on_state_change is not None
    print("  [OK] Display buffer callbacks linked")

    return True


def test_simulated_pipeline():
    """Simulate the full pipeline with mock data."""
    print("\n" + "=" * 60)
    print("Test 7: Simulated Pipeline (No Real Audio)")
    print("=" * 60)

    from aria.ui.streaming_display import DisplayBuffer, DisplayState
    from aria.core.asr.base import ASRResult, TranscriptType

    buffer = DisplayBuffer()
    results = []

    buffer.set_callbacks(
        on_update=lambda t, f: results.append(f"Update: '{t}' (final={f})"),
        on_state_change=lambda s: results.append(f"State: {s.name}"),
    )

    # Simulate workflow
    print("  Simulating: User presses hotkey...")
    buffer.start_listening()

    print("  Simulating: Speech detected...")
    buffer.start_transcribing()

    print("  Simulating: Interim result 1...")
    buffer.add_interim("你", confidence=0.6)

    print("  Simulating: Interim result 2...")
    buffer.add_interim("你好", confidence=0.75)

    print("  Simulating: Interim result 3...")
    buffer.add_interim("你好世", confidence=0.8)

    print("  Simulating: Final result...")
    buffer.add_final("你好世界")

    print("  Simulating: Ready to insert...")
    buffer.mark_ready()

    # Verify
    assert buffer.get_final_text() == "你好世界"
    assert buffer.state == DisplayState.READY_TO_INSERT
    print(f"  [OK] Final text: '{buffer.get_final_text()}'")
    print(f"  [OK] Final state: {buffer.state.name}")
    print(f"  [OK] Callback count: {len(results)}")

    return True


def run_all_tests():
    """Run all integration tests."""
    print("\n" + "=" * 70)
    print("  ARIA STREAMING INTEGRATION TEST")
    print("=" * 70)

    tests = [
        ("Module Imports", test_imports),
        ("DisplayBuffer State Machine", test_display_buffer),
        ("TranscriptSegment Styles", test_transcript_segment),
        ("ASRResult Properties", test_asr_result),
        ("WhisperEngine Configuration", test_whisper_engine_init),
        ("StreamingManager Setup", test_streaming_manager_setup),
        ("Simulated Pipeline", test_simulated_pipeline),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
                print(f"  [FAIL] {name}")
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {name}: {e}")

    print("\n" + "=" * 70)
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print("=" * 70)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)

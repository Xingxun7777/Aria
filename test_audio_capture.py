# -*- coding: utf-8 -*-
"""Test audio capture with configured device"""
import json
import numpy as np
import sounddevice as sd
import time

# Load config
config_path = "config/hotwords.json"
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

configured_device = config.get("general", {}).get("audio_device")
print(f"Configured device: '{configured_device}'")

# Find device ID
devices = sd.query_devices()
device_id = None
for i, d in enumerate(devices):
    if d["max_input_channels"] > 0:
        if configured_device and (
            d["name"] == configured_device
            or configured_device in d["name"]
            or d["name"] in configured_device
        ):
            device_id = i
            break

print(f"Using device ID: {device_id}")
print()

# Record 2 seconds of audio
print("Recording 2 seconds... SPEAK NOW!")
duration = 2
sample_rate = 16000

try:
    audio = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device_id,
    )
    sd.wait()

    # Analyze
    audio = audio.flatten()
    max_amp = np.max(np.abs(audio))
    mean_amp = np.mean(np.abs(audio))

    print(f"\nResults:")
    print(f"  Max amplitude: {max_amp:.6f}")
    print(f"  Mean amplitude: {mean_amp:.6f}")
    print(f"  Max (0-32767 scale): {int(max_amp * 32767)}")

    if max_amp < 0.01:
        print("\n[WARNING] Very low audio level - check microphone!")
    elif max_amp < 0.05:
        print("\n[INFO] Audio level is low but detectable")
    else:
        print("\n[OK] Audio level looks good!")

except Exception as e:
    print(f"Error: {e}")

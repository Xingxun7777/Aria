# -*- coding: utf-8 -*-
"""Test audio device detection"""
import json
import sounddevice as sd

# Load config
config_path = "config/hotwords.json"
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

configured_device = config.get("general", {}).get("audio_device")
print(f"Configured device: '{configured_device}'")
print()

# List all input devices
print("=== Available Input Devices ===")
devices = sd.query_devices()
for i, d in enumerate(devices):
    if d["max_input_channels"] > 0:
        is_default = " (DEFAULT)" if i == sd.default.device[0] else ""
        match = (
            " <-- MATCH"
            if configured_device
            and (configured_device in d["name"] or d["name"] in configured_device)
            else ""
        )
        print(f"  ID {i}: {d['name']}{is_default}{match}")

print()
print(f"Default input device ID: {sd.default.device[0]}")

# Try to find the configured device
if configured_device:
    found_id = None
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            if d["name"] == configured_device:
                found_id = i
                break
            if configured_device in d["name"] or d["name"] in configured_device:
                found_id = i
                break

    if found_id is not None:
        print(f"\n✓ Configured device found: ID {found_id}")
    else:
        print(f"\n✗ Configured device NOT found!")

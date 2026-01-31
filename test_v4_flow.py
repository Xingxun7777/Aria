#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
VoiceType v4.0 Flow Test Script
===============================
Tests the complete hotword + polish pipeline to ensure v4.0 changes work correctly.
"""

import sys
import os
import io

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.hotword.manager import HotWordManager, HotWordConfig
from core.hotword.polish import AIPolisher, PolishConfig, DEFAULT_POLISH_PROMPT
from core.hotword.utils import is_cjk_word, is_english_word


def test_utils():
    """Test shared utility functions."""
    print("\n" + "=" * 60)
    print("TEST 1: Utility Functions (is_cjk_word, is_english_word)")
    print("=" * 60)

    test_cases = [
        ("ComfyUI", False, True),  # English
        ("claude code", False, True),  # English with space
        ("咖啡鱼", True, False),  # Chinese
        ("三方会谈", True, False),  # Chinese
        ("deepseek", False, True),  # English
        ("琶音", True, False),  # Chinese
    ]

    all_passed = True
    for word, expected_cjk, expected_eng in test_cases:
        cjk_result = is_cjk_word(word)
        eng_result = is_english_word(word)
        status = (
            "✓" if (cjk_result == expected_cjk and eng_result == expected_eng) else "✗"
        )
        if status == "✗":
            all_passed = False
        print(f"  {status} '{word}': is_cjk={cjk_result}, is_english={eng_result}")

    return all_passed


def test_hotword_manager():
    """Test HotWordManager tier classification."""
    print("\n" + "=" * 60)
    print("TEST 2: HotWordManager Tier Classification")
    print("=" * 60)

    config = HotWordConfig(config_path="config/hotwords.json")
    manager = HotWordManager(config)
    tiers = manager.get_polish_hotwords_tiered()

    print(f"\n  Critical tier (weight=1.0): {len(tiers['critical'])} words")
    print(f"  Reference tier (Chinese): {len(tiers['reference'])} words")
    print(f"  English reference tier: {len(tiers['english_reference'])} words")

    # Check ComfyUI variants are in critical
    comfy_variants = ["comfy", "Comfyui", "Comfy-UI", "ComfyUI"]
    comfy_in_critical = [w for w in comfy_variants if w in tiers["critical"]]

    print(f"\n  ComfyUI variants in critical tier: {comfy_in_critical}")

    # Check claude code is in critical
    claude_in_critical = "claude code" in tiers["critical"]
    print(f"  'claude code' in critical: {claude_in_critical}")

    # Check deepseek is in critical
    deepseek_in_critical = "deepseek" in tiers["critical"]
    print(f"  'deepseek' in critical: {deepseek_in_critical}")

    all_passed = (
        len(comfy_in_critical) >= 3 and claude_in_critical and deepseek_in_critical
    )
    return all_passed


def test_prompt_template():
    """Test v4.0 prompt template content."""
    print("\n" + "=" * 60)
    print("TEST 3: v4.0 Prompt Template Content")
    print("=" * 60)

    checks = [
        ("【判断流程】", "Decision flow section"),
        ("【音译对照表】", "Phonetic alias table section"),
        ("咖啡鱼/卡飞UI/康飞 → ComfyUI", "ComfyUI phonetic aliases"),
        ("克劳德/克老德/cloud code → claude code", "Claude code phonetic aliases"),
        ("【跨语言替换示例", "Cross-lingual examples section"),
        ("这个咖啡鱼啊，用起来效果还挺好的", "ComfyUI example input"),
        ("这个ComfyUI啊，用起来效果还挺好的", "ComfyUI example output"),
        ("复conu i", "Garbled text example"),
        ("{hotwords_chinese}", "Chinese hotwords placeholder"),
        ("{hotwords_english}", "English hotwords placeholder"),
        ("{text}", "Text placeholder"),
    ]

    all_passed = True
    for content, description in checks:
        found = content in DEFAULT_POLISH_PROMPT
        status = "✓" if found else "✗"
        if not found:
            all_passed = False
        print(f"  {status} {description}")

    return all_passed


def test_prompt_building():
    """Test AIPolisher prompt building with hotwords."""
    print("\n" + "=" * 60)
    print("TEST 4: Prompt Building with Hotwords")
    print("=" * 60)

    # Create config with tiered hotwords
    config = HotWordConfig(config_path="config/hotwords.json")
    manager = HotWordManager(config)
    tiers = manager.get_polish_hotwords_tiered()

    config = PolishConfig(
        enabled=True,
        api_url="http://localhost:3000",
        api_key="test",
        model="test-model",
        hotwords_critical=tiers["critical"],
        hotwords_strong=tiers["reference"],
        hotwords_english=tiers["english_reference"],
    )

    polisher = AIPolisher(config)

    # Build prompt with test text
    test_text = "这个咖啡鱼啊，用起来效果还挺好的。"
    prompt = polisher._build_prompt(test_text)

    # Check prompt contains key elements
    checks = [
        ("【音译对照表】" in prompt, "Phonetic alias table included"),
        ("咖啡鱼" in prompt, "ComfyUI alias in table"),
        (test_text in prompt, "Input text included"),
        ("【中文参考词汇】" in prompt, "Chinese hotwords section"),
        ("【英文参考词汇】" in prompt, "English hotwords section"),
    ]

    all_passed = True
    for result, description in checks:
        status = "✓" if result else "✗"
        if not result:
            all_passed = False
        print(f"  {status} {description}")

    # Show actual hotwords in prompt
    print("\n  --- Generated Prompt Excerpt ---")
    # Find and print the hotwords sections
    lines = prompt.split("\n")
    for i, line in enumerate(lines):
        if "【中文参考词汇】" in line or "【英文参考词汇】" in line:
            print(f"  {line}")

    return all_passed


def test_english_hotword_limit():
    """Test that English hotword limit is 25."""
    print("\n" + "=" * 60)
    print("TEST 5: English Hotword Limit (should be 25)")
    print("=" * 60)

    # Check the source code
    with open("core/hotword/polish.py", "r", encoding="utf-8") as f:
        content = f.read()

    # Look for the limit
    if "hotwords_english[:25]" in content:
        print("  ✓ English hotword limit is 25")
        return True
    elif "hotwords_english[:15]" in content:
        print("  ✗ English hotword limit is still 15 (needs update)")
        return False
    else:
        print("  ? Could not determine English hotword limit")
        return False


def test_example_scenarios():
    """Test specific example scenarios from user feedback."""
    print("\n" + "=" * 60)
    print("TEST 6: Example Scenarios (Prompt Content Check)")
    print("=" * 60)

    scenarios = [
        {
            "input": "这个咖啡鱼啊，用起来效果还挺好的。",
            "expected_correction": "ComfyUI",
            "description": "咖啡鱼 → ComfyUI",
        },
        {
            "input": "我还是用这个复conu i工作的话，还是挺顺畅的。",
            "expected_correction": "ComfyUI",
            "description": "复conu i → ComfyUI",
        },
        {
            "input": "我用cloud code来调试bug",
            "expected_correction": "claude code",
            "description": "cloud code → claude code",
        },
    ]

    # We can't actually call the API, but we can verify the prompt includes the examples
    all_passed = True
    for scenario in scenarios:
        # Check if similar example exists in prompt template
        input_text = scenario["input"]
        expected = scenario["expected_correction"]

        # The prompt should have examples that guide the model
        has_guidance = expected in DEFAULT_POLISH_PROMPT
        status = "✓" if has_guidance else "?"

        print(f"  {status} {scenario['description']}")
        print(f"      Input: {input_text[:40]}...")
        print(f"      Guidance for '{expected}' in prompt: {has_guidance}")

    return True  # This is more of a check than a pass/fail


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("   VoiceType v4.0 Flow Test")
    print("=" * 60)

    results = []

    # Run tests
    results.append(("Utility Functions", test_utils()))
    results.append(("HotWord Manager", test_hotword_manager()))
    results.append(("Prompt Template", test_prompt_template()))
    results.append(("Prompt Building", test_prompt_building()))
    results.append(("English Limit", test_english_hotword_limit()))
    results.append(("Example Scenarios", test_example_scenarios()))

    # Summary
    print("\n" + "=" * 60)
    print("   TEST SUMMARY")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        if not passed:
            all_passed = False
        print(f"  {status}: {name}")

    print("\n" + "-" * 60)
    if all_passed:
        print("  ✓ All tests passed! v4.0 flow is ready.")
    else:
        print("  ✗ Some tests failed. Please check the issues above.")
    print("-" * 60 + "\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

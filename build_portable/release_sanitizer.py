"""
Shared release sanitization helpers.

Used by:
- build_portable/build.py for dist packaging cleanup
- build_portable/release_prep.py for source-tree pre-release cleanup
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Callable, Iterable

Logger = Callable[[str], None]

GENERIC_WAKEWORDS = ["小助手", "助手", "小白", "小朋友", "小溪"]
DEFAULT_PROMPT_TEMPLATE = (
    "你是语音转文字润色助手。请直接输出润色后的文本。\n\n"
    "【任务目标】\n"
    "1. 智能结构化：根据语义逻辑自动分段。若识别到并列、递进或对比关系，请自动调整为清晰的段落或编号列表。\n"
    "2. 基础修正：修正同音错字，补充正确标点，在中英文/数字之间添加空格。\n"
    '3. 自然流畅：删除无意义的口语填充词（如"那个"、"就是说"），但保留语气词（吗、呢、吧）。\n\n'
    "【禁止】\n"
    "- 不翻译语言（英文保持英文）\n"
    "- 不添加 Markdown 符号\n"
    "- 不过度改写原意\n\n"
    "原文：{text}"
)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rmtree_force(path: Path) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except PermissionError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def _remove_file(path: Path) -> bool:
    if not path.exists():
        return False
    path.unlink()
    return True


def _remove_matching_files(root: Path, patterns: Iterable[str]) -> int:
    removed = 0
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file() and _remove_file(path):
                removed += 1
    return removed


def _remove_backups_and_caches(search_roots: Iterable[Path]) -> tuple[int, int]:
    bak_removed = 0
    pycache_removed = 0
    seen_pycache: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for bak_file in root.rglob("*.bak"):
            if bak_file.is_file() and _remove_file(bak_file):
                bak_removed += 1
        for pycache in root.rglob("__pycache__"):
            if pycache.is_dir() and pycache not in seen_pycache:
                _rmtree_force(pycache)
                seen_pycache.add(pycache)
                pycache_removed += 1
    return bak_removed, pycache_removed


def _reset_runtime_dir(path: Path) -> None:
    if path.exists():
        for child in path.iterdir():
            if child.is_dir():
                _rmtree_force(child)
            else:
                try:
                    os.chmod(child, stat.S_IWRITE)
                except OSError:
                    pass
                try:
                    child.unlink(missing_ok=True)
                except PermissionError:
                    child.write_text("", encoding="utf-8")
    path.mkdir(parents=True, exist_ok=True)


def _sanitize_hotwords_config(config_dir: Path, logger: Logger) -> None:
    hotwords_file = config_dir / "hotwords.json"
    template_file = config_dir / "hotwords.template.json"

    if template_file.exists():
        shutil.copy2(template_file, hotwords_file)
        logger("  Reset: config/hotwords.json <- hotwords.template.json")

    config = _read_json(hotwords_file, {})

    config["hotwords"] = []
    config["hotword_weights"] = {}
    config["replacements"] = {}
    config["domain_context"] = ""
    config["personalization_rules"] = ""
    config["reply_style"] = ""
    config["app_categories"] = {}

    general = config.setdefault("general", {})
    general["hotkey"] = general.get("hotkey") or "grave"
    general["audio_device"] = ""

    polish = config.setdefault("polish", {})
    polish["enabled"] = False
    polish["api_url"] = ""
    polish["api_key"] = ""
    polish["model"] = ""
    polish["timeout"] = int(polish.get("timeout", 10) or 10)
    polish["prompt_template"] = polish.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE
    for key in (
        "api_url_backup",
        "api_key_backup",
        "model_backup",
        "slow_threshold_ms",
        "switch_after_slow_count",
    ):
        polish.pop(key, None)

    local_polish = config.setdefault("local_polish", {})
    local_polish["enabled"] = False
    local_polish["model_path"] = ""
    local_polish["n_gpu_layers"] = int(local_polish.get("n_gpu_layers", -1) or -1)
    local_polish["n_ctx"] = int(local_polish.get("n_ctx", 512) or 512)

    _write_json(hotwords_file, config)
    logger("  Sanitized: config/hotwords.json")


def _sanitize_wakeword_config(config_dir: Path, logger: Logger) -> None:
    wakeword_file = config_dir / "wakeword.json"
    config = _read_json(
        wakeword_file,
        {
            "enabled": False,
            "wakeword": "",
            "available_wakewords": GENERIC_WAKEWORDS,
            "cooldown_ms": 500,
            "commands": {},
        },
    )

    config["enabled"] = False
    config["wakeword"] = ""
    config["available_wakewords"] = GENERIC_WAKEWORDS.copy()
    config["cooldown_ms"] = int(config.get("cooldown_ms", 500) or 500)
    config.setdefault("commands", {})

    _write_json(wakeword_file, config)
    logger("  Sanitized: config/wakeword.json (blank + disabled)")


def _sanitize_commands_config(config_dir: Path, logger: Logger) -> None:
    commands_file = config_dir / "commands.json"
    config = _read_json(
        commands_file,
        {
            "enabled": False,
            "prefix": "",
            "cooldown_ms": 500,
            "commands": {},
        },
    )

    config["enabled"] = False
    config["prefix"] = ""
    config["cooldown_ms"] = int(config.get("cooldown_ms", 500) or 500)
    config.setdefault("commands", {})

    _write_json(commands_file, config)
    logger("  Sanitized: config/commands.json (blank prefix + disabled)")


def sanitize_release_tree(
    app_root: Path,
    logger: Logger,
    *,
    cache_roots: list[Path] | None = None,
) -> None:
    """
    Sanitize a source tree or packaged app tree for release.

    Args:
        app_root: Project root or packaged aria app root.
        logger: Logging callback.
        cache_roots: Roots where __pycache__ / *.bak cleanup should run.
    """
    app_root = Path(app_root)
    cache_roots = cache_roots or [app_root]

    config_dir = app_root / "config"
    data_dir = app_root / "data"
    debug_dir = app_root / "DebugLog"

    if config_dir.exists():
        _sanitize_hotwords_config(config_dir, logger)
        _sanitize_wakeword_config(config_dir, logger)
        _sanitize_commands_config(config_dir, logger)

    removed_logs = _remove_matching_files(app_root, ["*.log", "*_error.log"])
    if removed_logs:
        logger(f"  Removed: {removed_logs} root log file(s)")

    _reset_runtime_dir(debug_dir)
    logger("  Reset: DebugLog/")

    _reset_runtime_dir(data_dir / "history")
    logger("  Reset: data/history/")

    _reset_runtime_dir(data_dir / "history_txt")
    logger("  Reset: data/history_txt/")

    _reset_runtime_dir(data_dir / "insights")
    logger("  Reset: data/insights/")

    _write_json(data_dir / "reminders.json", {"version": 1, "reminders": []})
    logger("  Reset: data/reminders.json")

    _write_text(data_dir / "highlights.txt", "")
    logger("  Reset: data/highlights.txt")

    bak_removed, pycache_removed = _remove_backups_and_caches(cache_roots)
    if bak_removed:
        logger(f"  Removed: {bak_removed} backup file(s)")
    if pycache_removed:
        logger(f"  Removed: {pycache_removed} __pycache__ directorie(s)")


def verify_release_tree(app_root: Path) -> list[str]:
    """
    Verify release sanitization output.

    Returns:
        List of human-readable issues. Empty list means OK.
    """
    issues: list[str] = []
    app_root = Path(app_root)

    hotwords_file = app_root / "config" / "hotwords.json"
    hotwords = _read_json(hotwords_file, {})
    if not hotwords_file.exists():
        issues.append("missing config/hotwords.json")
    if hotwords.get("hotwords"):
        issues.append("config/hotwords.json still has hotwords")
    if hotwords.get("replacements"):
        issues.append("config/hotwords.json still has replacements")
    if hotwords.get("domain_context"):
        issues.append("config/hotwords.json still has domain_context")
    if hotwords.get("personalization_rules"):
        issues.append("config/hotwords.json still has personalization_rules")
    if hotwords.get("reply_style"):
        issues.append("config/hotwords.json still has reply_style")
    if hotwords.get("general", {}).get("audio_device"):
        issues.append("config/hotwords.json still has audio_device")
    if hotwords.get("local_polish", {}).get("model_path"):
        issues.append("config/hotwords.json still has local model_path")

    polish = hotwords.get("polish", {})
    if polish.get("api_key"):
        issues.append("config/hotwords.json still has api_key")
    if polish.get("api_key_backup"):
        issues.append("config/hotwords.json still has api_key_backup")
    if polish.get("api_url_backup"):
        issues.append("config/hotwords.json still has api_url_backup")

    hotwords_text = (
        hotwords_file.read_text(encoding="utf-8") if hotwords_file.exists() else ""
    )
    if re.search(r"sk-[A-Za-z0-9_-]{8,}", hotwords_text):
        issues.append("config/hotwords.json still matches API key pattern")

    wakeword = _read_json(app_root / "config" / "wakeword.json", {})
    if wakeword.get("enabled"):
        issues.append("config/wakeword.json is still enabled")
    if wakeword.get("wakeword"):
        issues.append("config/wakeword.json still has active wakeword")

    commands = _read_json(app_root / "config" / "commands.json", {})
    if commands.get("enabled"):
        issues.append("config/commands.json is still enabled")
    if commands.get("prefix"):
        issues.append("config/commands.json still has prefix")

    for rel_dir in ("DebugLog", "data/history", "data/history_txt", "data/insights"):
        path = app_root / rel_dir
        if not path.exists():
            issues.append(f"missing {rel_dir}/")
            continue
        children = list(path.iterdir())
        if rel_dir == "DebugLog":
            non_empty = [
                child
                for child in children
                if child.is_dir() or child.stat().st_size > 0
            ]
            if non_empty:
                issues.append(f"{rel_dir}/ still has non-empty runtime artifacts")
        elif children:
            issues.append(f"{rel_dir}/ is not empty")

    reminders = _read_json(app_root / "data" / "reminders.json", {})
    if reminders.get("reminders"):
        issues.append("data/reminders.json still has reminders")

    highlights_file = app_root / "data" / "highlights.txt"
    if highlights_file.exists() and highlights_file.read_text(encoding="utf-8").strip():
        issues.append("data/highlights.txt is not empty")

    if list(app_root.glob("*.log")):
        issues.append("root still has *.log files")

    return issues

"""
Pre-release cleanup for the source workspace.

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! WARNING: This script sanitizes the SOURCE TREE directly!                !!
!! It will DELETE: API keys, hotwords, history, debug logs, and other      !!
!! user-specific data from your local working copy.                        !!
!!                                                                         !!
!! For normal releases, use instead:                                        !!
!!   python build_portable/release_all.py                                  !!
!! which only sanitizes the DIST COPY and never touches source files.      !!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

This script is only for extreme cases where you need to manually clean the
source tree before committing (e.g., preparing a public repo push).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from release_sanitizer import sanitize_release_tree, verify_release_tree

PROJECT_ROOT = Path(__file__).parent.parent.resolve()


def log(msg: str) -> None:
    print(f"[RELEASE PREP] {msg}")


def _find_running_project_processes() -> list[dict]:
    current_pid = os.getpid()
    command = (
        f"$root = {json.dumps(str(PROJECT_ROOT))}; "
        f"$current = {current_pid}; "
        "Get-Process -Name python,pythonw -ErrorAction SilentlyContinue | "
        'Where-Object { $_.Id -ne $current -and $_.Path -and $_.Path -like "*$root*" } | '
        "Select-Object Id,ProcessName,Path | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip()
    if not output:
        return []

    data = json.loads(output)
    if isinstance(data, dict):
        return [data]
    return data


def main() -> int:
    # Interactive safety confirmation
    print()
    print("=" * 70)
    print("  WARNING: This will DELETE user data from your SOURCE directory!")
    print("  (API keys, hotwords, history, debug logs, etc.)")
    print()
    print("  For normal releases, use: python build_portable/release_all.py")
    print("=" * 70)
    print()

    confirm = input("Type YES to continue, anything else to abort: ").strip()
    if confirm != "YES":
        log("Aborted by user.")
        return 1

    cache_roots = [
        PROJECT_ROOT / "__pycache__",
        PROJECT_ROOT / "build_portable",
        PROJECT_ROOT / "config",
        PROJECT_ROOT / "core",
        PROJECT_ROOT / "system",
        PROJECT_ROOT / "ui",
    ]

    log(f"Project root: {PROJECT_ROOT}")
    running = _find_running_project_processes()
    if running:
        log("Detected running Aria processes. Close them before release prep:")
        for proc in running:
            log(
                f"  - PID {proc.get('Id')} {proc.get('ProcessName')} {proc.get('Path')}"
            )
        return 1

    log("Cleaning local logs, runtime data, and user-specific config...")
    sanitize_release_tree(PROJECT_ROOT, log, cache_roots=cache_roots)

    issues = verify_release_tree(PROJECT_ROOT)
    if issues:
        log("Verification failed:")
        for issue in issues:
            log(f"  - {issue}")
        return 1

    log("Workspace is sanitized for packaging.")
    log(
        "Next step: build_portable\\release-lite.bat or build_portable\\release-full.bat"
    )
    log("Archive creation stays as the final manual step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

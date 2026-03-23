"""
Aria One-Click Release Script
====================================
Safe, automated release pipeline: build → compress → GitHub Release.

Safety guarantee:
  - NEVER touches source tree config/data
  - Only operates on dist copy (build.py handles sanitization internally)
  - Verifies no API keys leak into the archive

Usage:
    python build_portable/release_all.py [--dry-run] [--skip-build] [--skip-upload]

    --dry-run      Print what would be done without executing
    --skip-build   Skip build step (use existing dist)
    --skip-upload  Build and compress but don't upload to GitHub
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# =============================================================================
# Paths
# =============================================================================

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
BUILD_DIR = PROJECT_ROOT / "build_portable"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
SEVEN_ZIP = Path("G:/7-Zip/7z.exe")
DIST_NAME = "Aria_release_lite"
DIST_DIR = PROJECT_ROOT / "dist_portable" / DIST_NAME

# GitHub
GH_REPO = "Xingxun7777/Aria"


# =============================================================================
# Helpers
# =============================================================================


def log(msg: str) -> None:
    print(f"[RELEASE] {msg}")


def log_phase(phase: int, title: str) -> None:
    print()
    print(f"{'=' * 60}")
    print(f"  Phase {phase}: {title}")
    print(f"{'=' * 60}")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, print it, and check for errors."""
    display = " ".join(str(c) for c in cmd)
    log(f"  $ {display}")
    return subprocess.run(cmd, check=True, **kwargs)


def read_version() -> str:
    """Read version from __init__.py."""
    init_file = PROJECT_ROOT / "__init__.py"
    text = init_file.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not match:
        raise RuntimeError(f"Cannot find __version__ in {init_file}")
    return match.group(1)


# =============================================================================
# Phase 0: Pre-flight checks
# =============================================================================


def _find_running_aria_processes() -> list[dict]:
    """Check for running Aria processes (reuses release_prep.py logic)."""
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
    return [data] if isinstance(data, dict) else data


def check_no_aria_running() -> None:
    """Abort if Aria is running (files may be locked)."""
    log("Checking for running Aria processes...")
    running = _find_running_aria_processes()
    if running:
        log("ERROR: Aria processes are running. Close them first:")
        for proc in running:
            log(
                f"  PID {proc.get('Id')} - {proc.get('ProcessName')} - {proc.get('Path')}"
            )
        sys.exit(1)
    log("  No Aria processes running.")


def check_tools() -> None:
    """Verify required tools are available."""
    log("Checking required tools...")

    # 7-Zip
    if not SEVEN_ZIP.exists():
        raise RuntimeError(f"7-Zip not found at {SEVEN_ZIP}")
    log(f"  7z: {SEVEN_ZIP}")

    # gh CLI
    result = subprocess.run(
        ["gh", "--version"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("gh CLI not found. Install: https://cli.github.com/")
    gh_ver = result.stdout.strip().split("\n")[0]
    log(f"  gh: {gh_ver}")

    # venv Python
    if not VENV_PYTHON.exists():
        raise RuntimeError(f".venv Python not found at {VENV_PYTHON}")
    log(f"  python: {VENV_PYTHON}")


def check_git_clean() -> None:
    """Warn if there are uncommitted changes (non-blocking)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    changes = result.stdout.strip()
    if changes:
        lines = changes.split("\n")
        log(f"  WARNING: {len(lines)} uncommitted change(s) in working tree")
        log("  (This is non-blocking, but the git tag will point at the last commit)")


# =============================================================================
# Phase 1: Build
# =============================================================================


def run_build() -> None:
    """Run build.py to create dist (sanitization happens inside build.py on dist copy)."""
    log("Running build.py (lite mode)...")
    run(
        [str(VENV_PYTHON), str(BUILD_DIR / "build.py"), "--dist-name", DIST_NAME],
        cwd=str(PROJECT_ROOT),
    )


def run_build_launcher_exe() -> None:
    """Compile Aria.exe launcher via PyInstaller."""
    log("Building launcher EXE...")
    run(
        [
            str(VENV_PYTHON),
            str(BUILD_DIR / "build_launcher_exe.py"),
            "--dist-name",
            DIST_NAME,
        ],
        cwd=str(PROJECT_ROOT),
    )


def verify_no_api_keys() -> None:
    """Final safety check: scan dist hotwords.json for leaked API keys."""
    log("Verifying no API keys in dist...")

    hotwords_file = DIST_DIR / "_internal" / "app" / "aria" / "config" / "hotwords.json"
    if not hotwords_file.exists():
        raise RuntimeError(f"hotwords.json not found in dist: {hotwords_file}")

    content = hotwords_file.read_text(encoding="utf-8")

    # Check for common API key patterns
    patterns = [
        (r"sk-[A-Za-z0-9_-]{8,}", "OpenAI/OpenRouter API key"),
        (r"sk-or-v1-[A-Za-z0-9]{48,}", "OpenRouter API key"),
        (r"AIza[A-Za-z0-9_-]{35}", "Google API key"),
    ]

    for pattern, desc in patterns:
        if re.search(pattern, content):
            raise RuntimeError(
                f"SECURITY: {desc} found in dist hotwords.json!\n"
                f"File: {hotwords_file}\n"
                f"Pattern: {pattern}\n"
                f"Build aborted. This should not happen — check build.py sanitization."
            )

    # Also verify key fields are empty
    config = json.loads(content)
    polish = config.get("polish", {})
    if polish.get("api_key"):
        raise RuntimeError("SECURITY: polish.api_key is not empty in dist!")

    log("  No API keys found. Safe to distribute.")


# =============================================================================
# Phase 2: Compress
# =============================================================================


def compress_7z(version: str) -> Path:
    """Compress dist directory into a .7z archive."""
    archive_name = f"Aria-v{version}-lite.7z"
    archive_path = PROJECT_ROOT / archive_name

    if archive_path.exists():
        log(f"  Removing old archive: {archive_name}")
        archive_path.unlink()

    log(f"Compressing to {archive_name}...")
    run(
        [
            str(SEVEN_ZIP),
            "a",
            "-t7z",
            "-mx=7",  # high compression (GitHub limit is 2 GiB)
            "-mmt=on",  # multi-threaded
            str(archive_path),
            str(DIST_DIR / "*"),
        ],
    )

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    log(f"  Archive: {archive_name} ({size_mb:.0f} MB)")
    return archive_path


# =============================================================================
# Phase 3: GitHub Release
# =============================================================================


def ensure_git_tag(version: str) -> None:
    """Create and push git tag if it doesn't exist."""
    tag = f"v{version}"

    # Check local tags
    result = subprocess.run(
        ["git", "tag", "-l", tag],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=False,
    )

    if tag in result.stdout.strip().split("\n"):
        log(f"  Tag {tag} already exists locally.")
        return

    log(f"  Creating tag {tag}...")
    run(["git", "tag", tag], cwd=str(PROJECT_ROOT))
    log(f"  Pushing tag {tag} to origin...")
    run(["git", "push", "origin", tag], cwd=str(PROJECT_ROOT))


def extract_changelog(version: str) -> str:
    """Extract release notes for a specific version from CHANGELOG.md."""
    changelog = PROJECT_ROOT / "CHANGELOG.md"
    if not changelog.exists():
        return f"Aria v{version}"

    text = changelog.read_text(encoding="utf-8")

    # Match ## [version] section until next ## or end
    pattern = rf"## \[{re.escape(version)}\].*?\n(.*?)(?=\n## \[|\Z)"
    match = re.search(pattern, text, re.DOTALL)

    if match:
        notes = match.group(1).strip()
        if notes:
            return notes

    return f"Aria v{version}"


def create_or_update_release(version: str) -> None:
    """Create GitHub Release if it doesn't exist."""
    tag = f"v{version}"

    # Check if release exists
    result = subprocess.run(
        ["gh", "release", "view", tag, "--repo", GH_REPO],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == 0:
        log(f"  Release {tag} already exists.")
        return

    notes = extract_changelog(version)
    log(f"  Creating release {tag}...")
    run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            GH_REPO,
            "--title",
            f"Aria v{version}",
            "--notes",
            notes,
        ],
    )


def upload_asset(version: str, archive_path: Path) -> None:
    """Upload archive to GitHub Release (overwrites existing)."""
    tag = f"v{version}"
    log(f"Uploading {archive_path.name} to release {tag}...")
    run(
        [
            "gh",
            "release",
            "upload",
            tag,
            str(archive_path),
            "--repo",
            GH_REPO,
            "--clobber",
        ],
    )
    log(f"  Uploaded: {archive_path.name}")


# =============================================================================
# Phase 4: Summary
# =============================================================================


def print_summary(version: str, archive_path: Path | None) -> None:
    """Print final summary."""
    print()
    print("=" * 60)
    print("  RELEASE COMPLETE")
    print("=" * 60)
    print(f"  Version:  v{version}")
    if archive_path and archive_path.exists():
        size_mb = archive_path.stat().st_size / (1024 * 1024)
        print(f"  Archive:  {archive_path.name} ({size_mb:.0f} MB)")
    print(f"  Release:  https://github.com/{GH_REPO}/releases/tag/v{version}")
    print()
    print("  Verify:")
    print(f"    gh release view v{version} --repo {GH_REPO}")
    print("=" * 60)


# =============================================================================
# Main
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aria one-click release: build → compress → GitHub Release"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip build step (use existing dist directory)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Build and compress but don't create/upload to GitHub",
    )
    args = parser.parse_args()

    try:
        version = read_version()
        log(f"Aria Release Pipeline v{version}")
        log(f"Source: {PROJECT_ROOT}")
        log(f"Dist:   {DIST_DIR}")

        if args.dry_run:
            log("[DRY RUN MODE]")
            print()
            log(f"Would build lite version to: {DIST_DIR}")
            log(f"Would compress to: Aria-v{version}-lite.7z")
            if not args.skip_upload:
                log(f"Would create/update GitHub release: v{version}")
                log(f"Would upload archive to: {GH_REPO}")
            return 0

        # Phase 0: Pre-flight
        log_phase(0, "Pre-flight Checks")
        check_no_aria_running()
        check_tools()
        check_git_clean()

        # Phase 1: Build
        archive_path = None
        if not args.skip_build:
            log_phase(1, "Build Lite")
            run_build()
            run_build_launcher_exe()
            verify_no_api_keys()
        else:
            log_phase(1, "Build (SKIPPED)")
            if not DIST_DIR.exists():
                raise RuntimeError(
                    f"--skip-build specified but dist not found: {DIST_DIR}"
                )
            log(f"  Using existing dist: {DIST_DIR}")
            verify_no_api_keys()

        # Phase 2: Compress
        log_phase(2, "Compress")
        archive_path = compress_7z(version)

        # Phase 3: GitHub Release
        if not args.skip_upload:
            log_phase(3, "GitHub Release")
            ensure_git_tag(version)
            create_or_update_release(version)
            upload_asset(version, archive_path)
        else:
            log_phase(3, "GitHub Release (SKIPPED)")
            log("  --skip-upload specified, skipping GitHub operations")

        # Phase 4: Summary
        print_summary(version, archive_path)
        return 0

    except subprocess.CalledProcessError as e:
        log(f"ERROR: Command failed with exit code {e.returncode}")
        if e.cmd:
            log(f"  Command: {' '.join(str(c) for c in e.cmd)}")
        return 1

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

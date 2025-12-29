"""
VoiceType Portable Build Script v2.0
====================================
Creates a portable distribution using embedded Python.
Reviewed and fixed by Codex + Gemini 三方会谈.

Usage:
    python build_portable/build.py

Output:
    dist_portable/VoiceType/  - Ready-to-distribute folder
"""

import os
import sys
import stat
import shutil
import urllib.request
import zipfile
import platform
import struct
import json
import re
from pathlib import Path

# =============================================================================
# Configuration
# =============================================================================

PYTHON_VERSION = "3.10.11"  # Match your dev environment
PYTHON_MAJOR, PYTHON_MINOR = 3, 10  # Explicit for safety

PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"

# Paths
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
BUILD_DIR = PROJECT_ROOT / "build_portable"
DIST_DIR = PROJECT_ROOT / "dist_portable" / "VoiceType"
CACHE_DIR = BUILD_DIR / ".cache"

# Source directories to copy (relative to PROJECT_ROOT)
SOURCE_DIRS = ["core", "ui", "features", "scheduler", "system", "config"]
SOURCE_FILES = [
    "app.py",
    "__init__.py",
    "__main__.py",
    "launcher.py",
    "progress_ipc.py",
]

# Data directories (assets, resources, etc.)
DATA_DIRS = ["assets", "resources", "models"]

# Directories to completely exclude from distribution
EXCLUDE_DIRS = ["DebugLog", "logs", ".git", "__pycache__"]

# File patterns to exclude (for security and privacy)
EXCLUDE_PATTERNS = [
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".git",
    "*.bak",  # Backup files (may contain secrets)
    "DebugLog",  # Debug logs with user audio/transcripts
    "*_error.log",  # Error logs with local paths
    "*.log",  # All log files
    ".env",  # Environment variables
    "*.tmp",  # Temporary files
    "*.tmp.*",  # Temporary files with extensions
]


# =============================================================================
# Helper Functions
# =============================================================================


def log(msg: str):
    print(f"[BUILD] {msg}")


def rmtree_force(path: Path):
    """Remove directory tree, handling read-only files on Windows."""

    def onerror(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def download_file(url: str, dest: Path):
    """Download a file atomically with progress indication."""
    log(f"Downloading {url}...")
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    log(f"Downloaded to {dest}")


def extract_zip(zip_path: Path, dest_dir: Path):
    """Extract a zip file."""
    log(f"Extracting {zip_path}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    log(f"Extracted to {dest_dir}")


def copy_tree(src: Path, dst: Path, ignore_patterns=None):
    """Copy directory tree, creating destination if needed."""
    if ignore_patterns:
        ignore = shutil.ignore_patterns(*ignore_patterns)
    else:
        ignore = None

    if dst.exists():
        rmtree_force(dst)
    shutil.copytree(src, dst, ignore=ignore)


# =============================================================================
# Build Steps
# =============================================================================


def step_validate_environment():
    """Step 0: Validate build environment."""
    log("Step 0: Validating build environment...")

    # Check architecture
    if struct.calcsize("P") * 8 != 64:
        raise RuntimeError("This build script is amd64-only.")

    # Warn if Python version mismatch
    current_major, current_minor = sys.version_info[:2]
    if (current_major, current_minor) != (PYTHON_MAJOR, PYTHON_MINOR):
        log(
            f"WARNING: Build Python is {current_major}.{current_minor}, "
            f"but embedded is {PYTHON_MAJOR}.{PYTHON_MINOR}. Native wheels may break!"
        )

    log("Environment OK.")


def step_prepare_dirs():
    """Step 1: Prepare directory structure."""
    log("Step 1: Preparing directories...")

    # Clean and create dist directory
    rmtree_force(DIST_DIR)

    DIST_DIR.mkdir(parents=True)
    (DIST_DIR / "_internal").mkdir()
    (DIST_DIR / "_internal" / "app").mkdir()
    (DIST_DIR / "_internal" / "Lib" / "site-packages").mkdir(parents=True)

    # Create cache dir
    CACHE_DIR.mkdir(exist_ok=True)

    log("Directories prepared.")


def step_download_python():
    """Step 2: Download embedded Python if not cached."""
    log("Step 2: Checking Python embeddable package...")

    zip_name = f"python-{PYTHON_VERSION}-embed-amd64.zip"
    zip_path = CACHE_DIR / zip_name

    if not zip_path.exists():
        download_file(PYTHON_EMBED_URL, zip_path)
    else:
        log(f"Using cached {zip_name}")

    # Extract to _internal
    internal_dir = DIST_DIR / "_internal"
    extract_zip(zip_path, internal_dir)

    # Validate extraction
    stdlib_zip = f"python{PYTHON_MAJOR}{PYTHON_MINOR}.zip"
    required = [
        f"python{PYTHON_MAJOR}{PYTHON_MINOR}.dll",
        "pythonw.exe",
        "python.exe",
        f"python{PYTHON_MAJOR}{PYTHON_MINOR}._pth",
        stdlib_zip,
    ]
    missing = [name for name in required if not (internal_dir / name).exists()]
    if missing:
        raise RuntimeError(f"Embedded Python incomplete, missing: {missing}")

    log("Python embeddable package ready and validated.")


def step_configure_pth():
    """Step 3: Configure python3xx._pth for import paths."""
    log("Step 3: Configuring Python paths...")

    internal_dir = DIST_DIR / "_internal"

    # Deterministic _pth file selection
    pth_file = internal_dir / f"python{PYTHON_MAJOR}{PYTHON_MINOR}._pth"
    if not pth_file.exists():
        raise FileNotFoundError(f"Expected {pth_file.name} in {internal_dir}")

    log(f"Found {pth_file.name}")

    # Build _pth content with correct order:
    # 1. stdlib first (prevents shadowing)
    # 2. site-packages (dependencies)
    # 3. app (our code root)
    # 4. app/voicetype (CRITICAL: allows "import core", "import ui" etc.)
    # 5. import site (enables .pth processing, must be last)
    stdlib_zip = f"python{PYTHON_MAJOR}{PYTHON_MINOR}.zip"

    pth_lines = [
        stdlib_zip,  # stdlib first
        "Lib\\site-packages",  # dependencies (use backslash for Windows)
        "app",  # app root
        "app\\voicetype",  # CRITICAL: inner package for relative imports
        "",
        "import site",  # MUST be last line
    ]

    pth_file.write_text("\n".join(pth_lines) + "\n", encoding="utf-8")
    log(f"Updated {pth_file.name}")


def step_copy_source():
    """Step 4: Copy source code to _internal/app/."""
    log("Step 4: Copying source code...")

    app_dir = DIST_DIR / "_internal" / "app"

    # Create voicetype package directory
    voicetype_dir = app_dir / "voicetype"
    voicetype_dir.mkdir(exist_ok=True)

    # Copy directories (using EXCLUDE_PATTERNS for security)
    for dir_name in SOURCE_DIRS:
        src = PROJECT_ROOT / dir_name
        if src.exists():
            dst = voicetype_dir / dir_name
            copy_tree(src, dst, ignore_patterns=EXCLUDE_PATTERNS)
            log(f"  Copied {dir_name}/")

    # Copy files
    for file_name in SOURCE_FILES:
        src = PROJECT_ROOT / file_name
        if src.exists():
            dst = voicetype_dir / file_name
            shutil.copy2(src, dst)
            log(f"  Copied {file_name}")

    # Copy data directories (assets, resources, etc.)
    for dir_name in DATA_DIRS:
        src = PROJECT_ROOT / dir_name
        if src.exists():
            dst = voicetype_dir / dir_name
            copy_tree(src, dst, ignore_patterns=EXCLUDE_PATTERNS)
            log(f"  Copied data: {dir_name}/")

    log("Source code copied.")


def step_clean_sensitive_data():
    """Step 4.5: Clean ALL user-specific and sensitive data."""
    log("Step 4.5: Cleaning sensitive and user data...")

    voicetype_dir = DIST_DIR / "_internal" / "app" / "voicetype"
    config_dir = voicetype_dir / "config"

    if not config_dir.exists():
        log("  No config directory found, skipping.")
        return

    # ==========================================================================
    # 1. Clean hotwords.json - Remove ALL user-specific data
    # ==========================================================================
    hotwords_file = config_dir / "hotwords.json"
    if hotwords_file.exists():
        try:
            with open(hotwords_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # Clean API keys
            if "polish" in config and "api_key" in config["polish"]:
                config["polish"]["api_key"] = "YOUR_API_KEY_HERE"
                log("  Cleaned: polish.api_key")

            # Clean user-specific data
            if "hotwords" in config:
                config["hotwords"] = []
                log("  Cleaned: hotwords (emptied)")

            if "hotword_weights" in config:
                config["hotword_weights"] = {}
                log("  Cleaned: hotword_weights (emptied)")

            if "replacements" in config:
                config["replacements"] = {}
                log("  Cleaned: replacements (emptied)")

            if "domain_context" in config:
                config["domain_context"] = ""
                log("  Cleaned: domain_context (emptied)")

            # Set default ASR engine to funasr (better out-of-box experience)
            config["asr_engine"] = "funasr"
            log("  Set: asr_engine = funasr (default)")

            # Set polish_mode to local (doesn't require API)
            config["polish_mode"] = "local"
            log("  Set: polish_mode = local (default)")

            # Clean hardcoded paths (replace absolute paths with relative)
            def clean_paths(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(v, str) and re.search(
                            r"[A-Z]:[/\\]", v, re.IGNORECASE
                        ):
                            if "model_path" in k.lower() or "path" in k.lower():
                                obj[k] = "./models/YOUR_MODEL_HERE"
                                log(f"  Cleaned path: {path}.{k}")
                        elif isinstance(v, (dict, list)):
                            clean_paths(v, f"{path}.{k}")
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        if isinstance(v, (dict, list)):
                            clean_paths(v, f"{path}[{i}]")

            clean_paths(config)

            # Write cleaned config
            with open(hotwords_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

            log("  Updated hotwords.json")

        except Exception as e:
            log(f"  WARNING: Failed to clean hotwords.json: {e}")

    # ==========================================================================
    # 2. Remove backup files (may contain API keys)
    # ==========================================================================
    for bak_file in config_dir.glob("*.bak"):
        bak_file.unlink()
        log(f"  Removed: {bak_file.name}")

    # ==========================================================================
    # 3. Remove ALL log files
    # ==========================================================================
    for log_file in voicetype_dir.glob("*.log"):
        log_file.unlink()
        log(f"  Removed: {log_file.name}")

    for log_file in voicetype_dir.glob("*_error.log"):
        log_file.unlink()
        log(f"  Removed: {log_file.name}")

    # ==========================================================================
    # 4. Clean DebugLog directory (session files, audio recordings, debug logs)
    # ==========================================================================
    debug_log_dir = voicetype_dir / "DebugLog"
    if debug_log_dir.exists():
        # Count files before removal
        session_count = len(list(debug_log_dir.glob("session_*.json")))
        log_count = len(list(debug_log_dir.glob("*.log")))
        audio_count = len(list(debug_log_dir.glob("*.wav")))

        rmtree_force(debug_log_dir)
        log(
            f"  Removed: DebugLog/ ({session_count} sessions, {log_count} logs, {audio_count} audio)"
        )

    # Recreate empty DebugLog directory (needed at runtime)
    debug_log_dir.mkdir(exist_ok=True)
    log("  Created: empty DebugLog/")

    # ==========================================================================
    # 5. Clean InsightStore data (user voice transcripts)
    # ==========================================================================
    insights_dir = voicetype_dir / "data" / "insights"
    if insights_dir.exists():
        insight_files = list(insights_dir.glob("*.json"))
        for f in insight_files:
            f.unlink()
        if insight_files:
            log(f"  Removed: {len(insight_files)} insight file(s) from data/insights/")

    # Recreate empty insights directory
    insights_dir.mkdir(parents=True, exist_ok=True)
    log("  Created: empty data/insights/")

    # ==========================================================================
    # 6. Remove __pycache__ directories
    # ==========================================================================
    pycache_count = 0
    for pycache in voicetype_dir.rglob("__pycache__"):
        if pycache.is_dir():
            rmtree_force(pycache)
            pycache_count += 1
    if pycache_count > 0:
        log(f"  Removed: {pycache_count} __pycache__ directories")

    log("All sensitive and user data cleaned.")


def step_copy_site_packages():
    """Step 5: Copy site-packages from current venv."""
    log("Step 5: Copying site-packages...")

    # Find venv site-packages
    venv_sp = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"
    if not venv_sp.exists():
        log("WARNING: .venv/Lib/site-packages not found!")
        log("You need to manually copy dependencies.")
        return

    dest_sp = DIST_DIR / "_internal" / "Lib" / "site-packages"

    log(f"  Source: {venv_sp}")
    log(f"  Destination: {dest_sp}")
    log("  This may take a while for large dependencies...")

    # Robust ignore filter using path parts (not substring matching)
    def ignore_filter(directory, names):
        base = Path(directory)
        ignored = []

        for name in names:
            name_lower = name.lower()

            # Always ignore Python cache
            if name == "__pycache__" or name_lower.endswith((".pyc", ".pyo")):
                ignored.append(name)
                continue

            # Skip editable install artifacts (CRITICAL for portability)
            if name_lower.endswith(".egg-link") or name_lower == "easy-install.pth":
                ignored.append(name)
                continue

            # Skip pip and wheel (not needed at runtime)
            if name_lower in {"pip", "wheel"}:
                ignored.append(name)
                continue

            # PySide6 optimizations (use path parts for cross-platform)
            try:
                rel = (base / name).relative_to(venv_sp)
                parts = [p.lower() for p in rel.parts]

                # Skip PySide6/Qt/qml
                if len(parts) >= 3 and parts[:3] == ["pyside6", "qt", "qml"]:
                    ignored.append(name)
                    continue

                # Skip PySide6/translations
                if len(parts) >= 2 and parts[:2] == ["pyside6", "translations"]:
                    ignored.append(name)
                    continue

                # Skip PySide6/examples
                if len(parts) >= 2 and parts[:2] == ["pyside6", "examples"]:
                    ignored.append(name)
                    continue

            except ValueError:
                pass  # Not relative to venv_sp, skip check

        return ignored

    rmtree_force(dest_sp)
    shutil.copytree(venv_sp, dest_sp, ignore=ignore_filter)

    # Calculate size
    total_size = sum(f.stat().st_size for f in dest_sp.rglob("*") if f.is_file())
    log(f"  Copied {total_size / 1024 / 1024:.1f} MB of packages")


def step_create_launcher():
    """Step 6: Create launcher scripts."""
    log("Step 6: Creating launcher...")

    # 1. Main CMD launcher
    cmd_content = """@echo off
cd /d "%~dp0"
start "" "_internal\\pythonw.exe" -s -m voicetype.launcher
"""
    (DIST_DIR / "VoiceType.cmd").write_text(cmd_content, encoding="utf-8")
    log("  Created VoiceType.cmd")

    # 2. VBS silent launcher (robust version with absolute paths)
    vbs_content = '''Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = base
python = base & "\\_internal\\pythonw.exe"
WshShell.Run """" & python & """ -s -m voicetype.launcher", 0, False
'''
    (DIST_DIR / "VoiceType.vbs").write_text(vbs_content, encoding="utf-8")
    log("  Created VoiceType.vbs (silent launch)")

    # 3. DEBUG launcher (shows console and errors)
    debug_content = """@echo off
cd /d "%~dp0"
echo ========================================
echo VoiceType DEBUG Mode
echo ========================================
echo Python: _internal\\python.exe
echo.
"_internal\\python.exe" -s -m voicetype.launcher
echo.
echo ========================================
echo Application exited. Press any key to close.
pause > nul
"""
    (DIST_DIR / "VoiceType_debug.bat").write_text(debug_content, encoding="utf-8")
    log("  Created VoiceType_debug.bat (debug mode)")

    log("Launchers created.")


def step_create_shortcut_generator():
    """Step 7: Create a script to generate desktop shortcut."""
    log("Step 7: Creating shortcut generator...")

    # Copy icon file to dist
    icon_src = PROJECT_ROOT / "assets" / "voicetype.ico"
    if icon_src.exists():
        icon_dst = DIST_DIR / "voicetype.ico"
        shutil.copy2(icon_src, icon_dst)
        log("  Copied voicetype.ico")
    else:
        log("  Warning: voicetype.ico not found, shortcut will use default icon")

    # PowerShell script with icon support
    ps_content = """# Run this to create a desktop shortcut
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\\Desktop\\VoiceType.lnk")
$Shortcut.TargetPath = "$PSScriptRoot\\_internal\\pythonw.exe"
$Shortcut.Arguments = "-s -m voicetype.launcher"
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.Description = "VoiceType - Local AI Voice Dictation"
# Set custom icon
$IconPath = "$PSScriptRoot\\voicetype.ico"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}
$Shortcut.Save()
Write-Host "Desktop shortcut created!"
"""
    (DIST_DIR / "CreateShortcut.ps1").write_text(ps_content, encoding="utf-8")

    # CMD wrapper for PowerShell (bypasses execution policy)
    cmd_wrapper = """@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0CreateShortcut.ps1"
pause
"""
    (DIST_DIR / "CreateShortcut.cmd").write_text(cmd_wrapper, encoding="utf-8")
    log("  Created CreateShortcut.ps1 and CreateShortcut.cmd")


def step_verify_build():
    """Step 8: Verify the build is functional."""
    log("Step 8: Verifying build...")

    internal_dir = DIST_DIR / "_internal"

    # Check critical files exist
    checks = [
        internal_dir / "pythonw.exe",
        internal_dir / "python.exe",
        internal_dir / f"python{PYTHON_MAJOR}{PYTHON_MINOR}.dll",
        internal_dir / "app" / "voicetype" / "launcher.py",
        internal_dir / "Lib" / "site-packages" / "numpy",
        internal_dir / "Lib" / "site-packages" / "torch",
    ]

    for path in checks:
        if path.exists():
            log(f"  OK: {path.name}")
        else:
            log(f"  MISSING: {path}")

    log("Verification complete.")


def step_summary():
    """Final step: Print summary."""
    log("=" * 60)
    log("BUILD COMPLETE!")
    log("=" * 60)
    log(f"Output: {DIST_DIR}")
    log("")
    log("Directory structure:")
    log("  VoiceType/")
    log("  +-- VoiceType.cmd        <- Standard launcher")
    log("  +-- VoiceType.vbs        <- Silent launcher (recommended)")
    log("  +-- VoiceType_debug.bat  <- Debug mode (shows errors)")
    log("  +-- CreateShortcut.cmd   <- Create desktop shortcut")
    log("  +-- _internal/")
    log("      +-- python.exe / pythonw.exe")
    log("      +-- app/voicetype/   <- Source code")
    log("      +-- Lib/site-packages/ <- Dependencies")
    log("")
    log("TESTING:")
    log("  1. First, run VoiceType_debug.bat to check for errors")
    log("  2. If OK, use VoiceType.vbs for silent launch")
    log("  3. Upload _internal/pythonw.exe to VirusTotal (should be 0 detections)")


# =============================================================================
# Main
# =============================================================================


def main():
    log("VoiceType Portable Build v2.0")
    log(f"Project root: {PROJECT_ROOT}")
    log("")

    try:
        step_validate_environment()
        step_prepare_dirs()
        step_download_python()
        step_configure_pth()
        step_copy_source()
        step_clean_sensitive_data()  # Clean API keys and sensitive paths
        step_copy_site_packages()
        step_create_launcher()
        step_create_shortcut_generator()
        step_verify_build()
        step_summary()

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

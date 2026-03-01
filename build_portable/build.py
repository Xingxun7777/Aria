"""
Aria Portable Build Script v2.0
====================================
Creates a portable distribution using embedded Python.
Reviewed and fixed by Codex + Gemini 三方会谈.

Usage:
    python build_portable/build.py

Output:
    dist_portable/Aria/  - Ready-to-distribute folder
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

PYTHON_VERSION = "3.12.4"  # MUST match .venv Python version exactly
PYTHON_MAJOR, PYTHON_MINOR = 3, 12  # Explicit for safety

PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"

# Paths
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
BUILD_DIR = PROJECT_ROOT / "build_portable"
DIST_DIR = PROJECT_ROOT / "dist_portable" / "Aria"
CACHE_DIR = BUILD_DIR / ".cache"
RUNTIME_EXE_NAME = "AriaRuntime.exe"

# Source directories to copy (relative to PROJECT_ROOT)
SOURCE_DIRS = ["core", "ui", "system", "config"]
SOURCE_FILES = [
    "app.py",
    "__init__.py",
    "__main__.py",
    "launcher.py",
    "progress_ipc.py",
]

# Data directories (assets, resources, etc.)
DATA_DIRS = ["assets", "resources"]

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
    """Remove directory tree, handling read-only and locked files on Windows."""

    def onerror(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except PermissionError:
            # File locked by OS (e.g. after segfault) — skip it
            log(f"  WARN: Cannot delete locked file, skipping: {p}")

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

    # Create a runtime alias so Task Manager shows a friendly process name
    try:
        pythonw_exe = internal_dir / "pythonw.exe"
        runtime_exe = internal_dir / RUNTIME_EXE_NAME
        if pythonw_exe.exists():
            shutil.copy2(pythonw_exe, runtime_exe)
            log(f"Created runtime alias: {runtime_exe.name}")
    except Exception as e:
        log(f"WARNING: Failed to create runtime alias: {e}")

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
    # 4. import site (enables .pth processing, must be last)
    stdlib_zip = f"python{PYTHON_MAJOR}{PYTHON_MINOR}.zip"

    pth_lines = [
        stdlib_zip,  # stdlib first
        ".",  # executable dir — stdlib .pyd extensions live here (_ctypes, winsound, etc.)
        "Lib\\site-packages",  # dependencies (use backslash for Windows)
        "app",  # app root (allows `import aria`)
        "",
        "import site",  # enables .pth processing in site-packages, MUST be last
    ]

    pth_file.write_text("\n".join(pth_lines) + "\n", encoding="utf-8")
    log(f"Updated {pth_file.name}")


def step_copy_source():
    """Step 4: Copy source code to _internal/app/."""
    log("Step 4: Copying source code...")

    app_dir = DIST_DIR / "_internal" / "app"

    # Create aria package directory
    aria_dir = app_dir / "aria"
    aria_dir.mkdir(exist_ok=True)

    # Copy directories (using EXCLUDE_PATTERNS for security)
    for dir_name in SOURCE_DIRS:
        src = PROJECT_ROOT / dir_name
        if src.exists():
            dst = aria_dir / dir_name
            copy_tree(src, dst, ignore_patterns=EXCLUDE_PATTERNS)
            log(f"  Copied {dir_name}/")

    # Copy files
    for file_name in SOURCE_FILES:
        src = PROJECT_ROOT / file_name
        if src.exists():
            dst = aria_dir / file_name
            shutil.copy2(src, dst)
            log(f"  Copied {file_name}")

    # Copy data directories (assets, resources, etc.)
    for dir_name in DATA_DIRS:
        src = PROJECT_ROOT / dir_name
        if src.exists():
            dst = aria_dir / dir_name
            copy_tree(src, dst, ignore_patterns=EXCLUDE_PATTERNS)
            log(f"  Copied data: {dir_name}/")

    log("Source code copied.")


def step_clean_sensitive_data():
    """Step 4.5: Clean ALL user-specific and sensitive data."""
    log("Step 4.5: Cleaning sensitive and user data...")

    aria_dir = DIST_DIR / "_internal" / "app" / "aria"
    config_dir = aria_dir / "config"

    if not config_dir.exists():
        log("  No config directory found, skipping.")
        return

    # ==========================================================================
    # 1. Replace hotwords.json with template (clean defaults for distribution)
    # ==========================================================================
    hotwords_file = config_dir / "hotwords.json"
    template_file = config_dir / "hotwords.template.json"

    if template_file.exists():
        try:
            # Use the template as the distribution config (has safe defaults)
            shutil.copy2(template_file, hotwords_file)
            log("  Replaced hotwords.json with template (clean defaults)")

            # Verify no API keys leaked
            with open(hotwords_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            api_key = config.get("polish", {}).get("api_key", "")
            if api_key and "YOUR_" not in api_key.upper():
                # Template somehow has a real key - sanitize it
                config["polish"]["api_key"] = "YOUR_OPENROUTER_API_KEY_HERE"
                with open(hotwords_file, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                log("  WARNING: Template had real API key - sanitized")

        except Exception as e:
            log(f"  WARNING: Failed to use template: {e}")
            # Fallback: try to clean existing hotwords.json
            if hotwords_file.exists():
                try:
                    with open(hotwords_file, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    if "polish" in config and "api_key" in config["polish"]:
                        config["polish"]["api_key"] = "YOUR_API_KEY_HERE"
                    config.get("hotwords", []).clear()
                    config.get("replacements", {}).clear()
                    with open(hotwords_file, "w", encoding="utf-8") as f:
                        json.dump(config, f, ensure_ascii=False, indent=2)
                    log("  Fallback: cleaned hotwords.json in place")
                except Exception as e2:
                    log(f"  WARNING: Fallback clean also failed: {e2}")
    elif hotwords_file.exists():
        log("  WARNING: No template found, cleaning hotwords.json in place")
        try:
            with open(hotwords_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            if "polish" in config and "api_key" in config["polish"]:
                config["polish"]["api_key"] = "YOUR_API_KEY_HERE"
            for key in ["hotwords", "hotword_weights", "replacements"]:
                if key in config:
                    if isinstance(config[key], list):
                        config[key] = []
                    elif isinstance(config[key], dict):
                        config[key] = {}
            if "domain_context" in config:
                config["domain_context"] = ""
            if "general" in config:
                config["general"]["audio_device"] = ""
            with open(hotwords_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            log("  Cleaned hotwords.json (no template available)")
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
    for log_file in aria_dir.glob("*.log"):
        log_file.unlink()
        log(f"  Removed: {log_file.name}")

    for log_file in aria_dir.glob("*_error.log"):
        log_file.unlink()
        log(f"  Removed: {log_file.name}")

    # ==========================================================================
    # 4. Clean DebugLog directory (session files, audio recordings, debug logs)
    # ==========================================================================
    debug_log_dir = aria_dir / "DebugLog"
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
    insights_dir = aria_dir / "data" / "insights"
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
    # 6. Reset wakeword.json to default (user-friendly wakeword)
    # ==========================================================================
    wakeword_file = config_dir / "wakeword.json"
    if wakeword_file.exists():
        try:
            with open(wakeword_file, "r", encoding="utf-8") as f:
                wakeword_config = json.load(f)

            # Set default wakeword for distribution
            wakeword_config["wakeword"] = "小助手"

            # Add to available list if not present
            if "小助手" not in wakeword_config.get("available_wakewords", []):
                wakeword_config.setdefault("available_wakewords", []).insert(
                    0, "小助手"
                )

            with open(wakeword_file, "w", encoding="utf-8") as f:
                json.dump(wakeword_config, f, ensure_ascii=False, indent=2)

            log("  Set: wakeword = 小助手 (default)")

        except Exception as e:
            log(f"  WARNING: Failed to reset wakeword.json: {e}")

    # ==========================================================================
    # 7. Reset commands.json prefix to match wakeword
    # ==========================================================================
    commands_file = config_dir / "commands.json"
    if commands_file.exists():
        try:
            with open(commands_file, "r", encoding="utf-8") as f:
                cmd_config = json.load(f)

            cmd_config["prefix"] = "小助手"

            with open(commands_file, "w", encoding="utf-8") as f:
                json.dump(cmd_config, f, ensure_ascii=False, indent=2)

            log("  Set: commands.json prefix = 小助手 (matches wakeword)")

        except Exception as e:
            log(f"  WARNING: Failed to reset commands.json: {e}")

    # ==========================================================================
    # 8. Remove __pycache__ directories
    # ==========================================================================
    pycache_count = 0
    for pycache in aria_dir.rglob("__pycache__"):
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
        raise RuntimeError(
            f".venv/Lib/site-packages not found at {venv_sp}!\n"
            "Cannot build without dependencies. Run: python -m venv .venv && pip install -r requirements.txt"
        )

    dest_sp = DIST_DIR / "_internal" / "Lib" / "site-packages"

    log(f"  Source: {venv_sp}")
    log(f"  Destination: {dest_sp}")
    log("  This may take a while for large dependencies...")

    # =========================================================================
    # Size optimization: exclude ~3 GB of build-time-only / unused content
    # =========================================================================

    # Top-level packages not used by Aria at runtime
    SKIP_PACKAGES = {
        "pip",
        "wheel",
        "gradio",
        "gradio_client",
        "cython",
        "pythonwin",
        "setuptools",
        "_distutils_hack",
        "pkg_resources",
        "numba",  # JIT compiler, not used by Aria
        "llvmlite",  # LLVM backend for numba, not used by Aria
    }

    # torch subdirectories not needed for inference
    # NOTE: Only skip dirs NOT referenced by torch/__init__.py.
    # distributed, testing, onnx, _inductor are all imported at init → MUST keep.
    TORCH_SKIP_DIRS = {
        "include",  # C++ headers (36 MB) — not Python code
        "test",  # test suite (not same as "testing" module)
        "bin",  # CLI tools (8 MB)
    }

    # PySide6 subdirectories not needed at runtime
    PYSIDE6_SKIP_DIRS = {
        "qml",
        "translations",
        "examples",
        "metatypes",
        "typesystems",
        "include",
        "scripts",
    }

    # Specific DLLs safe to remove (multi-GPU only)
    TORCH_SKIP_DLLS = {
        "cusolvermg64_11.dll",  # multi-GPU solver (150 MB)
    }

    def ignore_filter(directory, names):
        base = Path(directory)
        ignored = []

        for name in names:
            name_lower = name.lower()

            # Always ignore Python cache
            if name == "__pycache__" or name_lower.endswith((".pyc", ".pyo")):
                ignored.append(name)
                continue

            # Skip editable install artifacts and orphaned .pth files
            # (CRITICAL for portability — these reference dev machine paths)
            if name_lower.endswith(".egg-link") or name_lower == "easy-install.pth":
                ignored.append(name)
                continue

            # Skip .pth files whose backing packages are excluded
            # distutils-precedence.pth → needs _distutils_hack (excluded)
            if name_lower == "distutils-precedence.pth":
                ignored.append(name)
                continue

            # Skip .lib files — static linker libraries, never needed by Python
            # (saves ~2.7 GB, mostly dnnl.lib at 2.2 GB)
            if name_lower.endswith(".lib"):
                ignored.append(name)
                continue

            # Skip unused top-level packages
            if name_lower in SKIP_PACKAGES:
                ignored.append(name)
                continue

            # Path-based exclusions using relative parts
            try:
                rel = (base / name).relative_to(venv_sp)
                parts = [p.lower() for p in rel.parts]

                # torch: skip non-runtime subdirectories
                if len(parts) >= 2 and parts[0] == "torch":
                    if parts[1] in TORCH_SKIP_DIRS:
                        ignored.append(name)
                        continue

                # torch/lib: skip specific multi-GPU DLLs
                if (
                    len(parts) >= 3
                    and parts[0] == "torch"
                    and parts[1] == "lib"
                    and name_lower in TORCH_SKIP_DLLS
                ):
                    ignored.append(name)
                    continue

                # PySide6: skip non-runtime subdirectories
                if len(parts) >= 2 and parts[0] == "pyside6":
                    if parts[1] in PYSIDE6_SKIP_DIRS:
                        ignored.append(name)
                        continue

                # PySide6/Qt/qml (nested path)
                if len(parts) >= 3 and parts[:3] == ["pyside6", "qt", "qml"]:
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

    runtime_exe = f"_internal\\{RUNTIME_EXE_NAME}"
    if not (DIST_DIR / "_internal" / RUNTIME_EXE_NAME).exists():
        runtime_exe = "_internal\\pythonw.exe"

    # 1. Main CMD launcher
    cmd_content = f"""@echo off
cd /d "%~dp0"
start "" "{runtime_exe}" -s -m aria.launcher
"""
    (DIST_DIR / "Aria.cmd").write_text(cmd_content, encoding="utf-8")
    log("  Created Aria.cmd")

    # 2. VBS silent launcher (robust version with absolute paths)
    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = base
python = base & "\\{runtime_exe}"
WshShell.Run """" & python & """ -s -m aria.launcher", 0, False
'''
    (DIST_DIR / "Aria.vbs").write_text(vbs_content, encoding="utf-8")
    log("  Created Aria.vbs (silent launch)")

    # 3. DEBUG launcher (shows console and errors)
    debug_content = """@echo off
cd /d "%~dp0"
echo ========================================
echo Aria DEBUG Mode
echo ========================================
echo Python: _internal\\python.exe
echo.
"_internal\\python.exe" -s -m aria.launcher
echo.
echo ========================================
echo Application exited. Press any key to close.
pause > nul
"""
    (DIST_DIR / "Aria_debug.bat").write_text(debug_content, encoding="utf-8")
    log("  Created Aria_debug.bat (debug mode)")

    log("Launchers created.")


def step_create_shortcut_generator():
    """Step 7: Create a script to generate desktop shortcut."""
    log("Step 7: Creating shortcut generator...")

    runtime_exe = f"_internal\\{RUNTIME_EXE_NAME}"
    if not (DIST_DIR / "_internal" / RUNTIME_EXE_NAME).exists():
        runtime_exe = "_internal\\pythonw.exe"

    # Copy icon file to dist
    icon_src = PROJECT_ROOT / "assets" / "aria.ico"
    if icon_src.exists():
        icon_dst = DIST_DIR / "aria.ico"
        shutil.copy2(icon_src, icon_dst)
        log("  Copied aria.ico")
    else:
        log("  Warning: aria.ico not found, shortcut will use default icon")

    # PowerShell script with icon support
    ps_content = f"""# Run this to create a desktop shortcut
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\\Desktop\\Aria.lnk")
$RuntimeExe = "$PSScriptRoot\\{runtime_exe}"
if (Test-Path $RuntimeExe) {{
    $Shortcut.TargetPath = $RuntimeExe
}} else {{
    $Shortcut.TargetPath = "$PSScriptRoot\\_internal\\pythonw.exe"
}}
$Shortcut.Arguments = "-s -m aria.launcher"
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.Description = "Aria - Local AI Voice Dictation"
# Set custom icon
$IconPath = "$PSScriptRoot\\aria.ico"
if (Test-Path $IconPath) {{
    $Shortcut.IconLocation = "$IconPath,0"
}}
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
    """Step 8: Verify the build is functional (fail-fast on any issue)."""
    log("Step 8: Verifying build...")

    internal_dir = DIST_DIR / "_internal"

    # =========================================================================
    # Phase 1: Check critical files exist
    # =========================================================================
    checks = [
        internal_dir / "pythonw.exe",
        internal_dir / "python.exe",
        internal_dir / RUNTIME_EXE_NAME,
        internal_dir / f"python{PYTHON_MAJOR}{PYTHON_MINOR}.dll",
        internal_dir / "app" / "aria" / "launcher.py",
        internal_dir / "app" / "aria" / "config" / "hotwords.json",
        internal_dir / "Lib" / "site-packages" / "numpy",
        internal_dir / "Lib" / "site-packages" / "torch",
        internal_dir / "Lib" / "site-packages" / "PySide6",
    ]

    missing = []
    for path in checks:
        if path.exists():
            log(f"  OK: {path.name}")
        else:
            log(f"  MISSING: {path}")
            missing.append(str(path))

    if missing:
        raise RuntimeError(
            f"Build verification failed! Missing {len(missing)} critical files:\n"
            + "\n".join(f"  - {p}" for p in missing)
        )

    # =========================================================================
    # Phase 2: Import smoke test using the embedded Python
    # =========================================================================
    log("  Running import smoke test...")
    python_exe = internal_dir / "python.exe"
    smoke_test = (
        "import sys; "
        "import ctypes; "
        "import winsound; "
        "import PySide6.QtCore; "
        "import torch; "
        "import numpy; "
        "import qwen_asr; "
        "from aria.app import AriaApp; "
        "from aria.ui.qt.main import main; "
        "from aria.core.asr.qwen3_engine import Qwen3ASREngine; "
        "print('SMOKE_TEST_PASSED')"
    )

    import subprocess

    result = subprocess.run(
        [str(python_exe), "-s", "-c", smoke_test],
        cwd=str(DIST_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )

    if "SMOKE_TEST_PASSED" in result.stdout:
        log("  Smoke test PASSED — all critical imports work")
    else:
        error_msg = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise RuntimeError(
            f"Build smoke test FAILED! The embedded Python cannot import critical modules.\n"
            f"Exit code: {result.returncode}\n"
            f"Error: {error_msg}"
        )

    log("Verification complete.")


def step_summary():
    """Final step: Print summary."""
    log("=" * 60)
    log("BUILD COMPLETE!")
    log("=" * 60)
    log(f"Output: {DIST_DIR}")
    log("")
    log("Directory structure:")
    log("  Aria/")
    log("  +-- Aria.cmd        <- Standard launcher")
    log("  +-- Aria.vbs        <- Silent launcher (recommended)")
    log("  +-- Aria_debug.bat  <- Debug mode (shows errors)")
    log("  +-- CreateShortcut.cmd   <- Create desktop shortcut")
    log("  +-- _internal/")
    log("      +-- python.exe / pythonw.exe")
    log("      +-- app/aria/   <- Source code")
    log("      +-- Lib/site-packages/ <- Dependencies")
    log("")
    log("TESTING:")
    log("  1. First, run Aria_debug.bat to check for errors")
    log("  2. If OK, use Aria.vbs for silent launch")
    log("  3. Upload _internal/pythonw.exe to VirusTotal (should be 0 detections)")


# =============================================================================
# Main
# =============================================================================


def main():
    log("Aria Portable Build v2.0")
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

"""
Aria Portable Build Script v2.0
====================================
Creates a portable distribution using embedded Python.
Reviewed and fixed by Codex + Gemini 三方会谈.

Usage:
    python build_portable/build.py [--full] [--dist-name NAME]

Output:
    dist_portable/<NAME>/  - Ready-to-distribute folder
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
    # 5b. Clean HistoryStore data (user voice/translation/reply history)
    # ==========================================================================
    history_dir = aria_dir / "data" / "history"
    if history_dir.exists():
        history_files = list(history_dir.glob("*.jsonl"))
        for f in history_files:
            f.unlink()
        if history_files:
            log(f"  Removed: {len(history_files)} history file(s) from data/history/")
    history_dir.mkdir(parents=True, exist_ok=True)
    log("  Created: empty data/history/")

    # ==========================================================================
    # 5c. Clean OCR debug log
    # ==========================================================================
    ocr_log = aria_dir / "DebugLog" / "ocr_debug.log"
    if ocr_log.exists():
        ocr_log.unlink()
        log("  Removed: ocr_debug.log")

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
    _patch_nagisa_unicode_path_support(dest_sp)

    # Calculate size
    total_size = sum(f.stat().st_size for f in dest_sp.rglob("*") if f.is_file())
    log(f"  Copied {total_size / 1024 / 1024:.1f} MB of packages")


def _patch_nagisa_unicode_path_support(site_packages_dir: Path):
    """Patch nagisa so its bundled model can load from non-ASCII install paths."""
    tagger_py = site_packages_dir / "nagisa" / "tagger.py"
    if not tagger_py.exists():
        log("  nagisa not found, skipping Unicode-path patch")
        return

    text = tagger_py.read_text(encoding="utf-8").replace("\r\n", "\n")
    patch_marker = "ARIA_PORTABLE_NAGISA_UNICODE_FIX"
    if patch_marker in text:
        log("  nagisa Unicode-path patch already applied")
        return

    import_block = "import os\nimport re\nimport sys\n"
    patched_import_block = (
        "import hashlib\n"
        "import os\n"
        "import re\n"
        "import shutil\n"
        "import sys\n"
        "import tempfile\n"
    )
    if import_block not in text:
        raise RuntimeError(
            f"Cannot patch nagisa imports: expected block not found in {tagger_py}"
        )
    text = text.replace(import_block, patched_import_block, 1)

    base_block = (
        "base = os.path.dirname(os.path.abspath(__file__))\nsys.path.append(base)\n"
    )
    patched_base_block = """_PACKAGE_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_PACKAGE_BASE)

# ARIA_PORTABLE_NAGISA_UNICODE_FIX
def _path_is_ascii(path):
    try:
        path.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _get_windows_short_path(path):
    if os.name != "nt":
        return None

    try:
        import ctypes

        size = ctypes.windll.kernel32.GetShortPathNameW(path, None, 0)
        if size <= 0:
            return None

        buf = ctypes.create_unicode_buffer(size)
        result = ctypes.windll.kernel32.GetShortPathNameW(path, buf, size)
        if result <= 0:
            return None

        short_path = buf.value
        if short_path and os.path.exists(short_path):
            return short_path
    except Exception:
        return None

    return None


def _iter_ascii_cache_roots():
    candidates = [
        os.path.join(os.environ.get("PUBLIC", r"C:\\Users\\Public"), "Documents", "AriaRuntimeCache"),
        os.path.join(os.environ.get("ProgramData", r"C:\\ProgramData"), "AriaRuntimeCache"),
        tempfile.gettempdir(),
        os.path.join(os.environ.get("SystemRoot", r"C:\\Windows"), "Temp"),
        r"C:\\Temp",
    ]
    seen = set()

    for candidate in candidates:
        if not candidate:
            continue
        for current in (candidate, _get_windows_short_path(candidate)):
            if not current or current in seen:
                continue
            seen.add(current)
            if _path_is_ascii(current):
                yield current


def _get_safe_data_base(package_base):
    if _path_is_ascii(package_base):
        return package_base

    short_path = _get_windows_short_path(package_base)
    if short_path and _path_is_ascii(short_path):
        return short_path

    data_src = os.path.join(package_base, "data")
    if not os.path.isdir(data_src):
        return package_base

    digest = hashlib.sha1(package_base.encode("utf-8")).hexdigest()[:12]
    for cache_root in _iter_ascii_cache_roots():
        safe_base = os.path.join(cache_root, "aria_nagisa", digest)
        safe_data = os.path.join(safe_base, "data")

        try:
            model_file = os.path.join(safe_data, "nagisa_v001.model")
            if not os.path.exists(model_file):
                os.makedirs(safe_base, exist_ok=True)
                if os.path.isdir(safe_data):
                    shutil.rmtree(safe_data)
                shutil.copytree(data_src, safe_data)
            return safe_base
        except Exception:
            continue

    return package_base


_DATA_BASE = _get_safe_data_base(_PACKAGE_BASE)
"""
    if base_block not in text:
        raise RuntimeError(
            f"Cannot patch nagisa base path: expected block not found in {tagger_py}"
        )
    text = text.replace(base_block, patched_base_block, 1)

    replacements = {
        "base + '/data/nagisa_v001.dict'": "os.path.join(_DATA_BASE, 'data', 'nagisa_v001.dict')",
        "base + '/data/nagisa_v001.model'": "os.path.join(_DATA_BASE, 'data', 'nagisa_v001.model')",
        "base + '/data/nagisa_v001.hp'": "os.path.join(_DATA_BASE, 'data', 'nagisa_v001.hp')",
    }
    for old, new in replacements.items():
        if old not in text:
            raise RuntimeError(
                f"Cannot patch nagisa default model path: '{old}' not found in {tagger_py}"
            )
        text = text.replace(old, new)

    tagger_py.write_text(text, encoding="utf-8")
    log("  Patched nagisa/tagger.py for non-ASCII portable paths")


def _run_embedded_python(python_exe: Path, code: str, cwd: Path, timeout: int = 120):
    """Run code with the embedded Python and capture output consistently."""
    import subprocess

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [str(python_exe), "-s", "-c", code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


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


def step_verify_build(full_mode: bool = False):
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
        internal_dir / "Lib" / "site-packages" / "nagisa_utils.cp312-win_amd64.pyd",
        internal_dir
        / "Lib"
        / "site-packages"
        / "nagisa"
        / "data"
        / "nagisa_v001.model",
    ]

    # Full mode: verify both ASR models are bundled
    if full_mode:
        models_dir = internal_dir / "app" / "aria" / "models"
        checks.append(models_dir / "Qwen3-ASR-1.7B")
        checks.append(models_dir / "Qwen3-ASR-0.6B")

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
        "import nagisa; "
        "import torch; "
        "import numpy; "
        "from aria.core.asr.qwen3_engine import check_qwen3_installation; "
        "assert check_qwen3_installation(); "
        "from aria.app import AriaApp; "
        "from aria.ui.qt.main import main; "
        "from aria.core.asr.qwen3_engine import Qwen3ASREngine; "
        # v1.0.2 new modules
        "from aria.core.context.screen_ocr import ScreenOCR; "
        "from aria.core.history.store import HistoryStore; "
        "from aria.core.history.models import RecordType; "
        "from aria.ui.qt.workers.reply_worker import ReplyWorker; "
        "print('SMOKE_TEST_PASSED')"
    )

    import subprocess

    result = _run_embedded_python(python_exe, smoke_test, DIST_DIR, timeout=120)

    if "SMOKE_TEST_PASSED" in result.stdout:
        log("  Smoke test PASSED — all critical imports work")
    else:
        error_msg = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise RuntimeError(
            f"Build smoke test FAILED! The embedded Python cannot import critical modules.\n"
            f"Exit code: {result.returncode}\n"
            f"Error: {error_msg}"
        )

    # =========================================================================
    # Phase 3: Unicode path probe (portable users often extract to Chinese dirs)
    # =========================================================================
    log("  Running Unicode-path qwen_asr probe...")

    verify_root = BUILD_DIR / ".verify_unicode"
    link_path = verify_root / "语音Aria"

    if link_path.exists():
        subprocess.run(
            ["cmd", "/c", "rmdir", str(link_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    if verify_root.exists():
        rmtree_force(verify_root)
    verify_root.mkdir(parents=True, exist_ok=True)

    junction = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(DIST_DIR)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if junction.returncode != 0:
        raise RuntimeError(
            "Unicode-path probe setup failed.\n"
            f"Exit code: {junction.returncode}\n"
            f"Error: {junction.stderr.strip() or junction.stdout.strip() or '(no output)'}"
        )

    unicode_probe = (
        "from aria.core.asr.qwen3_engine import check_qwen3_installation; "
        "assert check_qwen3_installation(); "
        "import qwen_asr; "
        "import nagisa; "
        "print('NAGISA_PATH=' + nagisa.__file__); "
        "print('UNICODE_QWEN_IMPORT_OK')"
    )

    try:
        unicode_result = subprocess.run(
            [str(link_path / "_internal" / "python.exe"), "-s", "-c", unicode_probe],
            cwd=str(link_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    finally:
        subprocess.run(
            ["cmd", "/c", "rmdir", str(link_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        rmtree_force(verify_root)

    if "UNICODE_QWEN_IMPORT_OK" in unicode_result.stdout:
        log(
            "  Unicode-path probe PASSED — qwen_asr imports correctly from Chinese paths"
        )
    else:
        error_msg = (
            unicode_result.stderr.strip()
            or unicode_result.stdout.strip()
            or "(no output)"
        )
        raise RuntimeError(
            "Unicode-path qwen_asr probe FAILED!\n"
            f"Exit code: {unicode_result.returncode}\n"
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
    log(f"  {DIST_DIR.name}/")
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


def _ensure_qwen3_models(models_src: Path):
    """Ensure both Qwen3-ASR models (1.7B + 0.6B) exist locally.

    For --full builds, BOTH models must be bundled so the portable version
    works offline regardless of user's GPU (auto selects 1.7B or 0.6B based
    on VRAM). If a model is missing locally, download it from HuggingFace.
    """
    import os

    # HuggingFace mirror for China users (same logic as launcher.py)
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    if "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    REQUIRED_MODELS = [
        ("Qwen/Qwen3-ASR-1.7B", "Qwen3-ASR-1.7B"),
        ("Qwen/Qwen3-ASR-0.6B", "Qwen3-ASR-0.6B"),
    ]

    models_src.mkdir(parents=True, exist_ok=True)

    for repo_id, local_name in REQUIRED_MODELS:
        local_path = models_src / local_name
        has_model = local_path.is_dir() and any(local_path.glob("*.safetensors"))

        if has_model:
            log(f"  Model already exists: {local_name}")
            continue

        log(f"  Model missing: {local_name}, downloading from {repo_id}...")
        log(f"  (This may take a while for large models)")

        try:
            from huggingface_hub import snapshot_download

            snapshot_download(
                repo_id,
                local_dir=str(local_path),
            )
            # Verify download
            if any(local_path.glob("*.safetensors")):
                size_gb = sum(
                    f.stat().st_size for f in local_path.rglob("*") if f.is_file()
                ) / (1024**3)
                log(f"  Downloaded: {local_name} ({size_gb:.1f} GB)")
            else:
                raise RuntimeError(
                    f"Download completed but no .safetensors found in {local_path}"
                )
        except Exception as e:
            log(f"  ERROR: Failed to download {repo_id}: {e}")
            raise RuntimeError(
                f"Cannot download {repo_id}. Check network and try again.\n"
                f"You can also manually download it:\n"
                f"  huggingface-cli download {repo_id} --local-dir {local_path}"
            ) from e


def step_bundle_models():
    """Step 4.6 (optional): Bundle ASR models for offline / full distribution."""
    log("Step 4.6: Bundling local models...")

    models_src = PROJECT_ROOT / "models"
    models_dst = DIST_DIR / "_internal" / "app" / "aria" / "models"

    # Ensure both Qwen3-ASR models exist (download if missing)
    _ensure_qwen3_models(models_src)

    if not models_src.exists():
        log("  No models/ directory found, skipping.")
        return

    bundled = 0
    for model_dir in models_src.iterdir():
        # Skip cache directories
        if model_dir.name.startswith("."):
            continue

        if not model_dir.is_dir():
            # Copy loose files (e.g., GGUF models for local_polish)
            dst_file = models_dst / model_dir.name
            models_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(model_dir, dst_file)
            size_mb = model_dir.stat().st_size / (1024 * 1024)
            log(f"  Copied model file: {model_dir.name} ({size_mb:.0f} MB)")
            bundled += 1
            continue

        # Check if directory has model files (safetensors, bin, gguf)
        has_model = any(
            model_dir.glob(pattern)
            for pattern in ["*.safetensors", "*.bin", "*.gguf", "*.onnx"]
        )
        if not has_model:
            log(f"  Skipping {model_dir.name} (no model files found)")
            continue

        dst = models_dst / model_dir.name
        dst.mkdir(parents=True, exist_ok=True)

        # Copy all files in model directory (skip .cache subdirs)
        size_total = 0
        for f in model_dir.rglob("*"):
            if f.is_file() and ".cache" not in f.parts:
                rel = f.relative_to(model_dir)
                dst_file = dst / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst_file)
                size_total += f.stat().st_size

        size_gb = size_total / (1024**3)
        log(f"  Bundled: {model_dir.name} ({size_gb:.1f} GB)")
        bundled += 1

    if bundled == 0:
        log("  No models to bundle.")
    else:
        log(f"  Total models bundled: {bundled}")


def main():
    import argparse

    global DIST_DIR

    parser = argparse.ArgumentParser(description="Aria Portable Build")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Bundle ASR models for offline distribution (傻瓜版)",
    )
    parser.add_argument(
        "--dist-name",
        default="Aria",
        help="Output directory name under dist_portable/ (default: Aria)",
    )
    args = parser.parse_args()

    dist_name = args.dist_name.strip()
    if not dist_name or dist_name in {".", ".."}:
        parser.error("--dist-name must be a non-empty directory name")
    if Path(dist_name).name != dist_name:
        parser.error("--dist-name must be a single directory name, not a path")

    DIST_DIR = PROJECT_ROOT / "dist_portable" / dist_name

    log("Aria Portable Build v2.0")
    log(f"Project root: {PROJECT_ROOT}")
    log(f"Output dir: {DIST_DIR}")
    if args.full:
        log("Mode: FULL (with bundled models)")
    else:
        log("Mode: LITE (no models, download on first run)")
    log("")

    try:
        step_validate_environment()
        step_prepare_dirs()
        step_download_python()
        step_configure_pth()
        step_copy_source()
        step_clean_sensitive_data()  # Clean API keys and sensitive paths
        if args.full:
            step_bundle_models()  # Bundle ASR models for offline use
        step_copy_site_packages()
        step_create_launcher()
        step_create_shortcut_generator()
        step_verify_build(full_mode=args.full)
        step_summary()

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Runtime import workarounds for third-party packages.

Currently handles a Windows-specific qwen_asr/nagisa issue:

- qwen_asr imports qwen3_forced_aligner at module import time
- qwen3_forced_aligner imports nagisa
- nagisa instantiates Tagger() at import time
- nagisa uses dyNET to populate nagisa_v001.model
- dyNET fails to read that model from non-ASCII Windows paths

Portable builds are commonly extracted into Chinese/Japanese/Korean folders,
so we mirror nagisa into an ASCII-only cache directory before qwen_asr is
imported.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _is_ascii_only(path: str | Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _find_nagisa_package() -> tuple[Path, Path]:
    spec = importlib.util.find_spec("nagisa")
    if spec is None or spec.origin is None:
        raise ImportError("nagisa is not installed")

    # IMPORTANT: do NOT call .resolve() here.
    # On Windows, portable builds are often launched through a Unicode junction
    # path. Path.resolve() collapses that junction back to the original ASCII
    # target path, which would hide the very Unicode-path problem we are trying
    # to detect.
    pkg_dir = Path(spec.origin).parent
    site_packages_dir = pkg_dir.parent

    version_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    candidates = sorted(site_packages_dir.glob("nagisa_utils*.pyd"))
    pyd_path = next(
        (candidate for candidate in candidates if version_tag in candidate.name.lower()),
        None,
    )
    if pyd_path is None and candidates:
        pyd_path = candidates[0]
    if pyd_path is None:
        raise ImportError(
            f"nagisa_utils*.pyd not found next to nagisa package: {site_packages_dir}"
        )

    return pkg_dir, pyd_path


def _pick_ascii_cache_base() -> Path:
    candidates: list[Path] = []

    public_dir = os.environ.get("PUBLIC")
    if public_dir:
        candidates.append(Path(public_dir) / "Documents" / "AriaRuntimeCache")

    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.append(Path(program_data) / "AriaRuntimeCache")

    candidates.append(Path(tempfile.gettempdir()) / "AriaRuntimeCache")

    for candidate in candidates:
        if not _is_ascii_only(candidate):
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError:
            continue

    raise RuntimeError(
        "No writable ASCII-only cache directory is available for the qwen_asr/nagisa workaround."
    )


def _build_nagisa_mirror_key(pkg_dir: Path, pyd_path: Path) -> str:
    tracked_files = [
        pkg_dir / "__init__.py",
        pkg_dir / "tagger.py",
        pkg_dir / "model.py",
        pkg_dir / "data" / "nagisa_v001.dict",
        pkg_dir / "data" / "nagisa_v001.hp",
        pkg_dir / "data" / "nagisa_v001.model",
        pyd_path,
    ]

    digest = hashlib.sha1()
    for tracked in tracked_files:
        digest.update(str(tracked).encode("utf-8"))
        if tracked.exists():
            stat = tracked.stat()
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()[:16]


def _mirror_nagisa_package(pkg_dir: Path, pyd_path: Path) -> Path:
    cache_base = _pick_ascii_cache_base()
    cache_root = cache_base / f"py{sys.version_info.major}{sys.version_info.minor}"
    mirror_root = cache_root / f"nagisa-{_build_nagisa_mirror_key(pkg_dir, pyd_path)}"
    mirror_pkg = mirror_root / "nagisa"
    mirror_pyd = mirror_root / pyd_path.name

    if mirror_pkg.exists() and mirror_pyd.exists():
        return mirror_root

    mirror_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = mirror_root.parent / f"{mirror_root.name}.tmp-{os.getpid()}"
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)

    try:
        shutil.copytree(
            pkg_dir,
            staging_root / "nagisa",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        shutil.copy2(pyd_path, staging_root / pyd_path.name)

        try:
            staging_root.rename(mirror_root)
        except FileExistsError:
            shutil.rmtree(staging_root, ignore_errors=True)
        except OSError:
            if not mirror_root.exists():
                raise
            shutil.rmtree(staging_root, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise

    if not mirror_pkg.exists() or not mirror_pyd.exists():
        raise RuntimeError(f"Failed to build nagisa mirror at {mirror_root}")

    return mirror_root


def prepare_qwen_asr_import() -> Path | None:
    """
    Ensure qwen_asr can be imported safely from Windows non-ASCII install paths.

    Returns:
        Path to the ASCII nagisa mirror when the workaround is active, else None.
    """

    pkg_dir, pyd_path = _find_nagisa_package()

    if _is_ascii_only(pkg_dir) and _is_ascii_only(pyd_path):
        return None

    mirror_root = _mirror_nagisa_package(pkg_dir, pyd_path)
    mirror_root_str = str(mirror_root)

    loaded_nagisa = sys.modules.get("nagisa")
    if loaded_nagisa is not None:
        loaded_path = getattr(loaded_nagisa, "__file__", "")
        if not _is_ascii_only(loaded_path):
            for name in list(sys.modules):
                if name == "nagisa_utils" or name.startswith("nagisa"):
                    sys.modules.pop(name, None)

    if mirror_root_str not in sys.path:
        sys.path.insert(0, mirror_root_str)

    os.environ["ARIA_NAGISA_ASCII_MIRROR"] = mirror_root_str
    return mirror_root

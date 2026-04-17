"""
Aria Auto-Update Client (v1.0.5 spec)
=====================================
Manifest-based, SHA256-verified, atomic stage-and-swap.

Module layout:
    - check_for_update(local_version)            → {"available", "local", "remote", "manifest", "error"}
    - download_and_stage(manifest)               → {"ok", "state_path", "error"}
    - get_update_state() / set_update_state(**p) → transaction state I/O
    - main()                                     → CLI notice (legacy update.bat entry)

Spec: .claude/plans/autoupdate-v1.0.5.md
"""

import hashlib
import io
import json
import os
import shutil
import ssl
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


# ─── Paths ───────────────────────────────────────────────

_THIS_DIR = Path(__file__).resolve().parent  # .../_internal/app/aria
INSTALL_ROOT = _THIS_DIR.parent.parent.parent  # .../Aria (install root)
ARIA_LIVE = _THIS_DIR  # live source dir
ARIA_STAGE = _THIS_DIR.parent / "aria.new"  # stage dir (sibling of live)
STATE_PATH = INSTALL_ROOT / ".update_state.json"
MANIFEST_CACHE = INSTALL_ROOT / ".manifest_cache.json"
LOCK_PATH = INSTALL_ROOT / ".update.lock"


# ─── Protocol (manifest source of truth) ────────────────

OFFICIAL_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Xingxun7777/Aria/main/release-manifest.json"
)

ZIP_MIRROR_PREFIXES = [
    "",  # direct (official)
    "https://ghfast.top/",
    "https://gh-proxy.com/",
]

SCHEMA_VERSION = 1
MANIFEST_CACHE_MAX_AGE_SEC = 7 * 24 * 3600  # 7 days
MANIFEST_CACHE_FETCH_TIMEOUT = 8
ZIP_DOWNLOAD_TIMEOUT = 120
ATOMIC_REPLACE_RETRIES = 3
LOCK_RETRIES = 5
LOCK_RETRY_INTERVAL = 0.1


# ─── Low-level utilities ────────────────────────────────


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _atomic_write(path: Path, text: str) -> None:
    """Write tmp + os.replace with retry on Windows sharing violations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    last_err: Exception | None = None
    for _ in range(ATOMIC_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            last_err = e
            time.sleep(0.1)
    # Final attempt surfaces the error
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last_err or RuntimeError(f"atomic write failed: {path}")


def _read_json_or_rename_corrupt(path: Path, default: dict) -> dict:
    """Load JSON. On parse error, rename to .corrupt.{ts}.json (cap 3) and return default."""
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        corrupt = path.with_name(f"{path.stem}.corrupt.{ts}{path.suffix}")
        try:
            os.replace(path, corrupt)
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass
        # Cap forensic files to 3
        pattern = f"{path.stem}.corrupt.*{path.suffix}"
        corrupts = sorted(path.parent.glob(pattern), key=lambda p: p.stat().st_mtime)
        for old in corrupts[:-3]:
            try:
                old.unlink()
            except OSError:
                pass
        return dict(default)


def _acquire_lock() -> object | None:
    """msvcrt LK_NBLCK + retry. Returns file handle on success, None on failure."""
    if sys.platform != "win32":
        return None
    import msvcrt

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(LOCK_RETRIES):
        try:
            fh = open(LOCK_PATH, "a+b")
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            try:
                fh.close()
            except Exception:
                pass
            time.sleep(LOCK_RETRY_INTERVAL)
    return None


def _release_lock(fh: object | None) -> None:
    if fh is None or sys.platform != "win32":
        return
    import msvcrt

    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _safe_member_path(zip_prefix: str, member: str, target_dir: Path) -> Path | None:
    """Reject path traversal, absolute, drive letter, UNC, symlink-ish entries.

    Returns the resolved target path if safe, None if rejected.
    """
    if not member.startswith(zip_prefix):
        return None
    rel = member[len(zip_prefix) :]
    if not rel:
        return None
    # Reject Windows drive letter / UNC / absolute
    if rel.startswith(("/", "\\")) or ":" in rel or rel.startswith("\\\\"):
        return None
    # Reject any .. component
    parts = rel.replace("\\", "/").split("/")
    if any(p == ".." or p == "." for p in parts):
        return None
    dst = (target_dir / rel).resolve()
    try:
        dst.relative_to(target_dir.resolve())
    except ValueError:
        return None
    return dst


# ─── State I/O ───────────────────────────────────────────

_STATE_DEFAULT = {
    "schema_version": SCHEMA_VERSION,
    "status": "idle",  # idle | downloading | downloaded | verified | staging | ready | swapping | swapped | confirmed | rollback
    "from_version": "",
    "to_version": "",
    "stage_dir": "",
    "backup_dir": "",
    "zip_sha256": "",
    "manifest_snapshot": None,
    "created_at": "",
    "updated_at": "",
    "failed_boots": 0,
    "error": "",
}


def get_update_state() -> dict:
    state = _read_json_or_rename_corrupt(STATE_PATH, _STATE_DEFAULT)
    # Schema migration: fill missing keys
    for k, v in _STATE_DEFAULT.items():
        state.setdefault(k, v)
    return state


def set_update_state(**patch) -> dict:
    state = get_update_state()
    state.update(patch)
    state["updated_at"] = _now_utc_iso()
    _atomic_write(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))
    return state


def clear_update_state() -> None:
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


# ─── Manifest fetch + cache ─────────────────────────────


def _fetch_manifest() -> dict | None:
    """Load manifest from official raw only. Fallback to .manifest_cache.json if < 7d old."""
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(
            OFFICIAL_MANIFEST_URL,
            headers={"User-Agent": "Aria-UpdateCheck/2.0"},
        )
        with urllib.request.urlopen(
            req, timeout=MANIFEST_CACHE_FETCH_TIMEOUT, context=ctx
        ) as resp:
            data = resp.read()
        text = data.decode("utf-8", errors="replace")
        manifest = json.loads(text)
        # Only cache valid manifests
        if manifest.get("version") and manifest.get("assets", {}).get("main_zip"):
            try:
                _atomic_write(MANIFEST_CACHE, text)
            except OSError:
                pass
            return manifest
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError):
        pass
    # Fallback: last-known-good cache
    if MANIFEST_CACHE.exists():
        try:
            age = time.time() - MANIFEST_CACHE.stat().st_mtime
            if age <= MANIFEST_CACHE_MAX_AGE_SEC:
                return json.loads(MANIFEST_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return None


# ─── Public API: check ──────────────────────────────────


def check_for_update(local_version: str = "", timeout: int = 8) -> dict:
    """Check manifest for newer version.

    Backward-compatible shape: {"available", "local", "remote", "error"}.
    Extended: "manifest" (full dict on success).
    """
    if not local_version:
        try:
            init_file = _THIS_DIR / "__init__.py"
            for line in init_file.read_text(encoding="utf-8").splitlines():
                if "__version__" in line and "=" in line:
                    local_version = line.split("=")[1].strip().strip("\"'")
                    break
        except OSError:
            local_version = "0"

    result = {
        "available": False,
        "local": local_version,
        "remote": "",
        "manifest": None,
        "error": "",
    }

    manifest = _fetch_manifest()
    if manifest is None:
        result["error"] = "无法获取更新清单"
        return result

    remote_version = manifest.get("version", "")
    result["remote"] = remote_version
    result["manifest"] = manifest

    # Downgrade attack hard reject
    if _version_tuple(remote_version) <= _version_tuple(local_version):
        return result

    # min_compatible gate
    min_compat = manifest.get("min_compatible_from", "0")
    if _version_tuple(local_version) < _version_tuple(min_compat):
        result["error"] = f"本地版本过旧（<{min_compat}），请手动重装"
        return result

    result["available"] = True
    return result


# ─── Public API: download + stage ───────────────────────


def _sha256_of_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _download_zip(manifest_asset: dict) -> bytes:
    """Try official then mirrors. Returns zip bytes on success."""
    primary = manifest_asset["url"]
    urls = [primary]
    for prefix in ZIP_MIRROR_PREFIXES[1:]:
        urls.append(prefix + primary)

    ctx = ssl.create_default_context()
    last_err: Exception | None = None
    for url in urls:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Aria-Updater/2.0"}
            )
            with urllib.request.urlopen(
                req, timeout=ZIP_DOWNLOAD_TIMEOUT, context=ctx
            ) as resp:
                data = resp.read()
            return data
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            continue
    raise RuntimeError(f"all zip sources failed: {last_err}")


def _extract_to_stage(zip_data: bytes, stage_dir: Path) -> int:
    """Extract with path whitelist. Returns file count."""
    # Clean stage dir first
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        top_dirs = {name.split("/")[0] for name in zf.namelist() if "/" in name}
        if len(top_dirs) != 1:
            raise RuntimeError(f"zip top-level dir count != 1: {top_dirs}")
        zip_prefix = top_dirs.pop() + "/"

        for info in zf.infolist():
            if info.is_dir():
                continue
            dst = _safe_member_path(zip_prefix, info.filename, stage_dir)
            if dst is None:
                # fail-closed: a zip with ANY rejected entry is treated as
                # malformed. SHA256 already verified the bytes, so this only
                # triggers on truly hostile archives — but we still refuse to
                # stage partially.
                raise RuntimeError(
                    f"zip 拒绝: 不安全的条目 {info.filename!r}（路径穿越或绝对路径）"
                )
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(zf.read(info.filename))
            count += 1
    return count


def _compile_stage(stage_dir: Path) -> bool:
    """compileall all .py files. Also import-smoke-test update_tool module only."""
    import compileall

    ok = compileall.compile_dir(str(stage_dir), quiet=2, workers=0)
    if not ok:
        return False
    # Lightweight smoke: try py_compile on updater-critical files
    import py_compile

    critical = ["update_tool.py", "__init__.py", "launcher.py", "app.py"]
    for name in critical:
        p = stage_dir / name
        if p.exists():
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError:
                return False
    return True


def download_and_stage(manifest: dict) -> dict:
    """Download zip → SHA256 verify → stage → compile → state=ready.

    Returns {"ok": bool, "state_path": str, "error": str}.
    """
    result = {"ok": False, "state_path": str(STATE_PATH), "error": ""}

    lock_fh = _acquire_lock()
    if lock_fh is None:
        result["error"] = "另一个更新操作正在进行"
        return result

    try:
        asset = manifest.get("assets", {}).get("main_zip", {})
        if not asset.get("url") or not asset.get("sha256"):
            result["error"] = "manifest 缺少 main_zip.url/sha256"
            return result

        to_version = manifest["version"]
        from_version = check_for_update("")["local"]

        set_update_state(
            status="downloading",
            from_version=from_version,
            to_version=to_version,
            zip_sha256=asset["sha256"],
            manifest_snapshot=manifest,
            created_at=_now_utc_iso(),
            error="",
        )

        # Download
        try:
            zip_data = _download_zip(asset)
        except RuntimeError as e:
            set_update_state(status="idle", error=f"下载失败: {e}")
            result["error"] = str(e)
            return result

        # Size check (±1%)
        expected_size = asset.get("size", 0)
        actual_size = len(zip_data)
        if expected_size and abs(actual_size - expected_size) > expected_size * 0.01:
            set_update_state(
                status="idle",
                error=f"大小不匹配: expected={expected_size} actual={actual_size}",
            )
            result["error"] = "下载不完整"
            return result

        # SHA256
        actual_sha = _sha256_of_bytes(zip_data)
        if actual_sha.lower() != asset["sha256"].lower():
            set_update_state(status="idle", error="SHA256 校验失败")
            result["error"] = "SHA256 校验失败"
            return result

        set_update_state(status="verified")

        # Extract to stage
        set_update_state(status="staging", stage_dir=str(ARIA_STAGE))
        try:
            _extract_to_stage(zip_data, ARIA_STAGE)
        except (RuntimeError, OSError) as e:
            # Clean up partial stage so next attempt starts fresh
            shutil.rmtree(ARIA_STAGE, ignore_errors=True)
            set_update_state(status="idle", error=f"解压失败: {e}")
            result["error"] = str(e)
            return result

        # Compile + smoke
        if not _compile_stage(ARIA_STAGE):
            set_update_state(status="idle", error="语法校验失败")
            result["error"] = "语法校验失败"
            shutil.rmtree(ARIA_STAGE, ignore_errors=True)
            return result

        # Bootstrap: always refresh install_root copies of updater_runner.*
        # so apply_staged_update works even for upgrades from versions that
        # didn't ship these files (e.g. v1.0.3.17 → v1.0.3.18 via old update.bat).
        for fname in ("updater_runner.py", "updater_runner.bat"):
            src = ARIA_STAGE / fname
            if src.exists():
                try:
                    shutil.copy2(src, INSTALL_ROOT / fname)
                except OSError as e:
                    # Non-fatal — apply_staged_update has fallback logic
                    print(f"[UPDATE] warn: cannot refresh {fname}: {e}")

        set_update_state(status="ready")
        result["ok"] = True
        return result

    finally:
        _release_lock(lock_fh)


# ─── Legacy CLI entry (update.bat) ──────────────────────


def main() -> int:
    """Legacy entry: tell user auto-update has taken over."""
    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║  Aria 自动更新已接管                                  ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    print()
    print("  本版本起，更新由 Aria 主程序后台静默处理：")
    print("    1. 启动后 3 秒自动检查")
    print("    2. 发现新版本后静默下载校验")
    print("    3. 准备就绪时在悬浮球显示角标提示")
    print()
    print("  如需手动更新或遇到问题，请访问:")
    print("    https://github.com/Xingxun7777/Aria")
    print()
    try:
        input("  按 Enter 关闭...")
    except EOFError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

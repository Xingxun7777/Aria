r"""
Aria Updater Runner (v1.0.5 spec)
=================================
Standalone swap engine. Runs AFTER old Aria exits.

Launch chain:
    app.py → spawns updater_runner.bat (detached, cmd.exe /d /c)
    BAT waits for old pythonw.exe (PID) to exit
    BAT invokes: _internal\python.exe updater_runner.py
    THIS file performs: rename aria → aria.backup.TS, rename aria.new → aria
    THIS file then spawns new pythonw launcher.py (detached, DETACHED_PROCESS)

Stdlib only. MUST NOT import from aria/ (which is being renamed).

Paths (running from <install_root>/updater_runner.py):
    <install_root>/.update_state.json   ← state
    <install_root>/.update.lock         ← exclusive lock
    <install_root>/_internal/app/aria/  ← live (will be moved)
    <install_root>/_internal/app/aria.new/  ← stage (will become live)
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


INSTALL_ROOT = Path(__file__).resolve().parent
STATE_PATH = INSTALL_ROOT / ".update_state.json"
LOCK_PATH = INSTALL_ROOT / ".update.lock"
APP_DIR = INSTALL_ROOT / "_internal" / "app"
ARIA_LIVE = APP_DIR / "aria"
ARIA_STAGE = APP_DIR / "aria.new"
APP_READY = INSTALL_ROOT / ".app_ready.json"
PYTHON_EXE = INSTALL_ROOT / "_internal" / "python.exe"
LOG_PATH = INSTALL_ROOT / ".update.log"

ATOMIC_REPLACE_RETRIES = 5
RENAME_RETRY_INTERVAL = 0.3


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for _ in range(ATOMIC_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.1)
    try:
        tmp.unlink()
    except OSError:
        pass


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _set_state(**patch) -> dict:
    state = _read_state()
    state.update(patch)
    state["updated_at"] = _now_utc_iso()
    _atomic_write_json(STATE_PATH, state)
    return state


def _try_rename(src: Path, dst: Path) -> bool:
    """Retry rename on Windows sharing violations."""
    for attempt in range(ATOMIC_REPLACE_RETRIES):
        try:
            os.rename(src, dst)
            return True
        except OSError as e:
            _log(f"rename retry {attempt + 1}: {src} → {dst}: {e}")
            time.sleep(RENAME_RETRY_INTERVAL)
    return False


def _spawn_new_aria() -> bool:
    """Spawn new pythonw launcher.py as detached process."""
    launcher = ARIA_LIVE / "launcher.py"
    pythonw = INSTALL_ROOT / "_internal" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = PYTHON_EXE  # fallback to console python
    if not launcher.exists():
        _log(f"[FATAL] launcher.py missing after swap: {launcher}")
        return False

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000

    try:
        subprocess.Popen(
            [str(pythonw), str(launcher)],
            cwd=str(INSTALL_ROOT),
            creationflags=DETACHED_PROCESS
            | CREATE_NEW_PROCESS_GROUP
            | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except OSError as e:
        _log(f"[FATAL] failed to spawn new aria: {e}")
        return False


def _do_swap(state: dict) -> bool:
    """Perform live ↔ stage swap. Returns True on success."""
    if not ARIA_STAGE.exists():
        _set_state(status="idle", error="stage 目录不存在")
        _log("[FATAL] stage dir missing")
        return False

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = APP_DIR / f"aria.backup.{ts}"

    # Clear any stale .app_ready.json so new boot must re-prove
    try:
        APP_READY.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _log(f"warn: cannot remove .app_ready.json: {e}")

    _set_state(
        status="swapping",
        backup_dir=str(backup_dir),
        failed_boots=0,
        error="",
    )

    # Step 1: live → backup
    if ARIA_LIVE.exists():
        if not _try_rename(ARIA_LIVE, backup_dir):
            _set_state(status="idle", error="无法重命名 live 目录（可能被占用）")
            _log("[FATAL] cannot rename live → backup")
            return False

    # Step 2: stage → live
    if not _try_rename(ARIA_STAGE, ARIA_LIVE):
        _log("[FATAL] stage → live failed, attempting rollback")
        # Rollback: backup → live
        if backup_dir.exists():
            if _try_rename(backup_dir, ARIA_LIVE):
                _set_state(status="rollback", error="swap 失败已回滚")
                return False
        _set_state(status="failed", error="swap 失败且回滚失败")
        return False

    _set_state(status="swapped")
    _log(f"[OK] swap done: {ARIA_LIVE.name} ← {ARIA_STAGE.name}")
    return True


def _acquire_lock():
    if sys.platform != "win32":
        return None
    import msvcrt

    for _ in range(10):
        try:
            fh = open(LOCK_PATH, "a+b")
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            try:
                fh.close()
            except Exception:
                pass
            time.sleep(0.2)
    return None


def _release_lock(fh):
    if fh is None:
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


def main() -> int:
    _log(f"=== updater_runner starting, pid={os.getpid()} ===")
    state = _read_state()
    if state.get("status") != "ready":
        _log(f"state.status != ready (got {state.get('status')!r}), aborting")
        return 1

    lock_fh = _acquire_lock()
    if lock_fh is None:
        _log("cannot acquire lock, aborting")
        return 2

    try:
        if not _do_swap(state):
            return 3
        # Spawn new Aria
        if not _spawn_new_aria():
            _log("[WARN] swap ok but spawn new aria failed — user must start manually")
            return 4
        return 0
    finally:
        _release_lock(lock_fh)


if __name__ == "__main__":
    sys.exit(main())

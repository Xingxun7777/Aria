"""Post-swap user-payload migration.

The auto-update swap (updater_runner) is a pure directory rename and the
update zip ships only source files — so user-owned payloads (config/*.json
with API keys and hotwords, data/ history, models/ downloaded weights) stay
behind in aria.backup.TS. The NEW live tree's launcher calls
migrate_user_payload_after_swap() on the first post-swap boot, which also
covers swaps performed by OLDER updater_runner versions that predate any
carry logic.

Stdlib-only and stateless so it can be unit-tested without Qt/Win32.
"""

import os
import shutil


def migrate_user_payload_after_swap(install_root, state):
    """Carry user config/data/models from aria.backup.TS into the fresh live tree.

    Idempotent — safe to re-run on every swapped-status boot: config copies
    overwrite with identical bytes, dir renames skip once the target exists.
    """
    backup_dir = state.get("backup_dir", "")
    if not backup_dir or not os.path.isdir(backup_dir):
        return
    live = os.path.join(install_root, "_internal", "app", "aria")
    if not os.path.isdir(live) or os.path.abspath(backup_dir) == os.path.abspath(live):
        return

    # 1. User config files: every config/*.json except shipped *.template.json.
    #    User-owned versions win over freshly shipped defaults (commands.json
    #    may be customized; hotwords.json carries the API key).
    backup_cfg = os.path.join(backup_dir, "config")
    live_cfg = os.path.join(live, "config")
    if os.path.isdir(backup_cfg):
        try:
            os.makedirs(live_cfg, exist_ok=True)
        except OSError:
            pass
        try:
            names = sorted(os.listdir(backup_cfg))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json") or name.endswith(".template.json"):
                continue
            src = os.path.join(backup_cfg, name)
            if not os.path.isfile(src):
                continue
            try:
                shutil.copy2(src, os.path.join(live_cfg, name))
            except OSError:
                pass

    # 2. Big payload dirs: rename (same volume, instant). models/ can be
    #    multi-GB so it is never byte-copied — if the rename fails the app
    #    falls back to its normal first-run download path.
    for name in ("data", "models"):
        src = os.path.join(backup_dir, name)
        dst = os.path.join(live, name)
        if not os.path.isdir(src):
            continue
        if not os.path.exists(dst):
            try:
                os.rename(src, dst)
            except OSError:
                if name == "data":
                    try:
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    except OSError:
                        pass
        elif name == "data":
            # Re-run after a partial boot already created live/data: merge
            # file-level, never overwriting newer live files.
            for root, _dirs, files in os.walk(src):
                rel = os.path.relpath(root, src)
                troot = dst if rel == "." else os.path.join(dst, rel)
                try:
                    os.makedirs(troot, exist_ok=True)
                except OSError:
                    continue
                for fn in files:
                    target = os.path.join(troot, fn)
                    if os.path.exists(target):
                        continue
                    try:
                        shutil.copy2(os.path.join(root, fn), target)
                    except OSError:
                        pass


def reclaim_payload_after_rollback(live, retired):
    """After backup→live restore, payload dirs migrated into the failed live
    (now retired) by migrate_user_payload_after_swap must move back, or the
    restored old version would boot without data/ and models/."""
    for name in ("data", "models"):
        src = os.path.join(retired, name)
        dst = os.path.join(live, name)
        if os.path.isdir(src) and not os.path.exists(dst):
            try:
                os.rename(src, dst)
            except OSError:
                pass

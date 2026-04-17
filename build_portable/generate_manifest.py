"""
Aria Release Manifest Generator
================================
Generate release-manifest.json for the auto-update channel.

Run from repo root after version bump + commit, before pushing:
    python build_portable/generate_manifest.py

What this does:
    1. Read __version__ from __init__.py
    2. Extract changelog notes for current version from CHANGELOG.md
    3. Build source zip via `git archive` (deterministic from tree)
    4. Compute SHA256 + size of the zip
    5. Write release-manifest.json to repo root
    6. Print next steps (tag + push + gh release upload)

Why `git archive` instead of GitHub's on-the-fly main.zip:
    GitHub's refs/heads/main.zip is a moving target; the bytes can change
    between version check and download. `git archive v{version}` is
    reproducible from a fixed tree hash, and we upload it as a GitHub
    Release asset — immutable, pinned by tag.

Output:
    release-manifest.json (repo root, gets committed to release branch)
    dist_release/Aria-source-{version}.zip (for `gh release create --attach`)
"""

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist_release"
MANIFEST_PATH = REPO_ROOT / "release-manifest.json"
SCHEMA_VERSION = 1

GITHUB_OWNER = "Xingxun7777"
GITHUB_REPO = "Aria"

MIRROR_PREFIXES = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
]


def read_version() -> str:
    init_file = REPO_ROOT / "__init__.py"
    text = init_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "__version__" in line and "=" in line:
            return line.split("=")[1].strip().strip("\"'")
    raise RuntimeError(f"Cannot read __version__ from {init_file}")


def extract_notes(version: str) -> str:
    """Pull the first bullet list under `## [{version}]` from CHANGELOG.md."""
    changelog = REPO_ROOT / "CHANGELOG.md"
    if not changelog.exists():
        return ""
    text = changelog.read_text(encoding="utf-8")
    pattern = rf"##\s*\[{re.escape(version)}\][^\n]*\n(.*?)(?=\n##\s*\[|\Z)"
    m = re.search(pattern, text, flags=re.DOTALL)
    if not m:
        return ""
    section = m.group(1).strip()
    # Condense: pick bolded bullet titles only, cap length
    bullets = re.findall(r"\*\*([^*]+)\*\*", section)
    if not bullets:
        return section[:200]
    summary = "；".join(bullets[:4])
    return summary[:300]


def git_archive_zip(version: str, out_path: Path) -> None:
    """Create a deterministic zip from current HEAD tree (not from a tag).

    We archive HEAD because at the moment this script runs, the version-bump
    commit is on HEAD. The tag will be created after, and the zip upload will
    be attached to the release named v{version}.
    """
    DIST_DIR.mkdir(exist_ok=True)
    prefix = f"Aria-{version}/"
    # Fix mtime so successive amends don't change the zip SHA256.
    # Without this, git archive stamps all entries with the commit's timestamp,
    # which changes every `git commit --amend` → chicken/egg with manifest SHA.
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            f"--prefix={prefix}",
            "--mtime=2000-01-01T00:00:00Z",
            "-o",
            str(out_path),
            "HEAD",
        ],
        cwd=REPO_ROOT,
        check=True,
    )

    # git archive embeds HEAD's commit SHA in the zip central-directory
    # comment. That defeats determinism: every `git commit --amend` changes
    # the commit SHA and thus the zip comment, breaking manifest stability.
    # Strip the comment so the zip bytes depend only on tree content + mtime.
    _strip_zip_comment(out_path)


def _strip_zip_comment(zip_path: Path) -> None:
    """Rewrite the zip file without any central-directory comment."""
    import zipfile
    import shutil as _sh

    tmp = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(zip_path, "r") as src:
        with zipfile.ZipFile(
            tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                # Re-create ZipInfo to keep fixed mtime + no extra fields
                new_info = zipfile.ZipInfo(
                    filename=info.filename, date_time=(2000, 1, 1, 0, 0, 0)
                )
                new_info.external_attr = info.external_attr
                new_info.compress_type = zipfile.ZIP_DEFLATED
                dst.writestr(new_info, data)
    tmp.replace(zip_path)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def asset_url(version: str) -> str:
    return (
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/"
        f"releases/download/v{version}/Aria-source-{version}.zip"
    )


def mirror_urls(primary: str) -> list[str]:
    return [prefix + primary for prefix in MIRROR_PREFIXES]


def infer_scope() -> str:
    """Default to python_only; require explicit override via CLI for full."""
    return "python_only"


def build_manifest(version: str, zip_path: Path) -> dict:
    primary = asset_url(version)
    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "released_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "update_scope": infer_scope(),
        "critical": False,
        "notes_summary": extract_notes(version),
        "assets": {
            "main_zip": {
                "url": primary,
                "mirrors": mirror_urls(primary),
                "size": zip_path.stat().st_size,
                "sha256": sha256_of(zip_path),
            }
        },
        "min_compatible_from": "1.0.3.2",
    }


def write_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def print_next_steps(version: str, zip_path: Path, manifest: dict) -> None:
    print()
    print("=" * 60)
    print(f"Manifest generated for v{version}")
    print("=" * 60)
    print(f"Zip:      {zip_path}")
    print(f"Size:     {manifest['assets']['main_zip']['size']:,} bytes")
    print(f"SHA256:   {manifest['assets']['main_zip']['sha256']}")
    print(f"Manifest: {MANIFEST_PATH}")
    print()
    print("Next steps:")
    print()
    print(f"  1. Review release-manifest.json + CHANGELOG summary")
    print(f"  2. git add release-manifest.json && git commit --amend --no-edit")
    print(f"  3. git tag v{version}")
    print(f"  4. git push origin main --tags")
    print(f"  5. Build release orphan as usual (force push to 'release' remote)")
    print(f"  6. Create GitHub Release + upload zip asset:")
    print(f"     gh release create v{version} --repo {GITHUB_OWNER}/{GITHUB_REPO} \\")
    print(f'       --title "Aria v{version}" \\')
    print(f"       --notes-from-tag \\")
    print(f'       "{zip_path}"')
    print()
    print("Clients will fetch manifest from:")
    print(
        f"  https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/"
        f"release-manifest.json"
    )
    print()


def main() -> int:
    version = read_version()
    zip_path = DIST_DIR / f"Aria-source-{version}.zip"
    print(f"[manifest] version = {version}")
    print(f"[manifest] archiving HEAD → {zip_path}")
    git_archive_zip(version, zip_path)

    manifest = build_manifest(version, zip_path)
    write_manifest(manifest)
    print_next_steps(version, zip_path, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())

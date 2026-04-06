"""
Aria 一键升级工具
=================
从 GitHub 拉取最新源码，覆盖更新本地文件。
不会影响：用户配置(hotwords.json)、模型、历史数据。

使用方法：
  双击 update.bat（放在 Aria 安装根目录）
"""

import io
import os
import shutil
import sys
import zipfile
from pathlib import Path


# ─── 配置 ───────────────────────────────────────────────

GITHUB_ZIP = "https://github.com/Xingxun7777/Aria/archive/refs/heads/main.zip"

# 下载失败时依次尝试的镜像
MIRRORS = [
    "https://ghfast.top/https://github.com/Xingxun7777/Aria/archive/refs/heads/main.zip",
    "https://gh-proxy.com/https://github.com/Xingxun7777/Aria/archive/refs/heads/main.zip",
    "https://ghproxy.cc/https://github.com/Xingxun7777/Aria/archive/refs/heads/main.zip",
]

# 要更新的源码目录（GitHub 仓库根目录 → 本地 _internal/app/aria/）
SOURCE_DIRS = ["core", "ui", "system", "aria"]

# 要更新的源码文件
SOURCE_FILES = [
    "app.py",
    "__init__.py",
    "__main__.py",
    "launcher.py",
    "progress_ipc.py",
]

# 要更新的配置模板（不会覆盖用户的 hotwords.json）
TEMPLATE_FILES = [
    "config/hotwords.template.json",
]

# 绝不覆盖的文件（用户个人配置）
NEVER_OVERWRITE = {
    "config/hotwords.json",
}

# 用户可能已自定义的文件（仅当本地不存在时才写入）
MERGE_IF_MISSING = {
    "config/wakeword.json",
    "config/commands.json",
}

# 根目录要更新的文件
ROOT_FILES = [
    "update.bat",
    "Aria.cmd",
    "Aria.vbs",
    "Aria_debug.bat",
]


# ─── 工具函数 ────────────────────────────────────────────


def print_banner():
    print()
    print("  ╔═══════════════════════════════════╗")
    print("  ║       Aria 一键升级工具            ║")
    print("  ╚═══════════════════════════════════╝")
    print()


def get_local_version(aria_root: Path) -> str:
    """读取本地版本号。"""
    init_file = aria_root / "__init__.py"
    if init_file.exists():
        for line in init_file.read_text(encoding="utf-8").splitlines():
            if "__version__" in line and "=" in line:
                return line.split("=")[1].strip().strip("\"'")
    return "未知"


def get_zip_version(zip_root: Path) -> str:
    """读取下载包中的版本号。"""
    init_file = zip_root / "__init__.py"
    if init_file.exists():
        for line in init_file.read_text(encoding="utf-8").splitlines():
            if "__version__" in line and "=" in line:
                return line.split("=")[1].strip().strip("\"'")
    return "未知"


def check_aria_running() -> bool:
    """检查 Aria 是否正在运行。"""
    import subprocess

    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AriaRuntime.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "AriaRuntime.exe" in result.stdout:
            return True
    except Exception:
        pass

    # 也检查通过 pythonw.exe 运行的情况
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='pythonw.exe'", "get", "commandline"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "launcher.py" in result.stdout or "aria" in result.stdout.lower():
            return True
    except Exception:
        pass

    return False


def download_zip(timeout: int = 30) -> bytes:
    """从 GitHub 或镜像下载最新源码 zip。"""
    import urllib.request
    import ssl

    # 创建不验证证书的上下文（某些企业网络需要）
    ctx = ssl.create_default_context()

    urls = [GITHUB_ZIP] + MIRRORS
    last_error = None

    for i, url in enumerate(urls):
        source = "GitHub" if i == 0 else f"镜像 {i}"
        print(f"  尝试 {source}...", end="", flush=True)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Aria-Updater/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = resp.read()
            print(f" 成功 ({len(data) / 1024 / 1024:.1f} MB)")
            return data
        except Exception as e:
            last_error = e
            print(f" 失败 ({type(e).__name__})")

    raise RuntimeError(
        f"所有下载源均失败。最后错误: {last_error}\n" "请检查网络连接后重试。"
    )


def extract_and_update(zip_data: bytes, aria_root: Path, install_root: Path):
    """解压并更新文件。"""
    updated = 0
    skipped = 0

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        # GitHub zip 内有一个顶层目录 "Aria-main/"
        top_dirs = {name.split("/")[0] for name in zf.namelist() if "/" in name}
        if len(top_dirs) != 1:
            raise RuntimeError(f"zip 结构异常，预期 1 个顶层目录，实际 {len(top_dirs)}")
        zip_prefix = top_dirs.pop() + "/"

        # ── 更新源码目录 ──
        for src_dir in SOURCE_DIRS:
            prefix = zip_prefix + src_dir + "/"
            dst_base = aria_root / src_dir

            for info in zf.infolist():
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue

                rel_path = info.filename[len(prefix) :]
                dst_path = dst_base / rel_path

                dst_path.parent.mkdir(parents=True, exist_ok=True)
                dst_path.write_bytes(zf.read(info.filename))
                updated += 1

        # ── 更新源码文件 ──
        for src_file in SOURCE_FILES:
            zip_path = zip_prefix + src_file
            if zip_path in zf.namelist():
                dst = aria_root / src_file
                dst.write_bytes(zf.read(zip_path))
                updated += 1

        # ── 更新配置模板 ──
        for tmpl in TEMPLATE_FILES:
            zip_path = zip_prefix + tmpl
            if zip_path in zf.namelist():
                dst = aria_root / tmpl
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(zf.read(zip_path))
                updated += 1

        # ── 处理用户可能自定义的配置 ──
        for merge_file in MERGE_IF_MISSING:
            zip_path = zip_prefix + merge_file
            dst = aria_root / merge_file
            if zip_path in zf.namelist():
                if dst.exists():
                    skipped += 1  # 保留用户自定义
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(zf.read(zip_path))
                    updated += 1

        # ── 更新根目录文件（启动脚本、升级脚本）──
        for root_file in ROOT_FILES:
            zip_path = zip_prefix + root_file
            if zip_path in zf.namelist():
                dst = install_root / root_file
                dst.write_bytes(zf.read(zip_path))
                updated += 1

        # ── 更新升级工具自身 ──
        updater_zip = zip_prefix + "update_tool.py"
        if updater_zip in zf.namelist():
            dst = aria_root / "update_tool.py"
            dst.write_bytes(zf.read(updater_zip))
            updated += 1

    # ── 清理 __pycache__ 防止旧 .pyc 覆盖新 .py ──
    for cache_dir in aria_root.rglob("__pycache__"):
        try:
            shutil.rmtree(cache_dir)
        except Exception:
            pass

    return updated, skipped


# ─── 自动检测更新（后台轻量检查）──────────────────────────

GITHUB_RAW_INIT = "https://raw.githubusercontent.com/Xingxun7777/Aria/main/__init__.py"
GITHUB_RAW_MIRRORS = [
    "https://ghfast.top/https://raw.githubusercontent.com/Xingxun7777/Aria/main/__init__.py",
    "https://raw.gitmirror.com/Xingxun7777/Aria/main/__init__.py",
]


def _parse_version(text: str) -> str:
    for line in text.splitlines():
        if "__version__" in line and "=" in line:
            return line.split("=")[1].strip().strip("\"'")
    return ""


def _version_tuple(v: str) -> tuple:
    """Convert '1.0.3.2' to (1, 0, 3, 2) for comparison."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def check_for_update(local_version: str = "", timeout: int = 8) -> dict:
    """Check if a newer version is available on GitHub.

    Non-blocking safe: designed to run in a background thread.

    Args:
        local_version: Current local version string. If empty, reads from __init__.py.
        timeout: HTTP request timeout in seconds.

    Returns:
        dict with keys:
            "available": bool — True if newer version exists
            "local": str — local version
            "remote": str — remote version (empty if check failed)
            "error": str — error message if check failed
    """
    import urllib.request
    import ssl

    if not local_version:
        try:
            init_file = Path(__file__).parent / "__init__.py"
            local_version = _parse_version(init_file.read_text(encoding="utf-8"))
        except Exception:
            local_version = "0"

    result = {"available": False, "local": local_version, "remote": "", "error": ""}

    ctx = ssl.create_default_context()
    urls = [GITHUB_RAW_INIT] + GITHUB_RAW_MIRRORS

    for url in urls:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Aria-UpdateCheck/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                remote_text = resp.read().decode("utf-8", errors="replace")
            remote_ver = _parse_version(remote_text)
            if not remote_ver:
                continue

            result["remote"] = remote_ver
            if _version_tuple(remote_ver) > _version_tuple(local_version):
                result["available"] = True
            return result
        except Exception:
            continue

    result["error"] = "无法连接更新服务器"
    return result


# ─── 主流程 ──────────────────────────────────────────────


def main():
    print_banner()

    # 定位路径
    # 脚本位于 _internal/app/aria/update_tool.py
    # aria_root = _internal/app/aria/
    # install_root = Aria/ (最外层)
    script_path = Path(__file__).resolve()
    aria_root = script_path.parent
    install_root = (
        aria_root.parent.parent.parent
    )  # _internal/app/aria → _internal/app → _internal → Aria

    # 验证路径
    if not (aria_root / "app.py").exists():
        print("  [错误] 无法定位 Aria 源码目录。")
        return 1

    # 检查 Aria 是否在运行
    if check_aria_running():
        print("  [警告] 检测到 Aria 正在运行！")
        print("         请先关闭 Aria 再运行升级。")
        print()
        return 1

    # 显示当前版本
    local_ver = get_local_version(aria_root)
    print(f"  当前版本: {local_ver}")
    print()

    # 下载
    print("  [1/3] 下载最新版本...")
    try:
        zip_data = download_zip()
    except RuntimeError as e:
        print(f"\n  [错误] {e}")
        return 1

    # 解压到临时目录检查版本
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        top_dirs = {name.split("/")[0] for name in zf.namelist() if "/" in name}
        zip_prefix = top_dirs.pop() + "/"
        init_zip = zip_prefix + "__init__.py"
        if init_zip in zf.namelist():
            init_content = zf.read(init_zip).decode("utf-8", errors="replace")
            remote_ver = "未知"
            for line in init_content.splitlines():
                if "__version__" in line and "=" in line:
                    remote_ver = line.split("=")[1].strip().strip("\"'")
                    break
            print(f"\n  最新版本: {remote_ver}")

            if remote_ver == local_ver:
                print("  已经是最新版本，无需升级。")
                print()
                return 0

    # 更新
    print()
    print("  [2/3] 更新文件...")
    try:
        updated, skipped = extract_and_update(zip_data, aria_root, install_root)
    except Exception as e:
        print(f"\n  [错误] 更新失败: {e}")
        import traceback

        traceback.print_exc()
        return 1

    print(f"         更新了 {updated} 个文件")
    if skipped:
        print(f"         保留了 {skipped} 个用户自定义配置")

    # 验证
    print()
    print("  [3/3] 验证...")
    new_ver = get_local_version(aria_root)
    print(f"         版本: {local_ver} → {new_ver}")

    if (aria_root / "app.py").exists() and (aria_root / "launcher.py").exists():
        print("         核心文件完整 [OK]")
    else:
        print("         [警告] 核心文件可能不完整，请检查！")

    print()
    print("  ===================================")
    print(f"  升级完成！请重新启动 Aria。")
    print("  ===================================")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已取消。")
        sys.exit(1)
    except Exception as e:
        print(f"\n  [未预期的错误] {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

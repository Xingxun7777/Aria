"""
Windows scheduled-task helpers for opt-in elevated voice commands.

This module never registers tasks implicitly from the wakeword executor.  Task
creation is called only from Settings after explicit user consent and UAC.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


ARIA_TASK_PREFIX = "Aria_Elevated_"
ARIA_TASK_DESCRIPTION_PREFIX = "Managed by Aria voice assistant."
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_DRIVE_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_ARGS_AFTER_EXE_RE = re.compile(r"^([a-zA-Z]:[\\/].*?\.exe)\s+.+$", re.IGNORECASE)
_SHELL_METACHARS = set("&|><;$`\n\r\"'")
LOL_BIN_DENYLIST = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "wscript.exe",
    "cscript.exe",
    "mshta.exe",
    "rundll32.exe",
    "regsvr32.exe",
    "bash.exe",
    "wsl.exe",
    "wmic.exe",
    "forfiles.exe",
    "installutil.exe",
    "msbuild.exe",
    "regasm.exe",
    "certutil.exe",
    "bitsadmin.exe",
}


def compute_task_name(entry_id: str) -> str:
    digest = hashlib.sha256(str(entry_id).encode("utf-8")).hexdigest()[:16]
    return f"{ARIA_TASK_PREFIX}{digest}"


def _looks_like_task_name(task_name: str) -> bool:
    if not task_name.startswith(ARIA_TASK_PREFIX):
        return False
    return bool(_HEX16_RE.fullmatch(task_name[len(ARIA_TASK_PREFIX) :]))


def _normalize_task_name(task_name: str) -> str:
    return task_name.strip().lstrip("\\")


def is_user_writable_chain(path: str) -> bool:
    target = Path(path)
    current = target.parent if target.suffix else target
    seen: set[str] = set()

    while current and str(current) not in seen:
        current_str = str(current)
        seen.add(current_str)
        if current.exists() and os.access(current_str, os.W_OK):
            return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def validate_elevated_target(command: str, trust_writable: bool) -> tuple[bool, str]:
    target = str(command or "").strip()
    if not target:
        return False, "elevated target is empty"
    if any(ch in target for ch in _SHELL_METACHARS):
        return False, "elevated target contains shell metacharacters"
    if not _DRIVE_ABS_RE.match(target) or not os.path.isabs(target):
        return False, "elevated target must be an absolute drive-letter path"

    args_match = _ARGS_AFTER_EXE_RE.match(target)
    if args_match and os.path.exists(args_match.group(1)):
        return False, "elevated target must be just the executable path, without arguments"

    path = Path(target)
    if path.suffix.lower() != ".exe":
        return False, "elevated target must be an .exe file"
    if path.name.lower() in LOL_BIN_DENYLIST:
        return False, f"elevated target uses denied Windows utility: {path.name}"
    if not path.exists() or not path.is_file():
        return False, "elevated target path does not exist"
    if not trust_writable and is_user_writable_chain(str(path)):
        return False, "elevated target is under a user-writable directory"
    return True, ""


def _build_task_xml(
    *,
    entry_id: str,
    phrase: str,
    command: str,
    working_dir: str,
    description: str | None,
) -> str:
    task_description = description or (
        f"{ARIA_TASK_DESCRIPTION_PREFIX} entry_id={entry_id}, phrase={phrase}. "
        "DO NOT EDIT MANUALLY - managed via Aria settings."
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(task_description)}</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _shell_execute_runas_wait(file: str, params: list[str]) -> tuple[bool, str]:
    see_mask_nocloseprocess = 0x00000040
    infinite = 0xFFFFFFFF
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(_ShellExecuteInfoW)
    info.fMask = see_mask_nocloseprocess
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = subprocess.list2cmdline(params)
    info.nShow = 0

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        return False, f"ShellExecuteExW failed: {err}"
    if info.hProcess:
        kernel32.WaitForSingleObject(info.hProcess, infinite)
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
        kernel32.CloseHandle(info.hProcess)
        if exit_code.value != 0:
            return False, f"schtasks exited with code {exit_code.value}"
    return True, ""


def register_elevated_task(
    entry_id: str,
    phrase: str,
    command: str,
    working_dir: str,
    description: str | None = None,
    *,
    trust_writable_target: bool = False,
) -> tuple[bool, str]:
    ok, reason = validate_elevated_target(command, trust_writable_target)
    if not ok:
        return False, reason
    task_name = compute_task_name(entry_id)
    target = str(Path(command))
    task_working_dir = str(Path(working_dir)) if working_dir else str(Path(target).parent)
    xml = _build_task_xml(
        entry_id=entry_id,
        phrase=phrase,
        command=target,
        working_dir=task_working_dir,
        description=description,
    )

    xml_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-16", suffix=".xml", delete=False
        ) as tmp:
            tmp.write(xml)
            xml_path = tmp.name
        return _shell_execute_runas_wait(
            "schtasks.exe",
            ["/Create", "/TN", task_name, "/XML", xml_path, "/F"],
        )
    except Exception as exc:
        return False, str(exc)
    finally:
        if xml_path:
            try:
                os.remove(xml_path)
            except OSError:
                pass


def unregister_elevated_task(entry_id: str) -> tuple[bool, str]:
    task_name = compute_task_name(entry_id)
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "").strip()
    return True, ""


def task_exists(entry_id: str) -> bool:
    task_name = compute_task_name(entry_id)
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True,
        text=True,
        shell=False,
    )
    return result.returncode == 0


def _extract_entry_id(description: str) -> str:
    match = re.search(r"\bentry_id=([0-9a-fA-F]+)\b", description or "")
    return match.group(1) if match else ""


def list_aria_tasks() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["schtasks", "/Query", "/FO", "CSV", "/V"],
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        return []

    tasks: list[dict[str, Any]] = []
    for row in csv.DictReader(result.stdout.splitlines()):
        raw_name = row.get("TaskName") or row.get("Task Name") or ""
        task_name = _normalize_task_name(raw_name)
        description = row.get("Description") or row.get("Comment") or ""
        if not _looks_like_task_name(task_name):
            continue
        if ARIA_TASK_DESCRIPTION_PREFIX not in description:
            continue
        tasks.append(
            {
                "task_name": task_name,
                "entry_id_from_description": _extract_entry_id(description),
                "exec_command": row.get("Task To Run") or row.get("Action") or "",
                "last_run": row.get("Last Run Time") or row.get("Last Run") or "",
            }
        )
    return tasks

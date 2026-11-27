"""Small, side-effect-free host capability detection for NerveForge."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path


def is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def available_shells() -> list[dict[str, str]]:
    if is_windows():
        shells = []
        if shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh.exe") or shutil.which("pwsh"):
            shells.append({"id": "powershell", "label": "PowerShell"})
        if shutil.which("cmd.exe") or shutil.which("cmd"):
            shells.append({"id": "cmd", "label": "Командная строка (CMD)"})
        return shells
    if is_linux() and (shutil.which("bash") or Path("/bin/bash").is_file()):
        return [{"id": "bash", "label": "Bash"}]
    return []


def default_shell() -> str | None:
    shells = available_shells()
    if is_windows():
        return next((shell["id"] for shell in shells if shell["id"] == "powershell"),
                    shells[0]["id"] if shells else None)
    return "bash" if shells else None


def platform_info() -> dict:
    system = platform.system() or "Unknown"
    if is_windows():
        os_id = "windows"
        label = f"Windows {platform.release()}".strip()
    elif is_linux():
        os_id = "linux"
        label = f"Linux {platform.release()}".strip()
    else:
        os_id = "unsupported"
        label = system
    shells = available_shells()
    return {
        "os": os_id,
        "label": label,
        "architecture": platform.machine(),
        "shells": shells,
        "default_shell": default_shell(),
        "workspace_file_access": True,
        # Windows command sandboxing is intentionally not claimed until a verified
        # isolation backend is installed; the explicit armed shell is separate.
        "workspace_command_sandbox": not is_windows() and is_linux(),
        "systemd_actions": is_linux(),
        "screenshot": is_linux(),
    }

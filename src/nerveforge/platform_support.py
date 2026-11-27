"""Small, side-effect-free host capability detection for NerveForge."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path


def is_android() -> bool:
    return bool(os.environ.get("ANDROID_ROOT") or os.environ.get("TERMUX_VERSION") or
                os.path.isdir("/data/data/com.termux") or "android" in sys.platform.lower())


def is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def is_linux() -> bool:
    return sys.platform.startswith("linux") and not is_android()


def available_shells() -> list[dict[str, str]]:
    if is_windows():
        shells = []
        if shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh.exe") or shutil.which("pwsh"):
            shells.append({"id": "powershell", "label": "PowerShell"})
        if shutil.which("cmd.exe") or shutil.which("cmd"):
            shells.append({"id": "cmd", "label": "Командная строка (CMD)"})
        return shells
    if is_android():
        shells = []
        if shutil.which("bash") or Path("/data/data/com.termux/files/usr/bin/bash").is_file():
            shells.append({"id": "bash", "label": "Bash (Termux)"})
        if shutil.which("zsh") or Path("/data/data/com.termux/files/usr/bin/zsh").is_file():
            shells.append({"id": "zsh", "label": "Zsh (Termux)"})
        if shutil.which("sh") or Path("/data/data/com.termux/files/usr/bin/sh").is_file() or Path("/bin/sh").is_file():
            shells.append({"id": "sh", "label": "Sh"})
        return shells or [{"id": "bash", "label": "Bash"}]
    if (is_linux() or not is_windows()) and (shutil.which("bash") or Path("/bin/bash").is_file()):
        return [{"id": "bash", "label": "Bash"}]
    return [{"id": "sh", "label": "Sh"}] if shutil.which("sh") else []


def default_shell() -> str | None:
    shells = available_shells()
    if is_windows():
        return next((shell["id"] for shell in shells if shell["id"] == "powershell"),
                    shells[0]["id"] if shells else None)
    return shells[0]["id"] if shells else None


def platform_info() -> dict:
    system = platform.system() or "Unknown"
    if is_windows():
        os_id = "windows"
        label = f"Windows {platform.release()}".strip()
    elif is_android():
        os_id = "android"
        label = f"Android (Termux) {platform.release()}".strip()
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
        "workspace_command_sandbox": is_linux(),
        "systemd_actions": is_linux(),
        "screenshot": is_linux(),
        "termux_api": is_android() and bool(shutil.which("termux-toast") or shutil.which("termux-vibrate")),
    }

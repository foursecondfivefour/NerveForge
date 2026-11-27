"""Per-user NerveForge locations without third-party dependencies."""
from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "NerveForge"
    return home / ".config" / "nerveforge"


def data_dir() -> Path:
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "NerveForge" / "data"
    return home / ".local" / "share" / "nerveforge"


def workspace_dir() -> Path:
    configured = os.environ.get("NERVEFORGE_WORKSPACE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "NerveForgeProjects"


def secure_chmod(path: Path, mode: int) -> None:
    """Set POSIX mode bits; Windows ACLs are inherited and are not chmod-managed."""
    if os.name != "nt":
        path.chmod(mode)

#!/usr/bin/env python3
"""Install a desktop/application-menu launcher for the project entry point."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
python = sys.executable
applications = Path.home() / ".local" / "share" / "applications"
applications.mkdir(parents=True, exist_ok=True)


def desktop_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'

entry = (
    "[Desktop Entry]\n"
    "Version=1.0\n"
    "Type=Application\n"
    "Name=NerveForge\n"
    "Comment=Запустить мост и туннель (Tailscale или Cloudflare)\n"
    f"Exec={desktop_quote(python)} {desktop_quote(str(root / 'run.py'))}\n"
    f"Path={root}\n"
    f"Icon={root / 'assets/nerveforge-mark.png'}\n"
    "Terminal=true\n"
    "Categories=Network;Utility;\n"
)

files = [applications / "nerveforge.desktop"]
desktop_cmd = shutil.which("xdg-user-dir")
if desktop_cmd:
    result = subprocess.run([desktop_cmd, "DESKTOP"], capture_output=True, text=True)
    desktop_dir = Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None
    if desktop_dir and desktop_dir.is_dir():
        files.append(desktop_dir / "NerveForge.desktop")

for file in files:
    file.write_text(entry, encoding="utf-8")
    file.chmod(0o755)
    print(f"Создан ярлык: {file}")
print("Теперь запускайте NerveForge из меню приложений. Откроется терминал с адресом/токеном; Ctrl+C останавливает оба процесса.")

#!/usr/bin/env python3
"""Install the fixed, path-restricted admin helper as root-owned code."""
import shutil
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
source = project_root / 'src' / 'nerveforge' / 'admin_helper.py'
target = Path('/usr/local/lib/nerveforge/admin_helper.py')
sudo = shutil.which('sudo')
install = shutil.which('install')
if not sudo or not install:
    raise SystemExit('sudo/install not found')
if not source.is_file():
    raise SystemExit(f'helper source missing: {source}')
cmd = [sudo, '-n', install, '-D', '-o', 'root', '-g', 'root', '-m', '0755', str(source), str(target)]
p = subprocess.run(cmd, capture_output=True, text=True)
if p.returncode:
    raise SystemExit('Не удалось установить root-owned helper без пароля:\n' + (p.stderr or p.stdout).strip())
print(f'Установлен ограниченный системный helper: {target}')
print('Он принимает только write в разрешённые каталоги, systemd daemon-reload или restart явно указанного .service.')

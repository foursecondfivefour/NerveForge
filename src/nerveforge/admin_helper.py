#!/usr/bin/python3
"""Root-owned, narrowly scoped helper. Install it with scripts/install_system_helper.py."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOTS = (Path('/etc/systemd/system'), Path('/etc/xdg/autostart'))
UNIT_EXTS = {'.service', '.timer', '.socket', '.path', '.target', '.mount', '.automount'}
MAX_BYTES = 256 * 1024


def safe_system_path(raw: str) -> tuple[Path, Path]:
    p = Path(raw)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('path must be absolute and contain no ..')
    allowed_root = None
    for root in ROOTS:
        if p == root or root in p.parents:
            allowed_root = root
            break
    if allowed_root is None:
        raise ValueError('path outside approved system configuration roots')
    if allowed_root.is_symlink() or not allowed_root.is_dir():
        raise ValueError('approved root missing or is a symlink')
    rel = p.relative_to(allowed_root)
    current = allowed_root
    for part in rel.parts[:-1]:
        current = current / part
        if not current.exists() or current.is_symlink() or not current.is_dir():
            raise ValueError('parent must exist and contain no symlinks')
    if p.exists() and (p.is_symlink() or not p.is_file()):
        raise ValueError('destination must be a regular file, not a symlink')
    if allowed_root == ROOTS[0] and p.suffix not in UNIT_EXTS:
        raise ValueError('only direct systemd unit files can be written')
    if allowed_root == ROOTS[1] and p.suffix != '.desktop':
        raise ValueError('only .desktop autostart files can be written')
    if p.parent != allowed_root:
        raise ValueError('nested system configuration paths are not writable in this version')
    return p, allowed_root


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit('helper must be invoked through sudo')
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SystemExit('request too large')
    req = json.loads(raw.decode('utf-8'))
    action = req.get('action')
    if action == 'write':
        path, _ = safe_system_path(str(req.get('path', '')))
        content = req.get('content')
        if not isinstance(content, str):
            raise ValueError('content must be text')
        data = content.encode('utf-8')
        if len(data) > MAX_BYTES:
            raise ValueError('content too large')
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        fd, tmp = tempfile.mkstemp(prefix='.nerveforge-', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chown(tmp, 0, 0)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        print(json.dumps({'ok': True, 'path': str(path), 'bytes': len(data)}))
        return 0
    if action == 'daemon_reload':
        p = subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=30)
        print(json.dumps({'ok': p.returncode == 0, 'output': (p.stdout + p.stderr)[-8000:]}))
        return p.returncode
    if action == 'restart_service':
        unit = str(req.get('unit', ''))
        if not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.service', unit) or '/' in unit:
            raise ValueError('invalid system service unit name')
        p = subprocess.run(['/usr/bin/systemctl', 'restart', unit], capture_output=True, text=True, timeout=60)
        print(json.dumps({'ok': p.returncode == 0, 'unit': unit, 'output': (p.stdout + p.stderr)[-8000:]}))
        return p.returncode
    raise ValueError('unsupported action')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}))
        raise SystemExit(1)

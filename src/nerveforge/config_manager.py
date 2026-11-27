"""Read-only service/config monitoring plus staged file changes for systemd/autostart."""
from __future__ import annotations

import configparser
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import paths, platform_support as host

MAX_CONFIG_BYTES = 64 * 1024
UNIT_SUFFIXES = {'.service', '.timer', '.socket', '.path', '.target', '.mount', '.automount'}
SYSTEMD_ROOT = Path('/etc/systemd/system')
SYSTEM_AUTOSTART = Path('/etc/xdg/autostart')


class ConfigError(ValueError):
    pass


def roots() -> list[tuple[Path, str]]:
    home = Path.home()
    if host.is_windows():
        appdata = Path(os.environ.get('APPDATA', str(home / 'AppData' / 'Roaming')))
        programdata = Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData'))
        return [
            (programdata / 'Microsoft/Windows/Start Menu/Programs/Startup', 'autostart-system'),
            (appdata / 'Microsoft/Windows/Start Menu/Programs/Startup', 'autostart-user'),
        ]
    return [
        (SYSTEMD_ROOT, 'systemd-system'),
        (home / '.config/systemd/user', 'systemd-user'),
        (SYSTEM_AUTOSTART, 'autostart-system'),
        (home / '.config/autostart', 'autostart-user'),
    ]


def allowed_config_path(raw: str, for_edit: bool = False) -> tuple[Path, str, bool]:
    if host.is_windows():
        raise ConfigError('Изменение системных конфигураций Windows этим режимом не поддерживается; он только читает Startup-папки.')
    p = Path(raw).expanduser()
    if not p.is_absolute() or '..' in p.parts:
        raise ConfigError('Укажите абсолютный путь без сегментов ..')
    matched = None
    for root, scope in roots():
        if p == root or root in p.parents:
            matched = (root, scope)
            break
    if not matched:
        raise ConfigError('Поддерживаются только systemd unit-файлы и autostart-конфиги в разрешённых каталогах')
    root, scope = matched
    if for_edit:
        if scope.startswith('systemd'):
            valid_name = p.suffix in UNIT_SUFFIXES and p.parent == root
        else:
            valid_name = p.suffix == '.desktop' and p.parent == root
        if not valid_name:
            raise ConfigError('Редактирование разрешено только прямых systemd unit-файлов или .desktop autostart-файлов; drop-in пока только отслеживаются')
        if p.parent.is_symlink():
            raise ConfigError('Каталог назначения не может быть symlink')
        if not p.parent.is_dir():
            if scope in ('systemd-user', 'autostart-user') and p.parent.parent.is_dir() and not p.parent.parent.is_symlink():
                pass
            else:
                raise ConfigError('Каталог назначения должен существовать')
        if p.exists() and (p.is_symlink() or not p.is_file()):
            raise ConfigError('Назначение должно быть обычным файлом, не symlink')
        return p, scope, scope in ('systemd-system', 'autostart-system')
    return p, scope, scope in ('systemd-system', 'autostart-system')


def read_config(path: Path, system: bool = False) -> str:
    try:
        data = path.read_bytes()
    except PermissionError:
        if not system:
            raise ConfigError('Нет прав на чтение файла')
        sudo = shutil.which('sudo')
        cat = '/usr/bin/cat'
        if not sudo or not Path(cat).exists():
            raise ConfigError('Нет доступа к системному файлу и не найден sudo/cat')
        p = subprocess.run([sudo, '-n', cat, '--', str(path)], capture_output=True, timeout=10)
        if p.returncode:
            raise ConfigError('Не удалось прочитать системный файл без пароля: ' + (p.stderr.decode(errors='replace')[-1000:]))
        data = p.stdout
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError('Файл превышает лимит чтения 64 KiB')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as e:
        raise ConfigError('Поддерживаются только текстовые UTF-8 файлы') from e


def validate_text(path: Path, content: str) -> str:
    if not isinstance(content, str):
        raise ConfigError('content должен быть строкой UTF-8')
    data = content.encode('utf-8')
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError('Файл превышает лимит 64 KiB')
    if '\x00' in content:
        raise ConfigError('Нулевые байты запрещены')
    if path.suffix == '.desktop':
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(content)
        except configparser.Error as e:
            raise ConfigError('Некорректный desktop-файл: ' + str(e)) from e
        if not parser.has_section('Desktop Entry'):
            raise ConfigError('В .desktop требуется секция [Desktop Entry]')
        kind = parser.get('Desktop Entry', 'Type', fallback='Application')
        if kind == 'Application' and not parser.get('Desktop Entry', 'Exec', fallback='').strip():
            raise ConfigError('Для Type=Application требуется непустой Exec')
        if not parser.get('Desktop Entry', 'Name', fallback='').strip():
            raise ConfigError('В [Desktop Entry] требуется Name')
        return 'desktop-file: структура [Desktop Entry] проверена'
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('='), comment_prefixes=('#', ';'))
    try:
        parser.read_string(content)
    except configparser.Error as e:
        raise ConfigError('Некорректная структура systemd unit: ' + str(e)) from e
    if shutil.which('systemd-analyze'):
        suffix = path.suffix
        fd, tmp_name = tempfile.mkstemp(prefix='mint-bridge-validate-', suffix=suffix)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            p = subprocess.run(['systemd-analyze', 'verify', tmp_name], capture_output=True, text=True, timeout=20)
            if p.returncode:
                message = (p.stdout + p.stderr).strip()[-3000:]
                raise ConfigError('systemd-analyze verify отклонил unit: ' + message)
        except subprocess.TimeoutExpired as e:
            raise ConfigError('Проверка systemd превысила 20 секунд') from e
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return 'systemd-analyze verify: OK'
    return 'Проверена INI-подобная структура; systemd-analyze не установлен'


def diff_text(path: Path, old: str, new: str) -> str:
    return ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=str(path) + ' (текущая)',
                                        tofile=str(path) + ' (предлагаемая)', n=3)) or '(изменений нет)'


def backup_path(path: Path, content: str | None) -> str:
    base = paths.data_dir() / 'backups'
    base.mkdir(parents=True, exist_ok=True)
    paths.secure_chmod(base, 0o700)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:10]
    dest = base / f'{stamp}-{time.time_ns()}-{digest}-{path.name}.bak'
    if content is None:
        dest.write_text('MINT_BRIDGE_NEW_FILE\n', encoding='utf-8')
    else:
        dest.write_text(content, encoding='utf-8')
    paths.secure_chmod(dest, 0o600)
    return str(dest)


def write_user_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix='.nerveforge-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        paths.secure_chmod(Path(tmp), old_mode)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def ensure_admin() -> None:
    helper = Path('/usr/local/lib/nerveforge/admin_helper.py')
    sudo = shutil.which('sudo')
    if not helper.is_file() or not sudo:
        raise ConfigError('Не установлен ограниченный root-helper. Запустите python3 scripts/install_system_helper.py один раз.')
    p = subprocess.run([sudo, '-n', '/usr/bin/true'], capture_output=True, timeout=5)
    if p.returncode:
        raise ConfigError('sudo -n без пароля недоступен для этого действия')


def _admin_helper(payload: dict) -> dict:
    helper = Path('/usr/local/lib/nerveforge/admin_helper.py')
    sudo = shutil.which('sudo')
    if not helper.is_file() or not sudo:
        raise ConfigError('Не установлен ограниченный root-helper. Запустите python3 scripts/install_system_helper.py один раз.')
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    p = subprocess.run([sudo, '-n', '/usr/bin/python3', str(helper)], input=raw,
                       capture_output=True, timeout=60)
    out = p.stdout.decode('utf-8', errors='replace')
    err = p.stderr.decode('utf-8', errors='replace')
    try:
        result = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        result = {'output': out}
    if p.returncode or not result.get('ok', p.returncode == 0):
        raise ConfigError(result.get('error') or err or result.get('output') or f'root-helper завершился с кодом {p.returncode}')
    return result


def apply_edit(raw_path: str, content: str, expected_old_hash: str | None = None) -> dict:
    path, scope, system = allowed_config_path(raw_path, for_edit=True)
    if path.exists() and path.is_symlink():
        raise ConfigError('Файл стал symlink после постановки запроса; правка остановлена')
    old = read_config(path, system=system) if path.exists() else None
    current_hash = hashlib.sha256(old.encode('utf-8')).hexdigest() if old is not None else None
    if current_hash != expected_old_hash:
        raise ConfigError('Файл изменился после предпросмотра; отменено. Создайте новый diff и подтвердите его заново.')
    validation = validate_text(path, content)
    backup = backup_path(path, old)
    if system:
        result = _admin_helper({'action': 'write', 'path': str(path), 'content': content})
    else:
        write_user_file(path, content)
        result = {'ok': True, 'path': str(path), 'bytes': len(content.encode('utf-8'))}
    return {'path': str(path), 'backup': backup, 'validation': validation, 'write': result}


def run_system_action(action: str, unit: str | None = None) -> dict:
    if host.is_windows():
        raise ConfigError('Изменение служб Windows через удалённый мост отключено; доступен только read-only статус.')
    if action in ('user_daemon_reload', 'restart_user_service'):
        argv = ['/usr/bin/systemctl', '--user']
        if action == 'user_daemon_reload':
            argv.append('daemon-reload')
        else:
            if not unit or not __import__('re').fullmatch(r'[A-Za-z0-9_.@:-]+\.service', unit):
                raise ConfigError('invalid user service unit name')
            argv.extend(['restart', unit])
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if p.returncode:
            raise ConfigError((p.stdout + p.stderr)[-3000:] or f'user systemctl returned {p.returncode}')
        return {'ok': True, 'output': (p.stdout + p.stderr)[-3000:]}
    if action not in ('daemon_reload', 'restart_service'):
        raise ConfigError('Недопустимое действие systemd')
    return _admin_helper({'action': action, **({'unit': unit} if unit else {})})


def _run_report(argv: list[str]) -> dict:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        return {'command': ' '.join(argv), 'return_code': p.returncode,
                'output': (p.stdout + p.stderr)[-16000:]}
    except Exception as e:
        return {'command': ' '.join(argv), 'return_code': None, 'output': str(e)}


def system_report() -> dict:
    if host.is_windows():
        powershell = shutil.which('powershell.exe') or shutil.which('pwsh.exe') or shutil.which('powershell') or shutil.which('pwsh')
        if not powershell:
            return {'generated_at': time.time(), 'platform': host.platform_info(),
                    'windows_services': {'return_code': None, 'output': 'PowerShell недоступен.'}}
        script = "Get-Service | Where-Object {$_.Status -ne 'Running'} | Select-Object -First 100 Name,Status | ConvertTo-Json -Compress"
        return {'generated_at': time.time(), 'platform': host.platform_info(),
                'non_running_services': _run_report([powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script])}
    return {
        'generated_at': time.time(),
        'platform': host.platform_info(),
        'system_failed_units': _run_report(['/usr/bin/systemctl', '--no-pager', '--failed', '--no-legend']),
        'system_active_services': _run_report(['/usr/bin/systemctl', '--no-pager', 'list-units', '--type=service', '--state=running', '--no-legend']),
        'user_failed_units': _run_report(['/usr/bin/systemctl', '--user', '--no-pager', '--failed', '--no-legend']),
        'user_active_services': _run_report(['/usr/bin/systemctl', '--user', '--no-pager', 'list-units', '--type=service', '--state=running', '--no-legend']),
    }


def hardware_report() -> dict:
    if host.is_windows():
        powershell = shutil.which('powershell.exe') or shutil.which('pwsh.exe') or shutil.which('powershell') or shutil.which('pwsh')
        if not powershell:
            return {'source': 'Windows platform API', 'return_code': None, 'output': 'PowerShell недоступен.'}
        script = "$os=Get-CimInstance Win32_OperatingSystem; $cs=Get-CimInstance Win32_ComputerSystem; $cpu=Get-CimInstance Win32_Processor | Select-Object -First 1; [pscustomobject]@{OS=$os.Caption;Version=$os.Version;Manufacturer=$cs.Manufacturer;Model=$cs.Model;LogicalProcessors=$cs.NumberOfLogicalProcessors;MemoryGiB=[math]::Round($cs.TotalPhysicalMemory/1GB,1);CPU=$cpu.Name} | ConvertTo-Json -Compress"
        result = _run_report([powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script])
        return {'source': 'Get-CimInstance (read-only)', **result}
    if shutil.which('inxi'):
        p = subprocess.run(['inxi', '-Fz', '-c', '0'], capture_output=True, text=True, timeout=20)
        text = p.stdout + p.stderr
        text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
        text = re.sub(r'\x03\d{1,2}', '', text)
        return {'source': 'inxi -Fz -c 0', 'return_code': p.returncode, 'output': text[-40000:]}
    parts = []
    for argv in (["uname", "-a"], ["lscpu"], ["free", "-h"],
                 ["lsblk", "-d", "-o", "NAME,SIZE,ROTA,TYPE,MODEL"],
                 ["bash", "-lc", "lspci -nn 2>/dev/null | grep -Ei 'VGA|3D|Display|Network|Ethernet|Audio|Host bridge' || true"]):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        parts.append('$ ' + ' '.join(argv) + '\n' + (p.stdout + p.stderr))
    return {'source': 'read-only fallback', 'return_code': 0, 'output': '\n'.join(parts)[-40000:]}


def service_status(unit: str, scope: str = 'system') -> dict:
    if host.is_windows():
        if not re.fullmatch(r'[A-Za-z0-9_.@:-]{1,128}', unit):
            raise ConfigError('Некорректное имя службы Windows')
        powershell = shutil.which('powershell.exe') or shutil.which('pwsh.exe') or shutil.which('powershell') or shutil.which('pwsh')
        if not powershell:
            raise ConfigError('PowerShell недоступен для проверки службы')
        script = f"Get-Service -Name '{unit}' -ErrorAction Stop | Select-Object Name,DisplayName,Status,StartType | ConvertTo-Json -Compress"
        result = _run_report([powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script])
        return {'scope': 'windows', 'unit': unit, **result}
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.(service|timer|socket|path|target)', unit):
        raise ConfigError('Некорректное имя systemd unit')
    if scope not in ('system', 'user'):
        raise ConfigError('scope должен быть system или user')
    argv = ['/usr/bin/systemctl']
    if scope == 'user':
        argv.append('--user')
    argv.extend(['--no-pager', 'show', unit, '-p', 'Id', '-p', 'Description', '-p', 'LoadState',
                 '-p', 'ActiveState', '-p', 'SubState', '-p', 'UnitFileState', '-p', 'Result', '-p', 'ExecMainStatus'])
    p = subprocess.run(argv, capture_output=True, text=True, timeout=12)
    return {'scope': scope, 'unit': unit, 'return_code': p.returncode,
            'output': (p.stdout + p.stderr)[-6000:]}


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if host.is_windows():
            return bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except OSError:
        return True
    return False


def scan_snapshot() -> dict:
    snapshot = {}
    visited = 0
    for root, scope in roots():
        if not root.exists() or not root.is_dir():
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _is_reparse_or_symlink(Path(current, d)))
            for name in sorted(files):
                path = Path(current) / name
                rel = str(path)
                try:
                    if _is_reparse_or_symlink(path):
                        try:
                            val = 'symlink:' + os.readlink(path)
                        except OSError:
                            val = 'reparse-point'
                        kind = 'symlink'
                    elif not path.is_file():
                        continue
                    elif path.stat().st_size > 128 * 1024:
                        val = 'oversize:' + str(path.stat().st_size)
                        kind = 'oversize'
                    else:
                        val = hashlib.sha256(path.read_bytes()).hexdigest()
                        kind = 'file'
                    snapshot[rel] = {'hash': val, 'scope': scope, 'kind': kind}
                    visited += 1
                    if visited >= 500:
                        return snapshot
                except (OSError, PermissionError):
                    snapshot[rel] = {'hash': 'unreadable', 'scope': scope, 'kind': 'unreadable'}
    return snapshot


class ConfigMonitor:
    def __init__(self):
        self.path = paths.data_dir() / 'config-monitor.json'
        self.lock = threading.Lock()
        self.snapshot = {}
        self.events = []
        self.last_scan = None
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.snapshot = data.get('snapshot', {})
            self.events = data.get('events', [])[-200:]
            self.last_scan = data.get('last_scan')
        except Exception:
            self.snapshot = {}
            self.events = []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'snapshot': self.snapshot, 'events': self.events[-200:], 'last_scan': self.last_scan}, ensure_ascii=False), encoding='utf-8')
        paths.secure_chmod(temp, 0o600)
        temp.replace(self.path)

    def scan(self) -> dict:
        fresh = scan_snapshot()
        with self.lock:
            if not self.snapshot:
                self.snapshot = fresh
                self.last_scan = time.time()
                self.save()
                return {'baseline_created': True, 'files': len(fresh), 'events': [], 'last_scan': self.last_scan}
            events = []
            for path in sorted(set(self.snapshot) | set(fresh)):
                old, new = self.snapshot.get(path), fresh.get(path)
                if old == new:
                    continue
                event = {'time': time.time(), 'path': path,
                         'change': 'added' if old is None else 'removed' if new is None else 'changed',
                         'scope': (new or old).get('scope')}
                events.append(event)
            if events:
                self.events.extend(events)
                self.events = self.events[-200:]
            self.snapshot = fresh
            self.last_scan = time.time()
            self.save()
            return {'baseline_created': False, 'files': len(fresh), 'events': events, 'last_scan': self.last_scan}

    def state(self) -> dict:
        with self.lock:
            return {'last_scan': self.last_scan, 'files': len(self.snapshot), 'events': list(reversed(self.events[-100:]))}

    def loop(self, interval: int = 60):
        while True:
            try:
                self.scan()
            except Exception as e:
                print(f'[monitor] scan error: {e}', flush=True)
            time.sleep(interval)

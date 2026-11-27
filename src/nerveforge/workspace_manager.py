"""Workspace-scoped file access and fail-closed bubblewrap command runner."""
from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from . import paths, platform_support as host

ROOT = paths.workspace_dir()
MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 100_000
MAX_TIMEOUT = 3600
BACKUP_ROOT = paths.data_dir() / "workspace-backups"


class WorkspaceError(ValueError):
    pass


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if host.is_windows():
            attrs = getattr(path.lstat(), "st_file_attributes", 0)
            return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT (junctions included)
    except OSError:
        return False
    return False


def ensure_root() -> Path:
    if not ROOT.is_absolute():
        raise WorkspaceError("Рабочая папка должна быть абсолютным путём")
    if _is_link_or_reparse(ROOT):
        raise WorkspaceError("Рабочая папка не может быть symlink или Windows reparse point")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = ROOT.stat()
    if not stat.S_ISDIR(st.st_mode):
        raise WorkspaceError("Рабочая папка должна быть каталогом")
    if not host.is_windows() and st.st_uid != os.getuid():
        raise WorkspaceError("Рабочая папка должна принадлежать текущему пользователю")
    paths.secure_chmod(ROOT, 0o700)
    return ROOT


def safe_path(raw: str, *, allow_root: bool = False) -> Path:
    root = ensure_root()
    if not isinstance(raw, str) or len(raw) > 1000:
        raise WorkspaceError("path должен быть относительной строкой длиной до 1000 символов")
    rel = Path(raw or ".")
    if rel.is_absolute() or getattr(rel, "drive", "") or getattr(rel, "root", "") or any(part in ("..", "") for part in rel.parts):
        raise WorkspaceError("Разрешены только относительные пути внутри рабочей папки")
    if host.is_windows():
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        for part in rel.parts:
            base = part.rstrip(" .").split(".", 1)[0].upper()
            if ":" in part or part.endswith((" ", ".")) or base in reserved:
                raise WorkspaceError("Недопустимое имя файла или alternate data stream в Windows")
    target = root / rel
    if target == root and not allow_root:
        raise WorkspaceError("Нужен путь к файлу или подпапке")
    # Reject symlink traversal, including a symlink as the final path.
    cur = root
    for part in rel.parts:
        if part == ".":
            continue
        cur = cur / part
        try:
            if _is_link_or_reparse(cur):
                raise WorkspaceError("Symlink/reparse point внутри рабочей папки не разрешён")
        except OSError as e:
            raise WorkspaceError("Не удалось проверить путь") from e
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as e:
        raise WorkspaceError("Путь выходит за пределы рабочей папки") from e
    return target


def list_files(raw: str = ".") -> dict:
    path = safe_path(raw, allow_root=True)
    if not path.exists() or not path.is_dir():
        raise WorkspaceError("Каталог не найден")
    entries = []
    with os.scandir(path) as scan:
        for entry in sorted(scan, key=lambda x: x.name.casefold()):
            if _is_link_or_reparse(Path(entry.path)):
                kind, size = "symlink-blocked", None
            elif entry.is_dir(follow_symlinks=False):
                kind, size = "directory", None
            elif entry.is_file(follow_symlinks=False):
                kind, size = "file", entry.stat(follow_symlinks=False).st_size
            else:
                kind, size = "other", None
            entries.append({"name": entry.name, "type": kind, "size": size})
            if len(entries) >= 500:
                break
    return {"root": str(ROOT), "path": raw or ".", "entries": entries, "truncated": len(entries) >= 500}


def read_file(raw: str) -> dict:
    path = safe_path(raw)
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            raise WorkspaceError("Читать можно только обычные текстовые файлы размером до 256 KiB")
        data = path.read_bytes()
    except FileNotFoundError as e:
        raise WorkspaceError("Файл не найден") from e
    if len(data) > MAX_FILE_BYTES or b"\x00" in data:
        raise WorkspaceError("Это не небольшой текстовый UTF-8 файл")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WorkspaceError("Поддерживаются только текстовые UTF-8 файлы") from e
    return {"path": raw, "content": text, "bytes": len(data)}


def write_file(raw: str, content: str) -> dict:
    if not isinstance(content, str):
        raise WorkspaceError("content должен быть текстом")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES or "\x00" in content:
        raise WorkspaceError("Лимит записи — 256 KiB; нулевые байты запрещены")
    path = safe_path(raw)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Re-check after creating parents; never write through a symlink.
    path = safe_path(raw)
    if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
        raise WorkspaceError("Назначение должно быть обычным файлом, не symlink/reparse point")
    backup = None
    if path.exists():
        if path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError("Отказ перезаписывать существующий файл больше 256 KiB")
        old = path.read_bytes()
        if b"\x00" in old:
            raise WorkspaceError("Отказ перезаписывать бинарный файл")
        try:
            old.decode("utf-8")
        except UnicodeDecodeError as e:
            raise WorkspaceError("Отказ перезаписывать файл, который не является UTF-8") from e
        if _is_link_or_reparse(BACKUP_ROOT):
            raise WorkspaceError("Каталог резервных копий не может быть symlink/reparse point")
        BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not host.is_windows() and BACKUP_ROOT.stat().st_uid != os.getuid():
            raise WorkspaceError("Каталог резервных копий принадлежит другому пользователю")
        paths.secure_chmod(BACKUP_ROOT, 0o700)
        backup = BACKUP_ROOT / (str(time.time_ns()) + ".bak")
        fd_b = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd_b, "wb") as f:
            f.write(old)
        old_backups = sorted(BACKUP_ROOT.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old_backups[50:]:
            try:
                stale.unlink()
            except OSError:
                pass
    fd, tmp = tempfile.mkstemp(prefix=".mintbridge-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        paths.secure_chmod(Path(tmp), 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return {"ok": True, "path": raw, "bytes": len(data), "backup": backup.name if backup else None}


PUBLIC_SCRIPT_MAX_BYTES = 64 * 1024


def create_public_asset(filename: str, content: str) -> dict:
    """Create one new UTF-8 C# or ShaderLab asset in the workspace root; never overwrite."""
    allowed_suffixes = {".cs", ".shader"}
    if not isinstance(filename, str) or not filename or len(filename) > 120:
        raise WorkspaceError("filename must be a short .cs or .shader filename")
    if filename in (".", "..") or any(ch in filename for ch in ("/", "\\", ":")):
        raise WorkspaceError("Only a filename is accepted; subdirectories are not allowed")
    if Path(filename).name != filename or Path(filename).suffix.lower() not in allowed_suffixes:
        raise WorkspaceError("Only .cs and .shader files are accepted")
    if any(ord(ch) < 32 for ch in filename):
        raise WorkspaceError("Control characters are not allowed in filenames")
    if not isinstance(content, str) or "\x00" in content:
        raise WorkspaceError("content must be UTF-8 text without NUL characters")
    data = content.encode("utf-8")
    if len(data) > PUBLIC_SCRIPT_MAX_BYTES:
        raise WorkspaceError("Public Unity asset uploads are limited to 64 KiB")

    target = safe_path(filename)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError as e:
        raise WorkspaceError("File already exists; public upload never overwrites") from e
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return {"ok": True, "path": filename, "bytes": len(data)}


def create_public_script(filename: str, content: str) -> dict:
    """Backward-compatible .cs-only public upload helper."""
    if not isinstance(filename, str) or not filename.lower().endswith(".cs"):
        raise WorkspaceError("Only a .cs filename is accepted")
    return create_public_asset(filename, content)


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n… вывод обрезан …"
    return text


def run_command(command: str, timeout_seconds: int) -> dict:
    if not isinstance(command, str) or not command.strip() or len(command) > 4000:
        raise WorkspaceError("command должен содержать 1–4000 символов")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_TIMEOUT:
        raise WorkspaceError("timeout_seconds должен быть от 1 до 3600; срок выбирает вызывающий по задаче")
    if host.is_windows():
        raise WorkspaceError("Запуск из проектной зоны на Windows пока закрыт: нет проверенной изоляционной песочницы. Используйте только явно включаемый удалённый shell.")
    bwrap = shutil.which("bwrap")
    prlimit = shutil.which("prlimit")
    if bwrap is None:
        raise WorkspaceError("Запуск закрыт: bubblewrap не установлен. Установите пакет bubblewrap; небезопасного запуска без песочницы нет.")
    if prlimit is None:
        raise WorkspaceError("Запуск закрыт: не найден prlimit из util-linux; небезопасного запуска без лимитов нет.")
    root = ensure_root()
    argv = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    # Only runtime binaries and libraries are exposed read-only; host /etc and home are not mounted.
    mounted_targets = set()
    for host_path, guest_path in (("/usr", "/usr"), ("/bin", "/bin"), ("/sbin", "/sbin"),
                                  ("/lib", "/lib"), ("/lib64", "/lib64")):
        p = Path(host_path)
        if p.exists() and guest_path not in mounted_targets:
            argv += ["--ro-bind", str(p.resolve()), guest_path]
            mounted_targets.add(guest_path)
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/home",
             "--dir", "/home/runner", "--bind", str(root), "/workspace", "--chdir", "/workspace",
             "--setenv", "HOME", "/home/runner", "--setenv", "TMPDIR", "/tmp",
             "--setenv", "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "--setenv", "LANG", "C.UTF-8", "--", "/bin/bash", "-lc", command]
    proc = None
    try:
        limits = [prlimit, f"--cpu={timeout_seconds + 5}:{timeout_seconds + 5}",
                  "--fsize=268435456:268435456", "--nofile=256:256", "--nproc=64:64",
                  "--as=2147483648:2147483648", "--", *argv]
        proc = subprocess.Popen(limits, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=timeout_seconds)
            return {"status": "done" if proc.returncode == 0 else "error",
                    "return_code": proc.returncode, "output": _truncate(output or "(без вывода)"),
                    "timeout_seconds": timeout_seconds}
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                output, _ = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                output, _ = proc.communicate()
            return {"status": "timeout", "return_code": 124,
                    "output": _truncate((output or "") + f"\nОстановлено по таймауту {timeout_seconds} секунд."),
                    "timeout_seconds": timeout_seconds}
    except Exception as e:
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise WorkspaceError(f"Не удалось запустить команду в песочнице: {e}") from e

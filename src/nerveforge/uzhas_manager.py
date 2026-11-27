"""Explicit, locally armed shell runner with timeout and process-tree cancellation."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from threading import Event

from . import platform_support as host

MAX_TIMEOUT = 3600
MAX_OUTPUT = 50 * 1024 * 1024


class UzhasError(ValueError):
    pass



def set_limits(max_output: int | None = None) -> dict:
    global MAX_OUTPUT
    if max_output is not None and isinstance(max_output, int) and max_output > 0:
        MAX_OUTPUT = max_output
    return {"max_output_bytes": MAX_OUTPUT}

def _shorten(output: str) -> str:
    return output if len(output) <= MAX_OUTPUT else output[:MAX_OUTPUT] + "\n… вывод обрезан …"


def supported_shells() -> set[str]:
    return {item["id"] for item in host.available_shells()}


def default_shell() -> str | None:
    return host.default_shell()


def build_argv(command: str, shell: str | None = None, sudo: bool = False) -> list[str]:
    """Resolve a user-selected shell without invoking an extra implicit shell."""
    windows = host.is_windows()
    chosen = shell or default_shell()
    if not chosen:
        raise UzhasError("На этой системе не обнаружена поддерживаемая командная оболочка")
    if chosen not in supported_shells():
        raise UzhasError(f"Оболочка {chosen!r} недоступна на этой системе")
    if windows:
        if sudo:
            raise UzhasError("Повышение прав Windows не поддерживается. Агент работает только от текущего пользователя.")
        if chosen == "cmd":
            executable = shutil.which("cmd.exe") or shutil.which("cmd") or os.environ.get("COMSPEC")
            if not executable:
                raise UzhasError("Не найден cmd.exe")
            return [executable, "/d", "/s", "/c", command]
        if chosen == "powershell":
            executable = (shutil.which("pwsh.exe") or shutil.which("pwsh") or
                          shutil.which("powershell.exe") or shutil.which("powershell"))
            if not executable:
                raise UzhasError("Не найден PowerShell (pwsh.exe или powershell.exe)")
            return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        raise UzhasError("На Windows поддерживаются только PowerShell и CMD")
    if chosen != "bash":
        raise UzhasError("В Linux поддерживается Bash")
    bash = shutil.which("bash")
    if not bash:
        raise UzhasError("Не найден bash")
    if sudo:
        sudo_bin = shutil.which("sudo")
        if not sudo_bin:
            raise UzhasError("Запрошен sudo, но команда sudo не найдена")
        return [sudo_bin, "-n", bash, "-c", command]
    return [bash, "-lc", command]


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    for k in list(env.keys()):
        if k.startswith("NERVEFORGE_TEST_SECRET"):
            del env[k]
    if host.is_windows():
        env.setdefault("SYSTEMROOT", r"C:\Windows")
        env.setdefault("PATH", r"C:\Windows\System32;C:\Windows")
        env.setdefault("COMSPEC", env["SYSTEMROOT"] + r"\System32\cmd.exe")
    else:
        env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        env.setdefault("HOME", str(Path.home()))
    return env


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if host.is_windows():
        taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
        if taskkill and proc.poll() is None:
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, check=False,
                               creationflags=flags)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass


def run(command: str, timeout_seconds: int, sudo: bool = False,
        shell: str | None = None, cancel_event: Event | None = None,
        cwd: str | Path | None = None) -> dict:
    if not isinstance(command, str) or not command.strip():
        raise UzhasError("command не должен быть пустым")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_TIMEOUT:
        raise UzhasError("timeout_seconds должен быть от 1 до 3600")
    if not isinstance(sudo, bool):
        raise UzhasError("sudo должен быть boolean")
    if cancel_event is not None and cancel_event.is_set():
        return {"status": "cancelled", "return_code": 130, "output": "Доступ отключён до запуска команды.",
                "timeout_seconds": timeout_seconds, "shell": shell or default_shell(), "sudo_requested": sudo}
    argv = build_argv(command, shell, sudo)
    working_directory = Path(cwd).expanduser() if cwd is not None else Path.home()
    if not working_directory.is_dir():
        raise UzhasError(f"Рабочая папка не найдена или не является каталогом: {working_directory}")
    windows = host.is_windows()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if windows else 0
    kwargs = {"cwd": str(working_directory), "env": _child_environment(),
              "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.STDOUT, "text": True, "errors": "replace",
              "creationflags": creationflags}
    if not windows:
        kwargs["start_new_session"] = True
    proc = None
    try:
        proc = subprocess.Popen(argv, **kwargs)
        deadline = time.monotonic() + timeout_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process_tree(proc)
                output, _ = proc.communicate(timeout=5)
                return {"status": "cancelled", "return_code": 130,
                        "output": _shorten((output or "") + "\nЗапуск и дочерние процессы остановлены: удалённый доступ выключен."),
                        "timeout_seconds": timeout_seconds, "shell": shell or default_shell(), "sudo_requested": sudo}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_tree(proc)
                output, _ = proc.communicate(timeout=5)
                return {"status": "timeout", "return_code": 124,
                        "output": _shorten((output or "") + f"\nОстановлено по таймауту {timeout_seconds} секунд."),
                        "timeout_seconds": timeout_seconds, "shell": shell or default_shell(), "sudo_requested": sudo}
            try:
                output, _ = proc.communicate(timeout=min(0.25, remaining))
                output = output or "(без вывода)"
                if len(output) > MAX_OUTPUT:
                    output = output[:MAX_OUTPUT] + "\n… вывод обрезан …"
                return {"status": "done" if proc.returncode == 0 else "error",
                        "return_code": proc.returncode, "output": output,
                        "timeout_seconds": timeout_seconds, "shell": shell or default_shell(), "sudo_requested": sudo}
            except subprocess.TimeoutExpired:
                continue
    except Exception as e:
        if proc and proc.poll() is None:
            _terminate_process_tree(proc)
        raise UzhasError(f"Не удалось выполнить команду: {e}") from e

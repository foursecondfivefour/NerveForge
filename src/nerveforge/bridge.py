#!/usr/bin/env python3
"""Local, approval-gated HTTP bridge for Linux Mint. Python stdlib only."""
from __future__ import annotations

import hashlib
import hmac
from collections import deque
import json
import os
import re
import secrets
import shutil
import signal
import socket
import concurrent.futures
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config_manager as cfg
from . import platform_support as host
from . import paths
from . import workspace_manager as ws
from . import uzhas_manager as uzhas

HOST = os.environ.get("NERVEFORGE_BIND", "127.0.0.1")
PORT = int(os.environ.get("MINT_BRIDGE_PORT", "8766"))
MAX_BODY = 64 * 1024 * 1024
MAX_COMMAND = 40 * 1024 * 1024
MAX_OUTPUT = 50 * 1024 * 1024
COMMAND_TIMEOUT = 120
MAX_PLAN_STEPS = 8
FULL_ACCESS_PHRASE = "yesiknow"
VERBOSE_HTTP = os.environ.get("NERVEFORGE_VERBOSE_HTTP") == "1"
REQUEST_LOG: deque[dict] = deque(maxlen=1000)

CONFIG_DIR = paths.config_dir()
TOKEN_FILE = CONFIG_DIR / "token"
DATA_DIR = paths.data_dir()
SCREEN_DIR = DATA_DIR / "screenshots"
PROJECT_DIR = Path(__file__).resolve().parents[2]
ASSET_DIR = PROJECT_DIR / "assets"
STATIC_DIR = PROJECT_DIR / "static"

lock = threading.RLock()
async_executor = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix='nf-async')
commands: dict[str, dict] = {}
active_id: str | None = None
config_monitor = cfg.ConfigMonitor()
yolo_enabled = False  # resets to OFF on every receiver restart
_yolo_calls = deque()
_workspace_calls = deque()
_public_script_calls_minute = deque()
_public_script_calls_hour = deque()
PUBLIC_SCRIPT_MAX_PER_MINUTE = 5
PUBLIC_SCRIPT_MAX_PER_HOUR = 30
workspace_jobs: dict[str, dict] = {}
workspace_run_lock = threading.Lock()
uzhas_enabled = False  # local-panel-only arm; always OFF after receiver restart
uzhas_last_activity = 0.0
UZHAS_IDLE_SECONDS = 86400 * 365
UZHAS_MAX_ARM_SECONDS = 86400 * 365
uzhas_duration_seconds = 86400 * 365
uzhas_armed_until = 0.0
_uzhas_calls = deque()
uzhas_jobs: dict[str, dict] = {}
uzhas_job_cancel: dict[str, threading.Event] = {}
uzhas_run_lock = threading.Lock()


def load_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if len(token) < 32:
        token = secrets.token_urlsafe(36)
        TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
        try:
            paths.secure_chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
        print(f"\nНовый токен создан: {TOKEN_FILE}\nТокен (передайте его только в этот чат, если хотите, чтобы я отправлял запросы):\n{token}\n", flush=True)
    return token

TOKEN = load_token()



def get_limits() -> dict:
    return {
        "max_command_bytes": MAX_COMMAND,
        "max_command_mb": round(MAX_COMMAND / (1024 * 1024), 2),
        "max_output_bytes": MAX_OUTPUT,
        "max_output_mb": round(MAX_OUTPUT / (1024 * 1024), 2),
        "max_body_bytes": MAX_BODY,
        "max_body_mb": round(MAX_BODY / (1024 * 1024), 2),
    }


def set_limits(max_command: int | None = None,
               max_output: int | None = None,
               max_body: int | None = None) -> dict:
    global MAX_COMMAND, MAX_OUTPUT, MAX_BODY
    if max_command is not None and isinstance(max_command, int) and max_command > 0:
        MAX_COMMAND = max_command
    if max_output is not None and isinstance(max_output, int) and max_output > 0:
        MAX_OUTPUT = max_output
        uzhas.set_limits(max_output)
    if max_body is not None and isinstance(max_body, int) and max_body > 0:
        MAX_BODY = max_body
    print(f"[limits] обновлены: command={MAX_COMMAND}B ({round(MAX_COMMAND/(1024*1024),1)}MB), output={MAX_OUTPUT}B ({round(MAX_OUTPUT/(1024*1024),1)}MB), body={MAX_BODY}B ({round(MAX_BODY/(1024*1024),1)}MB)", flush=True)
    return get_limits()

def shorten(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… вывод обрезан …"


def public_command(c: dict, local_ui: bool = False) -> dict:
    blocked = {"screenshot_path", "content", "old_hash"}
    if not local_ui:
        blocked.add("diff")
    return {k: v for k, v in c.items() if k not in blocked}


def register_pending(item: dict) -> tuple[dict | None, str | None]:
    with lock:
        if active_id is not None or any(c["status"] in ("pending", "executing") for c in commands.values()):
            return None, "another command is pending or running; decide/finish it first"
        cid = str(uuid.uuid4())
        item.update({"id": cid, "status": "pending", "created_at": time.time(), "output": None, "return_code": None})
        commands[cid] = item
        return item, None


def run_command(cmd: dict) -> tuple[int, str]:
    shell_command = cmd["command"]
    # Bridge is intentionally not a privilege-escalation mechanism.
    if re.search(r"\bsudo\b", shell_command):
        return 126, "Отклонено: sudo через мост не поддерживается. Выполните административную операцию вручную в локальном терминале."
    cwd = Path(cmd.get("cwd") or str(Path.home())).expanduser()
    if not cwd.is_dir():
        return 126, f"Рабочая папка не найдена или не является каталогом: {cwd}"
    try:
        result = uzhas.run(shell_command, COMMAND_TIMEOUT, False,
                           cmd.get("shell") or uzhas.default_shell(), cwd=cwd)
        return int(result.get("return_code", 1)), shorten(result.get("output") or "(команда завершилась без вывода)")
    except Exception as e:
        return 126, f"Не удалось выполнить команду: {e}"


def take_screenshot(cmd_id: str) -> tuple[int, str, str | None]:
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREEN_DIR / f"{cmd_id}.png"
    candidates = []
    if shutil.which("gnome-screenshot"):
        candidates.append(["gnome-screenshot", "-f", str(path)])
    if shutil.which("mate-screenshot"):
        candidates.append(["mate-screenshot", "-f", str(path)])
    if shutil.which("scrot"):
        candidates.append(["scrot", str(path)])
    if not candidates:
        return 127, "Не найдена утилита снимка экрана (gnome-screenshot, mate-screenshot или scrot).", None
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return 1, "Не найдена графическая сессия DISPLAY/WAYLAND_DISPLAY. Запустите мост из терминала внутри графического сеанса.", None
    errors = []
    for argv in candidates:
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=20, text=True, errors="replace")
            if p.returncode == 0 and path.exists() and path.stat().st_size:
                if path.stat().st_size > 20 * 1024 * 1024:
                    path.unlink(missing_ok=True)
                    return 1, "Скриншот превышает лимит 20 МБ.", None
                return 0, f"Скриншот готов ({path.stat().st_size} байт).", str(path)
            errors.append(f"{' '.join(argv)}: {p.stdout.strip() or 'код ' + str(p.returncode)}")
        except Exception as e:
            errors.append(f"{' '.join(argv)}: {e}")
    return 1, "Не удалось сделать скриншот. " + " | ".join(errors), None


def execute_approved(cmd_id: str) -> None:
    global active_id
    with lock:
        cmd = commands.get(cmd_id)
        if not cmd or cmd["status"] != "executing":
            active_id = None
            return
        kind = cmd["kind"]
    try:
        if kind == "command":
            code, output = run_command(cmd)
            extra = {"return_code": code, "output": output}
        elif kind == "plan":
            step_results = []
            code = 0
            for index, step in enumerate(cmd["steps"], 1):
                with lock:
                    current = commands.get(cmd_id)
                    if current:
                        current["current_step"] = index
                step_code, step_output = run_command(step)
                result = {"index": index, "command": step["command"], "cwd": step["cwd"],
                          "shell": step.get("shell", uzhas.default_shell()),
                          "return_code": step_code, "output": step_output,
                          "status": "done" if step_code == 0 else "error"}
                step_results.append(result)
                with lock:
                    current = commands.get(cmd_id)
                    if current:
                        current["step_results"] = list(step_results)
                if step_code != 0:
                    code = step_code
                    break
            output = f"Выполнено шагов: {len(step_results)} из {len(cmd['steps'])}."
            if len(step_results) < len(cmd["steps"]):
                output += " Остальные шаги пропущены после ошибки."
            extra = {"return_code": code, "output": output, "step_results": step_results}
        elif kind == "config_edit":
            result = cfg.apply_edit(cmd["path"], cmd["content"], cmd.get("old_hash"))
            code, output = 0, json.dumps(result, ensure_ascii=False)
            extra = {"return_code": code, "output": output, "backup": result.get("backup")}
        elif kind == "system_action":
            result = cfg.run_system_action(cmd["action"], cmd.get("unit"))
            code, output = 0, json.dumps(result, ensure_ascii=False)
            extra = {"return_code": code, "output": output}
        elif kind == "screenshot":
            code, output, screenshot_path = take_screenshot(cmd_id)
            extra = {"return_code": code, "output": output, "screenshot_path": screenshot_path}
        else:
            raise RuntimeError("unsupported request kind")
        status = "done" if code == 0 else "error"
    except Exception as e:
        status = "error"
        extra = {"return_code": 1, "output": f"Операция остановлена: {e}"}
    with lock:
        cmd = commands.get(cmd_id)
        if cmd:
            cmd.update(extra)
            cmd["status"] = status
            cmd["finished_at"] = time.time()
        active_id = None


def yolo_audit(action: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / 'yolo-readonly.jsonl'
    try:
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.replace(path.with_suffix('.old'))
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': time.time(), 'action': action}, ensure_ascii=False) + '\n')
        paths.secure_chmod(path, 0o600)
    except OSError as e:
        print(f'[yolo-audit] {e}', flush=True)


def workspace_allowed() -> bool:
    with lock:
        return yolo_enabled


def take_workspace_rate_slot() -> bool:
    now = time.monotonic()
    with lock:
        while _workspace_calls and now - _workspace_calls[0] > 60:
            _workspace_calls.popleft()
        if len(_workspace_calls) >= 60:
            return False
        _workspace_calls.append(now)
        return True


def take_public_script_slot() -> bool:
    """Global anonymous-upload throttles; IP headers are not trusted behind tunnels."""
    now = time.monotonic()
    with lock:
        while _public_script_calls_minute and now - _public_script_calls_minute[0] > 60:
            _public_script_calls_minute.popleft()
        while _public_script_calls_hour and now - _public_script_calls_hour[0] > 3600:
            _public_script_calls_hour.popleft()
        if (len(_public_script_calls_minute) >= PUBLIC_SCRIPT_MAX_PER_MINUTE
                or len(_public_script_calls_hour) >= PUBLIC_SCRIPT_MAX_PER_HOUR):
            return False
        _public_script_calls_minute.append(now)
        _public_script_calls_hour.append(now)
        return True


def workspace_audit(action: str, relpath: str = "") -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "workspace.jsonl"
    try:
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.replace(path.with_suffix(".old"))
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.time(), "action": action, "path": relpath[:1000]}, ensure_ascii=False) + "\n")
        paths.secure_chmod(path, 0o600)
    except OSError as e:
        print(f"[workspace-audit] {e}", flush=True)


def execute_workspace_job(job_id: str, command: str, timeout_seconds: int) -> None:
    with lock:
        job = workspace_jobs.get(job_id)
        if not job:
            return
        if not yolo_enabled:
            job.update({"status": "error", "return_code": 403,
                        "output": "Project YOLO был выключен до старта задачи.", "finished_at": time.time()})
            disabled = True
        else:
            job["status"] = "running"
            job["started_at"] = time.time()
            disabled = False
    if disabled:
        workspace_audit("run-cancelled-before-start", job_id)
        return
    try:
        if not workspace_run_lock.acquire(blocking=False):
            result = {"status": "error", "return_code": 409, "output": "Уже выполняется другая команда Project YOLO."}
        else:
            try:
                result = ws.run_command(command, timeout_seconds)
            finally:
                workspace_run_lock.release()
    except Exception as e:
        result = {"status": "error", "return_code": 1, "output": str(e)}
    with lock:
        job = workspace_jobs.get(job_id)
        if job:
            job.update(result)
            job["finished_at"] = time.time()
    workspace_audit("run-finished", job_id)


def _cancel_uzhas_jobs_locked() -> None:
    for event in uzhas_job_cancel.values():
        event.set()


def disarm_uzhas(reason: str = "local disable") -> None:
    global uzhas_enabled
    with lock:
        was_enabled = uzhas_enabled
        uzhas_enabled = False
        _cancel_uzhas_jobs_locked()
    if was_enabled:
        print(f"[uzhas] disarmed ({reason}); active process trees are being stopped", flush=True)


def uzhas_state() -> dict:
    global uzhas_enabled
    now = time.monotonic()
    reason = None
    with lock:
        job_running = any(job.get("status") == "running" for job in uzhas_jobs.values())
        idle_remaining = max(0, int(UZHAS_IDLE_SECONDS - (now - uzhas_last_activity))) if uzhas_enabled and not job_running else 0
        max_remaining = max(0, int(uzhas_armed_until - now)) if uzhas_enabled else 0
        if uzhas_enabled and not job_running and idle_remaining <= 0:
            reason = "idle timeout"
        elif uzhas_enabled and max_remaining <= 0:
            reason = "maximum arm duration reached"
        if reason:
            uzhas_enabled = False
            _cancel_uzhas_jobs_locked()
        armed = uzhas_enabled
        remaining = max_remaining if job_running else min(idle_remaining, max_remaining)
        result = {"armed": armed, "idle_timeout_seconds": UZHAS_IDLE_SECONDS,
                  "duration_seconds": uzhas_duration_seconds,
                  "seconds_remaining": remaining if armed else 0,
                  "idle_seconds_remaining": idle_remaining if armed else 0,
                  "max_seconds_remaining": max_remaining if armed else 0,
                  "seconds_since_activity": int(now - uzhas_last_activity) if armed else 0,
                  "job_running": job_running}
    if reason:
        print(f"[uzhas] automatically disarmed: {reason}; active process trees are being stopped", flush=True)
    return result


def uzhas_idle_monitor() -> None:
    while True:
        time.sleep(1)
        uzhas_state()


def uzhas_audit(action: str, job_id: str = "", command: str = "", sudo: bool = False, shell: str | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "uzhas.jsonl"
    try:
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.replace(path.with_suffix(".old"))
        record = {"time": time.time(), "action": action, "job_id": job_id,
                  "sudo_requested": bool(sudo), "shell": shell,
                  "command_sha256": hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest() if command else None}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        paths.secure_chmod(path, 0o600)
    except OSError as e:
        print(f"[uzhas-audit] {e}", flush=True)


def take_uzhas_rate_slot() -> bool:
    return True


def execute_uzhas_job(job_id: str, command: str, timeout_seconds: int, sudo: bool,
                      shell: str, cancel_event: threading.Event,
                      cwd: str | None = None) -> None:
    global uzhas_last_activity
    state = uzhas_state()
    with lock:
        job = uzhas_jobs.get(job_id)
        if not job:
            return
        if not state["armed"] or cancel_event.is_set():
            job.update({"status": "error", "return_code": 403,
                        "output": "У.Ж.А.С. выключен или истёк таймер активации.", "finished_at": time.time()})
            disarmed = True
        else:
            job["status"] = "running"
            job["started_at"] = time.time()
            disarmed = False
    if disarmed:
        with lock:
            uzhas_job_cancel.pop(job_id, None)
        uzhas_audit("cancelled-before-start", job_id, command, sudo, shell)
        return
    try:
        result = uzhas.run(command, timeout_seconds, sudo, shell, cancel_event, cwd=cwd)
    except Exception as e:
        result = {"status": "error", "return_code": 1, "output": str(e), "shell": shell}
    with lock:
        job = uzhas_jobs.get(job_id)
        if job:
            job.update(result)
            job["finished_at"] = time.time()
        uzhas_job_cancel.pop(job_id, None)
        if uzhas_enabled:
            uzhas_last_activity = time.monotonic()
    uzhas_audit("run-finished", job_id, command, sudo, shell)


def run_yolo_action(data: dict) -> dict:
    action = data.get('action')
    if action == 'hardware_report':
        return cfg.hardware_report()
    if action == 'system_report':
        return cfg.system_report()
    if action == 'config_scan':
        return config_monitor.scan()
    if action == 'config_events':
        return config_monitor.state()
    if action == 'service_status':
        return cfg.service_status(str(data.get('unit', '')), str(data.get('scope', 'system')))
    raise ValueError('action is not on the YOLO read-only allowlist')


def set_full_access(armed: bool, duration_seconds: int | None = None,
                    confirmation: str | None = None, *, require_phrase: bool = True) -> dict:
    """Arm/disarm unrestricted shell mode. The phrase is a deliberate UI guard."""
    global uzhas_enabled, uzhas_last_activity, uzhas_duration_seconds, uzhas_armed_until
    if armed and require_phrase and (confirmation or "").strip().lower() != FULL_ACCESS_PHRASE.lower():
        raise ValueError(f"Для полного доступа нужно ввести точно: {FULL_ACCESS_PHRASE}")
    duration = uzhas_duration_seconds if duration_seconds is None else duration_seconds
    if not isinstance(duration, int) or isinstance(duration, bool) or not 60 <= duration <= UZHAS_MAX_ARM_SECONDS:
        raise ValueError("duration_seconds must be 60–86400")
    now = time.monotonic()
    with lock:
        uzhas_duration_seconds = duration
        uzhas_enabled = bool(armed)
        uzhas_last_activity = now if armed else 0.0
        uzhas_armed_until = now + duration if armed else 0.0
        if not armed:
            _cancel_uzhas_jobs_locked()
    uzhas_audit("armed" if armed else "disarmed", shell="full-access")
    print(f"[full-access] {'ARMED for ' + str(duration) + 's' if armed else 'disarmed'}", flush=True)
    return uzhas_state()


def tunnel_state() -> dict:
    try:
        payload = json.loads((DATA_DIR / "tunnel.json").read_text(encoding="utf-8"))
        return {"mode": payload.get("mode", "none"), "url": payload.get("url"),
                "updated_at": payload.get("updated_at")}
    except (OSError, ValueError):
        return {"mode": "none", "url": None, "updated_at": None}


def quick_state() -> dict:
    with lock:
        pending = sum(1 for c in commands.values() if c.get("status") == "pending")
    platform_data = host.platform_info()
    platform_data["workspace_root"] = str(ws.ROOT)
    return {
        "platform": platform_data,
        "tunnel": tunnel_state(),
        "full_access": uzhas_state(),
        "pending": pending,
        "capabilities": {
            "tailscale": bool(shutil.which("tailscale") or shutil.which("tailscale.exe")),
            "cloudflared": bool(shutil.which("cloudflared") or shutil.which("cloudflared.exe")),
            "bubblewrap": bool(shutil.which("bwrap")),
        },
        "limits": get_limits(),
        "local_admin_port": 8767,
        "phrase": FULL_ACCESS_PHRASE,
    }


def recent_events(limit: int = 60) -> list[dict]:
    records: list[dict] = []
    for name in ("uzhas.jsonl", "workspace.jsonl", "yolo-readonly.jsonl"):
        path = DATA_DIR / name
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
                item = json.loads(line)
                item["source"] = name
                records.append(item)
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda item: item.get("time", 0), reverse=True)[:limit]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    wbufsize = 64 * 1024
    server_version = "NerveForge/1.0"

    def log_message(self, fmt, *args):
        # Keep HTTP noise in the panel instead of the launcher terminal.
        # Never store command text, tokens or request bodies.
        status = args[1] if len(args) > 1 else ""
        entry = {
            "time": time.time(),
            "method": self.command,
            "path": urlparse(self.path).path,
            "status": status,
            "client": self.client_address[0] if self.client_address else "",
        }
        with lock:
            REQUEST_LOG.append(entry)
        if VERBOSE_HTTP:
            print(f"[http] {self.command} {entry['path']} {status}", flush=True)

    def send_bytes(self, data: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status: int = 200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def send_public_script_json(self, obj, status: int = 200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        value = supplied[7:] if supplied.startswith("Bearer ") else ""
        if not hmac.compare_digest(value.encode(), TOKEN.encode()):
            self.send_json({"error": "unauthorized"}, 401)
            return False
        return True

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length < 0 or length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError("invalid JSON") from e
        if not isinstance(obj, dict):
            raise ValueError("JSON body must be an object")
        return obj

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                return self.send_bytes((STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return self.send_json({"error": "panel unavailable"}, 404)
        if path in ("/static/app.css", "/static/app.js"):
            name = path.rsplit("/", 1)[-1]
            content_type = "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
            try:
                return self.send_bytes((STATIC_DIR / name).read_bytes(), content_type)
            except OSError:
                return self.send_json({"error": "panel asset unavailable"}, 404)
        if path == "/assets/nerveforge-mark.png":
            try:
                return self.send_bytes((ASSET_DIR / "nerveforge-mark.png").read_bytes(), "image/png")
            except OSError:
                return self.send_json({"error": "brand asset unavailable"}, 404)
        if path == "/api/health":
            return self.send_json({"ok": True, "service": "nerveforge"})
        if not self.authorized():
            return
        local_ui = self.headers.get("Host", "").startswith(("127.0.0.1", "localhost"))
        if path == "/api/limits":
            return self.send_json(get_limits())
        if path == "/api/quick":
            return self.send_json(quick_state())
        if path == "/api/tunnel":
            return self.send_json(tunnel_state())
        if path == "/api/events":
            return self.send_json({"events": recent_events()})
        if path == "/api/requests":
            try:
                limit = int(parse_qs(urlparse(self.path).query).get("limit", ["200"])[0])
            except (ValueError, IndexError):
                limit = 200
            limit = max(1, min(limit, 1000))
            with lock:
                entries = list(REQUEST_LOG)[-limit:]
            return self.send_json({"requests": list(reversed(entries))})
        if path == "/api/full-access/state":
            return self.send_json(uzhas_state())
        if path == "/api/state":
            with lock:
                ordered = sorted(commands.values(), key=lambda c: c["created_at"], reverse=True)[:30]
                platform_data = host.platform_info()
                platform_data["workspace_root"] = str(ws.ROOT)
                return self.send_json({"active": active_id, "yolo_enabled": yolo_enabled,
                                       "platform": platform_data,
                                       "commands": [public_command(c, local_ui) for c in ordered]})
        if path == "/api/system/report":
            return self.send_json(cfg.system_report())
        if path == "/api/configs":
            return self.send_json(config_monitor.state())
        workspace_job_prefix = "/api/workspace/jobs/"
        if path.startswith(workspace_job_prefix):
            job_id = path[len(workspace_job_prefix):].strip("/")
            with lock:
                job = workspace_jobs.get(job_id)
                if not job:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json(dict(job))
        uzhas_job_prefix = "/api/uzhas/jobs/"
        full_job_prefix = "/api/full-access/jobs/"
        if path.startswith((uzhas_job_prefix, full_job_prefix)):
            prefix_used = full_job_prefix if path.startswith(full_job_prefix) else uzhas_job_prefix
            job_id = path[len(prefix_used):].strip("/")
            with lock:
                job = uzhas_jobs.get(job_id)
                if not job:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json(dict(job))
        prefix = "/api/commands/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            if rest.endswith("/screenshot"):
                cmd_id = rest[:-len("/screenshot")].strip("/")
                with lock:
                    cmd = commands.get(cmd_id)
                    img = cmd.get("screenshot_path") if cmd else None
                if not img or not Path(img).is_file():
                    return self.send_json({"error": "screenshot not found"}, 404)
                return self.send_bytes(Path(img).read_bytes(), "image/png")
            cmd_id = rest.strip("/")
            with lock:
                cmd = commands.get(cmd_id)
                if not cmd:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json(public_command(cmd, local_ui))
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        global active_id, uzhas_last_activity
        path = urlparse(self.path).path
        # Deliberately public, write-only endpoints: create-only Unity assets in
        # the configured workspace root. All other APIs still require a bearer token.
        if path in ("/api/public/scripts", "/api/public/assets"):
            if not take_public_script_slot():
                return self.send_public_script_json({"error": "anonymous Unity upload limit reached"}, 429)
            try:
                data = self.read_json()
                if set(data) != {"filename", "content"}:
                    return self.send_public_script_json({"error": "filename and content are required"}, 400)
                if path == "/api/public/scripts":
                    result = ws.create_public_script(data["filename"], data["content"])
                else:
                    result = ws.create_public_asset(data["filename"], data["content"])
                workspace_audit("anonymous-unity-asset-created", result["path"])
                return self.send_public_script_json({"ok": True, "result": result}, 201)
            except ws.WorkspaceError as e:
                status = 409 if "already exists" in str(e).lower() else 400
                return self.send_public_script_json({"error": str(e)}, status)
            except ValueError as e:
                return self.send_public_script_json({"error": str(e)}, 400)
            except Exception:
                return self.send_public_script_json({"error": "script creation failed"}, 500)
        if path == "/api/limits":
            if not isinstance(data, dict):
                return self.send_json({"error": "JSON object required"}, 400)
            max_cmd = data.get("max_command") or data.get("max_command_bytes")
            max_out = data.get("max_output") or data.get("max_output_bytes")
            max_b = data.get("max_body") or data.get("max_body_bytes")
            if "max_command_mb" in data:
                max_cmd = int(data["max_command_mb"] * 1024 * 1024)
            if "max_output_mb" in data:
                max_out = int(data["max_output_mb"] * 1024 * 1024)
            if "max_body_mb" in data:
                max_b = int(data["max_body_mb"] * 1024 * 1024)
            return self.send_json(set_limits(max_cmd, max_out, max_b))
        if path == "/api/local-bootstrap":
            # This route is spoofable through any tunnel that forwards the
            # client-supplied Host/Origin headers (observed with Tailscale Funnel).
            # The local panel now obtains the token from the loopback-only
            # listener on port 8767 instead. Kept only as an explicit preview hook.
            if os.environ.get("NERVEFORGE_PREVIEW_BOOTSTRAP") == "1":
                return self.send_json({"token": TOKEN})
            return self.send_json({"error": "use http://127.0.0.1:8767/bootstrap from the local panel"}, 403)
        if not self.authorized():
            return
        try:
            data = self.read_json()
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if path == "/api/full-access/config":
            if set(data) - {"armed", "duration_seconds", "confirmation"}:
                return self.send_json({"error": "only armed, duration_seconds and confirmation are accepted"}, 400)
            armed = data.get("armed")
            if not isinstance(armed, bool):
                return self.send_json({"error": "armed must be boolean"}, 400)
            try:
                state = set_full_access(armed, data.get("duration_seconds"), data.get("confirmation"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json(state)
        if path == "/api/full-access/stop":
            return self.send_json(set_full_access(False))
        if path in ("/api/uzhas/run", "/api/full-access/run"):
            state = uzhas_state()
            if not state["armed"]:
                return self.send_json({"error": "У.Ж.А.С. не активирован локально или истёк таймер доступа"}, 403)
            allowed_keys = {"command", "timeout_seconds", "sudo", "shell", "cwd"}
            if set(data) - allowed_keys or not {"command", "timeout_seconds"}.issubset(data):
                return self.send_json({"error": "required: command, timeout_seconds; optional: sudo, shell, cwd"}, 400)
            command = data.get("command")
            timeout_seconds = data.get("timeout_seconds")
            sudo_requested = data.get("sudo", False)
            shell = data.get("shell", uzhas.default_shell())
            if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
                return self.send_json({"error": f"command must be 1–{MAX_COMMAND} characters"}, 400)
            if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 86400:
                return self.send_json({"error": "timeout_seconds must be an integer from 1 to 3600"}, 400)
            if not isinstance(sudo_requested, bool):
                return self.send_json({"error": "sudo must be boolean; true explicitly requests sudo -n"}, 400)
            if not isinstance(shell, str):
                return self.send_json({"error": "shell must be a supported shell id"}, 400)
            cwd = data.get("cwd")
            if cwd is not None:
                if not isinstance(cwd, str) or len(cwd) > 1000:
                    return self.send_json({"error": "cwd must be a short string"}, 400)
                cwd = str(Path(cwd).expanduser())
                if not Path(cwd).is_dir():
                    return self.send_json({"error": "cwd is not an existing directory"}, 400)
            try:
                uzhas.build_argv(command, shell, sudo_requested)
            except uzhas.UzhasError as e:
                return self.send_json({"error": str(e)}, 400)
            if not take_uzhas_rate_slot():
                return self.send_json({"error": "У.Ж.А.С. limit: 10 запусков в минуту"}, 429)
            job_id = str(uuid.uuid4())
            cancel_event = threading.Event()
            with lock:
                uzhas_last_activity = time.monotonic()
                if len(uzhas_jobs) >= 100:
                    finished = sorted((j for j in uzhas_jobs.values() if j.get("status") not in ("queued", "running")),
                                      key=lambda j: j.get("created_at", 0))
                    for old in finished[:max(1, len(uzhas_jobs) - 80)]:
                        uzhas_jobs.pop(old["id"], None)
                        uzhas_job_cancel.pop(old["id"], None)
                uzhas_jobs[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(),
                                      "timeout_seconds": timeout_seconds, "sudo_requested": sudo_requested,
                                      "shell": shell, "cwd": cwd}
                uzhas_job_cancel[job_id] = cancel_event
            uzhas_audit("run-queued", job_id, command, sudo_requested, shell)
            async_executor.submit(execute_uzhas_job, job_id, command, timeout_seconds, sudo_requested, shell, cancel_event, cwd)
            return self.send_json({"ok": True, "id": job_id, "status": "queued",
                                   "timeout_seconds": timeout_seconds, "sudo_requested": sudo_requested,
                                   "shell": shell}, 202)
        if path.startswith("/api/workspace/"):
            # Project YOLO OR the deliberate full-access mode unlocks file operations.
            if not workspace_allowed() and not uzhas_state()["armed"]:
                return self.send_json({"error": "Project YOLO или полный доступ выключены"}, 403)
            if not take_workspace_rate_slot():
                return self.send_json({"error": "Project YOLO limit: 60 requests per minute"}, 429)
            try:
                if path == "/api/workspace/list":
                    if set(data) - {"path"}:
                        return self.send_json({"error": "only path is accepted"}, 400)
                    result = ws.list_files(data.get("path", "."))
                    workspace_audit("list", str(data.get("path", ".")))
                    return self.send_json({"ok": True, "result": result})
                if path == "/api/workspace/read":
                    if set(data) != {"path"}:
                        return self.send_json({"error": "path is required; no other fields are accepted"}, 400)
                    result = ws.read_file(data["path"])
                    workspace_audit("read", data["path"])
                    return self.send_json({"ok": True, "result": result})
                if path == "/api/workspace/write":
                    if set(data) != {"path", "content"}:
                        return self.send_json({"error": "path and content are required"}, 400)
                    result = ws.write_file(data["path"], data["content"])
                    workspace_audit("write", data["path"])
                    return self.send_json({"ok": True, "result": result})
                if path == "/api/workspace/run":
                    if set(data) != {"command", "timeout_seconds"}:
                        return self.send_json({"error": "command and timeout_seconds are required"}, 400)
                    command = data["command"]
                    timeout_seconds = data["timeout_seconds"]
                    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
                        return self.send_json({"error": f"command must be 1–{MAX_COMMAND} characters"}, 400)
                    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 86400:
                        return self.send_json({"error": "timeout_seconds must be an integer from 1 to 3600"}, 400)
                    job_id = str(uuid.uuid4())
                    with lock:
                        # Bound in-memory job history.
                        if len(workspace_jobs) >= 100:
                            finished = sorted((j for j in workspace_jobs.values() if j.get("status") not in ("queued", "running")),
                                              key=lambda j: j.get("created_at", 0))
                            for old in finished[:max(1, len(workspace_jobs) - 80)]:
                                workspace_jobs.pop(old["id"], None)
                        workspace_jobs[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(),
                                                  "timeout_seconds": timeout_seconds}
                    workspace_audit("run-queued", job_id)
                    async_executor.submit(execute_workspace_job, job_id, command, timeout_seconds)
                    return self.send_json({"ok": True, "id": job_id, "status": "queued", "timeout_seconds": timeout_seconds}, 202)
                return self.send_json({"error": "unknown workspace endpoint"}, 404)
            except Exception as e:
                workspace_audit("request-error", path.rsplit("/", 1)[-1])
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/yolo/run":
            allowed_keys = {"action", "unit", "scope"}
            if set(data) - allowed_keys:
                return self.send_json({"error": "YOLO accepts only allowlisted action parameters; shell commands are not accepted"}, 400)
            with lock:
                if not yolo_enabled:
                    return self.send_json({"error": "YOLO read-only mode is disabled in the local panel"}, 403)
                now = time.monotonic()
                while _yolo_calls and now - _yolo_calls[0] > 60:
                    _yolo_calls.popleft()
                if len(_yolo_calls) >= 10:
                    return self.send_json({"error": "YOLO limit: 10 read-only requests per minute"}, 429)
                _yolo_calls.append(now)
            action = data.get("action")
            try:
                result = run_yolo_action(data)
                yolo_audit(str(action))
                return self.send_json({"ok": True, "action": action, "result": result})
            except Exception as e:
                yolo_audit(str(action))
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/configs/scan":
            try:
                return self.send_json(config_monitor.scan())
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
        if path == "/api/config-edits":
            raw_path = data.get("path")
            content = data.get("content")
            if not isinstance(raw_path, str) or not isinstance(content, str):
                return self.send_json({"error": "path and content must be strings"}, 400)
            try:
                path_obj, scope, system = cfg.allowed_config_path(raw_path, for_edit=True)
                old = cfg.read_config(path_obj, system=system) if path_obj.exists() else None
                old_hash = hashlib.sha256(old.encode('utf-8')).hexdigest() if old is not None else None
                validation = cfg.validate_text(path_obj, content)
                if system:
                    cfg.ensure_admin()
                diff = cfg.diff_text(path_obj, old or '', content)
                if diff == '(изменений нет)':
                    return self.send_json({"error": "файл уже содержит предложенный текст"}, 400)
                item, error = register_pending({"kind": "config_edit", "path": str(path_obj), "scope": scope,
                                                "content": content, "old_hash": old_hash,
                                                "diff": diff, "validation": validation})
                if error:
                    return self.send_json({"error": error}, 409)
                print(f"[ВНИМАНИЕ] Запрошено изменение системного файла: {raw_path}. Поставлено в очередь подтверждений пользователя (id: {item['id'][:8]}).", flush=True)
                return self.send_json({"id": item["id"], "status": item["status"], "kind": item["kind"],
                                       "path": item["path"], "validation": validation}, 202)
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/system-actions":
            action = data.get("action")
            unit = data.get("unit")
            allowed_actions = ("daemon_reload", "restart_service", "user_daemon_reload", "restart_user_service")
            if action not in allowed_actions:
                return self.send_json({"error": "unsupported systemd action"}, 400)
            if action in ("restart_service", "restart_user_service") and (not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit)):
                return self.send_json({"error": "unit must be a .service name without a path"}, 400)
            if action in ("daemon_reload", "restart_service"):
                try:
                    cfg.ensure_admin()
                except Exception as e:
                    return self.send_json({"error": str(e)}, 400)
            item, error = register_pending({"kind": "system_action", "action": action, "unit": unit})
            if error:
                return self.send_json({"error": error}, 409)
            print(f"[ВНИМАНИЕ] Запрошено системное действие / служба: {action}. Поставлено в очередь подтверждений пользователя (id: {item['id'][:8]}).", flush=True)
            return self.send_json({"id": item["id"], "status": item["status"], "kind": item["kind"]}, 202)
        if path == "/api/commands":
            kind = data.get("kind", "command")
            if kind not in ("command", "screenshot"):
                return self.send_json({"error": "kind must be command or screenshot"}, 400)
            allowed_keys = {"kind", "command", "cwd", "shell"}
            if set(data) - allowed_keys:
                return self.send_json({"error": "unsupported command fields"}, 400)
            text = data.get("command", "")
            shell = data.get("shell", uzhas.default_shell())
            if kind == "command" and (not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMAND):
                return self.send_json({"error": f"command must be 1–{MAX_COMMAND} characters"}, 400)
            if kind == "command":
                if not isinstance(shell, str):
                    return self.send_json({"error": "shell must be a supported shell id"}, 400)
                try:
                    uzhas.build_argv(text, shell, False)
                except uzhas.UzhasError as e:
                    return self.send_json({"error": str(e)}, 400)
            cwd = data.get("cwd") or str(Path.home())
            if not isinstance(cwd, str) or len(cwd) > 1000:
                return self.send_json({"error": "invalid cwd"}, 400)
            cwd = str(Path(cwd).expanduser())
            if not Path(cwd).is_dir():
                return self.send_json({"error": "cwd must be an existing directory"}, 400)
            with lock:
                if active_id is not None or any(c["status"] in ("pending", "executing") for c in commands.values()):
                    return self.send_json({"error": "another command is pending or running; decide/finish it first"}, 409)
                cid = str(uuid.uuid4())
                c = {"id": cid, "kind": kind, "command": text if kind == "command" else "",
                     "shell": shell if kind == "command" else None,
                     "cwd": cwd, "status": "pending", "created_at": time.time(),
                     "output": None, "return_code": None, "screenshot_path": None}
                commands[cid] = c
            print(f"[queue] {kind} {cid[:8]}", flush=True)
            return self.send_json({"id": cid, "status": "pending", "kind": kind}, 202)
        if path == "/api/plans":
            raw_steps = data.get("steps")
            if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_PLAN_STEPS:
                return self.send_json({"error": f"steps must contain 1–{MAX_PLAN_STEPS} items"}, 400)
            steps = []
            for index, raw in enumerate(raw_steps, 1):
                if not isinstance(raw, dict):
                    return self.send_json({"error": f"step {index} must be an object"}, 400)
                if set(raw) - {"command", "cwd", "shell"}:
                    return self.send_json({"error": f"step {index}: unsupported fields"}, 400)
                text = raw.get("command")
                shell = raw.get("shell", uzhas.default_shell())
                cwd = raw.get("cwd") or str(Path.home())
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMAND:
                    return self.send_json({"error": f"step {index}: command must be 1–{MAX_COMMAND} characters"}, 400)
                if not isinstance(cwd, str) or len(cwd) > 1000:
                    return self.send_json({"error": f"step {index}: invalid cwd"}, 400)
                if not isinstance(shell, str):
                    return self.send_json({"error": f"step {index}: shell must be a supported shell id"}, 400)
                try:
                    uzhas.build_argv(text, shell, False)
                except uzhas.UzhasError as e:
                    return self.send_json({"error": f"step {index}: {e}"}, 400)
                cwd = str(Path(cwd).expanduser())
                if not Path(cwd).is_dir():
                    return self.send_json({"error": f"step {index}: cwd must be an existing directory"}, 400)
                steps.append({"command": text, "cwd": cwd, "shell": shell})
            with lock:
                if active_id is not None or any(c["status"] in ("pending", "executing") for c in commands.values()):
                    return self.send_json({"error": "another command is pending or running; decide/finish it first"}, 409)
                cid = str(uuid.uuid4())
                commands[cid] = {"id": cid, "kind": "plan", "steps": steps, "step_results": [],
                                 "current_step": 0, "status": "pending", "created_at": time.time(),
                                 "output": None, "return_code": None}
            print(f"[queue] plan {cid[:8]} ({len(steps)} steps)", flush=True)
            return self.send_json({"id": cid, "status": "pending", "kind": "plan", "steps": len(steps)}, 202)
        prefix = "/api/commands/"
        if path.startswith(prefix) and path.endswith("/approve"):
            cid = path[len(prefix):-len("/approve")].strip("/")
            with lock:
                c = commands.get(cid)
                if not c:
                    return self.send_json({"error": "not found"}, 404)
                if c["status"] != "pending":
                    return self.send_json({"error": "request is not pending"}, 409)
                if active_id is not None:
                    return self.send_json({"error": "another command is running"}, 409)
                c["status"] = "executing"
                c["approved_at"] = time.time()
                active_id = cid
            async_executor.submit(execute_approved, cid)
            return self.send_json({"id": cid, "status": "executing"}, 202)
        if path.startswith(prefix) and path.endswith("/reject"):
            cid = path[len(prefix):-len("/reject")].strip("/")
            with lock:
                c = commands.get(cid)
                if not c:
                    return self.send_json({"error": "not found"}, 404)
                if c["status"] != "pending":
                    return self.send_json({"error": "request is not pending"}, 409)
                c["status"] = "rejected"
                c["finished_at"] = time.time()
            return self.send_json({"id": cid, "status": "rejected"})
        return self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        if urlparse(self.path).path in ("/api/public/scripts", "/api/public/assets"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "300")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.send_json({"error": "CORS is disabled; use the local panel or an HTTP client."}, 405)


LOCAL_ADMIN_ORIGINS = {f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'}


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 256

    def server_bind(self):
        super().server_bind()
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (OSError, AttributeError):
            pass

    def get_request(self):
        sock, addr = super().get_request()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        return sock, addr


class LocalAdminHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'NerveForgeLocalAdmin/1.0'

    def log_message(self, fmt, *args):
        print(f'[local-admin] {self.command} {urlparse(self.path).path}', flush=True)

    def send_admin_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        origin = self.headers.get('Origin', '')
        if origin in LOCAL_ADMIN_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)

    def check_local_origin(self):
        if self.headers.get('Origin', '') not in LOCAL_ADMIN_ORIGINS:
            self.send_admin_json({'error': 'local panel origin required'}, 403)
            return False
        return True

    def authorized(self):
        supplied = self.headers.get('Authorization', '')
        value = supplied[7:] if supplied.startswith('Bearer ') else ''
        if not hmac.compare_digest(value.encode(), TOKEN.encode()):
            self.send_admin_json({'error': 'unauthorized'}, 401)
            return False
        return True

    def do_OPTIONS(self):
        origin = self.headers.get('Origin', '')
        if origin not in LOCAL_ADMIN_ORIGINS:
            return self.send_admin_json({'error': 'local panel origin required'}, 403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Max-Age', '300')
        self.send_header('Vary', 'Origin')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        # Token delivery lives only on this loopback-only listener.
        if path == '/bootstrap':
            if not self.check_local_origin():
                return
            return self.send_admin_json({'token': TOKEN})
        if not self.check_local_origin() or not self.authorized():
            return
        if path == '/yolo/state':
            with lock:
                return self.send_admin_json({'enabled': yolo_enabled, 'mode': 'read-only allowlist + sandboxed project workspace'})
        if path in ('/uzhas/state', '/full-access/state'):
            return self.send_admin_json(uzhas_state())
        return self.send_admin_json({'error': 'not found'}, 404)

    def do_POST(self):
        global yolo_enabled, uzhas_enabled, uzhas_last_activity, uzhas_duration_seconds, uzhas_armed_until
        if not self.check_local_origin() or not self.authorized():
            return
        path = urlparse(self.path).path
        if path not in ('/yolo/config', '/uzhas/config', '/full-access/config'):
            return self.send_admin_json({'error': 'not found'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 1024:
                return self.send_admin_json({'error': 'request too large'}, 400)
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(data, dict):
                return self.send_admin_json({'error': 'JSON body must be an object'}, 400)
            if path == '/yolo/config':
                enabled = data.get('enabled')
                if set(data) != {'enabled'} or not isinstance(enabled, bool):
                    return self.send_admin_json({'error': 'enabled boolean is required'}, 400)
                with lock:
                    yolo_enabled = enabled
                print(f"[yolo] Project YOLO {'enabled' if enabled else 'disabled'} from local panel", flush=True)
                return self.send_admin_json({'enabled': enabled, 'mode': 'read-only allowlist + sandboxed project workspace'})
            strict = path == '/full-access/config'
            armed = data.get('armed')
            if set(data) - {'armed', 'duration_seconds', 'confirmation'} or not isinstance(armed, bool):
                return self.send_admin_json({'error': 'armed boolean is required; duration_seconds and confirmation are optional'}, 400)
            try:
                state = set_full_access(armed, data.get('duration_seconds'),
                                        data.get('confirmation'), require_phrase=strict)
            except ValueError as e:
                return self.send_admin_json({'error': str(e)}, 400)
            return self.send_admin_json(state)
        except Exception as e:
            return self.send_admin_json({'error': str(e)}, 400)


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ReusableHTTPServer((HOST, PORT), Handler)
    local_admin = ReusableHTTPServer(("127.0.0.1", 8767), LocalAdminHandler)
    threading.Thread(target=local_admin.serve_forever, daemon=True).start()
    threading.Thread(target=config_monitor.loop, kwargs={"interval": 60}, daemon=True).start()
    threading.Thread(target=uzhas_idle_monitor, daemon=True).start()
    print(f"NerveForge слушает http://{HOST}:{PORT}", flush=True)
    if os.environ.get("NERVEFORGE_PREVIEW_BOOTSTRAP") == "1":
        print("ВНИМАНИЕ: NERVEFORGE_PREVIEW_BOOTSTRAP=1 — bootstrap токена открыт для любого origin (только для превью).", flush=True)
    print("ВНИМАНИЕ: /api/public/scripts и /api/public/assets открыты без токена; любой, кто знает URL, может загружать Unity-файлы.", flush=True)
    print("Ограничения: только корень workspace, новые .cs/.shader, до 64 KiB, максимум 5/мин и 30/час. Unity может выполнить код из C#-скрипта.", flush=True)
    print(f"Полный доступ: кнопка в панели + кодовая фраза {FULL_ACCESS_PHRASE!r}.", flush=True)
    print("Локальный bootstrap токена и YOLO toggle: loopback порт 8767 (не выставляется через туннель)", flush=True)
    print("HTTP-запросы пишутся в /api/requests и отображаются во вкладке «Журнал» панели.", flush=True)
    print("Локальная панель: http://127.0.0.1:%d/" % PORT, flush=True)
    print("Остановка: Ctrl+C. Не запускайте от root.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка моста.", flush=True)
    finally:
        disarm_uzhas("receiver shutdown")
        local_admin.shutdown()
        local_admin.server_close()
        server.server_close()

if __name__ == "__main__":
    main()

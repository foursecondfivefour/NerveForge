#!/usr/bin/env python3
"""Start the local bridge and an optional outbound tunnel.

Priority in ``auto`` mode: Tailscale Funnel -> Cloudflare Quick Tunnel -> local only.
Use ``--tunnel tailscale|cloudflared|none`` to force a specific behaviour.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import paths, platform_support as host

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("MINT_BRIDGE_PORT", "8766"))
# Reject Cloudflare's API endpoint: it appears in Quick Tunnel request errors,
# but is not a public hostname for a successfully-created tunnel.
URL_RE = re.compile(r"https://(?!api(?:\.|[/:]|$))[a-z0-9-]+\.trycloudflare\.com", re.I)
TS_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9.-]*\.ts\.net/?", re.I)
MODES = ("auto", "tailscale", "cloudflared", "none")
children: list[subprocess.Popen] = []
url_ready = threading.Event()
tunnel_url: str | None = None
_tunnel_mode: str = "none"
_tunnel_proc: subprocess.Popen | None = None
_keep_funnel = False


def stream_output(proc: subprocess.Popen, label: str, scan_url: bool = False, verbose: bool = False) -> None:
    """Print child output when verbose or important; extract Cloudflare URL cleanly."""
    global tunnel_url
    if not proc.stdout:
        return
    quick_tunnel_created = False
    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip("\n")
        if scan_url:
            if "your quick tunnel has been created" in line.lower():
                quick_tunnel_created = True
            if quick_tunnel_created:
                match = URL_RE.search(line)
                if match:
                    tunnel_url = match.group(0)
                    url_ready.set()
        if verbose and line:
            print(f"[{label}] {line}", flush=True)
    proc.stdout.close()


def _run(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def tailscale_binary() -> str | None:
    return shutil.which("tailscale") or shutil.which("tailscale.exe")


def tailscale_self() -> dict | None:
    ts = tailscale_binary()
    if not ts:
        return None
    try:
        result = _run([ts, "status", "--json"], timeout=10)
        if result.returncode:
            return None
        payload = json.loads(result.stdout or "{}")
        return payload.get("Self") or None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _iter_urls(value):
    if isinstance(value, str):
        for match in TS_URL_RE.finditer(value):
            yield match.group(0).rstrip("/")
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_urls(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_urls(item)


def tailscale_funnel_url() -> str | None:
    ts = tailscale_binary()
    if not ts:
        return None
    try:
        result = _run([ts, "funnel", "status", "--json"], timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            for url in _iter_urls(json.loads(result.stdout)):
                return url
        plain = _run([ts, "funnel", "status"], timeout=10)
        for url in _iter_urls(plain.stdout):
            return url
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    self_info = tailscale_self()
    dns = (self_info or {}).get("DNSName", "").strip().rstrip(".")
    return f"https://{dns}" if dns else None


def start_tailscale(port: int) -> tuple[subprocess.Popen | None, str]:
    """Enable a public Tailscale Funnel. Returns (None, URL) because --bg detaches."""
    global tunnel_url
    ts = tailscale_binary()
    if not ts:
        raise RuntimeError("tailscale CLI не найден в PATH")
    self_info = tailscale_self()
    if not self_info:
        raise RuntimeError("tailscale не авторизован: выполните `tailscale up`")
    result = _run([ts, "funnel", "--bg", "--https=443", str(port)], timeout=30)
    if result.returncode:
        message = (result.stderr or result.stdout or "").strip()[-1000:]
        raise RuntimeError(f"tailscale funnel не запустился: {message}")
    deadline = time.monotonic() + 25
    url = None
    while time.monotonic() < deadline:
        url = tailscale_funnel_url()
        if url:
            break
        time.sleep(0.7)
    if not url:
        raise RuntimeError("Funnel включён, но URL не удалось прочитать из `tailscale funnel status`")
    tunnel_url = url
    url_ready.set()
    return None, url


def stop_tailscale() -> None:
    if _keep_funnel or _tunnel_mode != "tailscale":
        return
    ts = tailscale_binary()
    if not ts:
        return
    try:
        _run([ts, "funnel", "reset"], timeout=15)
        print("[tailscale] Funnel сброшен при остановке приложения.", flush=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


def start_cloudflared(port: int) -> tuple[subprocess.Popen, str]:
    global tunnel_url
    cloudflared = shutil.which("cloudflared") or shutil.which("cloudflared.exe")
    if not cloudflared:
        raise RuntimeError("cloudflared не найден в PATH")
    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, **process_options(),
    )
    children.append(proc)
    threading.Thread(target=stream_output, args=(proc, "cloudflared", True, False), daemon=True).start()
    deadline = time.monotonic() + 60
    while not url_ready.wait(timeout=0.25):
        if proc.poll() is not None:
            raise RuntimeError("cloudflared завершился до создания Quick Tunnel")
        if time.monotonic() >= deadline:
            raise RuntimeError("Quick Tunnel не создан за 60 секунд")
    return proc, tunnel_url or ""



def switch_mode(mode: str, port: int = PORT) -> tuple[str, str | None]:
    """Switch active tunnel mode dynamically between 'cloudflared', 'tailscale', or 'none'."""
    global _tunnel_mode, _tunnel_proc, tunnel_url
    if mode not in ("cloudflared", "tailscale", "none"):
        raise ValueError("Режим должен быть 'cloudflared', 'tailscale' или 'none'")
    
    stop_children()
    stop_tailscale()
    _tunnel_proc = None
    tunnel_url = None
    url_ready.clear()
    
    public_url = None
    if mode == "tailscale":
        if not tailscale_binary():
            raise RuntimeError("Tailscale CLI не найден в PATH")
        _, public_url = start_tailscale(port)
        print(f"[tailscale] Funnel переключён и активен: {public_url}", flush=True)
    elif mode == "cloudflared":
        if not (shutil.which("cloudflared") or shutil.which("cloudflared.exe")):
            raise RuntimeError("cloudflared не найден в PATH")
        _tunnel_proc, public_url = start_cloudflared(port)
        print(f"[cloudflared] Quick Tunnel переключён и активен: {public_url}", flush=True)
    else:
        print("[туннель] переключён в локальный режим (без туннеля)", flush=True)
        
    write_tunnel_status(mode, public_url)
    return mode, public_url

def choose_mode(requested: str) -> str:
    if requested != "auto":
        return requested
    if shutil.which("cloudflared") or shutil.which("cloudflared.exe"):
        return "cloudflared"
    if tailscale_binary() and tailscale_self():
        return "tailscale"
    return "none"


def stop_children() -> None:
    for proc in reversed(children):
        if proc.poll() is None:
            if host.is_windows():
                taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
                if taskkill:
                    try:
                        subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=5,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        continue
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                try:
                    proc.terminate()
                except OSError:
                    pass
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    deadline = time.time() + 4
    for proc in reversed(children):
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    if host.is_windows():
                        proc.kill()
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass


def is_elevated() -> bool:
    if host.is_windows():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def confirm_elevated_run() -> bool:
    if not is_elevated():
        return True
    print(
        "ВНИМАНИЕ: NerveForge запущен с правами администратора. "
        "Команды полного доступа смогут менять системные файлы и настройки.",
        file=sys.stderr,
    )
    try:
        return input("Для подтверждения введите RUN AS ADMIN: ").strip() == "RUN AS ADMIN"
    except (EOFError, OSError):
        return False


def process_options() -> dict:
    if host.is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def write_tunnel_status(mode: str, url: str | None) -> None:
    """Small status file so the bridge API can report the active tunnel."""
    global _tunnel_mode
    _tunnel_mode = mode
    try:
        data_dir = paths.data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / "tunnel.json"
        target.write_text(json.dumps({"mode": mode, "url": url, "updated_at": time.time()},
                                     ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            target.chmod(0o600)
    except OSError:
        pass


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NerveForge receiver + tunnel")
    parser.add_argument("--tunnel", choices=MODES,
                        default=os.environ.get("NERVEFORGE_TUNNEL", "auto"),
                        help="auto (default), tailscale, cloudflared or none")
    parser.add_argument("--port", type=int, default=PORT, help="local receiver port")
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard")
    parser.add_argument("--keep-funnel", action="store_true",
                        help="do not reset Tailscale Funnel on exit")
    parser.add_argument("--verbose-http", action="store_true",
                        help="also mirror HTTP request log to the terminal")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    global _keep_funnel
    args = parse_args(argv)
    _keep_funnel = args.keep_funnel
    if not confirm_elevated_run():
        print("Запуск с правами администратора отменён.", file=sys.stderr)
        return 2

    port = args.port
    local_url = f"http://127.0.0.1:{port}"
    mode = choose_mode(args.tunnel)
    if mode == "tailscale" and not tailscale_binary():
        print("tailscale не найден. Используйте --tunnel cloudflared или none.", file=sys.stderr)
        return 2
    if mode == "cloudflared" and not (shutil.which("cloudflared") or shutil.which("cloudflared.exe")):
        print("cloudflared не найден. Используйте --tunnel tailscale или none.", file=sys.stderr)
        return 2

    package_env = os.environ.copy()
    package_env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT / "src"), package_env.get("PYTHONPATH", "")))
    )
    package_env["MINT_BRIDGE_PORT"] = str(port)
    package_env["NERVEFORGE_VERBOSE_HTTP"] = "1" if args.verbose_http else "0"
    bridge = subprocess.Popen(
        [sys.executable, "-u", "-m", "nerveforge.bridge"], cwd=ROOT, env=package_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        **process_options(),
    )
    children.append(bridge)
    threading.Thread(target=stream_output, args=(bridge, "приёмник", False, args.verbose_http), daemon=True).start()

    try:
        ready = False
        for _ in range(100):
            if bridge.poll() is not None:
                print("Приёмник завершился. Проверьте, не занят ли порт.", file=sys.stderr)
                return 1
            try:
                with urllib.request.urlopen(local_url + "/api/health", timeout=1) as response:
                    if response.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.2)
        if not ready:
            print("Приёмник не ответил за 20 секунд.", file=sys.stderr)
            return 1

        public_url = None
        tunnel_error = None
        if mode == "tailscale":
            try:
                _, public_url = start_tailscale(port)
                print(f"[tailscale] Funnel активен: {public_url}", flush=True)
            except Exception as exc:
                tunnel_error = str(exc)
                mode = "none"
        elif mode == "cloudflared":
            try:
                _, public_url = start_cloudflared(port)
                print(f"[cloudflared] Quick Tunnel активен: {public_url}", flush=True)
            except Exception as exc:
                tunnel_error = str(exc)
                mode = "none"
        write_tunnel_status(mode, public_url)

        token_file = paths.config_dir() / "token"
        token = token_file.read_text(encoding="utf-8").strip()
        print("\n" + "=" * 64)
        print("  NERVEFORGE ГОТОВ К РАБОТЕ")
        if public_url:
            print(f"  Публичный адрес:  {public_url}")
        else:
            print(f"  Публичный адрес:  нет (только локально)")
        print(f"  Локальная панель: {local_url}/")
        print(f"  Токен:            {token}")
        if tunnel_error:
            print(f"  Предупреждение:   {tunnel_error}")
        print("=" * 64 + "\n", flush=True)
        if not args.no_browser:
            webbrowser.open(local_url + "/")

        while bridge.poll() is None:
            if _tunnel_proc is not None and _tunnel_proc.poll() is not None:
                write_tunnel_status("none", None)
                print("[туннель] процесс завершился, остаётся локальный режим.", flush=True)
                break
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        print("\nОстанавливаю приёмник и туннель...", flush=True)
        return 0
    finally:
        stop_children()
        stop_tailscale()
        if _tunnel_mode != "none":
            write_tunnel_status("none", None)


if __name__ == "__main__":
    raise SystemExit(main())

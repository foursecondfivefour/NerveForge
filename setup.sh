#!/usr/bin/env bash
# One-pass installer for Linux Mint / Ubuntu: deps + launcher + first start.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Запустите обычным пользователем, без sudo: bash setup.sh" >&2
  exit 2
fi
if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
  echo "Этот простой установщик поддерживает Linux Mint и Ubuntu (APT)." >&2
  echo "На другой системе установите Python 3.10+ и (опционально) tailscale вручную." >&2
  exit 2
fi

# Install only what's missing. System packages are installed by APT after sudo confirmation.
packages=()
command -v python3 >/dev/null 2>&1 || packages+=(python3)
command -v curl >/dev/null 2>&1 || packages+=(curl)
command -v prlimit >/dev/null 2>&1 || packages+=(util-linux)

if ((${#packages[@]})); then
  echo "Установщик добавит недостающие системные пакеты: ${packages[*]}"
  echo "Сейчас появится обычный запрос пароля администратора от APT."
  sudo -v
  sudo apt-get update
  sudo apt-get install -y "${packages[@]}"
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Нужен Python 3.10 или новее. Обновите Linux Mint/Ubuntu и повторите bash setup.sh")
print(f"Python {sys.version.split()[0]} готов")
PY

# Cloudflare Quick Tunnel is primary; Tailscale is fallback.
if command -v cloudflared >/dev/null 2>&1; then
  echo "Cloudflared найден: будет использован Cloudflare Quick Tunnel (основной режим)."
elif command -v tailscale >/dev/null 2>&1; then
  echo "Cloudflared не найден; доступен резервный режим Tailscale."
else
  echo "Клиент туннеля не найден: приложение запустится в локальном режиме."
  echo "Для публичного доступа установите cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
fi

echo "Bubblewrap (песочница проектной зоны): $(command -v bwrap || echo 'не найден — запуск команд в YOLO будет закрыт, полный доступ работает без него')"

python3 "$ROOT/scripts/install_desktop_launcher.py"
echo
echo "Установка готова. Открываю NerveForge; окно терминала нужно оставить открытым."
echo "Закрыть приложение безопасно: Ctrl+C в этом окне."
echo
exec python3 "$ROOT/run.py"

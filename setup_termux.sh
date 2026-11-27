#!/usr/bin/env bash
# One-pass installer for Termux (Android): Python + Cloudflared / Tailscale + start.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "=================================================="
echo "  NerveForge 7.0.1 — Установка в Termux (Android)"
echo "=================================================="

pkg update -y || true
pkg install -y python git curl || true

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "Установка cloudflared..."
  pkg install -y cloudflared 2>/dev/null || echo "cloudflared не найден в стандартных репозиториях; можно установить через tur-repo"
fi

termux-setup-storage 2>/dev/null || true

echo
echo "NerveForge готов к запуску в Termux!"
echo "Запускаю сервер моста..."
echo
exec python3 "$ROOT/run.py"

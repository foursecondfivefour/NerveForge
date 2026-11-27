# Changelog

## 7.0.1

- Добавлена полная нативная поддержка **Android (Termux aarch64)** и установщик `setup_termux.sh`.
- Добавлены эндпоинты самодокументирования для ИИ-агентов: `/api/docs`, `/llms.txt` и `/api/agent-prompt`.
- Обновлена документация (`README.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`) с подробным руководством по использованию приёмника и интеграции с Arena.ai.
- Набор unit-тестов расширен до 65 тестов (включая кроссплатформенные тесты для Android/Termux).
- HTTP-запросы больше не печатаются в терминал: они пишутся в кольцевой буфер и отображаются во вкладке «Журнал» панели.
- Закрыт обход `/api/local-bootstrap` через подставные `Host`/`Origin` заголовки. Токен выдаёт только loopback-слушатель `127.0.0.1:8767/bootstrap`.
- Поддержка динамических лимитов данных до 40 МБ команд / 50 МБ вывода / 64 МБ тела запросов.

## 7.0.0

- Tailscale Funnel стал основным туннелем; Cloudflare Quick Tunnel — резервный и полностью опциональный. Новый режим `--tunnel auto|tailscale|cloudflared|none` и `--keep-funnel`.
- Веб-панель полностью переработана: тёмная тема по умолчанию, вкладки, компактная статусная строка, терминал с быстрыми командами, редактор файлов, журнал событий.
- Добавлен «полный доступ»: кнопка в панели + кодовая фраза `yesiknow`. После активации произвольные команды выполняются без очереди подтверждений, с произвольным `cwd` и выбором оболочки.
- Файловые операции рабочей папки разблокируются как проектной песочницей YOLO, так и полным доступом.
- Добавлены API `/api/quick`, `/api/tunnel`, `/api/events`, `/api/full-access/config`, `/api/full-access/run`, `/api/full-access/jobs/<id>`, `/api/full-access/stop`.
- Обновлены установщики: Tailscale определяется автоматически, для Windows добавлен флаг `-InstallTailscale`.
- Исправлен рассинхрон версии в `__init__.py`.

## 6.3.2

- Extended anonymous, create-only Unity upload to ShaderLab `.shader` assets via `/api/public/assets`; `.cs` uploads remain on `/api/public/scripts`.
- Kept bearer authentication on every API other than the explicitly public Unity asset upload routes.

## 6.3.1

- Added an always-on anonymous `POST /api/public/scripts` endpoint for creating new `.cs` files in the configured workspace root without a bearer token.
- Restricted anonymous uploads to new files only (no overwrite), 64 KiB per file, 5 requests per minute, and 30 per hour; all other API routes remain bearer-protected.
- Added prominent UI and startup warnings that public Unity script uploads can execute code when imported by Unity.
- Added Windows and cross-platform tests for anonymous upload scope, rate limits, path validation, and no-overwrite behavior.

## 6.3.0

- Added native Windows receiver startup with a PowerShell setup script, user-scoped data paths, and optional Unity `Assets` workspace selection.
- Added automatic OS/shell capability reporting; approved and locally armed commands can use PowerShell/CMD on Windows or Bash on Linux.
- Added configurable maximum arm duration, retained idle expiration, and quick-off cancellation of active process trees.
- Kept Windows project command execution fail-closed until a verified sandbox exists; Windows service mutation and elevation remain disabled.
- Added cross-platform API, timer, shell selection, path, and cancellation tests plus Windows CI coverage.

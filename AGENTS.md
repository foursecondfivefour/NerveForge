# Инструкция для ИИ-агентов (Arena.ai ↔ PC / Android Bridge)

Этот документ предназначен для ИИ-агентов (Arena.ai Agent Mode, Claude, ChatGPT, Gemini, Grok, Qwen и др.), подключающихся к удалённому устройству пользователя через шлюз-приёмник **NerveForge**.

---

## 1. Как устроен приёмник NerveForge

Приёмник запускается на машине пользователя (Linux, Windows или Android/Termux) и поднимает:
1. Локальный высокопроизводительный HTTP REST API на `127.0.0.1:8766`.
2. Защищённый исходящий туннель Cloudflare (`https://*.trycloudflare.com`) или Tailscale Funnel (`https://*.ts.net`).
3. Службу авторизации с Bearer-токеном.

Для агента нет необходимости скачивать репозиторий или знать внутреннее устройство NerveForge. Всё взаимодействие происходит через стандартные HTTP/JSON запросы.

---

## 2. Пошаговый протокол работы агента

### Шаг 1: Обнаружение платформы и статуса (`GET /api/quick`)
Отправьте запрос для определения целевой ОС, доступных оболочек и статуса доступа:

```bash
curl -H "Authorization: Bearer <TOKEN>" https://<TUNNEL_URL>/api/quick
```

**Пример ответа:**
```json
{
  "platform": {
    "os": "linux",             // "linux", "windows" или "android"
    "label": "Linux Mint 22.3",
    "architecture": "x86_64",
    "shells": [
      {"id": "bash", "label": "Bash"}
    ],
    "default_shell": "bash",
    "workspace_root": "/home/user/NerveForgeProjects"
  },
  "full_access": {
    "armed": true,             // Должно быть true для запуска терминала
    "seconds_remaining": 3590
  },
  "limits": {
    "max_command_mb": 40.0,
    "max_output_mb": 50.0
  }
}
```

---

### Шаг 2: Выполнение команд терминала (Full Access Shell)

Когда `full_access.armed == true`, агент может исполнять произвольные системные команды:

#### 1. Отправка команды на исполнение:
`POST /api/full-access/run`
```json
{
  "command": "git status && df -h",
  "timeout_seconds": 60,
  "shell": "bash",             // "bash", "zsh", "sh" для Linux/Android; "powershell", "cmd" для Windows
  "cwd": "/path/to/project"    // Опционально: рабочая папка
}
```

**Ответ сервера (`202 Accepted`):**
```json
{
  "ok": true,
  "id": "c7a8b9e0-1234-5678-9abc-def012345678",
  "status": "queued"
}
```

#### 2. Опрос результата:
`GET /api/full-access/jobs/<job_id>`

Опрашивайте эндпоинт с интервалом 0.3–0.5 секунды, пока `status` не перейдёт в терминальное состояние (`done`, `error`, `timeout`, `cancelled`):
```json
{
  "id": "c7a8b9e0-1234-5678-9abc-def012345678",
  "status": "done",
  "return_code": 0,
  "output": "On branch main
nothing to commit, working tree clean
"
}
```

---

### Шаг 3: Особенности платформ (Linux vs Windows vs Android)

* **🐧 Linux:**
  * Оболочка: `bash`.
  * Доступен стандартный стек GNU/Linux, `apt`, `systemctl`, `journalctl`.
* **🪟 Windows:**
  * Оболочки: `powershell` (рекомендуется) или `cmd`.
  * Поддержка PowerShell-командлетов (`Get-ChildItem`, `Get-Process`, `dotnet build`).
* **📱 Android (Termux):**
  * Оболочки: `bash`, `zsh`, `sh`.
  * Пакетный менеджер: `pkg` (`pkg install <pkg>`).
  * Память устройства: доступна по пути `/sdcard` или `~/storage/shared/`.
  * Аппаратные API: доступны через утилиты `termux-battery-status`, `termux-vibrate`, `termux-toast` (при установленном Termux:API).
  * Root/Elevation: поддерживаются утилиты `tsu` и `su` при наличии рут-прав.

---

### Шаг 4: Работа с файлами проекта (Workspace)

* **Список файлов:** `POST /api/workspace/list` с `{"path": "."}`
* **Чтение файла:** `POST /api/workspace/read` с `{"path": "Assets/Scripts/GameManager.cs"}`
* **Запись файла:** `POST /api/workspace/write` с `{"path": "Assets/Scripts/GameManager.cs", "content": "..."}`  
  *(Предыдущая версия файла автоматически сохраняется в бэкап).*

---

### Шаг 5: Быстрая генерация ассетов для Unity (Без токена)

Если требуется только создать новый C#-скрипт или шейдер в открытом проекте Unity:
* `POST /api/public/scripts` — создание `.cs` скрипта: `{"filename": "PlayerController.cs", "content": "..."}`
* `POST /api/public/assets` — создание `.shader` или `.cs`: `{"filename": "ToonShader.shader", "content": "..."}`

---

## 3. Готовый Python-клиент для ИИ-агента (Copy & Run)

Агент в Arena.ai может скопировать и выполнить этот универсальный блок:

```python
import urllib.request, json, time, ssl

TOKEN = "ВАШ_ТОКЕН"
BASE = "https://ВАШ_ТУННЕЛЬ.trycloudflare.com"
CTX = ssl.create_default_context()

def run_cmd(command: str, timeout: int = 60, cwd: str = None) -> tuple[int, str]:
    """Асинхронный запуск команды на целевом устройстве с опросом результата."""
    payload = {"command": command, "timeout_seconds": timeout}
    if cwd:
        payload["cwd"] = cwd
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/full-access/run", data=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, context=CTX, timeout=15) as resp:
        job_id = json.loads(resp.read().decode("utf-8"))["id"]

    start = time.time()
    while time.time() - start < timeout + 5:
        poll_req = urllib.request.Request(
            f"{BASE}/api/full-access/jobs/{job_id}",
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        with urllib.request.urlopen(poll_req, context=CTX, timeout=15) as resp:
            job = json.loads(resp.read().decode("utf-8"))
        if job.get("status") in ("done", "error", "failed", "timeout", "cancelled"):
            return job.get("return_code", -1), job.get("output", "")
        time.sleep(0.4)
    return -1, "Timeout polling command execution"

# Пример вызова:
exit_code, output = run_cmd("uname -a || systeminfo")
print(f"Exit code: {exit_code}\nOutput:\n{output}")
```

---

## 4. Встроенные эндпоинты самодокументирования

* `GET /api/docs` или `GET /llms.txt` — открытый текстовый манифест и руководство для агентов в Markdown.
* `GET /api/agent-prompt` — структурированный JSON с системным промптом и картой эндпоинтов.

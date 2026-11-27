# Инструкция для ИИ-агентов (Arena.ai ↔ PC Bridge)

Этот документ предназначен для ИИ-агентов (Arena.ai Agent Mode, Claude, ChatGPT, Gemini и др.), подключающихся к удалённому ПК пользователя через шлюз **NerveForge**.

---

## 1. Как агенту обнаружить хост и начать работу

Пользователь предоставляет вам **URL туннеля** (например, `https://sample-subdomain.trycloudflare.com`) и **Bearer-токен** авторизации.

### Шаг 1: Проверка статуса хоста
Выполните `GET /api/quick`:
```bash
curl -H "Authorization: Bearer <TOKEN>" https://<TUNNEL_URL>/api/quick
```

**Ответ содержит:**
* `platform.os` — целевая ОС (`linux` или `windows`).
* `platform.default_shell` — основная оболочка (`bash` или `powershell`).
* `platform.workspace_root` — рабочая папка (например, `Assets` Unity-проекта).
* `full_access.armed` — статус полного доступа (`true` / `false`).
* `limits` — максимальные размеры команд (40 МБ) и вывода (50 МБ).

---

## 2. Выполнение системных команд (Full Access Shell)

Когда `full_access.armed == true`, вы можете запускать произвольные команды в терминале:

### Отправка команды
`POST /api/full-access/run`
Заголовки:
* `Authorization: Bearer <TOKEN>`
* `Content-Type: application/json`

Тело запроса:
```json
{
  "command": "uname -a && uptime",
  "timeout_seconds": 30,
  "shell": "bash",
  "cwd": null
}
```

Сервер возвращает `202 Accepted`:
```json
{
  "ok": true,
  "id": "e4f8b2c1-3a5d-4f1e-9b2c-8d1a3e5f7a9b",
  "status": "queued"
}
```

### Опрос результата выполнения
`GET /api/full-access/jobs/<id>`

Повторяйте запрос с интервалом 0.3–0.5 сек, пока `status` не станет `done` или `error`:
```json
{
  "id": "e4f8b2c1-3a5d-4f1e-9b2c-8d1a3e5f7a9b",
  "status": "done",
  "return_code": 0,
  "output": "Linux 7.0.0-42-generic ...
"
}
```

---

## 3. Работа с файлами проекта (Workspace)

* **Список файлов:** `POST /api/workspace/list` с `{"path": "."}`
* **Чтение файла:** `POST /api/workspace/read` с `{"path": "Assets/Player.cs"}`
* **Запись файла:** `POST /api/workspace/write` с `{"path": "Assets/Player.cs", "content": "..."}`  
  *(Предыдущая версия файла автоматически сохраняется в бэкап).*

---

## 4. Генерация ассетов для Unity (Без токена)

Если требуется только создать новый C#-скрипт или шейдер в открытом проекте Unity:
* `POST /api/public/scripts` — создание `.cs` скрипта: `{"filename": "FpsController.cs", "content": "..."}`
* `POST /api/public/assets` — создание `.shader` или `.cs`: `{"filename": "CustomGlow.shader", "content": "..."}`

---

## 5. Готовый Python-клиент для агента (Copy & Run)

Агент может сразу скопировать и запустить этот код в своём окружении:

```python
import urllib.request, json, time, ssl

TOKEN = "ВАШ_ТОКЕН"
BASE = "https://ВАШ_ТУННЕЛЬ.trycloudflare.com"
CTX = ssl.create_default_context()

def run_cmd(command: str, timeout: int = 30) -> tuple[int, str]:
    """Асинхронный запуск команды на ПК через NerveForge с опросом результата."""
    data = json.dumps({"command": command, "timeout_seconds": timeout}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/full-access/run", data=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, context=CTX, timeout=10) as resp:
        job_id = json.loads(resp.read().decode("utf-8"))["id"]
    
    start = time.time()
    while time.time() - start < timeout + 5:
        poll_req = urllib.request.Request(
            f"{BASE}/api/full-access/jobs/{job_id}",
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        with urllib.request.urlopen(poll_req, context=CTX, timeout=10) as resp:
            job = json.loads(resp.read().decode("utf-8"))
        if job.get("status") in ("done", "error", "failed", "timeout", "cancelled"):
            return job.get("return_code", -1), job.get("output", "")
        time.sleep(0.4)
    return -1, "Timeout polling command execution"

# Пример использования:
code, output = run_cmd("git status")
print(f"Exit code: {code}
Output:
{output}")
```

---

## 6. Встроенные эндпоинты самодокументирования

* `GET /api/docs` или `GET /llms.txt` — возвращает полное текстовое руководство в Markdown.
* `GET /api/agent-prompt` — возвращает структурированный JSON с описанием всех маршрутов и базовым промптом.

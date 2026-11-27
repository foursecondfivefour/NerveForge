# Public Unity asset upload

NerveForge 6.3.2 exposes a deliberately unauthenticated upload path so an online agent can create Unity assets through a public tunnel without receiving the bridge bearer token.

> **Risk:** anyone who knows the public tunnel URL can call these routes. A `.cs` file is executable code in the Unity project: Unity compiles it, and some Editor scripts can run automatically. Rate limits and file-path restrictions are not authentication. Use only on a disposable, isolated test machine; do not run Unity as Administrator.

## Routes

### `POST /api/public/scripts`

Creates a new `.cs` file in the workspace root.

### `POST /api/public/assets`

Creates a new `.cs` or `.shader` file in the workspace root.

Both accept JSON with exactly these fields:

```json
{
  "filename": "AgentExample.cs",
  "content": "public class AgentExample {}"
}
```

Send `Content-Type: application/json`. No `Authorization` header is needed. Browser CORS preflight is enabled on these two routes only.

Example PowerShell request:

```powershell
$body = @{
  filename = 'AgentExample.cs'
  content = 'public class AgentExample {}'
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri 'https://<public-tunnel-host>/api/public/scripts' `
  -ContentType 'application/json' `
  -Body $body
```

The route writes to the configured workspace root, not an arbitrary path. To have Unity import files, configure that root to the intended project's `Assets` directory.

## Enforced limits

- Only a plain filename is accepted; subdirectories, absolute paths, traversal, ADS, and invalid Windows names are rejected.
- Allowed extensions are exactly `.cs` and `.shader` (the scripts route accepts `.cs` only).
- UTF-8 text only; NUL bytes are rejected.
- Maximum content size: 64 KiB per file.
- Create-only: an existing file is never overwritten. A name collision returns HTTP 409; choose a new filename.
- Shared global request limits: 5 uploads/minute and 30/hour per running receiver. The server does not trust client-supplied IP forwarding headers.
- Public upload cannot list, read, modify, or delete files, run commands, arm U.Ж.А.С., or call other APIs.

Successful creation returns HTTP 201 with the filename and byte count. Malformed/unsupported input returns 400, an existing filename returns 409, and rate limiting returns 429.

## Operational guidance

- Keep U.Ж.А.С. off. It is unrelated to this route and grants far broader shell capability when armed.
- Do not run NerveForge or Unity elevated while receiving anonymous code.
- Keep the public tunnel private to the intended test session. Stop the receiver/tunnel when upload is no longer needed.
- Review new scripts in Unity before adding them to scenes or entering Play mode. Avoid importing on machines with sensitive data or credentials.
- If anonymous uploads are not required, remove/disable the public route in a separately reviewed code change; do not assume rotating the bearer token disables these routes.

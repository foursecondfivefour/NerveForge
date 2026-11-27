# Agent instructions for NerveForge

## Start here

Before changing code, read `README.md`, `SECURITY.md`, and the relevant page under `docs/`. Treat the tests and current implementation as authoritative if prose appears stale. Keep changes focused; do not restore archived or removed versions.

## Product and platform boundaries

- Preserve the NerveForge UI design and keep frontend assets self-hosted in `static/` unless a task explicitly requests otherwise.
- The receiver binds to loopback. `run.py` coordinates the receiver and the configured tunnel process.
- Windows supports file access in the selected workspace and shell selection for the separately armed U.Ж.А.С. feature. **Project workspace command execution must remain fail-closed on Windows** until a verified sandbox exists.
- U.Ж.А.С. is distinct from Project YOLO and from public Unity asset upload. It must stay locally armed, time-limited, bearer-authenticated, and quick-off capable.
- If NerveForge is launched elevated on Windows, child processes inherit administrator privileges. Do not add automatic elevation or services without an explicit requirement.
- Do not add server-side Unity execution or editor automation. Unity integration here means creating files only.

## v7.0.0 additions — read before changing access control

- Tailscale Funnel is the primary tunnel; Cloudflare is optional fallback. Keep `--tunnel auto|tailscale|cloudflared|none` and the local-only mode working.
- Full access is deliberately armable through the bearer API with the exact phrase `yesiknow`. The phrase is a UI guard, not authentication. Preserve timers, quick-off and audit metadata.
- File operations unlock on either Project YOLO **or** full access. Windows project command execution stays fail-closed; use the full-access shell there.
- Keep the dark theme as default in the panel and all frontend assets local.

## Public Unity upload boundary — read before touching upload code

NerveForge 6.3.2 intentionally has unauthenticated, public, write-only routes:

- `POST /api/public/scripts`: create a new `.cs` file.
- `POST /api/public/assets`: create a new `.cs` or `.shader` file.

Both write only a simple filename in the configured workspace root, never overwrite, cap files at 64 KiB, and share limits of 5 requests/minute and 30/hour. They do not grant read, delete, overwrite, or command access. CORS is public for these two routes only. These restrictions do **not** make uploaded C# safe: Unity compiles it and editor scripts can execute code.

- Keep every other API bearer-protected (except intentionally loopback-only health/bootstrap behavior).
- Do not broaden anonymous upload to arbitrary extensions, paths, reads, deletes, overwrites, or command execution without a separate explicit design decision and tests.
- Keep startup and panel warnings in sync with behavior and update `README.md`, `SECURITY.md`, and tests whenever this boundary changes.
- Never place real tokens, tunnel URLs paired with credentials, or personal paths in docs, logs, fixtures, or examples.

## Engineering workflow

- Keep the application standard-library-only unless dependency changes are explicitly approved.
- Validate paths with `workspace_manager.safe_path`; reject traversal, symlinks, and Windows reparse points.
- Keep writes atomic and bounded. Public create-only writes must not overwrite existing files.
- Avoid platform-specific assumptions in shared code. Windows behavior must have tests and a Windows CI step.
- Do not claim Windows or Unity runtime validation from Linux unit tests.
- Bump `VERSION`, update `CHANGELOG.md`, and keep installation/security docs consistent for user-facing behavior changes.

## Checks

From the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run.py
node --check static/app.js
bash -n setup.sh
```

The GitHub Actions workflow additionally runs on `windows-latest` and parses `setup_windows.ps1`. For Windows-sensitive changes, report whether the actual Windows CI result is known; mocks on Linux are not a substitute.

# NerveForge architecture

> Обновление 7.0.0: основным outbound-туннелем стал Tailscale Funnel, Cloudflare Quick Tunnel — резервный. Полный доступ теперь включается через API с кодовой фразой и позволяет произвольный shell удалённо. Ниже описание сохранено как базовая модель; актуальные детали — в `README.md` и `SECURITY.md`.

NerveForge is a local receiver plus a browser panel and an optional outbound tunnel. It is not a remote desktop service and does not provide a general-purpose Windows workspace shell.

## Runtime processes and listeners

- `run.py` starts `nerveforge.bridge` and the selected tunnel client as child processes.
- The bridge listens on loopback at `127.0.0.1:8766` by default. `MINT_BRIDGE_PORT` can override the port.
- The local control listener uses loopback port `8767`; it is not meant to be forwarded through a tunnel. The browser panel calls it only from the local origin.
- `run.py` waits for `/api/health`, starts `cloudflared tunnel --url http://127.0.0.1:<port>`, and accepts a Quick Tunnel URL only after the provider confirms successful creation. If the tunnel exits or times out, the local receiver remains available in local-only mode.
- `Ctrl+C` in the launcher stops its child process trees. A tunnel hostname is a route to the running receiver, not an authentication credential.

## Main modules

| Path | Responsibility |
|---|---|
| `src/nerveforge/bridge.py` | HTTP routes, bearer authorization, command approval, local arming, audit events, rate limits |
| `src/nerveforge/platform_support.py` | OS and shell detection; capability reporting |
| `src/nerveforge/paths.py` | Per-user config, data, and workspace locations |
| `src/nerveforge/workspace_manager.py` | Workspace path validation, bounded file access, create-only public Unity asset writes, Linux sandbox runner |
| `src/nerveforge/uzhas_manager.py` | Shell dispatch, timeouts, process-tree cancellation |
| `src/nerveforge/tunnel.py` | Receiver/tunnel lifecycle, local browser launch, tunnel URL detection |
| `static/` | Local-only HTML, CSS, and JavaScript dashboard |
| `setup_windows.ps1` | Non-service Windows setup with explicit dependency/elevation prompts |

## Platform capabilities

### Windows

- Shells reported by the receiver: PowerShell and CMD; PowerShell is the default when available.
- User config is under `%APPDATA%\NerveForge`; logs/data are under `%LOCALAPPDATA%\NerveForge\data`.
- `NERVEFORGE_WORKSPACE` selects the file workspace. The installer persists it at user scope; the Unity project `Assets` directory can be selected.
- Workspace file operations are confined to the configured root and reject symlinks/reparse points.
- Project workspace command execution is disabled. Do not remove this fail-closed behavior until a Windows sandbox has been implemented and verified.
- U.Ж.А.С. is separate: it can run the selected shell only after local arming. If the receiver was started elevated, shell commands inherit that elevation.

### Linux Mint / Ubuntu

- Bash is detected automatically.
- Config is under `~/.config/nerveforge`; application data is under `~/.local/share/nerveforge`.
- Project commands require the configured Bubblewrap and `prlimit` isolation path; there is no unsandboxed fallback.
- Linux-only systemd diagnostics/actions and screenshot support are reported by capability detection.

## Security planes

1. **Loopback control plane** — local-only endpoints control Project YOLO and U.Ж.А.С. Remote callers cannot arm U.Ж.А.С.
2. **Bearer-protected API** — normal state, workspace management, approval requests, and remote shell requests require `Authorization: Bearer <token>`, with local bootstrap exceptions constrained to loopback origin/host.
3. **Anonymous Unity upload** — the two explicitly public routes documented in [PUBLIC_UNITY_UPLOAD.md](PUBLIC_UNITY_UPLOAD.md) can create only new Unity files in the workspace root. They are not a sandbox and do not inherit token protection.

Never conflate Project YOLO, U.Ж.А.С., and anonymous Unity upload. Their capabilities and authorization boundaries differ.

## State and persistence

- The bearer token is stored in the per-user config directory as `token` and is created if absent/invalid.
- U.Ж.А.С. and Project YOLO reset to off when the receiver process restarts.
- Anonymous Unity upload is available whenever the 6.3.2 receiver is running; it does not need a token or a local toggle.
- File writes are UTF-8, bounded, and constrained to the workspace. Existing public-upload files are never overwritten.
- Audit logs record metadata rather than full shell command content/output or uploaded content.

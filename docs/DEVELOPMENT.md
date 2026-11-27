# Development and verification

## Requirements

- Python 3.10 or newer; the application code uses the Python standard library.
- Node.js is needed only for the JavaScript syntax check.
- On Windows, PowerShell parses `setup_windows.ps1`; CI performs this check on a Windows runner.
- Linux sandbox tests use mocks in CI. To validate real Linux command isolation, test on a supported Mint/Ubuntu host with Bubblewrap and `prlimit` installed.

## Local checks

Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run.py
node --check static/app.js
bash -n setup.sh
```

On Windows, run the Python and Node checks from PowerShell. The workflow at `.github/workflows/tests.yml` runs unit tests and compile checks on both `ubuntu-latest` and `windows-latest`, and parses the PowerShell installer on Windows.

## Test organization

- `test_bridge_platform_api.py`: authorization, platform reporting, disarmed shell rejection, loopback-only arming.
- `test_v7_features.py`: full-access phrase, remote arming, arbitrary command dispatch, cwd, workspace unlock, tunnel state, Tailscale URL parsing.
- `test_public_script_endpoint.py`: anonymous upload scope, no bearer requirement, CORS preflight, rate limiting, and create-only behavior.
- `test_workspace_manager.py`: traversal, symlink/reparse protections, bounded files, public asset creation, and no unsandboxed Windows command fallback.
- `test_windows_support.py`: shell dispatch, timer semantics, process cancellation, and Windows path handling.
- `test_tunnel_url.py`: do not mistake the Cloudflare API error URL for a provisioned Quick Tunnel.
- Tailscale: manually verify `tailscale funnel --bg --https=443 <port>` and `tailscale funnel status --json` on a real tailnet; CI has no tailnet.
- `test_elevated_mode.py`: explicit confirmation before elevated launch.

## Change checklist

1. Inspect existing behavior and tests before editing; avoid unrelated cleanup or restoration of removed versions.
2. Preserve separate authorization boundaries: local-only toggles, bearer-protected APIs, and explicitly public Unity upload routes.
3. Add a focused regression test for each behavior change, including negative/abuse cases for file paths and limits.
4. Update `README.md`, `SECURITY.md`, `CHANGELOG.md`, `VERSION`, and relevant `docs/` when observable behavior changes.
5. Run the checks above. Do not describe Linux mocks as a real Windows or Unity validation.
6. For Unity assets, validate against the declared render pipeline and Unity version when available; report when editor compilation could not be run.

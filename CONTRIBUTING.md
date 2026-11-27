# Contributing

- Keep credentials, tunnel URLs, user data, logs, and generated archives out of commits.
- Do not broaden the default allowlists or make UZHAS armed by default.
- Preserve the local-only arm endpoint and bearer-token checks for UZHAS. Validate both idle and maximum-duration expiry, and ensure quick-off terminates the command process tree.
- Keep system-level changes approval-gated; do not add a generic root shell.
- Run before submitting:

  ```bash
  python3 -m compileall -q src tests run.py
  python3 -m unittest discover -s tests -v
  ```

- Run the unit suite on Linux and Windows. The GitHub Actions matrix does this automatically; local Windows validation also requires a real PowerShell/CMD run because Linux mocks cannot verify Windows process behavior.
- Describe security-relevant behavior and any untested Mint/Bubblewrap/Windows process-tree assumptions in the pull request.

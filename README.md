# hard-restart-claude-code

Hard-restart the Claude Desktop app on Windows: find every running `claude.exe` from the Microsoft Store install, kill it, relaunch the app.

Useful when Claude Code's hook config (or any other config read at session start) needs to be re-loaded and "Quit from tray" alone isn't enough.

## Install

```powershell
# Optional: route uv tool installs off the system drive
# $env:UV_TOOL_DIR     = "<your-tools-dir>"
# $env:UV_TOOL_BIN_DIR = "<your-tools-bin-dir>"
uv tool install --editable . --force
```

After install, the binary lives at `<UV_TOOL_BIN_DIR>\hard-restart-claude-code.exe` (and `hrcc.exe`). With no overrides, `uv` places it under your user data dir — run `uv tool dir --bin` to see where.

## Usage

```powershell
hrcc                  # kill all matching processes, relaunch
hrcc --dry-run        # list what would be killed, change nothing
hrcc --no-launch      # kill only, do not relaunch
hrcc --exe <path>     # override the Claude Desktop exe path
```

The exe is discovered at run time via `Get-AppxPackage -Name 'Claude'`, so it follows Store updates. Override with `--exe` if discovery fails.

## How it matches processes

Lists `claude.exe` processes via `Get-CimInstance Win32_Process`, then keeps only those whose `ExecutablePath` starts with `%ProgramFiles%\WindowsApps\Claude_`. This avoids killing unrelated `claude` binaries (e.g. the Claude Code CLI or its node host) - the path anchor is deliberately narrow, and tests assert both that it never widens and that a lookalike outside `%ProgramFiles%` is rejected.

The anchor is a **prefix** test, not a substring test, and the decision is made in Python rather than in the PowerShell query. `%ProgramFiles%\WindowsApps` is admin-only, which is what makes a matched process trustworthy enough to kill and to read a `--user-data-dir` back from.

Processes are killed individually (`taskkill /F /PID`), not as a tree. `hrcc` is usually run from a shell inside Claude Desktop, so a tree kill would terminate `hrcc` itself before it could relaunch anything.

## Account profiles

Claude Desktop selects an account with `--user-data-dir`. `hrcc` reads that flag from the processes it is about to kill and reproduces it on relaunch, so restarting does not move you to a different account. Run `hrcc --dry-run` to see which profile it detected.

If two different profiles are running at once, `hrcc` relaunches only the lowest-pid one and warns on stderr.

## Tests

```powershell
uv run pytest
```

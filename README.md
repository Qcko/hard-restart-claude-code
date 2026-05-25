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

Default exe: `D:\WindowsApps\Claude_1.6608.2.0_x64__pzs8sxrjxfjjc\app\claude.exe`. Override with `--exe` if your version directory differs.

## How it matches processes

Lists `claude` processes via `Get-Process` and keeps only those whose `Path` contains `WindowsApps\Claude_`. This avoids killing unrelated `claude` binaries (e.g. the Claude Code CLI or its node host).

## Tests

```powershell
uv run pytest
```

# hard-restart-claude-code

Hard-restart the Claude Desktop app on Windows: find every running `claude.exe` from the Microsoft Store install, kill it, relaunch the app.

Useful when Claude Code's hook config (or any other config read at session start) needs to be re-loaded and "Quit from tray" alone isn't enough.

## Install

```powershell
$env:UV_TOOL_DIR = "E:\uv\tools"
$env:UV_TOOL_BIN_DIR = "E:\uv\tools\bin"
uv tool install --editable E:\dev\hard-restart-claude-code --force
```

After install, the binary lives at `E:\uv\tools\bin\hard-restart-claude-code.exe` (and `hrcc.exe`).

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
cd E:\dev\hard-restart-claude-code
uv run pytest
```

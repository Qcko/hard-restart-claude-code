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
hrcc                        # kill all matching processes, relaunch
hrcc --dry-run              # list what would be killed, change nothing
hrcc --no-launch            # kill only, do not relaunch
hrcc --exe <path>           # override the Claude Desktop exe path
hrcc --profile-dir <dir>    # relaunch against a specific --user-data-dir
hrcc --no-profile           # relaunch bare, discarding the profile in use
hrcc --json                 # emit the result as JSON instead of prose
hrcc --verify               # wait for the package to be serviceable first
```

The exe is discovered at run time via `Get-AppxPackage -Name 'Claude'`, so it follows Store updates. Override with `--exe` if discovery fails.

### Choosing the profile

By default `hrcc` relaunches against whatever `--user-data-dir` the running Desktop was using, so a restart keeps you on the same account.

`--profile-dir` overrides that and always wins over what was observed. Every matching Desktop process is still killed, whatever profile it was on. The value is validated before use: it must be an absolute local path, must not be a UNC or device path, must not contain NUL or newline characters, and must not name an existing file. A value that fails validation is an error — `hrcc` never quietly falls back to the profile that was running, because that would relaunch Desktop on one account while the caller believes it asked for another.

**Omitting `--profile-dir` is not the same as `--no-profile`.** Omitting it preserves the current profile; `--no-profile` discards it deliberately. A caller that means "launch bare" must say so, so that a dropped flag can never look like a successful switch.

### Waiting for the package

An MSIX update leaves the Claude package unusable for the second or two Windows needs to service it, and a launch landing in that window fails outright. `--verify` waits for the package to report `Ok` before launching, and launches the executable that package reports rather than one resolved earlier.

It is **off by default**, because a bare `hrcc` should stay a one-second command. `--profile-dir` implies it. `--package-budget` caps the wait.

The gate **fails open**: if the package state cannot be read, it stops waiting immediately, and if the budget runs out it launches anyway. A check that cannot verify is never the reason a working restart does not happen. The outcome is reported as `package_status`, one of `Ok`, `unreadable`, `budget-exhausted`, or whatever status Windows reported while waiting.

`--simulate-package-status STATUS` makes the reader report `STATUS` instead of asking Windows, so the wait can be exercised deliberately rather than only during a real update:

```powershell
hrcc --dry-run --simulate-package-status Disabled --package-budget 4
```

`--dry-run` runs the gate too. It only reads, so a dry run stays a dry run, and it is the only way to watch the wait without restarting Desktop.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 2 | Could not resolve a single Claude Desktop install, or `--exe` does not exist. Also argparse usage errors. |
| 3 | `--profile-dir` failed validation. |
| 4 | Contradictory flags (`--profile-dir` with `--no-launch`). |
| 5 | Could not confirm what was running, or Desktop would not go down. Nothing was relaunched; Desktop may already be stopped. |

### Stable interface

These are the parts other tools may depend on. Anything else is an implementation detail and may change without notice.

- The flag names above, and the exit-code table.
- `--json`, whose keys are `killed`, `launched`, `exe`, `dry_run`, `observed_profile`, `observed_profile_conflict`, `launch_profile_dir`, `profile_source`, `package_status`. On a handled error it emits `error` and `exit_code` instead. **Prefer `--json` to parsing the prose output** — the human-readable lines are free to change wording. One gap to code for: a *usage* error (an unknown flag, or `--profile-dir` together with `--no-profile`) is rejected by the argument parser before `--json` is considered, so it exits 2 with a usage message on stderr and **no JSON on stdout**. Treat empty stdout as a usage error and read stderr.
- `main` as an importable entry point (`from hard_restart_claude_code import main`), which is how a non-Python caller drives it through this package's interpreter.

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

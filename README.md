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
hrcc --verify               # wait for the package, then confirm Desktop came back
hrcc --progress-file <path> # publish what the restart is doing, as JSON
hrcc --label <text>         # an opaque display string for that file
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

### Confirming the relaunch

Under `--verify`, spawning the executable is not the end of the restart. `hrcc` then polls for a live Desktop, and if none appears it backs off and launches again, up to eight times. The attempt that worked is reported as `attempts`.

Whether Desktop came back is decided by looking for **Desktop**, never by watching the process `hrcc` spawned. An MSIX launcher hands off and exits within a second on a perfectly good launch, so treating that exit as failure would condemn a start that worked.

The child's fate decides one thing only - whether trying again is safe:

- **Child gone, Desktop still absent** after a few seconds' grace: the launch really failed. Back off and try again.
- **Child still running**: Desktop is coming up slowly, not failing. `hrcc` stops and reports it. Launching again here is how one restart becomes two Desktops on two data dirs, which is worse than the wait. A launcher that cannot report the child's fate at all counts as still running, for the same reason.
- **The process survey stops answering**: `hrcc` refuses to guess, and stops.

All three of those exit 5 with Desktop down, so a caller must read the exit code rather than assume a return means Desktop is back.

### Publishing progress

A hardened restart takes tens of seconds and can fail with nobody watching - the terminal that started it is usually inside the Desktop being killed. So `hrcc` **writes** what it is doing:

```powershell
hrcc --profile-dir D:\profiles\work --label work --progress-file $env:LOCALAPPDATA\myapp\restart-status.json
```

`hrcc` is a writer and never a UI owner. It does not draw anything, it does not spawn anything, and it has no opinion about who reads the file. `--label` is an **opaque display string** - it exists so the file can carry a human-meaningful name without `hrcc` learning what the caller thinks it is restarting for.

Two files are written. The **state file** is a single slot, rewritten on every phase change, and is therefore evidence of the *current* phase and never of the run - a fast success is indistinguishable from a run where the package gate never fired. Beside it, `<name>.trace.jsonl` is an append-only line-per-phase record of this one run, truncated when the run starts. That is the one to read afterwards.

The state file's shape:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Bumped when this table changes. |
| `phase` | One of `stopping`, `waiting-down`, `waiting-package`, `launching`, `waiting-up`, `done`, `stopped`, `failed`. `stopped` is a *success* with Desktop deliberately left down (`--no-launch`); `done` always means it is running. |
| `detail` | A short human-readable headline for the phase. |
| `label` | Whatever `--label` was given, or `null`. |
| `attempt`, `maxAttempts` | Which launch attempt is in flight, and the bound. Integers. |
| `packageStatus` | What the package reported while `waiting-package`, else `null`. |
| `error` | A short reason, on `failed` only. |
| `pid` | The `hrcc` process writing the file. |
| `startedAt`, `updatedAt` | RFC 3339 with an explicit UTC offset. |

Notes for anyone writing a reader:

- **Be lenient.** `hrcc` refuses to write an unknown phase, but a reader on a different release cadence should ignore fields it does not know and fall back to `detail` on a phase it does not recognise.
- **The timestamps carry an offset on purpose.** A naive UTC string is read as *local* time by PowerShell's `[datetime]`, which puts every frame past any staleness cutoff.
- **UTF-8, no BOM, integers stay integers.**
- The file is written temp-then-renamed, falling back to a plain overwrite when the rename fails - which it does, permanently, under MSIX path virtualization. A reader may therefore see a torn file on rare occasions and should treat an unparseable read as a skipped frame rather than an error.
- **Publishing never breaks the restart.** If the file cannot be written, `hrcc` carries on and warns.

- **One run at a time.** Two verifying runs cannot overlap - the second refuses with exit 6 (see below), so the state slot and the trace stay single-writer.

Without `--progress-file`, a verifying run publishes into `~/.hrcc/`. A bare `hrcc` publishes nothing, and neither does `--dry-run`, which changes nothing by definition. Given the flag explicitly, an unverified run still publishes `stopping` and then a terminal phase - every path that ends a restart leaves one behind, because a file parked on `stopping` forever cannot be told from a hung restart.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 2 | Could not resolve a single Claude Desktop install, or `--exe` does not exist. Also argparse usage errors. Under `--verify` a missing executable is a failed *attempt* rather than this error, because an update in flight puts the package back within seconds. |
| 3 | `--profile-dir` failed validation. |
| 4 | Contradictory flags (`--profile-dir` with `--no-launch`). |
| 5 | Could not confirm the state of the restart: what was running, that Desktop went down, or that it came back. Desktop may be stopped. The prose and JSON both report which pids were killed, which is how "nothing happened, safe to retry" stays distinguishable from "Desktop is down and did not come back". |
| 6 | Another verifying restart is already in progress; this one did nothing. Only `--verify` runs take the lock, and `--dry-run` never does, so a bare `hrcc` is neither blocked by one nor blocks one. |

### Stable interface

These are the parts other tools may depend on. Anything else is an implementation detail and may change without notice.

- The flag names above, and the exit-code table.
- `--json`, whose keys are `killed`, `launched`, `exe`, `dry_run`, `observed_profile`, `observed_profile_conflict`, `launch_profile_dir`, `profile_source`, `package_status`, `attempts`. `attempts` is the launch attempt that succeeded, and `0` whenever no launch was attempted at all - `--dry-run`, `--no-launch`, and every path that refuses before spawning. On a handled error it emits `error`, `exit_code`, `killed` and `attempts` instead, so a failed restart still says what it killed and how many launches it tried without anyone parsing the prose. **Prefer `--json` to parsing the prose output** — the human-readable lines are free to change wording. One gap to code for: a *usage* error (an unknown flag, or `--profile-dir` together with `--no-profile`) is rejected by the argument parser before `--json` is considered, so it exits 2 with a usage message on stderr and **no JSON on stdout**. Treat empty stdout as a usage error and read stderr.
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

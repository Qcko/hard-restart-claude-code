# BRIEF — hard-restart-claude-code

Immutable charter. Do not edit casually; the project's running state lives in
`SESSION.md`.

## Problem

Claude Code reads its hook configuration (and several other things) at session
start. When you change `~/.claude/settings.json`, the running session keeps
using the old config. "Quit" from the tray icon doesn't always actually exit
every helper process — Electron apps leave background workers behind that
silently keep an old session alive, so the next "fresh" launch inherits stale
state.

A truly clean restart requires: enumerate every running process from the
Claude Desktop install directory, kill them all, then relaunch.

## Scope (v1)

- Windows only. Microsoft Store install of Claude Desktop.
- One CLI: `hard-restart-claude-code` (alias `hrcc`).
- Default behavior: kill every `claude.exe` whose executable path is under
  `<ProgramFiles>\WindowsApps\Claude_`, wait briefly, relaunch the app.
- Flags: `--exe <path>` to override, `--dry-run` to inspect without acting,
  `--no-launch` to kill without relaunch.
- Zero runtime dependencies (stdlib only). Dev deps: `pytest`, `ruff`.
- `uv` for env management. Editable global install via `uv tool install`.

## Out of scope (v1)

- macOS/Linux. The Desktop app only ships for Windows here today.
- Restarting the Claude Code CLI (the terminal-side process). The CLI is per-
  shell; user closes the shell.
- Restoring window layout / open conversations after restart.
- A GUI / tray icon. CLI is enough.

## Scope (v1.1) — preserve the running profile

`hrcc` relaunches Claude Desktop with the `--user-data-dir` it was already
running under, rather than always launching bare. Restoring the session that
was running is a refinement of "relaunch the app", not a new responsibility:
the tool observes its own target's argv, and takes no dependency on whatever
put that flag there.

Still out of scope, and stated so it reads as considered rather than forgotten:

- **Taking a blocked MSIX update.** When "Relaunch to update" fails with "another
  program is currently using this file", the blocker is a browser
  native-messaging host living inside the package (see `DESIGN.md`). The fix is
  to remove the browser extension; `hrcc` does not kill browser-owned processes
  or wait on Windows deployments.
- **Any flag other than `--user-data-dir`.** Preserved argv is an allowlist, not
  a general replay of the old command line.

## Design principles

- **Match by install path, not just name.** `claude.exe` is also the name of
  unrelated binaries; matching only by basename risks killing the wrong thing.
  Keep `ExecutablePath` and test it as a **prefix** against
  `<ProgramFiles>\WindowsApps\Claude_`, in Python rather than in the PowerShell
  query. A substring test is not enough: it also matches a directory the user
  can create, and a matched process has its `--user-data-dir` read back and
  handed to the relaunch. `<ProgramFiles>\WindowsApps` is admin-only, and that
  is the property the match relies on.
- **Inject side effects.** `find_pids` / `kill_pids` / `launch` are passed in
  as callables to `hard_restart` so the orchestration is unit-testable without
  touching real processes.
- **Fail loudly on missing exe**, not silently. Exit code 2 from the CLI.
- **Stay consistent with `localguard`** — same project layout, same `uv` flow,
  same `BRIEF.md` / `SESSION.md` convention, same `uv tool` install location.
  The two tools are siblings.

## Success criteria

- `hrcc --dry-run` lists Claude Desktop PIDs accurately.
- `hrcc` reliably produces a fully-restarted Claude Desktop with new config
  loaded — verified by changing `~/.claude/settings.json` and observing the
  next session reads the change without manual intervention beyond `hrcc`.

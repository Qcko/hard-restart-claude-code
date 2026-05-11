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
- Default behavior: kill every `claude.exe` whose `Path` lives under
  `WindowsApps\Claude_*`, wait briefly, relaunch the app.
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

## Design principles

- **Match by install path, not just name.** `claude.exe` is also the name of
  unrelated binaries; matching only by basename risks killing the wrong thing.
  Filter by `$_.Path -like '*WindowsApps\Claude_*'`.
- **Inject side effects.** `find_pids` / `kill_pids` / `launch` are passed in
  as callables to `hard_restart` so the orchestration is unit-testable without
  touching real processes.
- **Fail loudly on missing exe**, not silently. Exit code 2 from the CLI.
- **Stay consistent with `localguard`** — same project layout, same `uv` flow,
  same `BRIEF.md` / `SESSION.md` convention, same `E:\uv\tools\bin` install
  location. The two tools are siblings.

## Success criteria

- `hrcc --dry-run` lists Claude Desktop PIDs accurately.
- `hrcc` reliably produces a fully-restarted Claude Desktop with new config
  loaded — verified by changing `~/.claude/settings.json` and observing the
  next session reads the change without manual intervention beyond `hrcc`.

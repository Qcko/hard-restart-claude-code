# DESIGN — hard-restart-claude-code

The *how*. `BRIEF.md` holds the *why* and the charter; `SESSION.md` is the
running log.

## Preserving the account profile on relaunch

### The problem

`hrcc` v1 always relaunched Claude Desktop bare:

```python
subprocess.Popen([str(exe)], ...)
```

The sibling project `account-swap` selects which account Desktop runs as by
passing `--user-data-dir <profile>`. So a bare relaunch silently moved the user
back to the default account, losing the account they were actually working in.

v1 mitigated this socially — a `SESSION.md` note saying "use `account-swap swap`
rather than bare `hrcc`". That held only because `hrcc` was a deliberate
config-reload tool, run by someone thinking about their Claude setup. It is a
weak mitigation: the check depends on the user remembering, at exactly the moment
their attention is on hooks or settings rather than on accounts.

Measured on a live system while writing this: 12 Desktop processes running, all
of them under `--user-data-dir=<secrets>\account-swap\userdata\reserve`. A bare
`hrcc` at that moment would have moved the session to the default account with
no message of any kind.

### The design

`hrcc` reads the `CommandLine` of the Desktop processes it is about to kill,
extracts `--user-data-dir` if present, and reproduces it on relaunch.

This is **not** a dependency on `account-swap`. `hrcc` observes its own target
process's argv and restores it, which is squarely within "restart what was
running". Any future flag worth preserving extends the same seam.

Consequences for structure:

- Process discovery moves from `Get-Process` to
  `Get-CimInstance Win32_Process`, because the command line is needed and
  `Get-Process` does not carry it. This also removes a latent hazard:
  `Get-Process`'s `.Path` requires `OpenProcess` rights and yields `$null` for
  processes outside the caller's reach, which would silently drop them from the
  match.
- PowerShell now returns JSON (`ConvertTo-Json`) rather than bare lines. A
  command line can contain spaces, quotes and delimiters, so line-splitting is
  not safe. Decoding tolerates a single object as well as an array, since
  PowerShell emits an object rather than a one-element array in some paths.
- `find_pids` becomes `find_processes`, returning `ClaudeProcess` records
  (`pid`, `path`, `profile_dir`). The profile therefore arrives through the
  existing injected `finder` seam, so `hard_restart` gains no new parameter — it
  is already at the Rule of 7 limit.
- `Result` gains `profile_dir` so the CLI can report what it preserved.

### The matcher stays narrow, deliberately

The path filter `*WindowsApps\Claude_*` is load-bearing safety, not incidental.
`account-swap` keeps a Claude Code CLI at

```
<secrets-dir>\account-swap\userdata\<profile>\claude-code\<version>\claude.exe
```

whose basename is also `claude.exe`. Widening the matcher to a `*Claude*`
substring would kill the user's own running CLI. A regression test asserts the
query keeps the `WindowsApps\Claude_` anchor and never widens to `*Claude*` —
testing an absence, because that absence is the safety property.

### Lifecycle

```mermaid
flowchart TD
    start([hrcc]) --> discover[discover_exe:<br/>Get-AppxPackage InstallLocation]
    discover --> found{Exactly one<br/>install location?}
    found -->|no| bail([exit 2: pass --exe])
    found -->|yes| enumerate[find_processes:<br/>Win32_Process, claude.exe<br/>under WindowsApps/Claude_]

    enumerate --> sort[Sort by pid<br/>so selection is deterministic]
    sort --> profile[Parse --user-data-dir<br/>from each CommandLine]
    profile --> select{How many<br/>distinct profiles?}
    select -->|0| bare[Relaunch bare]
    select -->|1| one[Preserve it]
    select -->|2 or more| conflict[Preserve lowest-pid one,<br/>warn on stderr]

    bare --> dry{--dry-run?}
    one --> dry
    conflict --> dry
    dry -->|yes| report[Report pids + profile<br/>that WOULD be preserved] --> done([exit 0])
    dry -->|no| kill[kill_pids:<br/>taskkill /F per pid, NOT /T]

    kill --> settle[sleep settle_seconds]
    settle --> nolaunch{--no-launch?}
    nolaunch -->|yes| skip([exit 0: killed, not launched])
    nolaunch -->|no| relaunch[Launch exe<br/>+ --user-data-dir if one was in use]
    relaunch --> finish([exit 0])
```

## Why `taskkill` must NOT use `/T`

Tree-kill looks like the right verb for a tool whose purpose is leaving nothing
behind, and it was tried. It is wrong here, and the reason is worth recording so
nobody re-adds it.

`hrcc` is normally run from a shell **inside** Claude Desktop — the
`restart-claude` skill invokes it through Desktop's own Bash tool. So the `hrcc`
process is a descendant of a `claude.exe` that is in the pid list it is about to
kill. Measured ancestry during development:

```
powershell.exe -> bash.exe -> bash.exe -> bash.exe -> claude.exe -> claude.exe (25988)
```

and pid 25988 was in that run's matched set. `taskkill /F /T /PID 25988` walks
the subtree and kills the shell and `hrcc` with it — so Desktop dies, nothing
relaunches, and the profile this design exists to preserve is never applied.
The failure is invisible to `--dry-run`, which returns before any kill happens.

Individual `/F /PID` kills are correct: the `Win32_Process` query already
enumerates every Desktop process directly, so there is no orphan left for `/T`
to catch that is not already in the list.

## Why `discover_exe` refuses two packages

`Get-AppxPackage -Name 'Claude'` can return two rows while an MSIX update is
being deployed — the staged new package and the installed old one. v1 took the
first non-empty line, which is a coin flip during exactly that window, and the
consequence is relaunching the wrong version.

Distinct install locations are now collected, and anything other than exactly one
returns `None`, so the CLI fails with its existing clear message instead of
guessing. Duplicate identical rows are tolerated, since they are not ambiguous.

## Appendix: why "Relaunch to update" can fail

Recorded because it took real instrumentation to find, and the cause is
non-obvious enough that the next person to hit it will have no idea where to
look. **This is not something `hrcc` works around** — see "Deliberately not
built" below.

Claude Desktop's "Relaunch to update" can fail with:

> Another program is currently using this file.

The blocker is `chrome-native-host.exe`, the browser native-messaging bridge,
whose image and working directory sit **inside the MSIX package**:

```
%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\ChromeNativeHost\
```

While it runs, MSIX cannot replace the package. It survives Desktop's exit
because Desktop is not its parent. Measured ancestry:

```
explorer.exe -> msedge.exe -> cmd.exe -> chrome-native-host.exe
```

The **browser** spawns it. A browser extension asks for a native-messaging host
by name; the browser reads the manifest registered under its own
`NativeMessagingHosts` registry key and launches the named executable itself,
wiring stdin/stdout to pipes. Claude Desktop registered a manifest pointing at a
binary inside its own updatable package, so the browser ends up pinning the
package Claude is trying to update.

Despite the naming, this is not Chrome-specific — the observed browser was Edge,
which is Chromium and speaks the same protocol.

The contrast that identifies it as a packaging mistake rather than a
configuration problem:

| host                                      | manifest location                          |
| ----------------------------------------- | ------------------------------------------ |
| `com.anthropic.claude_browser_extension`   | inside the MSIX package (pins it)          |
| `com.anthropic.claude_code_browser_extension` | `AppData\Roaming\Claude Code\` (pins nothing) |

Same feature, same protocol, two locations — only one of which can block an
update.

**The fix is to remove the Claude browser extension**, which unregisters the
host so nothing spawns it. That is a user action, not a `hrcc` responsibility.

## Deliberately not built

- **Update-taking.** An earlier draft had `hrcc` kill the native-messaging host,
  poll `Get-AppxPackage` for the MSIX deployment to settle, and relaunch — making
  `hrcc` the way to take a blocked update. Dropped once the root cause was
  understood: removing the browser extension eliminates the blocker entirely, so
  the machinery would have been several hundred lines of concurrency-shaped code
  guarding against a condition the user no longer has. If the extension is ever
  reinstalled, the appendix above is the starting point.
- **Killing the native-messaging host.** Follows from the above, and it reaches
  outside Claude Desktop into a browser's process tree — a side effect beyond
  this tool's remit.
- **Launching by AUMID.** `shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude` is
  version-independent, which is attractive, but `shell:AppsFolder` activation
  does not accept arbitrary argv and so cannot carry `--user-data-dir`. Profile
  preservation is worth more than version-independent activation, and
  `discover_exe` already re-resolves the path on every run.

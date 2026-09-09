# agent-browser-session-reaper

Automatic cleanup for abandoned `agent-browser` sessions, in two cooperating parts.

## Why

An `agent-browser` session is a daemon plus a headless-Chrome process tree. One idle
session measured on this machine on 2026-09-04: **14 processes, ~1.2 GB RSS**. Agent
runs that open a browser and never close it leave the whole tree resident. That day the
machine hit memory exhaustion twice (swap 13.6 GB used of 14.3 GB, background tasks
killed by the OS); two manual cleanups found 4 and then 5 abandoned sessions, together
~2.4 GB and 82 processes at peak.

## The two mechanisms

| | Session-end hook | Periodic sweep |
| --- | --- | --- |
| Fires | when a Claude Code session ends | every 15 minutes (launchd) |
| Closes | only sessions **that session claimed** | any session past the **age threshold** |
| Knows the owner | yes | no |
| Covers a crashed agent | no | yes |
| Script | `session_end_close.py` | `agent_browser_reaper.py` |

Neither replaces the other. The hook cannot fire for a session that was killed; the
sweep cannot tell whose browser it is looking at.

Ownership comes from a third piece, the `pre-bash` agent-hook
[`agent-browser-session-claim`](../../agent-hooks/agent-browser-session-claim/README.md),
which records every `agent-browser` session name a Claude Code session touches. Without
it the only possible session-end action would be `agent-browser close --all` — which,
on a machine running several agents in parallel tmux windows, would tear down every
sibling agent's live browser on every session exit.

## What the scripts actually run

Only agent-browser's own supported commands. **Nothing here ever sends a signal to a
process, and nothing uses `pkill`.**

- enumerate: `agent-browser session list --json` — verified read-only (0 matching
  processes before the call, 0 after). Note the trap this avoids: `agent-browser tab
  list` **launches** a browser when none is running, so it can never be used as an
  "is anything running?" probe.
- close: `agent-browser --session <name> close --json` — one named session at a time.

A PID read from `~/.agent-browser/<name>.pid` is used for exactly one thing: reading an
elapsed time out of `ps`. Even a stale or recycled PID can therefore only skew an age
estimate, never target a kill.

## How age is determined — and the honest limit

**agent-browser exposes no per-command last-activity timestamp.** That was measured, not
assumed:

- `session list` returns names only; `session info --json` returns
  `{active, pid, session, socketDir, version}` — no timestamps anywhere.
- `~/.agent-browser/<name>.target` mtime moves on navigation / tab rebinding but **not**
  on `get url`, `get title` or `eval` (five consecutive `get url` calls left it
  unchanged).
- The Chrome `--user-data-dir` mtime does not move on those commands either.
- The daemon's own CPU time is far too coarse (five commands moved it by 0.00s).

The daemon *does* keep its own last-activity clock internally — that is what
`--idle-timeout` / `AGENT_BROWSER_IDLE_TIMEOUT_MS` act on, and both were verified to
work (a session started with `--idle-timeout 20s` self-terminated within 30s) — but it
does not expose that clock to any caller.

So the sweep computes, deliberately named `last_seen` rather than "last activity":

```
last_seen = max(daemon process start time, mtime of every ~/.agent-browser/<name>.* sidecar)
```

Both inputs are real timestamps. Taking the **max** is the conservative direction: it can
only make a session look *younger*, i.e. err toward not closing it.

**The residual case, stated plainly:** a session driven hard for hours *without ever
navigating* looks its full lifetime old and will be closed at the threshold. The
mitigations are the generous default (3 h, longer than any agent verification run
observed here) and `KEEP`, which exempts named sessions outright. If a session must
never be touched, put its name in `KEEP`.

## Install / uninstall / status

```
./install.sh              # installs both mechanisms + the claim hook, loads launchd
./install.sh --status     # what is currently installed
./install.sh --uninstall  # removes all of it
```

`install.sh` copies every script to `~/.local/share/agent-tools/agent-browser-session-reaper/`
before anything points at it, because this installer normally runs from a throwaway
feature worktree and both the plist and the hook descriptor need a path that outlives it.

Installed artifacts:

| Path | What |
| --- | --- |
| `~/Library/LaunchAgents/com.hyperide.agent-browser-session-reaper.plist` | the 15-minute sweep |
| `~/.claude/settings.json` → `hooks.SessionEnd` | the session-end hook (backup written as `settings.json.agent-browser-bak-<stamp>`) |
| `~/.claude/hooks/agent-browser-session-claim.pre-bash.json` | the ownership recorder |
| `~/.config/agent-tools/agent-browser-reaper.conf` | config (created once, never overwritten) |
| `~/Library/Logs/com.hyperide.agent-browser-session-reaper.log` | sweep log |

## Configuration and defaults

`~/.config/agent-tools/agent-browser-reaper.conf`, `KEY=VALUE`. Every key is also
readable as `AGENT_BROWSER_REAPER_<KEY>` in the environment, which wins. launchd starts
jobs with no shell environment, so the file is what actually configures the sweep.

| Key | Default | Meaning |
| --- | --- | --- |
| `ENABLED` | `1` | master switch for the periodic sweep |
| `MAX_AGE_MIN` | `180` | close a session once `last_seen` is this old |
| `KEEP` | *(empty)* | comma-separated session names never closed by either mechanism |
| `SESSION_END_ENABLED` | `1` | switch for the session-end hook |
| `SESSION_END_REASONS` | `logout,prompt_input_exit,other` | which Claude Code end reasons act (deliberately not `clear`/`resume`) |
| `CLAIM_TTL_HOURS` | `24` | sweep prunes ownership records older than this |

## Turning it off

- **One session, permanently:** add its name to `KEEP`.
- **The sweep only:** `ENABLED=0` in the config file.
- **The session-end hook only:** `SESSION_END_ENABLED=0` in the config file.
- **All of it, removed:** `./install.sh --uninstall`.

## Testing

```
python3 -m pytest tests/test_agent_browser_reaper.py -q
python3 ops/agent-browser-session-reaper/agent_browser_reaper.py --dry-run
```

`--dry-run` reports what it would close and closes nothing.

## Status: manually installed, not wired into `rig apply`

Same honest scope as the sibling `ops/vscode-orphan-reaper`: `rig apply`'s machine-wide
provisioning (`lib/agenttools_service`) only knows how to install a `RunAtLoad` +
`KeepAlive` LaunchAgent for a long-running daemon, not a `StartInterval` periodic job.
The `pre-bash` claim hook *is* a normal catalog agent-hook and could be provisioned by
`rig` in the usual way; the SessionEnd entry in `settings.json` could not, because the
agents-hooks/v1 contract has no `session-end` point and `lib/cc_hook_bridge` dispatches
only PreToolUse / PostToolUse / Stop.

## Coverage gap: the session-end half depends on the claim hook firing

The session-end hook closes what the claim hook recorded, and nothing else. If the
`pre-bash` claim hook does not fire for a given tool call, that browser session is simply
never claimed and the session-end hook leaves it alone — the periodic sweep then closes it
on age. That is the intended failure mode (fail-safe: an unrecorded session is never
closed by the owner-aware half), but it does mean the session-end half is only as good as
the claim hook's coverage.

One measured instance of exactly that: `pre-bash` agent-hooks were observed **not** firing
for Bash tool calls made from inside a dispatched subagent in a Claude Agent SDK session on
2026-09-04 — a `git reset --hard` that `block-reset-hard` should have blocked went through
untouched, so nothing in that context was claimed either. Whether that is subagent-specific,
SDK-specific, or a settings-reload delay was not established. The hook itself is correct:
driving `lib/cc_hook_bridge` by hand with a synthetic `PreToolUse` Bash event writes the
expected marker. Verify coverage in your own harness before relying on the session-end half
alone.

## Coverage gap: other harnesses

The session-end half is **Claude Code only**. Codex and opencode both need their own
carrier bridge, and neither has a confirmed session-end contract (`lib/opencode_hook_bridge`
explicitly does not map `stop`, and Codex's `SubagentStart`/`SubagentStop` payloads are
not trusted yet). Browser sessions abandoned by those harnesses are covered by the
periodic sweep only.

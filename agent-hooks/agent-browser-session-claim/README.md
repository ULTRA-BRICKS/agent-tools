# agent-browser-session-claim (`pre-bash`)

Records which `agent-browser` sessions the current Claude Code session opened, so the
SessionEnd cleanup hook can close **only those** and leave every parallel agent's
browser alone.

## What it does

On every Bash command that invokes `agent-browser`, it touches

```
~/.cache/agent-tools/agent-browser-sessions/<claude-session-id>/<browser-session-name>
```

— the same marker pattern `skills-marker-writer` uses for `skills-read-gate`. It never
blocks: `on_error: open`, and every code path emits `allow` / exit 0.

Which names it claims:

- every explicit `--session NAME`, `--session=NAME`, or `AGENT_BROWSER_SESSION=NAME`
  found in the command line;
- `default` when the invocation names no session, because that is the name
  agent-browser itself uses in that case;
- nothing at all for read-only subcommands that cannot create a session
  (`session list`, `session info`, `session id`, `skills`, `doctor`, `profiles`,
  `plugin`, `--help`, `--version`).

## Why it has to exist

`agent-browser` records no owner for a session — a session is a bare global name, and
`agent-browser session list` returns names only. Without this marker the only action a
session-end cleanup could take is `agent-browser close --all`, which on a machine
running several agents in parallel tmux windows would tear down every sibling agent's
live browser on every session exit. Attribution is what makes automatic session-end
cleanup safe enough to run at all.

The consumer is `ops/agent-browser-session-reaper/session_end_close.py`; the motivating
incident (memory exhaustion from ~2.4 GB of abandoned browser sessions, 2026-09-04) is
written up in that directory's README.

## Why over-claiming is safe

Claiming a name that was never actually opened costs nothing: the session-end hook only
closes a claimed name that `agent-browser` independently reports as **live**, that is not
in the `KEEP` list, and that no other claim directory also claims. Under-claiming is the
expensive direction (a leaked browser tree), so the parser errs toward claiming.

## Install

`ops/agent-browser-session-reaper/install.sh` installs this descriptor with an absolute
`cmd` alongside the two cleanup mechanisms. To install it by hand instead: copy
`claim_agent_browser_session.py` somewhere stable, `chmod +x` it, set the descriptor's
`cmd` to that absolute path, and drop the descriptor into `~/.claude/hooks/`.

## Fail policy

`on_error: open`. A missed marker costs one uncleaned browser session — which the
periodic sweep still catches on age — while a blocked Bash command would cost the agent
its ability to work. Never make this hook `closed`.

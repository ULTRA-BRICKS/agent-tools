# pkill-guard

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that kills processes by **name/pattern match** — `pkill -f <name>`,
`killall <name>`, `kill $(pgrep <name>)`, `pgrep <name> | xargs kill` — when the pattern being
matched is a generic, widely-shared tool/process name (`node`, `codex`, `claude`, `review
diff`, `playwright`, ...). `pkill -f`/`killall`/`pgrep` match against **every process on the
machine** whose command line contains the pattern, not just the caller's own.

Ref: the 2026-07-01 agent-ecosystem retrospective (`hyperide` repo,
`docs/specs/2026-07-01-agent-ecosystem-retrospective.md`), gap **G-5**. Not linked directly —
`hyperide` is a separate, private repository, so a relative path here would escape this repo
and 404 on GitHub's web UI for anyone without that repo as a sibling checkout.

## Why this incident needed a hook

Two real incidents, both **accidental collateral damage**, not a deliberate bypass:

- 2026-06-26: a subagent's `pkill -f "review diff"` killed a **different session's** in-flight
  code review.
- 2026-06-27: a narrow-grep-based kill nearly killed another session's e2e matrix run.

`pkill`/`killall`/`pgrep` have no concept of "my own processes only" — a pattern that happens
to also appear in another concurrent session's command line kills that session's work too.
This hook turns "kill everything matching this word" into a deliberate, scoped, or
explicitly-approved action.

## What's blocked vs. allowed

A pattern is **dangerous** only when it is — or contains, as a whole word/phrase — a known
**shared** tool/process name (see `_SHARED_PROCESS_NAMES` in the script) **and** it carries no
session-scoping signal. This is a denylist of known-ambiguous names, not a blanket "block
every pattern kill":

- **Blocked**: `pkill -f "review diff"`, `killall node`, `kill $(pgrep -f codex)`,
  `pgrep -f playwright | xargs kill`, `ps aux | grep claude | awk '{print $2}' | xargs kill -9`
- **Allowed**:
  - `kill <pid> [<pid> ...]` (optionally with `-SIGNAL`/`-s SIGNAL`) — always PID-targeted,
    can't hit another session's process by name collision.
  - A pattern that also carries a session-scoping signal even if it names a shared tool —
    a path (`pkill -f "/Users/ultra/work/hyperide-worktrees/agent-x/.../vitest"`), the e2e
    harness's isolation prefix (`pkill -9 -f "hvsc-3-a1b2c3d4"`, the sanctioned recipe from
    `ext-test-projects`'s repo instructions), or any hex/uuid-looking or long-digit run.
  - A pattern not on the denylist at all — this hook fails **open** on unknown names; it gates
    known-ambiguous ones, not every `pkill` invocation on the machine.
  - `dev stop` (the project's own repo-scoped dev/e2e process stop command) never invokes
    pkill/killall/kill itself, so it is never touched by this hook.

## Parsed, not raw-matched

Detection is argv-based (shlex), same discipline as `block-reset-hard`/`block-raw-pr-merge`:
the command is tokenized (a newline is a command separator, same as `;`), split into
**pipeline groups** — separators `;`/`&&`/`||`/`&` start a new group, `|`/`|&` continue the
same group as a new stage — and each stage's real argv is recovered after stripping leading
shell-grouping tokens, inline `VAR=value` assignments, and a wrapper table (`sudo`, `timeout`,
`env`, `nice`, `time`, ...). A `#` comment is recognized with the same dual-pass (quoted vs.
raw token) scan `block-reset-hard` uses — a stray quote character sitting inside a real
comment (`echo ok # it's a comment`) is never mistaken for a genuinely unclosed quote that
would otherwise merge the comment forward into a later line and swallow a real command inside
what looks like one giant quoted argument. `xargs`'s wrapped command is resolved through the
same wrapper table too, so `pgrep -f node | xargs env kill` / `xargs sudo kill` are caught, not
just a bare `xargs kill`.

Three shapes are classified:

1. **Direct `pkill`/`killall`**: EVERY non-flag positional operand is checked, not just the
   trailing one — both tools accept multiple names (`killall node worker`), and a flag that
   itself takes a value (`pkill -f node -u root`) must not let that value be mistaken for "the"
   pattern and hide the real one. Trailing shell redirections (`>/dev/null 2>&1`) are stripped
   before this scan, so their target never gets treated as a candidate pattern either.
2. **`kill` fed a command substitution**: `$(pgrep ...)` / `` `pgrep ...` `` spans are replaced
   with opaque placeholder tokens *before* shlex tokenizing (so the substitution's own internal
   whitespace doesn't fragment `kill`'s argv), then re-parsed as their own mini pipeline to find
   the wrapped `pgrep` pattern. One level of substitution nesting is resolved.
3. **A pipeline that resolves PIDs by pattern and kills them**: ANY stage that actually issues
   the kill (bare `kill` or `xargs ... kill ...`), not just the last one — a kill can genuinely
   execute in a middle stage with a trailing consumer after it (`pgrep -f node | xargs kill |
   tee log.txt`); every OTHER `pgrep`/`grep`/`egrep`/`fgrep` stage in the same pipeline group is
   checked for a pattern (not just the first one — an accidental filtering stage before the real
   pattern, `grep -v noise | grep <pattern>`, must not hide it). Covers `pgrep <pattern> | xargs
   kill`, the "narrow grep" shape `ps aux | grep <pattern> | ... | xargs kill`, and a kill
   followed by more pipeline stages.

## No self-service bypass — Telegram hatch only

Deny-by-default. Request a one-time Telegram approval with a written justification:

```bash
RIG_HATCH_REQUEST_PKILL_GUARD="<reason>" pkill -f node
```

Routes through the shared `agenttools_hatch_escalation` helper (same mechanism as
`background-subagent-gate`/`block-raw-pr-merge`) — a blank/bare-flag value (`1`, `true`) is
rejected, no Telegram call is made, and the command is denied. Every attempt (approved or
denied) is auto-recorded in `overrides.log` (gap **G-8**, already shipped). Nothing set = the
block stands; use a PID-targeted `kill <pid>`, `dev stop`, or scope the pattern to something
session-unique instead of asking for an exception.

A late approval is not wasted: if the human approves after the ask window expires, the tg-ctl daemon keeps it as a single-use `approved:<id>` grant bound to this exact operation (RIG_HATCH_REQUEST_PKILL_GUARD="approved:<id>" inline for pre-bash retries of the SAME command in the SAME directory; for any hook — pre-write included — simply retrying the identical operation lets the shared lib consume the grant automatically via its pull, which requires this session's route identity to match the original ask — if the retry still denies, ask the human via `tg --tag decision`). A changed command/edit or a different directory is denied and does not consume the grant. See the shared lib docstring (`lib/agenttools_hatch_escalation/`) for the full Late-Approval Grant Protocol (agent-tools#742).

## Fail-closed

`on_error: "closed"`. A malformed event or a crash **blocks** rather than allows — a collateral
pattern-kill slipping through a broken guard is the exact failure this hook exists to stop.
Unbalanced quotes that can't be tokenized, when the raw text still plausibly names a denylisted
process via `pkill`/`killall`/`kill`/`pgrep`, are also treated as **unparseable → block**.

## Known limitations

- The denylist (`_SHARED_PROCESS_NAMES`) is necessarily incomplete. An unlisted shared name is
  not caught — this fails **open** toward not blocking legitimate, project-specific kills the
  hook has no reason to distrust, not toward silently missing a real collision (the two
  documented incidents are both on the denylist).
- Only **one level** of command-substitution nesting is resolved for `kill $(...)`; a
  substitution containing a further nested substitution is not recursed into.
- `pkill -u <user>` / `pkill -g <pgrp>` and similar value-taking flags are not recognized as
  consuming the next token, so their VALUE (e.g. `node` in `pkill -u node`) is read as a
  candidate pattern and can trigger a block that's really about a username filter, not a kill
  pattern — an **over**-block, never a bypass, and a deliberate tradeoff (see the source
  docstring) rather than an oversight.
- Process filtering via `awk '/pattern/{print $2}'` piped into a kill is not recognized — only
  `pgrep`/`grep`/`egrep`/`fgrep` stages are scanned for a pattern.
- A **relative** path (`pkill -f "node scripts/dev.js"`) is treated as session-scoping even
  though it's identical across every checkout of the same repo — only a bare, well-known
  **global** bin directory (`/usr/bin/node`, `/opt/homebrew/bin/python3`) is excluded from the
  path signal, not a shared relative path. Telling "unique to my session" apart from "merely
  contains a slash" in general needs the caller's actual worktree root, which this hook doesn't have.
- A shell **alias** for `pkill`/`kill`/`killall` is not resolved — same documented gap as every
  sibling hook in this catalog (aliases don't expand under a harness's `bash -c` anyway).
- The session-scoping check treats "a scoping-shaped token ANYWHERE in the pattern" as
  sufficient, which a **deliberately** crafted pattern can defeat: `pkill -f
  "node|session-a1b2c3d4"` is a regex alternation (pkill's `-f` pattern is a POSIX extended
  regex) that still matches every `node` process. Same class of gap as a variable / nested-shell
  indirection (`TARGET=node pkill -f "$TARGET"`, `sh -c 'pkill -f node'`) — the real pattern is
  only known after expansion this hook can't see. Both require deliberate crafting, not an
  accidental shape (this hook's stated threat model); not fixed here.
- The "global bin dir" exclusion only recognizes a handful of well-known system prefixes
  (`/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, `/bin`, `/sbin`) — a version-manager
  interpreter path (Homebrew Cellar, `nvm`, `pyenv`, `asdf`) still reads as session-scoped
  purely because it contains a `/`. Enumerating every version manager's path convention is an
  unbounded list not worth chasing here.
- A heredoc BODY that happens to mention a denylisted name is treated like a real command (no
  heredoc awareness in the newline-as-`;` tokenizer, unlike `block-raw-pr-merge`) — an
  **over**-block, never a bypass, but worth knowing before reaching for the Telegram hatch on a
  legitimately blocked doc edit.
- Command substitution is only inspected in the narrow `kill $(...)` shape — a substitution
  OUTSIDE a direct argument to `kill` (`echo $(pkill -f node)`, `x=$(pkill -f node)`) is not
  resolved at all.
- An inverted-match filtering stage ahead of the real pattern (`grep -v <shared-name> | xargs
  kill`) reads `<shared-name>` as the pattern the same as a normal `grep` would, even though
  `-v` means it's being **excluded**, not matched — an over-block, never a bypass.

## Install

```bash
chmod +x pkill_guard.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory. (rig apply does this for you.)
```

## Test

```bash
echo '{"args":{"command":"pkill -f \"review diff\""}}' | ./pkill_guard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"pkill -9 -f \"hvsc-3-a1b2c3d4\""}}' | ./pkill_guard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

echo '{"args":{"command":"kill 12345"}}' | ./pkill_guard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

Unit tests live in
[`tests/test_pkill_guard.py`](../../tests/test_pkill_guard.py):

```bash
uv run --with "pytest>=8,<9" python -m pytest tests/test_pkill_guard.py -q
```

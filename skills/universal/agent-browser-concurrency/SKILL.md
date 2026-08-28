---
name: agent-browser-concurrency
description: Use every time you run `agent-browser`, not just when you suspect concurrency — you can't know whether a sibling agent or a human is also using it right now. Prevents "Not attached to an active page" errors from two tasks sharing one browser and orphaned Chrome process trees piling up. Applies even for a task as simple as reading one page.
---

# Give every `agent-browser` task its own session, and close it when done

`agent-browser` runs a persistent daemon per **session** — each session is its
own browser instance with its own cookies, storage, and state (this isolation
does not extend to `--cdp`/`--auto-connect` mode, which attaches multiple
sessions to one already-running Chrome and shares its tabs — see the note at
the end of Rule 1). Every invocation that doesn't say otherwise uses the
session named `default`. If anything else on the machine also touches
`default` at the same time, you get `Not attached to an active page` (or
similar target-detached errors) — not from a lack of capacity, but because two
unrelated tasks were forced through one shared session. You can't observe
whether that's happening right now, so treat every invocation as if it might
be — including a plain page-read, and including two tasks that happen to
share a worktree.

## Rule 1 — keep your session name in a file keyed by a label only you use, not in memory

A dispatched agent's separate Bash tool calls don't share shell state — a
variable exported in one call is gone in the next. Asking yourself to
remember and retype a literal value across calls is fragile (a summarized
transcript can drop it, a typo silently switches you to a different session).
Use a small state file instead, and make generating it idempotent so the
exact same command is correct whether it's your first call or your fifth:

```bash
raw='<your-task-label>'
label="$(printf '%s' "$raw" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_')_$(printf '%s' "$raw" | cksum | cut -d' ' -f1)"
d="$(git rev-parse --absolute-git-dir 2>/dev/null)" || { d="${TMPDIR:-/tmp}/agent-browser-$(id -u)"; mkdir -m 700 -p "$d" 2>/dev/null; }
f="$d/agent-browser-session.$label"
umask 077
( set -C; echo "$label-$(date +%s)-$RANDOM-$$" > "$f" ) 2>/dev/null
s=""; for _ in 1 2 3 4 5; do s="$(cat "$f" 2>/dev/null)"; [ -n "$s" ] && break; sleep 0.05; done
if [ -z "$s" ] && [ -e "$f" ]; then
  # Present but still empty after ~200ms of retrying means the writer that
  # created it (via set -C) died before its echo landed — not a live sibling
  # mid-write. Reclaim it once: a genuine live sibling's write would have
  # shown up well within the retry window above.
  rm -f "$f"
  ( set -C; echo "$label-$(date +%s)-$RANDOM-$$" > "$f" ) 2>/dev/null
  s="$(cat "$f" 2>/dev/null)"
fi
if [ -n "$s" ]; then
  export AGENT_BROWSER_SESSION="$s"
else
  unset AGENT_BROWSER_SESSION
  echo "agent-browser-concurrency: could not get a session name for label '$label' ($f) — most likely the sanitized label plus checksum is too long for this filesystem, so the create itself never produced a file to recover; shorten the raw label. Not exporting a session var — behavior without one is unverified here, don't assume it's a safe default." >&2
fi
```

Three things in this snippet exist only to survive concurrent siblings, not
for style — don't simplify them away:

- **The checksum suffix.** Plain `tr` sanitizing is many-to-one — `review/quorum`,
  `review:quorum`, and `review quorum` all collapse to the same
  `review_quorum`, silently merging two actors the labeling rule says must
  stay distinct (this is exactly the multi-role-sharing-a-worktree case
  described below, where the raw labels are likely to differ only in
  punctuation). Appending a checksum of the *un-sanitized* label keeps two
  such labels apart even after `tr` collapses their visible characters.
- **`( set -C; echo ... > "$f" )`.** Without `set -C` (noclobber), the
  original "does the file already have content?  no?  then write" is a
  check-then-act race: two Bash tool calls for the same task, issued in
  parallel, can both see no file and both write, and only one of their
  generated names survives — the other's daemon is now orphaned under a name
  nothing points at. `set -C` makes bash's own `>` redirect fail (silently,
  via the discarded stderr) if the file already exists, so at most one
  concurrent writer ever succeeds.
- **The retry loop around `cat`.** `set -C` makes *creation* exclusive, but
  the file exists (empty) for the instant between the winning `echo`'s open
  and its write landing — a sibling's `cat` in that window reads `""`, not
  the winner's name. Retrying a few times at a 50ms interval closes that
  window in practice without needing a real lock; only export once `$s` is
  actually non-empty, never the raw single-shot `cat` result.

**`<your-task-label>` is required, not decoration — pick something that
distinguishes this task from any sibling that might run concurrently**, and
substitute it in both places above (a role name, a skill name, whatever you
already have; it doesn't need to be globally unique, just distinct from any
task that could genuinely run alongside yours). Always run it through the
`tr` step shown, even if it looks safe already: a label with a `/` in it (a
branch name, a path-like role such as `review/quorum`) makes the file write
fail silently — a plain Bash tool call has no `set -e`, so execution
continues past the failure and `AGENT_BROWSER_SESSION` ends up exported as
an empty string instead of a real session name. Whether the CLI then falls
back to `default` or fails some other way isn't verified here either way,
but nothing about this failure prints an error you'd notice — the label
silently didn't do its job. The `tr` step makes the
label filesystem-safe unconditionally, so this can't happen regardless of
where the label text came from. This is what makes the
scheme safe when two tasks share a worktree — `--absolute-git-dir` already
gives each worktree its own admin directory, so a fixed label is enough
whenever your task has its own worktree (the standard pattern for concurrent
work here — see the `worktree-isolation` skill, where installed); it only
takes on real weight when several actors deliberately share one worktree
(e.g. multiple review-quorum roles against one materialized diff), where each
actor's own role name serves directly as the label. The file itself lives
inside the git directory, not your working tree, so it's never visible to
`git add`, never needs a `.gitignore` entry, and can't be pre-committed by
repo content to hijack your session name. Outside a git repo (or if `git
rev-parse` fails for any other reason — an ancient git, a broken repo, not
just "no repo here"), it falls back to a per-uid directory under `$TMPDIR`,
created `0700` (`mkdir -m 700`, plus `umask 077` before the file itself is
created) so another local user can't read or enumerate it even if they guess
the deterministic `agent-browser-$(id -u)` path. That closes the read/list
side. It does **not** close the plant side: `mkdir -m 700 -p` on a directory
that already exists (planted earlier, before this task ever ran, with
different ownership) does not change that directory's existing permissions
or owner — `mkdir` is a no-op on an existing path regardless of the mode you
asked for. `set -C` still refuses to *overwrite* a planted file inside it
rather than silently trusting it, but it can't tell a legitimate leftover
from this task's own earlier attempt apart from a hostile plant by content
alone. Verifying the directory's actual ownership before trusting it (`[ "$(stat -f%u
"$d" 2>/dev/null || stat -c%u "$d")" = "$(id -u)" ]`, platform-specific
`stat` flags) is the remaining one-line-ish check this pass didn't add;
treat that specific gap as accepted for now on a single-operator machine,
revisit if this skill is ever used somewhere that assumption doesn't hold.

Run this one snippet at the start of **every** command you issue for this
task — first call or later, doesn't matter, it produces the same session
name once the file exists and a fresh unique one if it doesn't. Note that
this means reusing the same label on a later, genuinely unrelated task
inherits whatever session was left in that file — the label is what has to
be distinct, the file itself will happily hand back stale content to anyone
who asks with a matching label (see the crash-retry note below for the one
case where that's exactly what you want). `AGENT_BROWSER_SESSION` is a
recognized env var
(verified equivalent to passing `--session` on every command), so once it's
exported, plain `agent-browser` commands use it with no per-command flag
needed:

```bash
agent-browser open https://example.com
```

**Close as your last action** when the task is done. Unlike the open/use
snippet, do **not** reuse the idempotent-create line here — if the state
file is already gone (a sibling cleaned up, a previous `close` in this same
task already ran, `$TMPDIR` got cleared), generating a brand-new name and
"closing" it exits 0 having closed nothing, while any real daemon under the
lost name keeps running unreclaimed until Rule 2's idle timeout — silently
reporting success on a leak instead of surfacing that something needs
attention:

```bash
raw='<your-task-label>'
label="$(printf '%s' "$raw" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_')_$(printf '%s' "$raw" | cksum | cut -d' ' -f1)"
d="$(git rev-parse --absolute-git-dir 2>/dev/null)" || d="${TMPDIR:-/tmp}/agent-browser-$(id -u)"
f="$d/agent-browser-session.$label"
s="$(cat "$f" 2>/dev/null)"
if [ -n "$s" ]; then
  export AGENT_BROWSER_SESSION="$s"
  agent-browser close && rm -f "$f"
else
  echo "agent-browser-concurrency: no session recorded for label '$label' — nothing to close (if a session for this task is genuinely still running, its name is already lost and this step can't reclaim it; Rule 2's idle timeout is the only remaining backstop)" >&2
fi
```

(`&&`, not two separate commands, inside the `if` — if `close` fails, keep
the file so the session name isn't lost for a retry; only delete it once
`close` actually succeeded. Read the file into `$s` once and branch on
*that*, rather than testing `[ -s "$f" ]` and then `cat`-ing it as two
separate steps — a sibling's `rm -f` landing between those two steps would
otherwise make `cat` fail after the test already passed, exporting an empty
`AGENT_BROWSER_SESSION` and sending `close` at the shared `default` session
instead of doing nothing. The `if` turning "nothing recorded" into a
reported, distinct case — instead of fabricating and discarding a fresh
session in its place — is what's new here; the empty-vs-missing distinction
above is what keeps that check itself race-free.)

Don't use a shell `trap` for this — a trap set in one Bash tool call fires at
the end of *that call*, not at the end of your task, and won't carry into a
later call either; just issue `close` explicitly, as shown, every time (never
abbreviate this to a bare `agent-browser close` without the snippet above it
— in a fresh call with nothing exported yet, that targets the shared
`default` session instead, the exact collision this skill exists to
prevent). Never run `close --all` — it closes **every** session in the
default context, including ones belonging to other concurrent agents.

**Retrying after your own task crashed**, rather than starting fresh: if the
state file from a previous attempt with the same label is still there, the
snippet above reuses its session name rather than generating a new one —
`close` it first (safe even if nothing is running: verified exit 0, no
error) in case that attempt's daemon is still wedged, then proceed. This is
only correct because the label is genuinely yours; don't apply it to a file
whose label you don't recognize as your own past attempt, since you can't
tell a crashed predecessor's leftover file from a live sibling's by looking
at it — closing the wrong one recreates the exact race this skill prevents.
Delete the file yourself first if you specifically want a fresh session
instead of resuming the old one.

**`--cdp <port>` / `--auto-connect` mode:** these attach a session to an
*already-running* Chrome instead of launching an isolated one, so multiple
sessions there share the same tabs and browser-level state regardless of
session naming. Add `--pin-tab` so your session sticks to the tab it opened
instead of another session's tab moving under it. These modes are also
exempt from Rule 2's *default* idle timeout (see below) — your explicit
`close` is the only cleanup path unless an idle timeout was explicitly
configured.

## Rule 2 — a leftover browser tree eventually cleans itself up, but don't count on it mid-task

`agent-browser` ≥0.33.1 auto-closes a daemon (and its Chrome process tree)
after an hour with no commands, as a backstop for a task that got killed or
crashed before its `close` ran. This *default* timeout does not apply to
headed browsers (including Safari/iOS WebDriver) or `--auto-connect`
sessions — see Rule 1's note above — and it doesn't apply retroactively to a
daemon spawned by an older binary before an upgrade. If you (or your
environment) *explicitly* set `--idle-timeout` or `AGENT_BROWSER_IDLE_TIMEOUT_MS`
instead of relying on the default, that applies to every browser, headed or
not — so a headed session with an explicit timeout set is not exempt the way
one relying on the default would be.

This is version/config information to be aware of, not an action for a single
task to take: don't upgrade the shared CLI or close sessions you didn't open
as part of your own task's work — either one risks a live sibling on a
machine other agents may be using. If the version matters for what you're
doing, note it in your report; a deliberate upgrade (and any cleanup of
daemons from before it) is a separate operator action, not this skill's
concern.

## Rule 3 — don't assume every transient error has the same cause

If `Not attached to an active page` happens against a session you generated
yourself (Rule 1), check first whether the page/tab moved on its own (a link
opened a new tab, a popup grabbed focus, a redirect swapped targets — use
`tab list` to see what's there) before concluding the session itself is gone.
If it's genuinely gone, `close` and start a fresh session rather than
retrying the same name blind.

`Resource temporarily unavailable` (`EAGAIN`) is a different signal:

- **A deliberately raised client timeout, not a hang.** If
  `AGENT_BROWSER_DEFAULT_TIMEOUT` is set above 30000 (30s), the CLI's own read
  timeout can expire before a slow-but-fine daemon response arrives,
  surfacing as `EAGAIN`. This is the CLI's documented, expected trade-off for
  that setting — it already retries automatically, just with a longer
  response time — so if this is set, no separate fix is needed. If it isn't
  set, this doesn't apply; check the next thing.
- **A real per-user process/thread ceiling**, possible on a machine that's
  accumulated many un-closed sessions:
  ```bash
  ps -u "$(whoami)" | wc -l      # current process count for your user
  ulimit -u                      # your process limit
  agent-browser session list     # sessions still open — close ones you opened (Rule 1)
  ```

If neither explains it, a short bounded retry (2–3 attempts, brief backoff) is
reasonable for a genuinely transient hiccup — but retrying immediately against
an actual ceiling just repeats the same failure.

## Why this isn't "just add a queue"

The instinct on hitting concurrent-access errors is to serialize everything
behind a queue. For `agent-browser` specifically that overcorrects: the tool
already ships per-task session isolation; the fix is generating a genuinely
unique name and closing what you open, not building new coordination on top.
If a workload genuinely needs to bound how many Chrome trees run at once (a
CI box fanning out many browser jobs), that's a concurrency cap on the
*number of jobs*, best enforced by whatever already schedules them — not
something each agent should invent its own counter for.

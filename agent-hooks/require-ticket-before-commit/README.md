# require-ticket-before-commit

**Point:** `pre-bash` · **Fail policy:** `open` (on crash) · **Priority:** 25 · **Default:** **block** (strict)

On a `git commit`, checks the commit message and branch name for a reference to a
tracking ticket. If none is found — and the commit isn't an exempt chore/WIP/merge,
and no one-time Telegram hatch is approved — it **blocks** by default (strict), with a
reminder that non-trivial changes should start from a ticket with acceptance criteria,
motivation, and user-impact. Set `REQUIRE_TICKET_STRICT=0` to fall back to warn-only.
Enforces the `strict-ticket-discipline` skill; pairs with task-cli.

The block message always leads with the stable marker `[require-ticket] BLOCKED: no
ticket reference` (exit code `10`), so an external check (e.g. a task-cli Docker test)
can assert on one fixed string.

## What the block tells the reader to do (agent-tools#994)

A block says WHAT TO DO NEXT as a short numbered ladder, and the ladder depends on who reads it
(`lib/agenttools_guard_message`; role from the bridge-trusted `args.agent_id` and the launcher env
`RIG_AGENT_ID` / `RIG_PARENT_AGENT_ID` / `RIG_DETACHED_AGENT` / `PI_SESSION_FILE`, never from
model-controlled tool args). Step 1 is always the self-service remedy: run
`task new --title … --acceptance … --why … --impact … --if-not-done …` (a gate may refuse once:
fix the flags, retry), then re-run the commit with `Refs #<id>` (never `Closes`/`Fixes`, per the
`task` skill), and do not stop.

- **Subagent:** if it genuinely cannot, `write agent://<parent>` (omp: `Main` for a depth-1 child,
  `Parent` for `Parent.Child`) with what is missing and what it tried, then `yield` status
  `awaiting-coordinator` (not blocked/done/failed) — a subagent cannot block-wait; the
  coordinator's `write agent://<you>` revives it. Never retry with `REQUIRE_TICKET_STRICT=0` /
  `RIG_HATCH_REQUEST_*`, never go to the human. Subagents are not shown the hatch line.
- **Coordinator:** run `task new` itself and relay the id to the blocked subagent with
  `write agent://<id>` then `wait`; the Telegram hatch is the LAST line, for genuine
  trivial-ticketless commits only.
- **Unknown role:** both branches spelled out. The builder is missing/unloadable → a built-in
  neutral two-branch message with the same marker.

## Ticket-detection heuristic

Detection is intentionally **broad**: a missed reference at worst nags (the default is
warn / fail-open), while a false positive only lets through a commit that almost
certainly had a ticket anyway. It scans the commit message *and* the branch name
(ticket ids are often encoded as `feature/ABC-12-foo`) for:

| Form                          | Example                                  | Tracker        |
| ----------------------------- | ---------------------------------------- | -------------- |
| `#NNN`                        | `Refs #123`                              | GitHub issue/PR |
| `org/repo#NNN`                | `alex-mextner/task-cli#4`                | GitHub (qualified) |
| `GH-NNN`                      | `GH-123`                                 | GitHub         |
| `KEY-NNN` (≥2 uppercase)      | `ENG-456`, `ABC-12`                      | Linear / Jira / task-cli |
| `task:…` / `task #…` / `T-NN` | `task:ABC-12`, `T-9`                     | task-cli       |
| trailer keyword + ref         | `Closes #12`, `Fixes ABC-3`, `Refs: T-9` | any            |
| full tracker URL              | `github.com/o/r/issues/12`, `linear.app/.../issue/…` | any |

The message is pulled from the argv (`-m`/`--message`, `-mMsg`, and the contents of any
`-F`/`--file` commit-message file — read from disk, separate `-F <path>`, glued `-F<path>`,
and `--file=<path>` forms all), so a ticket id in any of those counts. **Clustered short
flags are de-clustered the way git parses them** (agent-tools#109): `git commit -am "…"` is
`-a` + `-m "…"`, and the message reader honors that — the first value letter (`m`/`F`) in a
short group wins, consuming the rest-of-cluster as a glued value (`-amMSG` → message `MSG`;
`-amF` → message `F`, *not* a separate `-F`; `-aFpath` → file `path`) or, when it's the
cluster's last char, the next token (`-am MSG`, `-aF file`). Before the fix a `-am "Closes #5"`
was read as an empty message and false-BLOCKED, and a `-am "chore: …"` lost its exemption. A
relative `-F` path
is resolved against the event's `cwd` (the command's working directory), so it works even when
the harness launches the hook outside the repo. A trailer keyword (`Closes`/`Fixes`/`Refs`)
only counts when it's followed by a real ref — `fix: null deref` is a bugfix subject, not a
reference, so in strict mode it correctly still needs a ticket.

**`-F -` (message on stdin) — fails CLOSED with a hint (agent-tools#104, reversing #102's
fail-open).** `git commit -F -` reads the message from *git's own stdin*, which a PreToolUse hook
**cannot** read (the hook's stdin is the JSON event, not git's). The gate genuinely can't
ticket-check an unreadable message — and silently allowing it made `-F -` a free bypass of the
whole ticket requirement, contradicting the strict default. So a `-F -` commit with **no ticket in
the branch name either** now **blocks** (exit 10, the stable marker) with an actionable hint.
Because the branch name is still readable, a `-F -` commit on a ticket-encoded branch
(`feature/ABC-12-foo`) **passes** — same as the editor-commit path (a `git commit` with no `-m`/`-F`
is likewise unreadable pre-commit and is checked against the branch only). To satisfy the gate, put
the ticket somewhere readable: pass the message via `-m "…Refs #123…"` or `-F <file>` (both read
and checked), or encode it in the branch name. The **Telegram hatch is consulted BEFORE the `-F -`
block**, so an approved `RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT` allows a genuinely
ticketless `-F -` commit. `-F <file>` is read and checked normally; only the `-` (stdin) form
blocks. `REQUIRE_TICKET_STRICT=0` downgrades this `-F -` block to a warn-only allow, like every
other no-ticket case.

## What counts as a `git commit` (argv-scoped detection)

The gate fires **only** on a real `git commit` invocation, decided by **parsing** the
command into argv — never by raw-string matching the words "git"/"commit". The command is
tokenized (quote-, comment-, separator- and multi-line-aware), each shell segment has its
leading shell noise / redirects / inline env / wrapper executables peeled, and a segment is
gated only when its executable is `git` and its subcommand is exactly `commit`. So a benign
command that merely *mentions* those words is **not** gated — `gh issue create --body
"…git commit…"`, `echo "git commit"`, `git log --grep=commit`, `git config commit.gpgsign`,
`git help commit`, and `git commit-graph write` all pass through (agent-tools#97; the same
class of over-match `block-no-verify` fixed in #59). A real commit behind a wrapper —
`env FOO=bar git commit`, `sudo git commit`, `sudo -u git git commit`,
`runuser -u git -- git commit`, `timeout 60 git commit`, `/usr/bin/git commit` — **is** still
gated. An *unlisted* wrapper (`unshare`, `bash -c '…'`) or an unparseable command is not gated
— a documented best-effort limitation, consistent with `on_error: open` (process discipline,
not a security boundary).

## Repos that keep tracker ids out of commit messages

A repo mirrored to a public one may forbid tracker ids in commit messages (Conloca's mirror leak
guard fails any commit to a public-mirrored file whose message names one). There the ticket goes in
the **branch name only** (`fix/GL-315-ticket-in-branch`) and the message carries none: the gate
reads the branch, so the commit passes. Write the key in capitals — the branch is matched with the
same `KEY-NUM` pattern as the message, so `fix/gl-315-…` is not a ticket (a lowercase match would
also hit version segments like `cli-0-12-2`). The pull request is not consulted: it usually does not
exist yet when the first commit is made.

## Exemptions (no ticket expected)

These commits skip the gate by default:

- conventional-commit chore types: `chore:`, `docs:`, `style:`, `ci:`, `build:`,
  `test:`, `revert:`
- WIP / fixup markers: `wip…`, `fixup!`, `squash!`, `amend!`, `merge…`, `revert…`
- `git commit --amend/--continue/--abort/--skip` (not authoring a fresh change)

## No self-service bypass — request a Telegram approval instead

There is **no** per-commit env var or message trailer an agent can set on its own command to
skip this gate (a self-grant is security theater). For a genuinely ticketless one-off: use a
`chore:`/`docs:` type if it is truly trivial, or (top-level agent only) request a **one-time
Telegram approval**; a subagent routes through its coordinator instead (see above):

```bash
RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="one-off backfill, no ticket warranted" git commit -m "feat: x"
```

This is a **pre-bash** hook, so the inline prefix is honored: the hook parses the leading
`RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT=…` assignment out of the command string the event
carries (a pre-bash hook runs in its own process *before* the shell evaluates the `VAR=x cmd`
prefix, so the value never reaches its `os.environ`). It also works when the commit is not the
first command on the line (`cd repo && RIG_HATCH_REQUEST_…="why" git commit …`). Exporting the var
into the harness environment works too and takes precedence over an inline value.

The request routes to the human over Telegram (`tg-ctl ask`) and the commit is allowed **only**
on their approval. It is **deny-by-default**: a blank value or a bare `1`/`true` (no real
justification) is rejected without sending the message, and any nonzero/timeout/error verdict
denies. This replaces the old `[skip-ticket: <reason>]` trailer and inline `REQUIRE_TICKET_SKIP=1`
self-service escapes (both removed). The repo-level `REQUIRE_TICKET_STRICT=0` warn-only dial is
kept (a rollout policy knob, not a per-command grant).

## Configure (env)

- `REQUIRE_TICKET_STRICT=0` — opt the whole gate back to **warn-only** (default: strict
  block). Any other value, including unset, is strict. This is a **repo-level rollout dial**,
  not a per-command self-grant.
- `RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="<justification>"` — request a one-time
  Telegram approval for a genuinely ticketless commit (deny-by-default; bare `1` rejected).
- `REQUIRE_TICKET_EXEMPT_TYPES="chore,docs"` — override the exempt commit-type set
  (comma-separated; replaces the default list).

## Relationship to the five ticket-documentation rules

The `strict-ticket-discipline` skill defines five documentation rules. This
hook owns exactly **one half of one of them**; the rest are not reachable from a
`pre-bash` commit gate. Stated plainly so nobody expects this hook to do more than it can:

- **Rule 1 (every related entity is a LINK) — commit-side only, and already covered.**
  This hook enforces the part of rule 1 that *is* visible at commit time: a non-chore
  commit must carry at least one resolvable ticket/PR reference. It deliberately accepts
  the **bare canonical refs** (`#123`, `ENG-456`, `org/repo#12`, a full URL) and does
  **not** demand a literal hyperlink in the commit subject. The commit-side and body-side
  halves of rule 1 differ because of the *medium*, not auto-linking: a commit subject is
  plain text by git convention, where a markdown hyperlink is non-idiomatic and a bare URL
  is noise, so the canonical short ref *is* the accepted linkable form (the platform
  resolves it on render). A ticket body is authored rich text, where a real link is both
  expected and a single keystroke away — so there a bare id is a dead end, and rule 1's
  *in-body* form (below) demands an actual link. Forcing a full URL into every commit
  subject would fight git convention and override the chore exemptions for zero added
  clickability — over-reach.
- **Rules 2–5 are CLI-side, by construction.** A `pre-bash` hook sees only the commit
  *command* (its message and the branch name). It has **no access to the ticket body** —
  it cannot read acceptance-criterion checkboxes, the proofs attached to them, the
  criterion count, or the user-impact prose. So rule 2 (no close with an unchecked box),
  rule 3 (no checked box without a visual proof), rule 4 (≥2 acceptance criteria), rule 5
  (plain-language user-impact) — and rule 1's in-body link rendering — belong to the
  **ticket CLI (task-cli)**, which checks them at ticket create / update / close / check
  time where the structured body is actually available. Those CLI checks are the companion
  change to the skill; until task-cli is wired (and on any harness without it) the skill
  carries the discipline. This hook is just the commit-time tripwire that a change
  references a ticket at all.

## Why an agent-hook

The check has to happen *before* the commit, mid-session, while you can still add the
reference. The gate **blocks** a ticketless non-chore commit by default — but it stays
`on_error: open`, so a *crash* in the check (not a missing ticket) still fails open and
never makes committing impossible. A genuinely ticketless commit has a sanctioned,
human-in-the-loop out (a `chore:`/`docs:` type when trivial, or a one-time Telegram approval
via `RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT`), so strict-by-default nudges the
discipline without wedging legitimate work — and there is no env/trailer an agent can grant
itself. For a hard CI backstop, pair with an optional `ci/ticket-required` gate.

## Install

```bash
chmod +x require_ticket_before_commit.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory.
```

## Test

```bash
chmod +x require_ticket_before_commit.py

# no ticket → BLOCK by default (exit 10), message leads with the stable marker
echo '{"args":{"command":"git commit -m \"feat: add export\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block","message":"[require-ticket] BLOCKED: ..."}  exit=10

# ticket present → clean allow
echo '{"args":{"command":"git commit -m \"feat: add export (Refs #123)\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

# removed self-service escapes no longer bypass — these now BLOCK (exit 10)
echo '{"args":{"command":"git commit -m \"feat: x [skip-ticket: one-off backfill]\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block", ...}  exit=10
echo '{"args":{"command":"REQUIRE_TICKET_SKIP=1 git commit -m \"feat: x\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block", ...}  exit=10
# for a genuine one-off, request a Telegram approval instead:
#   RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="<justification>" git commit …   # allows on human tap

# warn-only opt-out, no ticket → allow with advisory
REQUIRE_TICKET_STRICT=0 sh -c 'echo "{\"args\":{\"command\":\"git commit -m x\"}}" | ./require_ticket_before_commit.py'; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow", ...}  exit=0

# exempt chore type → clean allow even without a ticket
echo '{"args":{"command":"git commit -m \"chore: bump lockfile\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

Run the unit tests with `python3 test_require_ticket.py` (stdlib `unittest`, no deps).

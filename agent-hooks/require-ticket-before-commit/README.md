# require-ticket-before-commit

**Point:** `pre-bash` · **Fail policy:** `open` (on crash) · **Priority:** 25 · **Default:** **block** (strict)

On a `git commit`, checks the commit message and branch name for a reference to a
tracking ticket. If none is found — and the commit isn't an exempt chore/WIP/merge,
and no per-commit escape is present — it **blocks** by default (strict), with a
reminder that non-trivial changes should start from a ticket with acceptance criteria,
motivation, and user-impact. Set `REQUIRE_TICKET_STRICT=0` to fall back to warn-only.
Enforces the `strict-ticket-discipline` skill; pairs with task-cli.

The block message always leads with the stable marker `[require-ticket] BLOCKED: no
ticket reference` (exit code `10`), so an external check (e.g. a task-cli Docker test)
can assert on one fixed string.

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
the ticket somewhere readable: pass the message via `-m "…Closes #123…"` or `-F <file>` (both read
and checked), or encode it in the branch name. The deliberate escape for a *stdin*-message commit is
`REQUIRE_TICKET_SKIP=1 git commit …` — read from the command, not stdin; a `[skip-ticket: <reason>]`
**trailer does not work for `-F -`** (it would live in the unreadable stdin message). `-F <file>` is
read and checked normally; only the `-` (stdin) form blocks. `REQUIRE_TICKET_STRICT=0` downgrades
this `-F -` block to a warn-only allow, like every other no-ticket case.

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

## Exemptions (no ticket expected)

These commits skip the gate by default:

- conventional-commit chore types: `chore:`, `docs:`, `style:`, `ci:`, `build:`,
  `test:`, `revert:`
- WIP / fixup markers: `wip…`, `fixup!`, `squash!`, `amend!`, `merge…`, `revert…`
- `git commit --amend/--continue/--abort/--skip` (not authoring a fresh change)

## Per-commit escapes (the deliberate, documented bypass)

For the rare legitimate ticketless commit, escape a single commit with this hook's own
ticket-specific escape:

- a `[skip-ticket: <reason>]` trailer in the commit message (the reason is mandatory), or
- an inline env on the command: `REQUIRE_TICKET_SKIP=1 git commit …`.

The inline escape is scoped to the `git commit` segment, so an assignment on a sibling
command (`REQUIRE_TICKET_SKIP=1 echo x; git commit …`) does **not** bypass the gate.

## Configure (env)

- `REQUIRE_TICKET_STRICT=0` — opt the whole gate back to **warn-only** (default: strict
  block). Any other value, including unset, is strict.
- `REQUIRE_TICKET_SKIP=1` (inline on the commit command) — skip THIS commit's check.
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
never makes committing impossible. A no-ticket commit has clear, cheap escapes (a
`[skip-ticket: <reason>]` trailer or `REQUIRE_TICKET_SKIP=1`), so strict-by-default
nudges the discipline without wedging legitimate work. For a hard CI backstop, pair with
an optional `ci/ticket-required` gate.

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
echo '{"args":{"command":"git commit -m \"feat: add export (Closes #123)\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

# `[skip-ticket: reason]` escape → allow
echo '{"args":{"command":"git commit -m \"feat: x [skip-ticket: one-off backfill]\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

# inline REQUIRE_TICKET_SKIP=1 escape → allow
echo '{"args":{"command":"REQUIRE_TICKET_SKIP=1 git commit -m \"feat: x\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

# warn-only opt-out, no ticket → allow with advisory
REQUIRE_TICKET_STRICT=0 sh -c 'echo "{\"args\":{\"command\":\"git commit -m x\"}}" | ./require_ticket_before_commit.py'; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow", ...}  exit=0

# exempt chore type → clean allow even without a ticket
echo '{"args":{"command":"git commit -m \"chore: bump lockfile\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

Run the unit tests with `python3 test_require_ticket.py` (stdlib `unittest`, no deps).

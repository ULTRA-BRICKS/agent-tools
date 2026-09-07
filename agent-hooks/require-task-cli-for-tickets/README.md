# require-task-cli-for-tickets

**Point:** `pre-bash` · **Fail policy:** `open` (on crash) · **Priority:** 26 · **Default:** **block** (strict)

task-cli (`task`, `~/xp/task-cli`) is the ecosystem's **enforced ticket-creation interface**:
every ticket it creates carries acceptance criteria, motivation, user-impact, and (for UI
changes) screenshots — gates defined in `task.yaml`'s `enforce:` block. Creating or mutating a
ticket **directly**, bypassing task-cli, skips every one of those gates. This hook was written
after an `omp` session created a Linear issue directly instead of going through `task new`.

On a `gh issue <mutating-subcommand>` or a `gh api` call to a `repos/<owner>/<repo>/issues…` REST
path with a mutating signal — and only when task-cli is actually usable here (see below) — it
**blocks** by default (strict) with a reminder to use `task new`/`task classify` instead. Set
`REQUIRE_TASK_CLI_STRICT=0` to fall back to warn-only.

The block message always leads with the stable marker `[require-task-cli] BLOCKED: direct
ticket mutation`, so an external check can assert on one fixed string.

## What's detected

`gh issue`, argv-scoped (same parsing discipline as `require-ticket-before-commit` /
`block-no-verify`: the command is tokenized, wrappers/env/redirects peeled, and a segment is
inspected only when its executable is `gh` — never a raw string match on "gh"/"issue", so a
command that merely *mentions* those words is not gated):

| Subcommand | Gated? |
| --- | --- |
| `create` (and its gh-documented alias `new`), `edit`, `close`, `reopen`, `comment`, `delete`, `transfer`, `lock`, `unlock`, `pin`, `unpin` | **yes** — creates or mutates a ticket |
| `view`, `list`, `status` | no — read-only |

`gh api`, scoped to a GitHub REST path that addresses an issue or one of its sub-resources —
`repos/<owner>/<repo>/issues`, optionally `/<number>` and ONE trailing sub-resource segment
(`comments`, `labels`, `assignees`, `lock`, `reactions`, `sub_issues`, …) optionally followed by
its own identifier (e.g. `issues/42/labels/bug`, `issues/42/sub_issues/99`) — **or** the concrete
per-comment route `repos/<owner>/<repo>/issues/comments/<comment_id>` (which does not nest under
an issue number at all). The sub-resource match is shape-based, not an enumerated allowlist —
GitHub ships more issue-mutating sub-resources than the original four (`reactions`, `sub_issues`,
…) and a closed list is a permanent game of whack-a-mole against GitHub's own API surface;
matching the shape is safe because the path alone never gates anything by itself — **and** a
mutating signal is still required on top of it: an explicit `-X`/`--method` of `POST`/`PATCH`/
`PUT`/`DELETE`, or — with no explicit method — any `-f`/`-F`/`--field`/`--raw-field`/`--input`
(which makes `gh api` default to `POST`), in either the separate-token form (`-f title=x`) or
attached (`-ftitle=x`, `--field=title=x`). A plain `gh api repos/o/r/issues` with no method/fields
is `gh api`'s own GET default — a read — and is not gated, matching the `gh issue view`/`list`
exclusion (nor is a GET to an unlisted sub-resource like `issues/42/timeline`). An explicit
`--method`/`-X` always wins over field-flag inference: `-X GET -f state=open` is a read (gh's own
docs: `--method GET` sends fields as a query string, not a body). `-X` itself is recognized in
its separate-token (`-X POST`), compact (`-XPOST`), `=`-attached (`-X=POST`), and long
`--method=POST` forms — all verified live against the installed gh. `gh`'s own global flags
(`-R`/`--repo`/`--hostname`) are recognized as taking a separate value in EVERY position they can
appear — before `api`, and after it too (`gh api --hostname ghe.example repos/o/r/issues -X
POST`) — so the flag's value is never mistaken for the endpoint path. This closes the `gh issue
create` → `gh api repos/o/r/issues -f title=...` evasion without touching the many other
legitimate uses of `gh api` (PRs, releases, workflow runs, …).

`gh -R owner/repo issue create …` / `gh --repo owner/repo issue create …` (the `-R`/`--repo`/
`--hostname` global flags) are walked past, same as `git -C` in the commit-gate — in either
position, BEFORE `issue` or AFTER it (`gh issue -R owner/repo create …`, Cobra persistent flags
are valid in both spots). A wrapped invocation (`env FOO=bar gh issue create …`, `sudo gh issue
create …`, `timeout 60 gh issue create …`) is still gated — the same wrapper-peeling
`require-ticket-before-commit` uses.

## When does the gate actually fire — `task_cli_active(cwd)`

The gate only fires when task-cli is a **real, usable alternative right now**:

1. The `task` binary resolves on `PATH` (`shutil.which("task")`). A machine that never
   provisioned task-cli (via rig's `tools.items.task`) is never blocked with nothing to point at
   instead — this is the same "don't demand a tool that isn't there" posture the rest of this
   family takes with its own env dials.
2. The command's `cwd` is inside a git repository (`git rev-parse --is-inside-work-tree`).
   task-cli's default backend is **GitHub Issues** and it falls through to that cleanly with **no
   `rig.yaml` `task:` block at all** (task-cli's own README: *"a repo with no `task:` block (or
   no `rig.yaml`) falls through cleanly to `github-issues`"*) — so "task binary present + inside a
   git repo" already means task-cli is usable here. There is deliberately **no** separate
   `rig.yaml` `tools.items.task` / `task:`-block read in this hook: task-cli's own config cascade
   already makes that check redundant (any git repo qualifies once the binary is on PATH), and
   duplicating task-cli's cascade resolution here — including its own YAML nesting rules — would
   be a second, divergence-prone copy of logic task-cli already owns. (`tools.items.task` in
   `~/.config/rig/config.yaml` is in any case a **global**, machine-wide provisioning marker, not
   a per-repo signal — rig-cli's own `rig.yaml` carries no `tools:` block at all, confirming
   `tools.items` is resolved at the global config layer.)
3. That resolved `task` binary actually **identifies as task-cli**. `task` is a famously
   ambiguous name — Taskwarrior and go-task both install a `task` on PATH too — so `shutil.which`
   resolving *something* is not proof it resolved task-cli. `_task_binary_is_task_cli` runs
   `task --help` (read-only, no side effects) and checks for task-cli's own description string.
   Mirrors the exact collision guard ship.sh's own acceptance gate already has
   (`ci/ship/ship.sh`'s `_acceptance_gate_looks_like_task_cli_json`, agent-tools#521): trust
   neither PATH resolution nor a zero exit code as identity, only the tool's own output.

All three checks fail OPEN on error (a `git`/`task` invocation error, a broken `PATH`) — this
hook nags, it doesn't wedge a machine that can't answer the question.

## What's deliberately NOT covered

- **A nested shell interpreter** (`bash -c 'gh issue create --title x'`, `sh -c …`) or a command
  substitution (`echo "$(gh issue create --title x)"`) — same documented limitation as the rest
  of this hook family (`block-no-verify`, `block-devserver-primary`, `block-reset-hard`,
  `pkill-guard`). Detection here is argv-scoped: only the top-level command segment is tokenized
  and checked for a `gh` executable; a `gh` invocation buried inside another interpreter's
  string argument is not re-parsed. `no-shell-file-edit` is the one sibling that DOES unwrap
  `bash -c`/`sh -c` (up to a depth cap) because its own domain — file edits — makes that
  worthwhile; adding the same recursive unwrapping here is a real, separate feature, not a
  quick fix, and is tracked as a follow-up rather than folded into this pass.
- **A Linear MCP tool call** (e.g. a `mcp__…__save_issue`/`mcp__…__create_issue`-shaped tool
  invocation through a Linear MCP server) — the actual incident that prompted this hook. There is
  **no `pre-mcp-tool`-style hook point in this family yet**: `agent-hooks/README.md`'s point table
  lists `pre-agent`/`pre-bash`/`pre-write`/`pre-skill`/`pre-monitor`/`post-write`/`stop` — nothing
  that fires on an arbitrary MCP tool call by name. Building one would mean plumbing a new event
  through `lib/cc_hook_bridge`, `lib/codex_hook_bridge`, and `lib/opencode_hook_bridge` (three
  bridges, each with its own tool-name surface) — real, separate infrastructure work, not
  something a single new hook script can add. Tracked as a follow-up; until it lands, a harness
  with Linear MCP tools configured can still create a Linear ticket directly through them and
  this hook will not see it.
- **A raw Linear GraphQL API call** (`curl`/`httpie`/a script hitting `api.linear.app`). Left out
  on purpose, not merely deferred: Linear's API is a **single GraphQL endpoint for both reads and
  mutations** — there is no URL-path or HTTP-method split like GitHub's REST API gives `gh api`
  above. A Bash-level heuristic here would have to sniff the *body* for a `mutation` keyword
  (fragile against a file-based `-d @payload.json`, a base64/templated body, or any non-`curl`
  client — `requests`, a Node script, task-cli's own daemon) or fall back to gating every POST to
  `api.linear.app`, which would also block legitimate reads (Linear's API answers GraphQL
  *queries* over the same POST). That trade was judged worse than not gating it at all for a v1:
  either it silently misses the real bypass (a scripted client, a non-inline body) or it blocks
  correct read traffic. If this becomes a real incident vector, the MCP-tool hook point above is
  the right fix — it catches the call at the tool-invocation layer, before it becomes any
  particular wire protocol, rather than trying to pattern-match GraphQL bodies in a shell string.
- **`gh issue` subcommands not in the mutating set** by design match `gh issue view/list/status`
  as read-only, per the task's own explicit ask.

## No self-service bypass — request a Telegram approval instead

Same deny-by-default pattern as `require-ticket-before-commit`: no per-command env var or
argument an agent can set on its own command to skip this gate. For a genuine one-off, **ASK the
human**, or request a **one-time Telegram approval**:

```bash
RIG_HATCH_REQUEST_REQUIRE_TASK_CLI_FOR_TICKETS="one-off: migrating an old issue thread" gh issue edit 42 --title "..."
```

This is a **pre-bash** hook, so the inline prefix is honored (the hook parses the leading
`RIG_HATCH_REQUEST_REQUIRE_TASK_CLI_FOR_TICKETS=…` assignment out of the command string before
the shell would have evaluated it — same mechanism `require-ticket-before-commit` documents).
Exporting the var into the harness environment works too and takes precedence over an inline
value.

The request routes to the human over Telegram (`tg-ctl ask`) and the command is allowed **only**
on their approval. Deny-by-default: a blank value or a bare `1`/`true` (no real justification) is
rejected without sending the message, and any nonzero/timeout/error verdict denies.

## No harness exemptions

Like every hook in this family, detection is by parsed Bash argv only — there is no branch on
`event["harness"]` (claude-code / codex / opencode / omp / pi all see the identical gate, per the
`no-harness-exemptions-in-hooks` doctrine already established for this ecosystem). The trusted
identity used for the Telegram hatch is the same shared `agenttools_hatch_escalation` mechanism
every other hatch-using hook relies on (a hardcoded `tg-ctl` path list, never a harness-supplied
value).

## Configure (env)

- `REQUIRE_TASK_CLI_STRICT=0` — opt the whole gate back to **warn-only** (default: strict block).
  Any other value, including unset, is strict. Repo-level rollout dial, not a per-command grant.
- `RIG_HATCH_REQUEST_REQUIRE_TASK_CLI_FOR_TICKETS="<justification>"` — request a one-time
  Telegram approval for a genuine one-off (deny-by-default; bare `1` rejected).

## Relationship to `require-ticket-before-commit`

Two halves of the same discipline, gating opposite ends of the ticket lifecycle:

- `require-ticket-before-commit` (commit-side): a non-chore `git commit` must **reference** a
  ticket that already exists somewhere.
- `require-task-cli-for-tickets` (ticket-side, this hook): the ticket itself must be **created or
  mutated** through the enforced CLI, not by hand via `gh issue`/`gh api`.

Neither hook can substitute for the other: a commit can reference a ticket that was filed
correctly through task-cli (this hook doesn't need to fire again at commit time), and a ticket
created correctly through task-cli still needs its commit-side reference (that's the other
hook's job).

## Install

```bash
chmod +x require_task_cli_for_tickets.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory.
```

## Test

```bash
chmod +x require_task_cli_for_tickets.py

# gh issue create, task-cli active → BLOCK by default (exit 10), message leads with the stable marker
echo '{"args":{"command":"gh issue create --title x --body y"},"cwd":"/some/git/repo"}' | ./require_task_cli_for_tickets.py; echo " exit=$?"

# gh issue view → always allowed (read-only)
echo '{"args":{"command":"gh issue view 42"},"cwd":"/some/git/repo"}' | ./require_task_cli_for_tickets.py; echo " exit=$?"

# warn-only opt-out
REQUIRE_TASK_CLI_STRICT=0 sh -c 'echo "{\"args\":{\"command\":\"gh issue create --title x\"},\"cwd\":\"/some/git/repo\"}" | ./require_task_cli_for_tickets.py'; echo " exit=$?"
```

Run the unit tests with `python3 test_require_task_cli.py` (stdlib `unittest`, no deps — named
differently from the `tests/` mirror below so `pytest -q` from the repo root, which has no
per-directory pytest config, doesn't collide on two same-named test modules) — mirrored for
`pytest tests/` collection at `tests/test_require_task_cli_for_tickets.py` in the repo root.

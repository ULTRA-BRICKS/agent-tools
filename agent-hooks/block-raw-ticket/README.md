# block-raw-ticket

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

When **task-cli is configured** (`task` on PATH *and* the binary is actually task-cli —
the file contains `tasklib`, so Taskwarrior/go-task do not arm the gate), this hook blocks a
raw issue-tracker write that skips task-cli's gates.

> **Activation note.** An installed descriptor points straight at this script in the primary
> agent-tools checkout. Merging a change here activates it on every machine that has the hook
> installed — there is no separate install step to hold it back.

## What is blocked

**Tracker CLIs**

- `gh issue create` / `new` / `close` / `edit` / `comment` / `delete` / … (incl. `gh -R o/r …`)
- `linear issue create` / `update` / `delete` / …
- Forgejo `fj issue create` / `edit` / `comment` / `assign` / `unassign` / `close`, and
  `fj repo labels [REPO] create` / `delete` / `edit`. fj's value flags (`-H/--host`,
  `-C/--cwd`, `-R/--remote`, `-r/--repo`, `--body`, `--body-file`, `--template`,
  `-w/--with-msg`) are skipped in any order. **Verified** against the installed `fj --help`
  output (2026-09-22).
- Gitea `tea issues` (aliases `issue`, `i`) `create` / `close` / `reopen` / `edit`, top-level
  `tea comment`, and `tea label(s) create` / `delete` / `update`. **Unverified** — `tea` was
  not installed where this was written; the shape comes from Gitea's published tea docs.

**Raw REST writes** to a tracker endpoint — `[api/v1/|api/v3/]repos/<owner>/<repo>/issues[...]`
or `.../labels[...]` (Forgejo/Gitea, GitHub Enterprise, github.com, or a proxy that strips the
prefix; URL-decoded up to three times, so `repo%2Fissues` still matches, and path-normalized
the way the clients themselves do, so `repo/./issues`, `repo/x/../issues` and `repo//issues`
still match). Only the URL is checked, never a body value. The request method is resolved per
client, and an **explicit known method always wins**. An explicit method the hook cannot
resolve to a known verb (`-X$'POST'`, `-X "$M"`, `http $'POST' …`) counts as a write — it must
not switch the implied-write check off:

| Client | Explicit method | Implied write when no explicit method |
| --- | --- | --- |
| `curl` | `-X`/`--request`, incl. glued/grouped `-XPOST`, `-sXPOST` | `-T`/`--upload-file` → PUT; `-d`/`--data*`/`--json`/`-F`/`--form` (incl. `-dJSON`) → POST unless `-G`; `-I` → HEAD only when no body flag is present (`curl -I -d …` is a write) |
| `wget` | `--method POST` / `--method=POST` | `--post-data`/`--post-file` → POST; `--body-data`/`--body-file` → POST (conservative) |
| HTTPie (`http`, `https`, `xh`) | leading `METHOD` word (any case) | any body item (`f=v`, `f:=json`, `f@file`, `f=@file`) or `--raw` → POST; `--form`, `--download`, `--ignore-stdin` alone stay GET |
| `gh api` | `-X`/`--method`, incl. `-XPOST`, `-iX POST` | `--input` or any `-f`/`-F`/`--field`/`--raw-field` → POST |

curl's `--next` / `-:` split one invocation into independent requests; each is judged on its
own, so a `-X GET` on the first cannot cover a write in the second.

`gh api` is gated on **any host**: relative endpoints (`repos/o/r/issues`), absolute URLs, and
`--hostname`/`GH_HOST=` overrides alike. That includes github.com — the shared requirement is
API parity with the `gh issue` rule, so a GitHub REST issue write via `gh api` is blocked too.
`gh api graphql` is blocked when its `query=` field carries an issue/label mutation
(`createIssue`, `updateIssue`, `closeIssue`, `reopenIssue`, `deleteIssue`, `transferIssue`,
`pinIssue`, `unpinIssue`, `createLabel`, `updateLabel`, `deleteLabel`).

**Inline programs.** `python`/`python3 -c …` (incl. `-Ic`), `node`/`bun -e|-p|--eval|--print …`,
a here-string (`python3 <<< '…'`), or a heredoc fed to the interpreter (`python3 - <<'EOF'`,
`node <<EOF`) whose code **both** references a tracker path in a string **and** makes a write
call **in code**: `requests|httpx|axios|session|client|s.post/patch/put/delete(…)`,
`X().post(…)`, `.request("POST", …)`, a `method: 'POST'` / `method='POST'` /
`"method": "POST"` literal (covers `fetch`, node `http.request`), or a
`urllib.request.Request(url, data)` / `urlopen(url, data)` with a body and no explicit read
method. String literals are masked before call detection, so `print("requests.post(…)")` and
`console.log("… method: 'POST' …")` are inert text and stay allowed.

**Wrappers.** Any of the above inside `sh`/`bash`/`zsh`/`dash`/`ksh -c` (incl. `-lc`), `eval`,
or a shell heredoc (`bash <<'EOF'`, `sh -s <<EOF`) is re-scanned recursively. Chains (`&&`,
`||`, `;`, `|`, newline), subshells/groups, the exec wrappers `timeout`, `env`, `nice`, `time`,
`stdbuf`, `nohup`, `setsid`, `unbuffer`, `command`, `sudo`, `doas`, `xargs`, and the project
runners `uv`/`poetry`/`pipenv`/`pdm`/`hatch`/`rye run …` are peeled. Versioned interpreters
(`python3.11`, `pypy3`) count as Python.

## What stays allowed

- `task …` (the sanctioned path), `--help`/`-h` on `fj`/`tea`
- reads: `gh issue list`/`view`, `fj issue view`/`search`/`browse`/`templates`,
  `fj repo labels view`, `tea issues`/`issues list`, and any GET/HEAD on a tracker endpoint
  (including `curl -X GET -d …`, `curl -G -d …`, `http GET url field=x`, `wget --method=GET`)
- PR operations (`gh pr …`, `fj pr …`, `tea pulls …`, and REST/`gh api` calls on `/pulls`)
- prose and data: `echo "fj issue create …"`, commit messages, `cat <<EOF` notes whose lines
  are not themselves tracker commands (a data heredoc's body lines are still scanned as shell
  lines, so `cat > import.sh <<'EOF'` containing `fj issue create …` is over-blocked — the
  pre-existing behavior for `gh issue`, kept fail-closed)

Inert when `task` is **not** on PATH (task-cli is not activated).

Independent of that arming: a ticket **create** whose title matches
`Review uncommitted` is **always** blocked (`gh issue create`/`new`,
`linear issue create`/`new`, `task new`/`create`, `fj issue create <TITLE>`,
`tea issues create --title …`). That title must never become a ticket (ask 041). `gh pr` is
not a ticket and is left alone. `echo "Review uncommitted …"` is left alone. That check is
categorical and has no hatch — and, when task-cli is armed, any blocked tracker write whose
text contains `Review uncommitted` (a wrapped `bash -c '… create …'`, a REST body title) is
refused outright instead of being offered to the hatch.

## What the block tells the reader to do (agent-tools#994)

The block is a short numbered, role-aware ladder built by `lib/agenttools_guard_message` (same
detection as `require-ticket-before-commit`): step 1 redoes the write with
`task new --title … --acceptance … --why … --impact … --if-not-done …` (or `task change` /
`task done`); a **subagent** that cannot is told to `write agent://<parent>` and `yield` status
`awaiting-coordinator` (never the human, never a hatch/strict retry, and is not shown the hatch
line); the **coordinator** relays the result to the blocked subagent and sees the hatch as the LAST
line; an unknown role gets both branches. The builder missing → built-in neutral text.

## Escape hatch (deny-by-default)

No self-service bypass. A genuine one-time exception is requested with
`RIG_HATCH_REQUEST_BLOCK_RAW_TICKET="<written justification>"` — as a leading inline assignment
on the gated command or a process-env export — which routes **one** Telegram approval through
the shared `agenttools_hatch_escalation` helper (same mechanism as `block-raw-worktree-add` /
`block-raw-pr-merge`). One round-trip covers the whole command, however many mutators it
chains. A blank, bare `1`/`true`/`yes`/`on`, or short command-shaped value (`pytest`) is rejected
without a Telegram call. Only an explicit approval allows; a declined, expired/unanswered, or
unavailable helper denies. No subagent exemption. `timeout_ms` is `960000` (the helper's 930s
worst case plus margin); a `cc_hook_bridge` settings entry must set its own timeout above that.
The descriptor has one timeout for every invocation, so this ceiling also covers the hot path
that runs on every Bash call: a hook bug that hangs would stall a command up to 16 minutes
before `on_error: closed` denies it (was 5s). That is the same accepted trade-off
`block-raw-pr-merge` and `block-raw-worktree-add` make — the non-hatch path returns in
milliseconds and reads no stdin beyond the event.

Raw API commands often carry credentials, so the Telegram preview and the `overrides.log` audit
line show the command with credential **values** masked as `[REDACTED]` — Authorization/token
headers, `curl -u`/`--user`/HTTPie `-a` passwords, URL userinfo, `?token=`-style query values,
`--token`-style flags, JSON credential fields, and `*_TOKEN=`/`*_SECRET=`-style inline
assignments (`redact_for_display` in `lib/agenttools_hatch_escalation`). Redaction is
display-only: the approval fingerprint and the inline-hatch parser run on the original command
and cwd, so two commands that differ only in a secret render the same preview but can never
share an approval. Redaction is pattern-based; a credential in an unrecognized shape (a bare
positional token, an unusual header name) can still appear in the preview.

For legitimate issue comments, use `task comment <id> <text>` or
`task comment <id> --body-file <file>` through the repository's configured tracker route.

## Why an agent-hook (not a skill, not AGENTS.md)

"Use `task`, never raw `gh issue` / `linear`" was advice in the task skill. Advice cannot
stop an autonomous agent: omp was filing Linear issues directly, skipping acceptance
criteria, motivation, user-impact, and screenshots. A git-hook cannot help — the write
happens against the tracker, not at commit time. The only place to stop it is
**before the command runs**.

`require-ticket-before-commit` only looks at `git commit` messages; its own tests
document that `gh issue create` is allowed. This hook is the create-time counterpart.

## Fail-closed

`on_error: "closed"`. If the hook cannot inspect the command, it blocks rather than
allowing a tracker write through a broken guard. An unparseable command that still looks like
a tracker write (unbalanced quotes around `fj issue`, a tracker path, …) is hard-blocked and is
never offered to the hatch — an approval cannot cover text the hook could not classify. That
holds through wrappers and chains too: an unclassifiable part inside `bash -c`, `eval`, a shell
heredoc, or wrapper nesting deeper than the recursion cap makes the WHOLE command unclassifiable
(hard block), even if another part of it is a clear, otherwise-hatchable write. Unrelated
unparseable commands are allowed.

## Known residuals — this is a pre-tool guard, not a firewall

It reads the literal command text the agent's Bash tool call carries. It does **not** claim
total API containment; these routes remain open and are deliberately listed:

- **Program files and binaries.** `python3 mutate.py`, `node tool.js`, `bash script.sh`, a
  compiled helper, or any CLI that wraps the tracker API internally — file contents are never
  read.
- **Runtime-assembled URLs.** A URL or endpoint built from a variable or command substitution
  (`URL=…; curl -X POST "$URL"`, `curl -K config`, `gh api "$EP"`, `http $URL f=x`,
  `wget -i urls.txt`) is not resolved. (An unresolvable *method* with a literal tracker URL is
  blocked — see above.)
- **Heredocs not directly fed to the interpreter.** `cat <<EOF | python3 -`, `python3 -`
  reading from a redirected file, or a heredoc to an unlisted interpreter is not analysed as a
  program. Supported: `python`/`python3`/`node`/`nodejs`/`bun`/`sh`/`bash`/`zsh`/`dash`/`ksh`
  with the heredoc on the same command.
- **Unregistered wrapper value-flags.** Each peeled wrapper (`sudo`, `doas`, `xargs`, `timeout`,
  `env`, …) has a fixed list of options that take a value. An option missing from that list
  whose operand is detached (`wrapper --new-flag VALUE cmd …`) makes `VALUE` look like the
  command word, and the real command behind it is not inspected.
- **Unlisted interpreters, clients, and wrappers.** `perl -e`, `ruby -e`, `deno eval`,
  `php -r`, `powershell`, `Invoke-RestMethod`, `aria2c`, `nc`/`openssl s_client`, `npx`/`bunx`,
  `find -exec`, `parallel`, `watch`, … are not inspected or peeled.
- **Other trackers' raw APIs.** Linear's GraphQL HTTP API (`api.linear.app/graphql`), GitLab
  (`glab issue …`, `api/v4/projects/<id>/issues`), Jira, and any tracker not shaped
  `repos/<owner>/<repo>/(issues|labels)` are not covered. Only the `linear` CLI is blocked for
  Linear.
- **Inline-program heuristics.** Detection needs a tracker path inside a string literal plus a
  recognized write call in code. A client alias outside the listed names (`api.post(…)`), a
  method passed through a variable, or a JS template literal whose `${…}` builds the URL can
  slip through. A write call and a tracker URL that happen to sit in the same program but are
  unrelated is over-blocked.
- **GraphQL.** A query read from a file (`query=@q.graphql`, `--input`) or shell variable is not
  inspected. `addComment` and `addLabelsToLabelable`/`removeLabelsFromLabelable` are not in the
  mutation list because they also target pull requests and cannot be told apart from the text.
- **Explicit read methods win.** `curl -X GET -d …` / `http GET url f=x` / `wget
  --method=GET --body-data …` are treated as reads by design (the request method is what the
  server acts on).
- **`tea` shape is doc-derived.** If the real CLI has diverged from its docs, a write may slip
  through or a read may be over-blocked.
- **Other tracker resources.** Milestones, projects, wiki, reactions, and repository settings
  endpoints are not covered — only `issues` and `labels`.
- **Junk-title check scope.** With task-cli absent, the `Review uncommitted` refusal recognizes
  direct `gh`/`linear`/`task`/`fj`/`tea` creates only — a wrapped or REST-body junk create is
  caught only while task-cli is armed (via the no-hatch rule above). Conversely, an armed
  tracker write that merely *mentions* the phrase (e.g. an issue comment) cannot be hatched and
  must be reworded.
- **Interpreter options with values before `-c`/`-e`** (`python3 -W ignore -c …`, `node -r m
  -e …`) are recognized, but an interpreter option that takes a value *glued into* the program
  flag is not.

## Test

```bash
echo '{"args":{"command":"gh issue new --title x"}}' | ./block_raw_ticket.py; echo "exit=$?"
# task on PATH → {"decision":"block",…} exit=10
# task absent  → {"decision":"allow"} exit=0

echo '{"args":{"command":"gh issue create --title \"Review uncommitted foo diff\""}}' | ./block_raw_ticket.py; echo "exit=$?"
# always → {"decision":"block",…} exit=10  (even when task is absent)

echo '{"args":{"command":"curl -XPOST https://forge.example/api/v1/repos/o/r/issues -d title=x"}}' | ./block_raw_ticket.py; echo "exit=$?"
# task on PATH → {"decision":"block",…} exit=10
```

Automated: `uv run --with pytest pytest tests/test_block_raw_ticket.py` (every fixture is fed to
the parser only — nothing is executed and no request is sent).

# block-raw-pr-merge

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that merges a PR directly and bypasses the ship gate:

- `gh pr merge <PR>` (squash/merge/rebase, any flags; `gh -R o/r pr merge` too)
- `gh pr merge --admin <PR>` (the admin bypass is the most dangerous case)
- `gh api …/pulls/<n>/merge` with a write method (`-X PUT` / `--method POST`) — the REST merge route
- `gh api graphql` running a `mergePullRequest` / `enablePullRequestAutoMerge` mutation (inline —
  even with `$variables` — or a file/stdin/substitution-backed query, which is over-blocked because
  it can't be inspected at pre-exec time)
- any of the above behind a **wrapper** command (`env gh pr merge`, `sudo … gh pr merge`,
  `timeout 60 gh pr merge`, `nohup`, `command`, …) — the wrapped `gh` is still an invoked merge
- any of the above wrapped in a **command substitution** — `` `gh pr merge 1` ``, `$(gh pr merge 1)`,
  `echo "$(gh pr merge 1)"` (executed even inside double quotes), or a process substitution
  `<(gh pr merge 1)` — the substitution body is extracted and re-scanned. A `$( … )` inside a `#`
  comment or a **quoted** heredoc body (`<<'EOF'`) is NOT executed and stays allowed; an **unquoted**
  heredoc body (`<<EOF`) IS expanded, so a merge substitution there is blocked

Lets the sanctioned merge path through:

- `gh ship <PR>` (and a `gh alias` that runs ship)
- `pr-ship.sh` / `ship.sh` (the script the ship alias points at)
- any non-merge `gh pr` subcommand (`view`, `list`, `checkout`, `create`, `comment`, …)
- a non-merge `gh api` call: a GET on `…/pulls/<n>/merge` (a merge-**status read**, no write method),
  or an **inline, single-quoted** `gh api graphql` non-merge query/mutation — **including one that
  carries literal GraphQL `$variables`** (`-f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){…}}'`).
  The shell never expands a single-quoted `$t`, so the query GitHub receives is exactly the text the
  hook read; the literal merge scan is authoritative. This is what lets an agent resolve its own
  review threads (`resolveReviewThread` / `addPullRequestReviewThreadReply`) to satisfy `gh ship`'s
  unresolved-threads gate
- a **prose mention** of a merge route — in an argument, a commit message, or a heredoc body — where
  `gh` is not the command word (argv[0]) of a segment (`tg "…gh pr merge…"`, `echo mergePullRequest`)

**Over-block on an uninspectable graphql query:** a `gh api graphql` whose `query` value comes from a
file (`-F query=@f`), stdin (`--input`), or carries a **shell-EXPANDABLE** `$`/backtick — one that is
unquoted, double-quoted, or concatenated so the shell rewrites it at runtime (`-f query="$Q"`,
`-f query="${Q}"`, `-f query="$(cat q)"`, `-f query="mutation{$OP}"`, `-f query='…'$OP'…'`) — is
blocked, because a merge mutation could be spliced in past the literal scan. Quoting is read from the
**raw** command (shlex strips it from the parsed tokens), so the concatenation form is caught even
though it de-quotes to the same text as a literal query. A **single-quoted** query is inert and
allowed even when it uses GraphQL `$variables` — to run one, keep it single-quoted (`-f query='…'`).
A shell-expanded REST method (`-X $METHOD`) on a `…/merge` endpoint is over-blocked for the same
reason.

## Shell-faithful command parsing

Detection is argv-based: the command is normalized (quote-, escape-, comment- and heredoc-aware)
and split into shell segments, and each segment's argv[0] is checked. This closes evasions that a
naive parse misses — a merge on a **second line** (a bare newline is a command separator), after a
`\`-newline continuation, behind a `#` comment on a prior line, or after a subshell/`$(...)`.

Command/process substitutions (`$( … )`, backticks, `<( … )` / `>( … )`) execute their body — even
`$( … )` and backticks **inside double quotes** — so each body is extracted (quote-aware:
single-quoted spans suppress substitution and stay inert) and re-scanned with the same detector,
recursively for nesting. This is why `echo "$(gh pr merge 1)"` blocks but `echo '$(gh pr merge 1)'`
(single-quoted, inert) and `tg "…gh pr merge…"` (a plain string, no substitution) both pass.

**Prose-mention allow vs. unparseable fail-closed:** the "prose mention is allowed" rule applies to
a command that PARSES. If a command cannot be parsed (an unbalanced quote / shell syntax error) AND
its raw text contains a merge-route keyword (`gh` + `pr merge` / `pulls/<n>/merge` / `mergePullRequest`
/ `enablePullRequestAutoMerge`), it fails **closed** and is blocked — even a would-be prose mention —
because the hook cannot prove it is not a merge. A benign unparseable command with no such keyword
(`grep won't file`) still passes.

**Deliberate over-block (accepted rare false-positive — the safe direction for a security gate):**
a `gh pr merge` at the **executable position** of a **heredoc body line** is blocked, even though a
heredoc body is data. A `<<` inside arithmetic (`(( a << b ))`) is treated as a left-shift, not a
heredoc; but because no parser classifies every shell construct perfectly, a crafted heredoc can
plant a matching terminator to make a real merge look like body text (`(( 0 << merge ))` / `gh pr
merge 1` / `merge`). Rather than chase every mis-classification, any executable-position merge on a
skipped body line is re-injected and blocked. A legitimate heredoc almost never opens a line with a
literal `gh pr merge`; a prose mention still passes. If a genuine heredoc trips this, re-phrase it or
use the Telegram hatch below.

## Why an agent-hook (not a skill, not a git-hook)

"Use `gh ship`, never a raw merge" was for a long time **advice only** — a line in
`AGENTS.md`. Advice cannot stop an autonomous (auto-mode) agent from running `gh pr merge`:
the moment permission prompts are auto-accepted, a doc rule has zero enforcement and the
green-CI + mandatory-screenshot gates are silently skipped. A git-hook can't help either —
the merge happens server-side on GitHub, not at a local commit/push. The only place to stop
the bypass is **before the command runs**, which is what a `pre-bash` agent-hook does. This
is the enforcement counterpart of the `ci/ship` gate and the ship usage rule.

This guard is what makes auto-mode safe: the agent runs without babysitting, and the guard
catches the one irreversible action (a gate-skipping merge) before the side effect.

## No self-service override — external approval only

There is **no** `ALLOW_RAW_PR_MERGE` (+`_REASON`) env and **no** `# no-ship-guard: <reason>`
inline sentinel any more. An agent could set either on its own command, so those merely let the
guarded agent grant itself the exact bypass this hook exists to stop. The block is now
**deny-by-default**.

Use `gh ship <PR>` (the green-CI-gated, screenshot-checked path). For a genuine one-time
exception, **ask Alex** — or request a single approval by setting
`RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="<written justification>"`:

```bash
RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down, manual verify done" \
  gh pr merge 123 --admin
```

The inline `VAR=… <command>` form works here because this is a **pre-bash** hook: it parses the
leading `RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=…` assignment out of the command string the event
carries (a pre-bash hook runs in its own process *before* the shell evaluates that prefix, so the
value never lands in the hook's `os.environ` — only in the command text). Exporting the same var
into the harness environment works too and takes precedence. (For Edit/Write-gated hooks there is
no command string, so the var **must** be exported into the hook process env — see those hooks'
READMEs.)

That routes one Telegram approval request to Alex (deny-by-default): unset means the hook never
contacts Telegram (the merge just blocks); a blank or bare `1`/`true`/`yes`/`on` is rejected
without a Telegram call; a real justification runs the trusted `tg-ctl ask` and allows the raw
merge **only on exit 0** (any nonzero exit / error / timeout denies, the block leading with
`hatch escalation denied: <reason>`).

## Fail-closed

`on_error: "closed"`. If the hook can't inspect the command (a malformed event, a crash),
it **blocks** rather than allows — a raw merge slipping through a broken guard is the failure
this hook exists to stop.

## Install

```bash
chmod +x block_raw_pr_merge.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory. (rig apply does this for you.)
```

## Test

```bash
echo '{"args":{"command":"gh pr merge 123 --admin"}}' | ./block_raw_pr_merge.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"gh ship 123"}}' | ./block_raw_pr_merge.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

A one-time exception is an external Telegram approval — set
`RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="<why>"` (deny-by-default; only a `tg-ctl ask` reply whose
decision is explicitly `allow` permits the raw merge — a clean exit with no such reply denies).

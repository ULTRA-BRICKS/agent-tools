# block-reset-hard

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that irreversibly wipes uncommitted/untracked work with no undo:

- `git reset --hard` (with or without a ref: `git reset --hard`, `git reset --hard HEAD~3`,
  `git reset --hard origin/main`, ...) — discards uncommitted **tracked** changes.
- `git clean` with a real `-f`/`--force` flag, alone or clustered with `-d`/`-x` in any
  short-flag order (`-fd`, `-df`, `-fdx`, `-xdf`, `-f -d`, `--force --force`, ...) —
  discards **untracked** files.

Both are the same failure class — a destructive rewrite of the working tree git cannot undo
afterward — which is why one hook and one escape hatch cover both.

Lets the safe, reversible alternatives through:

- `git checkout -- <file>` / `git restore <file>` (discard specific tracked files, scoped)
- `git reset` with no `--hard` (bare, `--mixed`, `--soft`) — never touches the working tree
- `git clean -n` / `git clean --dry-run` (preview only, no deletion)
- `git clean` with no force flag at all (git itself refuses to delete without `-f`)
- text that merely **mentions** "reset --hard" or "clean -fd" — a commit message, a code
  comment, a doc, a `grep` for the string — parsed via real argv, never substring-matched

## Why this incident needed a hook

A subagent working an unrelated PR ran `git reset --hard` mid-session in a checkout shared
with a different session and wiped that other session's uncommitted work (recovered, but it
exposed the gap). The incident was **accidental**, not a deliberate bypass attempt. This
hook's real value is turning an accidental destructive reset/clean into a **deliberate,
logged** one. It previously shipped a self-service escape hatch (an env var / inline comment
the agent could set on its own command) — that was security theater, removed per Alex tg#6554.
The block is now **deny-by-default** with the external `approval_cmd` extension point below;
"nothing configured" means the block stands, not a free bypass.

## Why an agent-hook (not a git-hook)

Git has **no `pre-reset`/`pre-clean` hook point at all** — a git-hook literally cannot
intercept `reset`/`clean` before they run; the closest thing (`pre-commit`) fires at commit
time, long after a `reset --hard`/`clean -f` has already destroyed the working tree. The only
place to stop the side effect is **before the command runs**, which is what a `pre-bash`
agent-hook does — same reasoning as `block-no-verify` stopping `--no-verify` (the flag that
disables the very hook that would otherwise catch it).

## Parsed, not raw-matched

The verdict is made from the **parsed** command (same `punctuation_chars=True` shlex
tokenizer + segment-splitting the `#59` sibling hooks use), never a raw substring of the
whole string:

- the command is tokenized **line by line** — a newline is a command separator, same as `;`
  — so a two-line Bash call (`cd /repo` on line one, `git reset --hard` on line two) is still
  caught; a single flat shlex pass over the whole string would miss it entirely (the newline
  is just whitespace, so line one's `cd`/`echo` becomes argv[0] and line two is invisible);
- a `#` is only a comment at a **word boundary** — `foo#bar` is literal text to a real shell,
  not a comment start; shlex's default commenter gets this wrong (cuts at ANY unquoted `#`,
  even mid-word) and would silently truncate parsing, hiding a later chained command like
  `echo foo#bar && git clean -fd` (ported fix from `block-no-verify`/
  `require-review-before-commit`);
- the command is then split into shell segments on real separators (`&&`, `;`, `||`, `|`, ...);
- each segment's real argv is recovered by stripping leading shell-grouping/control-flow
  tokens (`(`, `{`, `!`, `if`, `then`, `do`, `while`, `until`, `for`, `case`, ...), inline
  `VAR=value` env assignments, and a table of common wrapper executables — `timeout`, `env`,
  `nice`, `ionice`, `nohup`, `setsid`, `stdbuf`, `time`, `unbuffer`, `command`, `sudo`, `doas`,
  `exec`, `taskset`, `chrt`, `setpriv`, `flock`, `runuser` — ported from
  `block-no-verify`/`require-ticket-before-commit`'s audited wrapper table, including each
  wrapper's own VALUE-taking flags (`sudo -u alice`, `nice -n 10`, `env -u FOO`) so the
  operand is correctly skipped instead of misread as the wrapped command;
- then git's own global options (`-C <dir>`, `-c key=val`, `--git-dir=...`, `--no-pager`,
  ...) are skipped so `git -C /some/path reset --hard` and `git --no-pager clean -fd` are
  still caught;
- only then is `argv[0]=="reset" && "--hard" in argv[1:]` or `argv[0]=="clean" && <a real
  force flag>` checked.

So all of these are caught, not just the bare form: `sudo -u alice git reset --hard`,
`nice -n 10 git clean -fd`, `time git reset --hard`, `command git reset --hard`,
`while git clean -fd; do ...; done`.

This is why a commit message, doc, or `grep` that merely contains the phrase "reset --hard"
or "clean -fd" as **text** is allowed — the words are data, not a parsed flag on a real git
invocation.

### `git clean` force-flag detection

Short-flag clusters are scanned character-by-character, stopping at `e` — `git clean` has
exactly one value-taking short option (`-e <pattern>`, glued form `-e<pattern>`), so the
letters after `e` in a cluster are that pattern's text, not further flags:

- `-fd`, `-df`, `-fdx`, `-xdf`, `-f -d`, `--force --force` → **force** (blocked)
- `-nf`, `-fn` → **force** (the `-n` doesn't cancel a real `-f` present anywhere in the
  clustering; conservative in the same direction as the rest of this guard)
- `-n -e"*.conf"` → **not force** (allowed — a dry-run with an exclude pattern)
- `-fe"*.o"` → **force** (the `f` comes before `e`, so it's a real flag, not part of the
  pattern)
- `-ef"*.o"` → **not force** (the `e` consumes the rest of the token as its pattern value —
  `f*.o"` — this is `-e` with a value, not `-e` plus `-f`)

## No self-service bypass — external approval only

There is **no** env-var (`ALLOW_GIT_RESET_HARD`) or inline `# no-reset-guard:` escape hatch any
more. Both let the very agent this hook constrains grant itself an exception — security
theater, not a permission gate (Alex tg#6554). They were removed.

The block is now **deny-by-default**. A repo owner can wire a real external-approval path in the
committed, code-reviewed `rig.yaml`. A one-time Telegram hatch can also be requested with a
written justification in `RIG_HATCH_REQUEST_BLOCK_RESET_HARD`; that request path runs before
`approval_cmd`, so an invalid/denied Telegram request does not fall through to a configured
`approval_cmd`. Because this is a **pre-bash** hook, the justification can be supplied inline on
the gated command itself (`RIG_HATCH_REQUEST_BLOCK_RESET_HARD="…" git …`): the hook parses the
leading assignment out of the command string the event carries (a pre-bash hook runs in its own
process *before* the shell evaluates that prefix, so the value never reaches its `os.environ`).
An exported value takes precedence over an inline one.

```yaml
agent_hooks:
  approval_cmd: "/path/to/approve.sh"   # optional; run when a reset --hard/clean -f would block
  approval_cmd_timeout_s: 5             # optional; default 5.0, capped at 6.0
  tg_ctl_path: "/path/to/tg-ctl"         # optional tg-ctl override — read ONLY from the ACCOUNT
                                         # HOME's rig.yaml (never this repo's; see hatch note below)
```

`agent_hooks.approval_cmd` is a **single, shared key** — the same `approval_cmd` is read by
this hook AND by `pin-primary-worktree`. A repo that wants different handling per guard should
point `approval_cmd` at one dispatcher script that branches on `RIG_APPROVAL_HOOK`
(`block-reset-hard` vs `pin-primary-worktree`) and `RIG_APPROVAL_KIND`.

When `approval_cmd` is set, the hook runs it as the block is about to fire and **allows only on exit
0**; a nonzero exit, an error, or a timeout all mean **denied**. With nothing configured, the
block stands. The command string comes only from `rig.yaml` (never from the agent or the
offending command); context reaches it as environment variables — `RIG_APPROVAL_HOOK`,
`RIG_APPROVAL_KIND` (`reset --hard` / `clean -f...`), `RIG_APPROVAL_CWD`, `RIG_APPROVAL_COMMAND`
— never string-interpolated, so there is no injection surface. For a `git -C <other-repo>`
command the config is resolved against `<other-repo>`'s rig.yaml (the repo actually being
wiped), not the shell cwd.

The Telegram hatch is intentionally not a self-service bypass. `RIG_HATCH_REQUEST_BLOCK_RESET_HARD`
must contain a nonblank written justification. If the env var is unset, the hook does not contact
Telegram and falls through to `approval_cmd` / default deny. If the env var is present but blank,
whitespace-only, or a bare flag value such as `1`, `true`, `yes`, or `on`, the hook does not contact
Telegram and denies. A real justification runs `tg-ctl ask` with the question as a JSON
ButtonRequest on stdin; only a well-formed reply on stdout whose decision is explicitly `allow`
allows — an empty or unparseable reply, an explicit deny, any nonzero exit, launch errors, and
timeouts all deny (a clean exit alone is NOT approval: `tg-ctl ask` exits 0 regardless of
outcome). The helper never resolves
`tg-ctl` from ambient `PATH`. **The approval binary is a trust anchor: it is resolved from the
account's REAL home only** (`agent_hooks.tg_ctl_path` in the home `rig.yaml`, home located via
`pwd.getpwuid` — never `$HOME`, never the repo/`cwd` the hook runs in), then the hardcoded absolute
candidates `/Users/ultra/.files/bin/tg-ctl` (`/Users/ultra/.files/repos/tg-cli/tg-ctl` after
realpath), `/usr/local/bin/tg-ctl`, `/opt/homebrew/bin/tg-ctl`. A **repo-local** `tg_ctl_path` is
deliberately IGNORED — otherwise a guarded agent could commit `rig.yaml` pointing it at an
always-exit-0 binary and self-approve (the exact self-service bypass this gate closes).

> `approval_cmd` is read with the same minimal, stdlib-only rig.yaml scanner this hook family
> uses (no YAML library is imported). It is a single-line scalar: quote the value, and avoid a
> literal `#` or a trailing nested-quote in it (point `approval_cmd` at a script path instead
> of inlining a complex shell one-liner).

> **Claude Code outer timeout:** this descriptor sets `timeout_ms: 960000`, which STRICTLY
> exceeds the helper's 930s worst case (`tg-ctl` ask's 900s cap plus a 30s cleanup margin) by a
> 30s headroom, so an unanswered or hung `tg-ctl` resolves to the hook's own deny before the
> bridge reaches this descriptor budget. This hook is `on_error: closed`, so a bridge timeout
> already fails safe (deny) — the headroom keeps the message a clear block rather than a generic
> dispatcher denial. Note this is only the agents-hooks/v1 descriptor budget enforced by
> `cc_hook_bridge`. Claude Code's own command-hook `timeout` defaults to 600s, and the live
> `~/.claude/settings.json` / rig-cli `hook_bridge_entries` currently register `cc_hook_bridge`
> without a `timeout`. Until rig-cli or settings add a hook `timeout` above 960s, Claude Code can
> still kill the bridge before a full Telegram wait finishes.

> **Repo-wide impact:** this hook is **always on** (no opt-in gate, unlike pin-primary-worktree).
> With nothing configured, `git reset --hard` and `git clean -f...` are a hard, non-bypassable
> block for every repo that installs this hook, until that repo's owner wires `approval_cmd`.

**Agents: ask, don't self-grant.** If you genuinely need a destructive reset/clean, ask the
human directly — you can no longer flip your own bypass.

## Fail-closed

`on_error: "closed"`. If the hook can't inspect the command (a malformed event, a crash), it
**blocks** rather than allows — a destructive reset/clean slipping through a broken guard is
the exact failure this hook exists to stop.

When the command itself can't be parsed (unbalanced quotes) but the raw text plausibly
contains one of the two dangerous forms, the decision is **block** with a message that names
the unbalanced quotes. The external `approval_cmd` is **not** consulted on this path — an
unparseable command isn't classified as a concrete `dangerous` verdict, so it fails closed on
the plumbing signal before any approval is requested; fix the quoting first. An unrelated
command with an unrelated unbalanced quote (e.g. `grep won't file`) is **allowed** — blocking
it would be pure over-block.

## Known limitations

There is no longer a self-service hatch (removed — Alex tg#6554), so the parser is the only
line between an accidental destructive command and the block. The bar for what's fixed vs.
documented here is: **would a confused, non-evasive agent produce this exact command by
accident?** Newline-separated commands and mid-word `#` (both fixed above — see "Parsed, not
raw-matched") clear that bar; these don't:

- `git reset --har` (an unambiguous long-option prefix git itself accepts) is not detected —
  only the literal `--hard` spelling is matched. No one accidentally abbreviates a destructive
  flag they weren't trying to type in full.
- `git clean -e -f` (a bare `-e` immediately followed by a SEPARATE `-f`-shaped token) is
  conservatively treated as a real `-f` (**blocked**) by this hook — but real git parses that
  trailing token as `-e`'s exclude-pattern VALUE, confirmed empirically (`git clean -e -f`
  prints `clean.requireForce is true and -f not given: refusing to clean` — i.e. real git does
  **not** treat it as force). This is a **false block**, not a bypass — the safe-by-default
  direction for a destructive-action guard, and not worth getopt-style value tracking for such
  an exotic input shape.
- `git -c clean.requireForce=false clean -d` (or the same setting already sitting in someone's
  **ambient** gitconfig, not passed inline) makes a bare `clean -d` — no `-f` at all — actually
  delete, contradicting this hook's "no force flag = git refuses" assumption. Not detected:
  nobody sets this inline by accident, but if it's already in ambient config, a bare
  `clean -d` genuinely slips past this hook. Worth knowing, not worth building a
  git-config-value detector for.
- **Approval-cwd resolution follows only `git -C <dir>`.** A cwd change made by a wrapper or a
  chained `cd` — `env -C <dir> git reset --hard`, `sudo --chdir <dir> git ...`, `cd other &&
  git reset --hard` — is not followed, so approval for such a command is resolved against the
  shell cwd's `rig.yaml`, not the relocated target repo. (Same documented class as
  pin-primary-worktree's `cd other-repo` scope note.) Deny-by-default still holds; the only
  residual is a cwd repo whose configured approver could then approve a wrapper-relocated
  destructive command aimed at a different repo.
- A shell **alias** for `git` (`alias g=git`) is not resolved — universal to this whole hook
  family. Aliases only expand in an *interactive* shell by default; a harness running via
  `bash -c "<command>"` doesn't expand them anyway, so this is rarely reachable in practice.
- `env -S/--split-string '<command>'` (a re-tokenized inline command string), a nested
  shell-string interpreter (`bash -c 'git reset --hard'`), a command substitution
  (`$(git reset --hard)`), or a wrapper outside a brace group are not re-parsed — same
  documented gap as `block-no-verify`; all require deliberately crafted input, not an
  accidental shape.
- An unlisted/exotic wrapper (`unshare`, `nsenter`, `firejail`, ...) is not peeled — the
  wrapper table mirrors `block-no-verify`/`require-ticket-before-commit`'s audited set, not
  every process-isolation tool that exists.

## Install

```bash
chmod +x block_reset_hard.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory. (rig apply does this for you.)
```

## Test

```bash
echo '{"args":{"command":"git reset --hard"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"git clean -fd"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"git checkout -- file.txt"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

echo '{"args":{"command":"git clean -n"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

# With a repo owner's approval_cmd wired in rig.yaml (agent_hooks.approval_cmd), a reset --hard
# is allowed only when that command exits 0; unconfigured / nonzero / timeout all block:
echo '{"cwd":"/repo-with-approval-cmd","args":{"command":"git reset --hard"}}' \
  | ./block_reset_hard.py; echo "exit=$?"
# → decision":"allow" iff approval_cmd exited 0, else block  exit=0|10
```

Unit + behavior tests live in
[`tests/test_block_reset_hard.py`](../../tests/test_block_reset_hard.py):

```bash
uv run --with "pytest>=8,<9" python -m pytest tests/test_block_reset_hard.py -q
```

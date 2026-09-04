# require-review-before-commit

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 20

On a `git commit`, checks that an AI code review ran recently for the current change. If
none did, it **blocks** with a reminder to review the uncommitted diff first.

## What it does NOT gate

- **Non-commit git ops** — `git stash`, `git worktree`, `git status`, `git add`. Only a real
  `git commit` segment is gated. The commit is detected from the *parsed argv*, not the raw
  string, so a `commit` token in a comment / message / pathspec / sibling command never trips
  it.
- **A commit WRAPPED in another program** — `sudo git commit`, `env VAR=1 git commit`, `time git
  commit`, `bash -c 'git commit …'`, a `(git commit …)` subshell or `{ git commit; }` group. The
  detector requires the
  segment's executable to BE git (or a path to it), so a wrapper is not recognized. This is a
  deliberate trade for precision (matching `commit` as a bare substring false-blocked `git help
  commit` / `git commit-graph`); the gate is process discipline (`on_error: open`), not a security
  boundary. The direct and absolute-path (`/usr/bin/git`) forms are fully covered.
- **Docs-only commits** — when every staged path is documentation (a doc extension
  `*.md`/`*.mdx`/`*.markdown`/`*.rst`/`*.adoc`/`*.rdoc`/`*.pod`, or a non-code file under a
  `docs/` directory), the commit is
  allowed without a review marker. **Source/script/config never counts as docs** — code even
  under `docs/` (`docs/conf.py`, `docs/build.py`, `docs/Makefile`) still requires review. A bare
  `.txt` requires review when it's *outside* `docs/` (e.g. a top-level `requirements.txt`); under
  `docs/` a `.txt` is treated as documentation (release notes, plans). This fast-path reads the
  staged *index*, so it does
  NOT fire for `git commit -a/-am`, `-p/--patch/--interactive`, `--pathspec-from-file`, an
  explicit pathspec, or a `--git-dir`/`GIT_DIR=…`-redirected commit — those can include content
  the cwd index doesn't list, so the gate stays.

## No self-service skip — external approval only

There is **no** `REVIEW_SKIP=1` inline-env bypass and **no** `[skip-review: <reason>]`
commit-message trailer any more. An agent could set either on its own commit, so those
"gates" were security theater — they are removed and the block is now **deny-by-default**.

For a genuine one-time exception, **ask the human** — or request a single approval by setting
`RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT="<written justification>"`. That routes one
Telegram approval request to Alex (deny-by-default): the env var must carry a real
justification — unset means the hook never contacts Telegram, and a blank or bare
`1`/`true`/`yes`/`on` is rejected without a Telegram call. A real justification runs the
trusted `tg-ctl ask` with the question as a JSON ButtonRequest on stdin; **only a reply whose
decision is explicitly `allow` allows**, and an empty or unparseable reply, an explicit deny, any
nonzero exit, launch error, or timeout denies (the block then leads with
`hatch escalation denied: <reason>`).

You can supply the justification either as an inline prefix on the gated command
(`RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT="…" git commit …`) or by exporting the var into
the harness environment. This is a **pre-bash** hook, so it parses the inline assignment out of
the command string the event carries — a pre-bash hook runs in its own process *before* the shell
evaluates the `VAR=x cmd` prefix, so the value never reaches its `os.environ`. An exported value
takes precedence over an inline one.

## How it knows a review ran

It looks for a **marker file** that your review tool touches on a successful run, and
checks the marker is fresh (within a window, default 1h). Wire it up like:

```bash
review --uncommitted && touch "${REVIEW_MARKER:-$HOME/.cache/agent-tools/last-review}"
# or
codex exec review --uncommitted && touch "$REVIEW_MARKER"
```

Configure via env:

- `REVIEW_MARKER` — marker file path (default `~/.cache/agent-tools/last-review`)
- `REVIEW_FRESH_WINDOW_S` — how recent the marker must be, in seconds (default `3600`)

## Why an agent-hook

The review has to happen *before* the commit, mid-session, while the diff is still
uncommitted — a git-hook fires too late to prompt "go review first" usefully, and can't
know whether a *session* review ran. This enforces the `ai-review-before-commit` /
`pre-commit-gate` skills.

## Fail-open, on purpose

`on_error: "open"`. This is process discipline, not a security boundary — a crash in the
check must never make committing impossible. (Contrast `block-no-verify` and
`block-secrets-write`, which are fail-closed.) It blocks only when it can *confirm* no
recent review ran.

## Test

```bash
chmod +x require_review.py
rm -f ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"block ...  exit=10

mkdir -p ~/.cache/agent-tools && touch ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"allow"  exit=0
```

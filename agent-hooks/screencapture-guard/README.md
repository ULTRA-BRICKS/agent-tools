# screencapture-guard

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 12

Blocks macOS `screencapture` — path-qualified (`/usr/sbin/screencapture`) and
wrapper-prefixed (`timeout`/`sudo`/`env`/`nice`) forms included — and points the
agent at Playwright or CDP instead.

Ref: the 2026-07-01 agent-ecosystem retrospective (`hyperide` repo,
`docs/specs/2026-07-01-agent-ecosystem-retrospective.md`), gap **G-6**. Not linked
directly — `hyperide` is a separate, private repository, so a relative path here
would escape this repo and 404 on GitHub's web UI for anyone without that repo as
a sibling checkout.

## Why this needed a hook

The outer belt is already a harness `permissions.deny` glob (`Bash(screencapture:*)`).
Prefix globs miss wrappers and a path-qualified binary, and they do not name the
Playwright/CDP recipe. G-5 (`pkill-guard`) shipped as an argv-aware PreToolUse
hook; G-6 was still prose + glob. `screencapture` black-frames windows on other
Spaces and trips macOS Screen Recording grants, so a "screenshot proof" is often
an empty frame.

## What's blocked vs. allowed

- **Blocked:** `screencapture /tmp/x.png`, `screencapture -x out.png`,
  `/usr/sbin/screencapture -x /tmp/x.png`, `timeout 5 screencapture out.png`,
  `sudo screencapture out.png`, `env FOO=1 screencapture out.png`,
  `echo hi && screencapture out.png`.
- **Allowed:** a command that only mentions the word as text (`echo screencapture`,
  `git commit -m "never screencapture"`), `agent-browser screenshot`, `rg screencapture`,
  unrelated commands.

Detection is argv-based (shlex), same wrapper-peel discipline as `block-reset-hard`.
A command that cannot be parsed fails **closed** only when the raw string contains
the word `screencapture`; a benign unbalanced quote is still allowed.

## No self-service bypass

There is no env-var hatch. Screenshots go through Playwright or CDP; there is no
legitimate agent use of macOS `screencapture`.

## Known residuals

- `bash -c 'screencapture …'` / `sh -c` / `python -c` hiding the binary is not
  re-scanned (same residual `block-raw-worktree-add` documents). Wrappers in the
  shared table *are* peeled.
- The harness permissions.deny glob remains the outer belt; this hook is the
  argv-aware inner belt.

## Test

```bash
python -m pytest -q tests/test_screencapture_guard.py
```

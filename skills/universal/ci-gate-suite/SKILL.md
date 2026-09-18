---
name: ci-gate-suite
description: 'Use when setting up or extending CI/PR-merge gates — security scanning, an AI-review step, "block merge until X" rules, dependency/license review, commit-title lint, or a green-CI-gated merge command. Points at ready-made drop-ins in `ci/` instead of hand-rolling each gate.'
---

# CI gate suite: adopt the drop-ins, don't hand-roll

When a repo needs CI gates, the mistake is to invent each one from scratch — a fragile
hand-written YAML per concern, no escape hatch, no tier, an unpinned action. There is a
ready-made, vendor-neutral suite in [`ci/`](../../../ci/). **Look there first.** Each slot is
one concern with a GitHub Actions workflow, usually a generic shell script (for non-GitHub
CI), and a README stating the standard engine, the knobs, and the false-positive escape
hatch.

## The catalog (one line each — full details in `ci/README.md`)

**Security (run several — different bug classes):**
- `ci/secret-scan/` — **gitleaks**: stop credentials reaching git history.
- `ci/codeql/` — **CodeQL**: deep semantic SAST. Has a **self-gate** variant for a private
  repo with no GitHub Advanced Security (runs the analysis, gates on the SARIF locally).
- `ci/sast/` — **semgrep**: fast pattern SAST; pair with CodeQL.
- `ci/dependency-review/` — block a PR adding a vulnerable/disallowed-license dependency
  (GitHub's action) + a multi-ecosystem audit script for existing deps.

**Review hygiene / merge blocks:**
- `ci/review-threads/` — fail while review threads are unresolved (GraphQL `reviewThreads`).
- `ci/pr-checklist/` — fail while PR-body `- [ ]` boxes are unchecked (tamper-resistant
  `pull_request_target` + a tested parser).
- `ci/leftover-grep/` — fail on `.only`/`debugger`/`console.log`/untracked-`TODO`/conflict
  markers in the **added** lines (diff-scoped, dependency-free).
- `ci/screenshots/` — require an embedded image on UI-touching PRs (at open AND merge).

**Reviewers (advisory — inform, don't auto-block):**
- `ci/ai-review/` — run a **configurable** review CLI (codex / review-cli / your own) on
  the PR diff, post findings as a sticky comment.
- `ci/copilot-findings/` — surface (optionally gate on) Copilot review comments + Autofix
  code-scanning alerts. (No magic API — it aggregates two real surfaces; the README is
  honest about limits.)

**History / merge:**
- `ci/pr-title-lint/` — Conventional Commits on the PR title (for squash-merge repos).
- `ci/ship/` — a green-CI-gated merge command that re-runs the review-thread + screenshot
  preflights client-side, then merges and cleans up the branch/worktree.

## How to adopt

1. Read [`ci/README.md`](../../../ci/README.md) — the slot index and a recommended stack.
2. For each gate you want, read its slot README, copy `workflow.yml` into
   `.github/workflows/`, copy any sibling script, and set the knobs (env / `with:`).
3. **Pin** any third-party action you add to a commit SHA (the slots already do; keep it
   that way when you bump).
4. Make the important ones **required** in branch protection (admin, one-time) so they
   actually block — a non-required check is advisory regardless of how it's written.

## The principles every slot follows (apply them if you add one)

- **Name the standard engine** — don't make the reader guess (secret scan = gitleaks, SAST =
  semgrep/CodeQL). Wrap the tool; don't reinvent it.
- **Block vs warn tier, stated explicitly.** A required CI check defaults to *block* — a
  warn-only required check is ignored and worthless.
- **An escape hatch for false positives** (inline comment / allowlist / a logged override),
  never "delete the step to fix it later."
- **Advisory ≠ gate.** An AI/Copilot reviewer informs a human; gating hard on it punishes
  its false positives. Gate on deterministic checks; surface the probabilistic ones.
- **`pull_request_target` only ever reads PR *metadata*** (title/body), never checks out or
  runs PR-head code — the token is write-scoped. The `pr-checklist` and `pr-title-lint`
  slots show the safe pattern.

## Related

- `pre-commit-gate`, `ai-review-before-commit`, `secret-scanning`, `visual-proof-cycle`,
  `deferred-findings-tracking` skills — the local-habit twins of several gates here.
- [`../../../git-hooks/`](../../../git-hooks/) — the local pre-commit/pre-push backstops.

## Troubleshooting: a billing-blocked account is not a self-hosted-runner problem

- **The tell is the annotation text, not `conclusion` or `steps` alone.** Exhausted GitHub
  Actions billing/credits (or a failed payment) fails checks fast with an annotation like
  "recent account payments have failed or your spending limit needs to be increased" —
  that annotation, not the `conclusion` value (ordinary red CI is `failure` too) or an
  empty `steps` array (a queued job has one too, before any step starts), is what actually
  identifies it. A runner-capacity problem instead leaves jobs at `status: queued`
  ("Waiting for a runner"), never producing a conclusion, hosted or self-hosted.
- **Don't reach for self-hosted runners as the fix.** A failed payment can disable Actions
  dispatch for the account outright (no job runs at all); an exhausted *spending limit*
  specifically meters GitHub-hosted minutes, so self-hosted runner minutes keep working —
  but standing up a runner fleet is still very likely the wrong move: it's rarely the
  repo's intended architecture, and it doesn't address the billing problem the account
  actually has. The real remediation is on the billing account (restore the payment method
  / raise the spending limit); a same-repo `runs-on: self-hosted` workflow with an attached
  runner is a legitimately different, unrelated setup, not a workaround improvised in
  response to this failure.
- **If a ready PR needs to move now:** `ci/ship/` is purpose-built to detect this class of
  structural CI outage and substitute a local gate rather than refuse outright. Treat
  `ci/ship/README.md` as the sole source of truth for exactly what it substitutes and when
  `--skip-ci` is still required — this doc intentionally doesn't restate that mechanism, so
  it can't drift out of sync with it.

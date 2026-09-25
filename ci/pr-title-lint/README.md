# PR-title lint — Conventional Commits

Validates the **PR title** against [Conventional Commits](https://www.conventionalcommits.org)
(`type(scope)!: subject`). With **squash-merge**, the PR title becomes the commit subject on
the default branch — so this keeps history clean and machine-parseable (changelog tools,
semantic-release, `git log` greps by type).

**Engine = a dependency-free inline check** (stdlib `python3`, on every runner image) that applies
the same rules as [amannn/action-semantic-pull-request](https://github.com/amannn/action-semantic-pull-request).
That action was dropped because it calls the GitHub REST API and crashes on Forgejo / Gitea runners
(`Cannot read properties of undefined (reading 'login')`). The inline check makes no API call: it
reads the title from the event payload (`$GITHUB_EVENT_PATH`), so it behaves the same on GitHub and
on Forgejo.

## Quick start

```bash
cp ci/pr-title-lint/workflow.yml .github/workflows/pr-title-lint.yml
# Optional hard block: Settings -> Branches -> required checks -> add "pr-title-lint".
```

## Knobs

Env on the workflow step:

- `TYPES` — the allowed Conventional types, space-separated (default: the standard set). Trim to taste.
- `SCOPES` / `REQUIRE_SCOPE` — restrict scopes to a space-separated list, and/or require one
  (`"true"`). Leave `SCOPES` empty to allow any scope.
- `SUBJECT_PATTERN` — a regex the subject must match (default: lowercase start, no trailing
  period); `SUBJECT_PATTERN_ERROR` is the message, with `{subject}` / `{title}` placeholders.

A manual run (`workflow_dispatch` with a `title` input) validates a given title — useful to
test the gate itself.

## Scope: title only

This gate checks the **PR title** (the squash subject). It does **not** validate every
commit message on the branch — that's a job for a `commit-msg` git-hook
([`../../git-hooks/`](../../git-hooks/)), which catches it locally at commit time. Run both
if you don't squash-merge.

## Pinning

Nothing to pin: the check uses no third-party action, only the runner's `python3`.

## Why `pull_request_target`

So the trusted base-branch copy of the check runs (a PR cannot weaken the gate it must pass).
It reads **only the title** from the event payload — it never checks out or runs PR-head code,
and the job needs no token scope beyond `contents: read`.

## When to use

Any repo that squash-merges and wants a Conventional-Commit history (or feeds an automated
changelog / release tool). Pairs with the `atomic-commits` skill.

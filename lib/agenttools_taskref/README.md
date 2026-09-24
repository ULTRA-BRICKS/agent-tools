# agenttools-taskref — backend-aware task-code validation

Validates a candidate task/ticket code (`--task CODE`, `$REVIEW_TASK_CODE`, or any other
free-form "which ticket is this for" string) against what a REAL code actually looks like
for a given repo — instead of accepting any string, which is exactly how
`~/.config/review-cli/run-stats.jsonl` ended up with thousands of invented, non-resolvable
codes (bare placeholders like `CODE`/`TEST-1`, free-text slugs) recorded as if they were
real ticket references (agent-tools#607).

A repo's own `task.yaml` (or `rig.yaml`'s `task:` block) already declares this — read the
same way [task-cli](https://git.hyperide.ai/ultrabricks/task-cli)'s own `tasklib/config.py`
reads it:

- `backend: github-issues` (the default) — only a real, resolvable issue/PR number in the
  repo's own `github.repo` (or an explicit `owner/repo#N` cross-repo form) validates.
  Confirmed via `gh api repos/<owner>/<repo>/issues/<n>` — a numeric-shaped string that
  404s is rejected exactly like a free-text slug.
- `backend: linear` — only a code carrying the repo's actual configured `linear.team`
  prefix validates. (No live Linear API existence check — see "Scope" below.)

## Usage

```python
from pathlib import Path
from agenttools_taskref import validate

result = validate("607", Path("/path/to/agent-tools"))
result.ok          # True
result.canonical   # "607" (same-repo) or "owner/repo#607" (explicit cross-repo form)
result.url         # "https://github.com/alex-mextner/agent-tools/issues/607"
result.backend     # "github-issues"
result.reason      # "resolved via gh api"

validate("TEST-1", Path("/path/to/agent-tools"))
# ValidationResult(ok=False, backend="github-issues",
#   reason="'TEST-1' is not a GitHub issue/PR reference (expected a bare number, '#N', or "
#          "'owner/repo#N'). Use `task find \"<query>\"` to locate the real ticket, or "
#          "`task new` to create one.")
```

`ValidationResult` is truthy/falsy on `.ok`, so `if not validate(code, repo_root): ...`
reads naturally.

## Why "no task.yaml" is not itself a failure

task-cli works with **zero** config files on an ordinary GitHub repo — it defaults to
`backend: github-issues` and resolves `github.repo: auto` from the git `origin` remote.
This library matches that: an absent `task.yaml`/`rig.yaml` falls through to the same
zero-config default, not a rejection. The actual "no config" failure case is
**unresolvable** — a `github-issues` repo with no git `origin` (so `auto` has nothing to
resolve), or a `linear` repo with no configured team — mirroring task-cli's own error
messages for those cases.

## Scope

Deliberately narrower than task-cli's full config cascade: only the two layers that decide
"what does a real code look like for this repo" (`rig.yaml`'s `task:` block, the repo's own
`task.yaml`) are read — not task-cli's global `~/.config/task-cli/config.yaml` layer or its
`--config`/env-var overrides, which are machine-local and orthogonal to "is this a real,
committed-repo-shaped ticket reference".

The `linear` backend validates the team-prefix shape only, not live existence against the
Linear API — unlike GitHub there is no already-trusted, ubiquitous CLI in this ecosystem to
shell out to for that (the way `gh` is used here), and a Linear API key is not guaranteed
present on every machine that runs this validator.

## Public API

| Symbol | Purpose |
| ------ | ------- |
| `validate(code, repo_root) -> ValidationResult` | the entry point; never raises |
| `ValidationResult` | dataclass: `ok`, `code`, `canonical`, `url`, `backend`, `reason` (truthy on `.ok`) |
| `load_repo_task_config(repo_root) -> RepoTaskConfig` | just the backend/repo/team resolution, for callers that want it standalone |
| `RepoTaskConfig` | dataclass: `backend`, `github_repo`, `linear_team`, `sources` |
| `TaskRefError` | raised only by `load_repo_task_config` (a `ValueError` subclass); `validate()` catches it internally and returns `ok=False` instead |
| `VALID_BACKENDS` | `frozenset({"github-issues", "linear"})` |

## Why stdlib-only at import time

`yaml` is imported **lazily**, inside the config loader — importing `agenttools_taskref`
does not require PyYAML. `gh` is invoked as a subprocess only when actually validating a
`github-issues` candidate; its absence degrades to a clear `ok=False` result
("gh CLI not found on PATH"), never a crash.

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the
`agenttools-taskref` distribution — same layout as `agenttools_config`,
`agenttools_hatch_escalation`, etc.

```sh
pip install -e /path/to/agent-tools/lib/agenttools_taskref      # editable install
```

```toml
# pyproject.toml of a consumer (e.g. review-cli)
[project]
dependencies = ["agenttools-taskref"]
```

# agenttools-dev

`agenttools-dev` installs the `dev` CLI: a small project-scoped front door for common
development actions that agents need to run without widening permission to arbitrary shell
or process control. It covers named `rig.yaml` scripts plus configured dev server and e2e
job lifecycle commands.

It is stdlib-only at import time. Reading `rig.yaml` uses `agenttools_config` lazily inside
`dev run`, so `dev --help` and shell completion do not import PyYAML.

## Commands

```bash
dev start <target> [-- <args>...]
dev run <script> [-- <args>...]
dev has-script <script>
dev list
dev status [target]
dev logs <target> [--tail N]
dev e2e run <target> [-- <args>...]
dev e2e status <target>
dev e2e logs <target> [--tail N]
dev e2e stop <target>
dev stop <target>
dev stop --pid <pid>
dev stop --port <port>
dev stop --pgid <pgid>
dev env --add-project <path>
```

## Config shape

Keep ordinary named commands in top-level `scripts:`:

```yaml
scripts:
  test: uv run --with pytest pytest tests/
  typecheck: uv run mypy .
  server: pnpm run dev
  e2e: pnpm exec playwright test
  e2e-smoke: pnpm exec playwright test --project=chromium
```

Use the small `dev:` section only when lifecycle metadata matters, such as a server port or
an e2e job that can be started/listed/stopped. The command strings still live in top-level
`scripts:`; `dev:` references them by name and adds metadata.

```yaml
dev:
  server:
    script: server
    url: http://localhost:5173
    ready_url: http://localhost:5173
    ports: [5173]
    process_matchers: ["pnpm run dev", "vite"]
    logs_root: .dev/logs/server
  e2e:
    script: e2e
    requires_server: true
    artifacts_root: test-results
    logs_root: .dev/logs/e2e
    jobs:
      smoke:
        script: e2e-smoke
        requires_server: true
        artifacts_root: test-results/smoke
        logs_root: .dev/logs/e2e-smoke
```

`dev.start/list/stop` commands are intentionally limited to development/e2e runners. Obvious
destructive command heads such as `rm`, `kill`, `git reset`, or `git clean` are refused; keep
those outside `dev`. Docker-based e2e runners such as `docker compose up e2e` are allowed.

### `dev run`

`dev run <script>` locates the current repo root, reads the merged `rig.yaml` cascade via
`agenttools_config`, and executes the named top-level `scripts:` entry from the repo root.
If no top-level script exists with that name, it can run an e2e target by resolving
`dev.e2e.script` or `dev.e2e.jobs.<name>.script` back to top-level `scripts:`. Script
commands pass through the same development/e2e safety validation as lifecycle
commands, so destructive raw shell stays outside `dev`. Project-local shell wrappers such as
`bash scripts/test.sh` are allowed; shell `-c` payloads are recursively checked so wrappers like
`bash -lc 'npm test'` can run while `bash -lc 'rm -rf .'` is refused.
Inline redirection and command substitution are refused in `scripts:` commands; put complex
logging or shell composition into a project-local wrapper script and invoke that script.

Script values can be either a string command or a mapping with `cmd:`:

```yaml
scripts:
  test: uv run --with pytest pytest tests/
  web:
    cmd: npm run dev
```

Extra args after `--` are shell-quoted before they are appended:

```bash
dev run test -- -q "tests/unit path"
```

Missing scripts or invalid script config exit `2` with an actionable error.

`dev has-script <script>` is a quiet existence check for portable shell hooks. It uses the same
merged rig.yaml loader as `dev run`, so hooks do not parse YAML with ad hoc shell code.

The agent orchestration hook is narrower than the CLI: common test/e2e names such as
`dev run test`, `dev run e2e`, and `dev run smoke` are treated as orchestration, while other
script names remain implementation-shaped and may warn/block on repeat. The CLI still supports
those scripts for direct use.

### `dev start`, `dev list`, `dev status`, and `dev logs`

`dev start <target>` starts a configured `dev.server` target (named `server`), `dev.e2e`
(named `e2e`), or `dev.e2e.jobs.<target>` in
the background from the repo root, records its pid/process group under Git's private path for
the current worktree, and prints the pid. Extra args after `--` are shell-quoted and appended.

`dev list` prints configured targets and any recorded running/stale pids.

`dev status [target]` reports configured/running/stale state. For e2e jobs with
`artifacts_root` and `logs_root`, it reports the latest artifact/log path it can find. This
covers the common progress checks agents otherwise
performed with raw `ps`, `kill -0`, `lsof`, `ls -td run-grp-*`, `grep playwright.log`, and
`cat exit-code`.

`dev logs <target>` prints the configured target log, usually from the latest
`logs_root` directory. Use `--tail N` for a bounded tail. Configured `logs_root` and
`artifacts_root` paths must resolve inside the current repo or `DEV_PROJECT_PATHS`.

`dev stop <target>` uses the recorded pid for that target. If a server has no recorded pid
but declares `ports:`, it resolves pids from those listening ports and then applies the same
validation as `dev stop --pid/--port`.

### `dev e2e`

`dev e2e run/status/logs/stop <target>` are first-class aliases for configured
`dev.e2e` / `dev.e2e.jobs.<target>` jobs. They exist so e2e lifecycle commands can be allowlisted as
`dev:*` while raw Docker/Playwright/process probing remains behind the CLI.

### `dev stop --pid/--port/--pgid`

`dev stop` resolves a process by pid or by listening TCP port, then validates both:

- the process command looks like a development tool (`npm`, `pnpm`, `yarn`, `bun`,
  `vite`, `next`, `webpack`, `tsx`, `node`, `python`, `uv`, `pytest`, `cargo`, `go`,
  `make`, `docker`, and similar runners);
- the process cwd points inside the current repo root or an explicitly allowed extra project root.
  If cwd cannot be inspected, `dev` refuses to stop the process rather than trusting arbitrary
  path-looking argv tokens.

Only after both checks pass does it send `SIGTERM`.

`--port` uses `lsof` to map a listening port to a pid. `--pid` uses small `ps`/`lsof`
helpers to inspect the process command and cwd. `--pgid` validates visible processes in the
process group before sending SIGTERM to the group; as with any process-group signal, a process
could join between the final inspection and signal delivery, so use it for project-owned dev/e2e
groups rather than arbitrary shared groups. Validation also applies when stopping a named target,
and targets started by `dev start` are stopped by recorded process group.

## Log-derived scope

The current scope is shaped by Hyperide Claude permission-log mining:

- Covered: validated cleanup of dev/e2e workloads by pid, listening port, configured target,
  or process group.
- Covered: dev/e2e progress and status through configured state, latest run directories,
  log paths, and exit-code files.
- Covered: test and e2e execution through `dev run <script>` and `dev e2e run <target>`.
- Implementation detail only: raw `pgrep`, `ps aux | grep`, `lsof`, `kill -0`,
  `docker logs`, `docker inspect`, artifact-directory discovery, and log greps. These belong
  behind `dev status`, `dev logs`, and `dev stop`, not in the agent allowlist.
- Deliberately outside `dev`: `gh ship --skip-ci`, raw `gh pr merge`, force-push,
  destructive `git reset`/`git clean`/`git stash`, review/visual/ticket gate bypass env
  flags, external issue/comment creation, detached autonomous `codex exec`, and launchctl
  service restarts.

### Multi-project sessions

`DEV_PROJECT_PATHS` is an `os.pathsep`-separated list of extra project roots allowed for the
current session. Relative entries are resolved from the current repo when `dev stop` runs.

Agents should set this only when the user explicitly asks to work across multiple projects.
There is no persistent global config.

`dev env --add-project <path>` prints a shell export line instead of mutating the parent
environment:

```bash
eval "$(dev env --add-project ../api)"
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success |
| script exit | `dev run` returns the script command's exit code |
| `2` | invalid args/config, missing script, unsafe stop target |
| `127` | required platform helper/dependency missing |

## Installing locally

```bash
pip install -e /path/to/agent-tools/lib/agenttools_dev
```

For ad-hoc use from this checkout:

```bash
uv run --with /path/to/agent-tools/lib/agenttools_dev dev --help
```

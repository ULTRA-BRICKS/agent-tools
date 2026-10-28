# server-offload-gate

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 37 (just before
`heavy-op-memory-gate`) · **Applies to:** every session, subagents included

In a repo that says heavy work runs on the server, a whole test suite, a tree-wide `mypy` or a
docker build does not run on the workstation. The hook blocks the whole-suite shape locally and
hands back the one-line `server-run` equivalent.

## Why

On 2026-10-10 and 2026-10-11 the shared Linux workstation (48 GB RAM, 16 GB swap, many omp agent
sessions in parallel) thrashed to a manual reset twice. 10-10: one session's subagent swarm ran
dozens of concurrent `bun test ./tests` full suites plus `review diff`. 10-11: three concurrent
`review diff` runs, `uv run mypy` + `pytest` with testcontainers docker and several `bun test`
runs lost 26 GB in two minutes on an already-full swap; the kernel OOM killer never fired. The
projects' `AGENTS.md` files already said *"Heavy work runs on the server: locally a single test file
at most; full suite / mypy / integration tests go to the server via `server-run`"* — and the
agents ran the suites anyway. The doc was advice; this hook is the rule.

## When it fires

Only in a repo that opts in: an `AGENTS.md` or `CLAUDE.md` at the cwd or any parent directory
contains `server-run` or the phrase `Heavy work runs on the server`. Elsewhere it never blocks.

| Shape | Blocked | Still allowed |
| --- | --- | --- |
| `pytest`, `uv run pytest`, `python -m pytest`, `poetry run pytest`, `coverage run -m pytest` | no path; a directory (`tests/`, `tests/integration`); more than two files | one or two files (`tests/unit/test_x.py`, `tests/a.py::TestFoo`) |
| `scripts/check.sh` | always (a repo's whole check pipeline) | — |
| `mypy` | no file; a directory; more than one target; `-p`/`-m` package runs | exactly one file (`mypy src/pkg/mod.py`) |
| `bun test`, `vitest [run]`, `jest` | no file; a directory | one or more files (`bun test tests/x.test.ts`) |
| `npm`/`pnpm`/`yarn`/`bun` `test`, `t`, `run test` | always (the package's whole test script) | — |
| `cargo test` | without `--test NAME` | `cargo test --test integ` |
| `go test` | no package, or any `./...` | `go test ./pkg` |
| `docker build`, `docker buildx build`, `docker compose up`, `docker-compose up` | always | `docker ps`, `docker compose down` |

`--help` / `--version` / `--collect-only` are never blocked. The operand check uses the event's
cwd: a path that exists is judged by what it is; a path that does not exist here (another
checkout, a `cd` earlier in the chain) counts as a directory when its last component has no
extension (`tests/unit`), as a file otherwise (`tests/unit/test_x.py`).

## The message

```
Blocked (server-offload-gate): this runs pytest on a directory ('tests/': a whole tree of tests)
on the workstation, and this repo's AGENTS.md says heavy work runs on the server (/home/u/repo/AGENTS.md).
… Run the same command on the server instead — same checkout synced, output streamed back, exit code returned:
  server-run -- bash -c 'uv run pytest tests/'
(`server-run --dry-run -- …` prints the plan; …) Locally, ONE test file is fine …
```

The original command is quoted verbatim with `shlex.quote`, so a `cd x && A && B` chain lands on
the server as one `bash -c` script.

## Detection is argv-aware

Same duplicated tokenizer as `no-long-inline-process` / `block-no-verify` (each hook is a
standalone script): the command is shell-tokenized with quotes, comments and heredocs honored,
split into segments at `&&`, `||`, `;`, `|`, newlines and subshell/substitution breaks, and each
segment's real command is found after peeling leading shell keywords, inline `VAR=val`, no-op
wrappers (`timeout 600`, `env CI=1`, `nice`, `time`, …) and launchers (`uv run`, `python -m`,
`poetry run`, `npx`, `bunx`, `npm exec`, …; a launcher's `-c "…"` shell string is classified as
a nested command line). A runner name inside a quoted argument to another command — `git commit
-m "run pytest tests/"` — is one token and never matches. An unparseable command (unbalanced
quotes) allows: fail-open, like the descriptor's `on_error`.

## Escape hatch

The same deny-by-default Telegram hatch every hard-block hook in this catalog uses:
`RIG_HATCH_REQUEST_SERVER_OFFLOAD_GATE="<written justification>"` as an inline prefix on the
command asks the human live and proceeds only on a real-time approval. A blank value, a bare
`1`/`true`, or a single token like `pytest` is rejected without a Telegram call. No env flag
disables the gate.

## Relation to the other memory hooks

- `heavy-op-memory-gate` (priority 38, runs right after this one) judges the machine's *current*
  memory, swap, pressure and the count of heavy runners already live — regardless of repo.
- `no-long-inline-process` (35) keeps long processes off the orchestrator's main thread.

This hook is the only one that knows a repo has somewhere *better* to run the work.

## Test

```bash
chmod +x server_offload_gate.py
echo '{"args":{"command":"uv run pytest tests/"},"cwd":"/path/to/opted-in/repo"}' | ./server_offload_gate.py; echo " exit=$?"
# → block, exit=10, message ends with the server-run one-liner
echo '{"args":{"command":"uv run pytest tests/unit/test_x.py"},"cwd":"/path/to/opted-in/repo"}' | ./server_offload_gate.py; echo " exit=$?"
# → allow, exit=0
echo '{"args":{"command":"uv run pytest tests/"},"cwd":"/tmp"}' | ./server_offload_gate.py; echo " exit=$?"
# → allow, exit=0 (no AGENTS.md opt-in above /tmp)
```

Unit tests: `uv run --with pytest python -m pytest tests/test_server_offload_gate.py -q`.

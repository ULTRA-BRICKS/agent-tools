# Changelog

## Unreleased

### Added

- `decision-request-discipline`: effort saved is never an option's pro (no do-nothing /
  leave-as-is options padded with "zero work"), and keep-or-close of still-needed work is
  not a decision under a "finish everything" standing goal — finish it, split if large.
- `decision-request-discipline`: documents what tg ≥ 1.64.0 enforces (tg-cli#706): the
  Glossary section is required (the copy-pasteable example now carries one and passes
  `tg --dry-run`), and the intent gate checks UNTAGGED agent messages too — a prose question
  is blocked, a report that also asks something is sent but owes the question as a
  `--reply-to` decision reply (`tg obligations`), a decision+problem+report mix is blocked
  unless the message is already a well-formed decision request (then it is all that
  decision's context — the skill says to keep real blockers/reports out of it yourself).
- `block-raw-ticket`: Forgejo/Gitea and raw-API coverage (UB-25). With task-cli armed, the
  guard now also blocks `fj issue` / `fj repo labels` writes, `tea issues` / `tea comment` /
  `tea label` writes, REST writes to `repos/<o>/<r>/(issues|labels)` via curl, wget, HTTPie
  or `gh api` on any host (github.com included), `gh api graphql` issue/label mutations, and
  inline Python/Node programs making those writes — through `bash -c`, `eval`, heredocs,
  `sudo`/`doas`/`xargs` and `uv run`-style runners. Explicit GET/HEAD stays a read; an
  unresolvable method counts as a write. New deny-by-default
  `RIG_HATCH_REQUEST_BLOCK_RAW_TICKET` hatch (one ask per command, `timeout_ms` 960000);
  unparseable writes and junk-title creates are never hatchable. Pre-tool text guard, not a
  firewall — residuals are listed in the hook README.
- `agenttools_hatch_escalation`: display-only `redact_for_display` masks credential values in
  the Telegram approval preview and the `overrides.log` audit line (redacted before
  truncation). Approval identity stays bound to the original command and cwd.
- `gh ship --subject <text> --body <text> --author-email <email>`: set the merge commit's
  message and author instead of the provider default (a GitHub squash otherwise joins every
  commit message of the PR). Forgejo takes subject/body and refuses `--author-email`.

- `gh ship`: the machine-only Rig policy key `ci.items.ship.external_review_evidence_owners`
  (agent-tools#879). For a listed owner, a PR with zero GitHub-side reviews passes the
  external-review gate without the Telegram hatch when CI is green (or the hatch-gated
  `--skip-ci` path), the review quorum was met, the acceptance gate fully accepted the ticket,
  and there are no unresolved review threads or unaddressed PR comments; the decision runs after
  the acceptance gate and is audited as `external-review:authorized:evidence`. Any miss is
  listed and falls back to the unchanged hatch-or-refuse path.

- `skills/universal/runpod-offload`: heavy work (a full test suite, a build, a benchmark, ML
  or media batches) runs on a Runpod pod, not on the shared workstation (agent-tools#865).
  The skill ships `runpod-run [--cpu FLAVOR-VCPUS|--gpu ID] [--ttl MIN] -- <command>`:
  it reuses or creates a pod named `rr-<host>-<repo>` (quoting the live hourly price first;
  a CPU pod's default disk respects Runpod's 10 GB/vCPU cap), rsyncs the checkout minus
  everything git ignores, bootstraps the toolchain the lockfiles ask for (bun/node matching
  the local version, uv always), runs the command with a timeout streaming its output,
  exits with the remote exit code, and terminates the pod on any exit (EXIT trap) unless
  `--ttl` (a bare `--keep` without a deadline is refused). `--stop` only touches pods that
  prefix created; `--status` lists them, then every other pod on the account, each with $/h
  and age, plus the account total; `--dry-run` prints the plan offline; a missing
  `RUNPOD_API_KEY` exits 2 before any network call.
  Provisioned at `~/.agents/skills/runpod-offload/runpod-run`.

- `rig-detached-{omp,codex,opencode,claude-code}` hand the caller's forum topic to the detached
  child (agent-tools#848). Before detaching, each launcher runs `tg --session-topic` (tg-cli#553)
  and exports a numeric answer as `TG_TOPIC`, so the reparented child's `tg` calls and the
  task-cli `[task created]` notifier post into the starting session's topic instead of General.
  No session, no `tg`, a failing or older `tg` sets nothing and the launch is unchanged.

- `skills/universal/rig-detached-claude-code`: the Claude Code member of the detached-agent
  launcher family (agent-tools#831). `rig-detached-claude-code <name> <brief-file> [workdir]`
  starts `claude -p --permission-mode bypassPermissions -- <brief>` nohup-detached in its own
  session with `RIG_AGENT_ID`/`RIG_DETACHED_AGENT=1`, a private log in
  `$RIG_AGENT_LOG_DIR` (default `/tmp/agent-logs`), and returns at once with the launch line —
  the same contract as `rig-detached-{omp,codex,opencode}`, provisioned by rig to
  `~/.agents/skills/rig-detached-claude-code/`. `lib/cc_hook_bridge` now restores
  `args.agent_id` from those markers when Claude Code sends no top-level `agent_id`, so every
  subagent-aware hook classifies the detached session as a subagent, and the claude-code
  delegation recipes in `background-subagent-gate`, `orchestrator-stays-thin` and
  `no-long-inline-process` name the launcher for a child that must outlive the session.

- `lib/agenttools_gpu_slots`: a machine-wide GPU-model slot pool shared by every agent and
  harness (agent-tools#817). `RIG_GPU_SLOTS` (default 2) `flock` lock files in
  `~/.local/state/agent-tools/gpu-slots`; the kernel frees a slot the instant its holder dies,
  SIGKILL included. `python3 <agent-tools>/lib/agenttools_gpu_slots run -- <cmd>` waits for a
  slot (default up to 30 min, exit 75 on timeout) and then execs the model command holding it;
  `status` lists the holders. stt-cli acquires slots with the same protocol.

- `agent-hooks/package-store-guard` (pre-bash, every harness): blocks wiping a shared package
  store (`bun pm cache rm`, deleting/moving `~/.bun/install/cache` or an ancestor, `uv cache
  clean`, `uv cache prune --all`/`--ci`, deleting `~/.cache/uv`), creating a symlink named
  `node_modules`, and `npm install`/`npm ci`/`pnpm install`/`yarn` where the nearest lockfile is
  `bun.lock`/`bun.lockb`. Each block names the replacement (`bun install --frozen-lockfile`,
  `rig worktree gc`). It also blocks installing or starting the banned container VM runtimes
  (`colima start`, `limactl start`, `brew install colima|lima|podman|orbstack|docker-desktop`,
  `nix run nixpkgs#colima`), pointing to Apple `container` / `mocker` and the native
  fake-gcs-server backend, and `lefthook install`/`husky`/`simple-git-hooks` while
  `core.hooksPath` points outside the repo (a postinstall `lefthook install` replaced the global
  pre-commit composer on 2026-09-26). Its `blurb.md` adds the SessionStart rules that disk
  cleanup never wipes package-manager caches or stores, that hook managers stay repo-local, and
  that colima-style VMs stay off the machine.

- `ci/ship`: Forgejo provider (UB-25). `gh ship` now ships a PR from a checkout whose `origin` is
  a Forgejo host instead of silently reading the wrong remote. A gh-shaped adapter
  (`ci/ship/forgejo_provider.py`) feeds the unchanged gates from the Forgejo API, pins the PR head
  and merges with `head_commit_id`, binds credentials to the origin host, and refuses every call
  it cannot back with Forgejo evidence. See `ci/ship/README.md` "Forgejo provider".
- `ci/ship`: `--task-config <path>` routes the ship's ticket through task-cli's standard
  `--config` for the acceptance gate and the post-merge notify (UB-25: a Tasks-app ticket shipped
  from a Forgejo repository). A missing file, and any acceptance result that cannot be evaluated,
  refuses the merge.
- `ci/ship`: `--known-flake` works on Forgejo from read-only Actions evidence (UB-25). A check's
  identity is the server's workflow id plus job name; base evidence comes only from the exact
  base branch head, newest run per workflow and newest attempt per job. The predicate and the
  local gate are unchanged; missing or malformed evidence refuses.

### Changed

- `agent-hooks/orchestrator-stays-thin`: the block/warn message is five structured lines —
  `TRIGGER` (the exact rule that fired, quoting the offending segment(s): `chain of 4 segments;
  unrecognized heads: …`, `implementation-shaped segment: …`, `heredoc …`, `code write to …`),
  `WHY` (one sentence), `DO THIS` (the firing harness's delegation recipe plus what is allowed
  inline), `EXCEPTION` (the deny-by-default Telegram hatch) — replacing a single ~900-char
  paragraph that never named the rule, so the supervisor session guessed and reworded a pure
  orchestration command (agent-tools#861). Logic: `omp-steer` (any args), `tg-ctl
  status|inbox|audit-delivery|ask|omp-live status|send` and read-only `rig`
  (`status`, `doctor` without `--yes`/`--fix`/`--optional`, `sysmon status|sample|incidents`,
  `--version`) are sanctioned orchestration via shlex predicates built like
  `_is_tg_command`, deny-by-default tables for the mutating subcommands; `tr`/`read`/`printf`/
  `test`/`[`/`seq`/`diff`/checksums join the read-only filters and a print-only `awk` (no `-f`/
  `-i`/`-e`, no `system(`/`|`/`>` anywhere) is read-only like `sed -n`; `for`/`do`/`done`/`while`/
  `until`/`if`/`then`/`elif`/`else`/`fi` are segmentation keywords stripped before
  classification and never counted as chain steps (so a loop over sanctioned heads is one
  orchestration line, and `do git commit` is recognized as a commit instead of hiding behind
  `do`; only a plain-file redirect may trail `done`/`fi` — a process substitution `done >(cmd)`
  runs `cmd` and stays a judged segment, as does each live `$(…)` inside a `for … in` list —
  `for x in $(rm -rf y); do echo ok; done` blocks). New rule, closing the per-segment analogue of the
  agent-tools#363 `gh ship 205; rm -rf /`
  gap: a carve-out segment (`tg`/`task`/`omp-steer`/`tg-ctl`/`rig`/`review`/`dev`/read-only `gh`)
  chained with a segment the allow-list does not recognize is refused at any length —
  `omp-steer 1 x; rm -rf y` and `tg done && python3 deploy.py` now warn-then-block at 2 segments
  (the inner of a live `$(…)` beside a carve-out counts as a companion too). `ps`/`pgrep`/
  `pstree`/`top`/`ss`/`lsof` join the read-only inspection heads (the supervisor's subagent
  process checks were blocked at 10 segments). Pre-write: a `write` to an omp harness URI
  (`agent://`, `proc://`, `xd://`, `mcp://`, … — the #828 `_is_harness_uri` set) is never a code
  write, whatever the body — the orchestrator's steering note to a subagent was blocked after
  the warn tier.
  Docs/tests pin the real omp 18.8.1 subagent mechanism: the child's own Bash env carries no
  `PI_SESSION_FILE`; `omp_hook_bridge` forwards the child's `ctx.sessionManager.getSessionFile()`
  and restores `args.agent_id`, which is what exempts it.

- `cc_hook_bridge` and `codex_hook_bridge` no longer read the tg-ctl Stop-hook inbox on `Stop`
  (agent-tools#832). Claude Code and Codex are tmux-only (tg-cli#456): tg-ctl stopped queuing
  for them and archived their stranded backlog as `stranded-<stamp>.jsonl`. An unnamed cc/codex
  in an omp's cwd shares that omp's `cwd-<hash>` inbox key, so its `Stop` consumed the omp's
  hatch grant notice. A blocking stop hook still blocks with its own reason. `omp_hook_bridge`
  on `yield` is now the inbox's only consumer. `lib/agenttools_tg_inbox` 0.2.0 drops the
  Claude-only process lookup (`agent_key_for_process`, `parse_agent_name`); the key contract
  (`agent_key`, `sanitize_agent_name`, the inbox layout, the shared test vectors) is unchanged.
  Deploy order: apply this only after tg-cli 1.54.26 (tg-cli#456) is deployed. An older tg-ctl
  still queues for an out-of-tmux cc/codex, and nothing would consume those entries.

### Fixed

- `ops/mem-sentinel` (#884): the Linux per-omp-session memory watchdog moves under version
  control (`sudo ops/mem-sentinel/install.sh` installs `/usr/local/sbin/mem-sentinel` and its
  unit) and stops its false alarms. A session's footprint is now anon + shmem + swap from
  `memory.stat`, not `memory.current`, so reclaimable page cache no longer counts. Session
  steers fire only while MemAvailable is below 25 % or falling at 6 GB/min (the critical
  broadcast near earlyoom's threshold is unchanged); a low-swap steer goes to the session that
  holds the most swap, not the largest one. The kill advice names the heaviest child process
  by anon RSS and never the session's own omp root; the message shows anon and page cache
  separately and states its reason codes; an identical repeat steer waits 15 min instead of 3,
  and a session quiet that long starts again at warning #1. Steers go to the exact pid and
  sessionId of the scope's outermost omp (`tg-ctl omp-live send --session`; `omp-steer <pid>` also
  matched other sessions' cwd), never without a discovered sessionId, and a new recipient starts
  without the previous one's cooldown. Session discovery runs off the sampling thread so a hung
  `tg-ctl` no longer stalls the sampler. The unit drops `After=multi-user.target`, an ordering
  cycle with its own `WantedBy=multi-user.target`.

- The three `ship` Telegram hatches — `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM`,
  `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW`, `RIG_HATCH_REQUEST_SHIP_SKIP_CI` — now send their
  `tg-ctl ask` as a critical ButtonRequest (`"critical": true`, agent-tools#873). They used the
  lib's default non-critical mode, so tg-ctl's own 115 s `ASK_TOTAL_TIMEOUT_MS` expired the ask
  while the ship was still waiting and a later Approve tap landed on nothing. Critical mode lifts
  that daemon-side bound, drops the local wait bound, and makes a late Approve mint a grant a
  retry can redeem with `RIG_HATCH_REQUEST_SHIP_<GATE>="approved:hgr-…"`.

- On Forgejo, `ship` deletes a merged PR's head branch through the API, with the same credential
  that merged it (agent-tools#860). It used to run `git push origin --delete`, which needs a
  separate git credential. Without one the branch stayed behind silently, with no audit line
  (tg-cli PR #513). The branch is now kept, with the reason printed, if it is a fork head, the
  default/base branch, protected, has a tip that is not the merged head, or is still the head or
  base of another open PR. The merged head is the head the merge actually took: the pinned
  `head_commit_id`, or `refs/pull/<n>/head`. It is never the PR payload's `head.sha`, which Forgejo
  keeps at the branch's live tip. Every outcome goes to `SHIP_AUDIT_FILE` as `gate:"branch-delete"`.
  `python3 ci/ship/forgejo_provider.py prune-merged [--apply]` sweeps the existing backlog. It is a
  dry run unless `--apply` is given, and it only deletes branches whose tip is exactly a merged
  PR's merged head. Before each decision it re-reads the branch and the open PRs. Under `--apply`
  every outcome is audited with `source:"prune-merged"`.

- `worktree-only-writes` no longer refuses omp harness URIs or write-free `eval` cells from a
  main-checkout session (agent-tools#828). `write agent://<id>` / `agent://all`, `local://…`,
  `proc://<id>/kill`, `xd://report_issue`, `artifact://`, `skill://`, `history://` and `mcp://`
  were joined onto cwd as relative paths (`<main>/agent:/all`) and refused as main-branch
  writes, so agents started in a main checkout could not message peers, save reports or stop
  their own background jobs; every `eval` cell with no scraped file write was refused the same
  way. Both are now allowed. Real file writes stay blocked, including a relative path that
  contains a colon, any other or mis-cased scheme, a URI with a `..` segment, the
  file-rewriting `xd://ast_edit`/`xd://resolve`, and an `eval` write to a main path or to an
  unresolved target. `orchestrator-stays-thin` stops counting the same harness URIs as code
  writes, so an orchestrator messaging its subagents (`write agent://all`) is no longer
  warned and then blocked.

- Telegram hatches (`agenttools_hatch_escalation`, every hook and every `ship` gate) find tg-ctl
  on Linux and Cygwin, not only on the owner's Mac (agent-tools#852). The trusted list was three hardcoded paths
  (`/Users/ultra/.files/bin`, `/usr/local/bin`, `/opt/homebrew/bin`), so a `~/.local/bin/tg-ctl`
  install was refused with "tg-ctl is not available at a trusted executable path". Now: the
  machine-global rig config's `tg_ctl.tg_ctl_path` (`~/.config/rig/config.yaml` under the
  `getpwuid` home; replaces `agent_hooks.tg_ctl_path` in `~/rig.yaml`, which rig's schema
  rejects), then `~/.local/bin`, `~/bin`, `~/.files/bin`, `~/.linuxbrew/bin`,
  `/opt/homebrew/bin`, `/usr/local/bin`, `/home/linuxbrew/.linuxbrew/bin`, `/usr/bin`
  (`tg-ctl.exe` and `C:\…` config paths on Cygwin). Each hop is followed by hand and every
  directory and the binary must be owned by you or root and writable by nobody else
  (group-write only for your user-private group, gid 0, macOS `admin`, Cygwin
  Administrators/SYSTEM); a binary inside the checkout the agent works in (or its primary
  worktree) is refused. A denial now lists every path searched and why it was passed over.

- `ship`'s Rig machine external-review policy works under `python3 -I` (agent-tools#852). It imported PyYAML,
  which `-I` hides when it lives in user site-packages, so the policy silently stayed
  "enabled"; it now reads the config with the hatch lib's PyYAML-free `machine_config_mapping`.
  A `~/.config/rig` left group-writable by umask 002 is accepted when the group is your private
  group, and when an existing policy is not applied ship prints why instead of discarding it.

- `agent-hooks/require-review-before-commit`: an exported-empty `REVIEW_MARKER` no longer passes
  the commit gate without a review (agent-tools#825). It resolved to `Path('.')`, the current
  directory, whose mtime any file write refreshes; an empty value now means the default marker,
  as `${REVIEW_MARKER:-…}` does in the documented `touch`. A marker path naming a directory, a
  FIFO or anything else but a regular file never counts as a review, and a symlink loop or a
  path through a regular file blocks like a missing marker. A marker that cannot be stat'd for
  another reason (permission, I/O) still fails open.

- `ci/ship`: the review-quorum gate counts only reviews recorded in the PR's own repository
  (agent-tools#824). It used to send `--no-verify-identity`, so every repository's iterations for
  a task code counted: sme-archiving-gc's `#464` passed with consistency and security roles that
  came from rig-cli and review-cli records stored as `464`. Ship now asks `review task <code>
  --check -C <repo root> --verify-repo-only` (review-cli#589), which compares the repository
  alone. The primary checkout's own unrelated diff no longer matters, and reviews recorded before
  a repository moved hosts still count. Ship names the iterations it dropped as another
  repository's (repo and timestamp, the first 50 plus the full count). Three setup errors refuse
  outright, with no capacity-outage auto-proceed and no hatch: review-cli not reporting
  `identity_verification: repo_only`, a review-cli without `--verify-repo-only` (upgrade it), and
  a `--repo`/`GH_SHIP_REPO`/`GH_REPO` target other than the checkout's own repository (compared
  as `owner/repo`, ignoring case, URL or SSH form, an SSH URL's port and `.git`). The old
  `--min-roles`-less and `--quorum-check` retries are gone.
  `ci/ship/README.md` no longer says `SHIP_REVIEW_QUORUM=0` disables the gate; that value is
  rejected at parse (agent-tools#644).

- `agent-hooks/heavy-op-memory-gate` now sees wired-memory exhaustion and GPU model loads
  (agent-tools#817). On 2026-09-29 thirteen concurrent `whisper-cli` runs wired 95.7% of a 24 GB
  Mac into GPU buffers and kernel-panicked it while the jetsam level, the hook's only signal,
  stayed "normal". The hook now also blocks when the wired share of RAM is above
  `RIG_HEAVY_OP_MAX_WIRED_PCT` (default 75) or free + inactive + speculative + purgeable memory
  is below `RIG_HEAVY_OP_MIN_AVAILABLE_MB` (default 512), read from `vm_stat` (macOS) or
  `/proc/meminfo` (Linux). `stt`, `whisper-cli`, `whisper-server`, `llama-cli`, `llama-server`,
  the `mlx_lm`/`mlx_whisper` entry points, and any command handed a `.bin`/`.gguf`/`.ggml`/
  `.safetensors` model file are heavy, matched per simple command (loop bodies, `bash -c`
  scripts, and `<(…)`/`$(…)`/backtick substitutions included). Such a load is refused at once
  while every GPU slot is taken, unless it is queued through the slot wrapper.

- `ci/ship`: the version-bump gate reads dynamic Python versions. A `pyproject.toml` with
  `[project] dynamic = ["version"]` and setuptools `version = { attr = "pkg.__version__" }` or
  hatch `[tool.hatch.version] path` now resolves to the module that holds `__version__`, and the
  PR-diff and base-tip reads accept `__version__ = "X"` (also in a module pinned via
  `SHIP_VERSION_FILES`). Before, the setuptools form was always refused as UNCHANGED (qa-cli
  PR #23 had to ship with `--no-version-bump-ok`), and the hatch form skipped the gate entirely,
  so its source PRs merged without a bump. A dynamic source ship cannot read (a module outside
  the flat/`src/` layouts, an attribute other than `__version__`, a computed `__version__` with
  no literal `"X"`, a setuptools `file`) now refuses with the reason and a pointer to
  `SHIP_VERSION_FILES`, instead of reading the pyproject's own `version = { attr = ... }` line,
  where renaming the attr looked like a bump (auto-detected: a root package.json that declares
  the version still counts first, for polyglot repos). The resolved file must be a regular
  file inside the checkout: a PR's `path = "../.."`, absolute path or symlink cannot make ship
  stat or read files outside it. In a module only an assignment of a string literal counts (a
  `__version__ == "X"` comparison is not a bump), the value is read from that literal (a digit
  in an annotation such as `__version__: T1 = "X"` is not the version), and a PR that moves the
  version module and bumps it is matched by the file's new path. A comment inside the
  `dynamic = [...]` array (e.g. one holding a `]`) no longer hides the dynamic version and skips
  the gate. A custom hatch `pattern`, or both a setuptools attr and a hatch path, refuses as
  unreadable. Known limit: the gate matches lines, not Python, so a `__version__ = "X"` line in
  a docstring reads like the real one (parser-based reading: agent-tools#812).
- `ci/ship`: the version-bump gate no longer exits silently (exit 1, no message) when the base
  branch tip has no version value in the version file, e.g. a PR that adds its first version
  module; the base-tip check now passes as documented.
- `git-hooks/global-dispatcher/install-local-hooks.sh`: the dedup scanner strips `VAR=value`
  prefixes with one portable substitution instead of a GNU-only `sed ':a; …; ta'` loop. BSD sed
  (macOS) warned "unused label" and stripped nothing, so an env-prefixed local scan such as
  `GITLEAKS_CONFIG=x gitleaks git --staged` read as "no local secret scan" (agent-tools#810).


- `cc-hook-bridge`: one hard wall-clock budget (1100s) for all agent-hooks run on a single
  Claude Code tool call. Two hatch approval waits (960s each) in one pre-bash call used to
  exceed Claude Code's outer 1200s hook timeout, and a killed command hook fails OPEN. Each
  hook's timeout is now clamped to the remaining budget (fail-closed when clamped); once the
  budget is spent the remaining hooks are skipped and the call is blocked, overriding any
  earlier grant.
- `omp-hook-bridge`: round-3 hardening of the `eval` tool's write-call secret-scan heuristic
  (agent-tools#614). Four further security-review findings closed:
  - A JS `\u{HEX}` Unicode code-point escape that widens to a literal backslash (e.g. `\u{5c}`)
    no longer gets re-interpreted as the start of a brand-new escape sequence by a subsequent
    decode pass — decoding is now single-pass, one escape token at a time, so `\u{5c}n` decodes
    to the two literal characters backslash-then-`n`, never an actual newline.
  - A raw-string path literal (`Path(r"...")`) is no longer escape-decoded before being compared
    against `block-secrets-write`'s path allowlist — Python's raw-string semantics mean no
    escape in it is ever interpreted at runtime, so decoding it could make a suffix-anchored
    allowlist pattern (e.g. `.example$`) match text the real write never targets.
  - `write_text`/`write_bytes`'s `data=` keyword argument is now recognized regardless of its
    position among other keyword arguments, matching pathlib's real by-name keyword resolution
    (`write_text(encoding="utf-8", data=...)` is now recognized, not just `data=` as the first
    token).
  - `Bun.write`/`fs.writeFile*`/`fs.appendFile*` calls whose first (path) argument can't be
    confidently resolved to a literal now fall through to the path-less content-scraping bucket,
    the same way `Path(...)`/`open(...)` already did, instead of silently dropping the write's
    content from every scan.
- `omp-hook-bridge`: closed a total blind spot in the `eval` tool's write-call secret-scan
  heuristic (agent-tools#614) — a bare, receiver-less call to the Eval tool's own documented
  global `write(path, content)` builtin matched none of the existing patterns (all of which
  require a `Path(`/`open(`/`Bun.`/`fs.`-prefixed or dotted `.write(...)` receiver call), so
  its content never reached `block-secrets-write` at all. A new `_EVAL_BUILTIN_WRITE` pattern
  recognizes the bare builtin call while excluding any dotted receiver (`f.write(...)`, already
  handled separately) and decoy identifiers like `mywrite(...)`.
- `omp-hook-bridge`: round-5 hardening of the `eval` tool's write-call secret-scan heuristic
  (agent-tools#614). Two further security-review findings closed:
  - The bare Eval-tool builtin `write(path, content)` pattern (`_EVAL_BUILTIN_WRITE`) required
    BOTH arguments to be a recognized string literal, unlike `Path(...)`/`open(...)`, which
    already fall through to a path-less content-scraping bucket whenever the path argument
    isn't a confidently-resolved literal — a computed/unresolvable first argument
    (`write(some_computed_path_var, "API_KEY=...")`) silently dropped the write's content from
    every scan instead. A new `_EVAL_BUILTIN_WRITE_FALLBACK` pattern (mirroring round 3's
    `Bun.write`/`fs.writeFile*` fallbacks) closes it.
  - `_EVAL_OPEN_WRITE`'s permissive `,[^)]*` mode/tail (round 2) could also swallow a NESTED
    call expression sitting in that tail position — `open("foo.example",
    open("src/secret.py","w").write("API_KEY=..."))` let the tail consume up to the inner
    `open(...)` call's own closing paren, so the match's `.write(` anchor landed on the nested
    call instead, attributing the real secret to the outer call's allowlist-friendly decoy path
    `foo.example` and never scanning it under its real path (or path-lessly) at all — a genuine
    `ALLOW_PATH` allowlist BYPASS, not merely an under-scan. The tail (shared with the
    `.writelines(...)` sibling pattern, which had the identical exposure) now rejects any
    position that begins an `identifier(...)`-shaped call, falling through to no-match instead
    — `open(...)`'s existing whole-string scan then finds and correctly attributes the nested
    call on its own.

- `omp-hook-bridge`: round-6 hardening of the `eval` tool's write-call secret-scan heuristic
  (agent-tools#614). Four further security-review findings closed:
  - `_EVAL_KWARG_VALUE` (what `_EVAL_OTHER_KWARG` recognizes as another keyword argument's value
    sitting before `data=`) only matched a quoted literal, a bare identifier, or a number — a
    CALL EXPRESSION there (`write_text(encoding=locale.getpreferredencoding(),
    data="API_KEY=...")`) matched none of its alternatives, so the whole call went entirely
    unrecognized (content silently dropped), contradicting round 3's "`data=` recognized
    regardless of position" fix. A new alternative recognizes a call expression with up to one
    level of nested parens.
  - `_EVAL_OPEN_WRITELINES_LIST`'s `[^\]]*` content scan stopped at the FIRST `]`, including one
    sitting INSIDE a quoted list item's own string body — `.writelines(["]", "API_KEY=..."])`
    truncated the captured content to just the opening quote of the first item, silently
    dropping the real secret in the second item. A new `_EVAL_WRITELINES_LIST_BODY` fragment
    treats each quoted chunk atomically so an embedded `]` inside quotes no longer ends the scan
    early.
  - Python's 8-digit code-point escape (`\UHHHHHHHH`, distinct from the JS `\u{HEX}` form and
    the 4-digit `\uHHHH` form) was not recognized by `_EVAL_ESCAPE_TOKEN` as its own token — it
    fell through to the generic `\.` catch-all, which consumes only the leading `\U`, leaving
    the 8 hex digits as untouched literal text (`\U00000041PI_KEY=...` scanned as literal text
    instead of decoding to `API_KEY=...`, a secret-scan bypass). A new alternative tokenizes the
    full 8-digit escape as one unit.
  - Round 5's `_EVAL_OPEN_MODE_TAIL` guard only rejected a tail beginning with the SHAPE
    `identifier(` — a decoy like `open("foo.example", [open][0]("src/secret.py","w").write(
    "API_KEY=..."))` (subscript-then-call, `[open][0](...)`) begins with `[`, not an identifier
    character, so it wasn't rejected and the secret was misattributed to the allowlist-friendly
    decoy path `foo.example` again — the identical bypass class as round 5's own finding, via a
    different call shape. The guard is widened to reject any `(` or `[` in the tail.

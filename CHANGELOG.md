# Changelog

## Unreleased

### Added

- `agenttools-chatgpt` 0.3.4: `chatgpt-guardian --workspace W coverage --cdp-port N [--apply]` reconciles every
  ACTIVE chat of the account with enrollment and the owned browser (dry run by default; per-account
  `coverage_enabled` / `auto_watch_active` / `recovery_autostart` flags file). It reports
  enrolled / present / fresh / active / recovery-eligible separately, enrols only fresh unique running or
  `Thinking failed` chats that have no `watch:` key (a manual unwatch survives), restores missing enrolled chats
  through `startup` with a durable one-attempt-per-inventory ledger, optionally starts a FAILED (never a manually
  stopped) recovery service within bounds, and sends deduplicated Russian owner alerts through `tg --tag problem`.
  New 60-second timer + service templates take the CDP port as their parameter. `startup.restore` gained `skip`.
  `recovery_eligible` additionally requires an ACTIVE recovery service (failed/inactive reports 0) and is mirrored as
  `eligibility_for_probe` with `model_proof: "unknown"` (probe-level only, not a validated model or send).
- `agenttools-chatgpt` 0.3.5 (Refs #1020): coverage safety finish. Applied runs are serialised per account by a
  whole-run `flock` (`busy` report, released on errors; `startup --apply` takes the same lock) with a check-and-set
  restore reservation; pre/post restore inventory signatures use one predicate (enrolled chats open exactly once,
  including newly enrolled ones, never a spent login redirect); the ledger schema and scope are created in one
  transaction and an empty or scope-less empty ledger is initialised while foreign or corrupt data is refused;
  per-chat freshness also honours `check_stale`; one deduplicated owner alert reports an existing global `stop:*` hold
  without touching it; the `tg` notifier passes `--title "ChatGPT Guardian"`; cleared alerts are pruned after the
  re-arm window; the report schema is identical on early failures.
- `agenttools-chatgpt` 0.3.6 (Refs #1020): the observer no longer calls a readable DOM fresh when the conversation did
  not synchronize. `monitor_dom.js` reads the page's existing Resource Timing entries (read-only: no request, reload,
  navigation or buffer clearing) for the newest completed same-origin read of exactly this chat
  (`/backend-api/conversation/<id>` or `/conversations/<id>`, optionally followed by a query that is never read); a newest HTTP 429/401/403/5xx yields the sanitized
  observation error `conversation-sync-http-<status>` (status, reason and timing only) instead of a checked complete
  or working status, and a newer success supersedes an older failure. Other chats, query/encoded/foreign-origin
  paths, unrelated telemetry, bogus statuses and missing entries never block. An explicit app load-error page with no turns is `conversation-load-failed`. No retry, auto-clear or send-guard change.
- `agenttools-chatgpt` 0.3.7 (Refs #1020): the closed model-trigger label used by the active model probe is the exact
  rendered text a user sees: text under `aria-hidden`, `hidden`, `display:none`, `visibility:hidden` or `opacity:0`
  ancestors (e.g. the hidden `Thinking effort` placeholder) is excluded, so a full selected GPT-6 proof plus visible
  `Pro` / `5.6 Extra High` finalizes. One helper serves `menu-state` and `finalize-probe`; the label must still equal
  exactly `Pro` / `5.6 Extra High` (no prefix stripping), and the raw trigger signature still covers all text.
- `agenttools-chatgpt` 0.3.8 (Refs #1020): passive evidence is separated from mutation admission. One non-conversation
  tab (`/space?tab=all`) no longer blanks six legitimate conversations: the observer used to turn every session
  `unknown` and the sender paused globally whenever the TOTAL page count exceeded 6. Mutation admission
  (`live.resource_pause`: sends, Sol selection and `startup.restore`) now counts conversation tabs against a
  conversation budget of 6 (hard ceiling, never raised or disabled; `--max-pages` still lowers it) and every other
  page (`/space`, new tab, other chatgpt.com pages, external sites) against an explicit auxiliary budget of 2
  (`AUXILIARY_PAGE_BUDGET`), under the same MemAvailable guard; reasons are `conversation-budget`,
  `auxiliary-budget`, `low-memory`, `memory-unavailable`. A conversation URL is normalised once
  (`urls.conversation_identity`: trailing slash, query, fragment and project `/g/g-p-<id>-<slug>/c/<uuid>` variants share
  one identity); a duplicate tab counts physically and still vetoes that chat. `startup.restore` also counts tabs it
  created in the same run that are still blank or loading. The read-only observer reads up to
  `PASSIVE_CONVERSATION_CAP` = 6 distinct already-open conversations (enrolled first; one read per conversation) regardless
  of any pause (never `/space`, external or blank pages), labels snapshots with `resource_paused`, `page_count`,
  `conversation_page_count` and `auxiliary_page_count`, marks conversations over the cap `unobserved` (even if
  duplicated), and reports `summary.observation_counts` (stale and missing distinct) and `summary.coverage_complete`,
  which is never true while an enrolled chat is not current or a conversation went unread. `observer` gained
  `--max-pages` / `--min-available-mib` so `resource_paused` can mirror the live service's limits. The coverage report
  carries the same split, `observation_complete` and `resource_paused` (null = unknown). Running, draft, identity,
  approval/permission UI, HTTP 401/403/429, quota and uncertain-send vetoes, retries and auto-clear are unchanged.
- Worktree-only guard is ON BY DEFAULT and now covers commits (agent-tools#1012, Refs #1012):
  `worktree-only-writes`, `pin-primary-worktree` and `block-devserver-primary` enforce in every
  repo that has a `rig.yaml` unless it sets `agent_hooks.worktree_only: false` (or the session
  sets `RIG_WORKTREE_ONLY=0`); a directory with no `rig.yaml` is still never blocked.
  `pin-primary-worktree` additionally DENIES `git commit` (incl. `--amend`), `cherry-pick`,
  `merge` (except `--ff-only`), `rebase`, `revert` and `am` on the default branch of the PRIMARY
  checkout, follows `cd <dir> &&` / `git -C`, and all three block messages now give the exact
  `rig worktree create <feature> --from origin/<default> -C <root>` command instead of raw
  `git worktree add`. Closes the 2026-10-24 gap where garage-band (no `worktree_only` key) took
  four agent commits straight onto local main. A repo that legitimately works on main must opt out with
  `agent_hooks.worktree_only: false` in its `rig.yaml`.

- `agenttools_service` 0.3.0 (agent-tools#1005): the systemd user unit and the lifecycle verbs
  now agree. With an enabled unit `start`/`restart`/`stop`/`status` act on the unit (exactly one
  daemon; a leftover pidfile daemon is stopped first) and `status` reports the unit's real
  `ActiveState`/`MainPID` instead of "the unit file exists". New `restart` and `logs` (`-n`,
  `-f`) verbs; `Service(health_check=, ready_timeout=)` makes `start`/`restart`/`enable` wait for
  the server and exit 4 with the log path when it never answers; `enable` checks `loginctl`
  linger and turns it on (warns on stderr when it cannot); `service_checks(manager)` gives
  consumers' `doctor` generic unit/linger/`systemd-analyze verify` rows. The rendered unit drops
  `Wants/After=network-online.target`, which the user manager does not have. Behaviour change:
  `stop` with an enabled, systemd-answering unit now stops the unit (autostart stays installed)
  instead of exiting 3 with "run disable".
- `local-path-in-gh` agent-hook (pre-bash, priority 40, fail-open; agent-tools#1002): blocks `tg` / `tg report` text (not `--photo`/`--file` uploads) and `gh pr`/`gh issue` create/edit/comment/review/close/reopen/merge, `gh release create|edit`, `gh api` writes to issues/pulls/comments/reviews/releases/graphql, and task-cli `new|change|check|attach|done|comment` text and `--proof`/`--screenshot` values whose text names a local filesystem path (tmp dirs, `/home/<user>`, `/Users/<user>`, `~/`, `$HOME`, Windows drive paths, `file://`, the runtime home dir, non-http(s) link targets). A local path cannot be opened on GitHub and is gone after a reboot, so it is a fake proof. Body files, heredocs, `$(cat file)` and files written earlier in the same command are judged by content. The BLOCKED message names the flag and token (home masked as `<home>`) and says to paste the content (fenced block / `<details>`) or upload the image and link the https URL. Only bypass: `RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH` (one-time Telegram approval).
- `python-only`: exact-path Allow entries for the four Guardian browser-injected DOM adapters
  (`lib/agenttools_chatgpt/{dom,live_dom,monitor_dom,conversation_root}.js`, run by Chromium in
  the ChatGPT page; agent-tools#969, Refs #891). No glob: other JS in that package still blocks.
- `block-unlanded-removal` agent-hook (pre-bash, priority 41, fail-closed): removing a worktree or
  deleting a branch is allowed only when the work is landed and the tree is clean. A loose,
  quote-stripping prefilter (`git worktree remove/rm`, `git branch -d/-D/--delete`,
  `git push --delete/--mirror/--prune/:ref`) hands the command to `rig worktree guard --cwd <cwd>`,
  which answers pass/allow/deny; deny, or rig missing/nonzero/timeout/garbage output, blocks. No
  bypass; the deny text names `rig worktree verdict <target>` and the patch-rescue recipe.
- `ci/python-only` gate (agent-tools#945): rig-ecosystem tools are written in Python (owner
  decision 2026-06-14, restated 2026-10-17). One stdlib checker,
  `ci/python-only/python_only.py`, runs as the global pre-commit fragment `20-python-only` and
  as a CI slot. In an ecosystem tool repo (owner namespace, name `*-cli` or listed) it fails on
  added or changed TS/JS/Go/Rust/... files, node/bun scripts and shell scripts over 40 lines,
  except host-forced paths on a central allowlist with a reason. tg-cli is warn-only until
  tg-cli#768. Enabled for this repo in `rig.yaml`; AGENTS.md "Python-only tools" records the
  decision.
- `decision-request-discipline` skill: search recorded decisions (ROADMAP, ADRs, AGENTS.md)
  before offering options; never offer an option that contradicts a recorded owner decision.

### Changed

- `gh ship` Python port, structure only (agent-tools#976): `Ctx` declares every
  shared attribute with one type (no more `getattr(ctx, name, default)`); one `ctx.which`,
  `ctx.append_audit`/`audit_file`, `ctx.run_gh` and `sum_first_fields` instead of per-module
  copies; `cleanup()` loops over `plan_checkout` (decides, changes nothing) and
  `execute_checkout_plan` (prints the keep reasons, then acts); cross-module imports hoisted to
  module level; the acceptance-gate message helpers use plain isinstance checks. One deliberate
  difference: every audit writer now falls back to `~/.config/agent-tools/ship-audit.jsonl` when
  `HOME` is unset or empty (two of them used a cwd-relative path).

- `gh ship` (`ci/ship/ship.sh`) is now Python (agent-tools#946, forced by the python-only gate on
  the edit #974 needed). `ship.sh` is a short launcher for `ci/ship/ship.py` + the
  `ci/ship/shiplib/` package; flags, messages, env knobs, audit lines and exit codes are those of
  the former 4.6k-line bash script. Copying `ship.sh` alone no longer works: link it or run it
  from the checkout.
- `block-unlanded-removal` runs a PINNED rig and degrades on a stale one (agent-tools#961). The
  descriptor gains `"args": ["--rig", "/ABSOLUTE/PATH/TO/rig"]` (`rig apply` substitutes the
  launcher's absolute path); the hook never resolves `rig` through PATH, requires an absolute
  executable regular file whose `--version` prints `rig X.Y.Z` with X.Y.Z >= 0.64.0 (integer-tuple
  compare), and otherwise fails closed (denies every matched command: absent/relative/missing/
  non-executable pin, unverifiable version). The prefilter now has two classes: STRICT
  (removal-shaped) and LOOSE (only `$`/`{`/backtick next to a git word, e.g. every heredoc commit).
  A rig older than 0.64.0 (no `worktree guard`) denies STRICT with an "upgrade rig" message and
  allows LOOSE with a stderr warning, so a stale rig no longer wedges every heredoc commit. New
  STRICT shapes for rig-cli#740: `git branch` with `-f/-M/-C/-m/-c` (or `--f…/--m…/--c…`), `git
checkout -B`, `git switch -C/--force-create`, and `rm` with a recursive flag as a command word.
  Other #961 items (aliases, xargs, rescue hint) are unchanged.
- `block-unlanded-removal` deny text names the route that works for a PR closed as superseded
  (agent-tools#963): push the commits to a branch that is not being deleted
  (`git push origin <branch>:refs/heads/archive/<name>`), then retry. rig's verdict counts a commit
  held by another live remote branch as preserved (rig-cli#737); the hook only forwards rig's answer.
- `block-raw-worktree-add` no longer gates `git worktree remove`: removal safety is decided by
  `block-unlanded-removal`, so a safe removal outside `.worktrees/` is allowed. The location rule
  still applies to `add` and `move`.
- `model-freshness` checker (`lib/checker/model_freshness.py`) works on an OAuth-only machine
  (agent-tools#934). Each provider poll is merged with the keyless catalogs the logged-in
  harnesses cache — `omp models --json`, codex's `$CODEX_HOME/models_cache.json`, Claude
  Code's model-catalog cache with its `fable`/`opus`/`sonnet` alias table — and, for a
  provider none of those list, `https://models.dev/api.json`; `--json` shows each provider's
  `sources`. Every request now sends a `User-Agent`, which fixes the HTTP 403 from
  commandcode and models.dev. Anthropic's hyphenated versions are one version run, so
  `claude-fable-5` -> `claude-fable-5-1` and `claude-opus-4-8` -> `claude-opus-5-5` are
  proposed; a different variant (`gpt-5.5` -> `gpt-6.1-sol`) is not proposed but listed as a
  pin with newer variants (output, `--json` `pin_variants`, report). The checker also
  reports stale seats in review-cli's code defaults (`reviewlib/config.py`) and
  `~/.config/review-cli/config.yaml`, lists newer variants it will not choose, and writes a
  `git apply`-able patch for the code defaults (`--review-cli-patch`, and next to the report);
  the user config is only reported. New flags: `--review-cli-root`, `--no-review-cli`,
  `--review-cli-patch`.
- `lib/contracts/models.yaml` pins the 2026-10-17 catalog ids: `claude-fable-5-1`,
  `claude-opus-5-5`, `gpt-6.1-sol`, `gemini-3.8-flash`, `moonshotai/Kimi-K3` (replaces
  Kimi-K2.7-Code, has vision), `Qwen/Qwen3.8-Max` (has vision) and `glm-5.3`, with the
  catalogs' context windows; roles and `<provider>:latest` aliases follow.

- `deferred-findings-tracking` skill: new "Bugs in tools" section — every bug hit in a tool
  (especially rig-ecosystem tools: rig, task-cli, tg-cli, review-cli, agent-tools hooks and
  `gh ship`, dev-cli, smoo/nx-plugin, mem-sentinel) gets a ticket in that tool's own tracker
  right away, with repro, expected vs actual and the workaround; dedupe first and comment on an
  existing ticket; a workaround never replaces the ticket. Plus a Common mistakes section
  (agent-tools#931).

### Fixed

- `gh ship` version-bump and screenshot gates now work on a GitHub PR whose diff GitHub refuses to
  serve (HTTP 406 above 20000 lines, also for `gh pr diff --name-only`; agent-tools#1023). The file
  list comes from the paginated pull-request files API, accepted only when it holds exactly the PR's
  `changedFiles` files, the PR is within the API's 3000-file cap and the head did not move; the version
  value comes from the version file's contents at the PR head and merge-base. Any mismatch or API
  error still refuses, there is no new override, and the Forgejo provider is unchanged.
- `omp-hook-bridge` (agent-tools#1003): the omp MAIN session now runs `stop` hooks. Only a subagent's
  `yield` mapped to `stop`, so an interactive/orchestrator omp session — which ends a turn with plain
  text — never ran `stop-completion-selfcheck` (or any Stop hook): 0 of ~5,400 recorded yields were
  from a top-level session. The extension registers omp 18.8's `session_stop` event, maps it to the
  v1 `stop` point and returns `{decision: "block", reason}`; stop events carry `args.transcript_path`,
  `args.harness_session_id`, `args.last_assistant_message`, `args.turn_id`, `args.stop_hook_active`;
  `firings.jsonl` rows record the `harness`. An interrupted session is never held; every failure
  fails open.
- `gh ship` cleanup (agent-tools#995): worktrees on the PR head commit under another branch name
  or detached are removed like the named one; a seeded `task.yaml` identical to the base's no longer
  makes the tree dirty (a differing one or any other change still does, and is never forced); cleanup
  ends with `rig worktree gc --yes` for what ship cannot attribute to the PR. The `--known-flake`
  local gate now says up front that it runs the whole local suite on this machine and how to scope
  it (`SHIP_LOCAL_TEST_CMD` / `.ship-config`); documented in the ship README.
- `gh ship` no longer deletes a developer's own long-lived `git clone --reference/--shared`
  checkout after a merge (agent-tools#979). A clone is removed as a directory only when its creator
  marked it disposable (`git config --local agent-tools.disposableClone true`, read from the
  clone's own config only) on top of the existing alternates and keep-guards; an unmarked clone is
  kept and moved onto the base, and ship prints the one-line marker command. Linked worktrees are
  unaffected. Existing unmarked per-PR clones are kept until marked.
- `visual-proof-gate`: test and story files (`*.test.*`, `*.spec.*`, `*.stories.*`,
  `__tests__/`, a `test/` dir outside routing roots), plain `.ts`/`.js`/`.json` under
  `components/ ui/ pages/ app/ views/`, and `.tsx`/`.jsx` files whose staged and HEAD content
  have no JSX are no longer classified user-visible; the directory rule there now applies only
  to extensions that can render (`public/` and `assets/` stay fully gated). A component
  staged with its test is still gated. Documented that `--write-marker && git commit` in one
  call stays blocked (the hook judges the command before it runs). agent-tools#973.

- `gh ship` post-merge cleanup (agent-tools#974, Refs #925, #962, #809): the base branch is the
  PR's `baseRefName` (`SHIP_DEFAULT_BRANCH` is only the fallback), so a repo whose base is not
  `main` no longer gets `pathspec 'main' did not match`; the local base ref is fast-forwarded
  only when it exists and is safe (no checkout switch, no `--autostash`, a dirty checkout is
  skipped with a notice). After reading the PR back as MERGED with the local tip not ahead of the
  merged head, ship removes the PR's checkout and local branch even when launched from inside it,
  including a per-PR `git clone --shared` clone (previously "no separate main checkout to re-root
  into — leaving it in place" left the clone and branch behind); a dirty tree, a branch with
  commits the merge did not take, a clone holding other unpushed work (branch, stash, tag, or an
  ignored `.env`-style file a rebuild would not restore), a clone borrowing from a mirror and a
  plain clone are kept. The remote head branch of a PR still OPEN after the merge call (merge
  queue, auto-merge) is kept.
- `runpod-offload` (`runpod-run`): a reaper pass over a marker whose pid is gone no longer
  prints `/proc/<pid>/cmdline: No such file or directory` (agent-tools#910).
- `runpod-offload` (`runpod-run`): failures that billed for nothing, and staging runs
  (agent-tools#910). A pod that does not answer ssh within 3 minutes (was 10) is terminated
  and the run exits 23. A create that Runpod answers with "no longer any instances" / "no
  instances available" is retried up to 3 times, 15/30/60 s apart, with the same flavor, then
  exits 3; another flavor is never tried. `--sync-dir` is repeatable: the first is the run's
  checkout, each further one is synced the same way to `/workspace/<its repo tag>`, the plan
  and the log print `<local> -> <remote>` for each, and the command gets them in
  `RUNPOD_RUN_SYNC_DIRS`, so a second checkout no longer needs its own staging run. The reaper
  never terminates a pod whose per-pod lock a live run holds, and a `--ttl` timer's own
  `--stop` now logs `reached its --ttl deadline` (once) instead of `no timer alive (reboot?)`.

- `ci/ship`: post-merge cleanup no longer deletes a PR worktree that installed agent hooks still
  execute code from (agent-tools#905). A `rig apply` run from a worktree to dogfood a hook
  writes that worktree's path into the hook descriptors; deleting the tree left
  runpod-offload-gate (`on_error: open`) silently open machine-wide after PR #904. The new
  `ci/ship/hook_refs.py` scans every harness's descriptors, bridge commands, extension links
  and `~/.config/agent-tools/env`; a referenced (or unscannable) tree is kept and ship prints
  the references plus the fix (`rig apply commit --only agent_hooks`, re-check, `rig worktree
remove`).
- `agent-hooks/heavy-op-memory-gate`: a run of one or two test files (`bun test ./tests/x.test.ts
-t foo`, `pytest a.py b.py::T`, `go test ./pkg/foo_test.go`, also through `uv run`/`python -m`/
  `poetry run`) is gated on the emergency signals only — `MemAvailable` floor, PSI, wired
  share, jetsam level — not on the memory budget, the swap check or the live-runner ceiling
  (agent-tools#911: one test file was blocked for "7 heavy runner(s) already live"). A bare
  suite, a directory operand, three or more files, or any other heavy command in the same line
  keeps the full checks. `decide`'s `review_admission` flag is now `emergency_only`.
- `agent-hooks/heavy-op-memory-gate`: `runpod-run [options] -- <command>` is no longer treated
  as a local heavy operation — its block message told agents to use `runpod-run`, then blocked
  `runpod-run -- bun test ./tests` on the swap and live-runner checks because the remote
  command's words were classified. The exemption is per simple command (a heavy command chained
  around it, or a `$(…)` the local shell expands into its argv, is still gated), and the
  live-runner `/proc` scan no longer counts a `runpod-run` process tree. Only the installed
  script is exempt: the program must resolve (PATH lookup, or an absolute/`~/` path, after
  symlinks) to `~/.agents/skills/runpod-offload/runpod-run` or the checkout's own copy (or one
  in its registered git worktrees); a
  relative path, or a command whose local part (not what follows runpod-run's `--`) defines
  any function or alias (incl. zsh `functions[…]`), assigns PATH/zsh `path`/HOME/XDG_CONFIG_HOME,
  uses `hash -p` or runs unseen shell code (`source`, `.`,
  `eval`), gets no exemption.
- `agent-hooks/heavy-op-memory-gate`: the fixed "2 heavy runners live → block" cap is now an
  adaptive memory budget. A new heavy op is admitted iff `MemAvailable` − Σ live runners'
  max(0, expected cost − tree PSS) − its own expected cost ≥ `RIG_HEAVY_OP_HEADROOM_MB` (4 GiB);
  expected costs: docker build 8 GiB, review run 3 GiB (measured by review-cli's sampler over
  294 runs: peak p50 678 MB, p95 1125 MB, max 3195 MB), other suites/runners 3 GiB
  (`RIG_HEAVY_OP_COST_REVIEW_MB` / `_DOCKER_MB` / `_SUITE_MB`). `RIG_HEAVY_OP_MAX_LIVE` stays as a
  hard backstop, default raised from 2 to 5. The swap-% check fires only while the budget leaves
  less than 2 × headroom (2026-10-14: swap 83% full of stale pages with 37 GB available and PSI
  0 blocked an idle box), and unconditionally when the live-runner scan is unavailable (an
  unreadable `/proc` counts as unavailable). A
  review's seat CLIs now count as one runner per review, with the whole tree's PSS
  (`smaps_rollup`; RSS double-counted shared worker pages; where PSS is unreadable the largest
  single RSS in the tree stands in). The block message shows the budget
  arithmetic and each runner's reservation.
- `agent-hooks/heavy-op-memory-gate`: a `review` pass is no longer double-gated. When the
  `review` program resolves to the installed review-cli (`~/.local/bin/review`'s checkout, its
  main checkout or a registered git worktree) and review-cli itself reports admission on (the
  hook calls its `reviewlib.admission.load_settings()`, review-cli#614, with the hook's own
  environment — so a command mentioning `REVIEW_ADMISSION`, or a review prefixed with an
  assignment, `env` option or `sudo`/`env`/`su` wrapper, is not handed off), review-cli queues the run itself; the hook
  skips the budget, swap and live-runner
  ceiling for it and blocks only on the emergency signals (MemAvailable floor, PSI, wired share,
  jetsam level). Same anti-spoofing rules as `runpod-run` (`sudo runpod-run` is not exempt either);
  a command that also starts other heavy work is not handed off but gated whole at the costlier
  label, and live reviews still count in other operations' budgets.

### Added

- `slop-check` universal skill: run `slop-check` (ultrabricks/slop-cli) on English and
  Russian prose before posting issues, PR bodies, release notes, review comments and tg reports;
  how to act on findings, exit codes 0/1/2 (agent-tools#923).

- `runpod-offload` (`runpod-run`): a spend ledger and `--report` (agent-tools#910). Every
  run-mode invocation (refusals, usage errors and Ctrl-C included; not `--dry-run`) appends one
  JSON line to `~/.cache/runpod-run/ledger.jsonl`, written by the script itself so a caller
  that discards stderr still leaves it: event, repo, command, pod, flavor and $/h, the boot /
  sync / bootstrap / command / idle seconds, exit code, estimated $, `RIG_AGENT_ID`, the
  session id, the parent command line and the `--reason`s. A `--stop` (or the reaper) that
  terminates a pod kept by `--ttl` appends an `event: terminate` line with its idle time.
  `runpod-run --report [--day YYYY-MM-DD]` prints a local day's billed $ (billing API, when a
  key is present) against the ledger split into A command / B boot+sync+bootstrap / C idle /
  D failed before the command / E failed run / G unattributed, runs per repo and per agent,
  trivial (<30 s) commands and refusal counts; a day from before the ledger is read from the
  run logs.
- `runpod-offload` (`runpod-run`): a daily $ budget (agent-tools#910). Before every pod create
  and every start of a stopped pod, today's billed $ (billing API from local midnight, cached
  5 min) + every live pod's $/h × the run's `--timeout` + the new pod's $/h × `--timeout` must
  stay below the owner's cap ($6 per local day; `~/.config/runpod-run/budget` `daily_usd = N`,
  `budget-override` `YYYY-MM-DD N` for one day; `~` is the account's passwd home, so `HOME` and
  `XDG_CONFIG_HOME` cannot move the files). Otherwise, or when billing or the price cannot
  be read, the run exits 21 before any pod bills, books an `event: budget` ledger line and
  tells the agent to send the owner a decision request (`tg --tag decision --title …`, in the
  decision-request format). `--dry-run` prints the budget line;
  `--report` now reads the billed $ through the same helper.
- `runpod-offload` (`runpod-run`): at most 3 live pods per host, with a FIFO queue
  (agent-tools#910). A run takes a pod slot before it reuses, starts or creates its pod (so
  before the budget check) and holds it until the pod is terminated; a pod kept by `--ttl`
  keeps its slot until its timer ends it, and the next run of that repo reuses it without a
  new slot. Further runs queue, print `queued for a pod slot (position N of M; 3 live: …)`
  when their position changes, and exit 22 after `--queue-timeout MINUTES` (default 60) with
  an `event: queue-timeout` ledger line. A slot is a marker `~/.cache/runpod-run/slots/<pod>`
  naming its holder's pid, so a killed run's slot frees itself; `--status` lists the slots
  and `--dry-run` says whether the run would reuse a slot, take one or queue. The ledger, the
  slots and their queue, the billing cache and the budget lock live in `~/.cache/runpod-run`
  of the account's passwd home (like the budget files), so `HOME` or `RUNPOD_RUN_STATE`
  cannot give a run fresh slots or an empty ledger; `RUNPOD_RUN_STATE` now moves only
  known_hosts, run logs, `--ttl` timers and per-pod locks.
- `runpod-offload` (`runpod-run`): an identical run within 24 h is replayed, not re-run
  (agent-tools#910). Same files and contents in every `--sync-dir`, same command, same
  settings (flavor, image, `--root`, `--timeout`, bootstrap, dirs): the run prints the
  earlier run's last 40 log lines and exits with its code, with an `event: dedup` ledger
  line and no API call, slot or pod. `--rerun` runs it anyway;
  `--dry-run` prints `dedup: hit …` or `dedup: miss`. Records live in
  `~/.cache/runpod-run/dedup/` of the account home.
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
- `agent-hooks/runpod-offload-gate` (pre-bash, priority 37, on_error open, every session incl.
  subagents): in a repo whose `AGENTS.md`/`CLAUDE.md` (cwd or any parent) mentions `runpod-run`
  or `Heavy work runs on Runpod`, blocks a WHOLE suite locally — pytest (also via `uv run` /
  `python -m` / `poetry run` / `coverage run -m`) with no path, a directory, or more than two
  files; `scripts/check.sh`; mypy with anything but exactly one file; `bun test`/`vitest`/`jest`
  with no file or a directory; `npm|pnpm|yarn|bun test`; `cargo test` without `--test`; `go test
./...`; `docker build`/`docker compose up` — and names the `runpod-run -- bash -c '<command>'`
  equivalent. One test file stays allowed. Argv-aware (the sibling hooks' tokenizer; `cd x &&`,
  wrappers, inline env and launchers peeled). Hatch: `RIG_HATCH_REQUEST_RUNPOD_OFFLOAD_GATE`.
  Added after the 2026-10-10/11 thrash incidents on the shared Linux workstation, where the
  projects' AGENTS.md already stated the rule and agents ran the suites anyway.

- `agent-hooks/heavy-op-memory-gate`: Linux thresholds calibrated against the 2026-10-10/11
  incidents (48 GB / 16 GB swap workstation; MemAvailable never fell below ~1.9 GB while the box
  thrashed, so the old 512 MB floor never fired). Linux now blocks a heavy operation when
  `MemAvailable` < max(8 GiB, 20% of MemTotal) (`RIG_HEAVY_OP_MIN_AVAILABLE_MB`), PSI
  `/proc/pressure/memory` `some avg10` > 10% (`RIG_HEAVY_OP_MAX_PSI_SOME10`), SwapFree < 30% of
  SwapTotal (`RIG_HEAVY_OP_MIN_SWAP_FREE_PCT`), or when ≥ 2 heavy runners are already live
  (`RIG_HEAVY_OP_MAX_LIVE`; a stateless `/proc` scan for pytest / bun test / vitest / jest / mypy /
  tsc -b / cargo test / go test / make test|check / docker build and the claude/codex/opencode
  seats under a `review` process, each listed with pid, RSS and its omp session via
  `tg-ctl omp-live status`). The jetsam-level mapping of PSI is gone: Linux reads PSI directly.
  macOS behaviour is unchanged.

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

- `runpod-offload` (`runpod-run`): spend policy (agent-tools#910). Before any network call,
  `--dry-run` included, it refuses with exit 20 a no-op command (`true`, `:`, `exit 0`), a
  single test-file invocation (`bun test x.test.ts`, `pytest tests/a.py`, `pytest -k foo`,
  `go test -run TestX`, also via `uv run`/`python -m`/`poetry run`/`npx`/`bunx`/`pnpm exec`)
  unless `--remote-reason` says why it needs a pod, any flavor above 4 vCPU or 16 GB and any
  `--gpu` without `--reason`, and a `--gpu` run whose command shows no sign of CUDA. `--ttl`
  is capped at 10 idle minutes (a larger value is clamped, with a note on stderr). The policy
  lives in the stdlib helper `runpod_run_policy.py` next to the script (now needs `python3`);
  exit codes 21-23 belong to the daily budget, slot queue and ssh-wait limits.

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

- `agent-hooks/format-on-write` (#901): the formatter is chosen by the repo's own config,
  never by what happens to be installed. biome (a biome config) wins for the JS/TS/JSON/CSS
  files it formats (HTML when the config opts in) unless its formatter is plainly switched off
  there (a lint-only biome); oxfmt and prettier run only when the repo configures them (a config file,
  a `package.json` `"prettier"` key, or a direct (dev)dependency); a configured-but-missing
  formatter is a no-op instead of falling through to another one. Previously a
  `node_modules/.bin/prettier` in a biome repo reformatted every agent `.ts` write with
  prettier's `trailingComma: "es5"`, failing the repo's biome lint in CI.

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

# ChatGPT Guardian observability implementation plan

**Goal:** Truthful coverage and continuation evidence in JSON and an interactive TUI.
**Architecture:** Separate read-only browser observer, independent transactional telemetry SQLite, pm snapshot reader/TUI. Existing recovery service and controls remain untouched.
**Tech Stack:** Python stdlib; existing Playwright only inside observer; curses only inside TUI.
**Spec:** User request, 2026-10-17: finish JSON/TUI, verify live behavior, explain why only two sessions are covered.

## Global constraints

No sends, model changes, navigation, page creation/reload/activation/closing, quota/uncertain resets, service/browser restarts or new coding/review agents. Validate workspace account; open source DB read-only. Missing source DB must not be created. A sent attempt is not proof of continuation. Show exact Europe/Belgrade times and source freshness. Distinguish enrollment, presence, availability and account-wide coverage limits. Deduplicate events. Keep pm startup stdlib-only. Source and docs are English. Do not edit the active pm-guardian-monitor worker's worktree.

## Review focus

Stale/corrupt telemetry; unrelated/old responses falsely confirming sends; duplicate/missing tabs; terminal control injection and resize; observer disconnect/crash preserving controls and event deduplication.

## Task 1: Projection and storage
Files: lib/agenttools_chatgpt/monitor.py; tests/test_chatgpt_guardian_monitor.py.
Interface: project_snapshot(workspace, controls, attempts, samples, previous, health, now) -> dict; MonitorStore(state_path, workspace).record/read.
- [ ] Write and run failing tests for read-only source access, coverage, continuation correlation, uncertain preservation, stale data and dedup.
- [ ] Implement pure projection and independent transactional telemetry.
- [ ] Run focused tests and regressions.

## Task 2: Observer and CLI
Files: monitor_observer.py, monitor_dom.js, cli.py, pyproject.toml and observer tests.
Interface: observer samples existing DOM only; monitor --json reads persisted evidence only.
- [ ] Test parser dispatch before Store construction, and passive DOM behavior.
- [ ] Implement bounded polling, single writer, Guardian journal health and snapshot export.
- [ ] Run one live read-only cycle and inspect actual evidence.

## Task 3: pm TUI
Files in separate pm-cli worktree: pmlib/chatgpt_tui.py, tests/test_chatgpt_tui.py, command integration/docs.
Interface: existing monitor JSON schema v1; monitor --tui and tui alias.
- [ ] Write/run failing rendering, search/filter, keyboard and refresh-error tests.
- [ ] Implement sessions/events/details panes and safe terminal restoration.
- [ ] Verify in a real pseudo-terminal with resize and quit.

## Task 4: Deploy and verify
- [ ] Run tests/lint; explicitly report unrelated baseline failures.
- [ ] Install tested packages without restarting existing recovery/browser processes.
- [ ] Start only the read-only observer; inspect two genuine cycles, event dedup and source timestamps.
- [ ] Report exact live counts, original service PID and remaining coverage limits.

## Ledger
Ruling: Preserve explicit enrollment rather than silently enabling recovery in other chats; display every already-open dedicated-browser chat read-only.
Ruling: Separate observer permits live telemetry without restarting recovery.
Ruling: Another pm monitor worker is active; TUI work uses an independent branch, reconciled only after inspecting its completed interface.

## Verification and deployment ledger — 2026-10-17, Europe/Belgrade

Task 1 complete: independent read-only source projection and transactional telemetry persistence. Tests cover sent versus correlated continuation, uncertain preservation, missing/duplicate pages, event deduplication, stale reads, deferred checks, early generation before final text, and rate-limit events.

Task 2 complete: passive observer and CLI installed on home. `monitor --json` dispatches before mutable recovery Store construction. The new `chatgpt-guardian-glide-observer.service` is enabled and running; repeated live cycles produced fresh snapshots and zero repeated events. Existing Guardian PID 3239114 remained active with no restarts. Observer PID 4062280 had no restarts.

Task 3 complete: installed `pm chatgpt monitor --tui` and `tui` alias verified against actual live telemetry in a pseudo-terminal with a terminal emulator. Live session titles, watched/available counts, search, details, event pane, resize and clean exit passed. The initial raw-byte smoke assertion was insufficient for incremental curses redraws; decoded terminal screen verification passed.

Task 4 verification: PM full suite 711 passed, 8 skipped. Guardian package full suite 400 passed in 327.21 seconds, with 3 existing multiprocessing/fork deprecation warnings. New/modified PM monitoring files and Guardian monitoring modules passed lint; `git diff --check` passed in both worktrees. Whole PM lint has exactly the same 35 findings as its unchanged base, with zero new findings and none in modified files. No full agent-tools monorepo lint/other unrelated package-suite claim is made.

Test environment ruling: a deep disk TMPDIR exceeded Chromium's Unix socket path limit. A short disk path `/home/ultra/.cache/gm-tests` fixed browser fixture startup. The earlier `/tmp` user-quota write error did not mean the root filesystem was full; recovery kept running throughout. No user data cleanup was performed.

Live evidence at 20:34:53: snapshot generated 20:34:45, real DOM checks 20:34:44, 2 enrolled / 3 available / 0 generating, one sent attempt in 24 hours with confirmed completed continuation. The dispatch was 18:10:36; completed response evidence was first recorded at 20:20:44, not an inferred actual completion time. Seven initial baseline events remained seven; no new sends or repeated notifications. Existing controls and unresolved-send state were preserved.

Coverage remains deliberately limited to already-open chats in the configured dedicated browser. Other active account sessions are not enumerated, and discovery never enrolls chats for recovery. Existing separate `pm-guardian-monitor` and original Guardian worktrees were not edited. Changes are installed locally from `pm-chatgpt-tui` and `chatgpt-observability`; no commit, remote push or main merge has been performed. No coding/review agents, ChatGPT messages, model changes, browser navigation or recovery-service restarts were initiated.

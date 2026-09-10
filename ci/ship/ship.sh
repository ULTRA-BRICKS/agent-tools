#!/usr/bin/env bash
# ship — green-CI-gated PR merge with pre-merge safety checks, then branch/worktree cleanup.
#
# A portable generalization of a "gh ship <PR>" helper: before merging it verifies the PR is
# actually ready, then squash-merges and cleans up. It refuses to merge when any of these is
# true (each a real way a bad merge sneaks in):
#   • PR is not OPEN / is CONFLICTING / is BEHIND its base (ruleset wants up-to-date).
#   • Required status checks aren't all passing (green-CI gate).
#   • There are unresolved review threads (see ci/review-threads/).
#   • The PR is younger than the review-dwell window (async review hasn't had time to form
#     its questions yet — "0 unresolved threads" is vacuous if no review has posted).
#   • The PR has ZERO GitHub-side reviews (`gh pr view --json reviews` is empty) — Guard-B and
#     "0 unresolved threads" are both vacuous when nobody reviewed at all (real incident: PR
#     #764/HYP-1380 merged this way). Deny-by-default, no self-service override — see the
#     external-review gate below (mirrors the review-quorum/--skip-ci hatch pattern).
#   • A UI-touching PR has no embedded screenshot (see ci/screenshots/) — unless overridden.
#   • The local branch has unpushed/diverged commits, or its worktree is dirty.
#   • The review-quorum bar (Guard-B of the self-merge-authority program) is not met: the
#     PR's task code has fewer than SHIP_REVIEW_QUORUM_MIN_ITER PASSED review-cli iterations,
#     across SHIP_REVIEW_QUORUM_MIN_ROLES distinct BOARD ROLES among those passed iterations —
#     role-based coverage is the PRIMARY/default check now (matching review-cli's own default,
#     review-cli#246). An explicit SHIP_REVIEW_QUORUM_MIN_MODELS additionally requires that many
#     distinct MODELS too (AND logic — both floors must be met); there is no default model floor
#     any more. A failed/degraded review never counts toward either floor. There is NO
#     self-service override — a one-time bypass goes through a live Telegram approval to Alex
#     (see the hatch escalation below), never a reason flag.
#
# After a successful merge, IF a `task` (task-cli) binary is on PATH and a ticket code can be
# derived from the branch/PR title/PR body, ship calls `task mark-shipped <code> --pr <url>
# [--commit <sha>]` so the ticket records the merge and prints its own acceptance instructions
# — this NEVER closes the ticket (only proof-backed acceptance does that) and NEVER blocks or
# fails the ship (best-effort: task-cli absent, an undetectable ticket code, or a task-cli
# error are all logged and skipped, not fatal). See "task-cli notify" below the merge step.
#
# All project-specific coupling is OPTIONAL and configured by env/flags — no issue-tracker,
# no path layout, no org is hard-coded.
#
# Requires: gh (authenticated), git. jq strongly recommended (required-checks-only gating).
# task-cli (the `task` binary) is optional — its absence only skips the post-merge ticket notify.
#
# Usage:
#   ship.sh <PR-number> [--repo <owner/repo>] [--skip-ci] [--dry-run]
#           [--no-screenshot-ok <reason>] [--resolve-addressed-threads]
#           [--screenshot <path> [desc]]...
#
# Flags:
#   --repo <owner/repo>    ship a PR that lives in a DIFFERENT repo than the current checkout:
#                          pins every gh call (view/checks/merge) to owner/repo via GH_REPO.
#                          The only way to ship into a non-CWD repo when a `cd && gh ship`
#                          is not possible. Accepts -R / --repo=… / -R=… / -R… too. When the
#                          target is not this checkout's own origin, remote-branch deletion is
#                          skipped (see the cleanup guard) so the wrong remote is never touched.
#   --skip-ci              admin-merge bypassing the green-CI gate (+ branch protection). This is
#                          DENY-BY-DEFAULT: it proceeds ONLY on a one-time live Telegram approval
#                          requested via RIG_HATCH_REQUEST_SHIP_SKIP_CI (see NOTE below). It is NOT
#                          the way to handle a billing-blocked / stuck CI — for that, run ship
#                          WITHOUT --skip-ci: the normal path auto-detects the outage, runs the
#                          local fallback gate, and does a normal (non-admin) merge.
#   --dry-run              print what would happen; change nothing.
#   --no-screenshot-ok R   override the UI screenshot requirement with a logged reason R.
#   --no-version-bump-ok R override the version-bump requirement with a logged reason R
#                          (genuine no-release ship: docs-only, pure test/CI, a revert).
#   --no-review-dwell-ok R override the review-dwell window with a logged reason R (a genuine
#                          fast-track: a trivial/urgent merge that doesn't need review latency).
#   --resolve-addressed-threads  before the unresolved-threads gate, auto-close review threads that
#                          are SAFE without a human: unresolved + isOutdated + authored entirely by
#                          bots, with no P0/P1/critical/blocker/security marker in any comment
#                          (#285). NOTE: isOutdated only means the anchored code CHANGED since the
#                          comment, not that the finding was fixed. Human, high-severity,
#                          unreadable-body, or still-current threads are never touched and still
#                          block. Also SHIP_RESOLVE_ADDRESSED_THREADS=1. A bot thread with >100
#                          comments is fail-closed (never auto-resolved).
#   --rewrite-magic-close  when the magic-close gate finds a Closes/Fixes/Resolves keyword
#                          targeting an issue/ticket in the PR title or body, rewrite the keyword
#                          to "Refs" via `gh pr edit` (audited) and continue, instead of refusing.
#                          Under --dry-run the edit is only printed.
#   --screenshot P [D]     attach a local screenshot P (desc D) via SHIP_IMAGE_UPLOAD_CMD,
#                          then post it as a PR comment. Repeatable.
#   --known-flake NAME     assert that the FAILED check named NAME (as printed by the green-CI
#                          gate's own "x <name> -> <conclusion>" refusal lines) is a pre-existing
#                          failure unrelated to this diff — a confirmed CI flake, not the "CI is
#                          structurally down" case ci_appears_structurally_down() already covers
#                          (that one needs ~80% of ALL checks failing; this is for the common
#                          shape where ONE check is flaky and everything else is green). NOT a
#                          blind trust-me flag: ship independently VERIFIES the assertion before
#                          it does anything — it queries the last SHIP_FLAKE_LOOKBACK_RUNS
#                          completed runs of the SAME workflow on the PR's BASE branch and
#                          requires that this exact check name also FAILED on at least one of
#                          them (see _known_flake_confirmed). An assertion that cannot be
#                          verified this way is REFUSED, same as not passing the flag at all — so
#                          this closes, rather than reopens, the exact escalate-to-Alex-for-a-
#                          confirmed-unrelated-flake gap this flag exists to remove (see the
#                          AGENTS.md "CI billing-block" note in the repos that document it).
#                          Repeatable — every check currently FAILED in the rollup must be
#                          covered by a verified --known-flake or the gate still hard-refuses
#                          (an unaccounted-for failure is never silently waved through). On
#                          success this runs the SAME local fallback gate as the CI-down path
#                          (run_local_ci_gate) and merges only if it passes — a known flake still
#                          gets a real local verification pass, it does not skip testing
#                          entirely. Logged to SHIP_AUDIT_FILE like every other gate decision.
#
# NOTE: the review-quorum gate (Guard-B) has NO override FLAG. When its bar is not met and you
# genuinely need to proceed, request a one-time bypass by setting the env var
#   RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"
# which asks Alex live on Telegram (via the shared agenttools_hatch_escalation lib) and proceeds
# ONLY on his real-time approval. SHIP_REVIEW_QUORUM=0 is forbidden and does not disable the gate.
#
# NOTE: --skip-ci has NO override flag either and is deny-by-default. Request a one-time bypass by
# setting RIG_HATCH_REQUEST_SHIP_SKIP_CI="<justification>" (same shared hatch lib, same live
# Telegram approval). Unlike the review-quorum gate there is NO ops off-switch env var — an
# agent-settable off-switch would defeat the whole point; the intended path is a live approval.
# (Threat-model caveat, same as the review-quorum gate: a shipper who fully controls the ship
# PROCESS's PATH can defeat ANY gate — fake gh/git/python3 — so this removes the fail-OPEN
# asymmetry for a benign shipper, it is not a claim to withstand a hostile PATH.)
#
# Knobs (env):
#   SHIP_MAIN_CHECKOUT     path to the primary (main) checkout for the post-merge pull.
#                          Default: first worktree in `git worktree list`.
#   SHIP_DEFAULT_BRANCH    base branch name (default: main).
#   SHIP_UI_PATH_REGEX     ERE; changed files matching it => "UI-touching" (screenshot
#                          required). Default covers common front-end paths. Empty = never
#                          treat as UI (disables the screenshot gate).
#   SHIP_MERGE_METHOD      squash | merge | rebase (default: squash).
#   SHIP_IMAGE_UPLOAD_CMD  optional command to upload a screenshot and PRINT a public URL on
#                          stdout. Receives the image path as {FILE} (or as $1 if no token).
#                          Used only with --screenshot. If unset, --screenshot embeds a
#                          local-path note instead (does NOT satisfy the screenshot gate).
#   SHIP_SKIP_VERSION_BUMP=1  override the version-bump gate (same effect as
#                          --no-version-bump-ok, with a generic env-set reason). Use only for a
#                          genuine no-release ship.
#   SHIP_VERSION_FILES     space-separated list of version files to check (relative to the repo
#                          root). Default: auto-detect pyproject.toml then package.json at the
#                          root. Set it for a non-standard layout (e.g. a nested package).
#   SHIP_REVIEW_DWELL      minimum seconds since the PR's last code push before a merge is
#                          allowed, so asynchronous review (multi-model / CI-AI / human) has
#                          time to FORM its comments. Default 600 (10 min). 0 disables the gate.
#   REVIEW_TASK_CODE       the task/ticket code (e.g. HYP-931) this ship belongs to, for the
#                          review-quorum gate. If unset, ship derives it from a ticket-like
#                          token in the branch name, then the PR body: HYP-<n> / PROJ-<n> (also
#                          inside a Linear URL, https://linear.app/<team>/issue/HYP-1440/…), a
#                          descriptive ALL-CAPS code, a keyword-anchored `Refs #<n>`, or a full
#                          issue URL of THIS repo (https://github.com/<owner>/<repo>/issues/<n>
#                          — the shape task-cli's links gate demands; both yield the literal
#                          `#<n>`). If none is found, the gate refuses (fail-closed) with guidance.
#   SHIP_REVIEW_QUORUM_ENABLED / SHIP_REVIEW_QUORUM  0/false/no is FORBIDDEN (agent-tools#644)
#                          and is rejected at parse — the gate stays enabled. There is no ops
#                          off-switch; the only bypass is RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM.
#   SHIP_TASK_NOTIFY_ENABLED  set to 0/false/no to disable the post-merge task-cli notify step
#                          (default: enabled). Best-effort and never blocks the merge either
#                          way — this only controls whether `task mark-shipped` gets called at
#                          all. See "task-cli notify" below the merge step for the full story.
#   SHIP_REVIEW_QUORUM_MIN_ITER    passed through to `review task --check --min-iter` as
#                          telemetry. Authorization is ROLE coverage (#644); ship requires
#                          QITER > 0 as a hollow-payload guard, not QITER >= this floor.
#   SHIP_REVIEW_QUORUM_MIN_ROLES   quorum floor: distinct BOARD ROLES among the passed iterations
#                          (default 3). Same >=3 clamp. This is now the PRIMARY/default gate
#                          mechanism and is ALWAYS enforced, matching review-cli's own default
#                          (review-cli#246).
#   SHIP_REVIEW_QUORUM_MIN_MODELS  quorum floor: distinct models (default 3). Same >=3 clamp.
#                          Enforced ONLY when this env var is explicitly set by the operator —
#                          there is no default model floor any more (role-based coverage is the
#                          default). When set, ship also passes --min-models to review-cli, so
#                          BOTH floors are required (mirrors review-cli's own explicit-vs-default
#                          AND logic, review-cli#246).
#   RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM  one-time bypass request for the review-quorum gate:
#                          set it to a written justification to ask Alex live on Telegram (via
#                          the shared agenttools_hatch_escalation lib); the gate proceeds ONLY on
#                          his real-time approval. Blank/bare-flag values are denied; unset means
#                          no bypass requested (the gate refuses with guidance). tg-ctl is
#                          resolved by the shared lib from a rig.yaml agent_hooks.tg_ctl_path in
#                          the OS account's REAL home (pwd.getpwuid — NOT the $HOME env var, NOT
#                          the PR's repo), then a trusted-paths allowlist. Neither the lib dir nor
#                          tg-ctl is env- or repo-overridable, so nothing the shipper/PR controls
#                          can redirect approval to a stub.
#                          This hatch is for a bar not met for REVIEW-CONTENT reasons (too few
#                          iterations/roles, an undiscoverable task code, review-cli
#                          unreachable). A CONFIRMED fleet-wide review-cli capacity outage
#                          (every configured provider's own capacity-check reads exhausted) is
#                          now auto-detected instead — see _review_capacity_all_exhausted below
#                          — and does NOT need this hatch at all.
#   SHIP_EXTERNAL_REVIEW_ENABLED / SHIP_EXTERNAL_REVIEW  IGNORED as env vars (agent-tools#624).
#                          A bare `SHIP_EXTERNAL_REVIEW=0` export no longer disables this gate
#                          — that was the ambient-leak hole #528/#609 left open. Durable
#                          disable is a `.ship-config` line (`SHIP_EXTERNAL_REVIEW=0` only —
#                          the `_ENABLED` alias is not a recognized file key), read from the
#                          PR's own BASE branch, never HEAD — see "Knobs (file)" and the
#                          external-review gate below. One-time per-PR bypass is
#                          RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW (see that knob).
#   RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW  one-time bypass request for the external-review
#                          gate, same shape/hardening as RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM
#                          above — see ci/ship/external_review_hatch.py.
#                          Same posture as the review-quorum hatch above: this is for a
#                          zero-GitHub-reviews refusal for review-ACCESS reasons (nobody has
#                          reviewed yet). A CONFIRMED fleet-wide review-cli capacity outage is
#                          now auto-detected instead (_review_capacity_all_exhausted) and does
#                          NOT need this hatch at all.
#   SHIP_ACCEPTANCE_GATE   set to 0/false/no to disable the pre-merge ACCEPTANCE gate (default:
#                          enabled) — the gate that runs `task gate <code> --json` (task-cli) and
#                          refuses the merge while the ticket has unchecked criteria or checks
#                          without a proof, OR when no ticket code is derivable (a PR with no
#                          ticket has no acceptance — agent-tools#639). Also settable as a
#                          committed `.ship-config` line
#                          (see "Knobs (file)"). Ops off-switch; the per-ticket opt-out is
#                          `task change <code> --post-merge-acceptance "<reason>"`, recorded ON
#                          the ticket and reported in the audit line. See "acceptance gate" below.
#   SHIP_MAGIC_CLOSE_GATE  set to 0/false/no to disable the magic-close keyword gate (default:
#                          enabled) — refuses a PR whose title/body carries close/closes/closed/
#                          fix/fixes/fixed/resolve/resolves/resolved followed by #N, owner/repo#N
#                          or a ticket code (ABC-123), because GitHub and Linear close the ticket
#                          on merge behind task-cli's gates. See "magic-close keyword gate" below.
#   SHIP_AUDIT_FILE        path for the gate audit JSONL (default
#                          ~/.config/agent-tools/ship-audit.jsonl). One line is appended per
#                          non-dry-run gated ship. review-quorum gate decisions: authorized /
#                          bypass:approved / bypass:denied / refused (has a `task_code` field).
#                          --skip-ci gate decisions: skipci:bypass:approved / skipci:bypass:denied
#                          / skipci:refused (has a `gate":"skip-ci"` field). external-review gate
#                          decisions: external-review:bypass:approved / :bypass:denied /
#                          :refused (has a `gate":"external-review"` field). acceptance gate
#                          decisions: authorized / authorized:post-merge-opt-out / refused /
#                          skipped / auto-closed / auto-close-failed (has `gate":"acceptance"`, a
#                          `task_code` when one was derived, and a `detail` — the opt-out reason,
#                          the open criteria, or why it was skipped; auto-closed/auto-close-failed
#                          are the post-merge `task done <code>` this gate also runs when every
#                          criterion was already proven pre-merge — see ci/ship/README.md's
#                          "Auto-close" section). magic-close gate decisions: refused / rewritten
#                          (has `gate":"magic-close"` and the matched phrases in `detail`).
#                          --dry-run prints the would-be audit instead of writing.
#
# Knobs (file):
#   .ship-config           optional, committed at the repo root — an AUDITED, per-repo override
#                          for the CI-down local test fallback gate (see "CI-down detection and
#                          local fallback gate" below) AND for two ops off-switches
#                          (SHIP_ACCEPTANCE_GATE, SHIP_EXTERNAL_REVIEW — see below). Unlike
#                          SHIP_LOCAL_TEST_CMD (an env var, test-only, never meant for production
#                          use — see that knob above), this file is checked into the repo itself,
#                          so it is reviewed exactly like rig.yaml/package.json already are — it
#                          does NOT introduce a new trust boundary. For the local-test knobs it is
#                          a production-safe, auditable way to tell the local gate where and how
#                          to run tests when auto-detection can't guess correctly (e.g. a
#                          monorepo-of-fixtures whose real suite lives in a subdirectory); for the
#                          gate off-switches it is a durable, per-repo alternative to exporting an
#                          env var from a machine-wide shell rc file (agent-tools#528: a
#                          `~/.zshenv` export of SHIP_EXTERNAL_REVIEW=0 once broke this script's
#                          own gate tests, because the test SUITE inherited the same global
#                          override — a committed per-repo file is never inherited by an unrelated
#                          test process, only read from the repo under test). SHIP_LOCAL_TEST_DIR,
#                          SHIP_LOCAL_TEST_CMD, and SHIP_ACCEPTANCE_GATE are read from the last
#                          COMMITTED content at this checkout's own HEAD (`git show
#                          HEAD:.ship-config`), never the working tree — an uncommitted/
#                          staged-only .ship-config does not set them — the "audited, committed"
#                          claim above is an enforced property, not just documentation.
#                          SHIP_EXTERNAL_REVIEW is the one exception: reading a merge-safety
#                          off-switch from a shipping PR's own HEAD would let that SAME PR commit
#                          the line and disable the gate on its own merge (agent-tools#600 review
#                          round 1 — recreates PR #764 inside the fix for #764), so it is read
#                          instead from the PR's BASE branch (`origin/<baseRefName>`, fetched
#                          fresh) — see the external-review gate below. It only takes effect once
#                          it has itself gone through a normal, reviewed merge to the base. Simple
#                          `KEY=value` lines, no quote-stripping (don't wrap values in quotes —
#                          `KEY="val"` means the literal value `"val"`, not `val`);
#                          `#`-only-prefixed lines and blank lines are ignored. Four whitelisted
#                          keys, nothing else is read or evaluated from the file:
#                            SHIP_ACCEPTANCE_GATE=0       ops off-switch for the pre-merge acceptance
#                                                  gate, per repo (same effect as the env var of
#                                                  the same name; any other value keeps it on).
#                            SHIP_EXTERNAL_REVIEW=0       ops off-switch for the external-review gate
#                                                  (zero-GitHub-reviews refusal, see that gate
#                                                  below), per repo, read from the BASE branch.
#                                                  Env SHIP_EXTERNAL_REVIEW=0 /
#                                                  SHIP_EXTERNAL_REVIEW_ENABLED=0 is ignored
#                                                  (agent-tools#624); this committed line is
#                                                  the only durable off-switch. Any other value
#                                                  keeps the gate on.
#                            SHIP_LOCAL_TEST_DIR=<path>   directory (relative to repo root) to run
#                                                  the test command from, or to scope
#                                                  auto-detection to when SHIP_LOCAL_TEST_CMD is
#                                                  not also set.
#                            SHIP_LOCAL_TEST_CMD=<cmd>    command line to eval for the local gate
#                                                  (same eval mechanism as the env var of the same
#                                                  name — this is just a committed, per-repo
#                                                  source for it instead of a per-invocation one).
#                          Precedence (highest first): SHIP_LOCAL_TEST_CMD env var (test-only) >
#                          .ship-config file > rig.yaml + `dev` CLI (root-only) > root auto-detect
#                          (pyproject.toml/package.json/Cargo.toml at repo root) > e2e/ subdirectory
#                          auto-detect (same three manifests, one level deep, e2e/ ONLY — test/ and
#                          tests/ are deliberately NOT auto-probed, see the priority-5 comment in
#                          _local_test_runner; use .ship-config for those) > fail closed. A
#                          present-but-empty/malformed .ship-config (neither key set, not committed
#                          at HEAD, or an unrecognized/unsafe SHIP_LOCAL_TEST_DIR — absolute,
#                          containing `..`, or resolving to the repo root itself — which invalidates
#                          the WHOLE file, not just the dir) is ignored (with a logged warning) and
#                          detection proceeds as if the file didn't exist.
set -euo pipefail

ORIG_PWD=$(pwd -P)
PR=""; SKIP_CI=0; DRY_RUN=0; NO_SHOT_OK=""; NO_VBUMP_OK=""; NO_DWELL_OK=""; REPO_FLAG=""
# --rewrite-magic-close: let the magic-close gate rewrite Closes/Fixes/Resolves -> Refs via
# `gh pr edit` and continue, instead of refusing. See "magic-close keyword gate" below.
REWRITE_MAGIC_CLOSE=0
SHOT_PATHS=(); SHOT_DESCS=()
# --known-flake NAME, repeatable — see the "known-flake gate" block below _ci_github_status_indicator.
KNOWN_FLAKES=()
# Set by the known-flake gate once its local test pass succeeds; consumed at the merge section
# below to log the "confirmed" audit line only once `gh pr merge` has actually succeeded — see
# that gate's own comment for why the audit must wait for the true terminal event.
KF_AUDIT_PENDING=""
# Auto-resolve addressed bot-nit review threads before the unresolved-threads gate (opt-in, #268).
# Enabled by --resolve-addressed-threads or SHIP_RESOLVE_ADDRESSED_THREADS=1; only ever closes a
# thread that is unresolved, OUTDATED (its code changed), authored ENTIRELY by bots, and has no
# high-severity marker in any readable comment (#285) — a human, high-severity, uncertain-body, or
# current thread is never touched (see the gate below).
RESOLVE_THREADS=0
case "${SHIP_RESOLVE_ADDRESSED_THREADS:-}" in 1|true|yes) RESOLVE_THREADS=1 ;; esac
USAGE='Usage: ship.sh <PR-number> [--repo <owner/repo>] [--skip-ci] [--dry-run] [--no-screenshot-ok <reason>] [--no-version-bump-ok <reason>] [--no-review-dwell-ok <reason>] [--resolve-addressed-threads] [--rewrite-magic-close] [--screenshot <path> [desc]]... [--known-flake <check-name>]...'

# Path to the review-quorum gate's hatch-escalation helper (ci/ship/review_quorum_hatch.py),
# derived ONLY from this script's own location. That helper imports the shared
# agenttools_hatch_escalation lib from a fixed path (lib/ two levels up) — the SAME lib the
# pin-primary-worktree / block-reset-hard agent-hooks use — so a bypass goes through live Telegram
# approval, never a self-service flag, and neither the lib nor tg-ctl is env-/repo-overridable
# (see the helper's docstring). If ship.sh was copied OUT of the checkout (e.g. to ~/bin), the
# helper import fails CLOSED — the gate still refuses; the bypass just can't run from a detached
# copy. Run ship from within the agent-tools checkout to use the hatch.
_SHIP_SELF_SRC="${BASH_SOURCE[0]:-$0}"
_SHIP_SELF_DIR="$(cd "$(dirname "$_SHIP_SELF_SRC")" 2>/dev/null && pwd -P || echo /nonexistent)"
_SHIP_HATCH_PY="$_SHIP_SELF_DIR/review_quorum_hatch.py"
# Same deal for the --skip-ci CI-bypass gate: its one-time Telegram approval routes through
# ci/ship/skip_ci_hatch.py, which imports the SAME shared agenttools_hatch_escalation lib from a
# fixed path and resolves tg-ctl off the account's REAL home — no self-service flag, no env-/repo-
# overridable authority. Fails CLOSED (refuse) if ship.sh was copied out of the checkout.
_SHIP_SKIP_CI_HATCH_PY="$_SHIP_SELF_DIR/skip_ci_hatch.py"
# Same deal for the external-review gate (see that gate below): its one-time Telegram bypass
# routes through ci/ship/external_review_hatch.py, same shared lib, same hardening.
_SHIP_EXTERNAL_REVIEW_HATCH_PY="$_SHIP_SELF_DIR/external_review_hatch.py"

# --- arg parse (PR number is the lone bare arg; --screenshot takes path + optional desc) --
args=("$@"); i=0; n=${#args[@]}
while [ "$i" -lt "$n" ]; do
  a=${args[$i]}
  case "$a" in
    --skip-ci) SKIP_CI=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --resolve-addressed-threads) RESOLVE_THREADS=1 ;;
    --rewrite-magic-close) REWRITE_MAGIC_CLOSE=1 ;;
    --no-screenshot-ok)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--no-screenshot-ok needs a <reason>." >&2; exit 1; }
      NO_SHOT_OK=${args[$i]}; [ -n "$NO_SHOT_OK" ] || { echo "--no-screenshot-ok reason empty." >&2; exit 1; } ;;
    --no-version-bump-ok)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--no-version-bump-ok needs a <reason>." >&2; exit 1; }
      NO_VBUMP_OK=${args[$i]}; [ -n "$NO_VBUMP_OK" ] || { echo "--no-version-bump-ok reason empty." >&2; exit 1; } ;;
    --no-review-dwell-ok)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--no-review-dwell-ok needs a <reason>." >&2; exit 1; }
      NO_DWELL_OK=${args[$i]}; [ -n "$NO_DWELL_OK" ] || { echo "--no-review-dwell-ok reason empty." >&2; exit 1; } ;;
    --known-flake)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--known-flake needs a <check-name>." >&2; exit 1; }
      [ -n "${args[$i]}" ] || { echo "--known-flake check-name empty." >&2; exit 1; }
      KNOWN_FLAKES+=("${args[$i]}") ;;
    --screenshot|--shot)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "$a needs a <path>." >&2; exit 1; }
      p=${args[$i]}; case "$p" in /*) : ;; *) p="$ORIG_PWD/$p" ;; esac
      SHOT_PATHS+=("$p")
      if [ "$((i+1))" -lt "$n" ] && [ "${args[$((i+1))]:0:1}" != "-" ]; then
        # The next bare token is this shot's desc UNLESS it's the (still unset) PR number.
        nxt=${args[$((i+1))]}; rest_bare=0; j=$((i+2))
        while [ "$j" -lt "$n" ]; do [ "${args[$j]:0:1}" != "-" ] && rest_bare=$((rest_bare+1)); j=$((j+1)); done
        if [ -z "$PR" ] && [ "$rest_bare" -eq 0 ]; then SHOT_DESCS+=(""); else SHOT_DESCS+=("$nxt"); i=$((i+1)); fi
      else SHOT_DESCS+=(""); fi ;;
    # --repo/-R pins every gh call to a remote repo (owner/repo) — the cross-repo ship path.
    # It is the ONLY way to ship a PR into a non-CWD repo when a `cd && gh ship` is forbidden.
    # Accept all gh-compatible spellings: `--repo X`, `-R X`, `--repo=X`, `-R=X`, `-RX`.
    # (Order matters: the attached `=`/glued forms must precede the bare `--repo|-R` case.)
    --repo=*) REPO_FLAG=${a#--repo=}; [ -n "$REPO_FLAG" ] || { echo "--repo= needs an <owner/repo> value." >&2; exit 1; } ;;
    -R=*)     REPO_FLAG=${a#-R=};     [ -n "$REPO_FLAG" ] || { echo "-R= needs an <owner/repo> value." >&2; exit 1; } ;;
    -R?*)     REPO_FLAG=${a#-R} ;;
    --repo|-R)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "$a needs an <owner/repo> argument." >&2; exit 1; }
      REPO_FLAG=${args[$i]} ;;
    -*) echo "Unknown flag: $a"$'\n'"$USAGE" >&2; exit 1 ;;
    *) [ -n "$PR" ] && { echo "Multiple PR numbers ($PR, $a) — pass one." >&2; exit 1; }; PR="$a" ;;
  esac
  i=$((i+1))
done
[ -n "$PR" ] || { echo "$USAGE" >&2; exit 1; }
# Validate --repo shape once: exactly owner/repo (a single slash, no extra path segments).
if [ -n "$REPO_FLAG" ]; then
  case "$REPO_FLAG" in
    */*/*) echo "Refusing: --repo value '$REPO_FLAG' looks like a URL segment — expected 'owner/repo'." >&2; exit 1 ;;
    */*)   : ;; # ok
    *)     echo "Refusing: --repo value '$REPO_FLAG' is not in owner/repo form." >&2; exit 1 ;;
  esac
fi

DEFAULT_BRANCH="${SHIP_DEFAULT_BRANCH:-main}"
MERGE_METHOD="${SHIP_MERGE_METHOD:-squash}"
UI_PATH_REGEX="${SHIP_UI_PATH_REGEX-(^|/)(components|pages|views|ui|app|src/app)/|\.(tsx|jsx|vue|svelte)$}"

run() { if [ "$DRY_RUN" = "1" ]; then echo "[dry-run] $*"; else "$@"; fi; }

# --- core.bare corruption guard --------------------------------------------------------
# A WORKING checkout whose `git config core.bare` is wrongly `true` is a real corruption
# class (external cause — see rig-cli #19/#52, where `rig doctor --fix` repairs it). It
# breaks EVERY git op in that directory (status / diff / commit / worktree all fail fatal
# with "this operation must be run in a work tree"), so ship's later main-refresh /
# worktree ops would fail CONFUSINGLY mid-ship (it bit a real ship this session). We catch
# it EARLY — before any destructive git op on the checkout — and ABORT (exit 1, ship's
# uniform "Refusing:" preflight code) with the repo + the one-line fix, rather than auto-fix
# (ship must never silently rewrite repo config).
#
# Detection keys off git's per-path `rev-parse --is-bare-repository` verdict (NOT a raw
# `core.bare` config read, NOT `worktree list`'s bare marker): both of those read `true` for a
# LEGITIMATE linked worktree of a genuine bare repo (shared config), whereas rev-parse correctly
# reports such a worktree as not-bare — at the worktree root AND from any subdirectory of it.
# That single property is the false-positive shield, so the two entry points below differ only
# in WHERE they look, not in the test:
#   • abort_if_core_bare DIR — used for an explicit checkout/worktree PATH (the main checkout,
#     each linked worktree). It also requires the WORKING-checkout layout (`.git` at DIR's root)
#     so a genuine bare repo dir (`foo.git`, no `.git` entry, rev-parse=true) is excluded.
#   • abort_if_cwd_core_bare — used for the AMBIENT cwd, which may be a SUBDIRECTORY of the
#     corrupt checkout (the realistic "ship launched from inside it" shape). `.git` lives only
#     at the worktree root, so the DIR layout gate above would wrongly pass a subdir. We can't
#     drop the genuine-bare exclusion entirely, though: a GENUINE bare repo (`foo.git`) — or any
#     subdir inside one — also reports rev-parse=bare, but there `core.bare=true` is LEGITIMATE,
#     so firing the "corruption" diagnostic (and worse, telling the user to run `config core.bare
#     false`, which would BREAK a real bare repo) is actively wrong. The clean discriminator,
#     evaluable from a subdir, is `rev-parse --is-inside-git-dir`: it is FALSE in a corrupt
#     working checkout (cwd is a work tree) but TRUE inside a genuine bare repo (the bare dir IS
#     the git dir). We also require a `.git` entry somewhere in the cwd's ancestry (present for a
#     working checkout, absent for a bare repo) as belt-and-suspenders.
# $3 is the exact fix command to print. It MUST match where core.bare actually lives: a plain
# `git config core.bare false` writes the SHARED config, but a WORKTREE-SCOPED core.bare=true
# (extensions.worktreeConfig + `config --worktree core.bare true`) keeps winning over a shared
# `false`, so for that case the fix must be `config --worktree core.bare false`. Callers compute
# the right command (see core_bare_fix_cmd) so the printed advice actually repairs the repo.
die_core_bare() {  # $1 = dir to name, $2 = human label, $3 = exact fix command to print
  {
    echo "Refusing: $2 at $1 has core.bare=true but is a WORKING checkout (corruption)."
    echo "  Every git op there fails 'fatal: this operation must be run in a work tree',"
    echo "  which would break ship's main-refresh mid-merge. Fix it, then re-run ship:"
    echo "    $3"
    echo "  (or run \`rig doctor --fix\` to detect + repair it)."
  } >&2
  exit 1
}
# Single-quote a string for safe copy-paste into a shell, escaping embedded single quotes via
# the '\'' idiom. The printed fix is meant to be pasted-and-run, so a path with a space, quote,
# or `$` must not break or get re-expanded.
shell_squote() {  # $1 = string -> prints a safely single-quoted form
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}
# Pick the correct `core.bare false` command for DIR: if core.bare=true lives in the
# WORKTREE-scoped config, the fix must also be --worktree-scoped, else a shared-config `false`
# is shadowed by the worktree `true` and the repo stays broken. The worktree scope only exists
# when extensions.worktreeConfig is enabled — without it, `config --worktree` aliases the shared
# config and would falsely select the --worktree form, so gate on that extension first.
core_bare_fix_cmd() {  # $1 = dir
  local q; q=$(shell_squote "$1")
  # Read both flags through git's --bool parser so a value written as 1/yes/on (not just the
  # literal "true") is matched — the same normalization the abort itself uses via rev-parse.
  if [ "$(git -C "$1" config --bool extensions.worktreeConfig 2>/dev/null)" = "true" ] \
     && [ "$(git -C "$1" config --worktree --bool --get core.bare 2>/dev/null)" = "true" ]; then
    printf 'git -C %s config --worktree core.bare false' "$q"
  else
    printf 'git -C %s config core.bare false' "$q"
  fi
}
abort_if_core_bare() {  # $1 = dir to check, $2 = human label for the diagnostic
  local dir="$1" label="$2"
  [ -e "$dir/.git" ] || return 0   # no working-checkout layout (genuine bare / not a checkout) — not our class
  [ "$(git -C "$dir" rev-parse --is-bare-repository 2>/dev/null || echo false)" = "true" ] || return 0
  die_core_bare "$dir" "$label" "$(core_bare_fix_cmd "$dir")"
}
# True iff some ancestor of the cwd (inclusive) holds a `.git` entry — the working-checkout
# layout. A genuine bare repo has none in its ancestry; a corrupt working checkout has one at
# its root. Resolve the physical cwd (`pwd -P`) so a symlinked path or a stale inherited $PWD
# can't make the ancestor walk miss the real `.git`.
cwd_has_ancestor_dotgit() {
  local d
  d=$(pwd -P 2>/dev/null) || d="$PWD"
  case "$d" in /*) : ;; *) return 1 ;; esac  # need an absolute path to walk to / safely
  while :; do
    [ -e "$d/.git" ] && return 0
    [ "$d" = "/" ] && return 1
    d=$(dirname "$d")
  done
}
abort_if_cwd_core_bare() {  # the cwd may be a subdir of the corrupt checkout — no DIR layout gate
  [ "$(git rev-parse --is-bare-repository 2>/dev/null || echo false)" = "true" ] || return 0
  # Genuine bare repo (or a subdir of one): rev-parse --is-inside-git-dir is TRUE there and the
  # cwd has no ancestor `.git`. Leave it alone — core.bare=true is legitimate, not corruption.
  # The two discriminators are ANDed, so default --is-inside-git-dir to `false` (treat-as-corrupt)
  # if rev-parse hiccups: that keeps the guard FIRING on a transient failure rather than silently
  # falling through to the bare `show-toplevel` crash. A standalone genuine bare repo is still
  # excluded by the independent no-ancestor-`.git` check below. (The only residual false-positive
  # needs BOTH a rev-parse hiccup AND a genuine bare repo nested under a working checkout — then
  # the ancestor `.git` is found and the guard mis-fires; this fail-safe trades that far-fetched
  # case for catching real corruption on a transient failure, which is the right bias for ship.)
  [ "$(git rev-parse --is-inside-git-dir 2>/dev/null || echo false)" = "true" ] && return 0
  cwd_has_ancestor_dotgit || return 0
  # Name + fix the PHYSICAL cwd (pwd -P), consistent with cwd_has_ancestor_dotgit, so a symlinked
  # or stale-$PWD path is reported as git actually sees it.
  local here; here=$(pwd -P 2>/dev/null) || here="$PWD"
  die_core_bare "$here" "current checkout" "$(core_bare_fix_cmd "$here")"
}

# Guard the CURRENT directory FIRST — before `git rev-parse --show-toplevel` below, which under
# core.bare=true dies with the bare `fatal: this operation must be run in a work tree` (and
# `set -e` aborts the script) — i.e. exactly the confusing failure this guard replaces, with NO
# diagnostic. The most realistic shape is ship launched from INSIDE the corrupt checkout (its
# root OR a subdir), so the cwd guard must run before any cwd-scoped git op. (MAIN_CHECKOUT is
# guarded again below in case it differs from the cwd, e.g. SHIP_MAIN_CHECKOUT points elsewhere.)
abort_if_cwd_core_bare

ROOT=$(git rev-parse --show-toplevel); cd "$ROOT"
command -v gh >/dev/null 2>&1 || { echo "gh CLI not found" >&2; exit 1; }

# --- cross-repo (--repo) support -------------------------------------------------------
# Derive THIS checkout's own origin repo (owner/repo) BEFORE any override. Used only to
# detect a cross-repo invocation — a --repo that names a repo OTHER than the one this
# checkout pushes to — so cleanup never `git push origin --delete`s the wrong remote's
# branch. sed returns its input unchanged on no-match, so the case-guard blanks anything
# that isn't a clean github.com owner/repo (a raw URL, an SSH form that leaked ':'/'@', a
# local path with extra slashes).
# Read the URL in its own step with `|| true`: `git remote get-url` exits non-zero when there
# is NO origin (a fresh/detached checkout), and under `set -euo pipefail` a bare
# `git … | sed` would let that failure abort the whole script. Assign first, transform second.
_CWD_ORIGIN_URL=$(git remote get-url origin 2>/dev/null) || true
_CWD_ORIGIN_REPO=$(printf '%s' "$_CWD_ORIGIN_URL" \
  | sed 's|.*github\.com[:/]\(.*\)\.git$|\1|;s|.*github\.com[:/]\(.*\)$|\1|')
case "$_CWD_ORIGIN_REPO" in *@*|*:*|""|*/*/*) _CWD_ORIGIN_REPO="" ;; esac

# Thread --repo through EVERY gh call via GH_REPO: gh honours it for all subcommands —
# including `gh api`, where the {owner}/{repo} placeholders expand from it — so this one
# assignment covers pr view / pr diff / pr merge / pr comment / api graphql below without
# touching each call site. GH_SHIP_REPO is a test/override hook (mirrors SHIP_MAIN_CHECKOUT).
# With no --repo we leave gh's normal cwd-based inference untouched: the no-repo path is
# byte-for-byte unchanged.
if [ -n "$REPO_FLAG" ]; then
  GH_REPO="$REPO_FLAG"; export GH_REPO
elif [ -n "${GH_SHIP_REPO:-}" ]; then
  GH_REPO="$GH_SHIP_REPO"; export GH_REPO
fi

# A --repo invocation is "foreign" when it names a repo we cannot positively confirm is this
# checkout's own origin: either it differs from the derived origin, or that origin is unknown
# (a local/non-github remote — the case in the test harness). In cleanup, `git push origin
# --delete <branch>` targets THIS checkout's origin, not --repo's remote, so deleting there
# would hit the wrong repo (or a same-named branch in it). We therefore skip remote-branch
# deletion for a foreign invocation and let GitHub's auto-delete-head-branch (or a manual
# delete) handle the target repo. This is complementary to the fork-PR CROSS_REPO check
# below (which covers a PR from a fork of the SAME repo); both independently skip deletion.
#
# SCOPE (inherited from the pre-thin-delegator ship script this ports): only the gh calls
# (view / checks / merge) and the REMOTE-branch delete are made target-aware. The remaining
# LOCAL operations — the version-bump gate's version-file lookup ($ROOT), the CI-down local
# test runner ($ROOT), and the local worktree/branch preflight + cleanup ($BRANCH) — stay
# bound to the CWD checkout, exactly as the original documented ("cross-repo cleanup requires
# the PR's branch to exist locally"). In practice a foreign PR's branch name rarely collides
# with a local branch here, so this is a soft limitation, not a regression. Making those paths
# fully target-aware needs a target-checkout mapping and is tracked as a follow-up (see the PR
# / issue) rather than folded into this minimal restoration.
# Foreign-ness is derived from the EFFECTIVE gh target, not `REPO_FLAG` alone. `gh` honours
# THREE ways to name a target repo — `--repo`, and the `GH_SHIP_REPO`/`GH_REPO` environment
# vars — any of which points every gh call (view/merge) at another repo. If foreign-ness saw
# only `REPO_FLAG`, a target named via either env var would leave `_FOREIGN_REPO_INVOKE=0` and
# slip past every guard that keys on it (remote-branch delete, local cleanup, AND the
# empty-rollup local-gate path), running the local gate against the AMBIENT checkout and
# merging the wrong repo. So fold all three in (precedence matches how GH_REPO is set above:
# --repo, else GH_SHIP_REPO, else a pre-existing ambient GH_REPO). A target equal to this
# checkout's own origin is NOT foreign. (Residual: the compare is a raw string, so the same
# origin written in a different form — a `.git` suffix, a URL, a case change — reads as foreign
# and conservatively skips cleanup; that errs toward not touching the wrong repo.)
_FOREIGN_REPO_INVOKE=0
_EFFECTIVE_TARGET="${REPO_FLAG:-${GH_SHIP_REPO:-${GH_REPO:-}}}"
if [ -n "$_EFFECTIVE_TARGET" ] && [ "$_EFFECTIVE_TARGET" != "$_CWD_ORIGIN_REPO" ]; then
  _FOREIGN_REPO_INVOKE=1
fi

MAIN_CHECKOUT="${SHIP_MAIN_CHECKOUT:-$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')}"
# Guard the MAIN checkout NOW — ship's post-merge refresh runs git ops against it (checkout /
# fetch / pull), which would fail opaquely under core.bare. Firing before the merge means a
# hard abort here is safe: nothing is merged yet.
abort_if_core_bare "$MAIN_CHECKOUT" "main checkout"

# --- resolve PR state -----------------------------------------------------------------
read -r BRANCH STATE MERGEABLE CROSS_REPO MERGE_STATE < <(gh pr view "$PR" \
  --json headRefName,state,mergeable,isCrossRepository,mergeStateStatus \
  -q '[.headRefName,.state,.mergeable,.isCrossRepository,.mergeStateStatus]|@tsv')
[ -n "$BRANCH" ] || { echo "Could not resolve PR #$PR" >&2; exit 1; }
[ "$STATE" = "OPEN" ] || { echo "Refusing: PR #$PR is $STATE, not OPEN." >&2; exit 1; }
[ "$MERGEABLE" = "CONFLICTING" ] && { echo "Refusing: PR #$PR is CONFLICTING — resolve the conflict first." >&2; exit 1; }
if [ "${MERGE_STATE:-}" = "BEHIND" ]; then
  echo "Refusing: PR #$PR head is BEHIND its base. Update it (gh pr update-branch $PR --rebase), wait for CI, re-run." >&2
  exit 1
fi

# --- CI-down detection and local fallback gate -----------------------------------------
# When ALL (or ≥ 80%) CI checks fail and the failure pattern matches a structural outage
# (GitHub Actions down, billing suspended, runner quota exhausted) rather than real test
# failures, blocking the merge is unhelpful. The signals: all/most checks fail + either
# the GitHub status page shows Actions degradation OR the checks completed suspiciously
# fast (< 30s — faster than any real test suite can run). When both signals agree, the
# local gate runs instead: local tests + leftover-marker scan + PR checklist + threads.
# If local gates pass → merge. If they fail or detection is ambiguous → block normally.
#
# Knobs (env):
#   SHIP_CI_STATUS_URL   URL for the GitHub status components API (default: githubstatus.com).
#                        Override in tests to point at a local fake.
#   SHIP_TEST_CI_DOWN    Set to "1" to force-trigger the CI-down path (test-only shortcut,
#                        bypasses the detection heuristics). Never set in production.
#   SHIP_LOCAL_TEST_CMD  Override the local test command (test-only; default: auto-detect).
#
# Local test auto-detection order (see _local_test_runner): SHIP_LOCAL_TEST_CMD env var (if
# set) > $root/.ship-config file (see "Knobs (file)" above) > rig.yaml + `dev` CLI (root-only)
# > root-level pyproject.toml/package.json/Cargo.toml > the same three manifests in e2e/ ONLY,
# one level down (bounded — no deeper recursion, no arbitrary directory scan, and deliberately
# NOT test/tests/ — see the priority-5 comment in _local_test_runner) > fail closed with
# "no recognized test runner found". The config file
# outranks rig.yaml deliberately: it exists precisely to override a heuristic that guessed
# wrong, so it must win over the OTHER heuristic (rig.yaml/dev) too, not just auto-detect.
# Residual caveat (same trust boundary as rig.yaml/package.json, not a new one — see
# _ship_config_load below): a PR can add/edit .ship-config in the same commit that needs
# verifying, same as it could already add a stub `"test": "true"` script to package.json —
# this file does not change what a malicious PR could already get away with, review still
# has to look at test-affecting changes either way.

# --- known-flake gate (--known-flake NAME) --------------------------------------------
# A NARROWER sibling of the CI-down path above: ci_appears_structurally_down() only fires when
# ~80% of ALL checks fail (a whole-infrastructure outage). The far more common real shape is
# "one specific check is a flaky, already-broken-on-main test and every other check is green" —
# that does not look like an outage at all, so the CI-down path never triggers for it, and
# without this gate the only way through was a human escalation for something a quick check of
# the base branch's own recent CI history already answers (this repeated, avoidable escalation
# is exactly what this gate exists to close — see AGENTS.md's "CI billing-block" note in repos
# that document that history).
#
# $1 = check NAME as it appears in the rollup (.name // .context — the same string the green-CI
# gate's own refusal already prints). $2 = that check's workflowName (may be empty for a
# StatusContext, which has none), used to scope the base-branch lookup to the SAME workflow so a
# same-named check from an unrelated workflow can't manufacture a false match. $3 = the PR's
# ACTUAL base branch (baseRefName), resolved ONCE by the caller (see the call site) — NOT
# re-derived per check. Two reasons this is a required argument, not an internal lookup: (a) a
# `gh pr view` failure inside a per-check helper is easy to silently paper over with a
# same-shaped fallback (a real prior version of this function did exactly that — see the caller
# for why that direction is wrong for THIS gate specifically), and (b) it avoids re-querying the
# identical answer once per failing check.
#
# Evidence, not trust: queries the last SHIP_FLAKE_LOOKBACK_RUNS (default 5) COMPLETED runs of
# that workflow on $3 and requires a job of the SAME name to have FAILED in at least one of
# them. A flake does not have to fail every run — that is what makes it a flake rather than a
# hard break — so ANY match in the window is sufficient; the absence of any match is what
# refuses the claim. Fail-closed throughout: a gh/jq read failure, an empty run list, or no
# matching failure anywhere in the window all return 1 (NOT confirmed) — the caller then treats
# the check as a genuine, blocking failure exactly as if --known-flake had never been passed
# for it.
#
# Scope, stated plainly: this confirms "this check NAME has also failed recently on the base
# branch, independent of this PR" — job-name granularity, not a guarantee that the SAME
# sub-test/assertion failed both times (a job that bundles a large suite, like this repo's own
# monorepo-wide "Tests" check, could in principle fail for two DIFFERENT reasons on two
# different runs and still match here). That is a real, accepted limitation, not an oversight —
# a shipper should still glance at the failure output before asserting the flag (the refusal
# message this gate's caller prints tells them exactly what failed), and the base-branch match
# is logged to SHIP_AUDIT_FILE precisely so an asserted-but-wrong claim is reviewable after the
# fact, same as the review-quorum and skip-ci gates already are.
_known_flake_confirmed() {
  local check_name="$1" wf_name="$2" base="$3" runs_json rid concl jobs_json match found=0 n
  [ -n "$base" ] || return 1
  n="${SHIP_FLAKE_LOOKBACK_RUNS:-5}"
  case "$n" in ''|*[!0-9]*) n=5 ;; esac
  [ "$n" -gt 0 ] || n=5
  # Clamped upper bound: an unbounded SHIP_FLAKE_LOOKBACK_RUNS turns the refusal path (every
  # candidate run inspected, none matching) into dozens-to-hundreds of sequential `gh run view`
  # calls — minutes of wall time before ship even gets to refuse. 25 is generous for "how many
  # recent runs could plausibly contain the same flake" while keeping the worst case bounded.
  [ "$n" -le 25 ] || n=25
  if [ -n "$wf_name" ]; then
    runs_json=$(gh run list --branch "$base" --workflow "$wf_name" --status completed --limit "$n" \
      --json databaseId,conclusion 2>/dev/null) || return 1
  else
    runs_json=$(gh run list --branch "$base" --status completed --limit "$n" \
      --json databaseId,conclusion 2>/dev/null) || return 1
  fi
  [ -n "$runs_json" ] && [ "$runs_json" != "null" ] || return 1
  while IFS=$'\t' read -r rid concl; do
    [ -n "$rid" ] || continue
    # A fully green run cannot contain a failing job with this name — cheap skip before the
    # extra `gh run view` call that would otherwise be needed for every recent run.
    case "$concl" in failure|cancelled|timed_out|action_required) : ;; *) continue ;; esac
    jobs_json=$(gh run view "$rid" --json jobs -q '.jobs' 2>/dev/null) || continue
    match=$(printf '%s' "$jobs_json" | jq -r --arg n "$check_name" \
      '(. // [])[] | select(.name == $n) | .conclusion' 2>/dev/null)
    if printf '%s\n' "$match" | grep -qx "failure"; then
      found=1
      echo "[ship] known-flake evidence: '$check_name' also FAILED on ${base} run ${rid} (recent, same workflow) — treating as pre-existing, not introduced by this PR." >&2
      break
    fi
  done < <(printf '%s' "$runs_json" | jq -r '(. // [])[] | [(.databaseId|tostring), .conclusion] | @tsv' 2>/dev/null)
  [ "$found" = "1" ]
}

# One known-flake audit line -> SHIP_AUDIT_FILE, mirroring _skip_ci_audit_log's shape/dry-run
# contract. $1 = decision (confirmed | refused), $2 = space-joined check names asserted.
_known_flake_audit_log() {
  local decision="$1" checks="$2"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would append known-flake audit: decision=${decision} checks=${checks}" >&2
    return 0
  fi
  local file="${SHIP_AUDIT_FILE:-$HOME/.config/agent-tools/ship-audit.jsonl}"
  mkdir -p "$(dirname "$file")" 2>/dev/null || return 0
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg ts "$ts" --arg pr "$PR" --arg dec "$decision" --arg gate "known-flake" \
      --arg checks "$checks" \
      '{ts:$ts, pr:$pr, gate:$gate, decision:$dec, checks:$checks}' \
      >> "$file" 2>/dev/null || true
  else
    local esc_pr esc_checks
    esc_pr=$(printf '%s' "$PR" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    esc_checks=$(printf '%s' "$checks" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '{"ts":"%s","pr":"%s","gate":"known-flake","decision":"%s","checks":"%s"}\n' \
      "$ts" "$esc_pr" "$decision" "$esc_checks" >> "$file" 2>/dev/null || true
  fi
}

# Query the GitHub status page for Actions component health.
# Stdout: "degraded" if Actions is not fully operational, "ok" if fine, "unknown" on error.
_ci_github_status_indicator() {
  local url resp indicator
  url="${SHIP_CI_STATUS_URL:-https://www.githubstatus.com/api/v2/components.json}"
  resp=$(curl -sS --max-time 10 "$url" 2>/dev/null) || { echo "unknown"; return; }
  command -v jq >/dev/null 2>&1 || { echo "unknown"; return; }
  # Look for a component whose name matches "Actions" (case-insensitive).
  indicator=$(printf '%s' "$resp" \
    | jq -r '.components[]? | select(.name | test("Actions";"i")) | .status' 2>/dev/null \
    | head -1 || true)
  case "${indicator:-}" in
    operational) echo "ok" ;;
    "")          echo "unknown" ;;
    *)           echo "degraded" ;;
  esac
}

# Check whether ALL workflow runs for the PR's head SHA completed suspiciously fast
# (< 30 s from creation to update) — a sign of infra/billing failure, not real tests.
# Stdout: "1" if suspicious, "0" otherwise.
#
# NOTE: this catches an outage where the RUN itself never queues (fails immediately),
# but NOT a billing block where GitHub queues the run for a while before rejecting the
# job — see _ci_billing_block_annotation_detected below for that shape, confirmed live
# 2026-09-09 (sme-archiving-gc PR #401): job started_at→completed_at was 2s, but the
# run's own created_at→updated_at was 2m33s, well past this function's 30s cutoff. Keep
# both signals — this one still catches a same-second full-run rejection this repo
# hasn't hit yet, the annotation check catches the queued-then-rejected shape.
_ci_runs_timing_suspicious() {
  local head_sha="$1" runs_json total fast
  runs_json=$(gh api "repos/{owner}/{repo}/actions/runs?head_sha=$head_sha&per_page=20" \
    2>/dev/null) || { echo "0"; return; }
  total=$(printf '%s' "$runs_json" | jq '.workflow_runs | length' 2>/dev/null || echo 0)
  [ "${total:-0}" -gt 0 ] || { echo "0"; return; }
  # Count completed runs whose wall-clock (created_at → updated_at) was under 30 seconds.
  fast=$(printf '%s' "$runs_json" | jq '
    [ .workflow_runs[]
      | select(.status == "completed")
      | ((.updated_at | fromdateiso8601) - (.created_at | fromdateiso8601))
      | select(. < 30) ] | length' 2>/dev/null || echo 0)
  [ "$fast" = "$total" ] && [ "$total" -gt 0 ] && echo "1" || echo "0"
}

# Check every FAILED check-run on the PR's head SHA for GitHub's own fixed billing-block
# annotation text ("recent account payments have failed" / "spending limit needs to be
# increased" — the literal message GitHub attaches to a check run whose job never started
# because the account can't pay for compute). This is a direct, unambiguous signal, unlike
# the timing heuristic above: a billing-blocked run can sit queued for minutes before
# GitHub marks it completed, so its wall-clock span often does NOT look "suspiciously
# fast" even though the job itself executed zero real steps.
# Stdout: "1" if the billing-block message is found on any failed check run, "0" otherwise.
_ci_billing_block_annotation_detected() {
  local head_sha="$1" runs_json failed_ids id ann_json
  runs_json=$(gh api "repos/{owner}/{repo}/commits/$head_sha/check-runs?per_page=100" \
    2>/dev/null) || { echo "0"; return; }
  failed_ids=$(printf '%s' "$runs_json" \
    | jq -r '.check_runs[]? | select(.conclusion == "failure") | .id' 2>/dev/null)
  [ -n "$failed_ids" ] || { echo "0"; return; }
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    ann_json=$(gh api "repos/{owner}/{repo}/check-runs/$id/annotations" 2>/dev/null) || continue
    if printf '%s' "$ann_json" | jq -e '
        .[]? | select(.message
          | test("account payments have failed|spending limit needs to be increased"; "i"))
      ' >/dev/null 2>&1; then
      echo "1"; return
    fi
  done <<< "$failed_ids"
  echo "0"
}

# Return 0 (exit code) if CI appears structurally down rather than genuinely failing.
# $1 = total check count, $2 = failed check count
# Uses SHIP_TEST_CI_DOWN=1 to force the path in tests without network calls.
ci_appears_structurally_down() {
  local total="$1" failed="$2"
  # Test escape hatch: force-trigger detection without the heuristics.
  [ "${SHIP_TEST_CI_DOWN:-0}" = "1" ] && return 0
  [ "${total:-0}" -gt 0 ] || return 1
  # Only fire when ≥ 80% of checks failed — a partial failure is a real failure.
  # Use cross-multiplication (failed*10 >= total*8) to avoid the integer-truncation
  # rounding error that `total * 8 / 10` gives for small totals (e.g. 2 checks:
  # 2*8/10=1, so 1/2=50% would falsely satisfy the threshold).
  [ $(( failed * 10 )) -ge $(( total * 8 )) ] || return 1
  # Get the PR head SHA for timing analysis.
  local head_sha timing status billing
  head_sha=$(gh pr view "$PR" --json headRefOid -q '.headRefOid' 2>/dev/null) || head_sha=""
  status=$(_ci_github_status_indicator)
  timing="0"
  billing="0"
  if [ -n "$head_sha" ]; then
    timing=$(_ci_runs_timing_suspicious "$head_sha")
    billing=$(_ci_billing_block_annotation_detected "$head_sha")
  fi
  echo "[ship] CI-down probe: github_status=$status timing_suspicious=$timing billing_block=$billing total=$total failed=$failed" >&2
  # Positive detection if any signal confirms structural failure.
  [ "$status" = "degraded" ] || [ "$timing" = "1" ] || [ "$billing" = "1" ] || return 1
  return 0
}

# Parse the audited per-repo config file $root/.ship-config, if present. Sets two globals
# for the caller: SHIP_CFG_DIR and SHIP_CFG_CMD (each "" when unset/absent/rejected). Only
# whole-line `#` comments and the two whitelisted `KEY=value` keys are recognized — the file
# is never eval'd itself. Any non-blank, non-comment line that doesn't match one of the two
# keys is logged and ignored (not silently dropped) so a typo doesn't silently downgrade the
# gate to auto-detection.
#
# The file is read from the last COMMITTED content at HEAD (`git show HEAD:.ship-config`),
# never the working tree — the "audited, committed" trust story in the header doc is an
# enforced property, not just a claim. Reading the worktree copy would let a tracked-but-
# locally-modified file (or a staged-but-never-committed one) take effect with no audit
# trail; reading HEAD means only content that has actually landed in history — reviewed
# like any other committed change — can ever run. A file present in the working tree but
# absent/uncommitted at HEAD is ignored with a warning, exactly as if it didn't exist.
#
# SHIP_LOCAL_TEST_DIR is rejected (logged) if it is an absolute path, contains a `..`
# component, or resolves to the repo root itself (`.`, `./`, or any all-`.`-segments path).
# Rejection invalidates the WHOLE file (both keys cleared), not just the DIR — a rejected
# dir alongside a still-present SHIP_LOCAL_TEST_CMD must not silently relocate that command
# to run from the repo root instead of the (rejected) directory the author asked for; that
# would verify a different suite than intended. Detection then proceeds exactly as if the
# file didn't exist, per the header doc.
#
# Threat model: $root/.ship-config's committed content is under the SAME trust boundary as
# rig.yaml/package.json (both already dictate what the gate runs) — a PR that edits it is
# reviewed like any other change, this does not add a new attack surface (see the
# header-doc caveat above). The DIR safety check is accident-prevention (a typo'd
# absolute/traversal/root path), not a security boundary — SHIP_LOCAL_TEST_CMD is arbitrary
# eval'd shell regardless of DIR.
_ship_config_load() {
  # $1 = root. $2 = ref to read .ship-config FROM (default HEAD). Callers whose knobs describe
  # the PR's OWN code/tests (SHIP_LOCAL_TEST_DIR/CMD, SHIP_ACCEPTANCE_GATE) legitimately want
  # HEAD. A caller gating a merge-safety property that a PR must not be able to grant itself
  # (SHIP_EXTERNAL_REVIEW — see that gate) MUST pass an already-reviewed ref instead, e.g.
  # `origin/<baseRefName>` — see agent-tools#600 review round 1 (a self-committed HEAD read
  # would let a PR disable the anti-#764 gate on its own merge).
  local root="$1" ref="${2:-HEAD}" content line key val
  # These four globals are OVERWRITTEN on every call, from whichever $ref that call used — there
  # is no per-ref namespacing. A caller MUST consume the value(s) it needs immediately (snapshot
  # into a caller-owned variable if anything else might run before it reads them), never assume
  # they still hold this call's data after another _ship_config_load call runs (review-cli
  # finding, agent-tools#600 round 2 — flagged as a real footgun even though no caller in this
  # script's current call order is actually bitten by it today).
  SHIP_CFG_DIR=""
  SHIP_CFG_CMD=""
  SHIP_CFG_ACCEPTANCE_GATE=""
  SHIP_CFG_EXTERNAL_REVIEW=""
  if ! (cd "$root" && git show "$ref:.ship-config") >/dev/null 2>&1; then
    [ "$ref" = "HEAD" ] && [ -f "$root/.ship-config" ] && \
      echo "[ship] local gate: .ship-config exists in the working tree but is not committed at HEAD — ignoring (uncommitted config is not audited)." >&2
    return 0
  fi
  # $ref:.ship-config must be a REGULAR FILE — mode 100644/100755, checked via `git ls-tree`,
  # not merely `git cat-file -t` == blob. Two non-regular tree entries are ALSO type `blob`
  # and would slip past a bare type check:
  #   - A TREE (someone committed a `.ship-config/` directory): `git show`/`git cat-file -s`
  #     both succeed on it and print a tree listing — tree entry names are plain filenames
  #     that can legally contain `=` and spaces, so an entry literally named
  #     `SHIP_LOCAL_TEST_CMD=<cmd>` would otherwise flow into the KEY=value parser below as
  #     if it were committed file CONTENT.
  #   - A SYMLINK (git mode 120000, e.g. `.ship-config -> /some/attacker/path`): its blob
  #     content is the link TARGET STRING, not test-runner config — `git cat-file -t` reports
  #     `blob` for symlinks too, so a type-only check would parse a target path as if it were
  #     a committed KEY=value line.
  # `git ls-tree` reports the tree ENTRY's mode (unlike `cat-file -t`, which resolves to the
  # blob's own type and can't distinguish a symlink's blob from a regular file's blob).
  local head_mode
  head_mode=$(cd "$root" && git ls-tree "$ref" -- .ship-config 2>/dev/null | awk '{print $1}')
  case "$head_mode" in
    100644|100755) ;;
    *)
      echo "[ship] local gate: .ship-config at $ref is not a regular file (mode ${head_mode:-unknown}, not 100644/100755) — ignoring." >&2
      return 0
      ;;
  esac
  # NUL-byte guard: bash silently STRIPS NUL bytes when a command substitution's output
  # becomes a variable's value (below), which could turn a byte sequence that never spells
  # a whitelisted key in the actual committed bytes (e.g. `SHIP_LOCAL_TEST_C<NUL>MD=...`)
  # into one that does once assigned to $content. Check the RAW git-show output (piped
  # straight to grep, never touching a bash variable) for a NUL byte first and refuse to
  # parse at all if found — a legitimate KEY=value text config never contains one. `grep -I`
  # (without `-a`) treats a NUL-containing input as binary and reports NO match even against
  # the empty pattern, while a plain-text input matches the (vacuously true) empty pattern —
  # note this is NOT `grep -a $'\0'`: bash's ANSI-C quoting truncates at the first NUL, so
  # that pattern silently becomes an EMPTY string and would match (and thus "reject") every
  # normal file — a bug caught by this diff's own test suite before it shipped. A genuinely
  # empty (0-byte) file ALSO fails `grep -I ''` (no lines to match at all) despite having no
  # NUL byte, so that case is excluded via a blob-size check first. Deliberately NOT `-q`:
  # under this script's `set -o pipefail`, `grep -q` can exit (and close its stdin pipe) as
  # soon as it sees a match, which for a config larger than the OS pipe buffer would SIGPIPE
  # the upstream `git show` and misreport a perfectly valid large text file as "binary" —
  # without `-q`, grep must read every line to emit them all, so `git show` always completes.
  local blob_size
  blob_size=$(cd "$root" && git cat-file -s "$ref:.ship-config" 2>/dev/null) || blob_size=0
  if [ "$blob_size" -gt 0 ] && ! (cd "$root" && git show "$ref:.ship-config" 2>/dev/null) | grep -I '' >/dev/null; then
    echo "[ship] local gate: .ship-config committed content contains a NUL byte (binary/corrupt) — refusing to parse, ignoring." >&2
    return 0
  fi
  content=$(cd "$root" && git show "$ref:.ship-config" 2>/dev/null)
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    case "$line" in
      ''|'#'*) continue ;;
      SHIP_LOCAL_TEST_DIR=*|SHIP_LOCAL_TEST_CMD=*|SHIP_ACCEPTANCE_GATE=*|SHIP_EXTERNAL_REVIEW=*)
        key="${line%%=*}"
        val="${line#*=}"
        val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        case "$key" in
          SHIP_LOCAL_TEST_DIR) SHIP_CFG_DIR="$val" ;;
          SHIP_LOCAL_TEST_CMD) SHIP_CFG_CMD="$val" ;;
          SHIP_ACCEPTANCE_GATE) SHIP_CFG_ACCEPTANCE_GATE="$val" ;;
          SHIP_EXTERNAL_REVIEW) SHIP_CFG_EXTERNAL_REVIEW="$val" ;;
        esac
        ;;
      *)
        echo "[ship] local gate: .ship-config: ignoring unrecognized line: $line" >&2 ;;
    esac
  done <<< "$content"
  if [ -n "$SHIP_CFG_DIR" ] && ! _ship_config_dir_is_safe "$SHIP_CFG_DIR"; then
    echo "[ship] local gate: .ship-config: SHIP_LOCAL_TEST_DIR '$SHIP_CFG_DIR' is not a safe repo-relative subdirectory (absolute, a '..' path component, or '.'/repo root itself — omit the key to mean root) — ignoring the whole file." >&2
    SHIP_CFG_DIR=""
    SHIP_CFG_CMD=""
    SHIP_CFG_ACCEPTANCE_GATE=""
    SHIP_CFG_EXTERNAL_REVIEW=""
  fi
}

# True (exit 0) if $1 is a safe repo-relative subdirectory reference: not absolute, no `..`
# PATH COMPONENT (a component match, not a substring match — a dir legitimately named
# `v1..2` or `foo..bar` must NOT be rejected), and does not resolve to "the repo root
# itself" — every path component being `.` (or empty, from a trailing/duplicate slash),
# e.g. `.`, `./`, `././.` — which would defeat the `mode="root"` pytest-args guarantee in
# _local_test_try_dir if silently routed through non-root auto-detect. Omit the key
# instead to scope to root.
_ship_config_dir_is_safe() {
  local p="$1" seg all_dot=1
  local -a _ship_cfg_dir_segs
  case "$p" in /*) return 1 ;; esac
  IFS='/' read -ra _ship_cfg_dir_segs <<< "$p"
  for seg in "${_ship_cfg_dir_segs[@]}"; do
    [ "$seg" = ".." ] && return 1
    [ "$seg" != "." ] && [ -n "$seg" ] && all_dot=0
  done
  [ "$all_dot" -eq 1 ] && return 1
  return 0
}

# True (exit 0) if $2 (a real, existing directory) is a STRICT physical descendant of $1
# (root) — i.e. inside it, but not equal to it — resolving symlinks on BOTH sides via
# `cd ... && pwd -P`. _ship_config_dir_is_safe only checks the CONFIGURED string lexically
# (absolute/`..`/root-equivalent); this additionally catches a directory — or any path
# component on the way to it — being a symlink that resolves OUTSIDE the repo, or a symlink
# that resolves to the root itself (`SHIP_LOCAL_TEST_DIR=suite` with `suite -> .`, which
# would otherwise bypass the lexical root-equivalence rejection). NOTE — scope: this check
# only defends the repo-boundary case; it does NOT and cannot generally prevent a symlink
# that redirects a candidate to a DIFFERENT directory that is still inside the repo (e.g.
# `e2e -> tests/fixture`) — a committed symlink like that is visible in the PR diff under
# the same review trust boundary as any other change here, not a new attack surface this
# helper is meant to close. Call this on every directory actually about to be `cd`'d into
# for testing (both the .ship-config-scoped dir and the priority-5 e2e/ candidate).
_dir_is_real_descendant_of_root() {
  local root="$1" dir="$2" real_root real_dir
  real_root=$(cd "$root" 2>/dev/null && pwd -P) || return 1
  real_dir=$(cd "$dir" 2>/dev/null && pwd -P) || return 1
  case "$real_dir" in
    "$real_root") return 1 ;;
    "$real_root"/*) return 0 ;;
    *) return 1 ;;
  esac
}

# Try auto-detecting a test manifest in directory $1 and run its matching command from
# there. $2 (optional) = "root" to force the classic unconditional `pytest tests/ -q`
# invocation (byte-for-byte the pre-existing root behavior — never loosened to a bare
# `pytest -q` repo-wide-discovery run just because this helper now also serves
# subdirectories); any other value (or omitted) auto-detects whether `$dir/tests` exists,
# which is only correct for a non-root $dir where the manifest's own directory IS the suite.
#
# Sets the global _LOCAL_TEST_MATCHED to 1 if a manifest was found (regardless of whether
# the test command itself passed or failed), or 0 if $1 has none of the three recognized
# manifests. Callers MUST branch on _LOCAL_TEST_MATCHED, never on this function's own return
# code, to decide whether to keep probing other candidates: the wrapped test command's exit
# status is returned as-is (e.g. pytest can legitimately exit 2 on a usage error/
# interruption), so overloading "no manifest here" onto a specific exit code would collide
# with a real test failure and risk running a DIFFERENT suite that happens to pass.
_local_test_try_dir() {
  local dir="$1" mode="${2:-auto}"
  _LOCAL_TEST_MATCHED=0
  if [ -f "$dir/pyproject.toml" ]; then
    _LOCAL_TEST_MATCHED=1
    echo "[ship] local gate: running pytest ($dir/pyproject.toml detected) ..."
    local -a pytest_args=(tests/ -q)
    if [ "$mode" != "root" ] && [ ! -d "$dir/tests" ]; then
      pytest_args=(-q)
    fi
    if command -v uv >/dev/null 2>&1; then
      (cd "$dir" && uv run --with pytest pytest "${pytest_args[@]}") 2>&1; return $?
    else
      (cd "$dir" && python3 -m pytest "${pytest_args[@]}") 2>&1; return $?
    fi
  fi
  if [ -f "$dir/package.json" ]; then
    _LOCAL_TEST_MATCHED=1
    echo "[ship] local gate: running npm test ($dir/package.json detected) ..."
    (cd "$dir" && npm test) 2>&1; return $?
  fi
  if [ -f "$dir/Cargo.toml" ]; then
    _LOCAL_TEST_MATCHED=1
    echo "[ship] local gate: running cargo test ($dir/Cargo.toml detected) ..."
    (cd "$dir" && cargo test) 2>&1; return $?
  fi
  return 0
}

# Execute the audited $root/.ship-config override (see "Knobs (file)" in the header doc and
# _ship_config_load's docstring). Sets the global _SHIP_CFG_ACTED to 1 if there was a config
# to act on (regardless of whether the resulting command/detection passed or failed), or 0 if
# neither key was set (no file / not committed at HEAD / rejected). Callers MUST branch on
# _SHIP_CFG_ACTED, never on this function's own return code, for the exact same reason
# _local_test_try_dir uses _LOCAL_TEST_MATCHED instead of its return code: the configured
# command can legitimately exit 2 (pytest usage error, an arbitrary script's own exit 2), which
# would collide with a return-code sentinel for "nothing configured" and risk silently falling
# through to a DIFFERENT, passing heuristic even though the audited, explicitly-configured
# suite actually failed — exactly the failure mode this whole file exists to avoid. The
# SHIP_LOCAL_TEST_DIR existence check lives in exactly ONE place here (both the DIR+CMD and
# DIR-only paths need it).
_ship_config_run() {
  local root="$1" status
  _ship_config_load "$root"
  _SHIP_CFG_ACTED=0
  if [ -z "$SHIP_CFG_CMD" ] && [ -z "$SHIP_CFG_DIR" ]; then
    return 0
  fi
  _SHIP_CFG_ACTED=1
  if [ -n "$SHIP_CFG_DIR" ]; then
    if [ ! -d "$root/$SHIP_CFG_DIR" ]; then
      echo "[ship] local gate: FAILED — SHIP_LOCAL_TEST_DIR '$SHIP_CFG_DIR' does not exist under $root." >&2
      return 1
    fi
    if ! _dir_is_real_descendant_of_root "$root" "$root/$SHIP_CFG_DIR"; then
      echo "[ship] local gate: FAILED — SHIP_LOCAL_TEST_DIR '$SHIP_CFG_DIR' resolves (via a symlink) outside the repo root." >&2
      return 1
    fi
  fi
  if [ -n "$SHIP_CFG_CMD" ]; then
    if [ -n "$SHIP_CFG_DIR" ]; then
      echo "[ship] local gate: .ship-config: running in $SHIP_CFG_DIR: $SHIP_CFG_CMD"
      (cd "$root/$SHIP_CFG_DIR" && eval "$SHIP_CFG_CMD") 2>&1; return $?
    fi
    echo "[ship] local gate: .ship-config: running: $SHIP_CFG_CMD"
    (cd "$root" && eval "$SHIP_CFG_CMD") 2>&1; return $?
  fi
  echo "[ship] local gate: .ship-config: scoping auto-detect to $SHIP_CFG_DIR"
  _local_test_try_dir "$root/$SHIP_CFG_DIR"; status=$?
  [ "$_LOCAL_TEST_MATCHED" -eq 1 ] && return "$status"
  echo "[ship] local gate: FAILED — no recognized test runner found in $SHIP_CFG_DIR (no pyproject.toml/package.json/Cargo.toml)." >&2
  return 1
}

_local_test_runner() {
  local root="$1" dev_status status

  # Priority 1: test-only env override — never set in production (see header doc).
  if [ -n "${SHIP_LOCAL_TEST_CMD:-}" ]; then
    echo "[ship] local gate: running test command: $SHIP_LOCAL_TEST_CMD"
    eval "$SHIP_LOCAL_TEST_CMD" 2>&1; return $?
  fi

  # Priority 2: audited per-repo $root/.ship-config (see "Knobs (file)" in the header doc).
  # An explicit, committed override outranks every heuristic below it, INCLUDING rig.yaml —
  # it exists precisely to correct a case where a heuristic (auto-detect OR rig.yaml) guesses
  # wrong, so it must win over both, not just auto-detect.
  _ship_config_run "$root"; status=$?
  [ "$_SHIP_CFG_ACTED" -eq 1 ] && return "$status"

  # Priority 3: rig.yaml + `dev` CLI probe — root-only, unchanged from prior behavior.
  if [ -f "$root/rig.yaml" ] && command -v dev >/dev/null 2>&1 && dev --agenttools-dev-probe >/dev/null 2>&1; then
    if (cd "$root" && dev has-script --repo-only test >/dev/null 2>&1); then
      dev_status=0
    else
      dev_status=$?
    fi
    if [ "$dev_status" -eq 0 ]; then
      echo "[ship] local gate: running dev run --repo-only test (rig.yaml scripts.test)"
      (cd "$root" && dev run --repo-only test) 2>&1; return $?
    fi
    if [ "$dev_status" -ne 1 ]; then
      echo "[ship] local gate: FAILED — dev has-script --repo-only test failed; refusing to guess a fallback test runner." >&2
      return 1
    fi
  fi

  # Priority 4: root-level auto-detect (existing behavior, unchanged — "root" mode keeps
  # the classic unconditional `pytest tests/ -q`, byte-for-byte the pre-existing behavior).
  _local_test_try_dir "$root" root; status=$?
  [ "$_LOCAL_TEST_MATCHED" -eq 1 ] && return "$status"

  # Priority 5: bounded subdirectory auto-detect, ONE candidate only (e2e/), one level deep.
  # Handles the concrete monorepo-of-fixtures shape this feature targets (e.g. hyper-ext-e2e's
  # e2e/package.json) where the root has no manifest but e2e/ does. Deliberately narrow: an
  # earlier version of this also guessed test/ and tests/, but review flagged (repeatedly,
  # across independent rounds) that those two names are exactly where repos most often keep
  # FIXTURE manifests (a package.json/Cargo.toml with a trivially-passing test used for
  # something else entirely) — auto-running one would silently convert a conservative
  # fail-closed block into a false "verified" pass. e2e/ is kept because it is the concrete,
  # unambiguous case that motivated this feature (#309); any other/ambiguous subdirectory
  # name is exactly what .ship-config (priority 2) exists for — use it instead of growing
  # this list, do not recurse further or scan arbitrary directories.
  if [ -d "$root/e2e" ]; then
    if _dir_is_real_descendant_of_root "$root" "$root/e2e"; then
      _local_test_try_dir "$root/e2e"; status=$?
      [ "$_LOCAL_TEST_MATCHED" -eq 1 ] && return "$status"
    else
      echo "[ship] local gate: e2e/ resolves (via a symlink) outside the repo root — skipping the priority-5 candidate (same guard as SHIP_LOCAL_TEST_DIR; use .ship-config for an intentional alias)." >&2
    fi
  fi

  echo "[ship] local gate: FAILED — no recognized test runner found (no pyproject.toml/package.json/Cargo.toml at root or in e2e/)." >&2
  echo "[ship]   CI is down but tests cannot be verified locally — blocking conservatively." >&2
  return 1
}

# Scan the PR diff additions for leftover markers: an unfinished-work marker
# ("[T]ODO"/"[F]IXME"/"[H]ACK") or a standalone "[X]XX" marker.
# Returns 0 if clean, 1 if markers found or diff cannot be read.
#
# Bracket-expression trick on the FIRST letter of each marker (`[T]ODO` instead of
# spelling it bare): functionally IDENTICAL to the plain literal in an ERE — a
# single-character bracket class matches exactly that one character, same as writing
# it bare — but it means this file's own source (this line, and every comment/fixture
# that needs to spell an example) never contains any marker as ONE contiguous token.
# This is the standard "grep must not match its own pattern" idiom (the same trick as
# `ps aux | grep '[f]oo'` to exclude the grep process itself from its own output).
#
# Why this matters here specifically: this exact regex line, its own explanatory
# comments, and the fixtures in tests/test_ship.py that exercise it ALL legitimately
# need to spell these marker strings as literal example/test data. Without the
# bracket-expression split, ANY PR editing this gate's own implementation or tests
# would self-trigger the CI-outage fallback, regardless of whether it added a REAL
# leftover marker (agent-tools#318, found reviewing #317).
#
# Two designs were tried and rejected before this one (both add an exemption
# mechanism instead of removing the self-match at the source):
#   - An inline sentinel comment marking "ignore from here to here" inside the diff
#     content itself — rejected: the diff being scanned IS the untrusted PR content,
#     so an unclosed or duplicated sentinel written by the PR author would silently
#     blind the scanner to every file after it in the whole diff.
#   - A file-path exemption (skip any hunk whose diff header names this file or its
#     test file) — works and has no injection surface if hunk-boundary-aware, but
#     blanket-exempts ALL additions to those 2 files from this LOCAL FALLBACK gate,
#     opening a blind window exactly where a motivated author would most want it
#     closed: CI down + editing the gate itself + a genuine forgotten marker.
# The bracket-expression split has neither downside: no new mechanism, no exemption,
# no blind window — this file's own source simply never contains a literal match, so
# there's nothing to exempt. Unlike the file-path-exemption design, it needs no
# coordination with the CI-up ci/leftover-grep/leftover-grep.sh gate to stay
# consistent: this scanner just doesn't have a self-reference problem to solve.
# (leftover-grep.sh's OWN detector line has the identical literal-marker self-match
# exposure for a PR that edits leftover-grep.sh itself — not fixed here, since it's a
# separate file/gate outside agent-tools#318's scope; tracked as agent-tools#330. A
# related pre-existing diff-header-parsing false negative in both gates — a real
# added line that itself starts with "++" collides with the diff's own "+++ b/path"
# header prefix — is tracked as agent-tools#329.)
#
# The [X]XX marker alone gets an additional run-length guard —
# `(^|[^X])[X]XX($|[^X])`, a portable "no word-boundary regex, no grep -w" idiom in
# the same style this repo already uses in ci/leftover-grep/leftover-grep.sh's
# focused-test check — because a plain substring match collides with bash's
# conventional mktemp template suffix (`mktemp -d "/tmp/foo.XXXXXX"`), a common and
# legitimate pattern (agent-tools#316). This only excludes a marker inside a longer
# run of X's (4+), so it deliberately still catches one embedded in an identifier,
# e.g. an [X]XX-prefixed sentinel like `[X]XX_REMOVE_BEFORE_MERGE` or
# `foo[X]XX_debug()` — unlike a full word-boundary fix, which would also let those
# slip through (checked against neighboring X vs non-X only, not against "is this a
# word character"). Residual limitation, accepted as-is: a 3-character mktemp
# template (`foo.[X]XX`) is indistinguishable from a real standalone marker and still
# blocks; this is rare/weak enough (bash's own docs recommend 6+ X's) not to warrant
# a smarter check.
#
# The other three markers deliberately keep plain substring matching (unlike [X]XX).
# For [T]ODO/[F]IXME this local fallback gate must stay at least as strict as
# ci/leftover-grep/leftover-grep.sh's untracked-marker check (that gate greps for
# `[T]ODO|[F]IXME`, also substring, so it catches sentinel identifiers like
# `[T]ODO_REMOVE_BEFORE_MERGE`): loosening these two to whole-word would let such
# genuine leftovers slip through the CI-outage fallback while the normal CI-up gate
# still catches them — a fail-open divergence between the two gates. [H]ACK has no
# such parity requirement — ci/leftover-grep/leftover-grep.sh does NOT check for it at
# all, so this fallback gate is deliberately STRICTER than the CI-up gate on [H]ACK,
# not matched to it (a PR-time [H]ACK passes normal CI but blocks under this
# CI-outage-only fallback; accepted, since a stricter fallback is a safe direction to
# diverge in). There's no known real-world false positive for these three that would
# justify loosening any of them (mktemp templates are an [X]XX-specific nuisance; a
# project name or filename that happens to contain one of these four
# letter-sequences as a substring is a much rarer nuisance than a missed marker).
_local_leftover_check() {
  local pr="$1" diff_out
  echo "[ship] local gate: scanning PR diff for leftover markers ..."
  diff_out=$(gh pr diff "$pr" 2>/dev/null) || {
    echo "[ship] local gate: FAILED — could not read PR diff for leftover scan." >&2; return 1; }
  local hits
  hits=$(printf '%s\n' "$diff_out" \
    | grep -E '^\+' | grep -vE '^\+\+\+' \
    | grep -E '([T]ODO|[F]IXME|[H]ACK)|(^|[^X])[X]XX($|[^X])' || true)
  if [ -n "$hits" ]; then
    echo "[ship] local gate: FAILED — leftover markers in PR additions:" >&2
    printf '%s\n' "$hits" >&2
    return 1
  fi
  echo "[ship] local gate: leftover-marker scan OK."
  return 0
}

# Check PR body for unchecked checklist items (- [ ] lines).
# Returns 0 if no unchecked boxes, 1 otherwise.
_local_pr_checklist_check() {
  local pr="$1" body unchecked
  echo "[ship] local gate: checking PR checklist ..."
  body=$(gh pr view "$pr" --json body -q '.body // ""' 2>/dev/null) || {
    echo "[ship] local gate: FAILED — could not read PR body for checklist check." >&2; return 1; }
  # `grep -c` exits 1 when the count is zero; `|| true` suppresses that exit code
  # without emitting extra output (using `|| echo 0` would double the value: grep
  # already outputs "0" on stdout before exiting 1, then echo adds another "0").
  unchecked=$(printf '%s\n' "$body" | grep -cE '^- \[ \]' 2>/dev/null || true)
  if [ "${unchecked:-0}" -gt 0 ]; then
    echo "[ship] local gate: FAILED — PR has $unchecked unchecked checklist item(s)." >&2
    return 1
  fi
  echo "[ship] local gate: PR checklist OK."
  return 0
}

# Check unresolved review threads via the paginating GraphQL query. `gh pr view --json reviewThreads`
# is NOT a valid field (gh rejects it: "Unknown JSON field"), so the REST/`pr view` shape used to
# fail the gate unconditionally; use the same `reviewThreads` GraphQL query the main gate uses.
# Returns 0 if none, 1 if any unresolved or the query fails.
_local_review_threads_check() {
  local pr="$1" unresolved raw
  local q='query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{isResolved}}}}}'
  echo "[ship] local gate: checking review threads ..."
  raw=$(gh api graphql --paginate -F owner='{owner}' -F name='{repo}' -F pr="$pr" -f query="$q" \
    --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length' 2>/dev/null) || {
    echo "[ship] local gate: FAILED — could not check review threads." >&2; return 1; }
  unresolved=$(printf '%s' "$raw" | awk '{s+=$1} END{print s+0}')
  if [ "${unresolved:-0}" -gt 0 ]; then
    echo "[ship] local gate: FAILED — $unresolved unresolved review thread(s)." >&2
    return 1
  fi
  echo "[ship] local gate: review threads OK."
  return 0
}

# Orchestrate all local CI fallback gates. Called when CI infra appears structurally down.
# Returns 0 if ALL gates pass, 1 if any fail (conservative: block unless everything is clean).
run_local_ci_gate() {  # $1 (optional) = why this is running, for the banner (default: CI-down wording)
  local why="${1:-CI infrastructure appears down}"
  echo "[ship] === Running local CI fallback gates (${why}) ==="
  local gate_failed=0
  _local_test_runner "$ROOT"       || gate_failed=1
  _local_leftover_check "$PR"      || gate_failed=1
  _local_pr_checklist_check "$PR"  || gate_failed=1
  _local_review_threads_check "$PR" || gate_failed=1
  if [ "$gate_failed" = "0" ]; then
    echo "[ship] === Local CI fallback: ALL gates passed — safe to merge. ==="
    return 0
  fi
  echo "[ship] === Local CI fallback: FAILED — see above; not safe to merge. ===" >&2
  return 1
}

# Print ONLY the top-level `on:` block of a workflow read on stdin (from `^on:` to the next
# top-level key), with `#` comments stripped. So a `pull_request` mention OUTSIDE the trigger
# block — a comment, a job name, or an `if: github.event_name == 'pull_request'` expression
# under `jobs:` — is never mistaken for a PR trigger.
_workflow_on_block() {
  awk '
    /^on:([[:space:]]|$)/ { insec = 1; print; next }
    insec && /^[^[:space:]#]/ { insec = 0 }
    insec { print }
  ' | sed 's/#.*//'
}

# True (exit 0) iff the `on:` block read on stdin declares a `pull_request` / `pull_request_target`
# TRIGGER — matched STRUCTURALLY by INDENTATION, never as a free substring. A trigger is a DIRECT
# CHILD of `on:` (a key or a bare list item at the shallowest indent inside the block) or an inline
# value on the `on:` line itself. Every FILTER value (`branches:` / `paths:` / `tags:` lists) sits
# one level DEEPER than the trigger key, so a push-only workflow whose filter value merely contains
# — or is literally — `pull_request` (`branches: ['feature/pull_request-*']`, `paths:
# [docs/pull_request.md]`, or a bare `- pull_request` branch name under `push.branches`) is NOT
# misclassified. The inline `on:` case guards against a flow-mapping (`on: { push: { branches:
# [pull_request] } }`) with `[^{]`; a flow-style `on:` that legitimately maps `pull_request` is the
# one accepted residual — it fails toward REFUSE (safe direction), never toward an --admin merge.
# So is the quoted-key form `"on":` (unmatched by the block extractor): a safe refuse.
_on_block_declares_pr_trigger() {
  awk '
    BEGIN { childind = -1 }
    # inline `on:` scalar or [list] (NOT a flow-mapping `{...}`): triggers name-checked on line 1.
    NR == 1 && /^on:[[:space:]]*[^[:space:]{]/ {
      if ($0 ~ /^on:[^{]*pull_request(_target)?([^0-9A-Za-z_]|$)/) { found = 1 }
      next
    }
    /^[[:space:]]*$/ { next }
    {
      match($0, /^[[:space:]]*/); ind = RLENGTH
      if (ind == 0) next                    # the `on:` line itself (indent 0)
      if (childind < 0) childind = ind      # first indented line fixes the direct-child level
      if (ind == childind) {                # a DIRECT child of on: — a trigger key or list item
        if ($0 ~ /^[[:space:]]*pull_request(_target)?[[:space:]]*:/) found = 1
        if ($0 ~ /^[[:space:]]*-[[:space:]]*pull_request(_target)?[[:space:]]*$/) found = 1
      }
    }
    END { exit(found ? 0 : 1) }
  '
}

# True (exit 0) iff an EMPTY statusCheckRollup on this PR is a CI-OUTAGE (CI was EXPECTED to
# register a check but did not — billing suspended / Actions down / runner quota exhausted),
# rather than a repo that legitimately has no PR checks. The discriminator is a workflow whose
# top-level `on:` block declares a `pull_request` / `pull_request_target` trigger: a check
# SHOULD have appeared, so an empty rollup means it did not run (outage). A repo whose workflows
# only trigger on `push` / `schedule` / `workflow_dispatch` produces an empty rollup on a PR as
# CORRECT configured behavior — NOT an outage — so it stays a hard refuse.
#
# The signal is read from the REMOTE ref of the PR's OWN BASE branch (`origin/<baseRefName>`) —
# the exact state GitHub evaluates `pull_request` workflows from — NOT the local worktree or even
# local HEAD: an untracked, locally-modified, or committed-but-UNPUSHED `.github/workflows/*.yml`
# cannot flip a no-PR-CI repo into the local-gate path. The fetch uses an EXPLICIT refspec so a
# SUCCESSFUL fetch definitely advances the remote-tracking ref (a bare `fetch origin <branch>`
# only guarantees FETCH_HEAD, and could leave `origin/<base>` stale). Best-effort: if the fetch
# fails (offline) we fall back to the LAST-KNOWN `origin/<base>`, and if that ref does not exist
# at all we return false (refuse — the safe direction). There is deliberately NO env force-hook:
# tests exercise it with a real pushed `on: pull_request` workflow.
#
# RESIDUAL (documented): a `pull_request` trigger narrowed by `branches:` / `paths:` / `types:`
# filters may legitimately register no check for a PR the filter excludes; this heuristic still
# classifies that as an outage. The local gate (full test suite + leftover + checklist + threads)
# still runs before any such merge, so the merge is never unverified — only more permissive about
# WHEN the local gate substitutes for absent remote checks.
_empty_rollup_is_ci_outage() {
  local base ref f
  # GitHub evaluates a `pull_request` workflow from the PR's OWN BASE branch, which need not be
  # the repo default (a PR into a release/feature branch). Read the base ref and use it; fall
  # back to $DEFAULT_BRANCH only if it can't be read or looks unsafe (leading `-`, odd chars).
  base=$(gh pr view "$PR" --json baseRefName -q '.baseRefName' 2>/dev/null) || base=""
  case "$base" in ''|-*|*[!A-Za-z0-9._/-]*) base="$DEFAULT_BRANCH" ;; esac
  ref="origin/$base"
  git -C "$ROOT" fetch -q origin "$base:refs/remotes/origin/$base" 2>/dev/null || true
  git -C "$ROOT" rev-parse --verify --quiet "$ref" >/dev/null 2>&1 || return 1
  while IFS= read -r f; do
    case "$f" in *.yml|*.yaml) : ;; *) continue ;; esac
    git -C "$ROOT" show "$ref:$f" 2>/dev/null | _workflow_on_block | _on_block_declares_pr_trigger && return 0
  done < <(git -C "$ROOT" ls-tree -r --name-only "$ref" -- .github/workflows/ 2>/dev/null)
  return 1
}

# --- auto-resolve addressed bot-nit review threads (opt-in; #268) -----------------------
# Runs BEFORE every review-thread gate — the main unresolved-threads gate below AND the CI-down local
# fallback (`_local_review_threads_check`) — so the flag works on both paths. It closes only the
# threads that are SAFE to resolve without a human: unresolved AND isOutdated (the code the thread
# anchored to has changed) AND authored ENTIRELY by automated reviewers, with no P0/P1/critical/
# blocker/security marker in any comment (#285). A thread with ANY human comment, high-severity
# marker, unreadable body, or one still current is NEVER touched — it falls through and still blocks
# the merge. This lets a shipping agent close its own bot nits through
# `gh ship` instead of hand-running the resolveReviewThread mutation the block-raw-pr-merge hook used
# to false-block (#268). ship runs these gh calls as a child process, so the agent-hook never sees
# them; the hook fix is for an agent resolving threads directly. It runs during preflight, so if a
# LATER gate (CI-green, review-dwell, version-bump, quorum, clean-worktree) then refuses, some
# eligible bot nits may already be resolved on a PR that does not merge this run — accepted, because
# those nits are genuinely addressed and would need resolving next run anyway.
RESOLVE_THREAD_Q='query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated comments(first:100){totalCount nodes{author{login} body}}}}}}}'
# jq: emit the thread IDs eligible for auto-resolution, one per line. Fail-CLOSED on edge cases
# reviewer flagged: (a) if the thread has MORE than the 100 fetched comments (totalCount > fetched),
# a human reply could hide on an unfetched page, so a truncated thread is NOT eligible; (b) a
# null/ghost author login coalesces to "" (never a bot), so one deleted-account comment makes the
# thread ineligible instead of crashing jq; (c) a null/non-string body or one empty after removing
# whitespace/markdown is unreadable; and (d) ANY comment with a standalone high-severity marker
# excludes the whole thread. Hyphens count as marker boundaries, deliberately over-excluding
# compounds such as "security-first" rather than risking a missed "P1-blocking" finding (#285).
# All keep the "never auto-close a human/high-severity/uncertain thread" invariant.
RESOLVE_ELIGIBLE_JQ='def readable_body:
    if type == "string" then (gsub("[^[:alnum:]]"; "") | length) > 0 else false end;
  def has_high_severity_marker:
    test("(^|[^[:alnum:]])(p0|p1|critical|blocker|security)($|[^[:alnum:]])"; "i");
  [.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved == false and .isOutdated == true)
  | select((.comments.nodes | length) > 0)
  | select(.comments.totalCount != null and (.comments.nodes | length) >= .comments.totalCount)
  | select([.comments.nodes[].author.login // ""] | all(. as $l | ($l | endswith("[bot]")) or ($l == "chatgpt-codex-connector") or ($l == "codex-review-bot")))
  | select([.comments.nodes[].body | readable_body] | all)
  | select([.comments.nodes[].body | has_high_severity_marker] | any | not)
  | .id] | .[]'

_resolve_one_thread() {  # $1 = thread node id
  # `-f` (raw-field) for the opaque node id: `-F` auto-types and would read a leading `@` as a file.
  gh api graphql -f query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' \
    -f id="$1" >/dev/null 2>&1
}

resolve_addressed_bot_threads() {
  # `--repo` is honoured: ship exports GH_REPO (see the "Thread --repo through EVERY gh call" block
  # above), and gh resolves the `{owner}`/`{repo}` placeholders from it — same mechanism as the
  # unresolved-threads gate's THREAD_Q, so this targets the shipped PR's repo, not just the CWD's.
  local ids tid; local n=0
  ids=$(gh api graphql --paginate -F owner='{owner}' -F name='{repo}' -F pr="$PR" \
        -f query="$RESOLVE_THREAD_Q" --jq "$RESOLVE_ELIGIBLE_JQ" 2>/dev/null) || {
    echo "[ship] auto-resolve: could not query review threads (gh api failed) — skipping; the gate below still applies." >&2
    return 0; }
  [ -n "$ids" ] || { echo "[ship] auto-resolve: no addressed bot-nit threads to resolve."; return 0; }
  while IFS= read -r tid; do
    [ -n "$tid" ] || continue
    if [ "$DRY_RUN" = "1" ]; then
      echo "[ship] auto-resolve (dry-run): would resolve addressed bot thread $tid"; n=$((n+1)); continue
    fi
    if _resolve_one_thread "$tid"; then
      echo "[ship] auto-resolve: resolved addressed bot thread $tid"; n=$((n+1))
    else
      echo "[ship] auto-resolve: FAILED to resolve $tid — leaving it for the gate below." >&2
    fi
  done <<<"$ids"
  echo "[ship] auto-resolve: ${n} addressed bot-nit thread(s) $([ "$DRY_RUN" = "1" ] && echo 'would be resolved' || echo 'resolved')."
}

if [ "${RESOLVE_THREADS:-0}" = "1" ]; then
  resolve_addressed_bot_threads
fi

# --- green-CI gate: CI must EXIST + be green; pending CI is WATCHED to completion -------
# Design (CTO): "no CI" is itself a FAILED gate, not a free pass — refuse with guidance to
# set CI up. Existing-but-pending checks are WATCHED (polled to completion, up to
# SHIP_CI_WAIT) so you don't have to babysit; then the merge is gated on the final result of
# ALL checks. (gh has `gh run watch <run-id>` for a single run; we poll the PR-aggregate.)
#
# STALE-RUN DEDUP (why DEDUP_FILTER exists): GitHub's statusCheckRollup keeps EVERY historical
# run of a check name — it does NOT collapse to the latest. When a workflow is re-run (or reads
# mutable state like `pull_request.body`, e.g. the "PR Checklist" gate that fails while an
# acceptance box is unchecked, then passes once it is ticked), the rollup holds BOTH the old
# FAILURE and the new SUCCESS for the same check name. Counting every entry made this gate
# refuse a PR that GitHub itself reports mergeStateStatus=CLEAN (observed live on PR #653,
# 2026-07-12). Fix: collapse the rollup to the LATEST run per check (grouped by
# __typename + workflowName + name / context, keyed on completedAt/startedAt/createdAt) BEFORE
# evaluating pass/pending/fail — matching how GitHub computes mergeStateStatus. Fail-closed is
# preserved: if the LATEST run of a check is FAILURE/CANCELLED/pending, that latest verdict
# still gates. __typename is in the key so a StatusContext and a CheckRun that happen to share
# a name (a CheckRun can have a null workflowName) are NOT collapsed into one — collapsing them
# could hide a still-failing check behind a newer passing one of the other type.
if [ "$SKIP_CI" = "0" ]; then
  command -v jq >/dev/null 2>&1 || { echo "Refusing: jq is required for the CI gate (install jq — or --skip-ci only if CI is genuinely N/A)." >&2; exit 1; }
  SUCCESS_FILTER='((.conclusion=="SUCCESS" or .conclusion=="SKIPPED" or .conclusion=="NEUTRAL") or .state=="SUCCESS")'
  SETTLED_FILTER='(.status=="COMPLETED" or .state=="SUCCESS" or .state=="FAILURE" or .state=="ERROR")'
  # Collapse duplicate RUNS of the same check to the newest one, WITHOUT collapsing two
  # genuinely-distinct checks. Key = __typename + workflowName + detailsUrl-host + name /
  # context. __typename keeps a CheckRun and a StatusContext of the same name apart. The
  # detailsUrl/targetUrl host keeps two DIFFERENT providers that post an identically-named
  # CheckRun with a null workflowName (third-party apps) apart, so one provider's newer
  # SUCCESS cannot hide another provider's failing check of the same name; a re-run of ONE
  # check shares its host, so reruns still dedup. Recency (_dts): rank by the run's own
  # timestamp (completedAt, else startedAt, else createdAt) when it has one; a re-run always
  # post-dates the run it replaces, so its later timestamp wins. ONLY a run with NO timestamp
  # at all AND still queued/in-progress gets the "~" sentinel (sorts above any ISO-8601
  # timestamp) so a timestamp-less queued re-run is WATCHED to completion instead of being
  # dropped below the stale completed FAILURE it replaces. Fail-closed on a truly-red latest
  # run is preserved. Tolerates a null/missing rollup by folding to [].
  DEDUP_FILTER="def _dsettled: $SETTLED_FILTER;"'
    def _dhost: ((.detailsUrl // .targetUrl // "") | split("/") | (.[2] // ""));
    def _dkey: ((.__typename // "") + "\u001f" + (.workflowName // "") + "\u001f" + _dhost + "\u001f" + (.name // .context // ""));
    def _dts:  ((.completedAt // .startedAt // .createdAt) as $t | if $t != null then $t elif (_dsettled|not) then "~" else "" end);
    (. // []) | group_by(_dkey) | map(max_by(_dts))'
  CI_WAIT="${SHIP_CI_WAIT:-900}"; CI_POLL="${SHIP_CI_POLL:-20}"; CI_GRACE="${SHIP_CI_GRACE:-45}"
  START=$(date +%s); DEADLINE=$(( START + CI_WAIT )); GRACE_DEADLINE=$(( START + CI_GRACE ))
  while :; do
    # Read the rollup in TWO steps so a gh/API READ FAILURE is distinguishable from a
    # successfully-read EMPTY rollup. The old single pipeline (`gh … | jq … || echo '[]'`)
    # coerced BOTH the failure AND an empty result to `[]`, so a transient API/network failure
    # was silently treated as "no checks" — and could then fall into the empty-rollup outage
    # branch and merge even though the remote check state (possibly pending/red) was never
    # actually known. On a gh read FAILURE the remote state is UNKNOWN, not empty: retry within
    # the wait window and, if it never becomes readable, REFUSE — never merge on an unknown
    # rollup. A gh SUCCESS with a null/empty payload IS a genuine empty rollup and still folds
    # to `[]` (jq's `(. // [])`), so the outage/no-CI classification below is reached only from
    # a rollup that was successfully read.
    RAW_ROLLUP=''; GH_ROLLUP_RC=0
    RAW_ROLLUP=$(gh pr view "$PR" --json statusCheckRollup -q '.statusCheckRollup' 2>/dev/null) \
      || GH_ROLLUP_RC=$?
    if [ "$GH_ROLLUP_RC" -ne 0 ]; then
      NOW=$(date +%s)
      if [ "$NOW" -ge "$DEADLINE" ]; then
        echo "Refusing: could not read CI status for PR #$PR within ${CI_WAIT}s (gh/API error, exit $GH_ROLLUP_RC) — the remote check state is UNKNOWN; refusing rather than treating an unreadable rollup as 'no CI'. Retry when the API is reachable (or --skip-ci if CI is genuinely N/A)." >&2
        exit 1
      fi
      echo "[ship] could not read CI status for PR #$PR (gh/API error, exit $GH_ROLLUP_RC) — retrying (poll ${CI_POLL}s, $(( DEADLINE - NOW ))s left) ..."
      sleep "$CI_POLL"; continue
    fi
    ROLLUP=$(printf '%s' "$RAW_ROLLUP" | jq -c "$DEDUP_FILTER" 2>/dev/null || echo '[]')
    [ -n "$ROLLUP" ] || ROLLUP='[]'
    N=$(printf '%s' "$ROLLUP" | jq 'length' 2>/dev/null || echo 0)
    if [ "${N:-0}" = "0" ]; then
      # An empty rollup is ambiguous: either "no CI configured" OR "checks not registered yet"
      # (GitHub briefly returns [] on a freshly-opened PR before Actions enqueue). Give a grace
      # window for checks to appear; only after it lapses do we conclude there is genuinely no CI.
      NOW=$(date +%s)
      if [ "$NOW" -lt "$GRACE_DEADLINE" ]; then
        echo "[ship] no checks reported yet on PR #$PR — waiting ${CI_POLL}s for CI to register (grace $(( GRACE_DEADLINE - NOW ))s left) ..."
        sleep "$CI_POLL"; continue
      fi
      # CI registered NO checks after the grace window. If a PULL-REQUEST-triggered workflow is
      # configured on the pushed base branch, a check SHOULD have appeared but did not — a
      # billing/infra outage, exactly the "no money, CI keeps failing" case — so run the SAME
      # local fallback gate the checks-FAILED path uses and merge only if it is green, instead of
      # hard-refusing into a blind `--skip-ci` that verifies NOTHING locally. This is the SAME
      # posture (and the SAME plain, non-admin merge) as the checks-FAILED structural-outage path
      # below: a green local gate just `break`s and falls through to the normal `gh pr merge`, so
      # a branch-protection ruleset still gates the merge exactly as it would any other ship (if
      # required checks are genuinely absent GitHub blocks the merge and the operator can
      # `--skip-ci` deliberately — ship never silently --admin-bypasses protection here). A repo
      # whose workflows never trigger on PRs (push/schedule only) is NOT an outage and keeps the
      # hard refuse; a foreign --repo/GH_SHIP_REPO target also keeps the refuse — the local gate
      # runs against THIS checkout, not the target (#166).
      if [ "${_FOREIGN_REPO_INVOKE:-0}" != "1" ] && _empty_rollup_is_ci_outage; then
        echo "[ship] a PR-triggered workflow is configured but registered NO checks within ${CI_GRACE}s — CI infrastructure appears unavailable (billing/outage), not a real failure. Running local fallback gates instead of refusing." >&2
        if run_local_ci_gate; then
          echo "[ship] CI-down local gate PASSED (no checks registered) — CI outage, not a test failure; proceeding with the normal (non-admin) merge."
          break
        fi
        echo "Refusing: CI registered no checks AND local fallback gates also failed — not safe to merge." >&2
        exit 1
      fi
      { echo "Refusing: PR #$PR has NO CI checks (none registered within ${CI_GRACE}s) — set up CI before merging (an ungated merge is not allowed; 'no CI' is a failed gate, not a pass)."
        echo "  Provision CI: enable rig's ci block — \`rig apply\` writes secret-scan / codeql / dependency-review into .github/workflows — or add your own workflows."
        echo "  Override ONLY if CI is genuinely N/A for this repo: --skip-ci (deny-by-default — needs a one-time live Telegram approval via RIG_HATCH_REQUEST_SHIP_SKIP_CI)."; } >&2
      exit 1
    fi
    PENDING=$(printf '%s' "$ROLLUP" | jq "[.[] | select($SETTLED_FILTER | not)] | length" 2>/dev/null || echo 0)
    [ "${PENDING:-0}" = "0" ] && break
    NOW=$(date +%s); [ "$NOW" -ge "$DEADLINE" ] && { echo "Refusing: PR #$PR still has $PENDING pending check(s) after ${CI_WAIT}s. Let CI settle, then re-run." >&2; exit 1; }
    echo "[ship] $PENDING check(s) still running on PR #$PR — watching (poll ${CI_POLL}s, $(( DEADLINE - NOW ))s left before giving up) ..."
    sleep "$CI_POLL"
  done
  FAILED=$(printf '%s' "$ROLLUP" | jq "[.[] | select($SUCCESS_FILTER | not)] | length" 2>/dev/null || echo 1)
  if [ "${FAILED:-0}" != "0" ]; then
    TOTAL_CHECKS=$(printf '%s' "$ROLLUP" | jq 'length' 2>/dev/null || echo 0)
    if ci_appears_structurally_down "$TOTAL_CHECKS" "$FAILED"; then
      echo "[ship] CI infrastructure appears structurally unavailable (all/most checks failed, outage signal confirmed) — running local fallback gates instead of blocking on CI." >&2
      if run_local_ci_gate; then
        echo "[ship] CI-down local gate PASSED — CI infrastructure failure, not a test failure; proceeding with merge."
        # Do not exit 1 — fall through to the merge below.
      else
        echo "Refusing: CI unavailable AND local fallback gates also failed — not safe to merge." >&2
        exit 1
      fi
    else
      # known-flake gate (--known-flake NAME, repeatable) — narrower than the CI-down path
      # above: that one needs ~80% of ALL checks failing (a whole-infra outage); this covers
      # "one specific check is a confirmed pre-existing flake and everything else is green",
      # the far more common shape a human used to get pinged for. Only consulted when at
      # least one --known-flake was passed; EVERY currently-failed check must be BOTH
      # asserted via --known-flake AND independently CONFIRMED by _known_flake_confirmed
      # against the base branch's own recent CI history (see that function's doc comment for
      # what "confirmed" means and its scope) — one uncovered or unconfirmed failure still
      # hard-refuses the whole gate; there is no partial credit.
      #
      # CAVEAT (review finding, not yet closed): this gate confirms the CLAIM and runs a real
      # local test pass, then falls through to the SAME plain, non-admin `gh pr merge` every
      # other success path here uses. If the flaky check is also a REQUIRED status check under
      # branch protection, GitHub still refuses that merge — a required check FAILING (not
      # merely absent, unlike the empty-rollup outage case above) blocks the merge regardless
      # of what ship itself has already verified. In that shape a shipper still ends up at the
      # `--skip-ci` hatch after doing all the extra work this flag exists to avoid. This is not
      # silently unsafe (ship never bypasses branch protection here), just an unresolved UX
      # gap — tracked as a follow-up rather than fixed in this change.
      #
      # KNOWN_FLAKE_GATE_OK is POSITIVE confirmation, not "innocent until proven guilty": it
      # starts at 0 and is set to 1 ONLY after the loop has actually run over every failing
      # check and every single one confirmed. An earlier version of this gate started
      # optimistic (=1, flipped to 0 only inside the loop) — if the jq extraction of failing
      # check names ever came back EMPTY while $FAILED was still nonzero (a jq hiccup, or any
      # drift between how $FAILED was counted and this independent re-extraction from
      # $ROLLUP), the loop would run ZERO iterations, nothing would flip the flag off, and the
      # gate would silently PASS with no check ever actually confirmed — a fail-OPEN past red
      # CI (review finding). CHECKED_COUNT below makes the pass condition "every failing check
      # was individually confirmed", verified by counting, not by absence-of-a-negative-signal.
      KNOWN_FLAKE_GATE_OK=0
      if [ "${#KNOWN_FLAKES[@]}" -gt 0 ]; then
        # Resolve the PR's ACTUAL base branch ONCE (not per failing check — avoids redundant
        # gh calls) and FAIL CLOSED if it can't be read. This gate is evidence-gated by
        # design: an unreadable base isn't "assume main and proceed", it's "cannot verify the
        # claim" — same fail-closed posture the review-threads and dwell gates already use for
        # their own unreadable-data cases (review finding: an earlier version silently
        # defaulted to $DEFAULT_BRANCH here, which could verify a stacked PR's flake against
        # the WRONG branch's history on a transient gh failure, or on any base other than
        # $DEFAULT_BRANCH).
        KF_BASE=""
        if [ "${_FOREIGN_REPO_INVOKE:-0}" != "1" ]; then
          KF_BASE=$(gh pr view "$PR" --json baseRefName -q '.baseRefName' 2>/dev/null) || KF_BASE=""
          case "$KF_BASE" in ''|-*|*[!A-Za-z0-9._/-]*) KF_BASE="" ;; esac
        fi
        if [ -z "$KF_BASE" ]; then
          if [ "${_FOREIGN_REPO_INVOKE:-0}" = "1" ]; then
            # Same #166 hole the empty-rollup CI-down path already guards against: with a
            # foreign --repo/GH_SHIP_REPO target, run_local_ci_gate below would verify against
            # THIS (ambient) checkout's tests, not the target repo's — never let the
            # known-flake gate paper over that by "confirming" evidence for the right repo and
            # then testing the wrong one.
            echo "[ship] known-flake: refusing for a foreign --repo/GH_SHIP_REPO target — the local fallback gate below verifies THIS checkout, not the target repo (same #166 class the CI-down path guards against)." >&2
          else
            echo "[ship] known-flake: could not read this PR's base branch (gh pr view failed or returned an unsafe value) — refusing rather than guessing which branch's history to verify against." >&2
          fi
        else
          FAILED_NAMES_TSV=$(printf '%s' "$ROLLUP" | jq -r ".[] | select($SUCCESS_FILTER | not) | [(.name // .context), (.workflowName // \"\")] | @tsv" 2>/dev/null)
          KF_FAILED_COUNT=0
          KF_CONFIRMED_COUNT=0
          KF_ALREADY_LOST=0
          while IFS=$'\t' read -r fname fwf; do
            # Count EVERY row from this loop, including one whose name/context both render
            # empty — it still counted toward the earlier $FAILED total, so treating it as
            # "nothing to check" here (the previous `[ -n "$fname" ] || continue` skipped the
            # counter too) would let a genuinely-failing-but-unnamed row escape the "every
            # failure confirmed" requirement entirely (review finding). An empty $fname can
            # never match a --known-flake assertion anyway, so it falls straight to "not
            # asserted" below and correctly blocks.
            KF_FAILED_COUNT=$((KF_FAILED_COUNT + 1))
            if [ "$KF_ALREADY_LOST" = "1" ]; then
              # The gate has already failed on an earlier row this pass — the outcome is
              # decided (refuse). Skip the remaining gh-API evidence lookups entirely: they
              # cannot change the verdict, only cost latency (review finding, perf).
              echo "[ship] known-flake: '${fname:-<unnamed check>}' not evaluated — an earlier failing check already sank this gate." >&2
              continue
            fi
            asserted=0
            for kf in "${KNOWN_FLAKES[@]}"; do
              [ -n "$fname" ] && [ "$kf" = "$fname" ] && { asserted=1; break; }
            done
            if [ "$asserted" = "1" ] && _known_flake_confirmed "$fname" "$fwf" "$KF_BASE"; then
              KF_CONFIRMED_COUNT=$((KF_CONFIRMED_COUNT + 1))
              echo "[ship] known-flake: '$fname' asserted via --known-flake and CONFIRMED against ${KF_BASE}'s recent CI history." >&2
            elif [ "$asserted" = "1" ]; then
              echo "[ship] known-flake: '$fname' was asserted via --known-flake but could NOT be confirmed as a pre-existing failure on ${KF_BASE} — this alone blocks the merge." >&2
              KF_ALREADY_LOST=1
            else
              echo "[ship] known-flake: '${fname:-<unnamed check>}' is FAILED and was not asserted via --known-flake — this alone blocks the merge." >&2
              KF_ALREADY_LOST=1
            fi
          done <<< "$FAILED_NAMES_TSV"
          # Pass ONLY when every failing row (by count, matching $FAILED — not merely
          # "nonzero", closing the partial-undercount gap review flagged) was individually
          # confirmed, with no earlier-loss short-circuit having fired.
          [ "$KF_ALREADY_LOST" = "0" ] && [ "$KF_FAILED_COUNT" -gt 0 ] \
            && [ "$KF_FAILED_COUNT" -eq "$FAILED" ] && [ "$KF_CONFIRMED_COUNT" -eq "$KF_FAILED_COUNT" ] \
            && KNOWN_FLAKE_GATE_OK=1
        fi
      fi
      if [ "$KNOWN_FLAKE_GATE_OK" = "1" ]; then
        echo "[ship] known-flake gate PASSED — every failing check is a confirmed pre-existing flake on ${KF_BASE}, not something this PR introduced. Running local fallback gates instead of blocking on CI." >&2
        # Audit AFTER the terminal outcome, not before: a "confirmed" line means this ship
        # actually proceeded to merge, not merely that the base-branch evidence checked out —
        # SHIP_AUDIT_FILE is "one line per gated ship", so a reader must never see `confirmed`
        # for a run that in fact refused later (review finding, round 2: the FIRST fix only
        # deferred past run_local_ci_gate — every gate BELOW this one in the script
        # (unresolved-threads, review-dwell, branch sanity, version-bump, screenshot,
        # review-quorum) and the actual `gh pr merge` call can still refuse afterward, so the
        # audit line has to wait for the REAL terminal event, not just this gate's own local
        # check). KF_AUDIT_PENDING carries the asserted names to the merge section below,
        # which logs `confirmed` only once `gh pr merge` has actually succeeded — see "merge".
        if run_local_ci_gate "a confirmed pre-existing flake on ${KF_BASE}, not a CI outage"; then
          KF_AUDIT_PENDING="${KNOWN_FLAKES[*]}"
          echo "[ship] known-flake local gate PASSED — proceeding with merge."
          # Do not exit 1 — fall through to the merge below.
        else
          _known_flake_audit_log confirmed-local-gate-failed "${KNOWN_FLAKES[*]}"
          echo "Refusing: known-flake confirmed, but local fallback gates also failed — not safe to merge." >&2
          exit 1
        fi
      else
        [ "${#KNOWN_FLAKES[@]}" -gt 0 ] && _known_flake_audit_log refused "${KNOWN_FLAKES[*]}"
        echo "Refusing: PR #$PR has $FAILED check(s) not passing:" >&2
        printf '%s' "$ROLLUP" | jq -r ".[] | select($SUCCESS_FILTER | not) | \"  x \(.name // .context) -> \(.conclusion // .state)\"" >&2 2>/dev/null || true
        echo "  Fix CI, then re-run. If CI is genuinely billing-blocked/down, re-run WITHOUT --skip-ci — the normal path auto-detects a real outage and merges after its own local checks. (--skip-ci is now a deny-by-default hatch-gated admin bypass, NOT the billing path.)" >&2
        echo "  If a failing check looks UNRELATED to this diff, check whether it ALSO fails on this PR's BASE branch (e.g. \`gh run list --branch <base>\`, or re-run the same test locally against the base branch's HEAD — note this may not be ${DEFAULT_BRANCH} for a stacked PR). A CONFIRMED pre-existing flake does NOT need human escalation: re-run with \`--known-flake <check-name>\` (repeatable, one per failing check) — ship independently VERIFIES the claim against the PR's actual base branch's recent CI history before doing anything with it (see --known-flake in this script's own header comment); it refuses the same as now if the claim doesn't hold up. Escalate to a human only for a genuine billing-block or a failure that is actually related to this diff." >&2
        exit 1
      fi
    fi
  fi
fi

# --- unresolved review threads (see ci/review-threads/) --------------------------------
# Fail-CLOSED: if the API call fails we must NOT merge (a silent 0 would let an unresolved
# PR through). So check the gh exit status, not just the parsed count.
THREAD_Q='query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{isResolved}}}}}'
if THREAD_RAW=$(gh api graphql --paginate -F owner='{owner}' -F name='{repo}' -F pr="$PR" -f query="$THREAD_Q" \
     --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length' 2>/dev/null); then
  UNRESOLVED=$(printf '%s' "$THREAD_RAW" | awk '{s+=$1} END{print s+0}')
else
  echo "Refusing: could not query review threads for PR #$PR (gh api failed) — refusing to merge rather than fail open. Fix gh access and retry." >&2
  exit 1
fi
[ "${UNRESOLVED:-0}" = "0" ] || { echo "Refusing: PR #$PR has $UNRESOLVED unresolved review thread(s) — resolve them (or re-run with --resolve-addressed-threads to auto-close outdated bot-nit threads), then re-run." >&2; exit 1; }

# --- review-dwell gate: give async review time to FORM its comments before merging ---------
# WHY this exists (the gap it closes): the unresolved-threads gate above only fails when threads
# ALREADY EXIST. "0 unresolved threads" is ALSO true when no review has POSTED yet — so a PR
# opened and shipped within seconds passes that gate vacuously, before any multi-model / CI-AI /
# human review could form its questions. This gate enforces a minimum DWELL window since the PR's
# last code push, so a review has time to land; whatever it then posts becomes a thread the gate
# above forces resolved. Together they mean: comments get TIME to form AND must be resolved.
#
# Runs INDEPENDENTLY of --skip-ci: a premature merge is premature regardless of CI billing.
#
# The window starts at max(createdAt, pushedDate, committedDate, forcePushedAt):
#   • createdAt — the floor; a reviewer can't review before the PR exists, so even a PR built
#     from an old commit waits from PR-open.
#   • pushedDate — GitHub-CONTROLLED head-update time (when GitHub received the push). Reliable
#     for normal pushes but can be null (deprecated on the Commit object) and stale on force-pushes
#     to an already-on-GitHub commit.
#   • committedDate — the embedded commit date. Reliable fallback for pushedDate==null on the common
#     commit→push / rebase→push path.
#   • forcePushedAt — HeadRefForcePushedEvent.createdAt (last event); the authoritative signal
#     that the PR head was force-pushed to an old commit where pushedDate would be stale.
# Override with --no-review-dwell-ok <reason> (logged) or SHIP_REVIEW_DWELL=0 to disable.
# Fail-CLOSED: if the timestamps can't be read or parsed, refuse rather than merge un-waited.

# Convert an ISO-8601 UTC timestamp (GitHub's `2026-06-28T12:34:56Z`) to a Unix epoch, portably
# across GNU date (`-d`) and BSD/macOS date (`-j -f`). Drops fractional seconds and tolerates a
# missing trailing `Z`. Prints the epoch on success, nothing on failure (caller fails closed).
iso_to_epoch() {  # $1 = ISO-8601 timestamp -> prints epoch seconds, or nothing
  [ -n "${1:-}" ] || return 0
  local ts="${1%%.*}"                              # strip any fractional seconds
  case "$ts" in *Z) : ;; *) ts="${ts}Z" ;; esac    # normalize to a trailing Z
  date -u -d "$ts" +%s 2>/dev/null && return 0                         # GNU date
  date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null && return 0 # BSD/macOS date
  return 0
}

DWELL="${SHIP_REVIEW_DWELL:-600}"
case "$DWELL" in *[!0-9]*|'') echo "Refusing: SHIP_REVIEW_DWELL='$DWELL' must be a non-negative integer (seconds)." >&2; exit 1 ;; esac
if [ -n "$NO_DWELL_OK" ]; then
  echo "[ship] review-dwell gate OVERRIDDEN — reason: $NO_DWELL_OK"
elif [ "$DWELL" = "0" ]; then
  echo "[ship] review-dwell gate disabled (SHIP_REVIEW_DWELL=0)."
else
  # Fetch four timestamp signals from GitHub:
  #   createdAt        — PR open time (floor: dwell must pass from at least this point)
  #   pushedDate       — GitHub-controlled head-update time (when GitHub received the push);
  #                      reliable for normal pushes but can be null and is deprecated on Commit.
  #   committedDate    — embedded in the commit; reliable fallback for pushedDate==null.
  #   forcePushedAt    — HeadRefForcePushedEvent.createdAt (last entry); the authoritative signal
  #                      for "PR was force-pushed to an already-on-GitHub commit" that makes
  #                      pushedDate stale. Empty when there has been no force-push.
  # Window start = max(createdAt, pushedDate, committedDate, forcePushedAt).
  DWELL_Q='query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){pullRequest(number:$pr){createdAt commits(last:1){nodes{commit{committedDate pushedDate}}} timelineItems(last:1,itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT]){nodes{__typename ... on HeadRefForcePushedEvent{createdAt}}}}}}'
  if DWELL_RAW=$(gh api graphql -F owner='{owner}' -F name='{repo}' -F pr="$PR" -f query="$DWELL_Q" \
       --jq '.data.repository.pullRequest | [.createdAt, (.commits.nodes[0].commit.pushedDate // ""), (.commits.nodes[0].commit.committedDate // ""), (.timelineItems.nodes[0].createdAt // "")] | @tsv' 2>/dev/null); then
    PR_CREATED=$(printf '%s' "$DWELL_RAW" | awk -F'\t' 'NR==1{print $1}')
    PR_PUSHED=$(printf '%s' "$DWELL_RAW" | awk -F'\t' 'NR==1{print $2}')
    PR_COMMITTED=$(printf '%s' "$DWELL_RAW" | awk -F'\t' 'NR==1{print $3}')
    PR_FORCE_PUSHED=$(printf '%s' "$DWELL_RAW" | awk -F'\t' 'NR==1{print $4}')
  else
    echo "Refusing: could not read PR #$PR timestamps for the review-dwell gate (gh api failed) — refusing rather than merge un-waited. Fix gh access, or override with --no-review-dwell-ok <reason>." >&2
    exit 1
  fi
  # Window start = the LATEST of the four signals (each may be empty if unparseable/null).
  START=""
  for ts in "$PR_CREATED" "$PR_PUSHED" "$PR_COMMITTED" "$PR_FORCE_PUSHED"; do
    e=$(iso_to_epoch "$ts")
    if [ -n "$e" ] && { [ -z "$START" ] || [ "$e" -gt "$START" ]; }; then START="$e"; fi
  done
  if [ -z "$START" ]; then
    echo "Refusing: could not parse PR #$PR review-window timestamps ('$PR_CREATED' / '$PR_PUSHED' / '$PR_COMMITTED' / '$PR_FORCE_PUSHED') for the review-dwell gate — refusing rather than merge un-waited. Override with --no-review-dwell-ok <reason> if intended." >&2
    exit 1
  fi
  NOW=$(date +%s); ELAPSED=$(( NOW - START ))
  if [ "$ELAPSED" -lt "$DWELL" ]; then
    WAIT=$(( DWELL - ELAPSED ))
    { echo "Refusing: PR #$PR is only ${ELAPSED}s old since its last push — the review-dwell window is ${DWELL}s, so async review has not had time to form its questions yet."
      echo "  Wait ${WAIT}s and re-run (meanwhile, request/run the review so comments can land). Tune with SHIP_REVIEW_DWELL=<seconds> (0 disables)."
      echo "  Genuine fast-track (trivial/urgent): override with --no-review-dwell-ok <reason> (logged)."; } >&2
    exit 1
  fi
  echo "[ship] review-dwell gate OK — PR #$PR is ${ELAPSED}s past its last push (window ${DWELL}s)."
fi

# --- local branch sanity (no unpushed/diverged commits; clean worktree) ----------------
# A branch can be checked out in MORE THAN ONE worktree (git permits it; a stray/leftover
# tree often lingers). Collect ALL of them: feeding a single $WT that had concatenated two
# paths to `git -C` / `git worktree remove` was the exit-128 bug this script had. Parse the
# stable `--porcelain` line form (`worktree <path>` then `branch <ref>`); the `-z` variant
# isn't available on older git (e.g. Apple git 2.39), so don't rely on it.
# (bash 3.2 compatible — `#!/usr/bin/env bash` is 3.2 on stock macOS; guard every empty-array
# expansion with the `${arr[@]+...}` idiom so `set -u` doesn't trip on an empty WTS.)
WTS=()
while IFS= read -r wpath; do
  [ -n "$wpath" ] && WTS+=("$wpath")
done < <(
  git worktree list --porcelain 2>/dev/null \
    | awk -v b="refs/heads/$BRANCH" '/^worktree /{w=substr($0,10)} $0=="branch "b{print w}'
)
for wt in ${WTS[@]+"${WTS[@]}"}; do
  # Guard each linked PR worktree — this catches a REAL corruption the MAIN-checkout guard
  # above misses: WORKTREE-SCOPED core.bare=true (extensions.worktreeConfig + `git -C "$wt"
  # config --worktree core.bare true`). Plain core.bare on the MAIN config leaves linked
  # worktrees healthy (each has its own gitdir + work tree, so they report not-bare), but the
  # worktree-scoped form makes THIS worktree itself report rev-parse=bare with `status` failing.
  # That matters because the dirty-check right below runs `git -C "$wt" status --short
  # 2>/dev/null`: under the corruption it exits 128 and the fatal is swallowed, leaving empty
  # output — so a worktree with unshipped changes would read as "clean" and could later be
  # removed. Aborting here, before that fooled check, prevents losing unshipped work.
  abort_if_core_bare "$wt" "PR worktree"
  if [ -n "$(git -C "$wt" status --short 2>/dev/null)" ]; then
    echo "Refusing: worktree $wt has uncommitted changes. Commit or discard first." >&2
    git -C "$wt" status --short >&2; exit 1
  fi
done
git fetch -q origin "$BRANCH" 2>/dev/null || true
if git show-ref --verify --quiet "refs/heads/$BRANCH" && git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
  AHEAD=$(git rev-list --count "origin/$BRANCH..$BRANCH" 2>/dev/null || echo 0)
  [ "$AHEAD" = "0" ] || { echo "Refusing: local $BRANCH has $AHEAD unpushed commit(s). Push first." >&2; exit 1; }
fi

# --- version-bump gate: a release of shippable source MUST bump the declared version ----
# Rationale (skill: bump-version-on-release): a tool's version is a freshness signal — it
# tells a user which build they run and whether a fix landed. That only works if the
# declared version is bumped on EVERY release. The canonical failure is a version that never
# moves across many ships (e.g. a permanently-stale `0.1.0`), turning `--version` into noise.
# So: if this PR changes shippable SOURCE (not docs / not tests / not CI) and the repo's
# declared version field is UNCHANGED vs the PR base, refuse — this ship is a release and the
# version must move. Override for a genuine no-release ship via --no-version-bump-ok <reason>
# or SHIP_SKIP_VERSION_BUMP=1.
#
# Repo-agnostic: detection works off the PR's own changed files + diff (no tracker / layout /
# org hard-coded). A changed path is "shippable source" UNLESS it matches the docs/test/CI
# exclusion set below; the version file is auto-located (pyproject.toml `version =`, then
# package.json `"version"`) or pinned via SHIP_VERSION_FILES.

# A path is NON-shippable (does NOT, on its own, make a ship a release) iff it is docs, a
# test, or CI/meta. Everything else is treated as shippable source. Kept deliberately broad
# on the exclusion side so a docs-only / test-only / CI-only PR is never forced to bump.
is_nonshippable_path() {  # $1 = path -> rc 0 if non-shippable (docs/test/CI), 1 otherwise
  local base; base=${1##*/}
  # A dependency MANIFEST named with a docs-ish extension (`requirements.txt`,
  # `constraints*.txt`) is a SHIPPABLE change (a dep bump is a release), so it must NOT be
  # swept into the `.txt` docs bucket below — check it first and return "shippable".
  case "$base" in
    requirements.txt|requirements-*.txt|constraints.txt|constraints-*.txt) return 1 ;;
  esac
  case "$1" in
    # docs & prose
    *.md|*.mdx|*.markdown|*.rst|*.txt|*.adoc) return 0 ;;
    docs/*|*/docs/*|LICENSE|LICENSE.*|*/LICENSE|NOTICE|NOTICE.*|.gitignore|.gitattributes) return 0 ;;
    # CI / repo meta — provider config dirs AND the common single-file CI configs, so a
    # pure-CI PR on any major provider is exempt (not just GitHub Actions under .github/).
    .github/*|*/.github/*|.gitlab/*|.circleci/*|.buildkite/*) return 0 ;;
  esac
  case "$base" in
    .gitlab-ci.yml|.gitlab-ci.yaml|.travis.yml|.travis.yaml|azure-pipelines.yml|azure-pipelines.yaml) return 0 ;;
    appveyor.yml|appveyor.yaml|.appveyor.yml|Jenkinsfile|.drone.yml|bitbucket-pipelines.yml|cloudbuild.yaml|cloudbuild.yml) return 0 ;;
  esac
  case "$1" in
    # ship's own CI/merge-gate metadata, not product code — a repo adding/editing the
    # REPO-ROOT .ship-config to fix its local-fallback test detection is not a release.
    # Exact path match (not basename): only the root file is ever read by
    # _ship_config_load, so a nested same-named file (e.g. src/.ship-config, which ship
    # never consumes) must NOT be swept into this exemption.
    .ship-config) return 0 ;;
  esac
  case "$1" in
    # tests (common conventions across languages)
    test/*|*/test/*|tests/*|*/tests/*|__tests__/*|*/__tests__/*) return 0 ;;
    test_*.py|*/test_*.py|*_test.py|*/*_test.py) return 0 ;;
    *.test.ts|*.test.tsx|*.test.js|*.test.jsx|*.spec.ts|*.spec.tsx|*.spec.js|*.spec.jsx) return 0 ;;
    *_test.go|*/*_test.go) return 0 ;;
  esac
  return 1
}

# True iff at least one changed path is shippable source. Reads newline-separated paths on stdin.
any_shippable_source() {  # stdin: changed paths -> rc 0 if any shippable
  local p found=1
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if ! is_nonshippable_path "$p"; then found=0; fi
  done
  return $found
}

# Locate the repo's version file: the explicit override, else pyproject.toml (with a
# `[project] version =`), else package.json (with a `"version"`). Prints the path, or nothing.
locate_version_file() {
  local f
  if [ -n "${SHIP_VERSION_FILES:-}" ]; then
    for f in $SHIP_VERSION_FILES; do [ -f "$ROOT/$f" ] && { printf '%s' "$f"; return 0; }; done
    return 1
  fi
  if [ -f "$ROOT/pyproject.toml" ] && grep -Eq '^[[:space:]]*version[[:space:]]*=' "$ROOT/pyproject.toml"; then
    printf 'pyproject.toml'; return 0
  fi
  if [ -f "$ROOT/package.json" ] && grep -Eq '"version"[[:space:]]*:' "$ROOT/package.json"; then
    printf 'package.json'; return 0
  fi
  return 1
}

# True iff the PR's diff CHANGES THE VALUE of the version line of $1 — not merely that an
# added version line exists. The distinction matters: requiring only a `+version` line would
# pass a cosmetic edit (whitespace/quote churn) or even a DOWNGRADE as "bumped", which defeats
# the "the version must MOVE" intent. So we extract the removed (`-`) and added (`+`) version
# VALUES inside that file's diff section and require a NEW value that differs from the OLD.
# (A version DECLARED for the first time — an added `+` with no matching `-` — also counts: a
# brand-new version field is a move from "no version".)
version_line_bumped() {  # $1 = version file path; uses $PR_DIFF (full patch text)
  local file="$1" oldv newv
  # Restrict to that file's section of the unified diff so an unrelated `version` elsewhere
  # can't falsely satisfy the gate. A file's section runs from its `diff --git a/.. b/<file>`
  # header up to the next `diff --git`.
  local section
  section=$(printf '%s\n' "$PR_DIFF" \
    | awk -v f="$file" '
        /^diff --git / { insec = ($0 == "diff --git a/" f " b/" f) ? 1 : 0; next }
        insec { print }
      ')
  # Extract the quoted value from a version-declaration line (both pyproject `version = "X"`
  # and package.json `"version": "X"` quote the value). `head -1` guards against multiple
  # matches (e.g. a workspace member) — take the first, which is the package's own.
  newv=$(printf '%s\n' "$section" | grep -E '^\+[[:space:]]*(version[[:space:]]*=|"version"[[:space:]]*:)' \
           | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1)
  oldv=$(printf '%s\n' "$section" | grep -E '^-[[:space:]]*(version[[:space:]]*=|"version"[[:space:]]*:)' \
           | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1)
  [ -n "$newv" ] || return 1            # no new version value declared -> not bumped
  [ "$newv" != "$oldv" ] || return 1    # value unchanged (cosmetic-only edit) -> not bumped
  return 0
}

# The PR's NEW version value (the `+` line of $1's diff section), for the base-tip check
# below. Same extraction as version_line_bumped; empty when the PR declares none.
pr_new_version_value() {  # $1 = version file path; uses $PR_DIFF
  printf '%s\n' "$PR_DIFF" \
    | awk -v f="$1" '
        /^diff --git / { insec = ($0 == "diff --git a/" f " b/" f) ? 1 : 0; next }
        insec { print }
      ' \
    | grep -E '^\+[[:space:]]*(version[[:space:]]*=|"version"[[:space:]]*:)' \
    | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1
}

# The version value at the TIP of the PR's base branch (`origin/<baseRefName>`), or empty
# when it cannot be read (no such ref, no such file, no version line). Why the base TIP and
# not the PR diff: `gh pr diff` is relative to the PR's MERGE-BASE, so a PR branched before
# a sibling release shows `-0.34.1 / +0.35.0` even after main already moved to 0.35.0 — the
# diff says "bumped" while the squash-merge onto main changes no version at all
# (agent-tools#592: rig-cli #369 and #370 both bumped 0.34.1->0.35.0; #370 merged 44
# minutes later, the gate said OK, and a feature shipped under a number main already had).
# The fetch uses the explicit refspec so a SUCCESSFUL fetch definitely advances
# `origin/<base>`; offline it falls back to the last-known ref (best-effort, like
# `_empty_rollup_is_ci_outage`); a missing ref/file yields empty -> the check passes (the
# PR-diff test above stays the floor).
base_tip_version_value() {  # $1 = version file path -> prints the value or nothing
  local base ref
  base=$(gh pr view "$PR" --json baseRefName -q '.baseRefName' 2>/dev/null) || base=""
  case "$base" in ''|-*|*[!A-Za-z0-9._/-]*) base="$DEFAULT_BRANCH" ;; esac
  ref="origin/$base"
  git -C "$ROOT" fetch -q origin "$base:refs/remotes/origin/$base" 2>/dev/null || true
  git -C "$ROOT" rev-parse --verify --quiet "$ref" >/dev/null 2>&1 || return 0
  git -C "$ROOT" show "$ref:$1" 2>/dev/null \
    | grep -E '^[[:space:]]*(version[[:space:]]*=|"version"[[:space:]]*:)' \
    | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1
}

if [ "${SHIP_SKIP_VERSION_BUMP:-0}" = "1" ] && [ -z "$NO_VBUMP_OK" ]; then
  NO_VBUMP_OK="SHIP_SKIP_VERSION_BUMP=1 (env)"
fi
if [ -n "$NO_VBUMP_OK" ]; then
  echo "[ship] version-bump gate OVERRIDDEN — reason: $NO_VBUMP_OK"
else
  if PR_FILES_VB=$(gh pr diff "$PR" --name-only 2>/dev/null); then
    if printf '%s\n' "$PR_FILES_VB" | any_shippable_source; then
      # Shippable source changed -> this ship is a release. The version field must have moved.
      if VFILE=$(locate_version_file); then
        if PR_DIFF=$(gh pr diff "$PR" 2>/dev/null); then
          if ! version_line_bumped "$VFILE"; then
            { echo "Refusing: PR #$PR changes shippable source but the version in $VFILE is UNCHANGED — this ship is a release, so bump the version (skill: bump-version-on-release)."
              echo "  Semver: patch for a fix, minor for a feature, major for a breaking change. Edit $VFILE's version field, push, re-run ship."
              echo "  If this is genuinely NOT a release (docs-only / pure test or CI / a revert), override: --no-version-bump-ok <reason> (or SHIP_SKIP_VERSION_BUMP=1)."; } >&2
            exit 1
          fi
          # Bumped relative to the merge-base is not enough: the base branch may ALREADY
          # carry that value (a sibling PR released it first) — then merging changes no
          # version and this release ships under a number that is already taken.
          VB_NEW=$(pr_new_version_value "$VFILE"); VB_BASE=$(base_tip_version_value "$VFILE")
          if [ -n "$VB_NEW" ] && [ -n "$VB_BASE" ] && [ "$VB_NEW" = "$VB_BASE" ]; then
            { echo "Refusing: PR #$PR bumps $VFILE to $VB_NEW, but the base branch tip ALREADY has version $VB_BASE — another release took that number, so merging would change no version (the PR diff is relative to the merge-base, not to the current base)."
              echo "  Bump above $VB_BASE (rebase or edit $VFILE's version field), push, re-run ship."
              echo "  If this is genuinely NOT a release, override: --no-version-bump-ok <reason> (or SHIP_SKIP_VERSION_BUMP=1)."; } >&2
            exit 1
          fi
          echo "[ship] version-bump gate OK — $VFILE version is bumped in PR #$PR."
        else
          echo "Refusing: could not read the diff for PR #$PR — cannot evaluate the version-bump gate (override with --no-version-bump-ok <reason> if intended)." >&2
          exit 1
        fi
      else
        echo "[ship] version-bump gate: no version file (pyproject.toml/package.json) found at repo root — skipping (set SHIP_VERSION_FILES for a non-standard layout)."
      fi
    else
      echo "[ship] version-bump gate: PR #$PR changes no shippable source (docs/tests/CI only) — no bump required."
    fi
  else
    echo "Refusing: could not list changed files for PR #$PR — cannot evaluate the version-bump gate (override with --no-version-bump-ok <reason> if intended)." >&2
    exit 1
  fi
fi

# --- screenshot gate (optional uploader + UI-touching check) ---------------------------
# Compose the SHIP_IMAGE_UPLOAD_CMD template with `replacement` spliced in wherever `{FILE}`
# appears — bare (`{FILE}`) or wrapped in its OWN dedicated matching quotes (`"{FILE}"` /
# `'{FILE}'`, replaced WHOLE, quotes included: the caller's `replacement` already supplies its
# own quoting via `%q`, so nesting it inside the operator's quotes would corrupt it — see
# upload_png). A SINGLE left-to-right pass over the ORIGINAL `haystack` finds, at each point,
# whichever of the three forms starts EARLIEST (ties broken toward the quoted forms, since a
# bare `{FILE}` match is always a substring of a quoted one at the same position — `%%pat*`'s
# "not found" case is used AS its own sentinel: an unmatched candidate's prefix equals the
# full remaining haystack length, which is never shorter than a real match's prefix, so it
# can never incorrectly win). Never rescans `out`/`replacement` for a later pass to find
# another `{FILE}` in already-emitted text — REQUIRED, not cosmetic: `replacement` is a %q
# escaped path, and `%q` does not escape `{`/`}`, so a screenshot path containing the literal
# substring `{FILE}` would otherwise get corrupted by a later pass re-matching text this same
# call already emitted (regression pinned in tests). `%%"$needle"*` / `#*"$needle"` are
# pattern-REMOVAL operators (needle is quoted = literal, no glob), not `${var//pat/rep}`
# replacement, so `replacement`'s own content (which may contain `&`/`\`) is never reinterpreted
# by patsub_replacement (default-on since bash 5.2 for the two-argument replace form).
_upload_png_compose_cmd() {  # $1=template $2=replacement -> stdout composed command
  local haystack="$1" replacement="$2" out=""
  local dq='"{FILE}"' sq="'{FILE}'" bare='{FILE}'
  local prefix_dq prefix_sq prefix_bare best_prefix needle
  while :; do
    case "$haystack" in
      *"$bare"*) : ;;
      *) out+="$haystack"; break ;;
    esac
    prefix_dq="$haystack"; case "$haystack" in *"$dq"*) prefix_dq="${haystack%%"$dq"*}" ;; esac
    prefix_sq="$haystack"; case "$haystack" in *"$sq"*) prefix_sq="${haystack%%"$sq"*}" ;; esac
    prefix_bare="${haystack%%"$bare"*}"
    best_prefix="$prefix_dq"; needle="$dq"
    if [ "${#prefix_sq}" -lt "${#best_prefix}" ]; then best_prefix="$prefix_sq"; needle="$sq"; fi
    if [ "${#prefix_bare}" -lt "${#best_prefix}" ]; then best_prefix="$prefix_bare"; needle="$bare"; fi
    out+="$best_prefix$replacement"
    haystack="${haystack#*"$needle"}"
  done
  printf '%s' "$out"
}
# upload_png (HYP-1260): the untrusted screenshot PATH must never be interpolated into
# SHIP_IMAGE_UPLOAD_CMD as raw text before `eval` — a `"`/`;`/`$(...)`/backtick in the path
# would otherwise break out of the template's quoting and inject arbitrary shell syntax (the
# pre-fix code did exactly that: `eval "$SHIP_IMAGE_UPLOAD_CMD \"$png\""`).
#
# Fix: shell-quote the path via `printf %q` into a single, syntactically-inert token, splice
# THAT into the (trusted) template via `_upload_png_compose_cmd`, then eval the composed
# string exactly as before. A %q-quoted token always re-parses back to precisely the original
# string as one shell word, so the untrusted content can never break out of its own quoting —
# while the template keeps its full shell semantics (pipes, redirects, `&&`, an env-var prefix
# all still work, unlike an argv-array split of the template, which loses them).
#
# NOT supported: `{FILE}` embedded inside a larger quoted word alongside other text (e.g.
# `"file=@{FILE}"` — the quote isn't adjacent to the placeholder, so it doesn't match a
# recognized form and the bare substitution fires instead, landing the %q token inside the
# operator's quotes) — give `{FILE}` its OWN dedicated quotes instead, or use `--data=@{FILE}`
# unquoted (%q's own escaping handles a space-containing path there too).
upload_png() {
  local png="$1"
  [ -n "${SHIP_IMAGE_UPLOAD_CMD:-}" ] || return 1
  if [ "$DRY_RUN" = "1" ]; then printf 'https://example.invalid/dry-run/%s' "$(basename "$png")"; return 0; fi
  local out quoted_png cmd tmpl="$SHIP_IMAGE_UPLOAD_CMD"
  printf -v quoted_png '%q' "$png"
  # Strip trailing newline(s) from the template before doing anything else: a config value
  # that picked up a stray trailing newline (e.g. from an unguarded `$(cat file)`, or a
  # heredoc) is never semantically meaningful as "extra command syntax" here, but if left in
  # place it becomes an EMBEDDED newline once the path is appended below — and `eval` treats a
  # raw newline as a statement separator, silently splitting the composed command into two
  # garbled statements (verified via test: the uploader then runs with zero real args).
  while :; do
    case "$tmpl" in
      *$'\n') tmpl="${tmpl%$'\n'}" ;;
      *) break ;;
    esac
  done
  # Detect "no {FILE} placeholder at all -> append the path" from the (now newline-stripped)
  # TEMPLATE itself (matches the pre-fix `grep -q '{FILE}'` check), not by comparing composed
  # output back to the template: a path whose value happens to be the literal string `{FILE}`
  # would slip past an output-equality check (compose-then-compare is not a substitute for
  # asking the template directly).
  case "$tmpl" in
    *'{FILE}'*) cmd=$(_upload_png_compose_cmd "$tmpl" "$quoted_png") ;;
    *)          cmd="$tmpl $quoted_png" ;;
  esac
  out=$(eval "$cmd" 2>/dev/null)
  out=$(printf '%s' "$out" | grep -oE 'https?://[^ ]+' | tail -1)
  [ -n "$out" ] || return 1; printf '%s' "$out"
}

POSTED_IMAGE=0   # set only when a screenshot is ACTUALLY uploaded + embedded (a real image)
if [ "${#SHOT_PATHS[@]}" -gt 0 ]; then
  BODY="### Visual proof"; idx=0
  while [ "$idx" -lt "${#SHOT_PATHS[@]}" ]; do
    png=${SHOT_PATHS[$idx]}; desc=${SHOT_DESCS[$idx]}; [ -n "$desc" ] || desc=$(basename "$png")
    if [ ! -f "$png" ]; then
      echo "[ship] WARNING: screenshot not found: $png (does NOT satisfy the gate)." >&2
      BODY="$BODY"$'\n\n'"**$desc**"$'\n'"_Visual proof MISSING — file not found: \`$png\`_"
    elif url=$(upload_png "$png"); then
      echo "[ship] uploaded '$desc' -> $url"; BODY="$BODY"$'\n\n'"**$desc**"$'\n'"![$desc]($url)"; POSTED_IMAGE=1
    else
      # No uploader / upload failed: we post a path note, but a path note is NOT an embedded
      # image, so it must NOT satisfy the UI screenshot gate. Do not bump VALID_SHOTS here.
      echo "[ship] NOTE: no uploader (SHIP_IMAGE_UPLOAD_CMD) or upload failed for '$desc' — embedding a path note (does NOT satisfy the gate)." >&2
      BODY="$BODY"$'\n\n'"**$desc**"$'\n'"_Local path: \`$png\` — upload manually._"
    fi
    idx=$((idx+1))
  done
  echo "[ship] posting visual-proof comment to PR #${PR} ..."; run gh pr comment "$PR" --body "$BODY"
fi

UI_TOUCHING=0
if [ -n "$UI_PATH_REGEX" ]; then
  if PR_FILES=$(gh pr diff "$PR" --name-only 2>/dev/null); then
    printf '%s\n' "$PR_FILES" | grep -qE "$UI_PATH_REGEX" && UI_TOUCHING=1
  else
    echo "Refusing: could not list changed files for PR #$PR — cannot evaluate the screenshot gate." >&2; exit 1
  fi
fi
if [ "$UI_TOUCHING" = "1" ]; then
  HAS_IMAGE=0
  # Only a really-uploaded image, or an image already embedded in the PR, counts — a failed
  # upload's path note does NOT satisfy the gate.
  if [ "$POSTED_IMAGE" = "1" ]; then HAS_IMAGE=1
  else
    PR_TEXT=$(gh pr view "$PR" --json body,comments -q '(.body // "") + "\n" + ([.comments[].body] | join("\n"))' 2>/dev/null || echo "")
    printf '%s' "$PR_TEXT" | grep -qE '!\[[^]]*\]\(https?://|<img |user-attachments/' && HAS_IMAGE=1
  fi
  if [ "$HAS_IMAGE" = "0" ]; then
    if [ -n "$NO_SHOT_OK" ]; then echo "[ship] UI screenshot gate OVERRIDDEN — reason: $NO_SHOT_OK"
    else
      echo "Refusing: PR #$PR touches UI but has NO embedded screenshot. Pass --screenshot <path> [desc]" >&2
      echo "  (with SHIP_IMAGE_UPLOAD_CMD set), or --no-screenshot-ok <reason> to override." >&2
      exit 1
    fi
  fi
fi

# --- review-quorum preflight gate (Guard-B, self-merge-authority program) --------------
# WHY this exists: the earlier gates verify CI is green and human/AI review THREADS are
# resolved, but neither proves an actual multi-model review RAN. This gate is the "strictly
# controlled" guarantee — it hard-refuses the merge unless review-cli's own record shows the
# PR's task code has enough PASSED review iterations across enough distinct models among those
# passed iterations (only clean-verdict runs count — a review that ran but failed/degraded, and
# pre-verdict-field history, never satisfy the bar; see `review task --help`). Fail-CLOSED: a
# missing `review` CLI, an unreadable store, no derivable task code, OR a quorum reading 0
# iterations / 0 distinct models all refuse rather than merge unverified — ship re-derives the
# verdict from the counts and never trusts the subprocess's `passed` boolean alone (#242).
#
# There is NO self-service override. When the bar is not met (or cannot be verified) and the
# agent genuinely needs to proceed, it sets RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>";
# that routes through the shared agenttools_hatch_escalation lib (the SAME lib the
# pin-primary-worktree / block-reset-hard agent-hooks use) to ask Alex live on Telegram, and the
# gate proceeds ONLY on his real-time approval. SHIP_REVIEW_QUORUM=0 is forbidden (gate stays on).
#
# Runs independently of --skip-ci, same posture as the review-dwell gate above.

# Prints the first ticket-like token found in $1, or nothing. Tries the repo's own HYP-<n>
# convention first (case-insensitive, normalized to uppercase; a Linear URL such as
# `https://linear.app/<team>/issue/HYP-1440/<slug>` carries the code in its path and is caught
# by this same arm), then a generic UPPERCASE-PREFIX-<n> ticket token (2+ uppercase letters, a
# hyphen, digits) so other repos' conventions (JIRA-style PROJ-123, etc.) are also picked up,
# then a purely descriptive review-cli task code (2+ uppercase letters, then 2-OR-MORE
# hyphen-joined 2+-letter uppercase segments -- 3+ segments total, no digits) so hand-picked
# codes like `SME-ROADMAP-WORKTREE-NOTE` or `WT-GITIGNORE-EXCLUDE` -- real task codes
# review-cli's own run-stats log records fine, just without a numeric suffix -- are also
# auto-derived (#384), then, as the last fallbacks for repos that track work as plain GitHub
# issues rather than a lettered ticket prefix, a keyword-anchored GitHub issue reference
# (`Fixes #105`, `Refs #105`) returned literal as `#105`, and finally a full issue URL of the
# PR's OWN repo (`https://github.com/<owner>/<repo>/issues/105`, also returned literal as
# `#105`; see each arm's own comment). `LC_ALL=C` on every grep here: outside
# the C locale, `[A-Z]` bracket ranges are collation-dependent (glibc's en_US.UTF-8 can interleave
# case), which would make the "uppercase-only" guarantee below hold only by luck of the ambient
# locale -- same fix idiom the `tr` calls elsewhere in this file already use.
#
# The descriptive arm is extracted in two stages: first the FULL boundary-delimited token
# (letters/digits/hyphens, stops at the first char outside that set), then an exact whole-token
# match against the letters-only shape, evaluated per CANDIDATE (not the whole text), so one
# rejected candidate never shadows a clean one appearing later. That order matters: a token with
# a stray digit buried inside a segment (`SME-ROADMAP-V2-NOTE`) is rejected outright instead of
# silently grep -o'ing a truncated prefix (`SME-ROADMAP-V`) as if it were the real code.
#
# The 3-segment floor (each segment 2+ letters) is a deliberate, documented trade-off: it rules
# out the common TWO-word hyphenated English that would otherwise false-positive on ordinary
# PR-body prose -- "READ-ONLY", "CI-CD", "PRE-COMMIT", "API-KEY", "OPT-IN" are all 2 segments and
# do NOT match. Two known, ACCEPTED residuals remain, same class of imprecision the numeric
# pattern above already tolerates for stray "AB-12"-shaped text, and both fail CLOSED regardless
# -- review-cli has no record for a wrongly-derived code, so ship still refuses, just with a less
# friendly message than "could not derive a task code" (#384 review notes):
#   - a rarer three-word phrase ("END-TO-END", "DO-NOT-MERGE") can still slip through uncaught;
#   - a digit fused directly onto (or hyphen-separated immediately before) the token's OWN first
#     segment ("2FA-SETUP-FLOW", "123-ABC-DEF-GHI") isn't caught by the boundary-free `[A-Z]`
#     start -- POSIX ERE has no lookbehind to assert "not preceded by a digit/letter" here;
#   - the numeric arm above runs FIRST and returns on its first match anywhere in the text, so an
#     incidental uppercase-acronym-plus-digit token that has nothing to do with the real ticket
#     ("UTF-8", "SHA-256", "RFC-2119", "ISO-8601" all match `[A-Z][A-Z]+-[0-9]+`) can shadow a
#     valid descriptive code appearing later in the same PR body -- the descriptive arm below then
#     never even runs. Same fail-closed trade-off: review-cli has no record for "UTF-8" either, so
#     ship still refuses, just without ever trying the real code (#384 review round 3).
_review_quorum_extract_ticket() {  # $1 = text -> prints ticket code, or nothing; ALWAYS exits 0
  local text="$1" m
  m=$(printf '%s\n' "$text" | LC_ALL=C grep -oiE 'HYP-[0-9]+' | head -1 || true)
  if [ -n "$m" ]; then printf '%s' "$m" | LC_ALL=C tr '[:lower:]' '[:upper:]'; return 0; fi
  m=$(printf '%s\n' "$text" | LC_ALL=C grep -oE '[A-Z][A-Z]+-[0-9]+' | head -1 || true)
  if [ -n "$m" ]; then printf '%s' "$m"; return 0; fi
  m=$(printf '%s\n' "$text" | LC_ALL=C grep -oE '[A-Z][A-Z0-9]*(-[A-Z0-9]+)*' \
        | LC_ALL=C grep -xE '[A-Z][A-Z]+(-[A-Z][A-Z]+){2,}' | head -1 || true)
  if [ -n "$m" ]; then printf '%s' "$m"; return 0; fi
  # GitHub issue reference (`Fixes #105`, `Refs #105`) — for repos (like conloca-landing) that
  # track work as GitHub issues rather than Linear/JIRA-style codes. Returned LITERAL, not
  # normalized: task-cli's own `_route_id_to_project` (tasklib/cli.py) routes a bare GitHub id
  # by checking `tid.startswith("#")` first — a `GH-<n>`-style rewrite doesn't match that check,
  # falls through to the Linear-team-prefix branch instead, and `task mark-shipped` fails with
  # an unroutable-id error. review-cli's own `normalize_task_code` (reviewlib/stats.py) accepts
  # any non-whitespace, non-control-character token — `#105` passes it unchanged, so there is no
  # downstream reason to rewrite it.
  # Only a KEYWORD-ANCHORED local reference qualifies (`Closes|Fixes|Resolves|Refs|References`
  # immediately before the `#<n>`, GitHub's own closing-keyword grammar plus the `Refs` form used
  # when the ticket must NOT auto-close on merge). Three hazards rule out a bare `#<n>` anywhere
  # in the text, all of them concrete because this same helper feeds BOTH the review-quorum
  # record lookup and the post-merge `task mark-shipped` call:
  #   - GitHub's qualified cross-repo syntax (`other-org/other-repo#123`, incl. repo names ending
  #     in `-`/`.`) names an issue in ANOTHER repo; extracting its `#123` would mark an unrelated
  #     local issue shipped. The keyword form never matches it: `#` must follow the keyword
  #     directly, never a repo name.
  #   - a URL fragment (`https://example.org/docs/#105`) or a Markdown anchor is not an issue.
  #   - an incidental prose mention (`follow-up for #12`, `(#268)`) of an ALREADY-SHIPPED issue
  #     would resolve to that issue's OLD passed quorum record, granting an unreviewed PR merge
  #     authority — unlike a wrongly-derived HYP/PROJ token, a referenced GitHub issue routinely
  #     HAS a record in an issue-tracked repo, so the "fail-closed anyway" reasoning above does
  #     not hold here.
  # This arm is the LAST fallback — after the generic PREFIX-<n> arm AND the descriptive
  # ALL-CAPS arm above — so a `Fixes #45` aside never shadows a real `PROJ-123` or
  # `SME-ROADMAP-WORKTREE-NOTE` code in the same body (for a descriptive-code repo that aside
  # would otherwise be the very borrowing path described above). The keyword itself must not be
  # the TAIL of a repo path (`org/hot-fix#5`, `org/fix#5`, `org/my.fix#5` are all qualified refs),
  # so the char before it may not be a repo-name char (`-`/`.`) or `/` either; the digits must end
  # at a non-word boundary (`#123abc` is not an issue ref). Two DISTINCT anchored refs in one text
  # (`Partially fixes #12 ... Closes #200`) are an ambiguous task code: every other branch of this
  # gate is fail-closed, so nothing is derived and ship refuses instead of silently taking the
  # first in document order. The match may swallow at most ONE trailing WORD char (to reject
  # `#123abc` below) and never the non-word char after the digits: `grep -o` matches don't
  # overlap, so consuming a `,` in `closes #12,fixes #200` would eat the second ref's leading
  # boundary and hide the ambiguity. Known limit (POSIX ERE has no lookahead): the guard sees
  # honest ambiguity with realistic separators (space, `,`, `;`, `.`+space, newline); a second
  # ref glued on by a word char (`#34xrefs #56`) or by a `/`/`.`/`-` (`#12/fixes #200`) keeps no
  # admissible leading boundary and stays hidden — an author who can craft that could as well
  # omit the ref, so it is not defended against. Trade-off, accepted: a bare `#105` with no
  # keyword (a `fix/#105` branch name, a `#105 widget` title) is NOT derived either — pass
  # $REVIEW_TASK_CODE.
  m=$(printf '%s\n' "$text" | LC_ALL=C grep -oiE \
      '(^|[^A-Za-z0-9_./-])(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?|references?)[[:space:]]*:?[[:space:]]*#[0-9]+[A-Za-z0-9_]?' \
      | LC_ALL=C grep -oE '#[0-9]+[A-Za-z0-9_]?$' | LC_ALL=C grep -xE '#[0-9]+' | LC_ALL=C sort -u || true)
  # `|| true`: `grep -c` exits 1 when the count is 0 -- which under this script's
  # `set -euo pipefail` is exactly the NEW path this change exists to serve (a body with no
  # keyword-anchored ref, only a URL). Guarded like every other grep in this function.
  local kw_n; kw_n=$(printf '%s\n' "$m" | LC_ALL=C grep -c '#') || true
  if [ "$kw_n" -eq 1 ]; then printf '%s' "$m"; return 0; fi
  [ "$kw_n" -gt 1 ] && return 0   # ambiguous anchored refs: fail closed, never fall through
  # Full GitHub issue URL of the PR's OWN repo (agent-tools#564) — the LAST fallback. task-cli's
  # `links` gate REQUIRES a ticket to be referenced as a markdown link / full URL (never a bare
  # `#548`), so a PR body written correctly for that gate carried NO shape the arms above
  # recognize and this gate refused it with "could not derive a task code"; agents then
  # hand-set REVIEW_TASK_CODE, the manual step that gets forgotten. The URL yields the SAME
  # literal `#<n>` the keyword arm yields, so review-cli's quorum record and task-cli's routing
  # see one code regardless of body syntax. Rules, mirroring the keyword arm's hazards:
  #   - the URL's owner/repo must be one of the PR's own slugs (_ship_own_repo_slugs: explicit
  #     --repo, checkout origin, live PR URL; compared case-insensitively — GitHub slugs are);
  #     an issue of ANOTHER repo is never this PR's
  #     ticket (cross-repo companions like "tg-cli#301 <-> agent-tools#524" are routine here);
  #     an unknown own-repo (no github origin, no --repo, no PR url) derives nothing.
  #   - only `/issues/<n>` counts — a `/pull/<n>` link is a PR, not a ticket; `www.` and a
  #     trailing `#issuecomment-…` anchor are tolerated; the digits must end at a non-word
  #     boundary (`/issues/12abc` is not an issue), same as the `#123abc` rule above.
  #   - two DISTINCT same-repo issue URLs are ambiguous → nothing (fail-closed); the same
  #     issue linked twice is one ticket.
  # No keyword anchor is required (unlike the `#<n>` arm): the links gate's own output shape is
  # `Refs [#548](https://…/issues/548)`, where `[` sits between any keyword and the URL, so an
  # anchor would defeat the purpose. Accepted residual: a body whose ONLY same-repo issue link
  # is an incidental mention of some other, already-shipped issue derives that issue — such a
  # body already violates the links discipline (it does not link its own ticket at all), and a
  # second same-repo link (the real ticket) turns the case into the ambiguity refusal.
  m=$(printf '%s\n' "$text" | LC_ALL=C grep -oiE \
      'https?://(www\.)?github\.com/[^/[:space:]]+/[^/[:space:]]+/issues/[0-9]+[A-Za-z0-9_]?' || true)
  [ -n "$m" ] || return 0
  local issues; issues=$(_ship_own_repo_issue_numbers "$m" | LC_ALL=C sort -u | LC_ALL=C sed '/^$/d')
  if [ "$(printf '%s\n' "$issues" | LC_ALL=C grep -c .)" -eq 1 ]; then printf '#%s' "$issues"; fi
  return 0
}

# $1 = newline-separated GitHub issue URLs (as matched by the extractor above) -> prints the
# issue NUMBER of every URL that names the PR's OWN repo, one per line (duplicates kept — the
# caller dedups and applies the ambiguity rule); prints nothing when the own repo is unknown.
# ALWAYS exits 0.
_ship_own_repo_issue_numbers() {
  local slugs cand slug n
  slugs=$(_ship_own_repo_slugs)
  [ -n "$slugs" ] || return 0
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    cand=$(printf '%s' "$cand" | LC_ALL=C tr '[:upper:]' '[:lower:]' \
             | LC_ALL=C sed -E 's|^https?://(www\.)?github\.com/||')
    while IFS= read -r slug; do
      case "$cand" in
        "$slug"/issues/*) n="${cand##*/}"
                          case "$n" in *[!0-9]*) ;; *) printf '%s\n' "$n" ;; esac ;;
      esac
    done <<< "$slugs"
  done <<< "$1"
  return 0
}

# The PR's OWN "owner/repo" slug, lowercased, for the issue-URL arm above — or nothing when it
# cannot be established (then that arm derives nothing: "same repo" is unverifiable). Sources, in
# order: the explicit gh target (GH_REPO — set from --repo / GH_SHIP_REPO above, so a foreign
# --repo ship compares against the TARGET repo, not this checkout), then this checkout's github
# origin (_CWD_ORIGIN_REPO, free — the common path never pays a network call), then, only when
# both are empty (a non-github origin; the test harness), the PR's own URL from `gh pr view`.
# Memoized: one resolution per ship run, however many texts the matcher scans.
_SHIP_OWN_REPO_SLUGS=""; _SHIP_OWN_REPO_SLUGS_RESOLVED=0
# -> prints every lowercase "owner/repo" slug that names the PR's OWN repo, one per line (none
# when it cannot be told); resolved ONCE per run; ALWAYS exits 0. The set is the union of the
# explicit `gh` target (GH_REPO, so a foreign `--repo` ship compares against the TARGET repo,
# not this checkout), this checkout's GitHub origin, and the LIVE PR URL. All three are kept —
# not first-non-empty — because after a repository rename/transfer an existing clone keeps the
# OLD, redirecting origin slug while the live PR and its issue links carry the NEW one (codex
# review, PR #566): trusting the origin alone would classify a correct same-repo issue link as
# foreign and refuse. GitHub redirects the old slug to the same repository, so an issue URL
# under either slug names this PR's own repo. The live URL costs one `gh pr view` on the URL
# path only (the arms before it never call this).
_ship_own_repo_slugs() {
  if [ "$_SHIP_OWN_REPO_SLUGS_RESOLVED" = "0" ]; then
    _SHIP_OWN_REPO_SLUGS_RESOLVED=1
    local raw live
    live=$(gh pr view "$PR" --json url -q '.url // ""' 2>/dev/null) || live=""
    live=$(printf '%s\n' "$live" | LC_ALL=C sed -E 's|/pull/[0-9]+.*$||')
    for raw in "${GH_REPO:-}" "${_CWD_ORIGIN_REPO:-}" "$live"; do
      raw=$(_ship_normalize_repo_slug "$raw")
      [ -n "$raw" ] || continue
      _SHIP_OWN_REPO_SLUGS="${_SHIP_OWN_REPO_SLUGS}${raw}"$'\n'
    done
    _SHIP_OWN_REPO_SLUGS=$(printf '%s' "$_SHIP_OWN_REPO_SLUGS" | LC_ALL=C sort -u)
  fi
  printf '%s' "$_SHIP_OWN_REPO_SLUGS"
  return 0
}
# $1 = a repo reference in any form gh accepts (`OWNER/REPO`, `HOST/OWNER/REPO`, a full URL,
# `.git` suffix) -> prints the lowercase `owner/repo`, or nothing when it does not reduce to
# exactly two path segments (unusable). ALWAYS exits 0.
_ship_normalize_repo_slug() {
  local slug
  slug=$(printf '%s\n' "$1" | LC_ALL=C tr '[:upper:]' '[:lower:]' \
           | LC_ALL=C sed -E 's|^https?://||; s|^(www\.)?github\.com[:/]||; s|\.git$||; s|/+$||')
  case "$slug" in
    */*/*|/*|*/|"") ;;
    */*) printf '%s' "$slug" ;;
  esac
  return 0
}

# Clamp a quorum floor value to the hard minimum (3). Fail-closed: an unset, non-numeric,
# zero, negative, or below-floor value is raised to 3; only a well-formed integer >= 3 passes
# through unchanged (an operator may RAISE the bar via SHIP_REVIEW_QUORUM_MIN_ITER/MODELS/ROLES,
# never lower it below 3). A 0 floor would let an empty record satisfy the gate via `0 >= 0` (#242).
# $1 = raw value, $2 = label (for the warning); prints the clamped integer, ALWAYS exits 0.
_review_quorum_clamp_floor() {
  local raw="$1" label="$2" floor=3
  case "$raw" in
    ''|*[!0-9]*)  # unset, empty, negative (has '-'), or otherwise non-numeric -> hard floor
      [ -n "$raw" ] && echo "[ship] review-quorum: ignoring invalid ${label}='${raw}' — using hard floor ${floor}." >&2
      printf '%s' "$floor"; return 0 ;;
  esac
  if [ "$raw" -lt "$floor" ]; then
    echo "[ship] review-quorum: ${label}=${raw} is below the hard floor — raising to ${floor} (the bar can be raised, never lowered)." >&2
    printf '%s' "$floor"; return 0
  fi
  printf '%s' "$raw"
  return 0
}

# Append one audit line to the review-quorum audit log. Best-effort: a logging failure must
# never block or unblock the ship, so failures here are swallowed (`|| true`).
# $1=decision(authorized|bypass:approved|bypass:denied|refused) $2=task_code $3=iterations
# $4=models $5=reason(optional — the hatch verdict for bypass:* decisions) $6=roles(optional —
# distinct BOARD ROLES actually achieved) $7=min_roles(optional — the role floor enforced for
# this decision, review-cli#246: role-based coverage is now the primary/default gate mechanism,
# so the audit trail must show what actually gated it, not just the model count) $8=min_models
# (optional — 0 means the model floor was NOT enforced for this decision; a genuinely ENFORCED
# floor is always clamped to >=3 by _review_quorum_clamp_floor and can therefore never itself be
# 0, so 0 is an unambiguous "not enforced" sentinel — without this an `authorized` line reading
# `models:1` alone can't tell an auditor whether an explicit model floor was even in force)
_review_quorum_audit_log() {
  local decision="$1" code="$2" iterations="${3:-0}" models="${4:-0}" reason="${5:-}" \
        roles="${6:-0}" min_roles="${7:-0}" min_models="${8:-0}"
  # Honor the --dry-run contract ("print what would happen; change nothing"): a simulated ship
  # must not create or pollute the real audit record. The gate above still evaluates and prints
  # its authorized/refused verdict — only the persistent write is suppressed here.
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would append review-quorum audit: decision=${decision} task=${code} iter=${iterations} models=${models} (floor ${min_models}) roles=${roles}/${min_roles}" >&2
    return 0
  fi
  local file="${SHIP_AUDIT_FILE:-$HOME/.config/agent-tools/ship-audit.jsonl}"
  mkdir -p "$(dirname "$file")" 2>/dev/null || return 0
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg ts "$ts" --arg pr "$PR" --arg code "$code" --argjson it "$iterations" \
      --argjson m "$models" --argjson rl "$roles" --argjson mrl "$min_roles" \
      --argjson mm "$min_models" --arg dec "$decision" \
      --arg reason "$reason" \
      '{ts:$ts, pr:$pr, task_code:$code, iterations:$it, models:$m, min_models:$mm, roles:$rl, min_roles:$mrl, decision:$dec} +
       (if $reason == "" then {} else {override_reason:$reason} end)' \
      >> "$file" 2>/dev/null || true
  else
    # jq-less fallback so the audit line is never DROPPED just because jq is absent (jq-missing is
    # itself a gate refusal that can reach the hatch, so the fail-closed bypass:denied audit must
    # still land). Emit minimal JSON via printf. The free-text fields (pr, task_code, reason) are
    # each made JSON-safe: newlines/CR/tab -> space and other control chars stripped (so none can
    # inject a forged extra JSONL line), then backslash + double-quote escaped. `pr` is escaped too
    # — it is a bare CLI arg, so a quote/control char in it must not corrupt the line (the jq path
    # is already safe via --arg). iterations/models/roles/min_roles/min_models are validated
    # integers; decision is internal.
    local esc_pr esc_code esc_reason
    esc_pr=$(printf '%s' "$PR" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    esc_code=$(printf '%s' "$code" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    esc_reason=$(printf '%s' "$reason" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    { if [ -n "$reason" ]; then
        printf '{"ts":"%s","pr":"%s","task_code":"%s","iterations":%s,"models":%s,"min_models":%s,"roles":%s,"min_roles":%s,"decision":"%s","override_reason":"%s"}\n' \
          "$ts" "$esc_pr" "$esc_code" "${iterations:-0}" "${models:-0}" "${min_models:-0}" "${roles:-0}" "${min_roles:-0}" "$decision" "$esc_reason"
      else
        printf '{"ts":"%s","pr":"%s","task_code":"%s","iterations":%s,"models":%s,"min_models":%s,"roles":%s,"min_roles":%s,"decision":"%s"}\n' \
          "$ts" "$esc_pr" "$esc_code" "${iterations:-0}" "${models:-0}" "${min_models:-0}" "${roles:-0}" "${min_roles:-0}" "$decision"
      fi; } >> "$file" 2>/dev/null || true
  fi
}

# Ask Alex, live on Telegram, to approve a one-time review-quorum bypass, by invoking the
# ci/ship/review_quorum_hatch.py helper (which delegates to the shared agenttools_hatch_escalation
# lib exactly as the agent-hooks do). Called ONLY when RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM is set.
# The helper prints a verdict SENTINEL + reason on stdout ("APPROVED …" / "DENIED …") and owns the
# non-dry-run bypass:* audit line; in dry-run it writes no persistent audit and ship.sh prints the
# would-be audit preview. Its exit code: 0 approved, 1 requested-but-not-approved
# (blank/bare/denied/timeout), 3 the shared lib could not be imported (fail-closed — e.g. ship.sh
# copied out of the checkout). ship.sh authorizes ONLY on exit 0 AND a leading APPROVED sentinel.
#
# SECURITY: neither WHICH lib runs nor WHICH tg-ctl is asked is shipper-controllable. The helper
# imports the lib from a fixed path, and resolves tg-ctl off the OS identity's REAL home
# (pwd.getpwuid) — NOT the $HOME env var and NOT the repo worktree — so no ambient env var and no
# rig.yaml a PR commits can redirect approval to an always-approve stub. (SHIP_HATCH_TIMEOUT_S can
# only SHORTEN the wait, which fails CLOSED — a shorter wait denies, never approves — so it is a
# safe tuning knob, not a bypass.)
_review_quorum_hatch_check() {  # uses $TASK_CODE $QITER $QMODELS_N $PR
  # `python3 -I` = isolated mode: ignores PYTHONPATH/PYTHONHOME, skips the user site and
  # sitecustomize, and does not add cwd to sys.path — so a shipping agent cannot inject a
  # malicious module or a startup hook that would self-approve. The helper additionally loads the
  # shared lib by explicit file path (see its docstring), belt-and-suspenders.
  SHIP_HATCH_PR="$PR" \
  SHIP_HATCH_CODE="${TASK_CODE:-}" \
  SHIP_HATCH_ITER="${QITER:-0}" \
  SHIP_HATCH_MODELS="${QMODELS_N:-0}" \
  SHIP_HATCH_ROLES="${QROLES_N:-0}" \
  SHIP_DRY_RUN="$DRY_RUN" \
  python3 -I "$_SHIP_HATCH_PY"
}

# Consults review-cli's own capacity-check (reviewlib/capacity_check.py, review-cli's
# `review capacity-check --json`) for "is EVERY configured review-board seat confirmed
# exhausted right now" -- the SAME harness-limits/seat-cooldown evidence that command computes,
# never a re-implementation here. Fails CLOSED on ANY problem: a missing `review`/`jq`, a
# non-zero exit, empty/unparseable JSON, or a missing/non-true `all_exhausted` key all print
# "0" -- "not confirmed" is always the safe default; only a positive machine-readable
# confirmation ever short-circuits a manual hatch prompt. Stdout: "1<TAB><evidence text>" when
# confirmed, "0" otherwise. Always returns 0 -- callers read stdout, never $?.
_review_capacity_all_exhausted() {
  command -v review >/dev/null 2>&1 || { echo "0"; return 0; }
  command -v jq >/dev/null 2>&1 || { echo "0"; return 0; }
  local json rc=0
  json=$(review capacity-check --json 2>/dev/null) || rc=$?
  if [ "$rc" != "0" ] || [ -z "$json" ]; then echo "0"; return 0; fi
  local all_ex
  all_ex=$(printf '%s' "$json" | jq -r 'if .all_exhausted == true then "true" else "false" end' 2>/dev/null) || { echo "0"; return 0; }
  if [ "$all_ex" != "true" ]; then echo "0"; return 0; fi
  local evidence
  evidence=$(printf '%s' "$json" | jq -r '[.exhausted[]? | "\(.model): \(.evidence // .source // "exhausted")"] | join("; ")' 2>/dev/null) || evidence=""
  printf '1\t%s\n' "$evidence"
}

# Terminal handler for a review-quorum refusal: either the hatch escalation approves a one-time
# bypass (return 0 -> ship proceeds), or the ship is refused (exits 1). $1 is the human one-line
# summary of WHY the bar wasn't met (or couldn't be verified). Uses $TASK_CODE / $QITER /
# $QMODELS_N (set to safe defaults by the caller before any early refusal).
_review_quorum_refuse_or_hatch() {  # $1 = refusal summary
  local summary="$1"
  # Auto-detect a confirmed fleet-wide review-cli capacity outage BEFORE ever considering the
  # manual one-time hatch (Alex's repeated "stop asking me every time for the same known cause"
  # ask). Fires ONLY when review-cli's own capacity-check confirms EVERY configured board seat
  # is exhausted right now; an unmapped/never-dispatched seat always reads "not confirmed"
  # there (see _review_capacity_all_exhausted above), so this never fires on missing data.
  # Reaching this function at ALL already proves CI is not genuinely red on a real test
  # failure: the green-CI gate (ci_appears_structurally_down / _empty_rollup_is_ci_outage /
  # run_local_ci_gate, far above) runs UNCONDITIONALLY earlier in this script under
  # `set -euo pipefail` and `exit 1`s before this point on any unresolved real failure, so no
  # separate CI check is needed here -- only the review-capacity signal is new.
  local cap_out cap_flag=0 cap_evidence=""
  cap_out=$(_review_capacity_all_exhausted 2>/dev/null) || cap_out="0"
  case "$cap_out" in
    1*) cap_flag=1; cap_evidence="${cap_out#1$'\t'}" ;;
  esac
  if [ "$cap_flag" = "1" ]; then
    echo "[ship] review-quorum gate — ${summary}; review-cli confirms EVERY configured provider is exhausted right now (${cap_evidence:-see \`review capacity-check --json\`}) — auto-proceeding without a manual hatch prompt. Recorded to \$SHIP_AUDIT_FILE."
    _review_quorum_audit_log "auto-proceed:capacity-outage" "${TASK_CODE:-}" "${QITER:-0}" "${QMODELS_N:-0}" "$cap_evidence" "${QROLES_N:-0}" "${MIN_ROLES:-0}" "${AUDIT_MIN_MODELS:-0}"
    return 0
  fi
  # Distinguish TRULY-UNSET (no bypass requested) from set-but-empty (an invalid request the lib
  # denies): `${var+x}` is empty only when the var is unset. Unset -> refuse with the how-to;
  # set (even blank) -> route through the helper, which denies blank/bare and asks on a real reason.
  if [ -z "${RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM+x}" ]; then
    { echo "Refusing: review-quorum gate — ${summary}."
      echo "  Run more independent review iterations (e.g. \`review diff --task ${TASK_CODE:-<code>}\`) across distinct BOARD ROLES, then re-run ship."
      echo "  There is NO self-service override. To request a ONE-TIME bypass, set:"
      echo "    RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM=\"<justification>\""
      echo "  which asks Alex live on Telegram and proceeds ONLY on his real-time approval."
      echo "  SHIP_REVIEW_QUORUM=0 is forbidden and does not disable this gate."; } >&2
    _review_quorum_audit_log refused "${TASK_CODE:-}" "${QITER:-0}" "${QMODELS_N:-0}" "" "${QROLES_N:-0}" "${MIN_ROLES:-0}" "${AUDIT_MIN_MODELS:-0}"
    exit 1
  fi
  # The helper prints a verdict SENTINEL on stdout ("APPROVED <reason>" / "DENIED <reason>") and
  # writes its own non-dry-run bypass:approved / bypass:denied audit line. We authorize the bypass
  # ONLY on a clean exit 0 AND a leading APPROVED sentinel: a fake or broken `python3` that merely
  # exits 0 without the sentinel then fails CLOSED (refuse), instead of being mistaken for approval
  # — the same fail-closed posture the other gates have on a tool malfunction. (A shipper who fully
  # controls the ship PROCESS's PATH can defeat any gate — fake `gh`, `review`, `git` — so this is
  # not a claim to withstand a hostile PATH; it removes the fail-OPEN asymmetry for a benign one.)
  local hrc=0 hout
  hout=$(_review_quorum_hatch_check 2>/dev/null) || hrc=$?
  local hverdict="${hout%% *}" hreason=""
  case "$hout" in *" "*) hreason="${hout#* }" ;; esac
  if [ "$hrc" = "0" ] && [ "$hverdict" = "APPROVED" ]; then
    echo "[ship] review-quorum gate — ${summary}; a one-time Telegram hatch escalation was APPROVED by Alex — proceeding. (${hreason})"
    return 0
  fi
  { echo "Refusing: review-quorum gate — ${summary}."
    echo "  A Telegram hatch escalation was requested but NOT approved: ${hreason:-no approval}."
    echo "  Obtain live approval, or add more independent review iterations across distinct BOARD ROLES."; } >&2
  # The real helper logs bypass:denied itself only when it emitted the DENIED sentinel during a
  # real run. Under --dry-run it deliberately writes nothing, so ask the shared audit helper to
  # print the would-be audit line. In every other non-DENIED case (fake/broken/absent interpreter,
  # import-fail, unexpected verdict), the helper did NOT audit, so record the fail-closed
  # bypass:denied here. Never double-write.
  #
  # SCHEMA NOTE: the shell-written bypass:denied line below (and refused/authorized elsewhere in
  # this file) now carries roles/min_roles/min_models. The bypass:APPROVED line, in contrast, is
  # written entirely by the separate lib/agenttools_hatch_escalation lib (invoked from
  # _review_quorum_hatch_check above) and does NOT yet carry these fields — this diff only widened
  # the shell-side call sites, not that lib's own audit call. Tracked: agent-tools#414.
  case "$hverdict" in
    DENIED) [ "$DRY_RUN" = "1" ] && _review_quorum_audit_log "bypass:denied" "${TASK_CODE:-}" "${QITER:-0}" "${QMODELS_N:-0}" "$hreason" "${QROLES_N:-0}" "${MIN_ROLES:-0}" "${AUDIT_MIN_MODELS:-0}" ;;
    *) _review_quorum_audit_log "bypass:denied" "${TASK_CODE:-}" "${QITER:-0}" "${QMODELS_N:-0}" "$hreason" "${QROLES_N:-0}" "${MIN_ROLES:-0}" "${AUDIT_MIN_MODELS:-0}" ;;
  esac
  exit 1
}

# --- --skip-ci hatch gate (deny-by-default; one-time live Telegram approval) --------------
# `--skip-ci` is a BLIND admin-merge: it skips the entire green-CI gate AND branch protection
# (`gh pr merge --admin`), verifying nothing locally. The LEGITIMATE "CI is billing-blocked /
# infrastructure is down" case never needs it — the normal (SKIP_CI=0) path auto-detects the
# outage (_empty_rollup_is_ci_outage / ci_appears_structurally_down), runs the local fallback
# gate, and does a NORMAL non-admin merge. So a bare --skip-ci is a pure self-service bypass and
# is DENY-BY-DEFAULT: the ONLY way through is a one-time approval Alex grants live on Telegram,
# requested by setting RIG_HATCH_REQUEST_SHIP_SKIP_CI="<written justification>". No env var an
# agent can set alone unlocks it (a blank/bare "1" is rejected by the helper); authority (tg-ctl)
# is resolved from the account's REAL home, never the repo/cwd — same hardening as the
# review-quorum gate; see ci/ship/skip_ci_hatch.py.

# One skip-ci audit line -> SHIP_AUDIT_FILE, mirroring _review_quorum_audit_log's dry-run contract
# and jq-less JSON-safety. The Python helper owns the non-dry-run bypass:approved/denied line; this
# shell helper records only the cases the helper does NOT (env-absent "refused", and the fail-closed
# paths) — never double-writing.
_skip_ci_audit_log() {  # $1=decision $2=reason(optional)
  local decision="$1" reason="${2:-}"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would append skip-ci audit: decision=${decision}" >&2
    return 0
  fi
  local file="${SHIP_AUDIT_FILE:-$HOME/.config/agent-tools/ship-audit.jsonl}"
  mkdir -p "$(dirname "$file")" 2>/dev/null || return 0
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local esc_pr esc_branch esc_reason
  esc_pr=$(printf '%s' "$PR" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
  esc_branch=$(printf '%s' "${BRANCH:-}" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
  esc_reason=$(printf '%s' "$reason" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
  { if [ -n "$reason" ]; then
      printf '{"ts":"%s","pr":"%s","branch":"%s","gate":"skip-ci","decision":"%s","override_reason":"%s"}\n' \
        "$ts" "$esc_pr" "$esc_branch" "$decision" "$esc_reason"
    else
      printf '{"ts":"%s","pr":"%s","branch":"%s","gate":"skip-ci","decision":"%s"}\n' \
        "$ts" "$esc_pr" "$esc_branch" "$decision"
    fi; } >> "$file" 2>/dev/null || true
}

# Ask Alex, live on Telegram, to approve a one-time --skip-ci bypass. `python3 -I` = isolated mode
# (ignores PYTHONPATH/PYTHONHOME, skips user-site/sitecustomize, no cwd on sys.path) so a shipping
# agent cannot inject a self-approving module; the helper additionally loads the shared lib by
# explicit file path. Called ONLY when RIG_HATCH_REQUEST_SHIP_SKIP_CI is set.
_skip_ci_hatch_check() {  # uses $PR $BRANCH $DRY_RUN
  SHIP_HATCH_PR="$PR" \
  SHIP_HATCH_BRANCH="${BRANCH:-}" \
  SHIP_DRY_RUN="$DRY_RUN" \
  python3 -I "$_SHIP_SKIP_CI_HATCH_PY"
}

# Terminal gate for --skip-ci: returns 0 to proceed with the admin merge (env unset -> refuse with
# how-to; env set -> the helper's live Telegram verdict, authorized ONLY on exit 0 + a leading
# APPROVED sentinel so a fake/broken python3 fails CLOSED). Otherwise exits 1.
_skip_ci_hatch_gate() {
  if [ -z "${RIG_HATCH_REQUEST_SHIP_SKIP_CI+x}" ]; then
    { echo "Refusing: --skip-ci is a BLIND admin-merge that bypasses the green-CI gate AND branch protection — it is deny-by-default and needs a one-time live approval from Alex."
      echo "  The legitimate 'CI is billing-blocked / down' case does NOT need --skip-ci: run ship WITHOUT it — the normal path auto-detects the outage, runs the local fallback gate, and does a normal (non-admin) merge."
      echo "  There is NO self-service override. To request a ONE-TIME bypass, set:"
      echo "    RIG_HATCH_REQUEST_SHIP_SKIP_CI=\"<justification>\""
      echo "  which asks Alex live on Telegram and proceeds ONLY on his real-time approval."; } >&2
    _skip_ci_audit_log "skipci:refused" ""
    exit 1
  fi
  local hrc=0 hout
  hout=$(_skip_ci_hatch_check 2>/dev/null) || hrc=$?
  local hverdict="${hout%% *}" hreason=""
  case "$hout" in *" "*) hreason="${hout#* }" ;; esac
  if [ "$hrc" = "0" ] && [ "$hverdict" = "APPROVED" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      # In --dry-run the helper sends NO Telegram round-trip: an APPROVED here means only
      # "deny-by-default is satisfied (a real written justification is present)", NOT that Alex
      # approved. Say so — a real run would still require his live approval before the admin merge.
      echo "[ship] --skip-ci --dry-run: justification present — a REAL run would request live Telegram approval before the admin merge (no approval requested in dry-run). (${hreason})"
    else
      echo "[ship] --skip-ci: a one-time Telegram hatch escalation was APPROVED by Alex — proceeding with the admin merge. (${hreason})"
    fi
    return 0
  fi
  { echo "Refusing: --skip-ci hatch escalation was requested but NOT approved: ${hreason:-no approval}."
    echo "  Obtain live approval, or drop --skip-ci and let the normal CI gate / billing-outage fallback run."; } >&2
  # The helper logs its own bypass:denied only when it emitted DENIED during a REAL run. In dry-run
  # it writes nothing (ship prints the would-be line); in every other non-DENIED case (fake/broken/
  # absent interpreter, import-fail, unexpected verdict) it did NOT audit -> record fail-closed here.
  case "$hverdict" in
    DENIED) [ "$DRY_RUN" = "1" ] && _skip_ci_audit_log "skipci:bypass:denied" "$hreason" ;;
    *) _skip_ci_audit_log "skipci:bypass:denied" "$hreason" ;;
  esac
  exit 1
}

QUORUM_ENABLED=1
# SHIP_REVIEW_QUORUM=0 / SHIP_REVIEW_QUORUM_ENABLED=0 is forbidden (agent-tools#644).
# Reject at parse: never disable Guard-B. A 0 is not an ops off-switch.
# Test-only isolation: SHIP_TEST_DISABLE_REVIEW_QUORUM=1 is honored ONLY when
# PYTEST_CURRENT_TEST is set (pytest exports it into the test process and its
# subprocesses). A production `gh ship` has no such var, so this cannot recreate
# the SHIP_REVIEW_QUORUM=0 off-switch (agent-tools#644 Codex P1).
case "${SHIP_REVIEW_QUORUM_ENABLED:-1}" in
  0|false|no)
    echo "[ship] SHIP_REVIEW_QUORUM_ENABLED=${SHIP_REVIEW_QUORUM_ENABLED} is forbidden — review-quorum gate stays enabled." >&2
    ;;
esac
case "${SHIP_REVIEW_QUORUM:-1}" in
  0|false|no)
    echo "[ship] SHIP_REVIEW_QUORUM=${SHIP_REVIEW_QUORUM} is forbidden — review-quorum gate stays enabled." >&2
    ;;
esac
if [ -n "${PYTEST_CURRENT_TEST:-}" ]; then
  case "${SHIP_TEST_DISABLE_REVIEW_QUORUM:-0}" in 1|true|yes) QUORUM_ENABLED=0 ;; esac
fi

if [ "$QUORUM_ENABLED" = "0" ]; then
  echo "[ship] review-quorum gate disabled (SHIP_TEST_DISABLE_REVIEW_QUORUM=1, test-only)."
else
  # Hard fail-closed ROLE floor (#644): authorization is distinct_roles_passed >= MIN_ROLES
  # (clamped to >=3). passed_iterations must be >0 (hollow-payload guard), not >= MIN_ITER.
  # MIN_ITER is still parsed and passed to review-cli as telemetry. An explicit
  # SHIP_REVIEW_QUORUM_MIN_MODELS additionally requires that many models. A 0 role floor
  # would let empty evidence pass (#242), so clamp every unset/non-numeric/<3 value UP to 3.
  MIN_ITER=$(_review_quorum_clamp_floor "${SHIP_REVIEW_QUORUM_MIN_ITER:-}" "min-iter")
  MIN_MODELS=$(_review_quorum_clamp_floor "${SHIP_REVIEW_QUORUM_MIN_MODELS:-}" "min-models")
  MIN_ROLES=$(_review_quorum_clamp_floor "${SHIP_REVIEW_QUORUM_MIN_ROLES:-}" "min-roles")
  # Explicitness (not the resolved value) decides whether the model floor governs — mirroring
  # review-cli's own explicit-vs-default distinction (review-cli#246): `${VAR+x}` is empty only
  # when the var is genuinely unset, so an operator who sets SHIP_REVIEW_QUORUM_MIN_MODELS to a
  # bad value is still "explicit" here (the clamp above still raises it to the hard floor).
  MIN_MODELS_EXPLICIT=0
  [ -n "${SHIP_REVIEW_QUORUM_MIN_MODELS+x}" ] && MIN_MODELS_EXPLICIT=1
  # The min_models value recorded in the audit log: 0 (an unambiguous "not enforced" sentinel,
  # since a genuinely enforced floor is always clamped to >=3) unless the operator opted in.
  AUDIT_MIN_MODELS=0
  [ "$MIN_MODELS_EXPLICIT" = "1" ] && AUDIT_MIN_MODELS="$MIN_MODELS"
  # Safe defaults so an early refusal (before the review query) still has these for the audit
  # line and the hatch context.
  QITER=0; QMODELS_N=0; QMODELS=""; QROLES_N=0; QROLES=""; QERR=""; QPASSED=false

  TASK_CODE="${REVIEW_TASK_CODE:-}"
  [ -n "$TASK_CODE" ] || TASK_CODE=$(_review_quorum_extract_ticket "$BRANCH")
  if [ -z "$TASK_CODE" ]; then
    PR_BODY_QC=$(gh pr view "$PR" --json body -q '.body // ""' 2>/dev/null) || PR_BODY_QC=""
    TASK_CODE=$(_review_quorum_extract_ticket "$PR_BODY_QC")
  fi

  if [ -z "$TASK_CODE" ]; then
    _review_quorum_refuse_or_hatch "could not derive a task code (set \$REVIEW_TASK_CODE, or put the ticket code e.g. HYP-931 — or a link to THIS repo's issue, https://github.com/<owner>/<repo>/issues/<n> — in the branch name or PR body)"
  elif ! command -v review >/dev/null 2>&1; then
    _review_quorum_refuse_or_hatch "'review' CLI not found on PATH — cannot verify the bar for ${TASK_CODE} (install review-cli)"
  elif ! command -v jq >/dev/null 2>&1; then
    _review_quorum_refuse_or_hatch "jq not found — cannot evaluate the gate for ${TASK_CODE} (install jq)"
  else
    # Role-based coverage (--min-roles) is ALWAYS requested — it is the primary/default gate now
    # (review-cli#246). --min-models is passed ONLY when the operator explicitly set
    # SHIP_REVIEW_QUORUM_MIN_MODELS: passing it unconditionally would make review-cli treat the
    # model floor as EXPLICITLY requested too (a subprocess CLI flag can't carry "this is just
    # ship's internal default"), which would silently reinstate a default model floor via
    # review-cli's own AND logic (review-cli#246) — exactly what this change removes.
    #
    # Three-tier query, oldest-compatible-flag-set-last, because review-cli's own history proves
    # --min-roles and --quorum-check were NEVER both supported by the same build: --quorum-check
    # was renamed to --check on 2026-07-09 (review-cli#135), a full six weeks before --min-roles
    # existed at all (added 2026-08-21, review-cli#246). So sending --min-roles on a --quorum-check
    # attempt can never succeed against any real build — it would only misdiagnose "review-cli is
    # too old for role checking" as "could not query review-cli" (a real finding from review, since
    # a --check-supporting-but-pre-#246 build, built in that six-week window, would otherwise fail
    # BOTH the --check-with-roles attempt and the --quorum-check-with-roles fallback, landing on
    # the wrong, confusing refusal message):
    #   1. --check WITH --min-roles (+ --min-models if explicit) — the current/target shape.
    #   2. --check WITHOUT --min-roles (+ --min-models if explicit) — a build that has the --check
    #      rename but predates role support; succeeds with real iter/model data and NO role keys
    #      (review-cli only emits distinct_roles_passed/roles when --min-roles was actually asked),
    #      so the gate below reads 0 roles and refuses with an honest "0/N distinct roles" instead
    #      of a hollow "could not query" — accurate, still fail-closed (role coverage is mandatory
    #      now, so an old review-cli genuinely cannot satisfy this gate until upgraded).
    #   3. --quorum-check WITHOUT --min-roles (+ --min-models if explicit) — genuinely ancient,
    #      pre-rename builds; same honest 0-roles refusal as tier 2 if it succeeds.
    # In --json mode review-cli always prints JSON to stdout (pass or fail) — only an
    # unsupported-flag argparse error leaves stdout empty, which is each tier's trigger.
    # --no-verify-identity: a padded one-model `--task -m astra` record often has empty
    # recorded_diff_files; identity verification then zeros roles and ship hatches (PR 87).
    # Task code still scopes the record. review-cli#433 padding + role floor remain the bar.
    REVIEW_CHECK_ARGS_NOROLES=(--min-iter "$MIN_ITER" --no-verify-identity)
    [ "$MIN_MODELS_EXPLICIT" = "1" ] && REVIEW_CHECK_ARGS_NOROLES+=(--min-models "$MIN_MODELS")
    REVIEW_CHECK_ARGS=("${REVIEW_CHECK_ARGS_NOROLES[@]}" --min-roles "$MIN_ROLES")

    # QUORUM_ROLES_REQUESTED tracks whether the tier that actually produced QUORUM_JSON included
    # --min-roles, so a genuine "0 distinct roles" (tier 1: review-cli understood the request and
    # simply found no role-tagged history) can be told apart, below, from "this review-cli cannot
    # report roles at all" (tier 2/3: the request never even asked) — the two have different
    # remedies (re-review with roles vs. upgrade review-cli).
    QUORUM_ROLES_REQUESTED=1
    QUORUM_JSON=$(review task "$TASK_CODE" --check "${REVIEW_CHECK_ARGS[@]}" --json 2>/dev/null) || true
    if [ -z "$QUORUM_JSON" ]; then
      QUORUM_ROLES_REQUESTED=0
      QUORUM_JSON=$(review task "$TASK_CODE" --check "${REVIEW_CHECK_ARGS_NOROLES[@]}" --json 2>/dev/null) || true
    fi
    if [ -z "$QUORUM_JSON" ]; then
      QUORUM_JSON=$(review task "$TASK_CODE" --quorum-check "${REVIEW_CHECK_ARGS_NOROLES[@]}" --json 2>/dev/null) || true
    fi

    if [ -z "$QUORUM_JSON" ]; then
      _review_quorum_refuse_or_hatch "could not query review-cli (store unreadable / task ${TASK_CODE} unknown)"
    else
      # review-cli's quorum_check emits `passed_iterations` / `distinct_models_passed` /
      # `distinct_roles_passed` — the COUNT of PASSED iterations and the distinct models / BOARD
      # ROLES among them. An earlier revision read `.iterations` / `.distinct_models`, keys
      # review-cli NEVER emits, so both parsed to 0 (the "0 iterations across 0 models" in the
      # #242 incident log). Read ONLY the real keys — do NOT fall back to the never-emitted
      # legacy names: a payload carrying only `.iterations` / `.distinct_models` is not
      # review-cli's output (old build or hostile `review` on PATH), so it reads as 0/0 and fails
      # closed below, never authorizes via a laxer key.
      QPASSED=$(printf '%s' "$QUORUM_JSON" | jq -r '.passed // false' 2>/dev/null || echo false)
      QITER=$(printf '%s' "$QUORUM_JSON" | jq -r '.passed_iterations // 0' 2>/dev/null || echo 0)
      QMODELS_N=$(printf '%s' "$QUORUM_JSON" | jq -r '.distinct_models_passed // 0' 2>/dev/null || echo 0)
      QMODELS=$(printf '%s' "$QUORUM_JSON" | jq -r '(.models // []) | join(", ")' 2>/dev/null || echo "")
      QROLES_N=$(printf '%s' "$QUORUM_JSON" | jq -r '.distinct_roles_passed // 0' 2>/dev/null || echo 0)
      QROLES=$(printf '%s' "$QUORUM_JSON" | jq -r '(.roles // []) | join(", ")' 2>/dev/null || echo "")
      QERR=$(printf '%s' "$QUORUM_JSON" | jq -r '.error // empty' 2>/dev/null || echo "")
      # Whether the ANSWER (not just what ship sent) actually carried role data — read from the
      # payload's own shape (does it have the key at all?), not from QUORUM_ROLES_REQUESTED. A
      # review-cli that tolerates an unrecognized --min-roles (e.g. argparse's parse_known_args,
      # or a shim) could "succeed" on the roles-requesting tier while silently ignoring the flag —
      # QUORUM_ROLES_REQUESTED would then be wrong (it only tracks what ship attempted), but the
      # payload shape is still honest. Used ONLY for the disambiguating hint below; the authorize
      # gate keeps requiring QUORUM_ROLES_REQUESTED independently (defense-in-depth, not replaced).
      QUORUM_HAS_ROLE_KEY=$(printf '%s' "$QUORUM_JSON" | jq -r 'has("distinct_roles_passed")' 2>/dev/null || echo false)
      # Non-numeric / missing counts collapse to 0 so the arithmetic gate below fails closed
      # (jq can hand back "null" as text if a key holds JSON null).
      case "$QITER" in ''|*[!0-9]*) QITER=0 ;; esac
      case "$QMODELS_N" in ''|*[!0-9]*) QMODELS_N=0 ;; esac
      case "$QROLES_N" in ''|*[!0-9]*) QROLES_N=0 ;; esac

      # The model floor gates ONLY when the operator explicitly asked for it — vacuously
      # satisfied otherwise, mirroring review-cli's own explicit-vs-default AND logic
      # (review-cli#246): an explicit request is always honored, but there is no default model
      # floor any more now that role-based coverage is the primary/default mechanism.
      MODELS_GATE_OK=1
      if [ "$MIN_MODELS_EXPLICIT" = "1" ]; then
        MODELS_GATE_OK=0
        [ "$QMODELS_N" -gt 0 ] && [ "$QMODELS_N" -ge "$MIN_MODELS" ] && MODELS_GATE_OK=1
      fi

      # FAIL-CLOSED authorization (#242 / #644): NEVER authorize on the subprocess's `.passed`
      # boolean alone. The predicate is ROLE coverage: QROLES_N must be strictly positive and
      # meet MIN_ROLES. distinct_models_passed is telemetry — a padded one-model record may
      # report 0 models and still authorize (review-cli#433). Empty evidence (0 roles) with a
      # forged passed:true still refuses. Authorize ONLY when EVERY condition holds: the
      # subprocess agreed (passed==true), there is NO error key, ship itself actually ASKED for
      # role coverage on the query that answered (QUORUM_ROLES_REQUESTED), iteration and role
      # counts independently meet their >=3 floors, and (only when explicitly requested) the
      # model floor is met too.
      # Predicate is ROLE coverage (agent-tools#644 / review-cli#433). distinct_models_passed
      # and passed_iterations are telemetry: a padded one-model record may report 1 model,
      # fewer than MIN_ITER iterations, and even passed=false (review-cli still ANDs min_iter)
      # and must still authorize when distinct_roles_passed meets the role floor.
      # Empty evidence (0 roles) still fails closed, even with a forged passed:true.
      # .error / passed=false (review-cli min_iter AND, identity warnings) must NOT hatch
      # when role coverage is met (PR 87 / #648). One live model occupies every seat.
      ROLES_OK=0
      [ "$QROLES_N" -gt 0 ] && [ "$QROLES_N" -ge "$MIN_ROLES" ] && ROLES_OK=1
      if [ "$QUORUM_ROLES_REQUESTED" = "1" ] \
         && [ "$ROLES_OK" = "1" ] \
         && [ "$QITER" -gt 0 ] \
         && [ "$MODELS_GATE_OK" = "1" ]; then
        if [ "$MIN_MODELS_EXPLICIT" = "1" ]; then
          echo "[ship] AUTHORITY CONFIRMED — review quorum met: ${QITER} iterations across ${QROLES_N} roles and ${QMODELS_N} models for ${TASK_CODE}. Self-merge authorized by the review-quorum gate."
        else
          echo "[ship] AUTHORITY CONFIRMED — review quorum met: ${QITER} iterations across ${QROLES_N} roles for ${TASK_CODE}. Self-merge authorized by the review-quorum gate."
        fi
        _review_quorum_audit_log authorized "$TASK_CODE" "$QITER" "$QMODELS_N" "" "$QROLES_N" "$MIN_ROLES" "$AUDIT_MIN_MODELS"
      else
        MODELS_SUMMARY=""
        [ "$MIN_MODELS_EXPLICIT" = "1" ] && MODELS_SUMMARY=", ${QMODELS_N}/${MIN_MODELS} distinct models${QMODELS:+ (models seen: ${QMODELS})}"
        # A response whose OWN shape carries no role key at all means this review-cli build
        # cannot report role coverage — a version problem, not "reviews genuinely carry no role
        # labels" (a modern build that WAS asked always echoes the key, even as 0). Driven off
        # the payload shape (QUORUM_HAS_ROLE_KEY), not off what ship merely attempted
        # (QUORUM_ROLES_REQUESTED) — a build that silently tolerates an unrecognized --min-roles
        # would otherwise "succeed" on the roles-requesting tier while still omitting the key,
        # which QUORUM_HAS_ROLE_KEY catches and QUORUM_ROLES_REQUESTED alone would miss.
        ROLES_UNAVAILABLE_HINT=""
        if [ "$QUORUM_HAS_ROLE_KEY" != "true" ]; then
          ROLES_UNAVAILABLE_HINT=" — the installed review-cli does not support --min-roles (predates review-cli#246); upgrade it to restore role-based coverage"
        fi
        _review_quorum_refuse_or_hatch "bar NOT met for ${TASK_CODE} — ${QITER}/${MIN_ITER} iterations, ${QROLES_N}/${MIN_ROLES} distinct roles${QROLES:+ (roles seen: ${QROLES})}${MODELS_SUMMARY}${QERR:+ (${QERR})}${ROLES_UNAVAILABLE_HINT}"
      fi
    fi
  fi
fi

# --- external-review gate: refuse a merge with ZERO GitHub-side PR reviews ----------------
# WHY: Guard-B (the review-quorum gate above) verifies review-cli's own automated multi-model
# pass for the PR's task code — it is NOT a signal that anyone (human, Codex, a second agent)
# ever looked at THIS PR on GitHub. The unresolved-threads + review-dwell gates further above only
# prove that IF a review posted comments they're resolved, and that there was TIME for one to
# land — neither proves a review actually happened. All three passed for real on PR #764
# (HYP-1380, hyperide/hyper-saas): it merged via `gh ship` with `gh pr view --json reviews`
# returning `[]`, because Guard-B alone was wrongly treated as sufficient. This gate closes that
# gap directly: it queries GitHub's own review list and refuses outright when it is empty.
#
# ORDERING: deliberately placed here, AFTER every non-interactive preflight (local-branch-sanity,
# version-bump, screenshot, review-quorum) and right before the merge — NOT immediately after
# review-dwell, where it originally landed. Reason (review-cli Codex pass, GH-459 iteration 3,
# P1): the interactive path below can contact Alex live on Telegram. Running that BEFORE the
# cheap deterministic gates meant a zero-review PR with a valid hatch justification could burn a
# real-time approval and still refuse later on a dirty worktree / missing version bump / unmet
# quorum — the audit trail would show `bypass:approved` for a ship that never merged. Placing the
# hatch here (matching where review-quorum's own live-approval hatch already sits) means Alex is
# only interrupted once every non-interactive check has already passed and a merge is imminent.
#
# Residual, accepted (review-cli GLM pass, GH-459 iteration 4, P3): the `--skip-ci` hatch runs
# LATER still — inside the merge branches below (`_skip_ci_hatch_gate`), not here — because it
# only applies on the `--skip-ci` path. A ship run as `gh ship --skip-ci` on a zero-review PR
# with BOTH `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW` and `RIG_HATCH_REQUEST_SHIP_SKIP_CI` set can
# still burn the external-review Telegram approval first and then have --skip-ci's own hatch
# deny afterward — the exact "approved but never merged" shape this relocation otherwise closes,
# just for this one remaining combination. Narrower than the original bug (requires BOTH hatches
# requested simultaneously) and still fails closed (no bad merge happens) — accepted rather than
# further nesting this gate inside both merge branches.
#
# "Qualifying" = at least one entry in `reviews` (any state — APPROVED/COMMENTED/
# CHANGES_REQUESTED/DISMISSED all count, existence is the signal, not the verdict), from ANY
# author, including the PR's own. No self-review exclusion: this repo's PRs are opened under one
# shared GitHub identity regardless of who/what actually did the work (verified against #764 and
# #760 — both authored by the same account that also left a genuine Codex-independent review on
# #760), so filtering by author would wrongly block a real review under that identity. Residual,
# accepted (same threat-model boundary the rest of this script already lives with): a shipper
# who wants to defeat this can leave a trivial self-review — this gate closes the "nobody looked
# at all" case, it is not a content-quality bar.
#
# Deny-by-default, NO self-service flag for a SINGLE PR — an agent-settable override for one
# ship invocation would just recreate the exact failure this gate exists to close. Bypass for one
# PR only via a one-time live Telegram approval: RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW=
# "<justification>" (see external_review_hatch.py, which mirrors skip_ci_hatch.py's hardening).
#
# A committed `SHIP_EXTERNAL_REVIEW=0` line in the repo's `.ship-config` (see "Knobs (file)"
# above) is the ops off-switch for the WHOLE gate (default: enabled) — a durable, repo-level
# policy call, not a one-off. It is an AUDITED, per-repo, reviewed-like-any-other-code
# alternative that a test process never inherits, closing the agent-tools#528 gap (a
# machine-wide `~/.zshenv` export of SHIP_EXTERNAL_REVIEW=0 once broke this script's own
# gate tests because the test SUITE inherited the same override). Env
# SHIP_EXTERNAL_REVIEW=0 / SHIP_EXTERNAL_REVIEW_ENABLED=0 is IGNORED (agent-tools#624): an
# unreviewed ambient export must not waive a merge-safety gate whose entire purpose is
# closing the PR #764 zero-review hole. One-time per-PR bypass remains the live Telegram
# hatch (RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW), same as --skip-ci.
#
# UNLIKE _acceptance_gate/the local-test-cmd knobs, this `.ship-config` key is read from the PR's
# own BASE branch (`origin/<baseRefName>`, fetched fresh — same resolution as
# _empty_rollup_is_ci_outage above), NEVER from `$ROOT`'s HEAD. HEAD is the PR's OWN tip when
# `gh ship` runs from the PR's worktree (the normal case) — reading the off-switch from there
# would let a PR commit `SHIP_EXTERNAL_REVIEW=0` to `.ship-config` in the SAME PR that needs a
# review and disable the very gate that requires one, recreating PR #764 (HYP-1380) INSIDE the
# fix for #764 (review-cli finding, agent-tools#600 review round 1). Reading from the base
# instead means the off-switch itself only ever takes effect once it has already gone through a
# real, reviewed merge to the base branch — matching the ticket's own framing ("a normal,
# reviewed PR"). A `--repo`/foreign-target ship never reads this checkout's file for a DIFFERENT
# repo's PR either (same reasoning _acceptance_gate already applies to its own foreign-repo skip)
# — and with the env path gone, a foreign invoke has no off-switch at all (hatch only).
EXTREV_ENABLED=1
if [ "${_FOREIGN_REPO_INVOKE:-0}" != "1" ]; then
  EXTREV_PR_BASE=$(gh pr view "$PR" --json baseRefName -q '.baseRefName' 2>/dev/null) || EXTREV_PR_BASE=""
  case "$EXTREV_PR_BASE" in
    ''|-*|*[!A-Za-z0-9._/-]*)
      # UNLIKE _empty_rollup_is_ci_outage (which falls back to $DEFAULT_BRANCH on the same
      # failure — safe there because guessing wrong only costs an extra local test run),
      # guessing wrong HERE could read a DIFFERENT branch's off-switch and waive the review
      # requirement for a PR actually targeting some other, non-opted-out branch (review-cli
      # finding, agent-tools#600 round 3: e.g. `main` has the override, a PR targets `production`
      # which does not, but a flaky SECOND `gh pr view` call defaults this lookup back to `main`
      # and merges `production` with zero reviews anyway). So: skip the file lookup entirely and
      # leave the gate ENABLED — matching --known-flake's existing "unreadable base -> refuse,
      # never guess" convention (same file, `_known_flake_confirmed`). Env cannot disable the
      # gate either (agent-tools#624); only the `.ship-config` lookup is skipped.
      echo "[ship] external-review gate: could not read PR #$PR's base branch — skipping the .ship-config off-switch lookup (gate stays enabled)." >&2
      ;;
    *)
      EXTREV_BASE="$EXTREV_PR_BASE"
      # `+` forces the local refs/remotes/origin/<base> to update even on a NON-fast-forward
      # change upstream (e.g. an admin history-rewrites the base to revert a bad
      # SHIP_EXTERNAL_REVIEW=0). Without it, a plain refspec fetch silently no-ops on a non-ff —
      # a stale LOCAL cache of the OLD base tip would then keep reading the already-reverted
      # override forever on a long-lived checkout, defeating the whole point of reading from a
      # revocable, reviewed ref (review-cli finding, round 2). The exit code is checked
      # explicitly (round 3): a FAILED fetch (network/auth blip) must not fall through to reading
      # whatever stale ref happens to already exist locally — only a confirmed-fresh fetch may
      # authorize the off-switch; on failure the gate stays enabled, same as an unresolvable base.
      if git -C "$ROOT" fetch -q origin "+${EXTREV_BASE}:refs/remotes/origin/${EXTREV_BASE}" 2>/dev/null; then
        # Fully-qualified, not the short name `origin/<base>`: gitrevisions resolves a bare
        # `origin/<base>` through refs/tags/origin/<base> BEFORE refs/remotes/origin/<base> — a
        # pushed tag literally named `origin/<base>` would shadow the real remote-tracking ref
        # and let a tag-only write capability plant a fake off-switch (review-cli finding, round
        # 3). The fully-qualified path has no such ambiguity.
        EXTREV_BASE_REF="refs/remotes/origin/${EXTREV_BASE}"
        if git -C "$ROOT" rev-parse --verify --quiet "$EXTREV_BASE_REF" >/dev/null 2>&1; then
          _ship_config_load "$ROOT" "$EXTREV_BASE_REF"
          # Snapshot into a dedicated variable and consume ONLY that from here on — never
          # $SHIP_CFG_EXTERNAL_REVIEW again. _ship_config_load's SHIP_CFG_* globals are shared
          # across every caller (HEAD-scoped for the local-test-cmd/acceptance-gate keys,
          # base-ref-scoped here); this call also clobbers
          # SHIP_CFG_DIR/SHIP_CFG_CMD/SHIP_CFG_ACCEPTANCE_GATE with the BASE branch's values. No
          # current caller reads those between here and the next _ship_config_load(HEAD) call
          # inside _acceptance_gate (which reloads fresh before use, verified round 2), but a
          # future reordering could silently read base-branch data where HEAD data was intended
          # — snapshotting removes that hazard for this value at least.
          EXTREV_CFG_VALUE="$SHIP_CFG_EXTERNAL_REVIEW"
          case "$EXTREV_CFG_VALUE" in
            0|false|no) EXTREV_ENABLED=0 ;;
          esac
        fi
      fi
      ;;
  esac
fi

# One external-review audit line -> SHIP_AUDIT_FILE. Mirrors _skip_ci_audit_log's shape/dry-run
# contract; the Python hatch helper owns the bypass:approved/denied line for a REAL run, this
# shell helper records only what the helper does not (env-absent "refused", fail-closed paths).
_external_review_audit_log() {  # $1=decision $2=reason(optional)
  local decision="$1" reason="${2:-}"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would append external-review audit: decision=${decision}" >&2
    return 0
  fi
  local file="${SHIP_AUDIT_FILE:-$HOME/.config/agent-tools/ship-audit.jsonl}"
  mkdir -p "$(dirname "$file")" 2>/dev/null || return 0
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local esc_pr esc_branch esc_reason
  esc_pr=$(printf '%s' "$PR" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
  esc_branch=$(printf '%s' "${BRANCH:-}" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
  esc_reason=$(printf '%s' "$reason" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
  { if [ -n "$reason" ]; then
      printf '{"ts":"%s","pr":"%s","branch":"%s","gate":"external-review","decision":"%s","override_reason":"%s"}\n' \
        "$ts" "$esc_pr" "$esc_branch" "$decision" "$esc_reason"
    else
      printf '{"ts":"%s","pr":"%s","branch":"%s","gate":"external-review","decision":"%s"}\n' \
        "$ts" "$esc_pr" "$esc_branch" "$decision"
    fi; } >> "$file" 2>/dev/null || true
}

# Ask Alex, live on Telegram, to approve a one-time external-review bypass. Same `python3 -I`
# isolation + fixed-path lib import as the skip-ci hatch. Called ONLY when
# RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW is set.
_external_review_hatch_check() {  # uses $PR $BRANCH $DRY_RUN
  SHIP_HATCH_PR="$PR" \
  SHIP_HATCH_BRANCH="${BRANCH:-}" \
  SHIP_DRY_RUN="$DRY_RUN" \
  python3 -I "$_SHIP_EXTERNAL_REVIEW_HATCH_PY"
}

if [ "$EXTREV_ENABLED" = "0" ]; then
  echo "[ship] external-review gate disabled by the committed .ship-config on origin/${EXTREV_BASE} (SHIP_EXTERNAL_REVIEW=${EXTREV_CFG_VALUE})."
else
  # Guarded like every other `$(gh ...)` in this file (see the dwell/threads gates above) --
  # under `set -euo pipefail`, an UNGUARDED substitution would abort the whole script the
  # instant `gh` exits non-zero (expired auth, rate limit, network blip), skipping the
  # friendly refusal below and the fail-closed audit line entirely (found by review-cli GLM
  # pass, GH-459 iteration 2: F1 -- ship.sh:1734 was the one `$(gh ...)` call in the file
  # without an errexit guard).
  REVIEW_COUNT=$(gh pr view "$PR" --json reviews -q '(.reviews // []) | length' 2>/dev/null) || REVIEW_COUNT=""
  case "$REVIEW_COUNT" in
    ''|*[!0-9]*)
      echo "Refusing: could not query PR #$PR reviews (gh api failed) — refusing to merge rather than fail open." >&2
      _external_review_audit_log "external-review:refused" "gh query failed"
      exit 1 ;;
  esac
  if [ "$REVIEW_COUNT" -gt 0 ]; then
    echo "[ship] external-review gate OK — PR #$PR has $REVIEW_COUNT GitHub review(s)."
  else
    # Auto-detect the SAME confirmed fleet-wide review-cli capacity outage as the review-quorum
    # gate above (_review_capacity_all_exhausted): zero GitHub reviews AND every configured
    # review-cli provider confirmed exhausted right now means NOBODY -- human or bot -- could
    # have reviewed this PR through any currently-available channel, exactly the repeated,
    # already-diagnosed outage case this auto-detection exists to stop re-escalating.
    cap_out=$(_review_capacity_all_exhausted 2>/dev/null) || cap_out="0"
    cap_flag=0; cap_evidence=""
    case "$cap_out" in 1*) cap_flag=1; cap_evidence="${cap_out#1$'\t'}" ;; esac
    if [ "$cap_flag" = "1" ]; then
      echo "[ship] external-review gate — PR #$PR has zero GitHub reviews, but review-cli confirms EVERY configured provider is exhausted right now (${cap_evidence:-see \`review capacity-check --json\`}) — auto-proceeding without a manual hatch prompt. Recorded to \$SHIP_AUDIT_FILE."
      _external_review_audit_log "external-review:auto-proceed:capacity-outage" "$cap_evidence"
    elif [ -n "${RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW+x}" ]; then
      hrc=0; hout=$(_external_review_hatch_check 2>/dev/null) || hrc=$?
      hverdict="${hout%% *}"; hreason=""
      case "$hout" in *" "*) hreason="${hout#* }" ;; esac
      if [ "$hrc" = "0" ] && [ "$hverdict" = "APPROVED" ]; then
        if [ "$DRY_RUN" = "1" ]; then
          echo "[ship] external-review --dry-run: justification present — a REAL run would request live Telegram approval before merging with zero reviews. (${hreason})"
        else
          echo "[ship] external-review: a one-time Telegram hatch escalation was APPROVED by Alex — proceeding with zero GitHub reviews. (${hreason})"
        fi
      else
        { echo "Refusing: external-review hatch escalation was requested but NOT approved: ${hreason:-no approval}."
          echo "  Obtain live approval, or get an actual review posted on PR #$PR before shipping."; } >&2
        # The helper logs its own bypass:denied only when it emitted DENIED during a REAL run. In
        # dry-run it writes nothing (ship prints the would-be line); in every other non-DENIED case
        # (fake/broken/absent interpreter, import-fail, unexpected verdict) it did NOT audit ->
        # record fail-closed here.
        case "$hverdict" in
          DENIED) [ "$DRY_RUN" = "1" ] && _external_review_audit_log "external-review:bypass:denied" "$hreason" ;;
          *) _external_review_audit_log "external-review:bypass:denied" "$hreason" ;;
        esac
        exit 1
      fi
    else
      { echo "Refusing: PR #$PR has ZERO GitHub-side reviews. Guard-B (review-cli's own automated pass) and 0 unresolved review threads do NOT prove a review ever happened — see PR #764 (HYP-1380), which merged this exact way."
        echo "  Get a real review posted (a human, or a review bot like Codex) before shipping."
        echo "  There is NO self-service override. To request a ONE-TIME bypass, set:"
        echo "    RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW=\"<justification>\""
        echo "  which asks Alex live on Telegram and proceeds ONLY on his real-time approval."
        echo "  A SHIP_EXTERNAL_REVIEW=0 .ship-config line already MERGED to the base branch disables this gate entirely (ops off-switch; env SHIP_EXTERNAL_REVIEW=0 is ignored; a .ship-config committed only on THIS PR's own branch does not count)."; } >&2
      _external_review_audit_log "external-review:refused" ""
      exit 1
    fi
  fi
fi

# --- ticket gates: magic-close keywords + pre-merge acceptance (agent-tools#521) ---------
# Both close the same hole from two sides. task-cli's close gates (every criterion checked WITH
# a proof) only run inside `task done` — but GitHub's "Closes/Fixes/Resolves #N" keywords and
# Linear's GitHub integration (the same words before a ticket code, "Fixes HYP-1295") move the
# ticket to Done the INSTANT the PR merges, entirely outside task-cli. 127 of the 435 HYP
# tickets closed since June 2026 are Done with unchecked criteria (HYP-1440, HYP-1347,
# HYP-1295). A merge gate is the only place that can stop that automation, so:
#   1. the MAGIC-CLOSE gate refuses a PR whose title/body carries such a keyword (the fix is to
#      write "Refs <ref>" — links without closing; `--rewrite-magic-close` does it for you);
#   2. the ACCEPTANCE gate asks task-cli (`task gate <code> --json`) whether the ticket is
#      accepted and refuses while criteria are unchecked or checked without a proof.
# Both run under --dry-run (same refusal; the rewrite is only printed), both log one audit line
# to SHIP_AUDIT_FILE, and each has its own ops off-switch (SHIP_MAGIC_CLOSE_GATE=0,
# SHIP_ACCEPTANCE_GATE=0 — the latter also as a committed .ship-config line).

# The ticket-code derivation is SHARED by the acceptance gate (pre-merge) and the task-cli
# notify step (post-merge) — one matcher, defined once, here, before its first caller.
# Task-code derivation reuses _review_quorum_extract_ticket (the same matcher the review-quorum
# gate above uses) but tries MORE sources, each validated independently, falling through to the
# next on rejection: the gate's own $TASK_CODE (if it already ran and found one — avoids a
# redundant `gh pr view` on the common path) → branch → PR TITLE → PR body. The title is a
# deliberate gap-fix over the quorum gate (which only ever tries branch → body): a PR whose
# ticket code lives only in its title (a common shape — "HYP-931: fix the thing") would
# otherwise never be found here even though a human reads it immediately.
#
# EACH candidate — including a reused $TASK_CODE — is validated before being accepted; a
# rejected one falls through to try the NEXT source rather than giving up (review finding — an
# earlier version validated only the FINAL chosen source, so an invalid $TASK_CODE reuse — e.g.
# an explicit REVIEW_TASK_CODE=SME-ROADMAP-NOTE, or the branch matching the matcher's
# descriptive arm — short-circuited past a title/body that DID carry a real ticket id).
#
# The validation is task-cli's OWN id grammar (agent-tools#565), not a loose heuristic. It used
# to be "contains a digit", which is not a shape at all: it admitted things that are not tickets
# and that `task gate`/`task mark-shipped` can only 404 on. Two classes actually reached the
# merge gate on 2026-09-06:
#
#   * review-cli CHECK codes — `rig-cli-341`, `rig-cli-342`, `OC476-OPENCODE-BACKGROUND-TRUTH`
#     — a DIFFERENT entity from a ticket (a recorded review run). Each carries digits, so each
#     passed. `task gate` 404s -> exit 2 -> the acceptance gate logs "could not evaluate" and
#     SKIPS, so PRs 349, 556, 352 and 497 merged with the gate never actually run: it failed
#     OPEN on the very input it was supposed to reject.
#   * the PULL REQUEST's own number, as `GH-<PR>` or `#<PR>`. PR #499 carried its real ticket
#     (#495) in BOTH its branch and its title, but a `GH-499` reuse won the first candidate slot;
#     the gate judged issue #499 — unrelated, zero criteria — and refused. The merge was only
#     unblocked by recording a false post-merge-acceptance opt-out on #499. A PR number is never
#     a ticket id here, so it is rejected explicitly and derivation falls through.
#
# task-cli's grammar (tasklib/cli.py::_route_id_to_project) is exactly three shapes: `#<n>` and
# a bare `<n>` route to GitHub issues; ONE alphanumeric team prefix + `-` + digits (`HYP-931`,
# `PROJ-12`) routes to Linear by that prefix. Anything else cannot be routed at all.
#
# Known, deliberate limit: a prefix shape is indistinguishable from an ordinary hyphenated word
# ending in digits, so a PR title like "Fix UTF-8 decoding" still derives `UTF-8` — it is a
# well-formed id for a team that (almost certainly) does not exist, and task-cli answers exit 2.
# Narrowing that further would need the registered team list, which ship.sh does not have.
_ship_looks_like_a_ticket_id() {  # $1 = candidate code; true (exit 0) iff it has a task-cli id shape
  # Exactly the three shapes, as ONE alternation so the grammar reads off the line: `#<n>` or a
  # bare `<n>` (a GitHub issue), or one letter-led alphanumeric team prefix + a single hyphen +
  # digits only (`HYP-931`, `PROJ-12`). `rig-cli-341` (two hyphens) and
  # `OC476-OPENCODE-BACKGROUND-TRUTH` (non-numeric tail) both fail. `GH-<n>` is a member of the
  # prefix shape like any other; only `_ship_code_names_this_pr` and
  # `_ship_normalize_gh_code_for_task_cli` treat `GH-` as structurally special.
  [[ "$1" =~ ^(#[0-9]+|[0-9]+|[A-Za-z][A-Za-z0-9]*-[0-9]+)$ ]]
}

# True when the candidate names THIS pull request rather than a ticket. Accepts the same three
# spellings the code can arrive in — `#<n>`, a bare `<n>`, and the `GH-<n>` convention
# `_ship_normalize_gh_code_for_task_cli` rewrites — so none of them can gate a PR against itself.
# Rejecting the PR's own number can never discard a legitimate ticket: GitHub issues and pull
# requests share ONE number sequence per repo, and `#<n>`/`<n>` route to this repo's issues.
#
# $PR is ship.sh's REQUIRED first positional argument, validated at startup long before any
# derivation runs, so the digits-only `${PR:-}` guard below is a belt-and-braces assertion of
# that invariant rather than a reachable branch (`${PR:-}`, not `$PR`: the script runs under
# `set -u`, and an unset $PR must take this branch, not abort). It is written to fail OPEN (an
# empty or non-numeric $PR means "not this PR", so the candidate survives) deliberately: an unset $PR would mean ship.sh is being
# driven in a way this function cannot reason about at all, and silently discarding every
# numeric candidate there would turn a broken invocation into a gate that quietly never runs —
# the very failure mode agent-tools#565 is fixing.
_ship_code_names_this_pr() {  # $1 = candidate code; true (exit 0) iff it is this PR's number
  local n="$1"
  case "$n" in
    '#'*) n="${n#\#}" ;;
    [Gg][Hh]-*) n="${n#*-}" ;;
  esac
  case "$n" in '' | *[!0-9]*) return 1 ;; esac
  # Compare NUMERIC values, not strings (review round 2, Codex P2): `#01`/`GH-01` under PR #1
  # passes the grammar above, and a literal `"01" = "1"` string compare would call it "not this
  # PR" — handing the gate the PR itself, the exact #499 hole this guard closes. `10#` forces
  # base 10 so a zero-padded code is never read as octal. Both sides are checked digits-only
  # HERE, not by the startup invariant alone: a non-numeric operand makes `$(( ))` a fatal bash
  # syntax error that would kill ship.sh outright rather than fail this one candidate.
  case "${PR:-}" in '' | *[!0-9]*) return 1 ;; esac
  [ "$((10#$n))" = "$((10#$PR))" ]
}

# One candidate's full admission test, so every source below applies the SAME two rules. A
# NON-EMPTY candidate that fails is reported on stderr with its source: an explicit $TASK_CODE
# the operator typed would otherwise be replaced by a DIFFERENT ticket from the PR body with no
# trace, and the merge gate would judge a ticket nobody named.
_ship_task_code_candidate_ok() {  # $1 = candidate code  $2 = source label (for the rejection note)
  [ -n "$1" ] || return 1
  if ! _ship_looks_like_a_ticket_id "$1"; then
    echo "[ship] task-code: rejected '$1' from $2 — not a task-cli id shape (#<n>, <n>, PREFIX-<n>); trying the next source." >&2
    return 1
  fi
  if _ship_code_names_this_pr "$1"; then
    echo "[ship] task-code: rejected '$1' from $2 — it names this pull request (#$PR), not a ticket; trying the next source." >&2
    return 1
  fi
  return 0
}
_ship_derive_task_code_for_notify() {
  local candidate

  candidate="${TASK_CODE:-}"
  _ship_task_code_candidate_ok "$candidate" '$TASK_CODE' && { printf '%s' "$candidate"; return 0; }

  candidate=$(_review_quorum_extract_ticket "$BRANCH")
  _ship_task_code_candidate_ok "$candidate" "the branch name" && { printf '%s' "$candidate"; return 0; }

  local pr_title pr_body_local
  # One combined query for title+body (2 round trips instead of 2 separate `gh pr view` calls).
  #
  # IFS=$'\t' (review finding): a bare `read -r a b` splits on DEFAULT IFS (space/tab/newline),
  # not just the `@tsv` separator — a real title/body is virtually never a single word, so the
  # default-IFS form silently truncated `pr_title` to its first word and dumped the rest into
  # `pr_body_local`. Concretely: "Fix the thing (HYP-931)" read as `pr_title="Fix"` (no match)
  # instead of finding HYP-931 — the exact wrong-ticket hazard the digit check above exists to
  # catch, reachable because the RIGHT code got mis-parsed away before the check ever saw it.
  # jq's `@tsv` escapes any literal tab in a value, so splitting on tab alone is the correct,
  # unambiguous delimiter (title/body may freely contain spaces and newlines — the `gsub` above
  # only strips newlines so a MULTI-LINE body still round-trips as ONE tsv line).
  IFS=$'\t' read -r pr_title pr_body_local < <(gh pr view "$PR" --json title,body \
    -q '[(.title // ""), (.body // "")] | map(gsub("\n";" ")) | @tsv' 2>/dev/null) \
    || { pr_title=""; pr_body_local=""; }

  candidate=$(_review_quorum_extract_ticket "$pr_title")
  _ship_task_code_candidate_ok "$candidate" "the PR title" && { printf '%s' "$candidate"; return 0; }

  candidate=$(_review_quorum_extract_ticket "$pr_body_local")
  _ship_task_code_candidate_ok "$candidate" "the PR body" && { printf '%s' "$candidate"; return 0; }

  return 0
}

# One audit line for either ticket gate -> SHIP_AUDIT_FILE, mirroring _review_quorum_audit_log's
# dry-run contract and jq-less JSON-safety. $1=gate (acceptance|magic-close) $2=decision
# $3=task_code (may be empty) $4=detail (free text; may be empty).
_ticket_gate_audit_log() {
  local gate="$1" decision="$2" code="${3:-}" detail="${4:-}"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would append ${gate} audit: decision=${decision}${code:+ task=${code}}${detail:+ detail=${detail}}" >&2
    return 0
  fi
  local file="${SHIP_AUDIT_FILE:-$HOME/.config/agent-tools/ship-audit.jsonl}"
  mkdir -p "$(dirname "$file")" 2>/dev/null || return 0
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg ts "$ts" --arg pr "$PR" --arg gate "$gate" --arg dec "$decision" \
      --arg code "$code" --arg detail "$detail" \
      '{ts:$ts, pr:$pr, gate:$gate, decision:$dec} +
       (if $code == "" then {} else {task_code:$code} end) +
       (if $detail == "" then {} else {detail:$detail} end)' \
      >> "$file" 2>/dev/null || true
  else
    local esc_pr esc_code esc_detail
    esc_pr=$(printf '%s' "$PR" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    esc_code=$(printf '%s' "$code" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    esc_detail=$(printf '%s' "$detail" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '{"ts":"%s","pr":"%s","gate":"%s","decision":"%s"%s%s}\n' \
      "$ts" "$esc_pr" "$gate" "$decision" \
      "${esc_code:+,\"task_code\":\"${esc_code}\"}" "${esc_detail:+,\"detail\":\"${esc_detail}\"}" \
      >> "$file" 2>/dev/null || true
  fi
}

# --- magic-close keyword gate ---------------------------------------------------------
# The keyword ERE is spelled with per-letter case classes rather than a case-insensitive flag:
# macOS `sed -E` has no /I and BSD grep's -i cannot be shared with sed, and the SAME expression
# drives both the match (grep) and the rewrite (sed) so the two can never disagree about what a
# "magic-close phrase" is. Matches GitHub's full keyword list (close/closes/closed, fix/fixes/
# fixed, resolve/resolves/resolved) plus Linear's -ing forms, whitespace, then an issue
# reference (#N, owner/repo#N, or a full https://…/issues|pull/N URL — GitHub documents the
# full-URL form as an equally valid close target) or a ticket code (two+ letters, hyphen,
# digits — HYP-1295; Linear matches these case-insensitively too). A leading non-word char /
# line start keeps "prefixes #1" from matching; no colon is allowed between keyword and
# reference, so a conventional-commit title "fix: HYP-1295 …" (which neither GitHub nor Linear
# treats as a close) passes. Known false positive: a keyword before an acronym-with-number
# ("fixed UTF-8 decoding") — the refusal prints the exact phrase, so one word change clears it.
_MAGIC_CLOSE_KW='([Cc][Ll][Oo][Ss]([Ee]|[Ee][Ss]|[Ee][Dd]|[Ii][Nn][Gg])|[Ff][Ii][Xx]([Ee][Ss]|[Ee][Dd]|[Ii][Nn][Gg])?|[Rr][Ee][Ss][Oo][Ll][Vv]([Ee]|[Ee][Ss]|[Ee][Dd]|[Ii][Nn][Gg]))'
_MAGIC_CLOSE_REF='(([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+|[A-Za-z][A-Za-z]+-[0-9]+|https?://[A-Za-z0-9_./-]+/(issues|pull)/[0-9]+)'
# sed group map: \1 = leading char (or line start), \2..\5 = the keyword, \6 = the whitespace,
# \7..\9 = the reference (\9 only populated for the URL form). The rewrite keeps \1, \6 and \7
# and drops only the keyword.
_MAGIC_CLOSE_RE="(^|[^[:alnum:]_])${_MAGIC_CLOSE_KW}([[:space:]]+)${_MAGIC_CLOSE_REF}"

_magic_close_matches() {  # $1 = text -> one "keyword ref" phrase per line; ALWAYS exits 0
  # LC_ALL=C on BOTH grep and sed (review finding: an earlier version left the trailing sed in
  # the AMBIENT locale) — under a non-UTF-8-aware ambient locale, a multi-byte leading boundary
  # char (e.g. an em dash glued directly to the keyword, "Summary—Fixes #12") could otherwise
  # be stripped byte-by-byte inconsistently between the two tools. Detection itself never
  # depended on this (grep already ran under LC_ALL=C), so this is a display-consistency fix,
  # not a fail-open one — but pinning both sides to the SAME locale is the only way the header
  # comment's "grep and sed can never disagree" claim is actually true.
  # Whitespace-normalized (every run of whitespace, INCLUDING a newline, collapsed to one
  # space) BEFORE matching (review finding, round 4, GLM): GitHub's own close-keyword parser
  # is whitespace-tolerant across a linebreak — the review-quorum ticket-code derivation
  # already normalizes newlines to spaces for the identical reason (`gsub("\n";" ")`) — but
  # grep/sed are line-based, so an unnormalized scan would silently miss "Fixes\n#115" split
  # across two lines (a real shape: the commit scan synthesizes exactly `headline + "\n" +
  # body`). A phrase caught ONLY this way (not by a plain per-line scan) can't be safely fixed
  # by the line-based `_magic_close_rewrite` sed — `_magic_close_rewrite_pr` verifies its OWN
  # rewrite result and refuses rather than trust a rewrite that may have missed a cross-line
  # phrase.
  printf '%s' "$1" | LC_ALL=C tr '\n' ' ' | LC_ALL=C grep -oE "$_MAGIC_CLOSE_RE" | LC_ALL=C sed -E 's/^[^[:alnum:]_]//' || true
}
_magic_close_rewrite() {  # $1 = text -> stdout: the text with every keyword replaced by "Refs"
  # `~` as the s-command delimiter: the reference arm contains `/` (owner/repo#N).
  printf '%s\n' "$1" | LC_ALL=C sed -E "s~${_MAGIC_CLOSE_RE}~\\1Refs\\6\\7~g"
}
_magic_close_gate() {
  local title body commits t_hits b_hits c_hits hits tb_hits title_rc=0 body_rc=0 commits_rc=0
  # Track EACH `gh pr view` call's own exit code (review finding, round 2, Fable): the earlier
  # `|| title=""` made a genuine `gh` failure (rate-limited, network blip) indistinguishable
  # from "the title/body is legitimately empty" — a PR whose real body says "Closes HYP-1440"
  # would sail through as "no keyword found" the moment the fetch itself failed, exactly the
  # incident class this gate exists to stop. Fail closed on an unreadable fetch, like the
  # review-quorum gate does on an unreadable store.
  title=$(gh pr view "$PR" --json title -q '.title // ""' 2>/dev/null) || title_rc=$?
  body=$(gh pr view "$PR" --json body -q '.body // ""' 2>/dev/null) || body_rc=$?
  # GitHub also honours a close keyword in a PR's COMMIT messages (review finding, round 2,
  # Fable + Opus): with the default squash merge, GitHub's own squash-message template
  # includes each commit's message in the final commit body unless the repo customizes it, so
  # a keyword buried in one commit can still close the ticket even with a clean title/body.
  commits=$(gh pr view "$PR" --json commits -q '[.commits[] | (.messageHeadline // "") + "\n" + (.messageBody // "")] | join("\n")' 2>/dev/null) || commits_rc=$?
  if [ "$title_rc" != "0" ] || [ "$body_rc" != "0" ] || [ "$commits_rc" != "0" ]; then
    { echo "Refusing: magic-close gate — could not read PR #$PR's title/body/commits from GitHub (gh exit ${title_rc}/${body_rc}/${commits_rc}) — cannot verify it is free of a close keyword."
      echo "  Retry once gh/GitHub is reachable. SHIP_MAGIC_CLOSE_GATE=0 bypasses this gate entirely (ops off-switch) — only if you have verified the text by hand."; } >&2
    _ticket_gate_audit_log magic-close refused "" "gh pr view failed: title_rc=${title_rc} body_rc=${body_rc} commits_rc=${commits_rc}"
    exit 1
  fi
  t_hits=$(_magic_close_matches "$title")
  b_hits=$(_magic_close_matches "$body")
  c_hits=$(_magic_close_matches "$commits")
  if [ -z "$t_hits" ] && [ -z "$b_hits" ] && [ -z "$c_hits" ]; then
    echo "[ship] magic-close gate: no close/fix/resolve keyword targets an issue or ticket in #$PR's title/body/commits."
    return 0
  fi
  hits=$(printf '%s\n%s\n%s\n' "$t_hits" "$b_hits" "$c_hits" | sed '/^$/d' | tr '\n' ';' | sed 's/;$//')
  # `tb_hits` (title+body only, no commits) is what --rewrite-magic-close can actually FIX —
  # kept separate from `hits` (which also includes commit phrases) for the audit `detail` on
  # the rewrite path, so that line never claims a rewrite that didn't happen.
  tb_hits=$(printf '%s\n%s\n' "$t_hits" "$b_hits" | sed '/^$/d' | tr '\n' ';' | sed 's/;$//')
  if [ "$REWRITE_MAGIC_CLOSE" = "1" ]; then
    [ -n "$t_hits" ] || [ -n "$b_hits" ] && _magic_close_rewrite_pr "$title" "$body" "$t_hits" "$b_hits" "$tb_hits"
    if [ -n "$c_hits" ]; then
      # A commit-message hit can NEVER be fixed by --rewrite-magic-close — `gh pr edit` can't
      # touch commit history (and this script never rewrites/rebases a branch) — REGARDLESS of
      # whether title/body ALSO had hits (review finding, round 3, Opus + Fable: an earlier
      # version rewrote title/body and let the merge proceed anyway when a commit hit
      # coexisted, leaving the un-rewritable commit keyword to close the ticket on merge, and
      # logged a false `rewritten` audit line claiming the commit phrase was fixed too — it
      # wasn't). Any title/body rewrite above already landed (harmless, and worth keeping); the
      # merge itself must still be refused.
      { echo "Refusing: magic-close keyword found in a COMMIT message of PR #$PR — --rewrite-magic-close can only edit the PR title/body, never commit history (this script never rewrites or rebases a branch)."
        [ -n "$t_hits" ] || [ -n "$b_hits" ] && echo "  (the title/body keyword(s) were rewritten to Refs; the commit message keyword below was NOT — it still closes the ticket on merge.)"
        printf '%s\n' "$c_hits" | sed 's/^/  in a commit: "/; s/$/"/'
        echo "  Fix by hand: amend the offending commit message(s) locally, force-push the branch yourself (ship never does), then re-run ship — or reword just to drop the keyword (e.g. \"see HYP-1295\" instead of \"Fixes HYP-1295\")."; } >&2
      _ticket_gate_audit_log magic-close refused "" "$hits"
      exit 1
    fi
    _ticket_gate_audit_log magic-close rewritten "" "$tb_hits"
    return 0
  fi
  { echo "Refusing: magic-close keyword in PR #$PR — it would close the ticket the instant the PR merges, behind task-cli's acceptance gates."
    [ -n "$t_hits" ] && printf '%s\n' "$t_hits" | sed 's/^/  in the title: "/; s/$/"/'
    [ -n "$b_hits" ] && printf '%s\n' "$b_hits" | sed 's/^/  in the body:  "/; s/$/"/'
    [ -n "$c_hits" ] && printf '%s\n' "$c_hits" | sed 's/^/  in a commit: "/; s/$/"/'
    echo "  Why: GitHub's Closes/Fixes/Resolves #N and Linear's GitHub integration (the same words before a ticket code) move the ticket to Done on merge, so no criterion is ever checked with a proof."
    echo "  Fix: write \"Refs <ref>\" instead (e.g. \"Refs #115\", \"Refs HYP-1295\") — \`gh pr edit $PR --title/--body\` — then re-run ship;"
    if [ -n "$c_hits" ]; then
      echo "       a COMMIT message keyword has no automatic fix — amend it and force-push the branch yourself, or reword to drop the keyword; --rewrite-magic-close can fix a title/body hit but never a commit one."
    else
      echo "       or re-run with --rewrite-magic-close to have ship rewrite the keyword(s) to Refs via 'gh pr edit' and continue."
    fi
    echo "  SHIP_MAGIC_CLOSE_GATE=0 disables this gate entirely (ops off-switch)."; } >&2
  _ticket_gate_audit_log magic-close refused "" "$hits"
  exit 1
}
_magic_close_rewrite_pr() {  # $1=title $2=body $3=title hits $4=body hits $5=joined title/body hits
  # Does NOT itself audit-log a `rewritten` decision on success (review finding, round 3: this
  # is also called when a commit hit coexists and the overall verdict is still `refused` — the
  # caller logs exactly ONE final decision per gate invocation, not a `rewritten` line
  # immediately followed by a `refused` one for the same ship run). A `gh pr edit` FAILURE is
  # unconditionally terminal either way, so that failure path still audits here.
  local new_title="$1" new_body="$2"
  local -a edit_args=()
  [ -n "$3" ] && new_title="$(_magic_close_rewrite "$1")" && edit_args+=(--title "$new_title")
  [ -n "$4" ] && new_body="$(_magic_close_rewrite "$2")" && edit_args+=(--body "$new_body")
  # Verify the rewrite RESULT — not a post-edit re-fetch (review finding, round 5, Opus + GLM:
  # an earlier version re-fetched from GitHub after the edit to verify it landed, but that
  # re-fetch used the SAME unguarded `|| var=""` pattern round 2 had already fixed elsewhere —
  # a `gh` failure on the RE-fetch silently looked like "no keyword left", the exact fail-open
  # this whole gate exists to prevent; it also mispredicted under `--dry-run`, where nothing
  # was written yet the stale pre-edit text still re-matched). Scanning `new_title`/`new_body`
  # — text already computed locally, no network call — catches a cross-line phrase the
  # line-based rewrite sed couldn't reach in BOTH dry-run and real runs, with nothing to fail
  # open on.
  local residual
  residual=$(printf '%s\n%s\n' "$(_magic_close_matches "$new_title")" "$(_magic_close_matches "$new_body")" | sed '/^$/d' | tr '\n' ';' | sed 's/;$//')
  if [ -n "$residual" ]; then
    { echo "Refusing: magic-close gate — rewriting PR #$PR would still leave a close keyword in the title/body (a phrase the line-based rewrite could not reach — e.g. split across lines)."
      printf '%s\n' "$residual" | sed 's/^/  still present: "/; s/$/"/'
      echo "  Fix by hand: edit the PR title/body directly (write \"Refs <ref>\"), then re-run ship."; } >&2
    _ticket_gate_audit_log magic-close refused "" "rewrite-incomplete: $residual"
    exit 1
  fi
  echo "[ship] magic-close gate: --rewrite-magic-close — rewriting to Refs in #$PR: $5"
  if ! run gh pr edit "$PR" "${edit_args[@]}"; then
    { echo "Refusing: magic-close gate — 'gh pr edit $PR' failed while rewriting the keyword(s) to Refs ($5)."
      echo "  Fix the title/body by hand (write \"Refs <ref>\"), then re-run ship."; } >&2
    _ticket_gate_audit_log magic-close refused "" "rewrite-failed: $5"
    exit 1
  fi
}
MAGIC_CLOSE_ENABLED=1
case "${SHIP_MAGIC_CLOSE_GATE:-1}" in 0|false|no) MAGIC_CLOSE_ENABLED=0 ;; esac
if [ "$MAGIC_CLOSE_ENABLED" = "0" ]; then
  echo "[ship] magic-close gate disabled (SHIP_MAGIC_CLOSE_GATE=${SHIP_MAGIC_CLOSE_GATE})."
else
  _magic_close_gate
fi

# --- pre-merge acceptance gate --------------------------------------------------------
# Asks task-cli for the read-only verdict: `task gate <code> --json` (task-cli#115). Its contract,
# identical on both ends (task-cli README "The pre-merge gate" / ci/ship/README.md): exit 0 =
# accepted (every criterion checked WITH a proof, or a recorded post-merge opt-out, or the gate
# disabled in that repo's task config, or a cancelled ticket); exit 1 = NOT accepted; exit 2 =
# could not evaluate (unknown id, backend error). Exit 0/1 print one JSON object: {id, ok,
# state, gate_enabled, post_merge_acceptance (reason string or null), criteria (total; 0 is a
# refusal), unchecked: [{index, text}], proofless: [{index, text}]} — `index` is the 1-based
# number `task check <id> <n>` takes. The VERDICT is the exit code; the JSON only shapes the
# message and the audit detail, so a jq-less machine still gates correctly.
#
# Skips (logged, never a refusal) when task-cli is absent or the invocation targets a foreign
# --repo — a repo that never adopted task-cli, or a --repo that would read the wrong project.
# No ticket code is a REFUSAL (agent-tools#639): a PR with no ticket has no acceptance
# criteria, so nothing can be proven. The code derivation IS _ship_derive_task_code_for_notify
# (never a second matcher). Exit 2 is a logged skip: an unknown ticket (the digit-check false
# positives that matcher documents — "UTF-8") or a backend outage is not evidence that the
# ticket is unaccepted, and the ops off-switch exists for the case where an operator wants the
# gate silent for other reasons.
# `below_minimum` (task-cli#115 round 2) can be true with BOTH unchecked/proofless EMPTY — a
# ticket below its configured acceptance_min (e.g. one fully-proven criterion, minimum two)
# refuses `task gate` on count alone, not on any specific criterion's proof — so it needs its
# own line here or a refusal would print with nothing under it.
_acceptance_gate_print_gaps() {  # $1 = `task gate --json` stdout -> the open criteria, one per line
  if command -v jq >/dev/null 2>&1 && printf '%s' "$1" | jq -e . >/dev/null 2>&1; then
    printf '%s' "$1" | jq -r '
      (.unchecked[]? | "    [\(.index)] \(.text)  (unchecked)"),
      (.proofless[]? | "    [\(.index)] \(.text)  (checked without a proof)"),
      (if (.criteria // 1) == 0 then "    (the ticket has no acceptance criteria at all — nothing can be accepted)"
       elif (.below_minimum // false) then "    (only \(.criteria) acceptance criteria — below this repo'"'"'s configured minimum, even with every one it has proven)"
       else empty end)'
  else
    printf '    %s\n' "$1"
  fi
}
_acceptance_gate_detail() {  # $1 = `task gate --json` stdout -> one-line audit detail
  if command -v jq >/dev/null 2>&1 && printf '%s' "$1" | jq -e . >/dev/null 2>&1; then
    printf '%s' "$1" | jq -r '"criteria=\(.criteria // 0) below_minimum=\(.below_minimum // false) unchecked=\([.unchecked[]?.index | tostring] | join(",")) proofless=\([.proofless[]?.index | tostring] | join(","))"'
  else
    printf '%s' "$1" | LC_ALL=C tr '\n' ' '
  fi
}
_acceptance_gate_refuse() {  # $1 = task code $2 = `task gate --json` stdout; exits 1
  { echo "Refusing: acceptance gate — ticket ${1} is NOT accepted (task gate ${1} exit 1); merging now would close it behind task-cli with these criteria unproven:"
    _acceptance_gate_print_gaps "$2"
    echo "  Fix: task accept ${1}   (walks each open criterion, asking for a proof), or"
    echo "       task check ${1} <n> [<n> ...] --proof <path> [--proof <path> ...]   (one proof per criterion, in order; --force \"<reason>\" when a proof is impossible)"
    echo "       then \`task gate ${1}\` must exit 0. If acceptance is inherently post-merge (a release publish, a deploy the merge triggers):"
    echo "       task change ${1} --post-merge-acceptance \"<reason>\"   — recorded ON the ticket and in the ship audit log; task done still needs real proofs afterwards."
    echo "  SHIP_ACCEPTANCE_GATE=0 (env, or a committed .ship-config line) disables this gate entirely (ops off-switch)."; } >&2
  _ticket_gate_audit_log acceptance refused "$1" "$(_acceptance_gate_detail "$2")"
  exit 1
}
# Set by `_acceptance_gate` for the post-merge notify step to read (agent-tools#521 follow-up,
# 2026-09-05): tg-cli#301 shipped for real via PR #305 — every criterion was independently
# verified against the merged code — yet #301 sat OPEN for days because closing it needed a
# separate `task done` nobody remembered to run. A gate that only refuses a BAD merge does not
# by itself close that gap; `_ship_notify_task_cli` uses these two globals to also DO the
# close automatically when the criteria were already proven true before the merge, so the
# ticket does not depend on a human remembering the extra step. Genuinely empty/unset ("")
# means "do not attempt an automatic close" — either the gate never ran, refused (unreachable,
# ship would have exited), was skipped/disabled, or passed only via the post-merge opt-out
# (whose entire point is that acceptance is NOT yet true at merge time).
ACCEPTANCE_GATE_CODE=""
ACCEPTANCE_GATE_FULLY_ACCEPTED=""
# Whether an exit-0 `task gate --json` payload is safe to auto-close on (review finding,
# agent-tools#521 round 1, Opus + GLM): exit 0 alone covers FOUR distinct cases — fully
# proven, a recorded post-merge opt-out, `gate_enabled: false` for this ticket, and a
# cancelled ticket — and only the FIRST is actually "nothing left to prove". An earlier
# version treated "no opt-out reason" as sufficient, so a cancelled ticket or a gate-disabled
# repo (both legitimately exit 0 with criteria still unchecked) triggered a doomed `task done`
# attempt, logging a "criteria were fully proven" audit line that was simply false. Requires
# jq: without it, the opt-out/cancelled/disabled distinction can't be read at all, so this
# ALWAYS returns "not safe" — an unverifiable exit-0 is never grounds to auto-close.
_acceptance_gate_fully_accepted() {  # $1 = task gate --json stdout (exit 0) -> "1" or ""
  command -v jq >/dev/null 2>&1 || { printf ''; return 0; }
  # `state != "done"` too (review finding, round 2): task-cli's own contract example shows
  # `task gate` exit 0 with `"state":"done"` is a real, reachable shape — a follow-up PR
  # referencing an ALREADY-DONE ticket (`Refs HYP-931` against a ticket closed by an earlier
  # merge) hits this same path. `task done` on an already-Done ticket refuses (a re-close is
  # rejected, not a silent re-write — see task-cli's own transition validation), so treating
  # it as "safe to auto-close" produced the same false "fully proven" narrative the cancelled
  # case did. `done` and `cancelled` are task-cli's only two terminal states.
  if printf '%s' "$1" | jq -e '
      (.post_merge_acceptance == null) and (.gate_enabled != false) and
      (.state != "cancelled") and (.state != "done")
    ' >/dev/null 2>&1
  then printf '1'; else printf ''; fi
}
_acceptance_gate_pass() {  # $1 = task code $2 = `task gate --json` STDOUT ONLY (exit 0)
  local reason="" have_jq=0
  command -v jq >/dev/null 2>&1 && have_jq=1
  if [ "$have_jq" = "1" ]; then
    reason=$(printf '%s' "$2" | jq -r '.post_merge_acceptance // ""' 2>/dev/null) || reason=""
  fi
  ACCEPTANCE_GATE_CODE="$1"
  if [ -n "$reason" ]; then
    echo "[ship] acceptance gate: ${1} passes on a recorded post-merge-acceptance opt-out — reason: ${reason}"
    echo "[ship]   the criteria are still owed after the merge: task accept ${1}, then task done ${1}."
    _ticket_gate_audit_log acceptance "authorized:post-merge-opt-out" "$1" "$reason"
    # Deliberately NOT setting ACCEPTANCE_GATE_FULLY_ACCEPTED: the opt-out exists exactly
    # because acceptance is NOT yet true — an automatic `task done` here would just fail (or
    # worse, close a ticket whose criteria are genuinely still open).
    return 0
  fi
  echo "[ship] acceptance gate: ${1} accepted — task gate ${1} exit 0 ($(_acceptance_gate_detail "$2"))."
  if [ "$have_jq" = "0" ]; then
    _ticket_gate_audit_log acceptance authorized "$1" "no jq on PATH — opt-out/cancelled/disabled status unverifiable, skipping auto-close"
    return 0
  fi
  if [ -n "$(_acceptance_gate_fully_accepted "$2")" ]; then
    _ticket_gate_audit_log acceptance authorized "$1" ""
    ACCEPTANCE_GATE_FULLY_ACCEPTED=1
  else
    _ticket_gate_audit_log acceptance authorized "$1" "already done, cancelled, or the acceptance-checked gate is disabled for this ticket — skipping auto-close"
  fi
}
# Rewrites a "GH-<n>" code (case-insensitive: "gh-105" too, matching
# require-ticket-before-commit's `re.IGNORECASE` on the same pattern) into the literal "#<n>" form
# task-cli's own id argument expects (task mark-shipped/done accept "#123" or "HYP-456", never
# "GH-123" -- see the routing rationale on _review_quorum_extract_ticket's own comment, ~line
# 2052). "GH-<n>" is a convention, not a task-cli id: agent-hooks' require-ticket-before-commit
# recognizes it as a valid ticket reference (see
# agent-hooks/require-ticket-before-commit/require_ticket_before_commit.py), and it's also the
# shape review-quorum's own matcher used to SYNTHESIZE from a bare "Fixes #105" before #511
# changed that one arm to return the literal "#105" instead.
#
# #511 only closed that SYNTHESIS path. A "GH-<n>" token typed LITERALLY into a branch name, PR
# title, or PR body is still matched by the generic PREFIX-<n> arm (the same arm that matches this
# repo's own HYP-<n> convention) and derived as-is -- e.g. a branch named "GH-105-fix-crash"
# reaches here as "GH-105". The other way a "GH-<n>" code reaches here is $REVIEW_TASK_CODE set by
# hand (or by another tool) following the convention above -- note this is $REVIEW_TASK_CODE
# specifically, not a bare $TASK_CODE env var: the quorum gate (on by default) unconditionally
# overwrites $TASK_CODE from $REVIEW_TASK_CODE at its own assignment above, so an inherited
# $TASK_CODE only survives untouched as an external input when the gate is disabled
# (SHIP_REVIEW_QUORUM=0) -- which is how this file's tests drive that path directly.
#
# Either way, this normalization is the one place every path funnels through before reaching
# `task`; a rewrite is logged to stderr so a misrouted ticket (e.g. a repo whose task-cli Linear
# team key happens to be literally "GH") is traceable from the ship transcript instead of silent.
# Every other code shape (HYP-<n>, a descriptive all-caps review-cli code, an already-literal
# "#<n>") does not match and passes through byte-for-byte unchanged.
_ship_normalize_gh_code_for_task_cli() {  # $1 = candidate code -> prints the task-cli-safe code
  # The digits are captured once (BASH_REMATCH), not independently re-derived by a second glob —
  # a single source of truth for "what counts as the GH-<n> shape", so widening the regex later
  # (review finding) can't silently desync from a stripping pattern that no longer matches it.
  if [[ "$1" =~ ^[Gg][Hh]-([0-9]+)$ ]]; then
    local rewritten="#${BASH_REMATCH[1]}"
    echo "[ship] normalizing task-cli notify code: ${1} -> ${rewritten} (GitHub-issue convention)" >&2
    printf '%s' "$rewritten"
  else
    printf '%s' "$1"
  fi
}

_acceptance_gate_is_task_cli() {
  # command -v task is not enough (Taskwarrior / go-task also install as `task`).
  # The JSON-shape guard below only runs AFTER a ticket code is derived; the no-ticket
  # refusal must use the same identity check first or it blocks every ship on a machine
  # that merely has a different `task` (Codex P1 on agent-tools#641).
  local help
  help=$(task --help 2>/dev/null) || return 1
  case "$help" in
    *'pre-merge gate'*|*'acceptance criteria'*) return 0 ;;
    *) return 1 ;;
  esac
}
_acceptance_gate() {
  # The ops off-switches only echo, like the other gates' off-switches (no audit line: the
  # env one is a per-invocation operator choice, the .ship-config one is audited by being
  # committed at HEAD).
  case "${SHIP_ACCEPTANCE_GATE:-1}" in
    0|false|no) echo "[ship] acceptance gate disabled (SHIP_ACCEPTANCE_GATE=${SHIP_ACCEPTANCE_GATE})."; return 0 ;;
  esac
  _ship_config_load "$ROOT"
  case "$SHIP_CFG_ACCEPTANCE_GATE" in
    0|false|no) echo "[ship] acceptance gate disabled by the committed .ship-config (SHIP_ACCEPTANCE_GATE=${SHIP_CFG_ACCEPTANCE_GATE})."; return 0 ;;
  esac
  if ! command -v task >/dev/null 2>&1; then
    echo "[ship] acceptance gate: task-cli not on PATH — skipping (install task-cli to gate merges on accepted criteria)." >&2
    _ticket_gate_audit_log acceptance skipped "" "task-cli not on PATH"; return 0
  fi
  if [ "${_FOREIGN_REPO_INVOKE:-0}" = "1" ]; then
    echo "[ship] acceptance gate: --repo targets a foreign remote — skipping (a local 'task' here reads the wrong project)." >&2
    _ticket_gate_audit_log acceptance skipped "" "foreign --repo"; return 0
  fi
  local code; code=$(_ship_derive_task_code_for_notify)
  if [ -z "$code" ]; then
    if ! _acceptance_gate_is_task_cli; then
      echo "[ship] WARNING: acceptance gate could not evaluate — '$(command -v task)' on PATH did not look like task-cli (a DIFFERENT 'task' binary — e.g. Taskwarrior or go-task — may be shadowing task-cli). Skipping." >&2
      _ticket_gate_audit_log acceptance skipped "" "'task' on PATH does not look like task-cli"; return 0
    fi
    { echo "Refusing: acceptance gate — could not derive a task code for #$PR. A PR with no ticket has no acceptance criteria, so nothing can be proven."
      echo "  Fix: task create   (open a ticket with acceptance criteria) and reference it from the branch, title, or body (Refs #N / Refs HYP-N), then task gate <code> must exit 0."
      echo "  SHIP_ACCEPTANCE_GATE=0 (env, or a committed .ship-config line) disables this gate entirely (ops off-switch)."; } >&2
    _ticket_gate_audit_log acceptance refused "" "no task code"
    exit 1
  fi
  # Same rewrite the post-merge notify step applies: a "GH-<n>" derived code (or
  # $REVIEW_TASK_CODE set that way) is task-cli's convention, not its own id argument — it
  # expects the literal "#<n>" form. Without this, the acceptance gate would ask `task gate`
  # about a code it can't resolve even though mark-shipped, right after, resolves it fine.
  code=$(_ship_normalize_gh_code_for_task_cli "$code")
  echo "[ship] acceptance gate: asking task-cli — task gate ${code} --json ..."
  local out rc=0 errfile
  # Capture stdout and stderr SEPARATELY (review finding, agent-tools#521 round 1, Opus + GLM:
  # an earlier `2>&1` merged them, so ANY stderr line task-cli emits — a warning, a deprecation
  # notice — corrupted the JSON `_acceptance_gate_pass`/`_acceptance_gate_refuse` parse with jq;
  # the failure was silent (jq just returned empty) and mis-set the post-merge-opt-out /
  # fully-accepted verdict). `errfile` is best-effort (falls back to /dev/null, losing only the
  # diagnostic text, never the JSON stdout `$out` needs to stay pure).
  errfile=$(mktemp 2>/dev/null) || errfile=/dev/null
  # From "$ROOT", not via -C: task-cli's -C is a per-subcommand flag (see the notify step).
  out=$(cd "$ROOT" && task gate "$code" --json 2>"$errfile") || rc=$?
  if [ "$errfile" != "/dev/null" ] && [ -s "$errfile" ]; then
    echo "[ship] acceptance gate: task-cli stderr: $(LC_ALL=C tr '\n' ' ' < "$errfile")" >&2
  fi
  [ "$errfile" != "/dev/null" ] && rm -f "$errfile"  # always, not only when -s (review finding: the file leaked on the common empty-stderr path)
  # `task` is a famously ambiguous command name (Taskwarrior, go-task also install as `task`)
  # — review finding, round 2 (Opus): `command -v task` alone can resolve to one of those on a
  # developer's machine. Trust NEITHER exit 0 NOR exit 1 as a genuine task-cli result unless
  # `$out` has task-cli's OWN JSON shape — an earlier version only guarded the exit-1 arm,
  # leaving a foreign `task` that happens to exit 0 on garbage (its own "gate" subcommand
  # succeeding by coincidence, or simply printing nothing) to fail OPEN: `_acceptance_gate_pass`
  # would parse `""` as "no opt-out reason" and audit a false `authorized`, letting the merge
  # proceed with NOTHING actually verified (review finding, round 3, Opus).
  if [ "$rc" = "0" ] || [ "$rc" = "1" ]; then
    if ! _acceptance_gate_looks_like_task_cli_json "$out"; then
      echo "[ship] WARNING: acceptance gate could not evaluate ${code} — '$(command -v task)' on PATH did not return task-cli's expected JSON shape (a DIFFERENT 'task' binary — e.g. Taskwarrior or go-task — may be shadowing task-cli). Skipping: $(printf '%s' "$out" | LC_ALL=C tr '\n' ' ')" >&2
      _ticket_gate_audit_log acceptance skipped "$code" "'task' on PATH does not look like task-cli"
      return 0
    fi
  fi
  case "$rc" in
    0) _acceptance_gate_pass "$code" "$out" ;;
    1) _acceptance_gate_refuse "$code" "$out" ;;
    *)
      echo "[ship] WARNING: acceptance gate could not evaluate ${code} (task gate exit ${rc}) — skipping: $(printf '%s' "$out" | LC_ALL=C tr '\n' ' ')" >&2
      _ticket_gate_audit_log acceptance skipped "$code" "could not evaluate: exit ${rc}" ;;
  esac
}
# Whether `$1` (task gate's stdout) looks like task-cli's OWN JSON contract, not just any
# non-empty text — the discriminator for the PATH-collision guard above. With jq, requires the
# three fields every `task gate --json` payload carries; without it, a cheap substring
# heuristic (task-cli's payload always contains the literal `"criteria"` key).
_acceptance_gate_looks_like_task_cli_json() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$1" | jq -e 'has("id") and has("ok") and has("criteria")' >/dev/null 2>&1
  else
    case "$1" in *'"criteria"'*) return 0 ;; *) return 1 ;; esac
  fi
}
_acceptance_gate

# --- merge -----------------------------------------------------------------------------
if [ "$SKIP_CI" = "1" ]; then
  # Deny-by-default: the --skip-ci admin bypass proceeds ONLY on a one-time live Telegram approval
  # (RIG_HATCH_REQUEST_SHIP_SKIP_CI). Refuses here otherwise — before any merge.
  _skip_ci_hatch_gate
  echo "[ship] --skip-ci: admin-merging PR #${PR} (${BRANCH}) --${MERGE_METHOD} --admin ..."
  run gh pr merge "$PR" "--$MERGE_METHOD" --admin
else
  echo "[ship] preflight clean — merging PR #${PR} (${BRANCH}) --${MERGE_METHOD} ..."
  run gh pr merge "$PR" "--$MERGE_METHOD"
fi
# `run gh pr merge` above runs under `set -e` — a merge failure would have already aborted the
# script, so reaching here means the merge (or its --dry-run stand-in) succeeded. ONLY now is it
# true that a known-flake claim was "confirmed" in the sense the audit trail promises (an
# actual merge happened, not merely a passed local check that a LATER gate then refused).
if [ -n "$KF_AUDIT_PENDING" ]; then
  _known_flake_audit_log confirmed "$KF_AUDIT_PENDING"
fi

# --- task-cli notify (best-effort; never blocks or fails the ship) --------------------
# The merge above already succeeded. Tell task-cli about it so the ticket TRACKS the merge
# instead of drifting out of sync (the class of bug behind a real 13-ticket status-divergence
# cleanup) — and so the ticket surfaces its acceptance instructions (what proof is still
# needed) instead of silently going quiet. This is pure notification: `task mark-shipped`
# (task-cli) never closes the ticket itself, only PROPER acceptance does that — see its own
# docstring. A failure anywhere in this step is a WARNING, never a ship failure: the merge is
# already durable, this is best-effort bookkeeping on top of it.
#
# SHIP_TASK_NOTIFY_ENABLED=0 disables the whole step (ops off-switch, same shape as
# SHIP_REVIEW_QUORUM_ENABLED above) — the test harness's shared fixtures (tests/test_ship.py)
# default it off process-wide (review finding: those ~30 pre-existing fixtures never stub a
# fake `task`, so leaving this on-by-default there would invoke whatever REAL task-cli happens
# to be on the developer's PATH, against a fake PR carrying garbage `--pr`/`--commit` values —
# a real, unintended mutation of a real ticket store during a test run).
#
# The task-code derivation (_ship_derive_task_code_for_notify) is defined ABOVE the merge,
# next to the pre-merge acceptance gate that shares it — see "ticket gates".

# Call `task mark-shipped <code> --pr <url> [--commit <sha>]` for the just-merged PR. Skipped
# (with a logged reason, never an error) when: the feature is disabled (SHIP_TASK_NOTIFY_ENABLED=0);
# `task` isn't on PATH (task-cli not installed — ship must work in a repo that never adopted it);
# the invocation targets a foreign --repo (any local `task` here would write into the WRONG
# project, same guard cleanup() already applies to git ops); or no task code can be derived (the
# PR simply isn't tied to a ticket task-cli tracks — not every PR is, e.g. a dependency bump).
_ship_notify_task_cli() {
  case "${SHIP_TASK_NOTIFY_ENABLED:-1}" in
    0|false|no) echo "[ship] task-cli notify disabled (SHIP_TASK_NOTIFY_ENABLED=${SHIP_TASK_NOTIFY_ENABLED})." >&2; return 0 ;;
  esac
  command -v task >/dev/null 2>&1 || { echo "[ship] task-cli not on PATH — skipping ticket notify." >&2; return 0; }
  if [ "${_FOREIGN_REPO_INVOKE:-0}" = "1" ]; then
    echo "[ship] --repo targets a foreign remote — skipping ticket notify (a local 'task' here would write into the wrong project)." >&2
    return 0
  fi
  local code; code=$(_ship_derive_task_code_for_notify)
  code=$(_ship_normalize_gh_code_for_task_cli "$code")
  if [ -z "$code" ]; then
    echo "[ship] could not derive a task code for #$PR — skipping ticket notify (not every PR is tied to a tracked ticket; update it manually if this one is)." >&2
    return 0
  fi
  # One combined query for url+mergeCommit (review finding: 2 round trips instead of 2 separate
  # `gh pr view` calls). IFS=$'\t' for the same reason as the title/body read above — url and
  # a commit SHA never legitimately contain whitespace so default-IFS `read` was not actually
  # broken here, but splitting on the SAME unambiguous delimiter everywhere this pattern is
  # used is worth the one extra token (consistency; review finding).
  local pr_url merge_sha
  IFS=$'\t' read -r pr_url merge_sha < <(gh pr view "$PR" --json url,mergeCommit \
    -q '[(.url // ""), (.mergeCommit.oid // "")] | @tsv' 2>/dev/null) || { pr_url=""; merge_sha=""; }
  if [ -z "$pr_url" ]; then
    echo "[ship] could not resolve PR #$PR's URL — skipping ticket notify." >&2
    return 0
  fi
  echo "[ship] notifying task-cli: ${code} shipped via ${pr_url} ..."
  # Run task-cli FROM "$ROOT" (a subshell `cd`, scoped to this call only) rather than passing
  # `-C "$ROOT"` as an argument: task-cli's `-C`/`--cwd` is a PER-SUBCOMMAND flag and must
  # follow the subcommand name — `task -C <dir> mark-shipped ...` fails with "invalid choice:
  # '<dir>'" (a bug that shipped live and failed silently on its own merge). Running from the
  # target directory sidesteps the ordering question entirely (task-cli's own `-C` default is
  # `.`). `run` is a stateless passthrough (see its definition above) with no parent-shell side
  # effects, so scoping the `cd` around it too is safe.
  local -a mark_args=(mark-shipped "$code" --pr "$pr_url")
  [ -n "$merge_sha" ] && mark_args+=(--commit "$merge_sha")
  if (cd "$ROOT" || { echo "[ship] WARNING: could not cd to $ROOT for task-cli notify." >&2; exit 1; }; run task "${mark_args[@]}"); then
    # Only auto-close AFTER a successful mark-shipped (review finding, round 3, Codex): if
    # mark-shipped itself failed — an older task-cli lacking the subcommand, a transient
    # backend error — the merged PR/commit link never got recorded on the ticket. Closing it
    # anyway via auto-close would recreate exactly the task/repository divergence this whole
    # notify step exists to prevent: a Done ticket with no record of what shipped it.
    _ship_auto_close_accepted_ticket "$code"
  else
    echo "[ship] WARNING: 'task mark-shipped ${code}' failed — the ticket may be out of sync with this merge. Update it manually: task read ${code}" >&2
  fi
}

# Close the "nobody remembered the extra step" gap (agent-tools#521 follow-up, tg-cli#301/#305
# incident): when the pre-merge acceptance gate found EVERY criterion already checked with a
# proof (ACCEPTANCE_GATE_FULLY_ACCEPTED, set only on a genuine pass — never on the post-merge
# opt-out, where acceptance is deliberately not yet true), there is nothing left to verify —
# `task done` is a formality task-cli should be asked to do right here, not left for a human to
# remember days later. Best-effort and NEVER fails the ship: `task done` still enforces every
# OTHER close gate (formatting, links, screenshots, msgref, …), so a genuine failure there is
# expected sometimes and only ever a warning with the manual fallback command.
#
# $1 = the notify step's OWN derived code — only acts when it equals ACCEPTANCE_GATE_CODE (the
# code the pre-merge gate actually verified); a mismatch (e.g. the gate was skipped for #$PR
# but a different code happens to be derivable post-merge) must never auto-close a ticket this
# run never verified.
_ship_auto_close_accepted_ticket() {
  local code="$1"
  [ -n "$ACCEPTANCE_GATE_FULLY_ACCEPTED" ] || return 0
  [ "$code" = "$ACCEPTANCE_GATE_CODE" ] || return 0
  echo "[ship] acceptance gate: ${code} was fully accepted before the merge — closing it now: task done ${code} ..."
  # Same "cd $ROOT, don't pass -C" reasoning as the mark-shipped call above; output flows
  # straight through (not captured) — same style as that call.
  if (cd "$ROOT" || { echo "[ship] WARNING: could not cd to $ROOT to auto-close ${code}." >&2; exit 1; }; run task done "$code"); then
    echo "[ship] ${code} closed — task done succeeded (acceptance was already proven before the merge)."
    _ticket_gate_audit_log acceptance auto-closed "$code" ""
  else
    echo "[ship] WARNING: acceptance criteria were fully proven pre-merge, but 'task done ${code}' still failed (another close gate — formatting/links/screenshots/etc. — is likely blocking it). Close it by hand: task done ${code}" >&2
    _ticket_gate_audit_log acceptance auto-close-failed "$code" ""
  fi
}
if [ "$DRY_RUN" = "1" ]; then
  echo "[ship] [dry-run] would notify task-cli of the merge."
else
  # `_ship_notify_task_cli` already returns 0 on every path (each failure mode is caught and
  # logged internally) — the `|| echo` here is defence-in-depth matching the `cleanup ||`
  # guard below: a best-effort post-merge step must NEVER be able to abort the script and
  # mask an already-successful merge, even if a future edit adds a path that returns non-zero.
  _ship_notify_task_cli || echo "[ship] WARNING: task-cli notify step hit an unexpected error — #$PR IS merged; check/update the ticket manually." >&2
fi

# --- cleanup --------------------------------------------------------------------------
# The merge ABOVE already succeeded. Cleanup (remote branch delete, worktree/branch
# removal, main refresh) is best-effort housekeeping: a failure here must NEVER mask the
# fact that #$PR is merged, nor abort the script with a non-zero exit (the exit-128 that
# made a clean squash-merge look like a failed ship). So the whole cleanup runs in a
# function whose result is REPORTED, not propagated — `set -e` is relaxed inside it and
# every step is allowed to fail with a warning.

# Pull the main checkout with --autostash so a dirty-but-clean-merge WIP doesn't block
# the refresh.  Autostash is a no-op on a clean tree; it only matters when the checkout
# has uncommitted changes that conflict with the incoming pull.  If the stash-pop
# produces unmerged files (UU/AA/DD in the index) we print a clear diagnostic and EXIT 1
# so the broken state doesn't sneak past as a silent "all good".
# Called only when DRY_RUN != 1, always inside cleanup() where set +e is active.
_refresh_main_checkout() {
  local dir="$1" branch="$2" pull_rc unmerged
  git -C "$dir" pull --ff-only --autostash origin "$branch"; pull_rc=$?
  # `git pull --autostash` exits 0 even when the stash-pop conflicts — the conflict is
  # indicated only on stderr.  Check unmerged files unconditionally after every pull so a
  # silent stash-pop conflict is never left undetected.
  unmerged=$(git -C "$dir" ls-files --unmerged 2>/dev/null | wc -l | tr -d ' ')
  if [ "${unmerged:-0}" -gt 0 ]; then
    echo "[ship] PR #$PR IS merged. However, the stash-pop after the main-checkout" \
         "refresh conflicted — $unmerged unmerged path(s) in $dir." >&2
    echo "[ship]   Unmerged files:" >&2
    git -C "$dir" ls-files --unmerged 2>/dev/null | awk '{print $4}' | sort -u | \
      sed 's/^/[ship]     /' >&2
    echo "[ship]   Resolve manually:" \
         "git -C $(printf '%q' "$dir") add <files> && git -C $(printf '%q' "$dir") stash drop" >&2
    exit 1
  fi
  if [ "$pull_rc" -ne 0 ]; then
    echo "[ship] WARNING: could not fast-forward $branch — pull manually." >&2
  fi
}

cleanup() {
  set +e  # local to this function's subshell-free body: warn, don't abort
  if [ "${_FOREIGN_REPO_INVOKE:-0}" = "1" ]; then
    # --repo targets a foreign remote: EVERY cleanup action below operates on THIS checkout
    # (origin push --delete, worktree removal, `git branch -D`, main-checkout refresh) — all of
    # it would hit the WRONG repo. A same-named local branch/worktree here belongs to the
    # ambient checkout, not to ${GH_REPO}'s PR, and deleting it would destroy unrelated local
    # state (codex review P1 on #167). So skip ALL cleanup, not just the remote deletion.
    # GitHub auto-deletes the head branch in the target repo if that setting is on, otherwise
    # it's a manual delete.
    echo "[ship] --repo targets ${GH_REPO} (not this checkout's origin${_CWD_ORIGIN_REPO:+ ${_CWD_ORIGIN_REPO}}) — skipping remote branch deletion AND all local branch/worktree cleanup: any local '${BRANCH}' here belongs to this checkout, not ${GH_REPO}. GitHub auto-deletes ${BRANCH} in ${GH_REPO} if auto-delete-head-branch is enabled; otherwise delete it manually."
    return 0
  fi
  if [ "${CROSS_REPO:-false}" = "true" ]; then
    echo "[ship] PR head is a fork — leaving its branch to the fork owner."
  elif git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "[ship] deleting remote branch origin/${BRANCH} ..."
    run git push -q origin --delete "$BRANCH" || echo "[ship] WARNING: could not delete remote branch origin/${BRANCH} — delete it manually." >&2
  fi

  # Remove EACH worktree for this branch. The one the session is running INSIDE can't be
  # removed while it's our cwd — but we don't have to give up: re-root into the main checkout
  # first (this script's cwd becomes the deleted dir until we cd elsewhere), then remove it
  # from there. The preflight already refused a dirty worktree before merge, so at cleanup the
  # trees are normally clean — but as defence-in-depth (a post-merge hook could write into a
  # tree) we re-check EACH worktree and never escalate to --force on a tree that isn't
  # confirmed clean (a dirty/broken tree is left in place rather than destroying unshipped
  # work).
  SELF_REMOVED=""   # set to the main checkout we re-rooted into, iff we ACTUALLY removed our own tree
  for wt in ${WTS[@]+"${WTS[@]}"}; do
    [ -n "$wt" ] || continue
    is_self=0; reroot_target=""
    wt_real=$(cd "$wt" 2>/dev/null && pwd -P || echo "$wt")

    # Confirm-clean once per worktree: clean ONLY if `git status` both succeeds AND reports an
    # empty tree. A status failure (broken/missing gitdir) is NOT treated as clean — so we
    # never --force a tree we couldn't verify. Capture rc with `|| st_rc=$?` (not a trailing
    # `$?`) so this stays correct even if the block is ever moved out of cleanup()'s `set +e`.
    st_rc=0; st=$(git -C "$wt" status --porcelain 2>/dev/null) || st_rc=$?
    is_clean=0; { [ "$st_rc" -eq 0 ] && [ -z "$st" ]; } && is_clean=1

    if [ "$ORIG_PWD" = "$wt_real" ] || [ "${ORIG_PWD#"$wt_real"/}" != "$ORIG_PWD" ]; then
      # We are inside this worktree. A dirty/unverifiable self-tree is left alone (no re-root,
      # no removal) — destroying it would lose unshipped work.
      if [ "$is_clean" != "1" ]; then
        echo "[ship] WARNING: PR worktree $wt_real has uncommitted/unverifiable changes — leaving it in place (refusing to --force-remove unshipped work)." >&2
        continue
      fi
      # Re-root into the main checkout so we can remove it. The target must be a DISTINCT tree,
      # not the worktree itself nor a path UNDER it (a nested checkout) — removing the worktree
      # would otherwise delete our new cwd too.
      main_real=$(cd "$MAIN_CHECKOUT" 2>/dev/null && pwd -P || echo "")
      if [ -z "$main_real" ] || [ "$main_real" = "$wt_real" ] || [ "${main_real#"$wt_real"/}" != "$main_real" ]; then
        echo "[ship] running inside the PR's worktree ($wt_real) and no separate main checkout to re-root into — leaving it in place; remove it manually." >&2
        continue
      fi
      echo "[ship] inside the PR's worktree ($wt_real) — re-rooting into $main_real to remove it ..."
      cd "$main_real" || { echo "[ship] WARNING: could not cd into $main_real — leaving worktree $wt_real in place." >&2; continue; }
      is_self=1; reroot_target="$main_real"
    fi
    echo "[ship] removing worktree $wt ..."
    # Detach HEAD only for a confirmed-clean tree we intend to remove — never mutate (detach)
    # a dirty/unverifiable tree we're about to leave in place.
    [ "$is_clean" = "1" ] && [ "$(git -C "$wt" symbolic-ref --quiet --short HEAD 2>/dev/null || echo)" = "$BRANCH" ] && run git -C "$wt" checkout --detach --quiet
    # Plain `remove` first (git itself refuses a dirty/busy tree, which is safe). Escalate to
    # --force ONLY for a confirmed-clean tree — never force away a dirty/unverifiable one.
    removed=0
    if run git worktree remove "$wt" 2>/dev/null; then removed=1
    elif [ "$is_clean" = "1" ] && run git worktree remove --force "$wt"; then removed=1
    fi
    if [ "$removed" = "1" ]; then
      # Record self-removal ONLY after a confirmed real removal (so the caller-contract NOTE
      # below never fires falsely — not in dry-run, not when removal failed and the dir lives).
      [ "$is_self" = "1" ] && [ "$DRY_RUN" != "1" ] && SELF_REMOVED="$reroot_target"
    else
      echo "[ship] WARNING: could not remove worktree $wt — remove it manually (git worktree remove --force)." >&2
    fi
  done

  # Delete the local branch only once no worktree still has it checked out (git refuses
  # otherwise, and that refusal is fine — the merge is already done).
  if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    run git branch -D "$BRANCH" || echo "[ship] WARNING: could not delete local branch ${BRANCH} (still checked out somewhere?) — delete it manually." >&2
  fi
  run git worktree prune || true

  if [ -d "$MAIN_CHECKOUT" ]; then
    echo "[ship] refreshing main checkout at ${MAIN_CHECKOUT} ..."
    [ "$(git -C "$MAIN_CHECKOUT" symbolic-ref --quiet --short HEAD 2>/dev/null || echo)" = "$DEFAULT_BRANCH" ] || run git -C "$MAIN_CHECKOUT" checkout "$DEFAULT_BRANCH"
    run git -C "$MAIN_CHECKOUT" fetch origin
    if [ "$DRY_RUN" != "1" ]; then
      _refresh_main_checkout "$MAIN_CHECKOUT" "$DEFAULT_BRANCH"
    fi
  fi

  # CALLER CONTRACT: if we removed the worktree the SESSION was launched from, the parent
  # shell's cwd now points at a deleted directory (this script re-rooted, the parent did
  # not). Make that explicit so the caller cd's out before its next git/relative-path
  # command (which would otherwise fail with `getcwd: cannot access parent directories`).
  if [ -n "$SELF_REMOVED" ]; then
    echo "[ship] NOTE: removed the worktree you launched from — your shell's cwd is now gone. cd into $SELF_REMOVED (the main checkout) before your next command." >&2
  fi
  return 0
}

# Report the merge FIRST (it is the durable, already-applied result), then run cleanup
# in a way that can only warn. `|| true` belt-and-suspenders so even an unexpected
# non-zero from cleanup can't flip ship's exit code after a successful merge.
if [ "$DRY_RUN" = "1" ]; then
  echo "[ship] [dry-run] would merge #$PR, then clean up."
else
  echo "[ship] merged #$PR (${BRANCH}) — running best-effort cleanup ..."
fi
cleanup || echo "[ship] WARNING: post-merge cleanup hit an error — #$PR IS merged; finish cleanup manually." >&2

if [ "$DRY_RUN" = "1" ]; then echo "[ship] [dry-run] complete — nothing changed for #$PR."
else echo "[ship] shipped #$PR — merged + cleaned up."; fi

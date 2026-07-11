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
#   • A UI-touching PR has no embedded screenshot (see ci/screenshots/) — unless overridden.
#   • The local branch has unpushed/diverged commits, or its worktree is dirty.
#   • The review-quorum bar (Guard-B of the self-merge-authority program) is not met: the
#     PR's task code has fewer than SHIP_REVIEW_QUORUM_MIN_ITER recorded review-cli iterations
#     across SHIP_REVIEW_QUORUM_MIN_MODELS distinct models. There is NO self-service override —
#     a one-time bypass goes through a live Telegram approval to Alex (see the hatch escalation
#     below), never a reason flag.
#
# All project-specific coupling is OPTIONAL and configured by env/flags — no issue-tracker,
# no path layout, no org is hard-coded.
#
# Requires: gh (authenticated), git. jq strongly recommended (required-checks-only gating).
#
# Usage:
#   ship.sh <PR-number> [--repo <owner/repo>] [--skip-ci] [--dry-run]
#           [--no-screenshot-ok <reason>] [--screenshot <path> [desc]]...
#
# Flags:
#   --repo <owner/repo>    ship a PR that lives in a DIFFERENT repo than the current checkout:
#                          pins every gh call (view/checks/merge) to owner/repo via GH_REPO.
#                          The only way to ship into a non-CWD repo when a `cd && gh ship`
#                          is not possible. Accepts -R / --repo=… / -R=… / -R… too. When the
#                          target is not this checkout's own origin, remote-branch deletion is
#                          skipped (see the cleanup guard) so the wrong remote is never touched.
#   --skip-ci              admin-merge bypassing the green-CI gate (use only when CI is
#                          billing-blocked / stuck — it still runs the other preflights).
#   --dry-run              print what would happen; change nothing.
#   --no-screenshot-ok R   override the UI screenshot requirement with a logged reason R.
#   --no-version-bump-ok R override the version-bump requirement with a logged reason R
#                          (genuine no-release ship: docs-only, pure test/CI, a revert).
#   --no-review-dwell-ok R override the review-dwell window with a logged reason R (a genuine
#                          fast-track: a trivial/urgent merge that doesn't need review latency).
#   --screenshot P [D]     attach a local screenshot P (desc D) via SHIP_IMAGE_UPLOAD_CMD,
#                          then post it as a PR comment. Repeatable.
#
# NOTE: the review-quorum gate (Guard-B) has NO override FLAG. When its bar is not met and you
# genuinely need to proceed, request a one-time bypass by setting the env var
#   RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"
# which asks Alex live on Telegram (via the shared agenttools_hatch_escalation lib) and proceeds
# ONLY on his real-time approval. SHIP_REVIEW_QUORUM=0 disables the whole gate (ops off-switch).
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
#   SHIP_VERSION_FILES     space-separated list of version sources to check (relative to the
#                          repo root). Use path::ATTR for a Python module attribute. Default:
#                          auto-detect static pyproject.toml, dynamic pyproject.toml sources
#                          (VERSION / __init__.py), Poetry, then package.json. Set it for a
#                          non-standard layout (e.g. a nested package).
#   SHIP_REVIEW_DWELL      minimum seconds since the PR's last code push before a merge is
#                          allowed, so asynchronous review (multi-model / CI-AI / human) has
#                          time to FORM its comments. Default 600 (10 min). 0 disables the gate.
#   REVIEW_TASK_CODE       the task/ticket code (e.g. HYP-931) this ship belongs to, for the
#                          review-quorum gate. If unset, ship derives it from a ticket-like
#                          token in the branch name, then the PR body. If none is found, the
#                          gate refuses (fail-closed) with guidance.
#   SHIP_REVIEW_QUORUM_ENABLED / SHIP_REVIEW_QUORUM  set either to 0 to disable the
#                          review-quorum gate entirely (default: enabled).
#   SHIP_REVIEW_QUORUM_MIN_ITER    quorum floor: recorded review-cli iterations (default 3).
#   SHIP_REVIEW_QUORUM_MIN_MODELS  quorum floor: distinct models (default 3).
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
#   SHIP_AUDIT_FILE        path for the review-quorum gate's audit JSONL (default
#                          ~/.config/agent-tools/ship-audit.jsonl). One line is appended per
#                          non-dry-run gated ship (authorized / bypass:approved /
#                          bypass:denied / refused); --dry-run prints the would-be audit.
set -euo pipefail

ORIG_PWD=$(pwd -P)
PR=""; SKIP_CI=0; DRY_RUN=0; NO_SHOT_OK=""; NO_VBUMP_OK=""; NO_DWELL_OK=""; REPO_FLAG=""
SHOT_PATHS=(); SHOT_DESCS=()
USAGE='Usage: ship.sh <PR-number> [--repo <owner/repo>] [--skip-ci] [--dry-run] [--no-screenshot-ok <reason>] [--no-version-bump-ok <reason>] [--no-review-dwell-ok <reason>] [--screenshot <path> [desc]]...'

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

# --- arg parse (PR number is the lone bare arg; --screenshot takes path + optional desc) --
args=("$@"); i=0; n=${#args[@]}
while [ "$i" -lt "$n" ]; do
  a=${args[$i]}
  case "$a" in
    --skip-ci) SKIP_CI=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-screenshot-ok)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--no-screenshot-ok needs a <reason>." >&2; exit 1; }
      NO_SHOT_OK=${args[$i]}; [ -n "$NO_SHOT_OK" ] || { echo "--no-screenshot-ok reason empty." >&2; exit 1; } ;;
    --no-version-bump-ok)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--no-version-bump-ok needs a <reason>." >&2; exit 1; }
      NO_VBUMP_OK=${args[$i]}; [ -n "$NO_VBUMP_OK" ] || { echo "--no-version-bump-ok reason empty." >&2; exit 1; } ;;
    --no-review-dwell-ok)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--no-review-dwell-ok needs a <reason>." >&2; exit 1; }
      NO_DWELL_OK=${args[$i]}; [ -n "$NO_DWELL_OK" ] || { echo "--no-review-dwell-ok reason empty." >&2; exit 1; } ;;
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
DYNAMIC_VERSION_UNRESOLVED="__ship_dynamic_pyproject_version_unresolved__"

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
_FOREIGN_REPO_INVOKE=0
if [ -n "$REPO_FLAG" ] && [ "$REPO_FLAG" != "$_CWD_ORIGIN_REPO" ]; then
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
  local head_sha timing status
  head_sha=$(gh pr view "$PR" --json headRefOid -q '.headRefOid' 2>/dev/null) || head_sha=""
  status=$(_ci_github_status_indicator)
  timing="0"
  [ -n "$head_sha" ] && timing=$(_ci_runs_timing_suspicious "$head_sha")
  echo "[ship] CI-down probe: github_status=$status timing_suspicious=$timing total=$total failed=$failed" >&2
  # Positive detection if either signal confirms structural failure.
  [ "$status" = "degraded" ] || [ "$timing" = "1" ] || return 1
  return 0
}

_local_test_runner() {
  local root="$1" cmd dev_status
  # Allow a test-only override to avoid real test execution in hermetic tests.
  if [ -n "${SHIP_LOCAL_TEST_CMD:-}" ]; then
    echo "[ship] local gate: running test command: $SHIP_LOCAL_TEST_CMD"
    eval "$SHIP_LOCAL_TEST_CMD" 2>&1; return $?
  fi
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
  if [ -f "$root/pyproject.toml" ]; then
    echo "[ship] local gate: running pytest (pyproject.toml detected) ..."
    if command -v uv >/dev/null 2>&1; then
      uv run --with pytest pytest tests/ -q 2>&1; return $?
    else
      python3 -m pytest tests/ -q 2>&1; return $?
    fi
  fi
  if [ -f "$root/package.json" ]; then
    echo "[ship] local gate: running npm test (package.json detected) ..."
    npm test 2>&1; return $?
  fi
  if [ -f "$root/Cargo.toml" ]; then
    echo "[ship] local gate: running cargo test (Cargo.toml detected) ..."
    cargo test 2>&1; return $?
  fi
  echo "[ship] local gate: FAILED — no recognized test runner found (no pyproject.toml/package.json/Cargo.toml)." >&2
  echo "[ship]   CI is down but tests cannot be verified locally — blocking conservatively." >&2
  return 1
}

# Scan the PR diff additions for leftover markers (TODO/FIXME/HACK/XXX).
# Returns 0 if clean, 1 if markers found or diff cannot be read.
_local_leftover_check() {
  local pr="$1" diff_out
  echo "[ship] local gate: scanning PR diff for leftover markers ..."
  diff_out=$(gh pr diff "$pr" 2>/dev/null) || {
    echo "[ship] local gate: FAILED — could not read PR diff for leftover scan." >&2; return 1; }
  local hits
  hits=$(printf '%s\n' "$diff_out" \
    | grep -E '^\+' | grep -vE '^\+\+\+' \
    | grep -E '(TODO|FIXME|HACK|XXX)' || true)
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

# Check unresolved review threads via the simple (non-paginating) gh pr view query.
# Returns 0 if none, 1 if any unresolved or query fails.
_local_review_threads_check() {
  local pr="$1" unresolved
  echo "[ship] local gate: checking review threads ..."
  unresolved=$(gh pr view "$pr" --json reviewThreads \
    --jq '[.reviewThreads[] | select(.isResolved == false)] | length' 2>/dev/null) || {
    echo "[ship] local gate: FAILED — could not check review threads." >&2; return 1; }
  if [ "${unresolved:-0}" -gt 0 ]; then
    echo "[ship] local gate: FAILED — $unresolved unresolved review thread(s)." >&2
    return 1
  fi
  echo "[ship] local gate: review threads OK."
  return 0
}

# Orchestrate all local CI fallback gates. Called when CI infra appears structurally down.
# Returns 0 if ALL gates pass, 1 if any fail (conservative: block unless everything is clean).
run_local_ci_gate() {
  echo "[ship] === Running local CI fallback gates (CI infrastructure appears down) ==="
  local gate_failed=0
  _local_test_runner "$ROOT"       || gate_failed=1
  _local_leftover_check "$PR"      || gate_failed=1
  _local_pr_checklist_check "$PR"  || gate_failed=1
  _local_review_threads_check "$PR" || gate_failed=1
  if [ "$gate_failed" = "0" ]; then
    echo "[ship] === Local CI fallback: ALL gates passed — safe to merge despite CI outage. ==="
    return 0
  fi
  echo "[ship] === Local CI fallback: FAILED — see above; not safe to merge. ===" >&2
  return 1
}

# --- green-CI gate: CI must EXIST + be green; pending CI is WATCHED to completion -------
# Design (CTO): "no CI" is itself a FAILED gate, not a free pass — refuse with guidance to
# set CI up. Existing-but-pending checks are WATCHED (polled to completion, up to
# SHIP_CI_WAIT) so you don't have to babysit; then the merge is gated on the final result of
# ALL checks. (gh has `gh run watch <run-id>` for a single run; we poll the PR-aggregate.)
if [ "$SKIP_CI" = "0" ]; then
  command -v jq >/dev/null 2>&1 || { echo "Refusing: jq is required for the CI gate (install jq — or --skip-ci only if CI is genuinely N/A)." >&2; exit 1; }
  SUCCESS_FILTER='((.conclusion=="SUCCESS" or .conclusion=="SKIPPED" or .conclusion=="NEUTRAL") or .state=="SUCCESS")'
  SETTLED_FILTER='(.status=="COMPLETED" or .state=="SUCCESS" or .state=="FAILURE" or .state=="ERROR")'
  CI_WAIT="${SHIP_CI_WAIT:-900}"; CI_POLL="${SHIP_CI_POLL:-20}"; CI_GRACE="${SHIP_CI_GRACE:-45}"
  START=$(date +%s); DEADLINE=$(( START + CI_WAIT )); GRACE_DEADLINE=$(( START + CI_GRACE ))
  while :; do
    ROLLUP=$(gh pr view "$PR" --json statusCheckRollup -q '.statusCheckRollup' 2>/dev/null || echo '[]')
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
      { echo "Refusing: PR #$PR has NO CI checks (none registered within ${CI_GRACE}s) — set up CI before merging (an ungated merge is not allowed; 'no CI' is a failed gate, not a pass)."
        echo "  Provision CI: enable rig's ci block — \`rig apply\` writes secret-scan / codeql / dependency-review into .github/workflows — or add your own workflows."
        echo "  Override ONLY if CI is genuinely N/A for this repo: --skip-ci."; } >&2
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
      echo "Refusing: PR #$PR has $FAILED check(s) not passing:" >&2
      printf '%s' "$ROLLUP" | jq -r ".[] | select($SUCCESS_FILTER | not) | \"  x \(.name // .context) -> \(.conclusion // .state)\"" >&2 2>/dev/null || true
      echo "  Fix CI, then re-run (or --skip-ci if CI is billing-blocked)." >&2
      exit 1
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
[ "${UNRESOLVED:-0}" = "0" ] || { echo "Refusing: PR #$PR has $UNRESOLVED unresolved review thread(s) — resolve them, then re-run." >&2; exit 1; }

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
# exclusion set below; the version source is auto-located (static/dynamic pyproject,
# Poetry, then package.json) or pinned via SHIP_VERSION_FILES.

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

pyproject_declares_static_version() {  # rc 0 iff [project] has a literal version field
  [ -f "$ROOT/pyproject.toml" ] || return 1
  awk '
    /^\[project\][[:space:]]*$/ { in_project = 1; next }
    /^\[/ { in_project = 0 }
    in_project && /^[[:space:]]*version[[:space:]]*=/ { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$ROOT/pyproject.toml"
}

pyproject_declares_poetry_version() {  # rc 0 iff [tool.poetry] has a literal version field
  [ -f "$ROOT/pyproject.toml" ] || return 1
  awk '
    /^\[tool\.poetry\][[:space:]]*$/ { in_poetry = 1; next }
    /^\[/ { in_poetry = 0 }
    in_poetry && /^[[:space:]]*version[[:space:]]*=/ { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$ROOT/pyproject.toml"
}

pyproject_declares_dynamic_version() {  # rc 0 iff [project] dynamic includes "version"
  [ -f "$ROOT/pyproject.toml" ] || return 1
  awk '
    /^\[project\][[:space:]]*$/ { in_project = 1; in_dynamic = 0; next }
    /^\[/ { in_project = 0; in_dynamic = 0 }
    in_project {
      if ($0 ~ /^[[:space:]]*dynamic[[:space:]]*=/) in_dynamic = 1
      if (in_dynamic && $0 ~ /["\047]version["\047]/) found = 1
      if (in_dynamic && $0 ~ /\]/) in_dynamic = 0
    }
    END { exit(found ? 0 : 1) }
  ' "$ROOT/pyproject.toml"
}

version_file_spec() {  # $1 = path, $2 = optional attr or @plain -> prints an opaque spec
  if [ -n "${2:-}" ]; then
    printf '%s::%s' "$1" "$2"
  else
    printf '%s' "$1"
  fi
}

version_spec_path() {  # $1 = version file spec -> path for display/diff-section lookup
  printf '%s' "${1%%::*}"
}

explicit_version_file_spec() {  # $1 = SHIP_VERSION_FILES entry -> normalized spec
  local entry path f
  entry="$1"
  path=$(version_spec_path "$entry")
  case "$entry" in
    *::*) printf '%s' "$entry"; return 0 ;;
  esac
  case "$path" in
    pyproject.toml|*/pyproject.toml)
      if awk '
        /^\[project\][[:space:]]*$/ { in_project = 1; next }
        /^\[/ { in_project = 0 }
        in_project && /^[[:space:]]*version[[:space:]]*=/ { found = 1 }
        END { exit(found ? 0 : 1) }
      ' "$ROOT/$path"; then
        version_file_spec "$path" "@project"; return 0
      fi
      if awk '
        /^\[project\][[:space:]]*$/ { in_project = 1; in_dynamic = 0; next }
        /^\[/ { in_project = 0; in_dynamic = 0 }
        in_project {
          if ($0 ~ /^[[:space:]]*dynamic[[:space:]]*=/) in_dynamic = 1
          if (in_dynamic && $0 ~ /["\047]version["\047]/) found = 1
          if (in_dynamic && $0 ~ /\]/) in_dynamic = 0
        }
        END { exit(found ? 0 : 1) }
      ' "$ROOT/$path"; then
        if [ "$path" = "pyproject.toml" ] && f=$(locate_dynamic_pyproject_version_file); then
          printf '%s' "$f"; return 0
        fi
        printf '%s' "$DYNAMIC_VERSION_UNRESOLVED"; return 0
      fi
      if awk '
        /^\[tool\.poetry\][[:space:]]*$/ { in_poetry = 1; next }
        /^\[/ { in_poetry = 0 }
        in_poetry && /^[[:space:]]*version[[:space:]]*=/ { found = 1 }
        END { exit(found ? 0 : 1) }
      ' "$ROOT/$path"; then
        version_file_spec "$path" "@poetry"; return 0
      fi
      printf '%s' "$path"; return 0 ;;
    package.json|*/package.json|*.py) printf '%s' "$path"; return 0 ;;
    *) version_file_spec "$path" "@plain"; return 0 ;;
  esac
}

pyproject_package_dir() {  # prints normalized [project] name, e.g. draw-cli -> draw_cli
  [ -f "$ROOT/pyproject.toml" ] || return 1
  local name
  name=$(awk '
    /^\[project\][[:space:]]*$/ { in_project = 1; next }
    /^\[/ { in_project = 0 }
    in_project && /^[[:space:]]*name[[:space:]]*=/ {
      line = $0
      sub(/^[^=]*=[[:space:]]*/, "", line)
      sub(/[[:space:]]*#.*$/, "", line)
      gsub(/["\047[:space:]]/, "", line)
      print line
      exit
    }
  ' "$ROOT/pyproject.toml")
  [ -n "$name" ] || return 1
  printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | tr '.-' '__'
}

dynamic_attr_file_spec() {  # $1 = attr path like pkg._meta.VERSION -> path::VERSION
  local attr_path module attr module_path f
  attr_path="$1"
  case "$attr_path" in *.*) : ;; *) return 1 ;; esac
  module=${attr_path%.*}
  attr=${attr_path##*.}
  [ -n "$module" ] && [ -n "$attr" ] || return 1
  module_path=${module//./\/}
  for f in "$module_path.py" "$module_path/__init__.py" "src/$module_path.py" "src/$module_path/__init__.py"; do
    if [ -f "$ROOT/$f" ]; then
      version_file_spec "$f" "$attr"
      return 0
    fi
  done
  return 1
}

pyproject_setuptools_dynamic_version_source() {  # prints "attr:pkg.__version__" or "file:VERSION"
  [ -f "$ROOT/pyproject.toml" ] || return 1
  awk '
    function print_quoted_sources(prefix, line) {
      while (match(line, /["\047][^"\047]+["\047]/)) {
        print prefix substr(line, RSTART + 1, RLENGTH - 2)
        line = substr(line, RSTART + RLENGTH)
      }
    }
    /^\[tool\.setuptools\.dynamic\][[:space:]]*$/ { in_section = "inline"; next }
    /^\[tool\.setuptools\.dynamic\.version\][[:space:]]*$/ { in_section = "subtable"; next }
    /^\[/ { in_section = "" }
    in_section == "inline" && /^[[:space:]]*version[[:space:]]*=/ {
      line = $0
      if (line ~ /attr[[:space:]]*=/) {
        sub(/^.*attr[[:space:]]*=[[:space:]]*/, "", line)
        if (match(line, /["\047][^"\047]+["\047]/)) {
          print "attr:" substr(line, RSTART + 1, RLENGTH - 2)
          exit
        }
      }
      if (line ~ /file[[:space:]]*=/) {
        sub(/^.*file[[:space:]]*=[[:space:]]*/, "", line)
        print_quoted_sources("file:", line)
        exit
      }
    }
    in_section == "subtable" && /^[[:space:]]*attr[[:space:]]*=/ {
      line = $0
      sub(/^.*attr[[:space:]]*=[[:space:]]*/, "", line)
      if (match(line, /["\047][^"\047]+["\047]/)) {
        print "attr:" substr(line, RSTART + 1, RLENGTH - 2)
        exit
      }
    }
    in_section == "subtable" && /^[[:space:]]*file[[:space:]]*=/ {
      line = $0
      sub(/^.*file[[:space:]]*=[[:space:]]*/, "", line)
      print_quoted_sources("file:", line)
      exit
    }
  ' "$ROOT/pyproject.toml"
}

dynamic_source_file_from_setuptools() {
  local source path attr_path saw=1
  while IFS= read -r source; do
    [ -n "$source" ] || continue
    saw=0
    case "$source" in
      file:*)
        path=${source#file:}
        if [ -f "$ROOT/$path" ]; then
          version_file_spec "$path" "@plain"
          return 0
        fi ;;
      attr:*)
        attr_path=${source#attr:}
        if dynamic_attr_file_spec "$attr_path"; then
          return 0
        fi ;;
    esac
  done < <(pyproject_setuptools_dynamic_version_source || true)

  [ "$saw" -eq 0 ] || return 1
  printf '%s' "$DYNAMIC_VERSION_UNRESOLVED"
  return 0
}

locate_dynamic_pyproject_version_file() {
  local pkg f found="" count=0
  pyproject_declares_dynamic_version || return 1

  if f=$(dynamic_source_file_from_setuptools); then
    printf '%s' "$f"
    return 0
  fi

  if pyproject_declares_poetry_version; then
    version_file_spec 'pyproject.toml' '@poetry'
    return 0
  fi

  if [ -f "$ROOT/VERSION" ]; then
    printf 'VERSION'
    return 0
  fi

  pkg=$(pyproject_package_dir || true)
  if [ -n "$pkg" ]; then
    for f in "$pkg/__init__.py" "src/$pkg/__init__.py"; do
      if [ -f "$ROOT/$f" ] && grep -Eq '^[[:space:]]*__version__[[:space:]]*(:[^=]+)?[[:space:]]*=' "$ROOT/$f"; then
        version_file_spec "$f" "__version__"
        return 0
      fi
    done
  fi

  while IFS= read -r f; do
    if grep -Eq '^[[:space:]]*__version__[[:space:]]*(:[^=]+)?[[:space:]]*=' "$f"; then
      found=$(version_file_spec "${f#"$ROOT"/}" "__version__")
      count=$((count+1))
    fi
  done < <(find "$ROOT" \
    \( -path "$ROOT/.git" -o -path '*/.git' -o -path "$ROOT/.venv" -o -path "$ROOT/venv" -o -path "$ROOT/.tox" \
       -o -path '*/vendor' -o -path '*/.eggs' -o -path '*/site-packages' \
       -o -path '*/build' -o -path '*/dist' -o -path '*/node_modules' \) -prune \
    -o -name __init__.py -type f -print)

  if [ "$count" -eq 1 ]; then
    printf '%s' "$found"
    return 0
  fi
  return 1
}

# Locate the repo's version file: the explicit override, else pyproject.toml (with a
# `[project] version =`), else a dynamic pyproject source (`VERSION` or `__init__.py`), else
# package.json (with a `"version"`). Prints the path, or nothing.
locate_version_file() {
  local f
  if [ -n "${SHIP_VERSION_FILES:-}" ]; then
    for f in $SHIP_VERSION_FILES; do [ -f "$ROOT/$(version_spec_path "$f")" ] && { explicit_version_file_spec "$f"; return 0; }; done
    return 1
  fi
  if pyproject_declares_static_version; then
    version_file_spec 'pyproject.toml' '@project'; return 0
  fi
  if pyproject_declares_dynamic_version; then
    if f=$(locate_dynamic_pyproject_version_file); then
      printf '%s' "$f"; return 0
    fi
    if pyproject_declares_poetry_version; then
      version_file_spec 'pyproject.toml' '@poetry'; return 0
    fi
    printf '%s' "$DYNAMIC_VERSION_UNRESOLVED"; return 0
  fi
  if pyproject_declares_poetry_version; then
    version_file_spec 'pyproject.toml' '@poetry'; return 0
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
toml_diff_version_value() {  # $1 = section text, $2 = project|tool.poetry, $3 = +|-, $4 = file
  printf '%s\n' "$1" | awk -v wanted="$2" -v sign="$3" -v source_file="${4:-}" '
    BEGIN {
      if (source_file != "") {
        while ((getline src < source_file) > 0) {
          if (src ~ /^[[:space:]]*\[/) {
            file_in_target = (src ~ ("^[[:space:]]*\\[" wanted "\\][[:space:]]*$"))
          }
          file_target[++source_nr] = file_in_target
        }
        close(source_file)
      }
      source_unreadable = (source_file != "" && source_nr == 0)
      in_target = source_unreadable ? 0 : 1
    }
    function target_at(line_no) {
      if (source_unreadable) return 0
      if (source_nr > 0 && line_no > 0 && line_no <= source_nr) return file_target[line_no]
      return in_target
    }
    {
      if ($0 ~ /^@@ /) {
        hunk = $0
        sub(/^@@ -/, "", hunk)
        split(hunk, parts, " ")
        old_line = parts[1]
        new_line = parts[2]
        sub(/,.*/, "", old_line)
        sub(/^\+/, "", new_line)
        sub(/,.*/, "", new_line)
        old_line += 0
        new_line += 0
        next
      }
      marker = substr($0, 1, 1)
      line = $0
      if (marker == "+" || marker == "-" || marker == " ") line = substr($0, 2)
      if (line ~ /^[[:space:]]*\[/) {
        in_target = (line ~ ("^[[:space:]]*\\[" wanted "\\][[:space:]]*$"))
      }
      lookup_line = new_line
      if (marker == sign && target_at(lookup_line) && line ~ /^[[:space:]]*version[[:space:]]*=/) {
        if (match(line, /[0-9][0-9A-Za-z.+_-]*/)) {
          print substr(line, RSTART, RLENGTH)
          exit
        }
      }
      if (marker == " ") { old_line++; new_line++ }
      else if (marker == "-") { old_line++ }
      else if (marker == "+") { new_line++ }
    }'
}

version_is_increase() {  # $1 = old, $2 = new
  local old="$1" new="$2"
  [ -n "$new" ] || return 1
  [ "$new" != "$old" ] || return 1
  [ -n "$old" ] || return 0
  awk -v old="$old" -v new="$new" '
    function core(v) { sub(/[^0-9.].*$/, "", v); return v }
    function suffix(v, c) {
      v = substr(v, length(c) + 1)
      sub(/^[^0-9A-Za-z]+/, "", v)
      return v
    }
    function suffix_gt(old_s, new_s,   oa, na, olen, nlen, max, i, o, n, onum, nnum) {
      olen = split(old_s, oa, /[^0-9A-Za-z]+/)
      nlen = split(new_s, na, /[^0-9A-Za-z]+/)
      max = (olen > nlen) ? olen : nlen
      for (i = 1; i <= max; i++) {
        o = oa[i]; n = na[i]
        if (o == n) continue
        if (o == "") return 1
        if (n == "") return 0
        onum = (o ~ /^[0-9]+$/)
        nnum = (n ~ /^[0-9]+$/)
        if (onum && nnum) return ((n + 0) > (o + 0))
        if (onum && !nnum) return 1
        if (!onum && nnum) return 0
        return (n > o)
      }
      return 0
    }
    BEGIN {
      o = core(old); n = core(new)
      if (o !~ /^[0-9]+([.][0-9]+)*$/ || n !~ /^[0-9]+([.][0-9]+)*$/) exit 1
      olen = split(o, oa, "."); nlen = split(n, na, ".")
      max = (olen > nlen) ? olen : nlen
      for (i = 1; i <= max; i++) {
        oi = (oa[i] == "") ? 0 : oa[i] + 0
        ni = (na[i] == "") ? 0 : na[i] + 0
        if (ni > oi) exit 0
        if (ni < oi) exit 1
      }
      old_suffix = suffix(old, o)
      new_suffix = suffix(new, n)
      if (old_suffix != "" && new_suffix == "") exit 0
      if (old_suffix == "" && new_suffix != "") exit 1
      exit(suffix_gt(old_suffix, new_suffix) ? 0 : 1)
    }'
}

version_line_bumped() {  # $1 = version file spec; uses $PR_DIFF (full patch text)
  local spec="$1" file attr="" oldv newv
  file=$(version_spec_path "$spec")
  case "$spec" in *::*) attr=${spec#*::} ;; esac
  # Restrict to that file's section of the unified diff so an unrelated `version` elsewhere
  # can't falsely satisfy the gate. A file's section runs from its `diff --git a/.. b/<file>`
  # header up to the next `diff --git`.
  local section
  section=$(printf '%s\n' "$PR_DIFF" \
    | awk -v f="$file" '
        /^diff --git / { insec = ($0 == "diff --git a/" f " b/" f) ? 1 : 0; next }
        insec { print }
      ')
  # Extract the value from a version-declaration line. Static manifests quote the value;
  # dynamic pyproject versions commonly live in `__version__ = "X"` or a plain VERSION file.
  if [ "${file##*/}" = "pyproject.toml" ] && [ "$attr" = "@project" ]; then
    newv=$(toml_diff_version_value "$section" "project" "+" "$ROOT/$file")
    oldv=$(toml_diff_version_value "$section" "project" "-" "$ROOT/$file")
  elif [ "${file##*/}" = "pyproject.toml" ] && [ "$attr" = "@poetry" ]; then
    newv=$(toml_diff_version_value "$section" "tool[.]poetry" "+" "$ROOT/$file")
    oldv=$(toml_diff_version_value "$section" "tool[.]poetry" "-" "$ROOT/$file")
  elif [ "${file##*/}" = "VERSION" ] || [ "$attr" = "@plain" ]; then
    newv=$(printf '%s\n' "$section" | grep -E '^\+[[:space:]]*v?[0-9][0-9A-Za-z.+_-]*[[:space:]]*$|^\+[[:space:]]*"?[Vv]ersion"?[[:space:]]*[:=][[:space:]]*"?v?[0-9]' \
             | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1)
    oldv=$(printf '%s\n' "$section" | grep -E '^-[[:space:]]*v?[0-9][0-9A-Za-z.+_-]*[[:space:]]*$|^-[[:space:]]*"?[Vv]ersion"?[[:space:]]*[:=][[:space:]]*"?v?[0-9]' \
             | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1)
  elif [ -n "$attr" ]; then
    newv=$(printf '%s\n' "$section" | awk -v attr="$attr" '
      index($0, "+") == 1 {
        line = substr($0, 2)
        sub(/^[[:space:]]*/, "", line)
        if (line ~ ("^" attr "([[:space:]]*:[^=]+)?[[:space:]]*=")) {
          sub("^" attr "([[:space:]]*:[^=]+)?[[:space:]]*=[[:space:]]*", "", line)
          if (match(line, /[0-9][0-9A-Za-z.+_-]*/)) {
            print substr(line, RSTART, RLENGTH)
            exit
          }
        }
      }')
    oldv=$(printf '%s\n' "$section" | awk -v attr="$attr" '
      index($0, "-") == 1 {
        line = substr($0, 2)
        sub(/^[[:space:]]*/, "", line)
        if (line ~ ("^" attr "([[:space:]]*:[^=]+)?[[:space:]]*=")) {
          sub("^" attr "([[:space:]]*:[^=]+)?[[:space:]]*=[[:space:]]*", "", line)
          if (match(line, /[0-9][0-9A-Za-z.+_-]*/)) {
            print substr(line, RSTART, RLENGTH)
            exit
          }
        }
      }')
  else
    newv=$(printf '%s\n' "$section" | grep -E '^\+[[:space:]]*(__version__|version|VERSION)[[:space:]]*=|^\+[[:space:]]*"version"[[:space:]]*:' \
             | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1)
    oldv=$(printf '%s\n' "$section" | grep -E '^-[[:space:]]*(__version__|version|VERSION)[[:space:]]*=|^-[[:space:]]*"version"[[:space:]]*:' \
             | head -1 | grep -oE '[0-9][0-9A-Za-z.+_-]*' | head -1)
  fi
  version_is_increase "$oldv" "$newv"
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
        if [ "$VFILE" = "$DYNAMIC_VERSION_UNRESOLVED" ]; then
          { echo "Refusing: PR #$PR changes shippable source and pyproject.toml declares dynamic = [\"version\"], but ship could not locate the dynamic version source."
            echo "  Supported auto-detection: [tool.setuptools.dynamic] version attr/file, [tool.setuptools.dynamic.version] attr/file, root VERSION, package __init__.py __version__, or [tool.poetry] version."
            echo "  Configure SHIP_VERSION_FILES with the version source path (or path::ATTR for a Python attribute) for a non-standard layout, then re-run ship."; } >&2
          exit 1
        fi
        VFILE_PATH=$(version_spec_path "$VFILE")
        if PR_DIFF=$(gh pr diff "$PR" 2>/dev/null); then
          if ! version_line_bumped "$VFILE"; then
            { echo "Refusing: PR #$PR changes shippable source but the version in $VFILE_PATH is UNCHANGED — this ship is a release, so bump the version (skill: bump-version-on-release)."
              echo "  Semver: patch for a fix, minor for a feature, major for a breaking change. Edit $VFILE_PATH's version field, push, re-run ship."
              echo "  If this is genuinely NOT a release (docs-only / pure test or CI / a revert), override: --no-version-bump-ok <reason> (or SHIP_SKIP_VERSION_BUMP=1)."; } >&2
            exit 1
          fi
          echo "[ship] version-bump gate OK — $VFILE_PATH version is bumped in PR #$PR."
        else
          echo "Refusing: could not read the diff for PR #$PR — cannot evaluate the version-bump gate (override with --no-version-bump-ok <reason> if intended)." >&2
          exit 1
        fi
      else
        echo "[ship] version-bump gate: no version file (pyproject.toml/Poetry/package.json) found at repo root — skipping (set SHIP_VERSION_FILES for a non-standard layout)."
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
upload_png() {
  local png="$1"
  [ -n "${SHIP_IMAGE_UPLOAD_CMD:-}" ] || return 1
  if [ "$DRY_RUN" = "1" ]; then printf 'https://example.invalid/dry-run/%s' "$(basename "$png")"; return 0; fi
  local out
  if printf '%s' "$SHIP_IMAGE_UPLOAD_CMD" | grep -q '{FILE}'; then
    out=$(eval "${SHIP_IMAGE_UPLOAD_CMD//\{FILE\}/$png}" 2>/dev/null)
  else
    out=$(eval "$SHIP_IMAGE_UPLOAD_CMD \"$png\"" 2>/dev/null)
  fi
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
# PR's task code has enough recorded review iterations across enough distinct models (a
# STRUCTURAL check on runs that were dispatched, not on their verdicts — see `review task
# --help`). Fail-CLOSED: a missing `review` CLI, an unreadable store, or no derivable task
# code all refuse rather than merge unverified.
#
# There is NO self-service override. When the bar is not met (or cannot be verified) and the
# agent genuinely needs to proceed, it sets RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>";
# that routes through the shared agenttools_hatch_escalation lib (the SAME lib the
# pin-primary-worktree / block-reset-hard agent-hooks use) to ask Alex live on Telegram, and the
# gate proceeds ONLY on his real-time approval. SHIP_REVIEW_QUORUM=0 disables the whole gate.
#
# Runs independently of --skip-ci, same posture as the review-dwell gate above.

# Prints the first ticket-like token found in $1, or nothing. Tries the repo's own HYP-<n>
# convention first (case-insensitive, normalized to uppercase), then a generic
# UPPERCASE-PREFIX-<n> ticket token (2+ uppercase letters, a hyphen, digits) so other repos'
# conventions (JIRA-style PROJ-123, etc.) are also picked up. The generic pattern is
# deliberately uppercase-only so it doesn't false-match ordinary prose like "utf-8" or "step-2".
_review_quorum_extract_ticket() {  # $1 = text -> prints ticket code, or nothing; ALWAYS exits 0
  local text="$1" m
  m=$(printf '%s\n' "$text" | grep -oiE 'HYP-[0-9]+' | head -1 || true)
  if [ -n "$m" ]; then printf '%s' "$m" | tr '[:lower:]' '[:upper:]'; return 0; fi
  m=$(printf '%s\n' "$text" | grep -oE '[A-Z][A-Z]+-[0-9]+' | head -1 || true)
  printf '%s' "$m"
  return 0
}

# Append one audit line to the review-quorum audit log. Best-effort: a logging failure must
# never block or unblock the ship, so failures here are swallowed (`|| true`).
# $1=decision(authorized|bypass:approved|bypass:denied|refused) $2=task_code $3=iterations
# $4=models $5=reason(optional — the hatch verdict for bypass:* decisions)
_review_quorum_audit_log() {
  local decision="$1" code="$2" iterations="${3:-0}" models="${4:-0}" reason="${5:-}"
  # Honor the --dry-run contract ("print what would happen; change nothing"): a simulated ship
  # must not create or pollute the real audit record. The gate above still evaluates and prints
  # its authorized/refused verdict — only the persistent write is suppressed here.
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would append review-quorum audit: decision=${decision} task=${code} iter=${iterations} models=${models}" >&2
    return 0
  fi
  local file="${SHIP_AUDIT_FILE:-$HOME/.config/agent-tools/ship-audit.jsonl}"
  mkdir -p "$(dirname "$file")" 2>/dev/null || return 0
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg ts "$ts" --arg pr "$PR" --arg code "$code" --argjson it "$iterations" \
      --argjson m "$models" --arg dec "$decision" \
      --arg reason "$reason" \
      '{ts:$ts, pr:$pr, task_code:$code, iterations:$it, models:$m, decision:$dec} +
       (if $reason == "" then {} else {override_reason:$reason} end)' \
      >> "$file" 2>/dev/null || true
  else
    # jq-less fallback so the audit line is never DROPPED just because jq is absent (jq-missing is
    # itself a gate refusal that can reach the hatch, so the fail-closed bypass:denied audit must
    # still land). Emit minimal JSON via printf. The free-text fields (pr, task_code, reason) are
    # each made JSON-safe: newlines/CR/tab -> space and other control chars stripped (so none can
    # inject a forged extra JSONL line), then backslash + double-quote escaped. `pr` is escaped too
    # — it is a bare CLI arg, so a quote/control char in it must not corrupt the line (the jq path
    # is already safe via --arg). iterations/models are validated integers; decision is internal.
    local esc_pr esc_code esc_reason
    esc_pr=$(printf '%s' "$PR" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    esc_code=$(printf '%s' "$code" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    esc_reason=$(printf '%s' "$reason" | LC_ALL=C tr '\n\r\t' '   ' | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
    { if [ -n "$reason" ]; then
        printf '{"ts":"%s","pr":"%s","task_code":"%s","iterations":%s,"models":%s,"decision":"%s","override_reason":"%s"}\n' \
          "$ts" "$esc_pr" "$esc_code" "${iterations:-0}" "${models:-0}" "$decision" "$esc_reason"
      else
        printf '{"ts":"%s","pr":"%s","task_code":"%s","iterations":%s,"models":%s,"decision":"%s"}\n' \
          "$ts" "$esc_pr" "$esc_code" "${iterations:-0}" "${models:-0}" "$decision"
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
  SHIP_DRY_RUN="$DRY_RUN" \
  python3 -I "$_SHIP_HATCH_PY"
}

# Terminal handler for a review-quorum refusal: either the hatch escalation approves a one-time
# bypass (return 0 -> ship proceeds), or the ship is refused (exits 1). $1 is the human one-line
# summary of WHY the bar wasn't met (or couldn't be verified). Uses $TASK_CODE / $QITER /
# $QMODELS_N (set to safe defaults by the caller before any early refusal).
_review_quorum_refuse_or_hatch() {  # $1 = refusal summary
  local summary="$1"
  # Distinguish TRULY-UNSET (no bypass requested) from set-but-empty (an invalid request the lib
  # denies): `${var+x}` is empty only when the var is unset. Unset -> refuse with the how-to;
  # set (even blank) -> route through the helper, which denies blank/bare and asks on a real reason.
  if [ -z "${RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM+x}" ]; then
    { echo "Refusing: review-quorum gate — ${summary}."
      echo "  Run more independent review iterations (e.g. \`review diff --task ${TASK_CODE:-<code>}\`) across distinct models, then re-run ship."
      echo "  There is NO self-service override. To request a ONE-TIME bypass, set:"
      echo "    RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM=\"<justification>\""
      echo "  which asks Alex live on Telegram and proceeds ONLY on his real-time approval."
      echo "  SHIP_REVIEW_QUORUM=0 disables the gate entirely (ops off-switch)."; } >&2
    _review_quorum_audit_log refused "${TASK_CODE:-}" "${QITER:-0}" "${QMODELS_N:-0}" ""
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
    echo "  Obtain live approval, add more independent review iterations, or set SHIP_REVIEW_QUORUM=0 (ops off-switch)."; } >&2
  # The real helper logs bypass:denied itself only when it emitted the DENIED sentinel during a
  # real run. Under --dry-run it deliberately writes nothing, so ask the shared audit helper to
  # print the would-be audit line. In every other non-DENIED case (fake/broken/absent interpreter,
  # import-fail, unexpected verdict), the helper did NOT audit, so record the fail-closed
  # bypass:denied here. Never double-write.
  case "$hverdict" in
    DENIED) [ "$DRY_RUN" = "1" ] && _review_quorum_audit_log "bypass:denied" "${TASK_CODE:-}" "${QITER:-0}" "${QMODELS_N:-0}" "$hreason" ;;
    *) _review_quorum_audit_log "bypass:denied" "${TASK_CODE:-}" "${QITER:-0}" "${QMODELS_N:-0}" "$hreason" ;;
  esac
  exit 1
}

QUORUM_ENABLED=1
case "${SHIP_REVIEW_QUORUM_ENABLED:-1}" in 0|false|no) QUORUM_ENABLED=0 ;; esac
case "${SHIP_REVIEW_QUORUM:-1}" in 0|false|no) QUORUM_ENABLED=0 ;; esac

if [ "$QUORUM_ENABLED" = "0" ]; then
  echo "[ship] review-quorum gate disabled (SHIP_REVIEW_QUORUM_ENABLED/SHIP_REVIEW_QUORUM=0)."
else
  MIN_ITER="${SHIP_REVIEW_QUORUM_MIN_ITER:-3}"
  MIN_MODELS="${SHIP_REVIEW_QUORUM_MIN_MODELS:-3}"
  # Safe defaults so an early refusal (before the review query) still has these for the audit
  # line and the hatch context.
  QITER=0; QMODELS_N=0; QMODELS=""; QERR=""; QPASSED=false

  TASK_CODE="${REVIEW_TASK_CODE:-}"
  [ -n "$TASK_CODE" ] || TASK_CODE=$(_review_quorum_extract_ticket "$BRANCH")
  if [ -z "$TASK_CODE" ]; then
    PR_BODY_QC=$(gh pr view "$PR" --json body -q '.body // ""' 2>/dev/null) || PR_BODY_QC=""
    TASK_CODE=$(_review_quorum_extract_ticket "$PR_BODY_QC")
  fi

  if [ -z "$TASK_CODE" ]; then
    _review_quorum_refuse_or_hatch "could not derive a task code (set \$REVIEW_TASK_CODE, or put the ticket code e.g. HYP-931 in the branch name or PR body)"
  elif ! command -v review >/dev/null 2>&1; then
    _review_quorum_refuse_or_hatch "'review' CLI not found on PATH — cannot verify the bar for ${TASK_CODE} (install review-cli)"
  elif ! command -v jq >/dev/null 2>&1; then
    _review_quorum_refuse_or_hatch "jq not found — cannot evaluate the gate for ${TASK_CODE} (install jq)"
  else
    # Prefer --check (the review-cli rename target); fall back to --quorum-check when running
    # against a review-cli build that hasn't picked up the rename yet. In --json mode review-cli
    # always prints JSON to stdout (pass or fail) — only an unsupported-flag argparse error
    # leaves stdout empty, which is the fallback trigger.
    QUORUM_JSON=$(review task "$TASK_CODE" --check --min-iter "$MIN_ITER" --min-models "$MIN_MODELS" --json 2>/dev/null) || true
    if [ -z "$QUORUM_JSON" ]; then
      QUORUM_JSON=$(review task "$TASK_CODE" --quorum-check --min-iter "$MIN_ITER" --min-models "$MIN_MODELS" --json 2>/dev/null) || true
    fi

    if [ -z "$QUORUM_JSON" ]; then
      _review_quorum_refuse_or_hatch "could not query review-cli (store unreadable / task ${TASK_CODE} unknown)"
    else
      QPASSED=$(printf '%s' "$QUORUM_JSON" | jq -r '.passed // false' 2>/dev/null || echo false)
      QITER=$(printf '%s' "$QUORUM_JSON" | jq -r '.iterations // 0' 2>/dev/null || echo 0)
      QMODELS_N=$(printf '%s' "$QUORUM_JSON" | jq -r '.distinct_models // 0' 2>/dev/null || echo 0)
      QMODELS=$(printf '%s' "$QUORUM_JSON" | jq -r '(.models // []) | join(", ")' 2>/dev/null || echo "")
      QERR=$(printf '%s' "$QUORUM_JSON" | jq -r '.error // empty' 2>/dev/null || echo "")

      if [ "$QPASSED" = "true" ]; then
        echo "[ship] AUTHORITY CONFIRMED — review quorum met: ${QITER} iterations across ${QMODELS_N} models for ${TASK_CODE}. Self-merge authorized by the review-quorum gate."
        _review_quorum_audit_log authorized "$TASK_CODE" "$QITER" "$QMODELS_N" ""
      else
        _review_quorum_refuse_or_hatch "bar NOT met for ${TASK_CODE} — ${QITER}/${MIN_ITER} iterations, ${QMODELS_N}/${MIN_MODELS} distinct models${QMODELS:+ (models seen: ${QMODELS})}${QERR:+ (${QERR})}"
      fi
    fi
  fi
fi

# --- merge -----------------------------------------------------------------------------
if [ "$SKIP_CI" = "1" ]; then
  echo "[ship] --skip-ci: admin-merging PR #${PR} (${BRANCH}) --${MERGE_METHOD} --admin ..."
  run gh pr merge "$PR" "--$MERGE_METHOD" --admin
else
  echo "[ship] preflight clean — merging PR #${PR} (${BRANCH}) --${MERGE_METHOD} ..."
  run gh pr merge "$PR" "--$MERGE_METHOD"
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

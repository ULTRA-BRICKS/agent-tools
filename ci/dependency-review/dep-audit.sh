#!/usr/bin/env bash
# Dependency vulnerability audit — generic fallback for any CI or a repo WITHOUT GitHub's
# Dependency Graph (where actions/dependency-review-action can't run). Auto-detects the
# package manager and runs its native audit, failing on high/critical advisories.
#
# This is the "what's already in the tree" audit. The PR-time "don't let a new bad dep IN"
# gate is workflow.yml (dependency-review-action) — prefer that on public/GHAS repos.
#
# Detects, in order: bun, npm/pnpm/yarn (node), pip-audit (python), cargo-audit (rust),
# govulncheck (go). Runs every ecosystem it finds a manifest for.
#
# Knobs (env):
#   DEP_AUDIT_LEVEL          minimum severity to FAIL on: low|moderate|high|critical (default high).
#   DEP_AUDIT_ALLOW_MISSING  "1" = DON'T fail when a manifest is found but its scanner isn't
#                            installed (fail-OPEN). Default 0 = fail CLOSED: a detected
#                            ecosystem with no usable scanner is a gate failure, not a silent
#                            skip — otherwise "no audit ran" masquerades as "no vulns".
#
# Usage: sh ci/dependency-review/dep-audit.sh [AUDIT_DIR] [BASE_DIR]
#   AUDIT_DIR (optional, default '.'): the tree whose manifests/lockfiles to audit. The
#   tamper-resistant workflow (pull_request_target) passes the PR head's side worktree here
#   so the TRUSTED base copy of this script audits the PR's lockfiles as DATA — the auditors
#   only read the lockfile, they never run the package's install/lifecycle scripts.
#   BASE_DIR (optional): the PR's BASE branch tree, for scoping (task-cli#140). When given, the
#   JS-ecosystem branches (bun/npm/pnpm/yarn — the ones with a `--json` output this script can
#   parse via jq) also audit BASE_DIR and only FAIL for advisories NEW relative to it; an
#   advisory present on the base branch too is printed as a visible informational warning but
#   never fails the check. Without BASE_DIR (the default — old callers, or a push with no PR to
#   diff against) behavior is unchanged: any advisory at/above DEP_AUDIT_LEVEL fails, exactly as
#   before this fix. Python/Rust/Go audits are unaffected by BASE_DIR (still whole-tree only) —
#   see the base-diffing block below for why.
set -eu

LEVEL="${DEP_AUDIT_LEVEL:-high}"
ALLOW_MISSING="${DEP_AUDIT_ALLOW_MISSING:-0}"
AUDIT_DIR="${1:-.}"
BASE_DIR="${2:-}"
rc=0
ran=0
missing=0

# Resolve BASE_DIR to an absolute path BEFORE we `cd "$AUDIT_DIR"` below — a relative BASE_DIR
# would otherwise resolve against the wrong cwd once we've moved into AUDIT_DIR. A BASE_DIR
# that can't be resolved (deleted base branch, bad ref) is simply treated as "no baseline
# available" everywhere below — never a crash.
if [ -n "$BASE_DIR" ]; then
  case "$BASE_DIR" in
    /*) : ;;
    *) BASE_DIR="$(cd "$BASE_DIR" 2>/dev/null && pwd)" || BASE_DIR="" ;;
  esac
fi

# Audit the requested tree (default cwd). Fail closed if it doesn't exist — a vanished
# audit target must not masquerade as "no manifests, nothing to audit".
if [ ! -d "$AUDIT_DIR" ]; then
  echo "[dep-audit] audit dir '$AUDIT_DIR' does not exist — FAILING (cannot audit)." >&2
  exit 1
fi
cd "$AUDIT_DIR"

note() { echo "[dep-audit] $*" >&2; }
# A detected manifest whose scanner is absent: fail closed unless explicitly allowed.
miss() {
  if [ "$ALLOW_MISSING" = "1" ]; then
    note "$* — skipping (DEP_AUDIT_ALLOW_MISSING=1)."
  else
    note "$* — FAILING (no audit performed; set DEP_AUDIT_ALLOW_MISSING=1 to allow)."
    missing=$((missing+1))
  fi
}

# Classify a requirements file $1 for data-safe auditing. Prints exactly one word:
#   pinned — every line is a pinned PyPI spec (`name[extras]==version`) or a benign line (blank /
#            `#` comment / `--hash` / `--require-hashes` / `; marker`), AND at least one real
#            pinned spec is present -> safe to audit as data with `pip-audit --no-deps -r`.
#   empty  — only benign lines, NO pinned spec (empty / comments / options only). NOT a data
#            source: auditing it would check nothing, so the caller must not count it as covering
#            python — an empty/stub requirements.txt must NOT mask an un-auditable pyproject.toml
#            (agent-tools#131).
#   unsafe — at least one DIRECT reference: editable (`-e`), URL/VCS (`://`, `git+…`), a PEP 508
#            `name @ url`, a local path/archive, an `-r`/`-c` include (which could smuggle any of
#            those), a foreign-index option (`--index-url`/`--find-links`), or an UNPINNED/prefix
#            spec. pip-audit must BUILD such input to read its metadata (runs setup.py / a PEP 517
#            backend) = arbitrary PR code under pull_request_target (RCE). `--no-deps` suppresses
#            only TRANSITIVE resolution, not a direct-entry build, so it does NOT close this.
#            Fail closed (the caller's miss()).
req_file_classify() {  # $1 = requirements file -> prints: pinned | empty | unsafe
  awk '
    { line=$0; gsub(/\r/, "", line); sub(/#.*/, "", line)
      gsub(/^[ \t]+|[ \t]+$/, "", line)
      if (line == "") next
      if (line ~ /^--hash([ =])/) next                 # per-spec hash (incl. continuation)
      if (line ~ /^--require-hashes$/) next            # the hardened global option (safe)
      core=line
      sub(/[ \t]*\\$/, "", core)                       # trailing line-continuation
      sub(/[ \t]*;.*/, "", core)                       # environment markers
      sub(/[ \t]+--hash.*/, "", core)                  # inline hashes
      gsub(/^[ \t]+|[ \t]+$/, "", core)
      if (core ~ /^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]*\])?==[A-Za-z0-9][A-Za-z0-9.+!_-]*$/) { pinned=1; next }
      bad=1
    }
    END { if (bad) print "unsafe"; else if (pinned) print "pinned"; else print "empty" }
  ' "$1"
}

# ---- Base-branch baseline diffing (scoping fix, task-cli#140) ----
#
# A raw "audit the whole current tree, fail on anything >= LEVEL" makes every PR in a repo
# with ANY pre-existing high/critical transitive advisory permanently red, regardless of what
# the PR itself changed (a real incident: hyperide/hyper-saas#780, a docs-only PR, blocked on
# 6 pre-existing browserslist/fast-uri advisories it never touched). When BASE_DIR is given we
# run the SAME audit against it and only FAIL for advisories NEW relative to that baseline;
# advisories present in BOTH are printed as a visible informational warning, never blocking.
#
# Scope: this covers bun/npm/pnpm/yarn (the branches that already existed for "other package
# managers") because all four support a `--json` output whose advisory records carry the SAME
# GitHub Advisory Database numeric id space (verified live against a real advisory: id
# 1153171 / GHSA-c83g-rgw3-j3cx identically in bun, npm and pnpm's --json output) and jq can
# parse it regardless of whether the tool pretty-prints or compacts it. Python/Rust/Go are
# NOT extended here — pip-audit/cargo-audit/govulncheck's output isn't verified against this
# id scheme in this pass; they keep the original whole-tree behavior unchanged.
#
# Every path below fails OPEN toward the ORIGINAL behavior (audit the whole current tree, fail
# on anything found) whenever the baseline can't be trusted: no BASE_DIR given, BASE_DIR
# unresolvable, BASE_DIR has no matching lockfile, the ecosystem's --json run errors out with
# nothing parseable, or `jq` isn't installed. None of these crash the script or silently pass.
#
# KNOWN TRADEOFF (review finding, agent-tools#603): each ecosystem now runs its audit command
# up to 3x per CI run (plain + --json for AUDIT_DIR, --json for BASE_DIR) instead of once,
# tripling exposure to a live advisory-registry endpoint's rate limits/timeouts. A transient
# failure on JUST the BASE_DIR --json call (registry blip, not a real problem) degrades to the
# documented "no baseline available, failing on all" path above — i.e. the ORIGINAL
# unscoped behavior, not a new failure mode, but it does mean occasional network flakiness can
# still produce a false failure on a pre-existing (not new) advisory. Collapsing to one
# canonical --json call per tree (deriving the human-readable summary from the parsed JSON
# instead of a separate plain invocation) would remove this and is the natural follow-up if
# registry flakiness turns out to matter in practice; not done here to keep this change's
# blast radius to the scoping logic itself.

# Extract sorted-unique advisory ids at/above $LEVEL from an audit tool's --json output file.
# Matches ANY object anywhere in the document that has an "id" or "source" field (bun/pnpm use
# "id", npm's "via" entries use "source" — both draw the same numeric GitHub Advisory Database
# ids) alongside a "severity" field, filtered by severity rank — because --audit-level/--severity
# only affects the tool's OWN exit code, not which advisories appear in --json (verified live:
# a --audit-level=critical run still lists a high-severity advisory in its JSON).
ADVISORY_ID_JQ_FILTER='
  def sevrank(s): {"low":1,"moderate":2,"high":3,"critical":4}[s] // 0;
  [ .. | objects | select(has("id") or has("source")) | select(has("severity"))
    | select(sevrank(.severity) >= sevrank($lvl))
    | (.id // .source)
  ] | .[]
'

# Run "$@" (an audit invocation, WITHOUT --json) with --json appended, either in the CURRENT
# cwd ($1 = "") or in $1 (cd'd into via a subshell), and write the severity-filtered advisory
# ids to $2, one per line, sorted -u. Returns 0 when the result can be trusted for diffing;
# non-zero for ANY reason it can't be (the tool errored with nothing parseable, `jq` is
# unavailable, or the output didn't parse as JSON) — callers only branch on trustworthy-or-not,
# so a single failure code is deliberate; don't reintroduce a finer-grained one unless a caller
# actually needs to tell those reasons apart.
json_advisory_ids() {  # $1=dir ('' = cwd)  $2=ids_out_file  shift2 -> "$@"=audit cmd (no --json)
  local _dir="$1" _ids_out="$2" _trc _json_tmp
  shift 2
  _json_tmp="$(mktemp)"
  if [ -n "$_dir" ]; then
    if ( cd "$_dir" && "$@" --json ) >"$_json_tmp" 2>/dev/null; then _trc=0; else _trc=$?; fi
  else
    if ( "$@" --json ) >"$_json_tmp" 2>/dev/null; then _trc=0; else _trc=$?; fi
  fi
  : > "$_ids_out"
  if ! command -v jq >/dev/null 2>&1; then
    rm -f "$_json_tmp"
    return 1
  fi
  if ! jq -r --arg lvl "$LEVEL" "$ADVISORY_ID_JQ_FILTER" "$_json_tmp" 2>/dev/null | sort -u > "$_ids_out"; then
    rm -f "$_json_tmp"
    return 1
  fi
  rm -f "$_json_tmp"
  if [ "$_trc" != "0" ] && [ ! -s "$_ids_out" ]; then
    return 1
  fi
  return 0
}

# Does $2 look like it has $1's ecosystem lockfile — for deciding whether BASE_DIR can be
# audited as a baseline at all.
has_lockfile() {  # $1=eco $2=dir
  case "$1" in
    bun)  [ -f "$2/bun.lock" ] || [ -f "$2/bun.lockb" ] ;;
    npm)  [ -f "$2/package-lock.json" ] || [ -f "$2/npm-shrinkwrap.json" ] ;;
    pnpm) [ -f "$2/pnpm-lock.yaml" ] ;;
    yarn) [ -f "$2/yarn.lock" ] ;;
    *) return 1 ;;
  esac
}

# Compare current vs baseline advisory-id sets for one ecosystem and report. Sets the global
# `rc=1` ONLY for advisories NEW relative to the baseline (or for ANY advisory when there is no
# trustworthy baseline — the original, unscoped behavior). $4 is the human-readable reason
# there's no baseline (only used when $3 is empty) — the SOLE place that reason gets printed,
# so callers must not also `note` it themselves (a duplicate, differently-worded message for
# the same condition is confusing in a CI log).
report_advisory_diff() {  # $1=eco $2=cur_ids_file $3=base_ids_file_or_empty $4=no_base_reason
  local _eco="$1" _cur="$2" _base="$3"
  local _reason="${4:-no base-branch baseline to distinguish new vs pre-existing}"
  local _new _pre
  [ -s "$_cur" ] || return 0
  if [ -z "$_base" ]; then
    note "$_eco: FAIL — advisories at $LEVEL+ found ($_reason; failing on all, as before this scoping fix): $(tr '\n' ' ' < "$_cur")"
    rc=1
    return 0
  fi
  _new="$(mktemp)"; _pre="$(mktemp)"
  comm -23 "$_cur" "$_base" > "$_new"
  comm -12 "$_cur" "$_base" > "$_pre"
  if [ -s "$_pre" ]; then
    note "$_eco: pre-existing advisories already on the base branch — informational, NOT failing this check (not introduced by this PR): $(tr '\n' ' ' < "$_pre")"
  fi
  if [ -s "$_new" ]; then
    note "$_eco: FAIL — advisories NEW relative to the base branch (introduced by this PR): $(tr '\n' ' ' < "$_new")"
    rc=1
  fi
  rm -f "$_new" "$_pre"
}

# Run one ecosystem's audit, scoped to what's new relative to BASE_DIR (if any). $1 = the
# ecosystem label (also the has_lockfile() key); the rest is the audit command, WITHOUT --json
# (e.g. `bun audit --audit-level=high`). Always prints the tool's own plain/pretty output first
# (unchanged from before this fix), then does the JSON-based new-vs-baseline diff.
#
# TRUST CHECK (review finding, agent-tools#603): `json_advisory_ids` on its own can be fooled —
# if a future/untested ecosystem's --json schema doesn't match ADVISORY_ID_JQ_FILTER (e.g. a
# different key casing), jq parses fine and returns 0 ids WITHOUT erroring, so json_advisory_ids
# reports "trustworthy" with an empty set. Left unchecked, that would make `report_advisory_diff`
# see zero current ids and silently report a clean pass — even though `_pretty_rc` (the actual
# audit tool's own exit code from the plain, un-parsed invocation) says otherwise. So a
# non-clean `_pretty_rc` with an empty extracted id set is ALWAYS treated as untrustworthy here,
# regardless of what json_advisory_ids itself concluded — `_pretty_rc` is the one signal every
# supported ecosystem's audit command was already relied on for, before this fix existed.
audit_scope_to_base() {
  local _eco="$1"
  local _pretty_rc=0 _cur_ids _base_ids _base_ids_f _no_base_reason
  shift
  "$@" || _pretty_rc=$?

  _cur_ids="$(mktemp)"
  if json_advisory_ids "" "$_cur_ids" "$@" && { [ "$_pretty_rc" = "0" ] || [ -s "$_cur_ids" ]; }; then
    _base_ids=""
    _no_base_reason="no base-branch baseline to distinguish new vs pre-existing"
    if [ -n "$BASE_DIR" ]; then
      if has_lockfile "$_eco" "$BASE_DIR"; then
        _base_ids_f="$(mktemp)"
        if json_advisory_ids "$BASE_DIR" "$_base_ids_f" "$@"; then
          _base_ids="$_base_ids_f"
        else
          _no_base_reason="could not audit the base branch's lockfile for baseline comparison"
          rm -f "$_base_ids_f"
        fi
      else
        _no_base_reason="base branch tree has no matching lockfile"
      fi
    fi
    report_advisory_diff "$_eco" "$_cur_ids" "$_base_ids" "$_no_base_reason"
    [ -n "$_base_ids" ] && rm -f "$_base_ids"
  else
    note "$_eco: could not extract structured advisory data for baseline diffing (jq missing, the audit output didn't parse, or its shape didn't match the plain result) — falling back to the plain result above."
    [ "$_pretty_rc" != "0" ] && rc=1
  fi
  rm -f "$_cur_ids"
}

if [ -f bun.lock ] || [ -f bun.lockb ]; then
  if command -v bun >/dev/null 2>&1; then
    ran=1; note "bun audit --audit-level=$LEVEL"
    audit_scope_to_base bun bun audit --audit-level="$LEVEL"
  else
    miss "bun lockfile present but bun not installed"
  fi
elif [ -f package.json ]; then
  if [ -f pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then
    ran=1; note "pnpm audit --audit-level $LEVEL"
    audit_scope_to_base pnpm pnpm audit --audit-level "$LEVEL"
  elif [ -f yarn.lock ] && command -v yarn >/dev/null 2>&1; then
    ran=1; note "yarn npm audit (yarn berry) — failing on $LEVEL+"
    audit_scope_to_base yarn yarn npm audit --severity "$LEVEL"
  elif command -v npm >/dev/null 2>&1; then
    ran=1; note "npm audit --audit-level=$LEVEL"
    audit_scope_to_base npm npm audit --audit-level="$LEVEL"
  else
    miss "package.json present but no usable node package manager (npm/pnpm/yarn)"
  fi
fi

# Python: audit the audited tree's DECLARED dependencies, never the runner's environment.
# SECURITY (agent-tools#129): under the tamper-resistant pull_request_target workflow this
# script audits the PR's tree as DATA. pip-audit is the ONE auditor that can EXECUTE input: a
# RESOLVING run (`-r <file>`, `-e .`, or a project/VCS/local-path requirement) downloads and
# BUILDS sdists — running their setup.py — i.e. arbitrary PR code under a privileged trigger
# (RCE). So we never resolve/build here.
# COVERAGE (agent-tools#131): the no-argument form (`pip-audit`) audits the runner's INSTALLED
# environment, NOT this tree's manifests — and nothing installs the PR's deps (the workflow's
# HARD RULE forbids it), so a bare run sets ran=1 and "passes" on the runner's base packages
# while the PR's vulnerable deps go unchecked. Audit the manifests as DATA instead:
#   - requirements*.txt (and the `requirements/<env>.txt` layout) -> `pip-audit --no-deps -r
#     <file>`, but ONLY after req_file_classify() confirms the file is purely PINNED specs.
#     `--no-deps` skips TRANSITIVE resolution; the data-safe scan rejects any DIRECT reference
#     (editable / URL / VCS / local path / `-r` include / unpinned) that would make pip-audit
#     BUILD PR code (RCE — see req_file_classify). Together: no sdist is downloaded or built;
#     pip-audit reads the file's pinned entries and queries the advisory DB — auditing the PR's
#     actual declared deps, not the runner's environment.
#   - a requirements file with a direct-reference / unpinned line: can't be audited as data
#     without building -> fail CLOSED (pin every line to name==version, or DEP_AUDIT_ALLOW_MISSING=1).
#   - pyproject.toml / poetry.lock WITHOUT a pinned requirements*.txt: pip-audit can audit these
#     only by BUILDING the project (RCE under pull_request_target) or, for poetry.lock, not at
#     all. So we do NOT fall back to the env-only `pip-audit` (which would check nothing of the
#     PR's) — they fail CLOSED (pin a requirements*.txt, or set DEP_AUDIT_ALLOW_MISSING=1).
# HARD RULE: never give pip-audit a resolving `-r`/`-e`/project-path invocation here without
# `--no-deps` AND the pinned-only data-safe scan (the `tests/test_ci_gate_bugs_129.py` guards pin this).
py_manifest=0
for f in requirements*.txt requirements/*.txt; do
  [ -f "$f" ] && { py_manifest=1; break; }
done
if [ "$py_manifest" = "1" ] || [ -f pyproject.toml ] || [ -f poetry.lock ]; then
  if command -v pip-audit >/dev/null 2>&1; then
    audited_py=0   # a real (pinned) requirements file was audited
    refused_py=0   # a requirements file was refused as unsafe (already fail-closed)
    for f in requirements*.txt requirements/*.txt; do
      [ -f "$f" ] || continue
      case "$(req_file_classify "$f")" in
        pinned)
          ran=1; audited_py=1
          note "pip-audit --no-deps -r $f"
          pip-audit --no-deps -r "$f" || rc=1 ;;
        empty)
          note "skipping $f — no pinned spec to audit (empty / comments / options only)" ;;
        *)  # unsafe
          refused_py=1
          miss "python requirements file $f has a non-pinned or direct-reference line (editable/URL/VCS/local-path/-r include/foreign index) — auditing it would BUILD PR code under pull_request_target (RCE); pin every line to name==version, or set DEP_AUDIT_ALLOW_MISSING=1" ;;
      esac
    done
    # An un-auditable pyproject.toml/poetry.lock fails CLOSED — UNLESS we already audited a pinned
    # requirements file (assumed to represent the deps) or already refused one (already
    # fail-closed). An empty/stub requirements.txt must NOT mask this (agent-tools#131).
    if [ "$audited_py" = "0" ] && [ "$refused_py" = "0" ] && { [ -f pyproject.toml ] || [ -f poetry.lock ]; }; then
      miss "python project (pyproject.toml/poetry.lock) present but no pinned requirements*.txt to audit as data (a building project-audit would execute PR code under pull_request_target)"
    fi
  else
    miss "python manifest present but pip-audit not installed (pipx install pip-audit)"
  fi
fi

if [ -f Cargo.lock ]; then
  if command -v cargo-audit >/dev/null 2>&1 || cargo audit --version >/dev/null 2>&1; then
    ran=1; note "cargo audit"; cargo audit || rc=1
  else
    miss "Cargo.lock present but cargo-audit not installed (cargo install cargo-audit)"
  fi
fi

if [ -f go.mod ]; then
  if command -v govulncheck >/dev/null 2>&1; then
    ran=1; note "govulncheck ./..."; govulncheck ./... || rc=1
  else
    miss "go.mod present but govulncheck not installed (go install golang.org/x/vuln/cmd/govulncheck@latest)"
  fi
fi

if [ "$ran" = "0" ] && [ "$missing" = "0" ]; then
  note "no supported manifest/lockfile found — nothing to audit."
  exit 0
fi
if [ "$missing" -gt 0 ]; then
  note "FAIL — $missing detected ecosystem(s) had no usable scanner (fail-closed). Install the tool(s) above, or set DEP_AUDIT_ALLOW_MISSING=1."
  exit 1
fi
[ "$rc" = "0" ] && note "PASS — no advisories at $LEVEL+." || note "FAIL — advisories at $LEVEL+ above."
exit "$rc"

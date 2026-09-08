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
# Usage: sh ci/dependency-review/dep-audit.sh [AUDIT_DIR]
#   AUDIT_DIR (optional, default '.'): the tree whose manifests/lockfiles to audit. The
#   tamper-resistant workflow (pull_request_target) passes the PR head's side worktree here
#   so the TRUSTED base copy of this script audits the PR's lockfiles as DATA — the auditors
#   only read the lockfile, they never run the package's install/lifecycle scripts.
set -eu

LEVEL="${DEP_AUDIT_LEVEL:-high}"
ALLOW_MISSING="${DEP_AUDIT_ALLOW_MISSING:-0}"
AUDIT_DIR="${1:-.}"
rc=0
ran=0
missing=0

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

# Emit name==version for registry/PyPI packages from uv.lock / poetry.lock as TEXT.
# Why: handing the lockfile or pyproject to pip-audit would BUILD it (RCE under
# pull_request_target). uv/poetry export would invoke those tools on the PR tree.
# Skip editable/path/git/url/virtual/directory — uv.lock `source = { editable = "." }`
# is the local project, not a registry pin. Strip TOML single- and double-quoted
# strings. A package record that cannot be emitted as name==version (unknown source,
# leftover quoting, invalid name/version) is written as a non-pin line so classify
# fails CLOSED — a sibling valid pin must not mask an omitted record.
extract_python_lock_pins() {  # $1 = uv.lock | poetry.lock
  awk '
    function flush() {
      if (name == "") {
        ver = ""; skip = 0; saw_reg = 0
        return
      }
      emit_ok = 0
      if (ver != "" && skip == 0 && (mode != "uv" || saw_reg == 1)) {
        if (name ~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/ && ver ~ /^[A-Za-z0-9][A-Za-z0-9.+!_-]*$/) {
          print name "==" ver
          emit_ok = 1
        }
      }
      if (skip == 0 && emit_ok == 0)
        print "UNPARSED-LOCK-RECORD"
      name = ""; ver = ""; skip = 0; saw_reg = 0
    }
    BEGIN {
      n = ARGV[1]
      mode = (n ~ /uv\.lock$/) ? "uv" : "poetry"
    }
    /^\[\[package\]\]/ { flush(); next }
    /^\[package\.source\]/ { skip = 1; next }
    /^\[/ { next }
    /^name[ \t]*=/ {
      line = $0
      sub(/^[ \t]*name[ \t]*=[ \t]*/, "", line)
      gsub(/"/, "", line)
      gsub("\047", "", line)
      gsub(/^[ \t]+|[ \t]+$/, "", line)
      if (name == "") name = line
      next
    }
    /^version[ \t]*=/ {
      line = $0
      sub(/^[ \t]*version[ \t]*=[ \t]*/, "", line)
      gsub(/"/, "", line)
      gsub("\047", "", line)
      gsub(/^[ \t]+|[ \t]+$/, "", line)
      if (ver == "") ver = line
      next
    }
    /^source[ \t]*=/ {
      if ($0 ~ /(editable|path|git|url|virtual|directory)[ \t]*=/) skip = 1
      if ($0 ~ /registry[ \t]*=/) saw_reg = 1
      next
    }
    END { flush() }
  ' "$1"
}

# Write extracted pins to a temp file OUTSIDE the audited tree, classify, then
# `pip-audit --no-deps -r` that temp. Never pass the lockfile/pyproject/-e.
audit_python_lockfile_as_data() {  # $1 = lockfile
  [ -f "$1" ] || return 0
  _py_lock_tmp=$(mktemp /tmp/dep-audit-py.XXXXXX)
  extract_python_lock_pins "$1" > "$_py_lock_tmp"
  case "$(req_file_classify "$_py_lock_tmp")" in
    pinned)
      ran=1; audited_py=1
      note "pip-audit --no-deps -r (extracted registry pins)"
      pip-audit --no-deps -r "$_py_lock_tmp" || rc=1 ;;
    empty)
      note "skipping $1 — no registry/PyPI pins to audit as data" ;;
    *)
      refused_py=1
      miss "extracted pins from $1 were not data-safe — auditing them would BUILD PR code under pull_request_target (RCE); pin name==version, or set DEP_AUDIT_ALLOW_MISSING=1" ;;
  esac
  rm -f "$_py_lock_tmp"
}

if [ -f bun.lock ] || [ -f bun.lockb ]; then
  if command -v bun >/dev/null 2>&1; then
    ran=1; note "bun audit --audit-level=$LEVEL"
    bun audit --audit-level="$LEVEL" || rc=1
  else
    miss "bun lockfile present but bun not installed"
  fi
elif [ -f package.json ]; then
  if [ -f pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then
    ran=1; note "pnpm audit --audit-level $LEVEL"; pnpm audit --audit-level "$LEVEL" || rc=1
  elif [ -f yarn.lock ] && command -v yarn >/dev/null 2>&1; then
    ran=1; note "yarn npm audit (yarn berry) — failing on $LEVEL+"; yarn npm audit --severity "$LEVEL" || rc=1
  elif command -v npm >/dev/null 2>&1; then
    ran=1; note "npm audit --audit-level=$LEVEL"; npm audit --audit-level="$LEVEL" || rc=1
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
#   - uv.lock / poetry.lock: parse as TEXT (never `uv export` / `poetry export` — those invoke
#     the package manager on the PR tree). Emit name==version for registry/PyPI packages only;
#     skip editable/path/git/url/virtual/directory. Write pins to a temp file OUTSIDE the
#     audited tree, classify, then `pip-audit --no-deps -r <temp>`. Never pass the lockfile,
#     pyproject.toml, `-e`, or a project path to pip-audit (that would BUILD = RCE).
#   - pyproject.toml WITHOUT a pinned requirements*.txt AND without auditable lockfile pins:
#     pip-audit can audit it only by BUILDING the project (RCE under pull_request_target).
#     Fail CLOSED (commit uv.lock/poetry.lock, pin a requirements*.txt, or set
#     DEP_AUDIT_ALLOW_MISSING=1). Do NOT fall back to env-only `pip-audit`.
# HARD RULE: never give pip-audit a resolving `-r`/`-e`/project-path invocation here without
# `--no-deps` AND the pinned-only data-safe scan (the `tests/test_ci_gate_bugs_129.py` guards pin this).
py_manifest=0
for f in requirements*.txt requirements/*.txt; do
  [ -f "$f" ] && { py_manifest=1; break; }
done
if [ "$py_manifest" = "1" ] || [ -f pyproject.toml ] || [ -f poetry.lock ] || [ -f uv.lock ]; then
  if command -v pip-audit >/dev/null 2>&1; then
    audited_py=0   # a pinned requirements file or extracted lockfile pins were audited
    refused_py=0   # a requirements/lockfile extract was refused as unsafe (already fail-closed)
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
    # Lockfile pins cover pyproject when no pinned requirements file was audited (an empty
    # stub requirements.txt must not mask pyproject — agent-tools#131 — unless a lockfile
    # supplies registry pins). Skip if we already refused an unsafe requirements file.
    if [ "$audited_py" = "0" ] && [ "$refused_py" = "0" ]; then
      audit_python_lockfile_as_data uv.lock
      audit_python_lockfile_as_data poetry.lock
    fi
    # Un-auditable pyproject/lockfile fails CLOSED unless we audited pins or already refused.
    if [ "$audited_py" = "0" ] && [ "$refused_py" = "0" ] && { [ -f pyproject.toml ] || [ -f poetry.lock ] || [ -f uv.lock ]; }; then
      miss "python project present but no pinned requirements*.txt and no auditable uv.lock/poetry.lock registry pins to audit as data (commit uv.lock/poetry.lock or pin a requirements*.txt; a building project-audit would execute PR code under pull_request_target)"
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

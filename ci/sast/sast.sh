#!/usr/bin/env sh
# SAST via semgrep — generic shell runner for any CI (GitLab / Jenkins / Buildkite / cron)
# or a local pre-push check. Mirrors ci/sast/workflow.yml so non-GitHub CI is first-class.
#
# Standard engine = semgrep (https://semgrep.dev). Installs it if missing (pip/pipx/brew),
# runs the configured ruleset, and EXITS NON-ZERO on findings (block tier — the right CI
# default). Set SAST_WARN=1 to report findings without failing (warn tier).
#
# Knobs (env):
#   SEMGREP_CONFIG   ruleset(s). Default "auto" (registry rules for detected languages).
#                    Alternatives: p/ci, p/security-audit, p/owasp-top-ten, ./.semgrep.yml,
#                    or several space-separated.
#   SEMGREP_TARGET   path to scan. Default "." (repo root).
#   SAST_WARN        "1" = never fail the build (warn tier). Default block.
#   SEMGREP_EXTRA    extra flags passed through to `semgrep scan`.
#
# Usage:
#   sh ci/sast/sast.sh
#   SEMGREP_CONFIG="p/security-audit p/secrets" sh ci/sast/sast.sh
#   SAST_WARN=1 sh ci/sast/sast.sh
set -eu

SEMGREP_CONFIG="${SEMGREP_CONFIG:-auto}"
SEMGREP_TARGET="${SEMGREP_TARGET:-.}"
SAST_WARN="${SAST_WARN:-0}"
SEMGREP_EXTRA="${SEMGREP_EXTRA:-}"

ensure_semgrep() {
  if command -v semgrep >/dev/null 2>&1; then return 0; fi
  echo "[sast] semgrep not found — attempting install…" >&2
  if command -v pipx >/dev/null 2>&1; then
    pipx install semgrep >/dev/null 2>&1 && return 0
  fi
  if command -v pip3 >/dev/null 2>&1; then
    pip3 install --user --quiet semgrep && return 0
  fi
  if command -v pip >/dev/null 2>&1; then
    pip install --user --quiet semgrep && return 0
  fi
  if command -v brew >/dev/null 2>&1; then
    brew install semgrep >/dev/null 2>&1 && return 0
  fi
  echo "[sast] ERROR: could not install semgrep automatically." >&2
  echo "       Install it manually: pipx install semgrep  (or pip install semgrep, brew install semgrep)" >&2
  return 1
}

ensure_semgrep || exit 2

# Build the --config flags (semgrep accepts repeated --config).
set --
for cfg in $SEMGREP_CONFIG; do
  set -- "$@" --config "$cfg"
done

echo "[sast] semgrep $(semgrep --version 2>/dev/null || echo '?') — config: $SEMGREP_CONFIG, target: $SEMGREP_TARGET" >&2

# --error makes semgrep exit non-zero on findings; we own the exit policy below.
# Capture semgrep's OWN exit code. We must NOT gate on `if semgrep …; then … exit 0; fi` and
# read `rc=$?` in the fall-through: after an `if` whose condition is false (and no else), POSIX
# `$?` is the `if` statement's status — which is 0 — so the runner would print FAIL but
# `exit 0`, leaving CI green on a real finding or a scanner failure (the whole point of block
# tier). Disable errexit only around the scanner so a non-zero result is captured, not
# fatal-on-the-spot.
set +e
# shellcheck disable=SC2086  # SEMGREP_EXTRA is an intentional word-split of pass-through flags
semgrep scan "$@" --error $SEMGREP_EXTRA "$SEMGREP_TARGET"
rc=$?
set -e

if [ "$rc" -eq 0 ]; then
  echo "[sast] PASS — no findings." >&2
  exit 0
fi

# Warn tier honors "never fails the build" UNCONDITIONALLY — not just for findings, but for an
# operational semgrep error too (bad flag, unreadable ruleset, network failure). So warn tier
# exits 0 regardless of `rc`, after surfacing whatever semgrep printed.
if [ "$SAST_WARN" = "1" ]; then
  echo "[sast] WARN tier — semgrep exited $rc (findings and/or a scanner error), NOT failing the build (SAST_WARN=1)." >&2
  exit 0
fi
echo "[sast] FAIL — semgrep exited $rc (findings, or a scanner error). Fix them, or use an inline" >&2
echo "       // nosemgrep comment / .semgrepignore for justified false positives." >&2
exit "$rc"

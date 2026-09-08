#!/usr/bin/env bash
# install.sh — install the zombie-parent-reaper as a launchd LaunchAgent that
# runs every 15 minutes (StartInterval=900s). macOS only.
#
# MANUAL install for now (same honest scope as ops/vscode-orphan-reaper:
# rig apply's agenttools_service only knows RunAtLoad+KeepAlive daemons, not
# StartInterval periodic jobs). Idempotent: re-running overwrites the plist
# in place and reloads it.
#
# USAGE
#   ./install.sh            # install + load
#   ./install.sh --uninstall  # unload + remove
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.hyperide.zombie-parent-reaper"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs"
LOG_PATH="${LOG_DIR}/${LABEL}.log"

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "install.sh: unloading ${LABEL}"
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "install.sh: removed ${PLIST_DST}"
  exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install.sh: this reaper targets macOS launchd only — nothing to install on $(uname -s)." >&2
  exit 1
fi

# Prefer the SYSTEM python3, not `command -v python3`: this script is
# normally run from a feature worktree, and a worktree venv baked into the
# plist dies when the worktree is removed. `-x` alone is not enough (CLT
# stub). Same predicate as ops/vscode-orphan-reaper/install.sh.
_python3_runs() { "$1" -c '' >/dev/null 2>&1; }

if [[ -x /usr/bin/python3 ]] && _python3_runs /usr/bin/python3; then
  PYTHON3=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PYTHON3="$(command -v python3)"
  if ! _python3_runs "$PYTHON3"; then
    echo "install.sh: the only python3 found (${PYTHON3}) does not run (likely the CLT-less macOS stub) -- install real developer tools or a Homebrew python3 and re-run." >&2
    exit 1
  fi
  REPO_ROOT_FOR_CHECK="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$REPO_ROOT_FOR_CHECK" && "$PYTHON3" == "$REPO_ROOT_FOR_CHECK"/* ]]; then
    echo "install.sh: the only python3 found (${PYTHON3}) lives inside this checkout/worktree -- it will break once this worktree is removed. Install a system/Homebrew python3 (outside any worktree) and re-run." >&2
    exit 1
  fi
  VIRTUAL_ENV_NO_TRAILING_SLASH="${VIRTUAL_ENV:-}"
  VIRTUAL_ENV_NO_TRAILING_SLASH="${VIRTUAL_ENV_NO_TRAILING_SLASH%/}"
  if [[ -n "$VIRTUAL_ENV_NO_TRAILING_SLASH" && "$PYTHON3" == "$VIRTUAL_ENV_NO_TRAILING_SLASH"/* ]]; then
    echo "install.sh: the only python3 found (${PYTHON3}) is inside the currently active virtualenv (\$VIRTUAL_ENV=${VIRTUAL_ENV:-}) -- it will break once that environment is removed. Deactivate it, install a system/Homebrew python3, and re-run." >&2
    exit 1
  fi
else
  echo "install.sh: python3 not found (neither /usr/bin/python3 nor on PATH)" >&2
  exit 1
fi

SOURCE_SCRIPT_PATH="${SCRIPT_DIR}/reap_zombie_parents.py"
[[ -f "$SOURCE_SCRIPT_PATH" ]] || { echo "install.sh: missing ${SOURCE_SCRIPT_PATH}" >&2; exit 1; }

STABLE_DIR="$HOME/.local/share/agent-tools/zombie-parent-reaper"
mkdir -p "$STABLE_DIR"
SCRIPT_PATH="${STABLE_DIR}/reap_zombie_parents.py"
cp "$SOURCE_SCRIPT_PATH" "$SCRIPT_PATH"
chmod +x "$SCRIPT_PATH"

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$PLIST_DST")"

xml_escape() {
  local s=$1
  s=${s//&/&amp;}
  s=${s//</&lt;}
  s=${s//>/&gt;}
  s=${s//\"/&quot;}
  printf '%s' "$s"
}

sed_replacement_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//&/\\&}"
  s="${s//#/\\#}"  # '#' is this script's sed delimiter (s#...#...#)
  printf '%s' "$s"
}

PYTHON3_ESC="$(sed_replacement_escape "$(xml_escape "$PYTHON3")")"
SCRIPT_PATH_ESC="$(sed_replacement_escape "$(xml_escape "$SCRIPT_PATH")")"
LOG_PATH_ESC="$(sed_replacement_escape "$(xml_escape "$LOG_PATH")")"

TMP_PLIST="$(mktemp "${TMPDIR:-/tmp}/zombie-parent-reaper-plist.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT

sed \
  -e "s#__PYTHON3__#${PYTHON3_ESC}#" \
  -e "s#__SCRIPT_PATH__#${SCRIPT_PATH_ESC}#" \
  -e "s#__LOG_PATH__#${LOG_PATH_ESC}#" \
  "${SCRIPT_DIR}/com.hyperide.zombie-parent-reaper.plist.template" > "$TMP_PLIST"

if ! plutil -lint -s "$TMP_PLIST" >/dev/null 2>&1; then
  echo "install.sh: rendered plist failed plutil -lint — refusing to install a malformed unit:" >&2
  plutil -lint "$TMP_PLIST" >&2 || true
  exit 1
fi

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST_DST" 2>/dev/null || true
mv "$TMP_PLIST" "$PLIST_DST"
trap - EXIT
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || launchctl load "$PLIST_DST"

echo "install.sh: installed ${LABEL} — runs every 15 min via launchd, log at ${LOG_PATH}"
echo "install.sh: verify with: launchctl list | grep ${LABEL}"

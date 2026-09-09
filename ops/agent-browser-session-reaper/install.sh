#!/usr/bin/env bash
# install.sh — install BOTH agent-browser cleanup mechanisms on this machine:
#
#   1. a Claude Code SessionEnd hook  — closes the browser sessions a finishing
#      Claude Code session claimed (precise, owner-aware);
#   2. a launchd periodic sweep       — closes any session older than the age
#      threshold, every 15 minutes (owner-blind backstop).
#
# Mechanism 1 needs a third piece to know who owns what: the `pre-bash` agent-hook
# `agent-browser-session-claim`, which this script also installs into the harness
# hook dir. See ../../agent-hooks/agent-browser-session-claim/README.md.
#
# macOS only (launchd). Idempotent: re-run after editing the scripts to refresh
# everything in place.
#
# USAGE
#   ./install.sh              # install + load
#   ./install.sh --uninstall  # remove everything this installed
#   ./install.sh --status     # report what is currently installed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_HOOK_DIR="$(cd "${SCRIPT_DIR}/../../agent-hooks/agent-browser-session-claim" && pwd)"

LABEL="com.hyperide.agent-browser-session-reaper"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="$HOME/Library/Logs/${LABEL}.log"
STABLE_DIR="$HOME/.local/share/agent-tools/agent-browser-session-reaper"
CLAUDE_HOOK_DIR="$HOME/.claude/hooks"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
CLAIM_DESCRIPTOR="${CLAUDE_HOOK_DIR}/agent-browser-session-claim.pre-bash.json"
CONFIG_PATH="$HOME/.config/agent-tools/agent-browser-reaper.conf"

SWEEP_SCRIPT="${STABLE_DIR}/agent_browser_reaper.py"
SESSION_END_SCRIPT="${STABLE_DIR}/session_end_close.py"
CLAIM_SCRIPT="${STABLE_DIR}/claim_agent_browser_session.py"

# --- helpers ---------------------------------------------------------------------

# Actually RUN the interpreter rather than trusting `-x`: on a macOS box without
# Xcode Command Line Tools, /usr/bin/python3 is an executable stub that fails on
# every invocation. Same rationale (and same failure that bit it once) as the
# sibling ops/vscode-orphan-reaper/install.sh.
_python3_runs() { "$1" -c '' >/dev/null 2>&1; }

pick_python3() {
  if [[ -x /usr/bin/python3 ]] && _python3_runs /usr/bin/python3; then
    echo /usr/bin/python3
    return 0
  fi
  local found
  found="$(command -v python3 2>/dev/null || true)"
  if [[ -z "$found" ]] || ! _python3_runs "$found"; then
    echo "install.sh: no working python3 found" >&2
    return 1
  fi
  # Refuse an interpreter that lives inside this checkout or an active virtualenv:
  # this script is normally run from a throwaway feature worktree, and such a path
  # is baked into the plist and would break the moment the worktree is removed.
  local repo_root venv
  repo_root="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  venv="${VIRTUAL_ENV:-}"
  venv="${venv%/}"
  if [[ -n "$repo_root" && "$found" == "$repo_root"/* ]] \
     || [[ -n "$venv" && "$found" == "$venv"/* ]]; then
    echo "install.sh: the only python3 found (${found}) is inside this checkout or an active virtualenv — it will break once that is removed. Install a system/Homebrew python3 and re-run." >&2
    return 1
  fi
  echo "$found"
}

xml_escape() {
  local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"
  printf '%s' "$s"
}

# sed's REPLACEMENT side treats '&' as "the whole match" and '\' as an escape, and
# xml_escape's own output for '&' is "&amp;" — which contains a bare '&'. Apply
# this SECOND so both escaping layers compose.
sed_escape() {
  local s="$1"; s="${s//\\/\\\\}"; s="${s//&/\\&}"; s="${s//#/\\#}"
  printf '%s' "$s"
}

sub() { sed_escape "$(xml_escape "$1")"; }

# --- uninstall --------------------------------------------------------------------

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
    || launchctl unload "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST" "$CLAIM_DESCRIPTOR"
  # Unwiring settings.json must not be able to abort the rest of the uninstall.
  if [[ -f "$CLAUDE_SETTINGS" ]]; then
    PY="$(pick_python3 || true)"
    if [[ -n "$PY" ]]; then
      "$PY" "${SCRIPT_DIR}/wire_session_end_hook.py" --remove \
        --settings "$CLAUDE_SETTINGS" || \
        echo "install.sh: could not unwire the SessionEnd hook — remove the session_end_close.py entry from ${CLAUDE_SETTINGS} by hand." >&2
    else
      echo "install.sh: no working python3 — remove the session_end_close.py entry from ${CLAUDE_SETTINGS} by hand." >&2
    fi
  fi
  rm -rf "$STABLE_DIR"
  echo "install.sh: removed the launchd job, the SessionEnd hook, the claim hook and ${STABLE_DIR}"
  echo "install.sh: left in place (yours to delete): ${CONFIG_PATH}, ${LOG_PATH}, ~/.cache/agent-tools/agent-browser-sessions"
  exit 0
fi

if [[ "${1:-}" == "--status" ]]; then
  echo "launchd job : $(launchctl list 2>/dev/null | grep -F "$LABEL" || echo 'NOT loaded')"
  echo "plist       : $([[ -f $PLIST_DST ]] && echo "$PLIST_DST" || echo 'absent')"
  echo "claim hook  : $([[ -f $CLAIM_DESCRIPTOR ]] && echo "$CLAIM_DESCRIPTOR" || echo 'absent')"
  echo "scripts     : $([[ -d $STABLE_DIR ]] && echo "$STABLE_DIR" || echo 'absent')"
  echo "config      : $([[ -f $CONFIG_PATH ]] && echo "$CONFIG_PATH" || echo "absent (defaults apply)")"
  grep -q 'session_end_close.py' "$CLAUDE_SETTINGS" 2>/dev/null \
    && echo "SessionEnd  : wired in ${CLAUDE_SETTINGS}" \
    || echo "SessionEnd  : NOT wired in ${CLAUDE_SETTINGS}"
  exit 0
fi

# --- install ----------------------------------------------------------------------

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install.sh: the periodic sweep targets macOS launchd only — nothing to install on $(uname -s)." >&2
  exit 1
fi

PYTHON3="$(pick_python3)"

AGENT_BROWSER_BIN="$(command -v agent-browser 2>/dev/null || true)"
if [[ -z "$AGENT_BROWSER_BIN" ]]; then
  echo "install.sh: agent-browser is not on PATH — install it first (brew install agent-browser)." >&2
  exit 1
fi
# launchd starts jobs with a minimal PATH that does NOT include Homebrew, so the
# sweep would never find the CLI. Bake the CLI's own directory into the plist.
LAUNCHD_PATH="$(dirname "$AGENT_BROWSER_BIN"):/usr/bin:/bin:/usr/sbin:/sbin"

# Copy every script to STABLE storage before anything points at it: this installer
# normally runs from a throwaway feature worktree, and both the plist and the hook
# descriptor need an absolute path that outlives that worktree.
mkdir -p "$STABLE_DIR" "$(dirname "$PLIST_DST")" "$(dirname "$LOG_PATH")" \
         "$CLAUDE_HOOK_DIR" "$(dirname "$CONFIG_PATH")"
cp "${SCRIPT_DIR}/agent_browser_reaper.py" "$SWEEP_SCRIPT"
cp "${SCRIPT_DIR}/session_end_close.py" "$SESSION_END_SCRIPT"
cp "${REPO_HOOK_DIR}/claim_agent_browser_session.py" "$CLAIM_SCRIPT"
chmod +x "$SWEEP_SCRIPT" "$SESSION_END_SCRIPT" "$CLAIM_SCRIPT"

# 1. the pre-bash claim hook descriptor (absolute cmd, per the agents-hooks/v1 rule)
"$PYTHON3" - "$REPO_HOOK_DIR" "$CLAIM_SCRIPT" "$CLAIM_DESCRIPTOR" <<'PYEOF'
import json, sys
src_dir, script_path, dst = sys.argv[1], sys.argv[2], sys.argv[3]
with open(f"{src_dir}/agent-browser-session-claim.pre-bash.json", encoding="utf-8") as fh:
    descriptor = json.load(fh)
descriptor["cmd"] = script_path
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(descriptor, fh, indent=2)
    fh.write("\n")
print(f"install.sh: wrote {dst}")
PYEOF

# 2. the SessionEnd hook entry in Claude Code's settings.json (backed up first)
"$PYTHON3" "${SCRIPT_DIR}/wire_session_end_hook.py" \
  --settings "$CLAUDE_SETTINGS" --python "$PYTHON3" --script "$SESSION_END_SCRIPT"

# 3. a documented, editable config file — created once, never overwritten
if [[ ! -f "$CONFIG_PATH" ]]; then
  cp "${SCRIPT_DIR}/agent-browser-reaper.conf.example" "$CONFIG_PATH"
  echo "install.sh: wrote default config ${CONFIG_PATH}"
fi

# 4. the launchd periodic sweep
TMP_PLIST="$(mktemp "${TMPDIR:-/tmp}/agent-browser-reaper-plist.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT
sed \
  -e "s#__PYTHON3__#$(sub "$PYTHON3")#" \
  -e "s#__SCRIPT_PATH__#$(sub "$SWEEP_SCRIPT")#" \
  -e "s#__PATH__#$(sub "$LAUNCHD_PATH")#" \
  -e "s#__LOG_PATH__#$(sub "$LOG_PATH")#" \
  "${SCRIPT_DIR}/${LABEL}.plist.template" > "$TMP_PLIST"

if ! plutil -lint -s "$TMP_PLIST" >/dev/null 2>&1; then
  echo "install.sh: rendered plist failed plutil -lint — refusing to install a malformed unit:" >&2
  plutil -lint "$TMP_PLIST" >&2 || true
  exit 1
fi

# Only unload the OLD unit once the NEW one is known to be well-formed.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
  || launchctl unload "$PLIST_DST" 2>/dev/null || true
mv "$TMP_PLIST" "$PLIST_DST"
trap - EXIT
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || launchctl load "$PLIST_DST"

echo "install.sh: installed ${LABEL} — sweeps every 15 min, log at ${LOG_PATH}"
echo "install.sh: verify with: ./install.sh --status"
echo "install.sh: turn off with: ./install.sh --uninstall   (or set ENABLED=0 in ${CONFIG_PATH})"

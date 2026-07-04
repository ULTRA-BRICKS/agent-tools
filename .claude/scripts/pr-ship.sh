#!/usr/bin/env bash
# Provisioned by rig (ship_delegator). The global `gh ship` alias runs
# <repo>/.claude/scripts/pr-ship.sh. agent-tools' canonical, generalized ship
# implementation lives at ci/ship/ship.sh — delegate to it so `gh ship` works in this
# repo with the same green-CI-gated merge + cleanup as everywhere else. Repo-local
# ci/ship/ship.sh wins (agent-tools self-hosts); otherwise the rig-baked canonical path.
set -euo pipefail
toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"
repo_local="${toplevel:+$toplevel/ci/ship/ship.sh}"
if [[ -n "$repo_local" && -f "$repo_local" ]]; then
  exec "$repo_local" "$@"
fi
_rig_default=/Users/ultra/xp/agent-tools
AGENT_TOOLS_ROOT="${AGENT_TOOLS_ROOT:-$_rig_default}"
unset _rig_default
export AGENT_TOOLS_ROOT
canonical="$AGENT_TOOLS_ROOT/ci/ship/ship.sh"
if [[ -f "$canonical" ]]; then
  exec "$canonical" "$@"
fi
echo "pr-ship.sh: canonical ship.sh not found (repo-local $repo_local nor $canonical)." >&2
echo "Set AGENT_TOOLS_ROOT to your agent-tools checkout, or re-run 'rig apply'." >&2
exit 127

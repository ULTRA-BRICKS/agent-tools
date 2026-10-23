#!/usr/bin/env bash
# gh ship — green-CI-gated PR merge. The implementation is ship.py next to this file; this launcher
# stays shell only so `gh ship` and every caller keep the path ci/ship/ship.sh (the python-only rule
# caps shell at 40 lines, agent-tools#946). Usage and knobs: ci/ship/README.md.
set -euo pipefail
_src="${BASH_SOURCE[0]:-$0}"
# Follow symlinks so a `~/bin/ship -> <checkout>/ci/ship/ship.sh` link still finds ship.py and lib/.
_hops=0
while [ -L "$_src" ]; do
  _hops=$((_hops + 1)); [ "$_hops" -le 40 ] || { echo "ship: symlink loop resolving $0" >&2; exit 1; }
  _link="$(readlink "$_src")"
  case "$_link" in /*) _src="$_link" ;; *) _src="$(dirname "$_src")/$_link" ;; esac
done
_dir="$(cd "$(dirname "$_src")" 2>/dev/null && pwd -P || echo /nonexistent)"
_py=""
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    # Windows Git Bash: `python3` is often the Microsoft Store stub; prefer uv's or the `py` launcher.
    case "$(command -v python3 2>/dev/null)" in
      ""|*/WindowsApps/*)
        if command -v uv >/dev/null 2>&1; then _py="$(uv python find 2>/dev/null || true)"; fi
        if [ -z "$_py" ] && command -v py >/dev/null 2>&1; then _py="$(py -3 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"; fi ;;
    esac ;;
esac
_py="${_py:-python3}"
command -v "$_py" >/dev/null 2>&1 || { echo "python3 is required to run gh ship" >&2; exit 1; }
export SHIP_REAL_PYTHON="$_py"  # ship.py runs its helper scripts with this, not an inherited value
[ -f "$_dir/ship.py" ] || { echo "ship: $_dir/ship.py not found - link ci/ship/ship.sh, do not copy it alone" >&2; exit 1; }
exec "$_py" -B -I "$_dir/ship.py" "$@"

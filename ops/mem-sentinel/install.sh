#!/usr/bin/env bash
# install.sh: install/refresh the mem-sentinel daemon (per-omp-session memory watchdog).
# Usage (from any agent-tools checkout): sudo ops/mem-sentinel/install.sh
# Idempotent, no reboot; restarts the unit only when the script or unit changed.
# earlyoom (mem-guard.sh) is the last line of defence; the sentinel is the first: it tells the
# *owning* agent session to stop while there is still headroom, instead of letting the box
# thrash for 18 minutes. See README.md next to this file.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=/usr/local/sbin/mem-sentinel
UNIT=/etc/systemd/system/mem-sentinel.service
LOG=/var/log/hang-probe/sentinel.log

command -v python3 >/dev/null || { echo "python3 missing" >&2; exit 1; }
# parse-only check: py_compile would leave a root-owned __pycache__ in the checkout
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' "$HERE/mem_sentinel.py"
mkdir -p "$(dirname "$LOG")"

changed=0
for pair in "$HERE/mem_sentinel.py:$BIN:755" "$HERE/mem-sentinel.service:$UNIT:644"; do
  src=${pair%%:*}; rest=${pair#*:}; dst=${rest%%:*}; mode=${rest#*:}
  if ! cmp -s "$src" "$dst"; then install -m "$mode" "$src" "$dst"; changed=1; fi
done

# logrotate: diag.sh already rotates /var/log/hang-probe/*.log (which matches sentinel.log); a
# second stanza for the same file makes logrotate refuse with "duplicate log entry", so only add
# ours when nothing else covers the directory, and drop ours once something else does.
# (/dev/null keeps grep off stdin when the directory holds nothing else)
if grep -qs 'hang-probe/\*\.log' /dev/null $(ls /etc/logrotate.d/* 2>/dev/null | grep -v '/mem-sentinel$'); then
  rm -f /etc/logrotate.d/mem-sentinel
else
  cat > /etc/logrotate.d/mem-sentinel <<EOF
$LOG {
  daily
  rotate 7
  copytruncate
  compress
  missingok
  notifempty
}
EOF
fi

systemctl daemon-reload
systemctl enable --now mem-sentinel.service >/dev/null
[ "$changed" = 1 ] && systemctl restart mem-sentinel.service
sleep 1

echo "=== mem-sentinel status ==="
echo "mem-sentinel: $(systemctl is-active mem-sentinel)  enabled: $(systemctl is-enabled mem-sentinel)  pid: $(systemctl show -p MainPID --value mem-sentinel)"
echo "unit limits: $(systemctl show -p Nice,OOMScoreAdjust,MemoryMin --value mem-sentinel | paste -sd' ')"
echo "log: $LOG  last: $(tail -1 "$LOG" 2>/dev/null | cut -c1-220)"
echo "scopes now: $(ls -d /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/omp.slice/omp-*.scope 2>/dev/null | wc -l)  (0 = all sessions legacy, attributed by process tree)"
echo "tune: sudo systemctl edit mem-sentinel -> [Service] Environment=MEM_SENTINEL_GROWTH_GB=3 (see CONFIG in $BIN)"

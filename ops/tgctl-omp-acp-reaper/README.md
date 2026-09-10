# tgctl-omp-acp-reaper

Periodic (every 15 min, macOS launchd) sweep that SIGKILLs orphaned `omp acp`
helpers left behind by tg-cli's ctl-omp-acp / ctl-omp-reply integration tests.

See `reap_tgctl_omp_acp.py`'s module docstring for the 2026-09-09 OOM incident
writeup and the safety rationale (ppid == 1 + tgctl temp-dir marker + age
threshold + 0% CPU + token-boundary `omp acp` match).

## Status: manually installed, NOT yet wired into `rig apply`

Same honest scope as ops/vscode-orphan-reaper and ops/zombie-parent-reaper:
`rig apply`'s agenttools_service only knows RunAtLoad+KeepAlive daemons, not
StartInterval periodic jobs.

```
./install.sh              # renders the plist, installs to ~/Library/LaunchAgents, loads it
./install.sh --uninstall  # unloads and removes it
launchctl list | grep com.hyperide.tgctl-omp-acp-reaper   # verify it's loaded
```

Logs go to `~/Library/Logs/com.hyperide.tgctl-omp-acp-reaper.log`.

## Documented coupling (no automated drift guard)

The `/tgctl-omp-(acp|reply)-<suffix>` path token is OWNED by tg-cli test harnesses
(`mkdtempSync(join(tmpdir(), 'tgctl-omp-acp-'))` etc.). This script duplicates
that positive-match predicate in Python rather than importing it — a standalone
OS process under launchd has no access to tg-cli's TypeScript toolchain.

A live `omp acp` child still owned by the running tg-ctl daemon has ppid != 1
and is never a candidate. The unit tests fabricate ps rows proving that boundary.

## Testing

```
uv run --with pytest python -m pytest tests/test_reap_tgctl_omp_acp.py -q
```

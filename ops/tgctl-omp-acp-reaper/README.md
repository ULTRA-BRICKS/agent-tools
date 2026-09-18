# tgctl-omp-acp-reaper

Periodic (every 15 min, macOS launchd) sweep that SIGKILLs orphaned `omp acp`
helpers left behind by tg-cli's integration tests (see `_TGCTL_MKDTEMP_PREFIXES`
in `reap_tgctl_omp_acp.py` for the current list of covered test suites).

See `reap_tgctl_omp_acp.py`'s module docstring for the 2026-09-09 OOM incident
writeup and the safety rationale (ppid == 1 + tgctl temp-dir marker + age
threshold + 0% CPU + token-boundary `omp acp` match).

## Status: manually installed; superseded by rig's `tgctl_reaper` provisioning

`rig-cli` now provisions this reaper declaratively via `rig apply` (the `tgctl_reaper:` config
block, GLOBAL layer, default ON on macOS) — see `riglib/tgctl_reaper.py` +
`riglib/tgctl_reaper_daemon.py` in the `rig-cli` repo. It reuses this script's exact launchd
Label (`com.hyperide.tgctl-omp-acp-reaper`) so a machine that ran `./install.sh` by hand
converges onto the rig-managed version on the next `rig apply commit` (backup-on-conflict,
same as every other rig-managed artifact) instead of running a second, colliding LaunchAgent.
`install.sh` below remains for a machine not yet running `rig apply`, or for manual testing.

Same honest scope note applies to the STILL-unwired siblings ops/vscode-orphan-reaper and
ops/zombie-parent-reaper: `rig apply`'s agenttools_service only knows RunAtLoad+KeepAlive
daemons, not StartInterval periodic jobs — THIS reaper is the first of the three to get a
dedicated `StartInterval` provisioning module in rig-cli, not a fix to that shared limitation.

```
./install.sh              # renders the plist, installs to ~/Library/LaunchAgents, loads it
./install.sh --uninstall  # unloads and removes it
launchctl list | grep com.hyperide.tgctl-omp-acp-reaper   # verify it's loaded
```

Logs go to `~/Library/Logs/com.hyperide.tgctl-omp-acp-reaper.log`.

## Documented coupling (no automated drift guard)

The `/tgctl-<prefix>-<suffix>` path token (see `_TGCTL_MKDTEMP_PREFIXES` in
`reap_tgctl_omp_acp.py` for the current prefix list) is OWNED by tg-cli test
harnesses (`mkdtempSync(join(tmpdir(), 'tgctl-omp-acp-'))` etc.). This script
duplicates that positive-match predicate in Python rather than importing it —
a standalone OS process under launchd has no access to tg-cli's TypeScript
toolchain. When tg-cli adds a new mkdtemp-fixture prefix using this same
convention, `_TGCTL_MKDTEMP_PREFIXES` must be updated here too — this doc
deliberately does NOT re-enumerate the list to avoid a second copy drifting
out of sync with the code (agent-tools#775 was exactly that drift).

A live `omp acp` child still owned by the running tg-ctl daemon has ppid != 1
and is never a candidate. The unit tests fabricate ps rows proving that boundary.

## Testing

```
uv run --with pytest python -m pytest tests/test_reap_tgctl_omp_acp.py -q
```

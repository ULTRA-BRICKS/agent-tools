# mem-sentinel

Root systemd daemon (Linux) that watches memory per omp agent session and steers the session
that is eating RAM (`tg-ctl omp-live send <pid>`, exact pid - `omp-steer <pid>` would also
match the pid as a cwd substring) while the machine still has headroom. When the whole box is
about to go under it broadcasts to every session (`omp-steer all`) and raises a desktop
notification. earlyoom is the last line of defence; this is the first.

## What counts

- **Session footprint = anon + shmem + swap** from the scope's `memory.stat` and
  `memory.swap.current`. Page cache (`file - shmem`) is reclaimable and is never counted;
  `memory.current` includes it, which once drew ~18 false steers per cache-heavy session
  (agent-tools#884). Legacy sessions without a scope are summed by process tree from
  `/proc/<pid>/statm` (resident - shared).
- **Per-session steers fire only while the machine is short**: MemAvailable below
  `LOW_AVAIL_PCT` (25 %) or falling at `FALL_GB_PER_MIN` (6 GB/min). With low headroom:
  (a) dominant session, (d) low swap - blames the session holding the most swap
  (`memory.swap.current` >= `SWAP_BLAME_GB`), never just the largest one, (e) session over
  `SESSION_MAX_GB`. During a fast fall,
  only a session whose own footprint grows: (b) fast growth, (c) fastest grower.
- **Critical broadcast** (unchanged): MemAvailable below `CRIT_AVAIL_PCT` (10 %, about a
  minute above earlyoom's 4 %) or PSI `full avg10` above 20.
- **Kill advice** names the heaviest child process by anon RSS, never the session's own omp
  root or anything above it; below `KILL_MIN_GB` no pid is named.
- **Rate limit**: `STEER_COOLDOWN_S` (180 s) between steers to one session; a steer with the
  same reasons and a footprint within `REPEAT_DELTA_GB` of the last one waits
  `REPEAT_COOLDOWN_S` (900 s). A session quiet that long starts again at warning #1. Each
  steer states its reason codes (`Why: (a) ...`).

Every knob is a `MEM_SENTINEL_<NAME>` env var (see `CONFIG` in `mem_sentinel.py`); set them
with `sudo systemctl edit mem-sentinel`. `MEM_SENTINEL_DRY_RUN=1` logs instead of steering.

## Install

```bash
sudo ops/mem-sentinel/install.sh
```

Copies `mem_sentinel.py` to `/usr/local/sbin/mem-sentinel` and the unit to
`/etc/systemd/system/mem-sentinel.service`, enables it, and restarts it when either changed.
Logs: `journalctl -u mem-sentinel` and `/var/log/hang-probe/sentinel.log`.

## Test

```bash
uv run --with pytest python -m pytest tests/test_mem_sentinel.py -q
```

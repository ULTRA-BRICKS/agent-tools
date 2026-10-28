#!/usr/bin/env python3
"""mem-sentinel: per-omp-session memory watchdog that warns the *owning* agent session early.

Why this exists: on 2026-10-11 13:34 three concurrent `review diff` runs plus
pytest/testcontainers/bun-test lost 26 GB in two minutes. Swap was already full, the kernel OOM
killer never fired (file-cache thrash kept "making progress"), systemd-oomd only watches one
cgroup that holds every omp session at once, and the box thrashed for 18 minutes. The agents that
caused it had been told to offload heavy work to the server but nobody told them *while it happened*.
This daemon attributes memory to sessions (cgroup scope omp-<N>.scope, or process tree for legacy
sessions), detects the spike while there is still headroom, and delivers a steer message into the
session that owns it (`omp-steer`), escalating to a broadcast + desktop notification when the whole
machine is about to go under. earlyoom (installed by mem-guard.sh) is the last line; this is the
first. Stdlib only, 2 s loop, designed to keep running while the box thrashes (mlockall, see unit).

What counts (agent-tools#884): a session's footprint is anon + shmem + swap from memory.stat, never
memory.current - page cache is reclaimable and a cache-heavy session (git, rg, builds reading the
tree) once drew ~18 false steers while the box had 20-37 GB available. Session steers fire only
while the machine is actually short (MemAvailable low, or falling fast); the critical broadcast near
earlyoom's threshold is independent of that gate.

All knobs: MEM_SENTINEL_<NAME> env vars (see CONFIG). MEM_SENTINEL_DRY_RUN=1 logs instead of steering.
Installed as /usr/local/sbin/mem-sentinel by ops/mem-sentinel/install.sh in the agent-tools repo.
"""
import ctypes
import glob
import json
import os
import subprocess
import sys
import syslog
import threading
import time
from collections import deque

GB = 1024 ** 3


def _env(name, default, cast=float):
    v = os.environ.get("MEM_SENTINEL_" + name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except ValueError:
        sys.stderr.write(f"mem-sentinel: bad MEM_SENTINEL_{name}={v!r}, using {default}\n")
        return default


CONFIG = dict(
    INTERVAL=_env("INTERVAL", 2.0),
    DRY_RUN=bool(_env("DRY_RUN", 0, int)),
    LOG=_env("LOG", "/var/log/hang-probe/sentinel.log", str),
    USER=_env("USER", "ultra", str),
    UID=_env("UID", 1000, int),
    SESSIONS_TTL=_env("SESSIONS_TTL", 15.0),           # omp-steer list is a tg-ctl round trip: cache it
    HEARTBEAT_S=_env("HEARTBEAT_S", 30.0),             # forensic summary line cadence (0 = off)
    # Gate for every per-session steer: the machine is short only when MemAvailable is below
    # LOW_AVAIL_PCT or falling at FALL_GB_PER_MIN. Outside it nothing a session holds is a threat.
    # 25 % of 48 GB = 12 GB: at 13:35:30 on 2026-10-11 we passed 12 GB free with 90 s left.
    LOW_AVAIL_PCT=_env("LOW_AVAIL_PCT", 25.0),
    # (a) low headroom + one dominant session: the 30 % share / 8 GB floor picks the real culprit.
    TOP_SHARE_PCT=_env("TOP_SHARE_PCT", 30.0),
    TOP_ABS_GB=_env("TOP_ABS_GB", 8.0),
    # (b) early warning: 4 GB in 30 s is the signature of a full pytest/bun-test/mypy fan-out
    #     (the incident rate was ~13 GB/min); a single test file never does this.
    GROWTH_GB=_env("GROWTH_GB", 4.0),
    GROWTH_WINDOW_S=_env("GROWTH_WINDOW_S", 30.0),
    # (c) machine-wide fall rate; 6 GB/min means the 48 GB box has < 5 min of life left.
    FALL_GB_PER_MIN=_env("FALL_GB_PER_MIN", 6.0),
    # (d) swap was 100 % full for 13 h before the incident; below 30 % free there is no cushion.
    SWAPFREE_PCT=_env("SWAPFREE_PCT", 30.0),
    # (e) no legitimate local agent workload needs 16 GB anon; that is a full suite or a leak.
    SESSION_MAX_GB=_env("SESSION_MAX_GB", 16.0),
    # (f) broadcast: 10 % free (~4.8 GB) is ~1 min above earlyoom's 4 % trigger; PSI full>20 means
    #     every task already spends a fifth of its time stalled on memory = thrash has begun.
    CRIT_AVAIL_PCT=_env("CRIT_AVAIL_PCT", 10.0),
    PSI_FULL_AVG10=_env("PSI_FULL_AVG10", 20.0),
    STEER_COOLDOWN_S=_env("STEER_COOLDOWN_S", 180.0),
    # A steer with the same reasons and the same footprint (+-REPEAT_DELTA_GB) as the last one to
    # that session says nothing new: hold it this long (2026-10-14: #35..#39 were word-identical).
    REPEAT_COOLDOWN_S=_env("REPEAT_COOLDOWN_S", 900.0),
    REPEAT_DELTA_GB=_env("REPEAT_DELTA_GB", 1.0),
    # a child below this anon RSS is not worth naming as the process to kill
    KILL_MIN_GB=_env("KILL_MIN_GB", 0.5),
    # (d) names only a session that itself holds this much swap: on a full swap the largest
    # session is often not the one swapped out
    SWAP_BLAME_GB=_env("SWAP_BLAME_GB", 0.5),
    BROADCAST_COOLDOWN_S=_env("BROADCAST_COOLDOWN_S", 180.0),
    RING_S=_env("RING_S", 60.0),
)

CG_ROOT = "/sys/fs/cgroup"
USER_HOME = f"/home/{CONFIG['USER']}"
USER_PATH = f"{USER_HOME}/.local/bin:{USER_HOME}/.bun/bin:/usr/local/bin:/usr/bin:/bin"
IS_ROOT = os.geteuid() == 0


# ----------------------------------------------------------------------------- logging
class Log:
    def __init__(self, path):
        self.path = path
        self.fh = None
        for candidate in (path, os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "mem-sentinel.log")):
            try:
                os.makedirs(os.path.dirname(candidate), exist_ok=True)
                self.fh = open(candidate, "a", buffering=1)
                self.path = candidate
                break
            except OSError:
                continue
        self.echo = CONFIG["DRY_RUN"] or sys.stderr.isatty()
        syslog.openlog("mem-sentinel", syslog.LOG_PID, syslog.LOG_USER)

    def __call__(self, msg, prio=syslog.LOG_INFO):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
        if self.fh:
            try:
                self.fh.write(line + "\n")
                os.fdatasync(self.fh.fileno())   # must survive the reset that ends a thrash
            except OSError:
                pass
        if self.echo:
            sys.stderr.write(line + "\n")
        try:
            syslog.syslog(prio, msg)
        except OSError:
            pass


# ----------------------------------------------------------------------------- readers
def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def meminfo(proc_root="/proc"):
    out = {}
    txt = read_file(f"{proc_root}/meminfo") or ""
    for line in txt.splitlines():
        k, _, rest = line.partition(":")
        if rest.split():
            out[k] = int(rest.split()[0]) * 1024
    return out


def psi_full_avg10(proc_root="/proc"):
    txt = read_file(f"{proc_root}/pressure/memory") or ""
    for line in txt.splitlines():
        if line.startswith("full"):
            for tok in line.split():
                if tok.startswith("avg10="):
                    return float(tok[6:])
    return 0.0


def proc_snapshot(proc_root="/proc", page=None):
    """pid -> (ppid, comm, anon_bytes, cache_bytes) in one pass over /proc.

    anon = statm resident - shared; shared is file-backed (page cache) + shmem resident pages,
    reported as cache. ~2-3 ms per 100 pids.
    """
    page = page or os.sysconf("SC_PAGE_SIZE")
    procs = {}
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        stat = read_file(f"{proc_root}/{name}/stat")
        statm = read_file(f"{proc_root}/{name}/statm")
        if not stat or not statm:
            continue
        lp, rp = stat.find("("), stat.rfind(")")
        comm = stat[lp + 1:rp]
        fields = stat[rp + 2:].split()
        try:
            resident, shared = (int(x) for x in statm.split()[1:3])
            procs[int(name)] = (int(fields[1]), comm, max(resident - shared, 0) * page, shared * page)
        except (IndexError, ValueError):
            continue
    return procs


def descendants(root, children, stop=frozenset()):
    """root and its process subtree; a pid in `stop` (another live omp root) and its subtree are left out."""
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(c for c in children.get(p, ()) if c not in stop)
    return out


def ancestors(pid, procs):
    out, seen = [], set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        pid = procs[pid][0]
        out.append(pid)
    return out


def cgroup_mem(path):
    """Footprint of one cgroup from memory.stat: anon + shmem + swap, never page cache.

    memory.current also counts file pages (cache), which the kernel reclaims under pressure;
    a session that read 10 GB of the tree is not using 10 GB. In memory.stat, shmem is part of
    `file`, so cache = file - shmem.
    """
    stat = {}
    for line in (read_file(f"{path}/memory.stat") or "").splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            stat[k] = int(v)
    swap_txt = (read_file(f"{path}/memory.swap.current") or "").strip()
    anon, shmem = stat.get("anon", 0), stat.get("shmem", 0)
    swap = int(swap_txt) if swap_txt.isdigit() else 0
    return dict(anon=anon, shmem=shmem, swap=swap, cache=max(stat.get("file", 0) - shmem, 0),
                footprint=anon + shmem + swap)


def docker_bytes(cg_root=CG_ROOT):
    limited = f"{cg_root}/docker-limited.slice"
    if os.path.isdir(limited):
        return cgroup_mem(limited)["footprint"]
    return sum(cgroup_mem(p)["footprint"] for p in glob.glob(f"{cg_root}/system.slice/docker-*.scope"))


# ----------------------------------------------------------------------------- decisions
def session_reasons(sessions, growths, avail, total, swap_free, swap_total, fall):
    """key -> [reason] for every session that should be steered; {} while the machine is not short.

    Pure: sessions[key]["footprint"] is anon+shmem+swap bytes, growths[key] the footprint growth
    over GROWTH_WINDOW_S, fall the MemAvailable fall rate in bytes/min (positive = falling).
    Level reasons (a)/(d)/(e) blame whoever is largest, so they need low headroom; a fast fall
    alone only implicates a session whose own footprint is growing, (b)/(c).
    """
    avail_pct = avail * 100 / total
    low = avail_pct < CONFIG["LOW_AVAIL_PCT"]
    if not low and fall < CONFIG["FALL_GB_PER_MIN"] * GB:
        return {}
    used = max(total - avail, 1)
    swap_pct = swap_free * 100 / swap_total if swap_total else 100.0
    reasons = {}

    def flag(key, why):
        reasons.setdefault(key, []).append(why)

    ranked = sorted(sessions.items(), key=lambda kv: kv[1]["footprint"], reverse=True)
    if ranked and low:
        top_key, top = ranked[0]
        share = top["footprint"] * 100 / used
        if share >= CONFIG["TOP_SHARE_PCT"] or top["footprint"] >= CONFIG["TOP_ABS_GB"] * GB:
            flag(top_key, f"(a) avail {avail_pct:.0f}% < {CONFIG['LOW_AVAIL_PCT']:.0f}% and largest session holds {share:.0f}% of used")
    if low and swap_pct < CONFIG["SWAPFREE_PCT"] and sessions:
        swapper_key, swapper = max(sessions.items(), key=lambda kv: kv[1]["swap"])
        if swapper["swap"] >= CONFIG["SWAP_BLAME_GB"] * GB:
            flag(swapper_key, f"(d) SwapFree {swap_pct:.0f}% < {CONFIG['SWAPFREE_PCT']:.0f}% and this session "
                              f"holds {swapper['swap'] / GB:.1f} GB of swap, the most")
    for key, g in growths.items():
        if g >= CONFIG["GROWTH_GB"] * GB:
            flag(key, f"(b) grew {g / GB:.1f} GB in {CONFIG['GROWTH_WINDOW_S']:.0f} s")
    # (c) only when headroom is actually shrinking: with 30+ GB free the 30 s slope is noise
    # (seen 2026-10-12 02:25: -1.4 -> 8.5 -> 0.7 GB/min between ticks, three false steers).
    if fall >= CONFIG["FALL_GB_PER_MIN"] * GB and growths and avail_pct < 2 * CONFIG["LOW_AVAIL_PCT"]:
        fastest = max(growths, key=growths.get)
        if growths[fastest] >= GB:
            flag(fastest, f"(c) MemAvailable falling {fall / GB:.1f} GB/min, fastest-growing session")
    for key, s in sessions.items():
        if low and s["footprint"] >= CONFIG["SESSION_MAX_GB"] * GB:
            flag(key, f"(e) session uses {s['footprint'] / GB:.1f} GB > {CONFIG['SESSION_MAX_GB']:.0f} GB")
    return reasons


def heavy_children(s, procs, n=3):
    """Top-n (anon, comm, pid) of the session's processes that are not its omp root or above it.

    Killing the root omp (or the launcher that owns it) ends the whole session and loses its work;
    the steer must point at the command the session started.
    """
    excluded = set(s["roots"])
    for root in s["roots"]:
        excluded.update(ancestors(root, procs))
    rows = sorted(((procs[p][2], procs[p][1], p) for p in s["pids"] if p in procs and p not in excluded),
                  reverse=True)
    return [r for r in rows if r[0] > 0][:n]


# ----------------------------------------------------------------------------- sessions
class Sessions:
    """Live omp sessions as `tg-ctl omp-live status --json` (what omp-steer list prints) reports them.

    The tg-ctl round trip runs off the sampling thread: a wedged tg-ctl during the very thrash the
    sentinel exists for must not stop the 2 s sampler (or the critical broadcast) for its 20 s
    timeout. Ticks read the last good map; `by_pid` is swapped in one assignment.
    """

    def __init__(self, log):
        self.log = log
        self.by_pid = {}
        self.fetched = -1e9
        self.failed_once = False
        self.running = None    # the in-flight fetch thread, if any

    def refresh(self, now):
        if now - self.fetched < CONFIG["SESSIONS_TTL"] or (self.running and self.running.is_alive()):
            return
        self.fetched = now
        self.running = threading.Thread(target=self.fetch, daemon=True)
        self.running.start()

    def fetch(self):
        try:
            r = subprocess.run(user_argv(["tg-ctl", "omp-live", "status", "--json"]),
                               capture_output=True, text=True, timeout=20)
            if r.returncode != 0:     # tg-ctl reports failure as rc 1 + empty stdout: not "no sessions"
                raise ValueError(f"rc={r.returncode} {(r.stderr or '').strip()[:200]}")
            hosts = json.loads(r.stdout or "[]")
            self.by_pid = {int(h["pid"]): h for h in hosts if "pid" in h}
            self.failed_once = False
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            if not self.failed_once:   # keep the last good map; do not spam the log while tg-ctl is down
                self.log(f"sessions: omp-live status failed ({e.__class__.__name__}: {e}); keeping {len(self.by_pid)} cached",
                         syslog.LOG_WARNING)
            self.failed_once = True


def user_argv(argv):
    if IS_ROOT:
        return ["runuser", "-u", CONFIG["USER"], "--", "env", f"PATH={USER_PATH}", f"HOME={USER_HOME}"] + argv
    return argv


# ----------------------------------------------------------------------------- the sentinel
class Sentinel:
    def __init__(self, log=None, sessions=None, proc_root="/proc", cg_root=CG_ROOT):
        self.log = Log(CONFIG["LOG"]) if log is None else log
        self.sessions = Sessions(self.log) if sessions is None else sessions
        self.proc_root = proc_root
        self.cg_root = cg_root
        self.omp_slice = f"{cg_root}/user.slice/user-{CONFIG['UID']}.slice/user@{CONFIG['UID']}.service/omp.slice"
        self.rings = {}            # session key -> deque[(ts, footprint)]
        self.sys_ring = deque()    # (ts, avail)
        self.last_steer = {}       # key -> (ts, reason codes, footprint)
        self.steer_count = {}      # key -> n
        self.last_broadcast = -1e9
        self.last_heartbeat = -1e9
        self.desktop_env = None
        self.mem_total = meminfo(proc_root).get("MemTotal", 48 * GB)

    # -- sampling -------------------------------------------------------------------------
    def outermost(self, owners):
        """The scope's own omp: the one with no other live omp above it (a nested omp shares its
        parent's scope, and after pid wrap its pid can be the smaller one). Reads only the
        owners' ancestor chains, not all of /proc."""
        if len(owners) < 2:
            return owners[0] if owners else None
        others = set(owners)
        nested, unresolved = set(), []
        for pid in owners:
            p, seen = pid, set()
            while p > 1 and p not in seen:
                seen.add(p)
                stat = read_file(f"{self.proc_root}/{p}/stat") or ""
                try:
                    p = int(stat[stat.rfind(")") + 2:].split()[1])
                except (IndexError, ValueError):
                    unresolved.append(pid)   # chain broke (a process exited mid-walk): unknown
                    break
                if p in others:
                    nested.add(pid)
                    break
            else:
                return pid
        # no chain reached init cleanly: prefer one whose chain broke over one proven nested
        return next((p for p in unresolved if p not in nested), owners[0])

    def sample(self, now):
        """(procs, sessions); procs is the /proc snapshot, or None when no session needed it.

        Scoped sessions are measured from their cgroup alone; the full /proc scan runs only for
        legacy (unscoped) sessions here, or later when a steer has to name a process.
        """
        self.sessions.refresh(now)
        hosts = self.sessions.by_pid   # one snapshot: the fetch thread swaps the map, never mutates it
        omp_pids = set(hosts)
        # key -> dict(footprint, anon, shmem, swap, cache, pids, roots, steer_pid, host, label); `host` is
        # the discovery record (sessionId, cwd, title) the steer is delivered to and keyed by
        sessions = {}
        claimed = set()      # omp pids owned by a scope
        scoped = set()       # every pid inside a scope: a legacy tree must not count them twice
        for scope in sorted(glob.glob(f"{self.omp_slice}/omp-*.scope")):
            name = os.path.basename(scope)
            pids = [int(p) for p in (read_file(f"{scope}/cgroup.procs") or "").split()]
            owners = sorted(omp_pids.intersection(pids))
            claimed.update(owners)
            scoped.update(pids)
            steer_pid = self.outermost(owners)
            sessions[name] = dict(cgroup_mem(scope), pids=pids, roots=owners, steer_pid=steer_pid,
                                  host=hosts.get(steer_pid) or {}, label=name)
        legacy = sorted(omp_pids - claimed)
        procs = proc_snapshot(self.proc_root) if legacy else None
        children = {}
        for pid, (ppid, _, _, _) in (procs or {}).items():
            children.setdefault(ppid, []).append(pid)
        for pid in legacy:     # no scope: attribute by process tree
            tree = [p for p in descendants(pid, children, omp_pids - {pid}) if p not in scoped and p in procs]
            anon = sum(procs[p][2] for p in tree)
            sessions[f"pid-{pid}"] = dict(footprint=anon, anon=anon, shmem=0, swap=0,
                                          cache=sum(procs[p][3] for p in tree), pids=tree, roots=[pid],
                                          steer_pid=pid, host=hosts[pid], label=f"legacy omp pid {pid}")
        for key, s in sessions.items():
            ring = self.rings.setdefault(key, deque())
            ring.append((now, s["footprint"]))
            while ring and now - ring[0][0] > CONFIG["RING_S"]:
                ring.popleft()
        for key in list(self.rings):
            if key not in sessions:
                del self.rings[key]
        return procs, sessions

    def growth(self, key, now, window):
        ring = self.rings.get(key)
        if not ring or len(ring) < 2:
            return 0.0
        cur = ring[-1][1]
        base = cur
        for ts, b in ring:
            if now - ts <= window:
                base = b
                break
        return cur - base

    def fall_rate(self, now):
        """MemAvailable fall in bytes/min over the last ~30 s (positive = falling)."""
        if len(self.sys_ring) < 2:
            return 0.0
        oldest = None
        for ts, a in self.sys_ring:
            if now - ts <= 30:
                oldest = (ts, a)
                break
        if not oldest or now - oldest[0] < 8:
            return 0.0
        dt = now - oldest[0]
        return (oldest[1] - self.sys_ring[-1][1]) / dt * 60

    # -- decisions ------------------------------------------------------------------------
    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        mi = meminfo(self.proc_root)
        total, avail = mi["MemTotal"], mi["MemAvailable"]
        swap_total, swap_free = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
        psi = psi_full_avg10(self.proc_root)
        self.sys_ring.append((now, avail))
        while self.sys_ring and now - self.sys_ring[0][0] > CONFIG["RING_S"]:
            self.sys_ring.popleft()
        procs, sessions = self.sample(now)
        fall = self.fall_rate(now)
        avail_pct = avail * 100 / total
        swap_pct = swap_free * 100 / swap_total if swap_total else 100.0

        growths = {k: self.growth(k, now, CONFIG["GROWTH_WINDOW_S"]) for k in sessions}
        reasons = session_reasons(sessions, growths, avail, total, swap_free, swap_total, fall)
        heartbeat = CONFIG["HEARTBEAT_S"] and now - self.last_heartbeat >= CONFIG["HEARTBEAT_S"]
        # /proc and docker cgroups are only read when something will be reported: the quiet path
        # stays a meminfo read plus one memory.stat per omp scope
        if reasons and procs is None:
            procs = proc_snapshot(self.proc_root)
        docker = docker_bytes(self.cg_root) if reasons or heartbeat else 0
        for key, why in reasons.items():
            self.steer(key, sessions[key], growths[key], avail, fall, docker, procs, why, now)
        # a session that stayed quiet for REPEAT_COOLDOWN_S (or is gone) starts again at #1
        for key in [k for k, (ts, *_) in self.last_steer.items()
                    if k not in sessions or (k not in reasons and now - ts >= CONFIG["REPEAT_COOLDOWN_S"])]:
            del self.last_steer[key]
            self.steer_count.pop(key, None)

        ranked = sorted(sessions.items(), key=lambda kv: kv[1]["footprint"], reverse=True)
        if avail_pct < CONFIG["CRIT_AVAIL_PCT"] or psi > CONFIG["PSI_FULL_AVG10"]:
            self.broadcast(avail, fall, psi, ranked, now)

        if heartbeat:
            self.last_heartbeat = now
            tops = " ".join(f"{k}={v['footprint'] / GB:.1f}G(cache {v['cache'] / GB:.1f}G)" for k, v in ranked[:4])
            self.log(f"status avail={avail / GB:.1f}G ({avail_pct:.0f}%) swapfree={swap_free / GB:.1f}G ({swap_pct:.0f}%) "
                     f"psi_full10={psi:.1f} fall={fall / GB:.1f}G/min docker={docker / GB:.1f}G sessions={len(sessions)} {tops}")

    def message(self, s, growth, avail, fall, docker, procs, nth, why):
        rows = heavy_children(s, procs)
        top = ", ".join(f"{comm} pid {pid} {anon / GB:.1f}G" for anon, comm, pid in rows)
        if docker >= 0.5 * GB:
            top = f"{top}, docker {docker / GB:.1f}G" if top else f"docker {docker / GB:.1f}G"
        if rows and rows[0][0] >= CONFIG["KILL_MIN_GB"] * GB:
            action = (f"Stop or kill the heavy child process now (kill {rows[0][2]}, {rows[0][1]}; never your own "
                      f"omp pid {s['steer_pid']})")
        else:
            action = ("No single child process is heavy: stop the command that started the growth (test suite, "
                      "type checker, docker build), not your own omp session")
        if nth <= 1:
            prefix = "[mem-sentinel]"
        else:
            prefix = f"[mem-sentinel] WARNING #{nth}{', still growing' if growth >= 0.25 * GB else ''} -"
        return (f"{prefix} your session holds {s['footprint'] / GB:.1f} GB (anon {s['anon'] / GB:.1f} + shmem "
                f"{s['shmem'] / GB:.1f} + swap {s['swap'] / GB:.1f}; page cache {s['cache'] / GB:.1f} GB not counted, "
                f"reclaimable; heavy children: {top or 'none'}), {growth / GB:+.1f} GB in the last "
                f"{CONFIG['GROWTH_WINDOW_S']:.0f} s; machine has {avail / GB:.1f} GB of {self.mem_total / GB:.0f} "
                f"available, falling {max(fall, 0) / GB:.1f} GB/min. Why: {'; '.join(why)}. {action} and move it "
                f"to the server (skill://server-offload, server-run -- ...); at 4 % free earlyoom kills the largest "
                f"process without asking.")

    def steer(self, key, s, growth, avail, fall, docker, procs, why, now):
        codes = tuple(sorted(w.split(")", 1)[0] + ")" for w in why))
        last = self.last_steer.get(key)
        recipient = (s["steer_pid"], s["host"].get("sessionId"))
        if last and last[3] != recipient:
            # the recipient changed (typically: discovery found the scope's omp after a tick with no
            # one to deliver to): what was logged before never reached this session, start over
            last = None
            self.steer_count.pop(key, None)
        if last:
            since, same_codes, same_size = now - last[0], last[1] == codes, abs(last[2] - s["footprint"])
            if since < CONFIG["STEER_COOLDOWN_S"]:
                return
            if same_codes and same_size < CONFIG["REPEAT_DELTA_GB"] * GB and since < CONFIG["REPEAT_COOLDOWN_S"]:
                return
        self.last_steer[key] = (now, codes, s["footprint"], recipient)
        nth = self.steer_count[key] = self.steer_count.get(key, 0) + 1
        text = self.message(s, growth, avail, fall, docker, procs, nth, why)
        host = s["host"]
        who = f"{s['label']} omp={s['steer_pid']} cwd={host.get('cwd', '?')} title={host.get('title', '')!r}"
        verb = "would-steer" if CONFIG["DRY_RUN"] else "steer"
        if recipient[1] is None:
            verb += "(no live omp session in this scope)"
        self.log(f"{verb} #{nth} {who} reasons={'; '.join(why)} msg={text}", syslog.LOG_WARNING)
        if not CONFIG["DRY_RUN"] and recipient[1] is not None:
            # exact pid, not `omp-steer <pid>`: omp-steer also matches the target as a cwd
            # substring, so a pid that appears in another session's path would warn that one too
            pid = s["steer_pid"]
            session = ["--session", host["sessionId"]]
            self.run_bg(["tg-ctl", "omp-live", "send", str(pid), "--request-id",
                         f"mem-sentinel-{int(time.time() * 1000)}-{pid}", *session, "--", text], f"steer {key}")

    def broadcast(self, avail, fall, psi, ranked, now):
        if now - self.last_broadcast < CONFIG["BROADCAST_COOLDOWN_S"]:
            return
        self.last_broadcast = now
        tops = ", ".join(f"{self.sessions.by_pid.get(v['steer_pid'], {}).get('cwd', k)} {v['footprint'] / GB:.1f}G "
                         f"(anon {v['anon'] / GB:.1f}G)" for k, v in ranked[:3])
        text = (f"[mem-sentinel] MACHINE CRITICAL: {avail / GB:.1f} GB of {self.mem_total / GB:.0f} free, falling "
                f"{max(fall, 0) / GB:.1f} GB/min, memory stall {psi:.0f}%. Largest sessions: {tops or 'none attributed'}. "
                f"Every session: stop test suites, type checkers and docker builds NOW; earlyoom kills at 4 % free.")
        verb = "would-broadcast" if CONFIG["DRY_RUN"] else "broadcast"
        self.log(f"{verb} {text}", syslog.LOG_CRIT)
        if CONFIG["DRY_RUN"]:
            return
        self.run_bg(["omp-steer", "all", text], "broadcast")
        self.run_bg(["logger", "-p", "user.crit", "-t", "mem-sentinel", text], "logger", as_user=False)
        env = self.find_desktop_env()
        if env:
            self.run_bg(["notify-send", "-u", "critical", "-a", "mem-sentinel", "Memory critical", text], "notify-send", extra_env=env)

    def find_desktop_env(self):
        if self.desktop_env is not None:
            return self.desktop_env or None
        env = {}
        bus = f"/run/user/{CONFIG['UID']}/bus"
        if os.path.exists(bus):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        for name in os.listdir("/proc"):      # borrow DISPLAY/WAYLAND_DISPLAY from any desktop process of the user
            if not name.isdigit():
                continue
            try:
                if os.stat(f"/proc/{name}").st_uid != CONFIG["UID"]:
                    continue
                with open(f"/proc/{name}/environ", "rb") as f:
                    for kv in f.read().split(b"\0"):
                        if kv.startswith((b"DISPLAY=", b"WAYLAND_DISPLAY=", b"XDG_RUNTIME_DIR=")):
                            k, _, v = kv.decode(errors="replace").partition("=")
                            env.setdefault(k, v)
                if "DISPLAY" in env or "WAYLAND_DISPLAY" in env:
                    break
            except OSError:
                continue
        self.desktop_env = env if "DBUS_SESSION_BUS_ADDRESS" in env else {}
        return self.desktop_env or None

    def run_bg(self, argv, what, as_user=True, extra_env=None):
        """Delivery runs off-thread: omp-steer is a tg-ctl round trip and the loop must not stall."""
        cmd = user_argv(argv) if as_user else argv
        if extra_env and IS_ROOT:
            cmd = cmd[:cmd.index("env") + 1] + [f"{k}={v}" for k, v in extra_env.items()] + cmd[cmd.index("env") + 1:]

        def run():
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=40,
                                   env={**os.environ, **(extra_env or {})} if not IS_ROOT else None)
                out = (r.stdout.strip() or r.stderr.strip()).replace("\n", " | ")[:300]
                self.log(f"{what}: rc={r.returncode} {out}")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log(f"{what}: failed {e.__class__.__name__}: {e}", syslog.LOG_ERR)
        threading.Thread(target=run, daemon=True).start()

    # -- main loop ------------------------------------------------------------------------
    def run(self):
        lock_memory(self.log)
        self.log(f"start uid={os.geteuid()} dry_run={CONFIG['DRY_RUN']} log={self.log.path} slice={self.omp_slice} "
                 + " ".join(f"{k}={v}" for k, v in CONFIG.items() if k not in ("LOG", "DRY_RUN")))
        while True:
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as e:   # a sampling bug must never stop the watchdog
                self.log(f"tick error {e.__class__.__name__}: {e}", syslog.LOG_ERR)
            time.sleep(max(0.2, CONFIG["INTERVAL"] - (time.monotonic() - t0)))


def lock_memory(log):
    """Pin our pages so the sentinel keeps sampling while everything else is being swapped (needs root)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.mlockall(1 | 2) != 0:      # MCL_CURRENT | MCL_FUTURE
            log(f"mlockall unavailable (errno {ctypes.get_errno()}); continuing unpinned")
    except (OSError, AttributeError) as e:
        log(f"mlockall unavailable ({e}); continuing unpinned")


if __name__ == "__main__":
    Sentinel().run()

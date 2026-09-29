# agenttools-gpu-slots

A **machine-wide pool of GPU-model slots**, shared by every agent and harness on the machine
(agent-tools#817). Each running model (whisper, llama.cpp, mlx) holds one slot for its whole
lifetime; when every slot is taken, the next model waits.

Added after 2026-09-29: thirteen concurrent `whisper-cli` runs, each loading the 1.6 GB
large-v3-turbo model into Metal buffers, wired 24.65 of 25.77 GB and kernel-panicked the Mac.
GPU buffers are wired memory, so the jetsam pressure level stayed "normal" throughout, and
nothing capped how many model loads ran at once.

## Use

```bash
# Wait for a free slot (up to 30 min), then run the model command holding it.
python3 <agent-tools>/lib/agenttools_gpu_slots run -- whisper-cli -m ggml-large-v3-turbo.bin -f a.wav
python3 <agent-tools>/lib/agenttools_gpu_slots run --timeout 0 -- stt a.wav   # fail at once if busy

# Who holds the slots right now.
python3 <agent-tools>/lib/agenttools_gpu_slots status
```

Running the package directory needs no install. `python3 -m agenttools_gpu_slots` and the
`agenttools-gpu-slot` script (after `pip install -e lib/agenttools_gpu_slots`) are the same CLI.

`run` acquires a slot and then **execs** the command in place, so the lock is held by the model
process itself: the slot frees when the model exits, never earlier (a dead wrapper cannot leave a
model running outside the pool, because there is no separate wrapper process).

The wrapper holds ONE slot for the whole command, and everything the command starts shares it
(see re-entrancy below). Wrap each model run, not a script that starts several models in
parallel: `run -- bash -c 'stt a.wav & stt b.wav & wait'` runs two models on one slot.

| Exit              | Meaning                                                                   |
| ----------------- | ------------------------------------------------------------------------- |
| the command's own | the command ran (the wrapper process became it)                           |
| 2                 | usage error (no command, negative `--timeout`)                            |
| 75                | every slot stayed busy for `--timeout` seconds (EX_TEMPFAIL: retry later) |
| 126 / 127         | the command is not executable / not found                                 |

## Configuration

| Env                | Default                                | Meaning                                                                  |
| ------------------ | -------------------------------------- | ------------------------------------------------------------------------ |
| `RIG_GPU_SLOTS`    | `2`                                    | slots on this machine (an integer >= 1; anything else means the default) |
| `RIG_GPU_SLOT_DIR` | `~/.local/state/agent-tools/gpu-slots` | where the lock files live                                                |

Set both the same way for every agent and harness on the machine (a shell profile, not a
per-session export): the pool is only machine-wide when every holder and every probe agrees on
the directory and the count.

## Protocol (shared with stt-cli)

stt-cli acquires slots itself with the same protocol, so `stt` runs and wrapped runs draw from
the same pool. Change it only in lockstep with stt-cli.

- Lock files `slot-0.lock` … `slot-{N-1}.lock` in the slot dir (dir mode 0700, files 0600).
- Acquire: `open(O_RDWR|O_CREAT)` + `flock(LOCK_EX|LOCK_NB)` on each slot in turn; poll every
  0.25 s while all are busy. The holder then writes `pid=<pid> argv=<argv>` into the file
  (diagnostics only; the lock is the truth).
- Hold: keep the fd open for the model's lifetime. A child doing the real GPU work inherits it
  (`pass_fds`), so the slot stays taken until the model process itself exits. The wrapper's
  exec'd command passes the fd to every descendant, so a background process it leaves behind
  (`… & disown`, a forked server) keeps the slot until that process exits too.
- Re-entrancy: the holder exports `RIG_GPU_SLOT_FD=<fd>`. A nested acquirer whose inherited fd is
  one of the slot files (same device and inode) reuses that slot instead of taking a second one,
  so `wrapper run -- stt …` holds one slot, not two. A stale or unrelated fd number never bypasses
  the pool.

## Why flock, and why nothing can go stale

A lock taken with `flock` belongs to the open file description, and the kernel releases it the
instant the last process holding that description exits: normal exit, crash, SIGKILL, a harness
torn down mid-run. There are no pid files, counters or reapers, so there is no stale slot to
clean up and no way for a killed agent to deadlock the others. The directory and files persist,
but an unlocked file is simply a free slot.

The `heavy-op-memory-gate` pre-bash hook probes this pool (without waiting and without keeping a
lock) and refuses to start an un-queued GPU model load while every slot is taken; see
[its README](../../agent-hooks/heavy-op-memory-gate/README.md).

## Test

`uv run --with pytest python -m pytest tests/test_agenttools_gpu_slots.py -q`: five concurrent
holders in separate processes never run more than two at once, a SIGKILLed holder's slot is free
at once, a waiter picks up a killed holder's slot, the timeout exit, nested re-entrancy, and a
stale `RIG_GPU_SLOT_FD`. POSIX only (`fcntl.flock`); stdlib only.

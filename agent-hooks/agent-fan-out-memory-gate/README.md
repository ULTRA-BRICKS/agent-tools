# agent-fan-out-memory-gate

**Point:** `pre-agent` · **Fail policy:** `open` · **Priority:** 35

Refuses a new subagent dispatch while the machine is already under memory pressure (jetsam WARN+,
low free RAM, or high swap use). Configured via `agent_hooks.memory_gate` in the repo's `rig.yaml`
or `RIG_MEMORY_*` environment overrides.

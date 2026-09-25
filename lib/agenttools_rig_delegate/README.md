# agenttools-rig-delegate — one rig-aware `install-hooks` decision for every CLI

Every ecosystem CLI (`tg-ctl`, `review`, …) ships an `install-hooks`-style command that
writes agent-harness hooks directly (`~/.claude/settings.json`, `~/.codex/hooks.json`, …).
When [`rig`](https://git.hyperide.ai/ultrabricks/rig-cli) is also installed, those direct
writers become a **second** source of truth for the same hooks — the exact duplication
that makes codex warn `loading hooks from both …` and lets two tools fight over one file.

This lib is the single decision each such command makes at its top:

```
rig present  ->  DELEGATE to rig (rig owns the hooks; run `rig apply` / `rig config set`)
rig absent   ->  run the tool's own direct installer (the FALLBACK)
```

## Python CLIs

```python
import agenttools_rig_delegate as rd

def install_hooks() -> int:
    res = rd.delegate_or_fallback(["apply"], fallback=_install_hooks_directly)
    return res.returncode
```

- `find_rig()` / `rig_available()` — robust detection (PATH + well-known bins + `RIG_BIN`).
- `delegate(rig_args)` — run `rig <args>`, return the outcome (raises if rig absent).
- `delegate_or_fallback(rig_args, fallback)` — the decision above. A rig that is present
  **but fails** surfaces its exit code; it never silently falls back (that would re-create
  the double-write we set out to remove).

## Non-Python CLIs (e.g. `tg-ctl`, a bun/TS binary)

They cannot import Python, so they shell out to the CLI mirror:

```sh
if PYTHONPATH="$AGENT_TOOLS/lib" python3 -m agenttools_rig_delegate detect >/dev/null; then
  PYTHONPATH="$AGENT_TOOLS/lib" python3 -m agenttools_rig_delegate delegate apply
else
  install_hooks_directly            # fallback
fi
```

- `detect` — exit 0 (+ prints the rig path) if rig is present, exit 1 if absent.
- `delegate [RIG_ARG …]` — run `rig <RIG_ARG …>`, exit with rig's own code; exit `97`
  (the `NO_RIG_EXIT` sentinel) if rig is absent so the caller runs its own fallback. `97`
  is chosen to sit clear of rig's whole public exit contract (`0-8`, e.g. `3` ==
  `EXIT_DRIFT`, and `127` == missing dep) so a real `rig apply` drift/failure is never
  misread as "no rig".

Keep the library and the CLI mirror in lock-step (`__init__.py` / `__main__.py`).

## Stdlib-only

Zero runtime dependencies. Imported/run inside hook and CLI subprocesses, so it must load
fast and never pull in a third-party import (mirrors the ecosystem's stdlib-at-import rule).

#!/usr/bin/env python3
"""wire_session_end_hook.py — add (or remove) the agent-browser SessionEnd hook entry
in Claude Code's settings.json.

Claude Code only runs hooks declared in `settings.json`; it does NOT read the
agents-hooks/v1 descriptors in `~/.claude/hooks` directly, and `lib/cc_hook_bridge`
(the carrier that does) has no `session-end` point — it dispatches only PreToolUse,
PostToolUse and Stop. So the SessionEnd cleanup script has to be wired straight into
settings.json, which is what this does.

Editing settings.json in place is done carefully because it is the user's live harness
configuration:
  * a timestamped backup is written next to it before any change, matching the
    `.rig-bak-<stamp>` convention already used in that directory;
  * the file is parsed as JSON and re-serialised — never regex-patched;
  * the entry is keyed on the script path, so re-running replaces exactly one entry
    and leaves every other hook untouched;
  * `--remove` takes it back out.

Claude Code's default hook timeout is 1.5s and SessionEnd cannot block anything, so an
explicit `"timeout": 30` (seconds) is set — closing a browser takes a second or two and
a truncated run would leave the session open.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

MARKER = "session_end_close.py"
TIMEOUT_S = 30


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"wire_session_end_hook: cannot parse {path}: {exc}")
    return data if isinstance(data, dict) else {}


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.agent-browser-bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


def _without_our_entries(groups: list) -> list:
    """Every SessionEnd group except the ones this script previously installed."""
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = [h for h in group.get("hooks", [])
                 if not (isinstance(h, dict) and MARKER in str(h.get("command", "")))]
        if hooks:
            kept.append({**group, "hooks": hooks})
        elif not group.get("hooks"):
            kept.append(group)
    return kept


def _entry(python: str, script: str) -> dict:
    return {"hooks": [{"type": "command",
                       "command": f"{python} {script}",
                       "timeout": TIMEOUT_S}]}


def apply(settings_path: Path, python: str, script: str, remove: bool) -> None:
    data = _load(settings_path)
    hooks = data.setdefault("hooks", {}) if isinstance(data.get("hooks", {}), dict) else {}
    data["hooks"] = hooks
    groups = hooks.get("SessionEnd")
    groups = _without_our_entries(groups if isinstance(groups, list) else [])
    if not remove:
        groups.append(_entry(python, script))
    if groups:
        hooks["SessionEnd"] = groups
    else:
        hooks.pop("SessionEnd", None)
    backup = _backup(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    action = "removed from" if remove else "wired into"
    print(f"wire_session_end_hook: SessionEnd hook {action} {settings_path}"
          + (f" (backup: {backup.name})" if backup else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--settings", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--script", default="")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    if not args.remove and not args.script:
        parser.error("--script is required unless --remove is given")
    apply(Path(args.settings), args.python, args.script, args.remove)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Account-scoped state paths shared by CLI and HA startup.

Workspace names identify separate accounts; legacy unscoped files require an
offline reconciliation so stops, budgets and uncertain sends cannot be lost.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


def state_path(
    workspace: str, explicit: Path | None = None, *, ha: bool = False
) -> Path:
    if workspace not in ("home", "glide.vc"):
        raise ValueError("Workspace must be home or glide.vc")
    override = explicit or os.environ.get("CHATGPT_GUARDIAN_STATE")
    if override:
        return Path(override)
    config = (
        Path.home() / ".config/agent-tools/chatgpt-guardian" / (workspace + ".json")
    )
    if config.exists() or config.is_symlink():
        try:
            selected = json.loads(config.read_text())
            if (
                not isinstance(selected, dict)
                or set(selected) != {"workspace", "state"}
                or selected["workspace"] != workspace
                or not isinstance(selected["state"], str)
            ):
                raise ValueError("Invalid workspace state configuration")
            path = Path(selected["state"])
            if not path.is_absolute() or not path.is_file():
                raise ValueError(
                    "Configured state must be an absolute existing database file"
                )
            # Read-only validation cannot initialize an empty replacement. Store
            # still enforces the persisted account identity before using it.
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("SELECT account FROM account_scope").fetchall() != [
                    (workspace,)
                ]:
                    raise ValueError("Configured state belongs to a different account")
                db.execute("SELECT key,value FROM controls LIMIT 0")
                db.execute("SELECT chat FROM consent LIMIT 0")
                db.execute(
                    "SELECT chat,turn,generation,wall,state FROM attempts LIMIT 0"
                )
            return path
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ValueError("Invalid configured workspace state: " + str(exc)) from exc
    root = (
        Path("/data/guardian-state")
        if ha
        else Path.home() / ".local/state/agent-tools/chatgpt-guardian"
    )
    legacy = Path("/data/guardian.sqlite3") if ha else root / "state.sqlite"
    if legacy.exists():
        raise ValueError(
            "Legacy unscoped state exists; reconcile it offline by account before selecting scoped state"
        )
    return root / workspace / "state.sqlite"

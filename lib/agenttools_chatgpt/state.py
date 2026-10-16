"""Account-scoped state paths shared by CLI and HA startup.

Workspace names identify separate accounts; legacy unscoped files require an
offline reconciliation so stops, budgets and uncertain sends cannot be lost.
"""

from __future__ import annotations

import os
from pathlib import Path


def state_path(
    workspace: str, explicit: Path | None = None, *, ha: bool = False
) -> Path:
    if workspace not in ("home", "glide.vc"):
        raise ValueError("Workspace must be home or glide.vc")
    override = explicit or os.environ.get("CHATGPT_GUARDIAN_STATE")
    if override:
        return Path(override)
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

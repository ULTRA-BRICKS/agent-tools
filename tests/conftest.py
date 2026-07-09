"""Shared pytest fixtures for the agent-tools suite.

The single most important one is `_hermetic_hatch_home`: it guarantees NO test can ever reach the
real `tg-ctl` (and message the human) via a developer's real `~/rig.yaml`. Since the shared hatch
lib now resolves the approval binary from the account's REAL home (`resolve_home`), a machine that
happens to have `~/rig.yaml` with `agent_hooks.tg_ctl_path` would let any hatch test that patches
only `_TRUSTED_TG_CTL_PATHS` invoke the real binary. Pointing `resolve_home` at a clean temp home
for EVERY test closes that hole centrally, for every current and future test module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import agenttools_hatch_escalation  # noqa: E402 - after the sys.path insert above


@pytest.fixture(autouse=True)
def _hermetic_hatch_home(tmp_path_factory, monkeypatch):
    """Point the hatch helper's `resolve_home` at a clean temp home (no rig.yaml) for every test,
    so tg-ctl resolves via each test's own patched candidates / `_TRUSTED_TG_CTL_PATHS` and NEVER
    reads the developer's real `~/rig.yaml`. Tests that deliberately exercise a home rig.yaml
    override re-monkeypatch `resolve_home` in their own body (applied later, so it wins). All hook
    modules import the SAME `agenttools_hatch_escalation` object, so patching it here covers them
    all."""

    clean_home = tmp_path_factory.mktemp("hermetic-home")
    monkeypatch.setattr(agenttools_hatch_escalation, "resolve_home", lambda: str(clean_home))

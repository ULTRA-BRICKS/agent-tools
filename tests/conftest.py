"""Shared pytest fixtures for the agent-tools suite.

The single most important one is `_hermetic_hatch_home`: it guarantees hatch approval tests cannot
reach the real `tg-ctl` (and message the human) via a developer's real home. Since the shared
hatch lib resolves the approval binary from the account's REAL home (`resolve_home`: the
machine-global rig config's `tg_ctl.tg_ctl_path`, then `~/.local/bin`, `~/bin`, ...), any hatch
test that patches only `_TRUSTED_TG_CTL_PATHS` would otherwise invoke the developer's real binary.
Pointing `resolve_home` at a clean temp home for tests closes that hole centrally; the exported
`AGENTTOOLS_TEST_HERMETIC_HOME` additionally makes the lib refuse any tg-ctl outside pytest's
basetemp, which covers hook SUBPROCESSES the in-process patch never reaches. The two explicit
real-OS-home tests below are exempt from the home patch; they only assert `resolve_home()`'s
source of truth and do not resolve or execute `tg-ctl`.

It also exports `AGENT_TOOLS_OVERRIDES_LOG` (a full file path under the same clean temp home) for
every test. That env var is the escape-hatch audit sink's subprocess-reachable override (see
`agenttools_hatch_escalation._resolve_overrides_log_path`): several hook tests run the hook
script as a genuine SUBPROCESS (`subprocess.run([sys.executable, hook_path], ...)`), which gets a
brand-new interpreter that the `pwd.getpwuid`/`resolve_home` monkeypatches below never reach — so
without this env var, any subprocess test that sets the hatch env var to ANY value (even a bare
`1` that never contacts tg-ctl) would append a real audit line to the DEVELOPER'S real
`~/.config/agent-tools/overrides.log`. `monkeypatch.setenv` mutates the real process `os.environ`,
so a subprocess built from `dict(os.environ)` inherits this override automatically.

The module-level call right below (`_strip_ambient_ship_env`) closes the same class of hole for
`ci/ship/ship.sh`'s many gate-control knobs (`SHIP_EXTERNAL_REVIEW`, `SHIP_REVIEW_QUORUM_ENABLED`,
`SHIP_SKIP_VERSION_BUMP`, ...). `tests/test_ship.py` builds each subprocess env as
`env = dict(os.environ)` and only pops a specific known set of ambient vars (`GH_REPO`,
`SHIP_TEST_DIFF`, ...) rather than every `SHIP_*`/`RIG_HATCH_REQUEST_SHIP_*` var, so a developer
shell that happens to export one of ship's gate switches silently changes what the test exercises
(caught live: this machine started exporting `SHIP_EXTERNAL_REVIEW=0` machine-wide, which disabled
the external-review gate in five tests written to exercise it as enabled). This is deliberately a
plain module-level `os.environ` mutation, run ONCE here at conftest import time, NOT a per-test
`monkeypatch.delenv` autouse fixture: `tests/test_ship.py` relies on its own module-level
`os.environ.setdefault("SHIP_TEST_DISABLE_REVIEW_QUORUM", "1")` (also present in
`tests/test_ship_notify_task_cli.py`) plus `os.environ["SHIP_TASK_NOTIFY_ENABLED"] = "0"`
(test_ship.py only), applied once at each module's own collection time, which a per-test fixture
would strip during every test body (monkeypatch only restores the pre-test value AFTER each test's
teardown) — reintroducing exactly the ambient-enabled gates those defaults exist to turn off. Since
conftest.py is always imported before the test modules in this directory are collected, stripping
here first lets those modules' own one-time defaults apply cleanly against a guaranteed-clean
starting point.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

try:
    import pwd
except ImportError:  # pragma: no cover - Windows compatibility for test collection.
    pwd = None

# Any ambient env var ship.sh treats as a gate-control knob or hatch-request override. Kept as a
# module constant (not inlined into the loop below) so tests/test_conftest_ship_env.py has a
# stable target to assert against.
_SHIP_ENV_PREFIXES = ("SHIP_", "RIG_HATCH_REQUEST_SHIP_")


def _strip_ambient_ship_env() -> None:
    for name in tuple(os.environ):
        if name.startswith(_SHIP_ENV_PREFIXES):
            os.environ.pop(name, None)


_strip_ambient_ship_env()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import agenttools_hatch_escalation  # noqa: E402 - after the sys.path insert above


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_os_home: test intentionally exercises the real OS account home resolver",
    )


@pytest.fixture(autouse=True)
def _hermetic_hatch_home(tmp_path_factory, monkeypatch, request):
    """Point the hatch helper's `resolve_home` at a clean temp home (no rig config) for every
    test, so tg-ctl resolves via each test's own patched candidates / `_TRUSTED_TG_CTL_PATHS` and
    NEVER reads the developer's real home. Tests that deliberately exercise a home rig config
    override re-monkeypatch `resolve_home` in their own body (applied later, so it wins). All hook
    modules may load fresh hatch helper objects by explicit file path, so this fixture also patches
    the process-wide pwd module that newly loaded helpers call and any hook-local helper objects
    already imported during test-module collection. The real-OS-home tests are the only exception;
    they call `resolve_home()` directly and never resolve the approval binary."""

    hatch_modules_before = {
        name: module
        for name, module in sys.modules.items()
        if name == "agenttools_hatch_escalation"
        or name.startswith("agenttools_hatch_escalation.")
    }
    clean_home = tmp_path_factory.mktemp("hermetic-home")

    def _clean_home() -> str:
        return str(clean_home)

    class _PasswdEntry:
        pw_dir = str(clean_home)

    monkeypatch.setenv("AGENTTOOLS_TEST_HERMETIC_HOME", str(clean_home))
    monkeypatch.setenv("HOME", str(clean_home))
    monkeypatch.setenv("USERPROFILE", str(clean_home))
    monkeypatch.setenv(
        "AGENT_TOOLS_OVERRIDES_LOG",
        str(clean_home / ".config" / "agent-tools" / "overrides.log"),
    )
    is_real_os_home_test = request.node.get_closest_marker("real_os_home") is not None

    if is_real_os_home_test:
        def _blocked_tg_ctl_resolution(*_args, **_kwargs):
            raise AssertionError("real-OS-home tests must not resolve tg-ctl")

        monkeypatch.setattr(
            agenttools_hatch_escalation,
            "_find_tg_ctl",
            _blocked_tg_ctl_resolution,
        )
    elif pwd is not None:
        # Hook modules now load fresh helper objects by file path; patching pwd.getpwuid is the
        # process-wide barrier that keeps those fresh copies pointed at the hermetic home.
        monkeypatch.setattr(pwd, "getpwuid", lambda _uid: _PasswdEntry)

    if not is_real_os_home_test:
        monkeypatch.setattr(agenttools_hatch_escalation, "resolve_home", _clean_home)
        for module in list(sys.modules.values()):
            hatch = getattr(module, "hatch_escalation", None)
            if getattr(hatch, "__name__", None) == "agenttools_hatch_escalation":
                monkeypatch.setattr(hatch, "resolve_home", _clean_home, raising=False)

    yield

    for name in tuple(sys.modules):
        if name == "agenttools_hatch_escalation" or name.startswith("agenttools_hatch_escalation."):
            sys.modules.pop(name, None)
    sys.modules.update(hatch_modules_before)

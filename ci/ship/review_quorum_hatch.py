#!/usr/bin/env python3
"""One-time Telegram hatch escalation for ship's review-quorum gate (Guard-B).

Reached at runtime ONLY from ci/ship/ship.sh, when the review-quorum bar is not met (or cannot
be verified) AND the shipper set RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM. It delegates to the shared
agenttools_hatch_escalation lib — the SAME lib the pin-primary-worktree / block-reset-hard
agent-hooks use — to ask Alex live on Telegram (`tg-ctl ask`) and returns his real-time verdict.

Invariants (why a bypass here can't be self-authorized):
  * The shared lib is imported from a FIXED path (lib/ two levels above this file), never an env
    var — so a shipper can't point it at an always-approve stub.
  * tg-ctl is resolved by the lib from the account's REAL home (pwd.getpwuid(os.getuid()).pw_dir),
    NOT the $HOME env var and NOT the repo being merged. So neither an ambient env var nor a
    rig.yaml a PR commits can redirect approval; only a rig.yaml in the real account home (a
    global, reviewed location) may override tg-ctl, else the lib's hard-coded trusted-paths
    allowlist (the real tg-ctl) is used.
  * In a real run this module writes its OWN audit line for the hatch outcome
    (bypass:approved / bypass:denied), so the record is single-source and testable in-process.
    In dry-run it writes no persistent audit; ship.sh prints the would-be audit line.

Past bug: an earlier revision resolved tg-ctl from the repo worktree (cwd=os.getcwd()) and then
from $HOME — both shipper/PR-controllable, letting a committed or env-pointed rig.yaml self-approve
(codex review P0/P1 on PR #225). Fixed by keying resolution off the OS identity's home only.

Exit codes (read by ship.sh): 0 approved, 1 requested-but-not-approved (blank/bare/denied),
2 not requested (should not happen — ship only calls this when the env var is set),
3 the shared lib could not be imported (fail-closed).

The ask is sent as a CRITICAL ButtonRequest (agent-tools#873): no local wait bound and no
tg-ctl-side 115 s `ASK_TOTAL_TIMEOUT_MS`, so a ship blocks until Alex answers, and an Approve
tapped after the ship was interrupted mints a grant a retry redeems with
`RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="approved:hgr-…"`.
"""
from __future__ import annotations

import importlib.util
import os
import pwd
import sys
from pathlib import Path

# Load the shared lib from its EXACT file path (ci/ship/ -> ../../lib/agenttools_hatch_escalation/
# __init__.py), NOT by name off sys.path. A by-name `import` would let a shipping agent hijack the
# module via PYTHONPATH (an earlier same-named module) — which, since approval is just a truthy
# result, would self-authorize the bypass. spec_from_file_location loads THE file regardless of
# sys.path / PYTHONPATH / sys.modules. ship.sh additionally runs this under `python3 -I` (isolated:
# ignores PYTHONPATH/PYTHONHOME/user-site and skips sitecustomize), so no startup hook can run
# first either. If the file is absent (e.g. ship.sh copied out of the checkout) the import fails
# CLOSED with exit 3 and ship refuses rather than proceeding unverified.
_LIB_INIT = Path(__file__).resolve().parents[2] / "lib" / "agenttools_hatch_escalation" / "__init__.py"

try:  # pragma: no cover - failure path exercised via the detached-copy integration test
    _spec = importlib.util.spec_from_file_location("agenttools_hatch_escalation", _LIB_INIT)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"cannot build a module spec from {_LIB_INIT}")
    hatch_escalation = importlib.util.module_from_spec(_spec)
    # Register the freshly-loaded module in sys.modules under its canonical name BEFORE exec: the
    # lib's @dataclass resolves its own module via sys.modules[cls.__module__], and this also
    # OVERWRITES any same-named module a PYTHONPATH/sitecustomize hook may have pre-imported, so
    # the real file wins unconditionally.
    sys.modules["agenttools_hatch_escalation"] = hatch_escalation
    _spec.loader.exec_module(hatch_escalation)
except Exception as exc:  # noqa: BLE001 - any import failure must fail closed
    sys.stderr.write(f"hatch escalation lib unavailable: {exc}")
    raise SystemExit(3)

_HOOK_ID = "ship-review-quorum"

# Verdict sentinels printed on stdout — ship.sh gates the bypass on the APPROVED one (a positive
# signal), so a fake/broken interpreter that just exits 0 without it fails closed.
_VERDICT_APPROVED = "APPROVED"
_VERDICT_DENIED = "DENIED"
_VERDICT_NOT_REQUESTED = "NOTREQUESTED"


def resolve_home() -> str:
    """The account's REAL home directory, from the OS identity — deliberately NOT $HOME.

    tg-ctl resolution walks up from this dir for a rig.yaml `agent_hooks.tg_ctl_path` override, so
    keying off getpwuid (not the env) means a shipper cannot redirect approval by exporting a
    doctored HOME. Overridable in tests via monkeypatch (never via the environment)."""
    return pwd.getpwuid(os.getuid()).pw_dir


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _audit(decision: str, reason: str) -> None:
    """Append one hatch-outcome audit line to SHIP_AUDIT_FILE. Best-effort: a logging failure
    must never change the ship decision, so all errors are swallowed. Mirrors the JSON shape of
    ship.sh's _review_quorum_audit_log (ts/pr/task_code/iterations/models/decision + reason)."""
    path = os.environ.get("SHIP_AUDIT_FILE") or str(Path(resolve_home()) / ".config" / "agent-tools" / "ship-audit.jsonl")
    try:
        import datetime
        import json

        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pr": os.environ.get("SHIP_HATCH_PR", ""),
            "task_code": os.environ.get("SHIP_HATCH_CODE", ""),
            "iterations": int(os.environ.get("SHIP_HATCH_ITER", "0") or 0),
            "models": int(os.environ.get("SHIP_HATCH_MODELS", "0") or 0),
            "roles": int(os.environ.get("SHIP_HATCH_ROLES", "0") or 0),
            "decision": decision,
        }
        if reason:
            rec["override_reason"] = reason
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


def _dry_run_denial_reason() -> str:
    env_var = hatch_escalation.hatch_env_var(_HOOK_ID)
    raw = os.environ.get(env_var)
    justification = (raw or "").strip()
    if not justification:
        return f"{env_var} is blank; Telegram hatch escalation denied"
    if justification.lower() in hatch_escalation._BARE_FLAG_VALUES:
        return f"{env_var} needs a written justification, not bare {justification!r}"
    return "dry-run: would request Telegram hatch escalation; no request sent"


def main() -> int:
    # Predicate is role coverage (agent-tools#644). Zero models/iterations are telemetry
    # and must not lead the Telegram payload — that is how Alex saw distinct_models: 0.
    ctx = {
        "pr": os.environ.get("SHIP_HATCH_PR", ""),
        "task_code": os.environ.get("SHIP_HATCH_CODE", ""),
        "distinct_roles_passed": os.environ.get("SHIP_HATCH_ROLES", ""),
        "repo": os.getcwd(),
        "gate": "ship review-quorum (self-merge-authority Guard-B)",
    }
    iter_n = (os.environ.get("SHIP_HATCH_ITER") or "").strip()
    models_n = (os.environ.get("SHIP_HATCH_MODELS") or "").strip()
    if iter_n not in {"", "0"}:
        ctx["iterations"] = iter_n
    if models_n not in {"", "0"}:
        ctx["distinct_models"] = models_n
    if _truthy_env("SHIP_DRY_RUN"):
        sys.stdout.write(f"{_VERDICT_DENIED} {_dry_run_denial_reason()}\n")
        return 1

    result = hatch_escalation.request_hatch_approval(
        _HOOK_ID, ctx, cwd=resolve_home(), critical=True
    )
    # Emit an explicit verdict SENTINEL on stdout (single-lined) as a POSITIVE approval signal.
    # ship.sh authorizes the bypass only on exit 0 AND a leading "APPROVED " here, so a fake or
    # broken `python3` that merely exits 0 without producing the sentinel fails CLOSED (refuse) —
    # matching how the other gates fail closed on a tool malfunction, rather than fail open.
    reason = (result.reason or "").replace("\r", " ").replace("\n", " ")
    if result.approved:
        _audit("bypass:approved", result.reason or "")
        sys.stdout.write(f"{_VERDICT_APPROVED} {reason}\n")
        return 0
    if result.env_present:
        _audit("bypass:denied", result.reason or "")
        sys.stdout.write(f"{_VERDICT_DENIED} {reason}\n")
        return 1
    sys.stdout.write(f"{_VERDICT_NOT_REQUESTED} {reason}\n")
    return 2  # not requested — ship.sh only calls this when the env var is present


if __name__ == "__main__":
    raise SystemExit(main())

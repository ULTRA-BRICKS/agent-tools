#!/usr/bin/env python3
"""One-time Telegram hatch escalation for ship's --skip-ci CI-bypass gate.

Reached at runtime ONLY from ci/ship/ship.sh, when the shipper passed `--skip-ci` (a blind
admin-merge that skips the whole green-CI gate AND branch protection) AND set
RIG_HATCH_REQUEST_SHIP_SKIP_CI. It delegates to the shared agenttools_hatch_escalation lib — the
SAME lib the review-quorum gate / pin-primary-worktree / block-reset-hard agent-hooks use — to ask
Alex live on Telegram (`tg-ctl ask`) and returns his real-time verdict. There is NO self-service
override; `--skip-ci` is deny-by-default and the intended path is a live approval. (Threat-model
caveat, identical to the review-quorum gate: a shipper who fully controls the ship PROCESS's PATH
can defeat ANY gate — a fake `python3` that prints `APPROVED` and exits 0, a fake `gh`/`git`, etc.
The `-I` isolation + fixed-path lib import defend against injecting a self-approving MODULE and
remove the fail-OPEN asymmetry for a benign shipper; they are not a claim to withstand a hostile
PATH — that threat is out of scope here as it is everywhere else in ship.)

Why this gate exists: the LEGITIMATE "CI is billing-blocked / infrastructure is down" case is
already handled automatically WITHOUT --skip-ci — on the normal (SKIP_CI=0) path ship detects the
outage (`_empty_rollup_is_ci_outage` / `ci_appears_structurally_down`), runs the local fallback
gate, and does a NORMAL non-admin merge. So a bare `--skip-ci` is purely a blind, self-service
admin bypass that verifies nothing and overrides branch protection — exactly the thing that must
require an explicit human approval, not an agent-settable flag.

Invariants (why a bypass here can't be self-authorized) — identical to review_quorum_hatch.py:
  * The shared lib is imported from a FIXED path (lib/ two levels above this file), never an env
    var — so a shipper can't point it at an always-approve stub.
  * tg-ctl is resolved by the lib from the account's REAL home (pwd.getpwuid(os.getuid()).pw_dir),
    NOT the $HOME env var and NOT the repo being merged. So neither an ambient env var nor a
    rig.yaml a PR commits can redirect approval; only a rig.yaml in the real account home (a
    global, reviewed location) may override tg-ctl, else the lib's hard-coded trusted-paths
    allowlist (the real tg-ctl) is used.
  * ship.sh runs this under `python3 -I` (isolated: ignores PYTHONPATH/PYTHONHOME/user-site and
    skips sitecustomize), so no startup hook can run first and self-approve.
  * In a real run this module writes its OWN audit line for the hatch outcome
    (skipci:bypass:approved / skipci:bypass:denied), so the record is single-source.

Dry-run divergence from review_quorum_hatch.py (deliberate): `--skip-ci --dry-run` is a PREVIEW
that never actually merges, so it must not fire a live Telegram round-trip. In dry-run this helper
sends NO message but still enforces deny-by-default at the justification level — a blank/bare
RIG_HATCH_REQUEST_SHIP_SKIP_CI is DENIED, a real written justification prints an APPROVED sentinel
annotated "would request Telegram approval". The real (non-dry-run) merge ALWAYS requires a live
approval; a justification alone never unlocks a real admin merge.

Exit codes (read by ship.sh): 0 approved, 1 requested-but-not-approved (blank/bare/denied),
2 not requested (should not happen — ship only calls this when the env var is set),
3 the shared lib could not be imported (fail-closed).

The ask is sent as a CRITICAL ButtonRequest (agent-tools#873): no local wait bound and no
tg-ctl-side 115 s `ASK_TOTAL_TIMEOUT_MS`, so a ship blocks until Alex answers, and an Approve
tapped after the ship was interrupted mints a grant a retry redeems with
`RIG_HATCH_REQUEST_SHIP_SKIP_CI="approved:hgr-…"`.
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

_HOOK_ID = "ship-skip-ci"

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
    ship.sh's _skip_ci_audit_log (ts/pr/branch/gate/decision + reason)."""
    path = os.environ.get("SHIP_AUDIT_FILE") or str(Path(resolve_home()) / ".config" / "agent-tools" / "ship-audit.jsonl")
    try:
        import datetime
        import json

        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pr": os.environ.get("SHIP_HATCH_PR", ""),
            "branch": os.environ.get("SHIP_HATCH_BRANCH", ""),
            "gate": "skip-ci",
            "decision": decision,
        }
        if reason:
            rec["override_reason"] = reason
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


def _justification() -> str:
    env_var = hatch_escalation.hatch_env_var(_HOOK_ID)
    return (os.environ.get(env_var) or "").strip()


def _is_bare_or_blank(justification: str) -> bool:
    return (not justification) or justification.lower() in hatch_escalation._BARE_FLAG_VALUES


def _blank_bare_denial_reason(justification: str) -> str:
    env_var = hatch_escalation.hatch_env_var(_HOOK_ID)
    if not justification:
        return f"{env_var} is blank; --skip-ci hatch denied"
    return f"{env_var} needs a written justification, not bare {justification!r}"


def main() -> int:
    justification = _justification()
    ctx = {
        "pr": os.environ.get("SHIP_HATCH_PR", ""),
        "branch": os.environ.get("SHIP_HATCH_BRANCH", ""),
        "repo": os.getcwd(),
        "gate": "ship --skip-ci (blind admin-merge, bypasses the green-CI gate + branch protection)",
    }

    if _truthy_env("SHIP_DRY_RUN"):
        # A --dry-run preview never actually merges, so send NO Telegram message. Still enforce
        # deny-by-default: a blank/bare justification is denied; a real written justification lets
        # the PREVIEW proceed, annotated so nobody mistakes it for a granted live approval.
        if _is_bare_or_blank(justification):
            sys.stdout.write(f"{_VERDICT_DENIED} {_blank_bare_denial_reason(justification)}\n")
            return 1
        sys.stdout.write(
            f"{_VERDICT_APPROVED} dry-run: would request a one-time Telegram approval; no request sent\n"
        )
        return 0

    # Deny-by-default, enforced LOCALLY (not only by the shared lib): a blank/bare justification is
    # rejected here BEFORE request_hatch_approval, so a live `tg-ctl ask` is never fired for a
    # non-justification. The lib also rejects blank/bare, but keeping the guard local makes the
    # "no tg-ctl contact for a blank request" invariant self-evident and not lib-version-dependent.
    if _is_bare_or_blank(justification):
        _audit("skipci:bypass:denied", "blank/bare justification")
        sys.stdout.write(f"{_VERDICT_DENIED} {_blank_bare_denial_reason(justification)}\n")
        return 1

    result = hatch_escalation.request_hatch_approval(
        _HOOK_ID, ctx, cwd=resolve_home(), critical=True
    )
    # Emit an explicit verdict SENTINEL on stdout (single-lined) as a POSITIVE approval signal.
    # ship.sh authorizes the bypass only on exit 0 AND a leading "APPROVED " here, so a fake or
    # broken `python3` that merely exits 0 without producing the sentinel fails CLOSED (refuse).
    reason = (result.reason or "").replace("\r", " ").replace("\n", " ")
    if result.approved:
        _audit("skipci:bypass:approved", result.reason or "")
        sys.stdout.write(f"{_VERDICT_APPROVED} {reason}\n")
        return 0
    if result.env_present:
        _audit("skipci:bypass:denied", result.reason or "")
        sys.stdout.write(f"{_VERDICT_DENIED} {reason}\n")
        return 1
    sys.stdout.write(f"{_VERDICT_NOT_REQUESTED} {reason}\n")
    return 2  # not requested — ship.sh only calls this when the env var is present


if __name__ == "__main__":
    raise SystemExit(main())

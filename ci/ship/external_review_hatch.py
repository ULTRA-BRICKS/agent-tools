#!/usr/bin/env python3
"""One-time Telegram hatch escalation for ship's external-review gate.

Reached at runtime ONLY from ci/ship/ship.sh, when the PR has ZERO GitHub-side reviews
(`gh pr view --json reviews` returned an empty array) AND the shipper set
RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW. It delegates to the shared agenttools_hatch_escalation
lib — the SAME lib the review-quorum gate / --skip-ci gate / pin-primary-worktree /
block-reset-hard agent-hooks use — to ask Alex live on Telegram (`tg-ctl ask`) and returns his
real-time verdict. There is NO self-service override; the gate is deny-by-default and the
intended path is a live approval. (Threat-model caveat, identical to the other hatches: a
shipper who fully controls the ship PROCESS's PATH can defeat ANY gate — a fake `python3` that
prints `APPROVED` and exits 0, a fake `gh`/`git`, etc. The `-I` isolation + fixed-path lib import
defend against injecting a self-approving MODULE and remove the fail-OPEN asymmetry for a benign
shipper; they are not a claim to withstand a hostile PATH — that threat is out of scope here as
it is everywhere else in ship.)

Why this gate exists: PR #764 (HYP-1380, hyperide/hyper-saas) merged via `gh ship` with
`gh pr view --json reviews` returning `[]` — Guard-B (review-cli's own automated multi-model
pass, a DIFFERENT signal) was wrongly treated as sufficient by a dispatch instruction. ship.sh
had no check of its own for an actual GitHub-side review ever happening; the unresolved-threads
and review-dwell gates are both vacuously satisfiable with zero review activity. This hatch is
the deliberately-narrow escape valve for the rare legitimate case (e.g. a genuinely trivial,
urgent fix where getting a reviewer is impractical) — it requires the SAME live human sign-off
as --skip-ci, not a self-service flag, because a self-service flag here would just recreate the
exact failure this gate exists to close.

Invariants (why a bypass here can't be self-authorized) — identical to skip_ci_hatch.py:
  * The shared lib is imported from a FIXED path (lib/ two levels above this file), never an env
    var — so a shipper can't point it at an always-approve stub.
  * tg-ctl is resolved by the lib from the account's REAL home (pwd.getpwuid(os.getuid()).pw_dir),
    NOT the $HOME env var and NOT the repo being merged.
  * ship.sh runs this under `python3 -I` (isolated), so no startup hook can self-approve.
  * In a real run this module writes its OWN audit line for the hatch outcome
    (external-review:bypass:approved / external-review:bypass:denied), single-source.

Dry-run divergence (deliberate, matching skip_ci_hatch.py): `--dry-run` never actually merges, so
it must not fire a live Telegram round-trip. In dry-run this helper sends NO message but still
enforces deny-by-default at the justification level.

Exit codes (read by ship.sh): 0 approved, 1 requested-but-not-approved (blank/bare/denied),
2 not requested (should not happen — ship only calls this when the env var is set),
3 the shared lib could not be imported (fail-closed).

The ask is sent as a CRITICAL ButtonRequest (agent-tools#873): no local wait bound and no
tg-ctl-side 115 s `ASK_TOTAL_TIMEOUT_MS`, so a ship blocks until Alex answers, and an Approve
tapped after the ship was interrupted mints a grant a retry redeems with
`RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW="approved:hgr-…"`.
"""
from __future__ import annotations

import importlib.util
import os
import pwd
import sys
from pathlib import Path

# Load the shared lib from its EXACT file path — see skip_ci_hatch.py for the full rationale
# (by-name import would let PYTHONPATH hijack the module; -I isolation closes that too).
_LIB_INIT = Path(__file__).resolve().parents[2] / "lib" / "agenttools_hatch_escalation" / "__init__.py"

try:  # pragma: no cover - failure path exercised via the detached-copy integration test
    _spec = importlib.util.spec_from_file_location("agenttools_hatch_escalation", _LIB_INIT)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"cannot build a module spec from {_LIB_INIT}")
    hatch_escalation = importlib.util.module_from_spec(_spec)
    sys.modules["agenttools_hatch_escalation"] = hatch_escalation
    _spec.loader.exec_module(hatch_escalation)
except Exception as exc:  # noqa: BLE001 - any import failure must fail closed
    sys.stderr.write(f"hatch escalation lib unavailable: {exc}")
    raise SystemExit(3)

_HOOK_ID = "ship-external-review"

_VERDICT_APPROVED = "APPROVED"
_VERDICT_DENIED = "DENIED"
_VERDICT_NOT_REQUESTED = "NOTREQUESTED"


def resolve_home() -> str:
    """The account's REAL home directory, from the OS identity — deliberately NOT $HOME.
    Overridable in tests via monkeypatch (never via the environment)."""
    return pwd.getpwuid(os.getuid()).pw_dir


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _audit(decision: str, reason: str) -> None:
    """Append one hatch-outcome audit line to SHIP_AUDIT_FILE. Best-effort: a logging failure
    must never change the ship decision. Mirrors skip_ci_hatch.py's _audit shape."""
    path = os.environ.get("SHIP_AUDIT_FILE") or str(Path(resolve_home()) / ".config" / "agent-tools" / "ship-audit.jsonl")
    try:
        import datetime
        import json

        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pr": os.environ.get("SHIP_HATCH_PR", ""),
            "branch": os.environ.get("SHIP_HATCH_BRANCH", ""),
            "gate": "external-review",
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
        return f"{env_var} is blank; external-review hatch denied"
    return f"{env_var} needs a written justification, not bare {justification!r}"


def main() -> int:
    justification = _justification()
    ctx = {
        "pr": os.environ.get("SHIP_HATCH_PR", ""),
        "branch": os.environ.get("SHIP_HATCH_BRANCH", ""),
        "repo": os.getcwd(),
        "gate": "ship external-review (merging a PR with ZERO GitHub-side reviews)",
    }

    if _truthy_env("SHIP_DRY_RUN"):
        if _is_bare_or_blank(justification):
            sys.stdout.write(f"{_VERDICT_DENIED} {_blank_bare_denial_reason(justification)}\n")
            return 1
        sys.stdout.write(
            f"{_VERDICT_APPROVED} dry-run: would request a one-time Telegram approval; no request sent\n"
        )
        return 0

    # NOTE (real-run path only — the dry-run branch above never calls the shared lib at all, by
    # design, and is unaffected): a blank/bare value is NOT short-circuited locally here. The
    # shared lib's own `_request_present_hatch_approval` already denies blank/bare BEFORE ever
    # resolving/contacting tg-ctl (same safety property a local check would give), but ALSO
    # unconditionally records the attempt to the escape-hatch audit sink (`overrides.log`,
    # gap G-8) for every case where the env var carried any value at all -- blank, bare, denied,
    # or approved. A local early return here would skip that shared sink entirely, hiding a
    # blank/bare hatch attempt from the repo-wide "escape hatches have no audit sink" tracking
    # (review-cli Codex pass, GH-459: this exact gap, closed here by NOT special-casing blank).
    result = hatch_escalation.request_hatch_approval(
        _HOOK_ID, ctx, cwd=resolve_home(), critical=True
    )
    reason = (result.reason or "").replace("\r", " ").replace("\n", " ")
    if result.approved:
        _audit("external-review:bypass:approved", result.reason or "")
        sys.stdout.write(f"{_VERDICT_APPROVED} {reason}\n")
        return 0
    if result.env_present:
        _audit("external-review:bypass:denied", result.reason or "")
        sys.stdout.write(f"{_VERDICT_DENIED} {reason}\n")
        return 1
    sys.stdout.write(f"{_VERDICT_NOT_REQUESTED} {reason}\n")
    return 2  # not requested — ship.sh only calls this when the env var is present


if __name__ == "__main__":
    raise SystemExit(main())

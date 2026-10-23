"""review-quorum preflight gate (Guard-B) and the --skip-ci hatch gate (ship.orig.sh 2549-3265).

The quorum gate hard-refuses a merge unless review-cli's own record shows the PR's task code has
enough PASSED review iterations across enough distinct BOARD ROLES. It is fail-CLOSED: a missing
`review`/`jq`, an unreadable store, no derivable task code, or counts reading 0 all refuse; ship
re-derives the verdict from the counts and never trusts the subprocess's `passed` boolean (#242).
The only override is a live Telegram hatch approval (RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time

from shiplib.ctx import ShipExit

_SPACE = " \t\r\f\v"  # bash [[:space:]] within one line (grep works line by line)
_DIGITS = re.compile(r"[0-9]+")
_UPPER_TO_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}
_LOWER_TO_UPPER = {c: c - 32 for c in range(ord("a"), ord("z") + 1)}


def _lower(s: str) -> str:
    """`tr '[:upper:]' '[:lower:]'` under LC_ALL=C: ASCII letters only."""
    return s.translate(_UPPER_TO_LOWER)


def _upper(s: str) -> str:
    return s.translate(_LOWER_TO_UPPER)


def _which(ctx, name: str) -> bool:
    return shutil.which(name, path=ctx.env.get("PATH", os.defpath)) is not None


# ---- ticket extraction -----------------------------------------------------------------------

_HYP_RE = re.compile(r"HYP-[0-9]+", re.I | re.A)
_PREFIX_NUM_RE = re.compile(r"[A-Z][A-Z]+-[0-9]+", re.A)
_CANDIDATE_RE = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*", re.A)
_DESCRIPTIVE_RE = re.compile(r"[A-Z][A-Z]+(?:-[A-Z][A-Z]+){2,}", re.A)
_KEYWORD_REF_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_./-])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|references?)"
    r"[" + _SPACE + r"]*:?[" + _SPACE + r"]*#[0-9]+[A-Za-z0-9_]?",
    re.I | re.A,
)
_KEYWORD_TAIL_RE = re.compile(r"#[0-9]+[A-Za-z0-9_]?\Z", re.A)
_ISSUE_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[^/" + _SPACE + r"]+/[^/" + _SPACE + r"]+/issues/[0-9]+[A-Za-z0-9_]?",
    re.I | re.A,
)


def review_quorum_extract_ticket(ctx, text: str) -> str:
    """First ticket-like token in `text` (HYP-n, PREFIX-n, descriptive ALL-CAPS, `Fixes #n`, own-repo
    issue URL as `#n`), or "" — see the bash comments for the accepted residuals; never raises."""
    lines = text.split("\n")
    for line in lines:
        m = _HYP_RE.search(line)
        if m:
            return _upper(m.group(0))
    for line in lines:
        m = _PREFIX_NUM_RE.search(line)
        if m:
            return m.group(0)
    for line in lines:
        for cand in _CANDIDATE_RE.findall(line):
            if _DESCRIPTIVE_RE.fullmatch(cand):
                return cand
    # Keyword-anchored `#<n>` (never a bare `#n`, never another repo's `org/repo#n`). Two distinct
    # anchored refs are ambiguous -> nothing, never the first in document order.
    refs: set[str] = set()
    for line in lines:
        for m in _KEYWORD_REF_RE.finditer(line):
            tail = _KEYWORD_TAIL_RE.search(m.group(0))
            if tail and re.fullmatch(r"#[0-9]+", tail.group(0)):
                refs.add(tail.group(0))
    if len(refs) == 1:
        return next(iter(refs))
    if len(refs) > 1:
        return ""
    urls = [m.group(0) for line in lines for m in _ISSUE_URL_RE.finditer(line)]
    if not urls:
        return ""
    issues = sorted({n for n in ship_own_repo_issue_numbers(ctx, "\n".join(urls)) if n})
    if len(issues) == 1:
        return "#" + issues[0]
    return ""


def ship_own_repo_issue_numbers(ctx, urls: str) -> list[str]:
    """Issue NUMBER of every URL in the newline-separated `urls` that names the PR's own repo
    (duplicates kept; empty list when the own repo is unknown)."""
    slugs = ship_own_repo_slugs(ctx)
    if not slugs:
        return []
    numbers: list[str] = []
    for cand in urls.split("\n"):
        if not cand:
            continue
        cand = re.sub(r"^https?://(www\.)?github\.com/", "", _lower(cand), count=1)
        for slug in slugs:
            if cand.startswith(slug + "/issues/"):
                n = cand.rsplit("/", 1)[-1]
                if not re.search(r"[^0-9]", n):
                    numbers.append(n)
    return numbers


def ship_own_repo_slugs(ctx) -> list[str]:
    """Every lowercase "owner/repo" slug naming the PR's own repo (explicit gh target, checkout
    origin, live PR URL), sorted unique; resolved once per run. A renamed repo keeps the old origin
    slug while the live PR carries the new one, so all three are kept."""
    if not getattr(ctx, "own_repo_slugs_resolved", False):
        ctx.own_repo_slugs_resolved = True
        res = ctx.gh("pr", "view", ctx.pr, "--json", "url", "-q", '.url // ""', stderr="devnull")
        live = res.out.rstrip("\n") if res.rc == 0 else ""
        live = "\n".join(re.sub(r"/pull/[0-9]+.*$", "", ln) for ln in live.split("\n"))
        live = live.rstrip("\n")
        found: set[str] = set()
        for raw in (ctx.env.get("GH_REPO", ""), getattr(ctx, "cwd_origin_repo", ""), live):
            slug = ship_normalize_repo_slug(ctx, raw)
            if slug:
                found.add(slug)
        ctx.own_repo_slugs = sorted(found)
    return list(getattr(ctx, "own_repo_slugs", []))


_SLUG_SUBS = (
    (re.compile(r"^[a-z][a-z0-9+.-]*://([^@/]+@)?(www\.)?github\.com(:[0-9]+)?/"), ""),
    (re.compile(r"^[a-z][a-z0-9+.-]*://"), ""),
    (re.compile(r"^[^@/]+@"), ""),
    (re.compile(r"^(www\.)?github\.com[:/]"), ""),
    (re.compile(r"\.git$"), ""),
    (re.compile(r"/+$"), ""),
)


def ship_normalize_repo_slug(ctx, ref: str) -> str:
    """Lowercase `owner/repo` for a gh repo reference or github.com remote URL, or "" when it does
    not reduce to exactly two path segments."""
    lines = []
    for line in _lower(ref).split("\n"):
        for rx, repl in _SLUG_SUBS:
            line = rx.sub(repl, line, count=1)
        lines.append(line)
    slug = "\n".join(lines).rstrip("\n")
    if slug.count("/") != 1 or slug.startswith("/") or slug.endswith("/") or ":" in slug or not slug:
        return ""
    return slug


def ship_target_is_checkout_origin(ctx) -> bool:
    """True when the gh target names this checkout's own github.com origin (both normalized);
    False when either side does not normalize."""
    target = ship_normalize_repo_slug(ctx, getattr(ctx, "effective_target", ""))
    origin = ship_normalize_repo_slug(ctx, getattr(ctx, "cwd_origin_url", ""))
    return bool(target) and target == origin


def review_quorum_clamp_floor(ctx, raw: str, label: str) -> str:
    """Clamp a quorum floor to the hard minimum 3 (unset/non-numeric/<3 -> 3); returns the string."""
    floor = 3
    if not _DIGITS.fullmatch(raw):
        if raw:
            ctx.err(f"[ship] review-quorum: ignoring invalid {label}='{raw}' — using hard floor {floor}.")
        return str(floor)
    if int(raw) < floor:
        ctx.err(
            f"[ship] review-quorum: {label}={raw} is below the hard floor — raising to {floor} "
            "(the bar can be raised, never lowered)."
        )
        return str(floor)
    return raw


# ---- audit lines -----------------------------------------------------------------------------

_CTRL_STRIP = {c: None for c in (*range(0, 9), 11, 12, *range(14, 32))}


def _json_safe(s: str) -> str:
    """jq-less escape: tab/newline/CR -> space, other control chars dropped, then `\\` and `"`
    escaped, so a value can never forge an extra JSONL line."""
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ").translate(_CTRL_STRIP)
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _audit_file(ctx) -> str:
    return ctx.env.get("SHIP_AUDIT_FILE") or os.path.join(
        ctx.env.get("HOME") or os.path.expanduser("~"), ".config/agent-tools/ship-audit.jsonl"
    )


def _append_audit(file: str, line: str) -> None:
    try:
        with open(file, "a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _audit_target(ctx):
    """Audit file path after mkdir -p of its directory, or None (best effort: failures swallowed)."""
    file = _audit_file(ctx)
    try:
        os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
    except OSError:
        return None
    return file


def _utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def review_quorum_audit_log(
    ctx, decision, code, iterations="0", models="0", reason="", roles="0", min_roles="0", min_models="0"
) -> None:
    """Append one review-quorum audit line (best effort; dry-run only prints the preview).
    min_models 0 = "model floor not enforced" sentinel. jq's compact output when jq is on PATH,
    else the printf fallback with identical field order."""
    iterations, models, roles, min_roles, min_models = (
        str(v) or "0" for v in (iterations, models, roles, min_roles, min_models)
    )
    if ctx.dry_run:
        ctx.err(
            f"[dry-run] would append review-quorum audit: decision={decision} task={code} "
            f"iter={iterations} models={models} (floor {min_models}) roles={roles}/{min_roles}"
        )
        return
    file = _audit_target(ctx)
    if file is None:
        return
    ts = _utc_ts()
    if _which(ctx, "jq"):
        nums = (iterations, models, roles, min_roles, min_models)
        if not all(_DIGITS.fullmatch(n) for n in nums):
            return  # jq --argjson rejects a non-JSON number and `|| true` drops the line
        obj = {
            "ts": ts,
            "pr": ctx.pr,
            "task_code": code,
            "iterations": int(iterations),
            "models": int(models),
            "min_models": int(min_models),
            "roles": int(roles),
            "min_roles": int(min_roles),
            "decision": decision,
        }
        if reason != "":
            obj["override_reason"] = reason
        _append_audit(file, json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        return
    head = (
        f'{{"ts":"{ts}","pr":"{_json_safe(ctx.pr)}","task_code":"{_json_safe(code)}",'
        f'"iterations":{iterations},"models":{models},"min_models":{min_models},'
        f'"roles":{roles},"min_roles":{min_roles},"decision":"{decision}"'
    )
    if reason:
        line = head + f',"override_reason":"{_json_safe(reason)}"}}'
    else:
        line = head + "}"
    _append_audit(file, line)


# ---- review-quorum hatch / capacity / refusal ------------------------------------------------

def _q(ctx, name: str, default="0"):
    return getattr(ctx, name, default) or default


def review_quorum_hatch_check(ctx):
    """Ask Alex live on Telegram via review_quorum_hatch.py (`python3 -I`); returns the Result
    (stdout = verdict sentinel + reason, rc 0 approved / 1 not approved / 3 lib import failed)."""
    env = dict(ctx.env)
    env["SHIP_HATCH_PR"] = ctx.pr
    env["SHIP_HATCH_CODE"] = getattr(ctx, "task_code", "") or ""
    env["SHIP_HATCH_ITER"] = str(_q(ctx, "qiter"))
    env["SHIP_HATCH_MODELS"] = str(_q(ctx, "qmodels_n"))
    env["SHIP_HATCH_ROLES"] = str(_q(ctx, "qroles_n"))
    env["SHIP_DRY_RUN"] = "1" if ctx.dry_run else "0"
    return ctx.cmd([ctx.python, "-I", ctx.ship_hatch_py], stdout="capture", stderr="devnull", env=env)


def _jq_text(v) -> str:
    """jq string interpolation / `-r` rendering of one value."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _truthy(v) -> bool:
    return v is not None and v is not False


def review_capacity_all_exhausted(ctx) -> str:
    """`review capacity-check --json` verdict: "1\\t<evidence>\\n" when review-cli confirms EVERY
    board seat is exhausted, else "0\\n". Fails CLOSED on any problem."""
    if not _which(ctx, "review") or not _which(ctx, "jq"):
        return "0\n"
    res = ctx.cmd(["review", "capacity-check", "--json"], stdout="capture", stderr="devnull")
    raw = res.out.rstrip("\n")
    if res.rc != 0 or not raw:
        return "0\n"
    try:
        data = json.loads(raw)
    except ValueError:
        return "0\n"
    if not isinstance(data, dict):
        return "0\n"
    if data.get("all_exhausted") is not True:
        return "0\n"
    evidence = ""
    try:
        exhausted = data.get("exhausted")
        if isinstance(exhausted, list):
            items = exhausted
        elif isinstance(exhausted, dict):
            items = list(exhausted.values())
        elif exhausted is None:
            items = []
        else:
            items = []  # `.exhausted[]?` swallows the iteration error
        parts = []
        for item in items:
            if item is None:
                model, ev, src = None, None, None
            elif isinstance(item, dict):
                model, ev, src = item.get("model"), item.get("evidence"), item.get("source")
            else:
                raise ValueError("cannot index")
            val = ev if _truthy(ev) else (src if _truthy(src) else "exhausted")
            parts.append(f"{_jq_text(model)}: {_jq_text(val)}")
        evidence = "; ".join(parts).rstrip("\n")
    except ValueError:
        evidence = ""
    return f"1\t{evidence}\n"


def _split_verdict(hout: str) -> tuple[str, str]:
    """`${hout%% *}` / `${hout#* }`: verdict = text before the first space, reason = the rest."""
    if " " in hout:
        verdict, reason = hout.split(" ", 1)
        return verdict, reason
    return hout, ""


def review_quorum_refuse_or_hatch(ctx, summary: str) -> None:
    """Terminal handler for a quorum refusal: returns None when ship may proceed (capacity outage
    confirmed, or the hatch was APPROVED); otherwise audits and raises ShipExit(1)."""
    task_code = getattr(ctx, "task_code", "") or ""
    qiter, qmodels_n, qroles_n = _q(ctx, "qiter"), _q(ctx, "qmodels_n"), _q(ctx, "qroles_n")
    min_roles, audit_min_models = _q(ctx, "min_roles"), _q(ctx, "audit_min_models")
    cap_out = review_capacity_all_exhausted(ctx).rstrip("\n")
    if cap_out.startswith("1"):
        cap_evidence = cap_out[2:] if cap_out.startswith("1\t") else cap_out
        shown = cap_evidence or "see `review capacity-check --json`"
        ctx.out(
            f"[ship] review-quorum gate — {summary}; review-cli confirms EVERY configured provider is "
            f"exhausted right now ({shown}) — auto-proceeding without a manual hatch prompt. "
            "Recorded to $SHIP_AUDIT_FILE."
        )
        review_quorum_audit_log(
            ctx, "auto-proceed:capacity-outage", task_code, qiter, qmodels_n, cap_evidence,
            qroles_n, min_roles, audit_min_models,
        )
        return
    if "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM" not in ctx.env:
        ctx.err(f"Refusing: review-quorum gate — {summary}.")
        ctx.err(
            f"  Run more independent review iterations (e.g. `review diff --task {task_code or '<code>'}`) "
            "across distinct BOARD ROLES, then re-run ship."
        )
        ctx.err("  There is NO self-service override. To request a ONE-TIME bypass, set:")
        ctx.err('    RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"')
        ctx.err("  which asks Alex live on Telegram and proceeds ONLY on his real-time approval.")
        ctx.err("  SHIP_REVIEW_QUORUM=0 is forbidden and does not disable this gate.")
        review_quorum_audit_log(ctx, "refused", task_code, qiter, qmodels_n, "", qroles_n, min_roles, audit_min_models)
        raise ShipExit(1)
    # Authorize ONLY on exit 0 AND a leading APPROVED sentinel, so a fake/broken python3 that
    # merely exits 0 fails closed.
    res = review_quorum_hatch_check(ctx)
    hout = res.out.rstrip("\n")
    hverdict, hreason = _split_verdict(hout)
    if res.rc == 0 and hverdict == "APPROVED":
        ctx.out(
            f"[ship] review-quorum gate — {summary}; a one-time Telegram hatch escalation was "
            f"APPROVED by Alex — proceeding. ({hreason})"
        )
        return
    ctx.err(f"Refusing: review-quorum gate — {summary}.")
    ctx.err(f"  A Telegram hatch escalation was requested but NOT approved: {hreason or 'no approval'}.")
    ctx.err("  Obtain live approval, or add more independent review iterations across distinct BOARD ROLES.")
    # The helper audits bypass:denied itself only for a DENIED sentinel in a real run; under
    # --dry-run it writes nothing, and in every other case (fake/broken interpreter, import
    # failure, unexpected verdict) it did not audit, so record the fail-closed denial here.
    if hverdict == "DENIED":
        if ctx.dry_run:
            review_quorum_audit_log(
                ctx, "bypass:denied", task_code, qiter, qmodels_n, hreason, qroles_n, min_roles, audit_min_models
            )
    else:
        review_quorum_audit_log(
            ctx, "bypass:denied", task_code, qiter, qmodels_n, hreason, qroles_n, min_roles, audit_min_models
        )
    raise ShipExit(1)


def review_quorum_refuse_setup(ctx, summary: str, fix: str) -> None:
    """Terminal refusal for a setup error (foreign repo, old review-cli, unscoped counts): no
    capacity auto-proceed, no hatch. Raises ShipExit(1)."""
    ctx.err(f"Refusing: review-quorum gate — {summary}.")
    ctx.err(f"  {fix}")
    review_quorum_audit_log(
        ctx, "refused", getattr(ctx, "task_code", "") or "", _q(ctx, "qiter"), _q(ctx, "qmodels_n"), summary,
        _q(ctx, "qroles_n"), _q(ctx, "min_roles"), _q(ctx, "audit_min_models"),
    )
    raise ShipExit(1)


# ---- --skip-ci hatch gate --------------------------------------------------------------------

def skip_ci_audit_log(ctx, decision: str, reason: str = "") -> None:
    """One skip-ci audit line -> SHIP_AUDIT_FILE (dry-run only prints the preview). The helper owns
    the non-dry-run bypass:approved/denied line; this records only what it does not."""
    if ctx.dry_run:
        ctx.err(f"[dry-run] would append skip-ci audit: decision={decision}")
        return
    file = _audit_target(ctx)
    if file is None:
        return
    head = (
        f'{{"ts":"{_utc_ts()}","pr":"{_json_safe(ctx.pr)}","branch":"{_json_safe(ctx.branch or "")}",'
        f'"gate":"skip-ci","decision":"{decision}"'
    )
    if reason:
        line = head + f',"override_reason":"{_json_safe(reason)}"}}'
    else:
        line = head + "}"
    _append_audit(file, line)


def skip_ci_hatch_check(ctx):
    """Ask Alex live on Telegram to approve a one-time --skip-ci bypass via skip_ci_hatch.py
    (`python3 -I`); returns the Result (stdout = verdict sentinel + reason)."""
    env = dict(ctx.env)
    env["SHIP_HATCH_PR"] = ctx.pr
    env["SHIP_HATCH_BRANCH"] = ctx.branch or ""
    env["SHIP_DRY_RUN"] = "1" if ctx.dry_run else "0"
    return ctx.cmd([ctx.python, "-I", ctx.ship_skip_ci_hatch_py], stdout="capture", stderr="devnull", env=env)


def skip_ci_hatch_gate(ctx) -> None:
    """Terminal gate for --skip-ci: returns None to proceed with the admin merge (env unset ->
    refuse with how-to; env set -> helper verdict, authorized only on exit 0 + APPROVED);
    otherwise raises ShipExit(1)."""
    if "RIG_HATCH_REQUEST_SHIP_SKIP_CI" not in ctx.env:
        ctx.err(
            "Refusing: --skip-ci is a BLIND admin-merge that bypasses the green-CI gate AND branch "
            "protection — it is deny-by-default and needs a one-time live approval from Alex."
        )
        ctx.err(
            "  The legitimate 'CI is billing-blocked / down' case does NOT need --skip-ci: run ship "
            "WITHOUT it — the normal path auto-detects the outage, runs the local fallback gate, and "
            "does a normal (non-admin) merge."
        )
        ctx.err("  There is NO self-service override. To request a ONE-TIME bypass, set:")
        ctx.err('    RIG_HATCH_REQUEST_SHIP_SKIP_CI="<justification>"')
        ctx.err("  which asks Alex live on Telegram and proceeds ONLY on his real-time approval.")
        skip_ci_audit_log(ctx, "skipci:refused", "")
        raise ShipExit(1)
    res = skip_ci_hatch_check(ctx)
    hout = res.out.rstrip("\n")
    hverdict, hreason = _split_verdict(hout)
    if res.rc == 0 and hverdict == "APPROVED":
        if ctx.dry_run:
            # Dry-run sends no Telegram round-trip: APPROVED only means a written justification exists.
            ctx.out(
                "[ship] --skip-ci --dry-run: justification present — a REAL run would request live "
                "Telegram approval before the admin merge (no approval requested in dry-run). "
                f"({hreason})"
            )
        else:
            ctx.out(
                "[ship] --skip-ci: a one-time Telegram hatch escalation was APPROVED by Alex — "
                f"proceeding with the admin merge. ({hreason})"
            )
        return
    ctx.err(f"Refusing: --skip-ci hatch escalation was requested but NOT approved: {hreason or 'no approval'}.")
    ctx.err("  Obtain live approval, or drop --skip-ci and let the normal CI gate / billing-outage fallback run.")
    if hverdict == "DENIED":
        if ctx.dry_run:
            skip_ci_audit_log(ctx, "skipci:bypass:denied", hreason)
    else:
        skip_ci_audit_log(ctx, "skipci:bypass:denied", hreason)
    raise ShipExit(1)


# ---- gate body -------------------------------------------------------------------------------

def _jq_field(data, key: str, default: str) -> str:
    """`jq -r '.key // default'` as text; "" default for absent/null/false. Non-object -> default
    (jq errors and the bash `|| echo` fallback supplies it)."""
    if not isinstance(data, dict):
        return default
    v = data.get(key)
    if not _truthy(v):
        return default
    if isinstance(v, float) and math.isfinite(v) and v == int(v) and abs(v) < 2**63:
        return str(int(v))
    return _jq_text(v).rstrip("\n")


def _jq_join(data, key: str) -> str:
    """`jq -r '(.key // []) | join(", ")'`; "" on error (non-array, nested containers)."""
    if not isinstance(data, dict):
        return ""
    v = data.get(key)
    if not _truthy(v):
        return ""
    if not isinstance(v, list):
        return ""
    parts = []
    for item in v:
        if item is None:
            parts.append("")
        elif isinstance(item, (dict, list)):
            return ""
        else:
            parts.append(_jq_text(item))
    return ", ".join(parts).rstrip("\n")


def _jq_foreign(data) -> str:
    if not isinstance(data, dict):
        return ""
    raw = data.get("foreign_repo_iterations")
    if not _truthy(raw):
        raw = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = list(raw.values())
    else:
        return ""
    parts = []
    for item in items:
        if item is None:
            rid = ts = None
        elif isinstance(item, dict):
            rid, ts = item.get("repo_id"), item.get("ts")
        else:
            return ""
        parts.append(f"{_jq_text(rid)} {_jq_text(ts)}")
    if _truthy(data.get("foreign_repo_iterations_truncated")):
        parts.append("…")
    return ", ".join(parts).rstrip("\n")


def _digits_or_zero(s: str) -> str:
    return s if _DIGITS.fullmatch(s) else "0"


def run(ctx) -> None:
    ctx.quorum_enabled = True  # QUORUM_ENABLED
    enabled_raw = ctx.env.get("SHIP_REVIEW_QUORUM_ENABLED") or "1"
    if enabled_raw in ("0", "false", "no"):
        ctx.err(f"[ship] SHIP_REVIEW_QUORUM_ENABLED={enabled_raw} is forbidden — review-quorum gate stays enabled.")
    plain_raw = ctx.env.get("SHIP_REVIEW_QUORUM") or "1"
    if plain_raw in ("0", "false", "no"):
        ctx.err(f"[ship] SHIP_REVIEW_QUORUM={plain_raw} is forbidden — review-quorum gate stays enabled.")
    # Test-only isolation, honored only under pytest so production has no off-switch (#644).
    if ctx.env.get("PYTEST_CURRENT_TEST"):
        if (ctx.env.get("SHIP_TEST_DISABLE_REVIEW_QUORUM") or "0") in ("1", "true", "yes"):
            ctx.quorum_enabled = False

    if not ctx.quorum_enabled:
        ctx.out("[ship] review-quorum gate disabled (SHIP_TEST_DISABLE_REVIEW_QUORUM=1, test-only).")
        return

    # Role floor (#644): authorization is distinct_roles_passed >= MIN_ROLES (clamped to >=3);
    # MIN_ITER only travels to review-cli as telemetry; MIN_MODELS gates only when explicitly set.
    ctx.min_iter = review_quorum_clamp_floor(ctx, ctx.env.get("SHIP_REVIEW_QUORUM_MIN_ITER", ""), "min-iter")
    ctx.min_models = review_quorum_clamp_floor(ctx, ctx.env.get("SHIP_REVIEW_QUORUM_MIN_MODELS", ""), "min-models")
    ctx.min_roles = review_quorum_clamp_floor(ctx, ctx.env.get("SHIP_REVIEW_QUORUM_MIN_ROLES", ""), "min-roles")
    # Explicitness (set, even to a bad value), not the resolved value, decides whether it governs.
    ctx.min_models_explicit = "SHIP_REVIEW_QUORUM_MIN_MODELS" in ctx.env
    # 0 = "model floor not enforced" sentinel (an enforced floor is always clamped to >=3).
    ctx.audit_min_models = ctx.min_models if ctx.min_models_explicit else "0"
    # Safe defaults so an early refusal still has them for the audit line and hatch context.
    ctx.qiter = "0"
    ctx.qmodels_n = "0"
    ctx.qmodels = ""
    ctx.qroles_n = "0"
    ctx.qroles = ""
    ctx.qerr = ""

    ctx.task_code = ctx.env.get("REVIEW_TASK_CODE", "")
    if not ctx.task_code:
        ctx.task_code = review_quorum_extract_ticket(ctx, ctx.branch)
    if not ctx.task_code:
        res = ctx.gh("pr", "view", ctx.pr, "--json", "body", "-q", '.body // ""', stderr="devnull")
        body = res.out.rstrip("\n") if res.rc == 0 else ""
        ctx.task_code = review_quorum_extract_ticket(ctx, body)

    task_code = ctx.task_code
    if not task_code:
        review_quorum_refuse_or_hatch(
            ctx,
            "could not derive a task code (set $REVIEW_TASK_CODE, or put the ticket code e.g. HYP-931 — "
            "or a link to THIS repo's issue, https://github.com/<owner>/<repo>/issues/<n> — in the "
            "branch name or PR body)",
        )
        return
    if not _which(ctx, "review"):
        review_quorum_refuse_or_hatch(
            ctx, f"'review' CLI not found on PATH — cannot verify the bar for {task_code} (install review-cli)"
        )
        return
    if not _which(ctx, "jq"):
        review_quorum_refuse_or_hatch(ctx, f"jq not found — cannot evaluate the gate for {task_code} (install jq)")
        return
    if ctx.foreign_repo_invoke and not ship_target_is_checkout_origin(ctx):
        # The query counts only reviews recorded in THIS checkout's repository.
        target = getattr(ctx, "effective_target", "")
        origin_url = getattr(ctx, "cwd_origin_url", "") or "unset"
        review_quorum_refuse_setup(
            ctx,
            f"the PR is in {target}, but this checkout's origin is {origin_url}, and review history is "
            "only checked for this checkout's repository",
            f"Run ship from a checkout whose origin is {target} (a github.com origin; the owner/repo "
            "compare ignores case, URL or SSH form and .git).",
        )
        return
    tmpdir = ctx.env.get("TMPDIR") or "/tmp"
    try:
        fd, stderr_file = tempfile.mkstemp(prefix="ship-quorum.", dir=tmpdir)
        os.close(fd)
    except OSError:
        review_quorum_refuse_or_hatch(
            ctx, f"could not create a temp file in {tmpdir} to read review-cli's errors for {task_code}"
        )
        return
    os.unlink(stderr_file)

    # Role coverage (--min-roles) is always requested; --min-models only when the operator set
    # SHIP_REVIEW_QUORUM_MIN_MODELS (passing it unconditionally would reinstate a default model
    # floor in review-cli's AND logic). Scoped to this repository via --verify-repo-only (#824).
    ctx.review_check_args = ["-C", ctx.root, "--verify-repo-only", "--min-iter", ctx.min_iter, "--min-roles", ctx.min_roles]
    if ctx.min_models_explicit:
        ctx.review_check_args += ["--min-models", ctx.min_models]
    res = ctx.cmd(
        ["review", "task", task_code, "--check", *ctx.review_check_args, "--json"],
        stdout="capture", stderr="capture",
    )
    quorum_json = res.out.rstrip("\n")
    quorum_stderr = res.err.rstrip("\n")

    if not quorum_json:
        if re.search(r"unrecognized arguments.*--verify-repo-only", quorum_stderr, re.S):
            review_quorum_refuse_setup(
                ctx,
                f"the installed review-cli predates --verify-repo-only (review-cli#589), so it cannot "
                f"count {task_code}'s reviews for this repository alone",
                "Upgrade review-cli, then re-run ship.",
            )
        else:
            review_quorum_refuse_or_hatch(
                ctx, f"could not query review-cli (store unreadable / task {task_code} unknown)"
            )
        return

    try:
        data = json.loads(quorum_json)
    except ValueError:
        data = None  # every jq read fails -> the bash fallbacks (0 / empty)
    # Read ONLY the real keys review-cli emits (never the legacy `.iterations`/`.distinct_models`),
    # so a foreign payload reads 0/0 and fails closed.
    ctx.qiter = _digits_or_zero(_jq_field(data, "passed_iterations", "0"))
    ctx.qmodels_n = _digits_or_zero(_jq_field(data, "distinct_models_passed", "0"))
    ctx.qmodels = _jq_join(data, "models")
    ctx.qroles_n = _digits_or_zero(_jq_field(data, "distinct_roles_passed", "0"))
    ctx.qroles = _jq_join(data, "roles")
    ctx.qerr = _jq_field(data, "error", "")
    # identity_verification must read `repo_only`, else the counts span every repository.
    qidentity = _jq_field(data, "identity_verification", "")
    qforeign_n = _digits_or_zero(_jq_field(data, "excluded_foreign_repo_iterations", "0"))
    qforeign = _jq_foreign(data)
    foreign_summary = ""
    if int(qforeign_n) > 0:
        foreign_summary = (
            f"{qforeign_n} iteration(s) recorded in other repositories under {task_code} not counted: {qforeign}"
        )

    qiter, qmodels_n, qroles_n = int(ctx.qiter), int(ctx.qmodels_n), int(ctx.qroles_n)
    models_gate_ok = True
    if ctx.min_models_explicit:
        models_gate_ok = qmodels_n > 0 and qmodels_n >= int(ctx.min_models)

    # FAIL-CLOSED: never authorize on `.passed` alone. ROLE coverage is the predicate; iterations
    # must be positive (hollow-payload guard), models gate only when explicitly requested, and
    # review-cli must confirm repo scoping. .error / passed=false must not hatch when roles are met.
    roles_ok = qroles_n > 0 and qroles_n >= int(ctx.min_roles)
    if qidentity != "repo_only":
        review_quorum_refuse_setup(
            ctx,
            f"review-cli did not scope {task_code} to this repository (identity_verification="
            f"{qidentity or 'absent'}, expected repo_only), so its counts may include other "
            "repositories' reviews",
            "Upgrade review-cli (review-cli#589) if it predates --verify-repo-only; otherwise check "
            "that this checkout's origin remote names the PR's repository (review-cli could not "
            "resolve it), then re-run ship.",
        )
    elif roles_ok and qiter > 0 and models_gate_ok:
        if foreign_summary:
            ctx.err(f"[ship] review-quorum: {foreign_summary}.")
        if ctx.min_models_explicit:
            ctx.out(
                f"[ship] AUTHORITY CONFIRMED — review quorum met: {ctx.qiter} iterations across "
                f"{ctx.qroles_n} roles and {ctx.qmodels_n} models for {task_code}. Self-merge "
                "authorized by the review-quorum gate."
            )
        else:
            ctx.out(
                f"[ship] AUTHORITY CONFIRMED — review quorum met: {ctx.qiter} iterations across "
                f"{ctx.qroles_n} roles for {task_code}. Self-merge authorized by the review-quorum gate."
            )
        review_quorum_audit_log(
            ctx, "authorized", task_code, ctx.qiter, ctx.qmodels_n, "", ctx.qroles_n, ctx.min_roles,
            ctx.audit_min_models,
        )
        ctx.quorum_evidence = "met"
    else:
        models_summary = ""
        if ctx.min_models_explicit:
            seen = f" (models seen: {ctx.qmodels})" if ctx.qmodels else ""
            models_summary = f", {ctx.qmodels_n}/{ctx.min_models} distinct models{seen}"
        roles_seen = f" (roles seen: {ctx.qroles})" if ctx.qroles else ""
        err_part = f" ({ctx.qerr})" if ctx.qerr else ""
        foreign_part = f"; {foreign_summary}" if foreign_summary else ""
        review_quorum_refuse_or_hatch(
            ctx,
            f"bar NOT met for {task_code} — {ctx.qiter}/{ctx.min_iter} iterations, "
            f"{ctx.qroles_n}/{ctx.min_roles} distinct roles{roles_seen}{models_summary}{err_part}{foreign_part}",
        )

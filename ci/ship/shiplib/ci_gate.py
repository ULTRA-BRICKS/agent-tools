"""Bot-thread auto-resolve, green-CI gate, review-threads gate, review-dwell gate, branch sanity.

Port of ship.sh lines 1594-2060. The jq programs that gh runs server-side (`--jq`) are passed to gh
verbatim; the jq programs ship ran locally over the statusCheckRollup are reimplemented in Python.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from shiplib.ctx import ShipExit, sum_first_fields
from shiplib.ci_fallback import ci_appears_structurally_down, empty_rollup_is_ci_outage, known_flake_audit_log, known_flake_confirmed, run_local_ci_gate
from shiplib.setup import abort_if_core_bare
from shiplib.cleanup import resolve_base_branch
from shiplib.wt_state import head_alias_checkouts, list_checkouts, pr_head_oid, wt_status

RESOLVE_THREAD_Q = 'query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated comments(first:100){totalCount nodes{author{login} body}}}}}}}'

# Emit the thread IDs eligible for auto-resolution, one per line. Fail-CLOSED: a truncated thread
# (more comments than fetched), a ghost author, an unreadable body or any high-severity marker
# (hyphens count as marker boundaries on purpose, #285) makes the whole thread ineligible.
RESOLVE_ELIGIBLE_JQ = '''def readable_body:
    if type == "string" then (gsub("[^[:alnum:]]"; "") | length) > 0 else false end;
  def has_high_severity_marker:
    test("(^|[^[:alnum:]])(p0|p1|critical|blocker|security)($|[^[:alnum:]])"; "i");
  [.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved == false and .isOutdated == true)
  | select((.comments.nodes | length) > 0)
  | select(.comments.totalCount != null and (.comments.nodes | length) >= .comments.totalCount)
  | select([.comments.nodes[].author.login // ""] | all(. as $l | ($l | endswith("[bot]")) or ($l == "chatgpt-codex-connector") or ($l == "codex-review-bot")))
  | select([.comments.nodes[].body | readable_body] | all)
  | select([.comments.nodes[].body | has_high_severity_marker] | any | not)
  | .id] | .[]'''

THREAD_Q = 'query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{isResolved}}}}}'

DWELL_Q = 'query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){pullRequest(number:$pr){createdAt commits(last:1){nodes{commit{committedDate pushedDate}}} timelineItems(last:1,itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT]){nodes{__typename ... on HeadRefForcePushedEvent{createdAt}}}}}}'

_UNIT_SEP = "\x1f"


def resolve_one_thread(ctx, thread_id: str) -> bool:
    """resolveReviewThread mutation; True on success. `-f` (raw field): `-F` would read a leading `@` as a file."""
    r = ctx.gh(
        "api", "graphql",
        "-f", "query=mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}",
        "-f", f"id={thread_id}",
        stdout="devnull", stderr="devnull",
    )
    return r.rc == 0


def resolve_addressed_bot_threads(ctx) -> None:
    """Close unresolved+outdated threads authored only by bots; never touches anything uncertain."""
    n = 0
    r = ctx.gh(
        "api", "graphql", "--paginate",
        "-F", "owner={owner}", "-F", "name={repo}", "-F", f"pr={ctx.pr}",
        "-f", f"query={RESOLVE_THREAD_Q}", "--jq", RESOLVE_ELIGIBLE_JQ,
        stderr="devnull",
    )
    if r.rc != 0:
        ctx.err("[ship] auto-resolve: could not query review threads (gh api failed) — skipping; the gate below still applies.")
        return
    ids = r.out.rstrip("\n")
    if not ids:
        ctx.out("[ship] auto-resolve: no addressed bot-nit threads to resolve.")
        return
    for tid in ids.split("\n"):
        if not tid:
            continue
        if ctx.dry_run:
            ctx.out(f"[ship] auto-resolve (dry-run): would resolve addressed bot thread {tid}")
            n += 1
            continue
        if resolve_one_thread(ctx, tid):
            ctx.out(f"[ship] auto-resolve: resolved addressed bot thread {tid}")
            n += 1
        else:
            ctx.err(f"[ship] auto-resolve: FAILED to resolve {tid} — leaving it for the gate below.")
    ctx.out(f"[ship] auto-resolve: {n} addressed bot-nit thread(s) {'would be resolved' if ctx.dry_run else 'resolved'}.")


# ---- statusCheckRollup helpers (jq semantics) ----------------------------------------------

def _alt(*vals):
    """jq `a // b // c`: first value that is neither null nor false."""
    for v in vals:
        if v is not None and v is not False:
            return v
    return vals[-1] if vals else None


def _settled(c) -> bool:
    return c.get("status") == "COMPLETED" or c.get("state") in ("SUCCESS", "FAILURE", "ERROR")


def _success(c) -> bool:
    return c.get("conclusion") in ("SUCCESS", "SKIPPED", "NEUTRAL") or c.get("state") == "SUCCESS"


def _dedup_key(c) -> str:
    url = _alt(c.get("detailsUrl"), c.get("targetUrl"), "")
    parts = url.split("/")
    host = _alt(parts[2] if len(parts) > 2 else None, "")
    return (
        _alt(c.get("__typename"), "") + _UNIT_SEP + _alt(c.get("workflowName"), "") + _UNIT_SEP
        + host + _UNIT_SEP + _alt(c.get("name"), c.get("context"), "")
    )


def _dedup_ts(c) -> str:
    """Run recency; a timestamp-less unsettled run gets "~" so it outranks any ISO-8601 timestamp."""
    t = _alt(c.get("completedAt"), c.get("startedAt"), c.get("createdAt"))
    if t is not None:
        return t
    return "" if _settled(c) else "~"


def dedup_rollup(raw: str) -> list:
    """Collapse the rollup to the newest run per check (jq group_by + max_by); unparseable -> []."""
    try:
        data = json.loads(raw) if raw.strip() else None
        data = _alt(data, [])
        groups: dict[str, list] = {}
        for c in data:
            groups.setdefault(_dedup_key(c), []).append(c)
        out = []
        for key in sorted(groups):
            best = None
            for c in groups[key]:  # jq max_by keeps the LAST of equal maxima
                if best is None or not _dedup_ts(c) < _dedup_ts(best):
                    best = c
            out.append(best)
        return out
    except (ValueError, TypeError, AttributeError):
        return []


def _tsv_field(v) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return v.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    return json.dumps(v, separators=(",", ":"))


def _interp(v) -> str:
    """jq string interpolation of a value."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return v
    return json.dumps(v, separators=(",", ":"))


def _int_or_zero(s: str) -> int:
    """Bash arithmetic on a variable value: a non-number evaluates to 0."""
    try:
        return int(s.strip())
    except ValueError:
        return 0


def _known_flake_gate(ctx, rollup: list, failed: int) -> tuple[bool, str]:
    """Known-flake gate; returns (KNOWN_FLAKE_GATE_OK, KF_BASE). Positive confirmation only."""

    kf_base = ""
    if not ctx.known_flakes:
        return False, kf_base
    if not ctx.foreign_repo_invoke:
        r = ctx.gh("pr", "view", ctx.pr, "--json", "baseRefName", "-q", ".baseRefName", stderr="devnull")
        kf_base = r.out.rstrip("\n") if r.rc == 0 else ""
        if kf_base == "" or kf_base.startswith("-") or re.search(r"[^A-Za-z0-9._/-]", kf_base):
            kf_base = ""
    if not kf_base:
        if ctx.foreign_repo_invoke:
            ctx.err("[ship] known-flake: refusing for a foreign --repo/GH_SHIP_REPO target — the local fallback gate below verifies THIS checkout, not the target repo (same #166 class the CI-down path guards against).")
        else:
            ctx.err("[ship] known-flake: could not read this PR's base branch (gh pr view failed or returned an unsafe value) — refusing rather than guessing which branch's history to verify against.")
        return False, kf_base
    rows = [
        _tsv_field(_alt(c.get("name"), c.get("context"))) + "\t" + _tsv_field(_alt(c.get("workflowName"), ""))
        for c in rollup if not _success(c)
    ]
    lines = rows if rows else [""]  # a here-string of empty text still yields one (empty) read
    failed_count = 0
    confirmed_count = 0
    already_lost = False
    for line in lines:
        # `read -r` with IFS=tab drops leading tabs, so an empty name shifts the workflow into $fname.
        parts = re.split("\t+", line.lstrip("\t"), maxsplit=1)
        fname = parts[0]
        fwf = parts[1].rstrip("\t") if len(parts) > 1 else ""
        failed_count += 1
        shown = fname or "<unnamed check>"
        if already_lost:
            ctx.err(f"[ship] known-flake: '{shown}' not evaluated — an earlier failing check already sank this gate.")
            continue
        asserted = bool(fname) and fname in ctx.known_flakes
        if asserted and known_flake_confirmed(ctx, fname, fwf, kf_base):
            confirmed_count += 1
            ctx.err(f"[ship] known-flake: '{fname}' asserted via --known-flake and CONFIRMED against {kf_base}'s recent CI history.")
        elif asserted:
            ctx.err(f"[ship] known-flake: '{fname}' was asserted via --known-flake but could NOT be confirmed as a pre-existing failure on {kf_base} — this alone blocks the merge.")
            already_lost = True
        else:
            ctx.err(f"[ship] known-flake: '{shown}' is FAILED and was not asserted via --known-flake — this alone blocks the merge.")
            already_lost = True
    ok = (not already_lost) and failed_count > 0 and failed_count == failed and confirmed_count == failed_count
    return ok, kf_base


_REFUSE_TAIL_1918 = "  If a failing check looks UNRELATED to this diff, check whether it ALSO fails on this PR's BASE branch (e.g. `gh run list --branch <base>`, or re-run the same test locally against the base branch's HEAD — note this may not be {DEFAULT_BRANCH} for a stacked PR). A CONFIRMED pre-existing flake does NOT need human escalation: re-run with `--known-flake <check-name>` (repeatable, one per failing check) — ship independently VERIFIES the claim against the PR's actual base branch's recent CI history before doing anything with it (see --known-flake in this script's own header comment); it refuses the same as now if the claim doesn't hold up. Escalate to a human only for a genuine billing-block or a failure that is actually related to this diff."


def _poll_sleep(ctx, interval: str) -> None:
    """bash `sleep "$CI_POLL"`: the real sleep(1) rejects a bad interval and set -e ends the run."""
    rc = ctx.cmd(["sleep", interval], stdout="inherit").rc
    if rc != 0:
        raise ShipExit(rc)


def green_ci_gate(ctx) -> None:

    if ctx.which("jq") is None:
        raise ctx.refuse("Refusing: jq is required for the CI gate (install jq — or --skip-ci only if CI is genuinely N/A).")
    ci_wait_s = ctx.env.get("SHIP_CI_WAIT") or "900"
    ci_poll_s = ctx.env.get("SHIP_CI_POLL") or "20"
    ci_grace_s = ctx.env.get("SHIP_CI_GRACE") or "45"
    ci_wait, ci_grace = _int_or_zero(ci_wait_s), _int_or_zero(ci_grace_s)
    start = int(time.time())
    deadline = start + ci_wait
    grace_deadline = start + ci_grace
    pr = ctx.pr
    rollup: list = []
    n = 0
    while True:
        # Two-step read: a gh/API failure is an UNKNOWN rollup (retry, then refuse), never an empty one.
        r = ctx.gh("pr", "view", pr, "--json", "statusCheckRollup", "-q", ".statusCheckRollup", stderr="devnull")
        if r.rc != 0:
            now = int(time.time())
            if now >= deadline:
                raise ctx.refuse(f"Refusing: could not read CI status for PR #{pr} within {ci_wait_s}s (gh/API error, exit {r.rc}) — the remote check state is UNKNOWN; refusing rather than treating an unreadable rollup as 'no CI'. Retry when the API is reachable (or --skip-ci if CI is genuinely N/A).")
            ctx.out(f"[ship] could not read CI status for PR #{pr} (gh/API error, exit {r.rc}) — retrying (poll {ci_poll_s}s, {deadline - now}s left) ...")
            _poll_sleep(ctx, ci_poll_s)
            continue
        rollup = dedup_rollup(r.out)
        n = len(rollup)
        if n == 0:
            now = int(time.time())
            if now < grace_deadline:
                ctx.out(f"[ship] no checks reported yet on PR #{pr} — waiting {ci_poll_s}s for CI to register (grace {grace_deadline - now}s left) ...")
                _poll_sleep(ctx, ci_poll_s)
                continue
            # Foreign --repo targets keep the hard refuse: the local gate runs against THIS checkout (#166).
            if not ctx.foreign_repo_invoke and empty_rollup_is_ci_outage(ctx):
                ctx.err(f"[ship] a PR-triggered workflow is configured but registered NO checks within {ci_grace_s}s — CI infrastructure appears unavailable (billing/outage), not a real failure. Running local fallback gates instead of refusing.")
                if run_local_ci_gate(ctx):
                    ctx.out("[ship] CI-down local gate PASSED (no checks registered) — CI outage, not a test failure; proceeding with the normal (non-admin) merge.")
                    break
                raise ctx.refuse("Refusing: CI registered no checks AND local fallback gates also failed — not safe to merge.")
            ctx.err(f"Refusing: PR #{pr} has NO CI checks (none registered within {ci_grace_s}s) — set up CI before merging (an ungated merge is not allowed; 'no CI' is a failed gate, not a pass).")
            ctx.err("  Provision CI: enable rig's ci block — `rig apply` writes secret-scan / codeql / dependency-review into .github/workflows — or add your own workflows.")
            ctx.err("  Override ONLY if CI is genuinely N/A for this repo: --skip-ci (deny-by-default — needs a one-time live Telegram approval via RIG_HATCH_REQUEST_SHIP_SKIP_CI).")
            raise ShipExit(1)
        pending = sum(1 for c in rollup if not _settled(c))
        if pending == 0:
            break
        now = int(time.time())
        if now >= deadline:
            raise ctx.refuse(f"Refusing: PR #{pr} still has {pending} pending check(s) after {ci_wait_s}s. Let CI settle, then re-run.")
        ctx.out(f"[ship] {pending} check(s) still running on PR #{pr} — watching (poll {ci_poll_s}s, {deadline - now}s left before giving up) ...")
        _poll_sleep(ctx, ci_poll_s)

    failed = sum(1 for c in rollup if not _success(c))
    # Green = checks existed and every latest one passed (not the empty-rollup outage fallback).
    if failed == 0 and n != 0:
        ctx.ci_evidence = "green"
    if failed != 0:
        total_checks = len(rollup)
        if ci_appears_structurally_down(ctx, total_checks, failed):
            ctx.err("[ship] CI infrastructure appears structurally unavailable (all/most checks failed, outage signal confirmed) — running local fallback gates instead of blocking on CI.")
            if run_local_ci_gate(ctx):
                ctx.out("[ship] CI-down local gate PASSED — CI infrastructure failure, not a test failure; proceeding with merge.")
            else:
                raise ctx.refuse("Refusing: CI unavailable AND local fallback gates also failed — not safe to merge.")
            return
        flake_ok, kf_base = _known_flake_gate(ctx, rollup, failed)
        flakes = " ".join(ctx.known_flakes)
        if flake_ok:
            ctx.err(f"[ship] known-flake gate PASSED — every failing check is a confirmed pre-existing flake on {kf_base}, not something this PR introduced. Running local fallback gates instead of blocking on CI.")
            ctx.err("[ship] known-flake: the local fallback gate runs the repo's WHOLE local test suite on this machine, so it needs the project's test dependencies installed here; scope or replace it with SHIP_LOCAL_TEST_CMD=<cmd> or a committed .ship-config (SHIP_LOCAL_TEST_CMD= / SHIP_LOCAL_TEST_DIR=), or run the suite on a pod.")
            # The `confirmed` audit line waits for the real merge (kf_audit_pending, logged in accept_merge).
            if run_local_ci_gate(ctx, f"a confirmed pre-existing flake on {kf_base}, not a CI outage"):
                ctx.kf_audit_pending = flakes
                ctx.out("[ship] known-flake local gate PASSED — proceeding with merge.")
            else:
                known_flake_audit_log(ctx, "confirmed-local-gate-failed", flakes)
                raise ctx.refuse("Refusing: known-flake confirmed, but local fallback gates also failed — not safe to merge.")
        else:
            if ctx.known_flakes:
                known_flake_audit_log(ctx, "refused", flakes)
            ctx.err(f"Refusing: PR #{pr} has {failed} check(s) not passing:")
            for c in rollup:
                if not _success(c):
                    ctx.err(f"  x {_interp(_alt(c.get('name'), c.get('context')))} -> {_interp(_alt(c.get('conclusion'), c.get('state')))}")
            ctx.err("  Fix CI, then re-run. If CI is genuinely billing-blocked/down, re-run WITHOUT --skip-ci — the normal path auto-detects a real outage and merges after its own local checks. (--skip-ci is now a deny-by-default hatch-gated admin bypass, NOT the billing path.)")
            ctx.err(_REFUSE_TAIL_1918.replace("{DEFAULT_BRANCH}", ctx.default_branch))
            raise ShipExit(1)


def unresolved_threads_gate(ctx) -> None:
    """Fail-closed: an API failure refuses rather than reading as zero unresolved threads."""
    r = ctx.gh(
        "api", "graphql", "--paginate",
        "-F", "owner={owner}", "-F", "name={repo}", "-F", f"pr={ctx.pr}", "-f", f"query={THREAD_Q}",
        "--jq", "[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length",
        stderr="devnull",
    )
    if r.rc != 0:
        raise ctx.refuse(f"Refusing: could not query review threads for PR #{ctx.pr} (gh api failed) — refusing to merge rather than fail open. Fix gh access and retry.")
    unresolved = int(sum_first_fields(r.out.rstrip("\n")))
    if unresolved != 0:
        raise ctx.refuse(f"Refusing: PR #{ctx.pr} has {unresolved} unresolved review thread(s) — resolve them (or re-run with --resolve-addressed-threads to auto-close outdated bot-nit threads), then re-run.")


def iso_to_epoch(ts: str) -> int | None:
    """ISO-8601 UTC timestamp -> epoch seconds, or None when unparseable (caller fails closed)."""
    if not ts:
        return None
    ts = ts.split(".", 1)[0]
    if not ts.endswith("Z"):
        ts += "Z"
    try:
        return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def review_dwell_gate(ctx) -> None:
    dwell_s = ctx.env.get("SHIP_REVIEW_DWELL") or "600"
    if not re.fullmatch("[0-9]+", dwell_s):
        raise ctx.refuse(f"Refusing: SHIP_REVIEW_DWELL='{dwell_s}' must be a non-negative integer (seconds).")
    dwell = int(dwell_s)
    pr = ctx.pr
    if ctx.no_dwell_ok:
        ctx.out(f"[ship] review-dwell gate OVERRIDDEN — reason: {ctx.no_dwell_ok}")
        return
    if dwell_s == "0":
        ctx.out("[ship] review-dwell gate disabled (SHIP_REVIEW_DWELL=0).")
        return
    # Window start = max(createdAt, pushedDate, committedDate, forcePushedAt).
    r = ctx.gh(
        "api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-F", f"pr={pr}", "-f", f"query={DWELL_Q}",
        "--jq", '.data.repository.pullRequest | [.createdAt, (.commits.nodes[0].commit.pushedDate // ""), (.commits.nodes[0].commit.committedDate // ""), (.timelineItems.nodes[0].createdAt // "")] | @tsv',
        stderr="devnull",
    )
    if r.rc != 0:
        raise ctx.refuse(f"Refusing: could not read PR #{pr} timestamps for the review-dwell gate (gh api failed) — refusing rather than merge un-waited. Fix gh access, or override with --no-review-dwell-ok <reason>.")
    first = r.out.split("\n", 1)[0].split("\t")
    first += [""] * (4 - len(first))
    created, pushed, committed, force_pushed = first[:4]
    start: int | None = None
    for ts in (created, pushed, committed, force_pushed):
        e = iso_to_epoch(ts)
        if e is not None and (start is None or e > start):
            start = e
    if start is None:
        raise ctx.refuse(f"Refusing: could not parse PR #{pr} review-window timestamps ('{created}' / '{pushed}' / '{committed}' / '{force_pushed}') for the review-dwell gate — refusing rather than merge un-waited. Override with --no-review-dwell-ok <reason> if intended.")
    elapsed = int(time.time()) - start
    if elapsed < dwell:
        wait = dwell - elapsed
        ctx.err(f"Refusing: PR #{pr} is only {elapsed}s old since its last push — the review-dwell window is {dwell_s}s, so async review has not had time to form its questions yet.")
        ctx.err(f"  Wait {wait}s and re-run (meanwhile, request/run the review so comments can land). Tune with SHIP_REVIEW_DWELL=<seconds> (0 disables).")
        ctx.err("  Genuine fast-track (trivial/urgent): override with --no-review-dwell-ok <reason> (logged).")
        raise ShipExit(1)
    ctx.out(f"[ship] review-dwell gate OK — PR #{pr} is {elapsed}s past its last push (window {dwell_s}s).")


def local_branch_sanity(ctx) -> None:
    """Collect every worktree holding the PR branch (ctx.wts) plus every one sitting on the PR head
    commit under another name (ctx.wt_aliases: another local branch, or a detached HEAD); refuse
    dirty trees / unpushed commits."""

    cwd = ctx.root or None
    checkouts = list_checkouts(ctx, cwd)
    wts = [c.path for c in checkouts if c.branch == ctx.branch]
    base = resolve_base_branch(ctx)
    ctx.wt_aliases = {c.path: c.branch for c in head_alias_checkouts(ctx, checkouts, pr_head_oid(ctx), base)}
    for wt in [*wts, *ctx.wt_aliases]:
        # A worktree-scoped core.bare makes `status` exit 128 with its fatal swallowed below (reads "clean").
        abort_if_core_bare(ctx, wt, "PR worktree")
        st = wt_status(ctx, wt, base)
        if st.out.rstrip("\n") != "" and wt in ctx.wt_aliases:
            # Found by commit, not by the PR branch name: its edits are not this ship's to refuse over.
            ctx.err(f"[ship] leaving {wt} alone: it sits on the PR head under another name and has uncommitted changes.")
            del ctx.wt_aliases[wt]
        elif st.out.rstrip("\n") != "":
            ctx.err(f"Refusing: worktree {wt} has uncommitted changes. Commit or discard first.")
            ctx.err(st.out.rstrip("\n") + "\n", end="")
            raise ShipExit(1)
    ctx.wts = [*wts, *ctx.wt_aliases]  # read again by cleanup
    ctx.git("fetch", "-q", "origin", ctx.branch, cwd=cwd, stdout="inherit", stderr="devnull")
    branch = ctx.branch
    if (
        ctx.git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=cwd, stdout="devnull").rc == 0
        and ctx.git("show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=cwd, stdout="devnull").rc == 0
    ):
        c = ctx.git("rev-list", "--count", f"origin/{branch}..{branch}", cwd=cwd, stderr="devnull")
        ahead = c.out.rstrip("\n") if c.rc == 0 else "0"
        if ahead != "0":
            raise ctx.refuse(f"Refusing: local {branch} has {ahead} unpushed commit(s). Push first.")


def run(ctx) -> None:
    if ctx.resolve_threads:
        resolve_addressed_bot_threads(ctx)
    if not ctx.skip_ci:
        green_ci_gate(ctx)
    # --skip-ci never merges without its own hatch, so for the external-review evidence policy it
    # stands in for green CI.
    if ctx.skip_ci:
        ctx.ci_evidence = "skip-ci"
    unresolved_threads_gate(ctx)
    review_dwell_gate(ctx)
    local_branch_sanity(ctx)

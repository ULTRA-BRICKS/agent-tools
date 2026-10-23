"""External-review gate, ticket-gates preamble and magic-close keyword gate (ship.sh 3266-3872).

Segment run order (`run`): base-branch `.ship-config` off-switch lookup -> Rig machine policy ->
external-review gate -> magic-close gate. Everything else here is a function other segments or
this one call.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from shiplib.ctx import Ctx, Result, ShipExit
from shiplib.ci_fallback import ship_config_load
from shiplib.quorum_skipci import review_capacity_all_exhausted, review_quorum_extract_ticket

_EXTREV_BASE_BAD = re.compile(r"[^A-Za-z0-9._/-]")
_DIGITS = re.compile(r"[0-9]+")
_TICKET_ID = re.compile(r"#[0-9]+|[0-9]+|[A-Za-z][A-Za-z0-9]*-[0-9]+")
_GH_PREFIX = re.compile(r"[Gg][Hh]-")

# Keyword/ref grammar kept verbatim from the ERE (explicit per-letter classes, so no IGNORECASE).
_MAGIC_CLOSE_KW = (
    r"([Cc][Ll][Oo][Ss]([Ee]|[Ee][Ss]|[Ee][Dd]|[Ii][Nn][Gg])"
    r"|[Ff][Ii][Xx]([Ee][Ss]|[Ee][Dd]|[Ii][Nn][Gg])?"
    r"|[Rr][Ee][Ss][Oo][Ll][Vv]([Ee]|[Ee][Ss]|[Ee][Dd]|[Ii][Nn][Gg]))"
)
_MAGIC_CLOSE_REF = (
    r"(([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+|[A-Za-z][A-Za-z]+-[0-9]+"
    r"|https?://[A-Za-z0-9_./-]+/(issues|pull)/[0-9]+)"
)
# Group map: 1 = leading char (or line start), 2..5 = keyword, 6 = whitespace, 7.. = reference.
# [[:space:]] under LC_ALL=C is exactly space, \t \n \v \f \r.
_MAGIC_CLOSE_RE = re.compile(
    r"(^|[^A-Za-z0-9_])" + _MAGIC_CLOSE_KW + r"([ \t\n\v\f\r]+)" + _MAGIC_CLOSE_REF
)


def _esc(value: str) -> str:
    """tr '\\n\\r\\t' spaces | tr -d control chars | sed escape backslash and quote."""
    value = value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    value = "".join(
        ch for ch in value
        if not (ch <= "\x08" or ch in "\x0b\x0c" or "\x0e" <= ch <= "\x1f")
    )
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _joined(*groups: list[str]) -> str:
    """Non-empty lines of every group joined with ';' (`sed '/^$/d' | tr '\\n' ';' | sed 's/;$//'`)."""
    lines = [line for group in groups for line in group if line]
    joined = ";".join(lines)
    return joined[:-1] if joined.endswith(";") else joined


def _quoted_lines(prefix: str, lines: list[str]) -> list[str]:
    return [f'{prefix}{line}"' for line in lines]


# --- external-review gate ---------------------------------------------------------------------


def external_review_audit_log(ctx: Ctx, decision: str, reason: str = "") -> None:
    """One external-review audit line (dry-run: only announce it)."""
    if ctx.dry_run:
        ctx.err(f"[dry-run] would append external-review audit: decision={decision}")
        return
    ts = _now()
    esc_pr, esc_branch, esc_reason = _esc(ctx.pr), _esc(ctx.branch), _esc(reason)
    if reason:
        line = (
            f'{{"ts":"{ts}","pr":"{esc_pr}","branch":"{esc_branch}","gate":"external-review",'
            f'"decision":"{decision}","override_reason":"{esc_reason}"}}'
        )
    else:
        line = (
            f'{{"ts":"{ts}","pr":"{esc_pr}","branch":"{esc_branch}","gate":"external-review",'
            f'"decision":"{decision}"}}'
        )
    ctx.append_audit(line)


def external_review_hatch_check(ctx: Ctx) -> Result:
    """Run the Telegram hatch helper (stderr discarded, as the only caller did); Result carries rc+verdict."""
    env = dict(ctx.env)
    env["SHIP_HATCH_PR"] = ctx.pr
    env["SHIP_HATCH_BRANCH"] = ctx.branch
    env["SHIP_DRY_RUN"] = "1" if ctx.dry_run else "0"
    return ctx.cmd([ctx.python, "-I", ctx.ship_external_review_hatch_py], env=env, stderr="devnull")


def external_review_hatch_or_refuse(ctx: Ctx) -> bool:
    """True when the one-time hatch approved; otherwise refuses (raises ShipExit(1))."""
    if "RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW" in ctx.env:
        res = external_review_hatch_check(ctx)
        hrc = res.rc
        hout = res.out.rstrip("\n")
        hverdict = hout.split(" ", 1)[0]
        hreason = hout.split(" ", 1)[1] if " " in hout else ""
        if hrc == 0 and hverdict == "APPROVED":
            if ctx.dry_run:
                ctx.out(
                    "[ship] external-review --dry-run: justification present — a REAL run would request "
                    f"live Telegram approval before merging with zero reviews. ({hreason})"
                )
            else:
                ctx.out(
                    "[ship] external-review: a one-time Telegram hatch escalation was APPROVED by Alex — "
                    f"proceeding with zero GitHub reviews. ({hreason})"
                )
            return True
        ctx.err(
            f"Refusing: external-review hatch escalation was requested but NOT approved: {hreason or 'no approval'}."
        )
        ctx.err(f"  Obtain live approval, or get an actual review posted on PR #{ctx.pr} before shipping.")
        # The helper audits its own bypass:denied only for a DENIED verdict on a REAL run; every
        # other non-DENIED case (broken/absent interpreter, unexpected verdict) is recorded here.
        if hverdict == "DENIED":
            if ctx.dry_run:
                external_review_audit_log(ctx, "external-review:bypass:denied", hreason)
        else:
            external_review_audit_log(ctx, "external-review:bypass:denied", hreason)
        raise ShipExit(1)
    ctx.err(
        f"Refusing: PR #{ctx.pr} has ZERO GitHub-side reviews. Guard-B (review-cli's own automated pass) and 0 "
        "unresolved review threads do NOT prove a review ever happened — see PR #764 (HYP-1380), which merged this exact way."
    )
    ctx.err("  Get a real review posted (a human, or a review bot like Codex) before shipping.")
    ctx.err("  There is NO self-service override. To request a ONE-TIME bypass, set:")
    ctx.err('    RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW="<justification>"')
    ctx.err("  which asks Alex live on Telegram and proceeds ONLY on his real-time approval.")
    ctx.err(
        "  A SHIP_EXTERNAL_REVIEW=0 .ship-config line already MERGED to the base branch disables this gate "
        "entirely (ops off-switch; env SHIP_EXTERNAL_REVIEW=0 is ignored; a .ship-config committed only on "
        "THIS PR's own branch does not count)."
    )
    external_review_audit_log(ctx, "external-review:refused", "")
    raise ShipExit(1)


def _extrev_base_offswitch_lookup(ctx: Ctx) -> None:
    """Read SHIP_EXTERNAL_REVIEW from the PR's BASE branch (never HEAD, which is the PR's own tip)."""
    ctx.extrev_pr_base = ""
    if ctx.foreign_repo_invoke:
        return
    res = ctx.gh("pr", "view", ctx.pr, "--json", "baseRefName", "-q", ".baseRefName", stderr="devnull")
    base = res.out.rstrip("\n") if res.rc == 0 else ""
    ctx.extrev_pr_base = base
    if base == "" or base.startswith("-") or _EXTREV_BASE_BAD.search(base):
        # Guessing a base here could read a different branch's off-switch; leave the gate enabled.
        ctx.err(
            f"[ship] external-review gate: could not read PR #{ctx.pr}'s base branch — skipping the "
            ".ship-config off-switch lookup (gate stays enabled)."
        )
        return
    ctx.extrev_base = base
    # `+` forces the update on a non-fast-forward base rewrite; a failed fetch must not fall back to a
    # stale local ref, so only a confirmed-fresh fetch may authorize the off-switch.
    fetched = ctx.git(
        "-C", ctx.root, "fetch", "-q", "origin", f"+{base}:refs/remotes/origin/{base}",
        stderr="devnull",
    )
    if fetched.rc != 0:
        return
    # Fully-qualified ref: a bare `origin/<base>` would resolve through refs/tags/origin/<base> first.
    ctx.extrev_base_ref = f"refs/remotes/origin/{base}"
    verify = ctx.git(
        "-C", ctx.root, "rev-parse", "--verify", "--quiet", ctx.extrev_base_ref,
        stdout="devnull", stderr="devnull",
    )
    if verify.rc != 0:
        return

    ship_config_load(ctx, ctx.root, ctx.extrev_base_ref)
    # Snapshot: ship_config_load's cfg globals are shared with the HEAD-scoped callers.
    ctx.extrev_cfg_value = ctx.ship_cfg_external_review
    if ctx.extrev_cfg_value in ("0", "false", "no"):
        ctx.extrev_enabled = False


def _extrev_rig_policy(ctx: Ctx) -> None:
    """Rig machine policy: may disable the gate or switch a zero-review PR to the evidence decision."""
    ctx.extrev_rig_source = False
    # True = target owner is in external_review_evidence_owners: zero-review defers to the evidence decision.
    ctx.extrev_evidence_policy = False
    ctx.extrev_evidence_pending = False
    if not ctx.extrev_enabled:
        return
    if ctx.provider_resolved == "forgejo":
        res = ctx.gh("repo", "view", "", "--json", "nameWithOwner", "-q", ".nameWithOwner", stderr="devnull")
    else:
        target = ctx.effective_target or ctx.cwd_origin_repo
        res = ctx.gh(
            "repo", "view", target, "--json", "nameWithOwner,url", "-q",
            'select(.url | startswith("https://github.com/")) | .nameWithOwner',
            stderr="devnull",
        )
    ctx.extrev_rig_target = res.out.rstrip("\n") if res.rc == 0 else ""
    # stderr is NOT discarded: a rejected policy file prints one line naming why.
    dec = ctx.cmd(
        [ctx.python, "-I", os.path.join(ctx.self_dir, "rig_external_review_policy.py"),
         ctx.extrev_rig_target, ctx.root],
    )
    ctx.extrev_rig_decision = dec.out.rstrip("\n") if dec.rc == 0 else "enabled"
    if ctx.extrev_rig_decision == "disabled":
        ctx.extrev_enabled = False
        ctx.extrev_rig_source = True
    elif ctx.extrev_rig_decision == "evidence":
        ctx.extrev_evidence_policy = True


def _extrev_gate(ctx: Ctx) -> None:
    if not ctx.extrev_enabled:
        if ctx.extrev_rig_source:
            ctx.out(
                f"[ship] external-review gate disabled by Rig machine policy for {ctx.extrev_rig_target}. "
                "Other gates remain enabled."
            )
            external_review_audit_log(ctx, "external-review:disabled:rig-policy", ctx.extrev_rig_target)
        else:
            ctx.out(
                f"[ship] external-review gate disabled by the committed .ship-config on origin/{ctx.extrev_base} "
                f"(SHIP_EXTERNAL_REVIEW={ctx.extrev_cfg_value})."
            )
        return
    # A gh failure must reach the friendly fail-closed refusal, never abort the script unguarded.
    res = ctx.gh("pr", "view", ctx.pr, "--json", "reviews", "-q", "(.reviews // []) | length", stderr="devnull")
    count = res.out.rstrip("\n") if res.rc == 0 else ""
    if not _DIGITS.fullmatch(count):
        ctx.err(f"Refusing: could not query PR #{ctx.pr} reviews (gh api failed) — refusing to merge rather than fail open.")
        external_review_audit_log(ctx, "external-review:refused", "gh query failed")
        raise ShipExit(1)
    if int(count) > 0:
        ctx.out(f"[ship] external-review gate OK — PR #{ctx.pr} has {count} GitHub review(s).")
        return
    # Zero reviews AND every review-cli provider confirmed exhausted: nobody could have reviewed.

    cap_out = review_capacity_all_exhausted(ctx).rstrip("\n")
    cap_flag = 0
    cap_evidence = ""
    if cap_out.startswith("1"):
        cap_flag = 1
        cap_evidence = cap_out[2:] if cap_out.startswith("1\t") else cap_out
    if cap_flag == 1:
        ctx.out(
            f"[ship] external-review gate — PR #{ctx.pr} has zero GitHub reviews, but review-cli confirms EVERY "
            f"configured provider is exhausted right now ({cap_evidence or 'see `review capacity-check --json`'}) — "
            "auto-proceeding without a manual hatch prompt. Recorded to $SHIP_AUDIT_FILE."
        )
        external_review_audit_log(ctx, "external-review:auto-proceed:capacity-outage", cap_evidence)
    elif ctx.extrev_evidence_policy:
        # Deferred, not waived: the acceptance verdict is only known after the ticket gates below.
        ctx.extrev_evidence_pending = True
        ctx.out(
            f"[ship] external-review gate — PR #{ctx.pr} has zero GitHub reviews; Rig evidence policy for "
            f"{ctx.extrev_rig_target} defers the decision until after the acceptance gate (passes on green CI, "
            "review quorum met, acceptance proven, no unresolved threads or unaddressed comments; otherwise the hatch)."
        )
    else:
        external_review_hatch_or_refuse(ctx)


# --- ticket gates preamble --------------------------------------------------------------------


def ship_looks_like_a_ticket_id(ctx: Ctx, code: str) -> bool:
    """True iff `code` has a task-cli id shape: `#<n>`, `<n>`, or `PREFIX-<n>`."""
    return _TICKET_ID.fullmatch(code) is not None


def ship_code_names_this_pr(ctx: Ctx, code: str) -> bool:
    """True iff the candidate (`#<n>`, `<n>`, `GH-<n>`) is this PR's own number; fails open on a non-numeric PR."""
    n = code
    if n.startswith("#"):
        n = n[1:]
    elif _GH_PREFIX.match(n):
        n = n.split("-", 1)[1]
    if not _DIGITS.fullmatch(n):
        return False
    if not _DIGITS.fullmatch(ctx.pr or ""):
        return False
    return int(n) == int(ctx.pr)


def ship_task_code_candidate_ok(ctx: Ctx, candidate: str, source: str) -> bool:
    """Admission test for one task-code candidate; a non-empty rejected one is reported on stderr."""
    if not candidate:
        return False
    if not ship_looks_like_a_ticket_id(ctx, candidate):
        ctx.err(
            f"[ship] task-code: rejected '{candidate}' from {source} — not a task-cli id shape "
            "(#<n>, <n>, PREFIX-<n>); trying the next source."
        )
        return False
    if ship_code_names_this_pr(ctx, candidate):
        ctx.err(
            f"[ship] task-code: rejected '{candidate}' from {source} — it names this pull request "
            f"(#{ctx.pr}), not a ticket; trying the next source."
        )
        return False
    return True


def ship_derive_task_code_for_notify(ctx: Ctx) -> str:
    """Ticket code from $TASK_CODE -> branch -> PR title -> PR body (each validated); "" when none."""

    candidate = ctx.task_code
    if ship_task_code_candidate_ok(ctx, candidate, "$TASK_CODE"):
        return candidate

    candidate = review_quorum_extract_ticket(ctx, ctx.branch)
    if ship_task_code_candidate_ok(ctx, candidate, "the branch name"):
        return candidate

    res = ctx.gh(
        "pr", "view", ctx.pr, "--json", "title,body", "-q",
        '[(.title // ""), (.body // "")] | map(gsub("\\n";" ")) | @tsv',
        stderr="devnull",
    )
    # `IFS=$'\t' read -r a b` semantics: a line with no newline is rc 1 (both reset to empty); tab is
    # IFS-whitespace, so leading/consecutive/trailing tabs collapse.
    pr_title = pr_body_local = ""
    if "\n" in res.out:
        line = res.out.split("\n", 1)[0].lstrip("\t")
        if "\t" in line:
            pr_title, rest = line.split("\t", 1)
            pr_body_local = rest.lstrip("\t").rstrip("\t")
        else:
            pr_title = line

    candidate = review_quorum_extract_ticket(ctx, pr_title)
    if ship_task_code_candidate_ok(ctx, candidate, "the PR title"):
        return candidate

    candidate = review_quorum_extract_ticket(ctx, pr_body_local)
    if ship_task_code_candidate_ok(ctx, candidate, "the PR body"):
        return candidate

    return ""


def ticket_gate_audit_log(ctx: Ctx, gate: str, decision: str, code: str = "", detail: str = "") -> None:
    """One audit line for the acceptance / magic-close gate (dry-run: only announce it)."""
    if ctx.dry_run:
        suffix = (f" task={code}" if code else "") + (f" detail={detail}" if detail else "")
        ctx.err(f"[dry-run] would append {gate} audit: decision={decision}{suffix}")
        return
    ts = _now()
    if ctx.which("jq"):
        obj: dict[str, str] = {"ts": ts, "pr": ctx.pr, "gate": gate, "decision": decision}
        if code:
            obj["task_code"] = code
        if detail:
            obj["detail"] = detail
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        esc_code, esc_detail = _esc(code), _esc(detail)
        code_part = (',"task_code":"' + esc_code + '"') if esc_code else ""
        detail_part = (',"detail":"' + esc_detail + '"') if esc_detail else ""
        line = f'{{"ts":"{ts}","pr":"{_esc(ctx.pr)}","gate":"{gate}","decision":"{decision}"{code_part}{detail_part}}}'
    ctx.append_audit(line)


# --- magic-close keyword gate -----------------------------------------------------------------


def magic_close_matches(ctx: Ctx, text: str) -> list[str]:
    """Every "keyword ref" phrase in `text` (newlines folded to spaces first), leading boundary char dropped."""
    folded = text.replace("\n", " ")
    return [m.group(0)[len(m.group(1)):] for m in _MAGIC_CLOSE_RE.finditer(folded)]


def magic_close_rewrite(ctx: Ctx, text: str) -> str:
    """The text with every close keyword replaced by "Refs" (line by line, like sed)."""
    lines = [
        _MAGIC_CLOSE_RE.sub(lambda m: m.group(1) + "Refs" + m.group(6) + m.group(7), line)
        for line in text.split("\n")
    ]
    return "\n".join(lines).rstrip("\n")


def magic_close_gate(ctx: Ctx) -> None:
    title_res = ctx.gh("pr", "view", ctx.pr, "--json", "title", "-q", '.title // ""', stderr="devnull")
    body_res = ctx.gh("pr", "view", ctx.pr, "--json", "body", "-q", '.body // ""', stderr="devnull")
    # Commit messages count too: a squash merge folds each commit message into the final body.
    commits_res = ctx.gh(
        "pr", "view", ctx.pr, "--json", "commits", "-q",
        r'[.commits[] | (.messageHeadline // "") + "\n" + (.messageBody // "")] | join("\n")',
        stderr="devnull",
    )
    title_rc, body_rc, commits_rc = title_res.rc, body_res.rc, commits_res.rc
    # Fail closed on an unreadable fetch: "empty" must never be confused with "gh failed".
    if title_rc != 0 or body_rc != 0 or commits_rc != 0:
        ctx.err(
            f"Refusing: magic-close gate — could not read PR #{ctx.pr}'s title/body/commits from GitHub "
            f"(gh exit {title_rc}/{body_rc}/{commits_rc}) — cannot verify it is free of a close keyword."
        )
        ctx.err(
            "  Retry once gh/GitHub is reachable. SHIP_MAGIC_CLOSE_GATE=0 bypasses this gate entirely "
            "(ops off-switch) — only if you have verified the text by hand."
        )
        ticket_gate_audit_log(
            ctx, "magic-close", "refused", "",
            f"gh pr view failed: title_rc={title_rc} body_rc={body_rc} commits_rc={commits_rc}",
        )
        raise ShipExit(1)
    title = title_res.out.rstrip("\n")
    body = body_res.out.rstrip("\n")
    commits = commits_res.out.rstrip("\n")
    t_hits = magic_close_matches(ctx, title)
    b_hits = magic_close_matches(ctx, body)
    c_hits = magic_close_matches(ctx, commits)
    if not t_hits and not b_hits and not c_hits:
        ctx.out(f"[ship] magic-close gate: no close/fix/resolve keyword targets an issue or ticket in #{ctx.pr}'s title/body/commits.")
        return
    hits = _joined(t_hits, b_hits, c_hits)
    # title+body only: the part --rewrite-magic-close can actually fix.
    tb_hits = _joined(t_hits, b_hits)
    if ctx.rewrite_magic_close:
        if t_hits or b_hits:
            magic_close_rewrite_pr(ctx, title, body, t_hits, b_hits, tb_hits)
        if c_hits:
            # A commit-message keyword can never be fixed by a PR edit: refuse even after a title/body rewrite.
            ctx.err(
                f"Refusing: magic-close keyword found in a COMMIT message of PR #{ctx.pr} — --rewrite-magic-close can "
                "only edit the PR title/body, never commit history (this script never rewrites or rebases a branch)."
            )
            if t_hits or b_hits:
                ctx.err("  (the title/body keyword(s) were rewritten to Refs; the commit message keyword below was NOT — it still closes the ticket on merge.)")
            for line in _quoted_lines('  in a commit: "', c_hits):
                ctx.err(line)
            ctx.err(
                "  Fix by hand: amend the offending commit message(s) locally, force-push the branch yourself (ship never does), "
                'then re-run ship — or reword just to drop the keyword (e.g. "see HYP-1295" instead of "Fixes HYP-1295").'
            )
            ticket_gate_audit_log(ctx, "magic-close", "refused", "", hits)
            raise ShipExit(1)
        ticket_gate_audit_log(ctx, "magic-close", "rewritten", "", tb_hits)
        return
    ctx.err(
        f"Refusing: magic-close keyword in PR #{ctx.pr} — it would close the ticket the instant the PR merges, "
        "behind task-cli's acceptance gates."
    )
    for line in _quoted_lines('  in the title: "', t_hits):
        ctx.err(line)
    for line in _quoted_lines('  in the body:  "', b_hits):
        ctx.err(line)
    for line in _quoted_lines('  in a commit: "', c_hits):
        ctx.err(line)
    ctx.err(
        "  Why: GitHub's Closes/Fixes/Resolves #N and Linear's GitHub integration (the same words before a ticket code) "
        "move the ticket to Done on merge, so no criterion is ever checked with a proof."
    )
    ctx.err(
        f'  Fix: write "Refs <ref>" instead (e.g. "Refs #115", "Refs HYP-1295") — `gh pr edit {ctx.pr} --title/--body` — then re-run ship;'
    )
    if c_hits:
        ctx.err(
            "       a COMMIT message keyword has no automatic fix — amend it and force-push the branch yourself, or reword "
            "to drop the keyword; --rewrite-magic-close can fix a title/body hit but never a commit one."
        )
    else:
        ctx.err(
            "       or re-run with --rewrite-magic-close to have ship rewrite the keyword(s) to Refs via 'gh pr edit' and continue."
        )
    ctx.err("  SHIP_MAGIC_CLOSE_GATE=0 disables this gate entirely (ops off-switch).")
    ticket_gate_audit_log(ctx, "magic-close", "refused", "", hits)
    raise ShipExit(1)


def magic_close_rewrite_pr(
    ctx: Ctx, title: str, body: str, t_hits: list[str], b_hits: list[str], tb_hits: str
) -> None:
    """Rewrite close keywords in title/body via `gh pr edit`; refuses (exit 1) on a residual phrase or edit failure.

    Does not audit-log success: the caller logs exactly one final decision per gate run.
    """
    new_title, new_body = title, body
    edit_args: list[str] = []
    if t_hits:
        new_title = magic_close_rewrite(ctx, title)
        edit_args += ["--title", new_title]
    if b_hits:
        new_body = magic_close_rewrite(ctx, body)
        edit_args += ["--body", new_body]
    # Verify the locally computed rewrite (no re-fetch): a cross-line phrase the line-based rewrite
    # missed is caught in both dry-run and real runs, with no network call to fail open on.
    residual = _joined(magic_close_matches(ctx, new_title), magic_close_matches(ctx, new_body))
    if residual:
        ctx.err(
            f"Refusing: magic-close gate — rewriting PR #{ctx.pr} would still leave a close keyword in the title/body "
            "(a phrase the line-based rewrite could not reach — e.g. split across lines)."
        )
        for line in _quoted_lines('  still present: "', residual.split(";")):
            ctx.err(line)
        ctx.err('  Fix by hand: edit the PR title/body directly (write "Refs <ref>"), then re-run ship.')
        ticket_gate_audit_log(ctx, "magic-close", "refused", "", f"rewrite-incomplete: {residual}")
        raise ShipExit(1)
    ctx.out(f"[ship] magic-close gate: --rewrite-magic-close — rewriting to Refs in #{ctx.pr}: {tb_hits}")
    if ctx.run_gh("pr", "edit", ctx.pr, *edit_args, check=False) != 0:
        ctx.err(f"Refusing: magic-close gate — 'gh pr edit {ctx.pr}' failed while rewriting the keyword(s) to Refs ({tb_hits}).")
        ctx.err('  Fix the title/body by hand (write "Refs <ref>"), then re-run ship.')
        ticket_gate_audit_log(ctx, "magic-close", "refused", "", f"rewrite-failed: {tb_hits}")
        raise ShipExit(1)


# --- segment top level ------------------------------------------------------------------------


def run(ctx: Ctx) -> None:
    # The Ctx defaults (enabled, empty strings) hold until the lookups below change them.
    _extrev_base_offswitch_lookup(ctx)
    _extrev_rig_policy(ctx)
    _extrev_gate(ctx)

    gate_env = ctx.env.get("SHIP_MAGIC_CLOSE_GATE", "1") or "1"
    ctx.magic_close_enabled = gate_env not in ("0", "false", "no")
    if not ctx.magic_close_enabled:
        ctx.out(f"[ship] magic-close gate disabled (SHIP_MAGIC_CLOSE_GATE={ctx.env.get('SHIP_MAGIC_CLOSE_GATE', '')}).")
    else:
        magic_close_gate(ctx)

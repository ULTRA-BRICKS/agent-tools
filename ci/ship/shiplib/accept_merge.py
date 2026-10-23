"""Pre-merge acceptance gate, external-review evidence decision, Forgejo preflight, the merge, notify.

Port of ship.orig.sh lines 3873-4391. `run(ctx)` ends right after the task-cli notify step.
"""
from __future__ import annotations

import json
import os
import re
import shutil

from shiplib.ci_gate import THREAD_Q
from shiplib.ctx import ShipExit


def _which(ctx, name):
    return shutil.which(name, path=ctx.env.get("PATH", ""))


def _have_jq(ctx):
    return _which(ctx, "jq") is not None


def _flat(text):
    """`LC_ALL=C tr '\\n' ' '`."""
    return text.replace("\n", " ")


def _flag(value):
    """A bash 0/1 flag that another module may store as bool or as "1"/"0"."""
    return value is True or value == 1 or value == "1"


def _jq_parse(text):
    """`jq -e .`: the parsed value, or None when it is not valid JSON or is null/false."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if value is None or value is False:
        return None
    return value


def _jq_str(value):
    """jq string interpolation / tostring."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _jq_items(value):
    """`.[]?`: array elements, object values, nothing for scalars/null."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _field(item, key):
    return item.get(key) if isinstance(item, dict) else None


def _jq_default(value, fallback):
    """`value // fallback`."""
    return fallback if value is None or value is False else value


def _parsed_when_jq(ctx, text):
    return _jq_parse(text) if _have_jq(ctx) else None


def acceptance_gate_print_gaps(ctx, gate_json):
    """`task gate --json` stdout -> the open criteria, one per line (written to stderr by the caller)."""
    data = _parsed_when_jq(ctx, gate_json)
    if data is None:
        ctx.err("    " + gate_json)
        return
    if not isinstance(data, dict):
        return
    for item in _jq_items(data.get("unchecked")):
        ctx.err(f"    [{_jq_str(_field(item, 'index'))}] {_jq_str(_field(item, 'text'))}  (unchecked)")
    for item in _jq_items(data.get("proofless")):
        ctx.err(f"    [{_jq_str(_field(item, 'index'))}] {_jq_str(_field(item, 'text'))}  (checked without a proof)")
    criteria = _jq_default(data.get("criteria"), 1)
    if criteria == 0 and criteria is not False:
        ctx.err("    (the ticket has no acceptance criteria at all — nothing can be accepted)")
    elif _jq_default(data.get("below_minimum"), False):
        ctx.err(f"    (only {_jq_str(criteria)} acceptance criteria — below this repo's configured minimum, even with every one it has proven)")


def acceptance_gate_detail(ctx, gate_json):
    """`task gate --json` stdout -> one-line audit detail."""
    data = _parsed_when_jq(ctx, gate_json)
    if data is None:
        return _flat(gate_json)
    if not isinstance(data, dict):
        return ""
    unchecked = ",".join(_jq_str(_field(i, "index")) for i in _jq_items(data.get("unchecked")))
    proofless = ",".join(_jq_str(_field(i, "index")) for i in _jq_items(data.get("proofless")))
    return (
        f"criteria={_jq_str(_jq_default(data.get('criteria'), 0))} "
        f"below_minimum={_jq_str(_jq_default(data.get('below_minimum'), False))} "
        f"unchecked={unchecked} proofless={proofless}"
    )


def acceptance_gate_refuse(ctx, code, gate_json):
    """Print the refusal block, audit it, and exit 1 (raises ShipExit)."""
    from shiplib.extrev_ticket import ticket_gate_audit_log

    ctx.err(f"Refusing: acceptance gate — ticket {code} is NOT accepted (task gate {code} exit 1); merging now would close it behind task-cli with these criteria unproven:")
    acceptance_gate_print_gaps(ctx, gate_json)
    ctx.err(f"  Fix: task accept {code}   (walks each open criterion, asking for a proof), or")
    ctx.err(f"       task check {code} <n> [<n> ...] --proof <path> [--proof <path> ...]   (one proof per criterion, in order; --force \"<reason>\" when a proof is impossible)")
    ctx.err(f"       then `task gate {code}` must exit 0. If acceptance is inherently post-merge (a release publish, a deploy the merge triggers):")
    ctx.err(f"       task change {code} --post-merge-acceptance \"<reason>\"   — recorded ON the ticket and in the ship audit log; task done still needs real proofs afterwards.")
    ctx.err("  SHIP_ACCEPTANCE_GATE=0 (env, or a committed .ship-config line) disables this gate entirely (ops off-switch).")
    ticket_gate_audit_log(ctx, "acceptance", "refused", code, acceptance_gate_detail(ctx, gate_json))
    raise ShipExit(1)


def acceptance_gate_fully_accepted(ctx, gate_json):
    """Exit-0 payload safe to auto-close on (proven, not opt-out/cancelled/done/disabled) -> "1" or ""."""
    if not _have_jq(ctx):
        return ""
    data = _jq_parse(gate_json)
    if not isinstance(data, dict):
        return ""
    ok = (
        data.get("post_merge_acceptance") is None
        and data.get("gate_enabled") is not False
        and data.get("state") != "cancelled"
        and data.get("state") != "done"
    )
    return "1" if ok else ""


def acceptance_gate_strict(ctx):
    """Where the strict acceptance path applies (Forgejo provider / --task-config), else ""."""
    where = ""
    if (ctx.provider_resolved or "github") == "forgejo":
        where = "on the Forgejo provider"
    if ctx.task_config:
        where = (where + ", " if where else "") + f"with --task-config {ctx.task_config}"
    return where


def acceptance_gate_strict_verify(ctx, code, gate_json):
    """Strict path only: exit 0 counts only with ok=true, gate_enabled=true, not cancelled."""
    from shiplib.extrev_ticket import ticket_gate_audit_log

    where = acceptance_gate_strict(ctx)
    if not where:
        return
    if _have_jq(ctx):
        data = _jq_parse(gate_json)
        if (
            isinstance(data, dict)
            and data.get("ok") is True
            and data.get("gate_enabled") is True
            and data.get("state") != "cancelled"
        ):
            return
    ctx.err(f"Refusing: acceptance gate {where} — task gate {code} exited 0 but did not report an enforced acceptance (ok=true, gate_enabled=true, not cancelled): {_flat(gate_json)}")
    ctx.err("  An acceptance gate disabled for this ticket or repository is not evidence. Enable it (or record a post-merge-acceptance reason ON the ticket), then re-run ship.")
    ticket_gate_audit_log(ctx, "acceptance", "refused", code, "strict: exit 0 without an enforced ok")
    raise ShipExit(1)


def acceptance_gate_pass(ctx, code, gate_json):
    """Exit-0 verdict: records the code/fully-accepted evidence for the notify step."""
    from shiplib.extrev_ticket import ticket_gate_audit_log

    acceptance_gate_strict_verify(ctx, code, gate_json)
    have_jq = _have_jq(ctx)
    reason = ""
    if have_jq:
        data = _jq_parse(gate_json)
        if isinstance(data, dict):
            reason = _jq_str(_jq_default(data.get("post_merge_acceptance"), "")).rstrip("\n")
    ctx.acceptance_gate_code = code
    if reason:
        ctx.out(f"[ship] acceptance gate: {code} passes on a recorded post-merge-acceptance opt-out — reason: {reason}")
        ctx.out(f"[ship]   the criteria are still owed after the merge: task accept {code}, then task done {code}.")
        ticket_gate_audit_log(ctx, "acceptance", "authorized:post-merge-opt-out", code, reason)
        # Deliberately NOT marking fully accepted: acceptance is not yet true on the opt-out.
        return
    ctx.out(f"[ship] acceptance gate: {code} accepted — task gate {code} exit 0 ({acceptance_gate_detail(ctx, gate_json)}).")
    if not have_jq:
        ticket_gate_audit_log(ctx, "acceptance", "authorized", code, "no jq on PATH — opt-out/cancelled/disabled status unverifiable, skipping auto-close")
        return
    if acceptance_gate_fully_accepted(ctx, gate_json):
        ticket_gate_audit_log(ctx, "acceptance", "authorized", code, "")
        ctx.acceptance_gate_fully_accepted = "1"
    else:
        ticket_gate_audit_log(ctx, "acceptance", "authorized", code, "already done, cancelled, or the acceptance-checked gate is disabled for this ticket — skipping auto-close")


def ship_normalize_gh_code_for_task_cli(ctx, code):
    """Rewrite a case-insensitive "GH-<n>" code to the literal "#<n>" task-cli expects (logged)."""
    match = re.fullmatch(r"[Gg][Hh]-([0-9]+)", code)
    if match and not code.endswith("\n"):
        rewritten = f"#{match.group(1)}"
        ctx.err(f"[ship] normalizing task-cli notify code: {code} -> {rewritten} (GitHub-issue convention)")
        return rewritten
    return code


def acceptance_gate_is_task_cli(ctx):
    """True when `task --help` identifies task-cli (Taskwarrior / go-task also install as `task`)."""
    res = ctx.cmd(["task", "--help"], stderr="devnull")
    if res.rc != 0:
        return False
    return "pre-merge gate" in res.out or "acceptance criteria" in res.out


def acceptance_gate_unavailable(ctx, code, detail, warning):
    """Logged skip on GitHub (returns None); refusal (ShipExit) on the strict path."""
    from shiplib.extrev_ticket import ticket_gate_audit_log

    where = acceptance_gate_strict(ctx)
    if where:
        ctx.err(f"Refusing: acceptance gate could not evaluate {code or 'the ticket'} {where} ({detail}) — ticket evidence is required, not skipped.")
        ctx.err(f"  Make `task gate {code or '<code>'} --json` work from this checkout (task-cli must route the ticket), then re-run ship.")
        ticket_gate_audit_log(ctx, "acceptance", "refused", code, f"forgejo: could not evaluate: {detail}")
        raise ShipExit(1)
    ctx.err(warning)
    ticket_gate_audit_log(ctx, "acceptance", "skipped", code, detail)


def acceptance_gate_looks_like_task_cli_json(ctx, text):
    """Whether `task gate` stdout has task-cli's own JSON shape (id, ok, criteria)."""
    if _have_jq(ctx):
        try:
            data = json.loads(text)
        except ValueError:
            return False
        return isinstance(data, dict) and "id" in data and "ok" in data and "criteria" in data
    return '"criteria"' in text


def acceptance_gate(ctx):
    from shiplib.ci_fallback import ship_config_load
    from shiplib.extrev_ticket import ship_derive_task_code_for_notify, ticket_gate_audit_log

    gate = ctx.env.get("SHIP_ACCEPTANCE_GATE") or "1"
    if gate in ("0", "false", "no"):
        ctx.out(f"[ship] acceptance gate disabled (SHIP_ACCEPTANCE_GATE={gate}).")
        return
    ship_config_load(ctx, ctx.root)
    cfg = getattr(ctx, "ship_cfg_acceptance_gate", "")
    if cfg in ("0", "false", "no"):
        ctx.out(f"[ship] acceptance gate disabled by the committed .ship-config (SHIP_ACCEPTANCE_GATE={cfg}).")
        return
    task_path = _which(ctx, "task")
    if not task_path:
        acceptance_gate_unavailable(
            ctx, "", "task-cli not on PATH",
            "[ship] acceptance gate: task-cli not on PATH — skipping (install task-cli to gate merges on accepted criteria).")
        return
    if ctx.foreign_repo_invoke:
        ctx.err("[ship] acceptance gate: --repo targets a foreign remote — skipping (a local 'task' here reads the wrong project).")
        ticket_gate_audit_log(ctx, "acceptance", "skipped", "", "foreign --repo")
        return
    code = ship_derive_task_code_for_notify(ctx)
    if not code:
        if not acceptance_gate_is_task_cli(ctx):
            acceptance_gate_unavailable(
                ctx, "", "'task' on PATH does not look like task-cli",
                f"[ship] WARNING: acceptance gate could not evaluate — '{task_path}' on PATH did not look like task-cli (a DIFFERENT 'task' binary — e.g. Taskwarrior or go-task — may be shadowing task-cli). Skipping.")
            return
        ctx.err(f"Refusing: acceptance gate — could not derive a task code for #{ctx.pr}. A PR with no ticket has no acceptance criteria, so nothing can be proven.")
        ctx.err("  Fix: task create   (open a ticket with acceptance criteria) and reference it from the branch, title, or body (Refs #N / Refs HYP-N), then task gate <code> must exit 0.")
        ctx.err("  SHIP_ACCEPTANCE_GATE=0 (env, or a committed .ship-config line) disables this gate entirely (ops off-switch).")
        ticket_gate_audit_log(ctx, "acceptance", "refused", "", "no task code")
        raise ShipExit(1)
    # A "GH-<n>" code is a convention, not a task-cli id: task-cli expects the literal "#<n>".
    code = ship_normalize_gh_code_for_task_cli(ctx, code)
    ctx.out(f"[ship] acceptance gate: asking task-cli — task gate {code} --json ...")
    # stdout and stderr are captured SEPARATELY: stderr noise must never corrupt the JSON verdict.
    res = ctx.cmd(["task", "gate", code, *ctx.task_config_args, "--json"], cwd=ctx.root, stderr="capture")
    rc = res.rc
    out = res.out.rstrip("\n")
    if res.err:
        ctx.err(f"[ship] acceptance gate: task-cli stderr: {_flat(res.err)}")
    # Trust neither exit 0 nor exit 1 unless the output has task-cli's own JSON shape: a foreign
    # `task` that happens to exit 0 on garbage must not fail OPEN.
    if rc in (0, 1):
        if not acceptance_gate_looks_like_task_cli_json(ctx, out):
            acceptance_gate_unavailable(
                ctx, code, "'task' on PATH does not look like task-cli",
                f"[ship] WARNING: acceptance gate could not evaluate {code} — '{task_path}' on PATH did not return task-cli's expected JSON shape (a DIFFERENT 'task' binary — e.g. Taskwarrior or go-task — may be shadowing task-cli). Skipping: {_flat(out)}")
            return
    if rc == 0:
        acceptance_gate_pass(ctx, code, out)
    elif rc == 1:
        acceptance_gate_refuse(ctx, code, out)
    else:
        acceptance_gate_unavailable(
            ctx, code, f"could not evaluate: exit {rc}",
            f"[ship] WARNING: acceptance gate could not evaluate {code} (task gate exit {rc}) — skipping: {_flat(out)}")


# --- external-review evidence decision -----------------------------------------------------

_UNADDRESSED_JQ = """
    (.author.login // "") as $me
    | if $me == "" then error("PR author unknown") else . end
    | (.comments // []) as $all
    | [$all[] | . as $c
       | select((.author.login // "") != $me)
       | select((.isMinimized // false) | not)
       | select($c.createdAt == null
           or ([$all[] | select((.author.login // "") == $me and .createdAt != null and .createdAt > $c.createdAt)] | length == 0))
      ] | length"""


def external_review_unaddressed_comments(ctx):
    """Count of unaddressed PR comments as text, or None on a failed read (fail closed)."""
    res = ctx.gh("pr", "view", ctx.pr, "--json", "author,comments", "-q", _UNADDRESSED_JQ, stderr="devnull")
    if res.rc != 0:
        return None
    return res.out.rstrip("\n")


def _awk_sum_first_fields(text):
    total = 0.0
    for line in text.split("\n"):
        fields = line.split()
        if not fields:
            continue
        m = re.match(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", fields[0])
        if m:
            total += float(m.group(0))
    return str(int(total)) if total == int(total) else ("%.6g" % total)


def external_review_evidence_gate(ctx):
    from shiplib.extrev_ticket import external_review_audit_log, external_review_hatch_or_refuse

    if not _flag(getattr(ctx, "extrev_evidence_pending", "0")):
        return
    missing = []
    if ctx.ci_evidence not in ("green", "skip-ci"):
        missing.append("CI is not green (no checks, a CI-outage or known-flake local fallback is not green CI)")
    if ctx.quorum_evidence != "met":
        missing.append("the review quorum was not met on review-cli's own counts")
    if ctx.acceptance_gate_fully_accepted != "1":
        missing.append("acceptance is not fully proven (the acceptance gate was skipped or disabled, passed on a post-merge opt-out, or the ticket is already closed)")
    res = ctx.gh(
        "api", "graphql", "--paginate", "-F", "owner={owner}", "-F", "name={repo}", "-F", f"pr={ctx.pr}",
        "-f", f"query={THREAD_Q}",
        "--jq", "[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length",
        stderr="devnull")
    if res.rc == 0:
        threads = _awk_sum_first_fields(res.out)
        if threads != "0":
            missing.append(f"{threads} unresolved review thread(s)")
    else:
        missing.append("could not read the review threads")
    comments = external_review_unaddressed_comments(ctx)
    if comments is None:
        comments = ""
    if comments == "0":
        pass
    elif comments == "" or not comments.isascii() or not comments.isdigit():
        missing.append("could not read the PR comments")
    else:
        missing.append(f"{comments} unaddressed PR comment(s) — reply as the PR author or hide them as resolved")
    if not missing:
        ctx.out(f"[ship] external-review gate PASSED on evidence (Rig policy for {ctx.extrev_rig_target}): PR #{ctx.pr} has zero GitHub reviews, but CI is {ctx.ci_evidence}, the review quorum is met for {ctx.task_code or 'the ticket'}, acceptance is proven for {ctx.acceptance_gate_code}, and there are no unresolved threads or unaddressed comments.")
        external_review_audit_log(ctx, "external-review:authorized:evidence", f"ci={ctx.ci_evidence} quorum={ctx.task_code} acceptance={ctx.acceptance_gate_code}")
        return
    ctx.err(f"[ship] external-review gate: Rig evidence policy for {ctx.extrev_rig_target} NOT satisfied for PR #{ctx.pr} (zero GitHub reviews):")
    for item in missing:
        ctx.err(f"    - {item}")
    external_review_hatch_or_refuse(ctx)


# --- Forgejo final preflight ---------------------------------------------------------------

def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read().split("\n")
    except OSError:
        return []


def ship_forgejo_final_preflight(ctx):
    """Refuse on any entry in the Forgejo evidence ledger (re-reads the PR once first)."""
    if (ctx.provider_resolved or "github") != "forgejo":
        return
    ctx.gh("pr", "view", ctx.pr, "--json", "headRefOid", "-q", ".headRefOid", stdout="devnull", stderr="devnull")
    state_dir = getattr(ctx, "ship_forgejo_state_dir", "")
    fatal = os.path.join(state_dir, "fatal.log")
    calls = os.path.join(state_dir, "calls.log")
    problem = ""
    if (not os.path.isfile(os.path.join(state_dir, "ready")) or not os.path.isfile(fatal)
            or not os.path.isfile(calls) or os.path.lexists(os.path.join(state_dir, "broken"))):
        problem = "the evidence ledger is missing or could not be written"
    elif os.path.getsize(fatal) > 0:
        problem = "provider call(s) failed"
    else:
        lines = _read_lines(calls)
        starts = lines.count("start")
        oks = lines.count("ok")
        if not (starts > 0 and starts == oks):
            problem = f"provider call(s) did not complete (started {starts}, completed {oks})"
    if not problem:
        return
    ctx.err(f"Refusing: Forgejo provider evidence is incomplete for this run — {problem}; no gate result can be trusted:")
    if os.path.isfile(fatal) and os.path.getsize(fatal) > 0:
        lines = _read_lines(fatal)
        if lines and lines[-1] == "":
            lines.pop()
        for line in lines:
            ctx.err("  - " + line)
    ctx.err("  Fix the cause (or drop the GitHub-only flag that needs it), then re-run ship.")
    raise ShipExit(1)


# --- task-cli notify -----------------------------------------------------------------------

def _run_task_from_root(ctx, argv, cd_failure_msg):
    """bash `( cd "$ROOT" || { warn; exit 1; }; run task ... )` -> True when it succeeded."""
    if not os.path.isdir(ctx.root):
        ctx.err(cd_failure_msg)
        return False
    return ctx.run(argv, check=False, cwd=ctx.root) == 0


def ship_auto_close_accepted_ticket(ctx, code):
    """`task done` when the pre-merge gate fully accepted exactly this ticket; warning on failure."""
    from shiplib.extrev_ticket import ticket_gate_audit_log

    if not ctx.acceptance_gate_fully_accepted:
        return
    if code != ctx.acceptance_gate_code:
        return
    ctx.out(f"[ship] acceptance gate: {code} was fully accepted before the merge — closing it now: task done {code} ...")
    if _run_task_from_root(ctx, ["task", "done", code, *ctx.task_config_args],
                           f"[ship] WARNING: could not cd to {ctx.root} to auto-close {code}."):
        ctx.out(f"[ship] {code} closed — task done succeeded (acceptance was already proven before the merge).")
        ticket_gate_audit_log(ctx, "acceptance", "auto-closed", code, "")
    else:
        ctx.err(f"[ship] WARNING: acceptance criteria were fully proven pre-merge, but 'task done {code}' still failed (another close gate — formatting/links/screenshots/etc. — is likely blocking it). Close it by hand: task done {code}")
        ticket_gate_audit_log(ctx, "acceptance", "auto-close-failed", code, "")


def ship_notify_task_cli(ctx):
    """Best-effort `task mark-shipped` for the merged PR; every failure is a logged skip/warning."""
    from shiplib.extrev_ticket import ship_derive_task_code_for_notify

    notify = ctx.env.get("SHIP_TASK_NOTIFY_ENABLED") or "1"
    if notify in ("0", "false", "no"):
        ctx.err(f"[ship] task-cli notify disabled (SHIP_TASK_NOTIFY_ENABLED={notify}).")
        return
    if not _which(ctx, "task"):
        ctx.err("[ship] task-cli not on PATH — skipping ticket notify.")
        return
    if ctx.foreign_repo_invoke:
        ctx.err("[ship] --repo targets a foreign remote — skipping ticket notify (a local 'task' here would write into the wrong project).")
        return
    code = ship_derive_task_code_for_notify(ctx)
    code = ship_normalize_gh_code_for_task_cli(ctx, code)
    if not code:
        ctx.err(f"[ship] could not derive a task code for #{ctx.pr} — skipping ticket notify (not every PR is tied to a tracked ticket; update it manually if this one is).")
        return
    res = ctx.gh("pr", "view", ctx.pr, "--json", "url,mergeCommit",
                 "-q", '[(.url // ""), (.mergeCommit.oid // "")] | @tsv', stderr="devnull")
    pr_url = merge_sha = ""
    # `IFS=$'\t' read -r a b`: tab is IFS-whitespace (runs collapse, ends trim); no newline = EOF = rc 1.
    if "\n" in res.out:
        line = res.out.split("\n", 1)[0].strip("\t")
        parts = re.split("\t+", line, maxsplit=1) if line else []
        pr_url = parts[0] if parts else ""
        merge_sha = parts[1] if len(parts) > 1 else ""
    if not pr_url:
        ctx.err(f"[ship] could not resolve PR #{ctx.pr}'s URL — skipping ticket notify.")
        return
    ctx.out(f"[ship] notifying task-cli: {code} shipped via {pr_url} ...")
    mark_args = ["mark-shipped", code, "--pr", pr_url, *ctx.task_config_args]
    if merge_sha:
        mark_args += ["--commit", merge_sha]
    if _run_task_from_root(ctx, ["task", *mark_args], f"[ship] WARNING: could not cd to {ctx.root} for task-cli notify."):
        ship_auto_close_accepted_ticket(ctx, code)
    else:
        ctx.err(f"[ship] WARNING: 'task mark-shipped {code}' failed — the ticket may be out of sync with this merge. Update it manually: task read {code}")


# --- the segment's top-level statements ----------------------------------------------------

def _merge(ctx, extra):
    """`run gh pr merge ...` through ctx.gh so Forgejo routing applies; dry-run only prints."""
    args = ["pr", "merge", ctx.pr, f"--{ctx.merge_method}", *extra, *ctx.merge_message_args]
    if ctx.dry_run:
        ctx.out("[dry-run] gh " + " ".join(args))
        return
    rc = ctx.gh(*args, stdout="inherit").rc
    if rc != 0:
        raise ShipExit(rc)


def run(ctx):
    from shiplib.ci_fallback import known_flake_audit_log
    from shiplib.quorum_skipci import skip_ci_hatch_gate

    acceptance_gate(ctx)
    external_review_evidence_gate(ctx)

    if ctx.skip_ci:
        # Deny-by-default: the admin bypass proceeds only on a one-time live Telegram approval.
        skip_ci_hatch_gate(ctx)
        ship_forgejo_final_preflight(ctx)
        ctx.out(f"[ship] --skip-ci: admin-merging PR #{ctx.pr} ({ctx.branch}) --{ctx.merge_method} --admin ...")
        _merge(ctx, ["--admin"])
    else:
        ship_forgejo_final_preflight(ctx)
        ctx.out(f"[ship] preflight clean — merging PR #{ctx.pr} ({ctx.branch}) --{ctx.merge_method} ...")
        _merge(ctx, [])
    # Reaching here means the merge (or its dry-run stand-in) succeeded: only now is a
    # known-flake claim "confirmed" for the audit trail.
    if ctx.kf_audit_pending:
        known_flake_audit_log(ctx, "confirmed", ctx.kf_audit_pending)

    if ctx.dry_run:
        ctx.out("[ship] [dry-run] would notify task-cli of the merge.")
    else:
        try:
            ship_notify_task_cli(ctx)
        except Exception as exc:  # noqa: BLE001 - best-effort step after a durable merge (ShipExit included)
            ctx.err(f"[ship] WARNING: task-cli notify step hit an unexpected error ({exc!r}) — #{ctx.pr} IS merged; check/update the ticket manually.")

"""CI-down detection, known-flake gate, `.ship-config` loader and the local CI fallback gate.

Port of ship.sh lines 790-1593. When CI infrastructure is structurally down (not genuinely failing)
the local fallback gate (tests + leftover-marker scan + PR checklist + review threads) stands in.

Knobs (env): SHIP_CI_STATUS_URL (status API override for tests), SHIP_TEST_CI_DOWN=1 (force the
CI-down path, test-only), SHIP_LOCAL_TEST_CMD (override the local test command, test-only),
SHIP_FLAKE_LOOKBACK_RUNS (known-flake window, default 5, clamped to 25), SHIP_AUDIT_FILE.

Globals other segments read are set on ctx by `run` / `ship_config_load`:
ctx.ship_cfg_dir, ctx.ship_cfg_cmd, ctx.ship_cfg_acceptance_gate, ctx.ship_cfg_external_review.
They are overwritten on every `ship_config_load` call (no per-ref namespacing): consume them at once.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import time

_WS = " \t\n\v\f\r"  # bash [[:space:]]
_FLAKE_CONCLUSIONS = ("failure", "cancelled", "timed_out", "action_required")


def run(ctx) -> None:
    """Initialise the `.ship-config` globals; every other statement in this segment is a function."""
    ctx.ship_cfg_dir = ""
    ctx.ship_cfg_cmd = ""
    ctx.ship_cfg_acceptance_gate = ""
    ctx.ship_cfg_external_review = ""


def git_blob(ctx, *args, **kw):
    """`git <args>` with MSYS path conversion off (a `<rev>:<path>` spec is never a filesystem path)."""
    env = dict(ctx.env)
    env["MSYS_NO_PATHCONV"] = "1"
    return ctx.cmd(["git", *args], env=env, **kw)


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0]


# ---------------------------------------------------------------------------------------------
# known-flake gate
# ---------------------------------------------------------------------------------------------
def known_flake_confirmed(ctx, check_name, wf_name, base) -> bool:
    """True iff `check_name` FAILED in one of the last N completed runs of the workflow on `base`
    (fail-closed: any read failure, empty list or no match is False)."""
    if not base:
        return False
    n_raw = ctx.env.get("SHIP_FLAKE_LOOKBACK_RUNS", "5")
    n = int(n_raw) if re.fullmatch(r"[0-9]+", n_raw) else 5
    if not n > 0:
        n = 5
    # Clamp: an unbounded window turns the refusal path into hundreds of sequential `gh run view`
    # calls before ship even gets to refuse.
    if n > 25:
        n = 25
    args = ["run", "list", "--branch", base]
    if wf_name:
        args += ["--workflow", wf_name]
    args += ["--status", "completed", "--limit", str(n), "--json", "databaseId,conclusion"]
    res = ctx.gh(*args, stderr="devnull")
    if res.rc != 0:
        return False
    runs_json = res.out.rstrip("\n")
    if not runs_json or runs_json == "null":
        return False
    try:
        runs = json.loads(runs_json)
    except ValueError:
        return False
    if runs is None:
        runs = []
    if not isinstance(runs, list):
        return False
    for run_obj in runs:
        if not isinstance(run_obj, dict):
            return False
        rid_val = run_obj.get("databaseId")
        rid = rid_val if isinstance(rid_val, str) else json.dumps(rid_val)
        concl = run_obj.get("conclusion")
        if not rid:
            continue
        # A fully green run cannot contain a failing job with this name: skip the extra call.
        if concl not in _FLAKE_CONCLUSIONS:
            continue
        jobs_res = ctx.gh("run", "view", rid, "--json", "jobs", "-q", ".jobs", stderr="devnull")
        if jobs_res.rc != 0:
            continue
        try:
            jobs = json.loads(jobs_res.out)
        except ValueError:
            continue
        if jobs is None:
            jobs = []
        if not isinstance(jobs, list):
            continue
        matched = any(
            isinstance(j, dict) and j.get("name") == check_name and j.get("conclusion") == "failure"
            for j in jobs
        )
        if matched:
            ctx.err(
                f"[ship] known-flake evidence: '{check_name}' also FAILED on {base} run {rid} "
                "(recent, same workflow) — treating as pre-existing, not introduced by this PR."
            )
            return True
    return False


def known_flake_audit_log(ctx, decision, checks) -> None:
    """Append one known-flake line to SHIP_AUDIT_FILE (decision: confirmed|refused); dry-run only echoes."""
    if ctx.dry_run:
        ctx.err(f"[dry-run] would append known-flake audit: decision={decision} checks={checks}")
        return
    file = ctx.env.get("SHIP_AUDIT_FILE") or os.path.join(
        ctx.env.get("HOME", os.path.expanduser("~")), ".config/agent-tools/ship-audit.jsonl"
    )
    try:
        os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
    except OSError:
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = json.dumps(
        {"ts": ts, "pr": ctx.pr, "gate": "known-flake", "decision": decision, "checks": checks},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    try:
        with open(file, "a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# CI-down detection
# ---------------------------------------------------------------------------------------------
def ci_github_status_indicator(ctx) -> str:
    """Return "degraded" if the Actions component is not operational, "ok", or "unknown" on error."""
    url = ctx.env.get("SHIP_CI_STATUS_URL") or "https://www.githubstatus.com/api/v2/components.json"
    res = ctx.cmd(["curl", "-sS", "--max-time", "10", url], stderr="devnull")
    if res.rc != 0:
        return "unknown"
    indicator = ""
    try:
        data = json.loads(res.out)
    except ValueError:
        data = None
    comps = data.get("components") if isinstance(data, dict) else None
    if isinstance(comps, list):
        for comp in comps:
            if not isinstance(comp, dict):
                continue
            name = comp.get("name")
            if not isinstance(name, str):
                break  # jq's `test` errors on a non-string and ends the stream
            if re.search("Actions", name, re.I):
                status = comp.get("status")
                indicator = status if isinstance(status, str) else json.dumps(status)
                break
    if indicator == "operational":
        return "ok"
    if indicator == "":
        return "unknown"
    return "degraded"


def _parse_iso(ts) -> float:
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    ).timestamp()


def ci_runs_timing_suspicious(ctx, head_sha) -> str:
    """"1" if EVERY workflow run on head_sha completed in < 30s (infra/billing failure), else "0"."""
    res = ctx.gh(
        "api", f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={head_sha}&per_page=20", stderr="devnull"
    )
    if res.rc != 0:
        return "0"
    try:
        data = json.loads(res.out)
        runs = data["workflow_runs"]
        total = 0 if runs is None else len(runs)
    except (ValueError, KeyError, TypeError):
        total = 0
    if not total > 0:
        return "0"
    try:
        fast = sum(
            1
            for r in runs
            if r["status"] == "completed" and (_parse_iso(r["updated_at"]) - _parse_iso(r["created_at"])) < 30
        )
    except (ValueError, KeyError, TypeError):
        fast = 0
    return "1" if fast == total else "0"


def ci_billing_block_annotation_detected(ctx, head_sha) -> str:
    """"1" if any failed check run carries GitHub's billing-block annotation text, else "0"."""
    res = ctx.gh("api", f"repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs?per_page=100", stderr="devnull")
    if res.rc != 0:
        return "0"
    failed_ids = []
    try:
        data = json.loads(res.out)
        for cr in data.get("check_runs") or []:
            if cr.get("conclusion") == "failure":
                cid = cr.get("id")
                failed_ids.append(cid if isinstance(cid, str) else json.dumps(cid))
    except (ValueError, AttributeError, TypeError):
        pass
    if not failed_ids:
        return "0"
    pattern = re.compile("account payments have failed|spending limit needs to be increased", re.I)
    for cid in failed_ids:
        if not cid:
            continue
        ann = ctx.gh("api", f"repos/{{owner}}/{{repo}}/check-runs/{cid}/annotations", stderr="devnull")
        if ann.rc != 0:
            continue
        try:
            items = json.loads(ann.out)
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            msg = item.get("message") if isinstance(item, dict) else None
            if isinstance(msg, str) and pattern.search(msg):
                return "1"
    return "0"


def ci_appears_structurally_down(ctx, total, failed) -> bool:
    """True if CI looks structurally down (>= 80% failed plus an outage signal) rather than failing."""
    # Test escape hatch: force-trigger detection without the heuristics.
    if ctx.env.get("SHIP_TEST_CI_DOWN", "0") == "1":
        return True
    # Both outage signals are GitHub-only; a Forgejo PR's failing checks are never an outage.
    if (ctx.provider_resolved or "github") == "forgejo":
        return False
    total = int(total or 0)
    failed = int(failed or 0)
    if not total > 0:
        return False
    # Cross-multiplication avoids integer-truncation rounding (2 checks: 1/2 must not pass).
    if not failed * 10 >= total * 8:
        return False
    res = ctx.gh("pr", "view", ctx.pr, "--json", "headRefOid", "-q", ".headRefOid", stderr="devnull")
    head_sha = res.out.rstrip("\n") if res.rc == 0 else ""
    status = ci_github_status_indicator(ctx)
    timing = "0"
    billing = "0"
    if head_sha:
        timing = ci_runs_timing_suspicious(ctx, head_sha)
        billing = ci_billing_block_annotation_detected(ctx, head_sha)
    ctx.err(
        f"[ship] CI-down probe: github_status={status} timing_suspicious={timing} "
        f"billing_block={billing} total={total} failed={failed}"
    )
    return status == "degraded" or timing == "1" or billing == "1"


# ---------------------------------------------------------------------------------------------
# .ship-config loader
# ---------------------------------------------------------------------------------------------
def ship_config_load(ctx, root, ref="HEAD") -> None:
    """Parse `$ref:.ship-config` (committed content only, never the worktree) into ctx.ship_cfg_*.

    A caller gating a merge-safety property a PR must not grant itself passes an already-reviewed
    ref (e.g. origin/<base>) instead of HEAD."""
    ctx.ship_cfg_dir = ""
    ctx.ship_cfg_cmd = ""
    ctx.ship_cfg_acceptance_gate = ""
    ctx.ship_cfg_external_review = ""
    spec = f"{ref}:.ship-config"
    if git_blob(ctx, "show", spec, cwd=root, stdout="devnull", stderr="devnull").rc != 0:
        if ref == "HEAD" and os.path.isfile(os.path.join(root, ".ship-config")):
            ctx.err(
                "[ship] local gate: .ship-config exists in the working tree but is not committed at HEAD "
                "— ignoring (uncommitted config is not audited)."
            )
        return
    # Must be a REGULAR FILE tree entry: a tree or symlink is also type `blob`, and its content
    # (entry names / link target) would otherwise be parsed as KEY=value lines.
    ls = ctx.git("ls-tree", ref, "--", ".ship-config", cwd=root, stderr="devnull")
    fields = ls.out.split()
    head_mode = fields[0] if fields else ""
    if head_mode not in ("100644", "100755"):
        ctx.err(
            f"[ship] local gate: .ship-config at {ref} is not a regular file "
            f"(mode {head_mode or 'unknown'}, not 100644/100755) — ignoring."
        )
        return
    # NUL-byte guard on the RAW git-show output: bash strips NULs when capturing into a variable,
    # which could turn a non-key byte sequence into a whitelisted key.
    size_res = git_blob(ctx, "cat-file", "-s", spec, cwd=root, stderr="devnull")
    try:
        blob_size = int(size_res.out.rstrip("\n")) if size_res.rc == 0 else 0
    except ValueError:
        blob_size = 0
    shown = git_blob(ctx, "show", spec, cwd=root, stderr="devnull")
    if blob_size > 0 and (shown.rc != 0 or "\x00" in shown.out):
        ctx.err(
            "[ship] local gate: .ship-config committed content contains a NUL byte (binary/corrupt) "
            "— refusing to parse, ignoring."
        )
        return
    content = shown.out.replace("\x00", "").rstrip("\n") if shown.rc == 0 else ""
    for raw in content.split("\n"):
        line = raw.strip(_WS)
        if line == "" or line.startswith("#"):
            continue
        if line.startswith(
            ("SHIP_LOCAL_TEST_DIR=", "SHIP_LOCAL_TEST_CMD=", "SHIP_ACCEPTANCE_GATE=", "SHIP_EXTERNAL_REVIEW=")
        ):
            key, _, val = line.partition("=")
            val = val.strip(_WS)
            if key == "SHIP_LOCAL_TEST_DIR":
                ctx.ship_cfg_dir = val
            elif key == "SHIP_LOCAL_TEST_CMD":
                ctx.ship_cfg_cmd = val
            elif key == "SHIP_ACCEPTANCE_GATE":
                ctx.ship_cfg_acceptance_gate = val
            elif key == "SHIP_EXTERNAL_REVIEW":
                ctx.ship_cfg_external_review = val
        else:
            ctx.err(f"[ship] local gate: .ship-config: ignoring unrecognized line: {line}")
    if ctx.ship_cfg_dir and not ship_config_dir_is_safe(ctx, ctx.ship_cfg_dir):
        ctx.err(
            f"[ship] local gate: .ship-config: SHIP_LOCAL_TEST_DIR '{ctx.ship_cfg_dir}' is not a safe "
            "repo-relative subdirectory (absolute, a '..' path component, or '.'/repo root itself — "
            "omit the key to mean root) — ignoring the whole file."
        )
        # Rejection invalidates the WHOLE file so a still-present CMD is not relocated to the root.
        ctx.ship_cfg_dir = ""
        ctx.ship_cfg_cmd = ""
        ctx.ship_cfg_acceptance_gate = ""
        ctx.ship_cfg_external_review = ""


def ship_config_dir_is_safe(ctx, p) -> bool:
    """True if `p` is a repo-relative subdir: not absolute, no `..` COMPONENT, not the root itself."""
    if p.startswith("/"):
        return False
    all_dot = True
    for seg in _first_line(p).split("/"):
        if seg == "..":
            return False
        if seg != "." and seg != "":
            all_dot = False
    return not all_dot


def dir_is_real_descendant_of_root(ctx, root, dir) -> bool:
    """True if existing dir `dir` is a STRICT physical descendant of `root` (symlinks resolved)."""
    if not os.path.isdir(root) or not os.path.isdir(dir):
        return False
    real_root = os.path.realpath(root)
    real_dir = os.path.realpath(dir)
    if real_dir == real_root:
        return False
    return real_dir.startswith(f"{real_root}/")


def _eval_shell(ctx, command, cwd=None) -> int:
    """bash `eval "$command" 2>&1` under the script's `set -uo pipefail`: output goes to stdout."""
    return ctx.cmd(["bash", "-uo", "pipefail", "-c", command], cwd=cwd, stdout="inherit", stderr="stdout").rc


def _run_in(ctx, argv, cwd) -> int:
    return ctx.cmd(argv, cwd=cwd, stdout="inherit", stderr="stdout").rc


def local_test_try_dir(ctx, dir, mode="auto"):
    """Probe `dir` for pyproject.toml/package.json/Cargo.toml and run its test command from there.

    Returns (matched, status): callers branch on `matched`, never on the status, because a real
    test failure can legitimately exit with any code. mode "root" forces `pytest tests/ -q`."""
    if os.path.isfile(os.path.join(dir, "pyproject.toml")):
        ctx.out(f"[ship] local gate: running pytest ({dir}/pyproject.toml detected) ...")
        pytest_args = ["tests/", "-q"]
        if mode != "root" and not os.path.isdir(os.path.join(dir, "tests")):
            pytest_args = ["-q"]
        if shutil.which("uv", path=ctx.env.get("PATH")):
            return True, _run_in(ctx, ["uv", "run", "--with", "pytest", "pytest", *pytest_args], dir)
        return True, _run_in(ctx, [ctx.python, "-m", "pytest", *pytest_args], dir)
    if os.path.isfile(os.path.join(dir, "package.json")):
        ctx.out(f"[ship] local gate: running npm test ({dir}/package.json detected) ...")
        return True, _run_in(ctx, ["npm", "test"], dir)
    if os.path.isfile(os.path.join(dir, "Cargo.toml")):
        ctx.out(f"[ship] local gate: running cargo test ({dir}/Cargo.toml detected) ...")
        return True, _run_in(ctx, ["cargo", "test"], dir)
    return False, 0


def ship_config_run(ctx, root):
    """Execute the audited `.ship-config` override. Returns (acted, status); callers branch on
    `acted` (nothing configured) because the configured command may exit with any code."""
    ship_config_load(ctx, root)
    cfg_dir = ctx.ship_cfg_dir
    cfg_cmd = ctx.ship_cfg_cmd
    if not cfg_cmd and not cfg_dir:
        return False, 0
    if cfg_dir:
        if not os.path.isdir(f"{root}/{cfg_dir}"):
            ctx.err(f"[ship] local gate: FAILED — SHIP_LOCAL_TEST_DIR '{cfg_dir}' does not exist under {root}.")
            return True, 1
        if not dir_is_real_descendant_of_root(ctx, root, f"{root}/{cfg_dir}"):
            ctx.err(
                f"[ship] local gate: FAILED — SHIP_LOCAL_TEST_DIR '{cfg_dir}' resolves (via a symlink) "
                "outside the repo root."
            )
            return True, 1
    if cfg_cmd:
        if cfg_dir:
            ctx.out(f"[ship] local gate: .ship-config: running in {cfg_dir}: {cfg_cmd}")
            return True, _eval_shell(ctx, cfg_cmd, cwd=f"{root}/{cfg_dir}")
        ctx.out(f"[ship] local gate: .ship-config: running: {cfg_cmd}")
        return True, _eval_shell(ctx, cfg_cmd, cwd=root)
    ctx.out(f"[ship] local gate: .ship-config: scoping auto-detect to {cfg_dir}")
    matched, status = local_test_try_dir(ctx, f"{root}/{cfg_dir}")
    if matched:
        return True, status
    ctx.err(
        f"[ship] local gate: FAILED — no recognized test runner found in {cfg_dir} "
        "(no pyproject.toml/package.json/Cargo.toml)."
    )
    return True, 1


def local_test_runner(ctx, root) -> int:
    """Run the local test suite; returns the exit status (0 = passed). Priority: SHIP_LOCAL_TEST_CMD
    env > .ship-config > rig.yaml+dev > root manifest > e2e/ manifest > fail closed."""
    # Priority 1: test-only env override.
    env_cmd = ctx.env.get("SHIP_LOCAL_TEST_CMD", "")
    if env_cmd:
        ctx.out(f"[ship] local gate: running test command: {env_cmd}")
        return _eval_shell(ctx, env_cmd)

    # Priority 2: audited .ship-config outranks every heuristic below, rig.yaml included.
    acted, status = ship_config_run(ctx, root)
    if acted:
        return status

    # Priority 3: rig.yaml + `dev` CLI probe, root-only.
    path = ctx.env.get("PATH")
    if (
        os.path.isfile(os.path.join(root, "rig.yaml"))
        and shutil.which("dev", path=path)
        and ctx.cmd(["dev", "--agenttools-dev-probe"], stdout="devnull", stderr="devnull").rc == 0
    ):
        dev_status = ctx.cmd(
            ["dev", "has-script", "--repo-only", "test"], cwd=root, stdout="devnull", stderr="devnull"
        ).rc
        if dev_status == 0:
            ctx.out("[ship] local gate: running dev run --repo-only test (rig.yaml scripts.test)")
            return _run_in(ctx, ["dev", "run", "--repo-only", "test"], root)
        if dev_status != 1:
            ctx.err(
                "[ship] local gate: FAILED — dev has-script --repo-only test failed; "
                "refusing to guess a fallback test runner."
            )
            return 1

    # Priority 4: root-level auto-detect ("root" keeps the classic `pytest tests/ -q`).
    matched, status = local_test_try_dir(ctx, root, "root")
    if matched:
        return status

    # Priority 5: ONE bounded candidate, e2e/. Deliberately NOT test/ or tests/: those are where
    # repos keep FIXTURE manifests with trivially-passing tests, which would turn a conservative
    # fail-closed block into a false "verified" pass. Anything else belongs in .ship-config.
    if os.path.isdir(f"{root}/e2e"):
        if dir_is_real_descendant_of_root(ctx, root, f"{root}/e2e"):
            matched, status = local_test_try_dir(ctx, f"{root}/e2e")
            if matched:
                return status
        else:
            ctx.err(
                "[ship] local gate: e2e/ resolves (via a symlink) outside the repo root — skipping the "
                "priority-5 candidate (same guard as SHIP_LOCAL_TEST_DIR; use .ship-config for an "
                "intentional alias)."
            )

    ctx.err(
        "[ship] local gate: FAILED — no recognized test runner found "
        "(no pyproject.toml/package.json/Cargo.toml at root or in e2e/)."
    )
    ctx.err("[ship]   CI is down but tests cannot be verified locally — blocking conservatively.")
    return 1


# ---------------------------------------------------------------------------------------------
# local fallback checks
# ---------------------------------------------------------------------------------------------
# Bracketed first letters: this source never contains a marker as one contiguous token, so the gate
# does not self-trigger on a PR that edits it. [X]XX alone gets a run-length guard so mktemp
# templates (/tmp/foo.XXXXXX) do not match; the other three stay plain substrings on purpose.
_LEFTOVER_RE = re.compile(r"([T]ODO|[F]IXME|[H]ACK)|(^|[^X])[X]XX($|[^X])")


def local_leftover_check(ctx, pr) -> bool:
    """True if the PR diff additions contain no leftover markers; False if found or diff unreadable."""
    ctx.out("[ship] local gate: scanning PR diff for leftover markers ...")
    res = ctx.gh("pr", "diff", pr, stderr="devnull")
    if res.rc != 0:
        ctx.err("[ship] local gate: FAILED — could not read PR diff for leftover scan.")
        return False
    diff_out = res.out.rstrip("\n")
    hits = [
        ln
        for ln in diff_out.split("\n")
        if ln.startswith("+") and not ln.startswith("+++") and _LEFTOVER_RE.search(ln)
    ]
    if hits:
        ctx.err("[ship] local gate: FAILED — leftover markers in PR additions:")
        ctx.err("\n".join(hits))
        return False
    ctx.out("[ship] local gate: leftover-marker scan OK.")
    return True


def local_pr_checklist_check(ctx, pr) -> bool:
    """True if the PR body has no unchecked `- [ ]` checklist lines."""
    ctx.out("[ship] local gate: checking PR checklist ...")
    res = ctx.gh("pr", "view", pr, "--json", "body", "-q", '.body // ""', stderr="devnull")
    if res.rc != 0:
        ctx.err("[ship] local gate: FAILED — could not read PR body for checklist check.")
        return False
    body = res.out.rstrip("\n")
    unchecked = sum(1 for ln in body.split("\n") if ln.startswith("- [ ]"))
    if unchecked > 0:
        ctx.err(f"[ship] local gate: FAILED — PR has {unchecked} unchecked checklist item(s).")
        return False
    ctx.out("[ship] local gate: PR checklist OK.")
    return True


def local_review_threads_check(ctx, pr) -> bool:
    """True if no unresolved review threads (paginating GraphQL query); False on any, or query failure."""
    q = (
        "query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name)"
        "{pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} "
        "nodes{isResolved}}}}}"
    )
    ctx.out("[ship] local gate: checking review threads ...")
    res = ctx.gh(
        "api", "graphql", "--paginate", "-F", "owner={owner}", "-F", "name={repo}", "-F", f"pr={pr}",
        "-f", f"query={q}",
        "--jq", "[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length",
        stderr="devnull",
    )
    if res.rc != 0:
        ctx.err("[ship] local gate: FAILED — could not check review threads.")
        return False
    unresolved = 0.0
    for ln in res.out.split("\n"):
        fields = ln.split()
        if fields:
            m = re.match(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", fields[0])
            if m:
                unresolved += float(m.group(0))
    if unresolved > 0:
        ctx.err(f"[ship] local gate: FAILED — {int(unresolved)} unresolved review thread(s).")
        return False
    ctx.out("[ship] local gate: review threads OK.")
    return True


def run_local_ci_gate(ctx, why="CI infrastructure appears down") -> bool:
    """Run all local CI fallback gates; True only if EVERY one passes (conservative)."""
    ctx.out(f"[ship] === Running local CI fallback gates ({why}) ===")
    gate_failed = False
    if local_test_runner(ctx, ctx.root) != 0:
        gate_failed = True
    if not local_leftover_check(ctx, ctx.pr):
        gate_failed = True
    if not local_pr_checklist_check(ctx, ctx.pr):
        gate_failed = True
    if not local_review_threads_check(ctx, ctx.pr):
        gate_failed = True
    if not gate_failed:
        ctx.out("[ship] === Local CI fallback: ALL gates passed — safe to merge. ===")
        return True
    ctx.err("[ship] === Local CI fallback: FAILED — see above; not safe to merge. ===")
    return False


# ---------------------------------------------------------------------------------------------
# workflow `on:` analysis and empty-rollup outage detection
# ---------------------------------------------------------------------------------------------
def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def workflow_on_block(text) -> str:
    """Return ONLY the top-level `on:` block of a workflow (to the next top-level key), `#` comments
    stripped, so a `pull_request` elsewhere (comment, job name, `if:` expression) never counts."""
    out = []
    insec = False
    for ln in _split_lines(text):
        if re.match(r"on:([ \t\n\v\f\r]|$)", ln):
            insec = True
            out.append(ln)
            continue
        if insec and re.match(r"[^ \t\n\v\f\r#]", ln):
            insec = False
        if insec:
            out.append(ln)
    return "".join(re.sub(r"#.*", "", ln, count=1) + "\n" for ln in out)


def on_block_declares_pr_trigger(text) -> bool:
    """True iff the `on:` block declares a `pull_request`/`pull_request_target` TRIGGER, matched by
    indentation (a direct child of `on:`), never as a substring of a filter value."""
    childind = -1
    found = False
    for nr, ln in enumerate(_split_lines(text), start=1):
        if nr == 1 and re.match(r"on:[ \t\n\v\f\r]*[^ \t\n\v\f\r{]", ln):
            if re.match(r"on:[^{]*pull_request(_target)?([^0-9A-Za-z_]|$)", ln):
                found = True
            continue
        if re.fullmatch(r"[ \t\n\v\f\r]*", ln):
            continue
        ind = len(ln) - len(ln.lstrip(_WS))
        if ind == 0:
            continue
        if childind < 0:
            childind = ind
        if ind == childind:
            if re.match(r"[ \t\n\v\f\r]*pull_request(_target)?[ \t\n\v\f\r]*:", ln):
                found = True
            if re.match(r"[ \t\n\v\f\r]*-[ \t\n\v\f\r]*pull_request(_target)?[ \t\n\v\f\r]*$", ln):
                found = True
    return found


def empty_rollup_is_ci_outage(ctx) -> bool:
    """True iff an EMPTY statusCheckRollup is a CI outage: a workflow on the PR's BASE branch
    (remote `origin/<base>`, never local files) declares a pull_request trigger, so a check should
    have registered. Fails toward refuse (False) whenever the ref or workflows cannot be read."""
    res = ctx.gh("pr", "view", ctx.pr, "--json", "baseRefName", "-q", ".baseRefName", stderr="devnull")
    base = res.out.rstrip("\n") if res.rc == 0 else ""
    if not base or base.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]+", base):
        base = ctx.default_branch
    ref = f"origin/{base}"
    # Explicit refspec so a successful fetch definitely advances the remote-tracking ref;
    # best-effort (offline falls back to the last-known ref).
    ctx.cmd(
        ["git", "-C", ctx.root, "fetch", "-q", "origin", f"{base}:refs/remotes/origin/{base}"],
        stdout="inherit", stderr="devnull",
    )
    if ctx.cmd(
        ["git", "-C", ctx.root, "rev-parse", "--verify", "--quiet", ref], stdout="devnull", stderr="devnull"
    ).rc != 0:
        return False
    # Forgejo Actions reads the FIRST non-empty of .forgejo/workflows, .gitea/workflows,
    # .github/workflows; GitHub reads only .github/workflows.
    wf_dir = ".github/workflows"
    if (ctx.provider_resolved or "github") == "forgejo":
        for d in (".forgejo/workflows", ".gitea/workflows", ".github/workflows"):
            probe = ctx.cmd(
                ["git", "-C", ctx.root, "ls-tree", "--name-only", ref, "--", f"{d}/"], stderr="devnull"
            )
            if probe.out.rstrip("\n"):
                wf_dir = d
                break
    listing = ctx.cmd(
        ["git", "-C", ctx.root, "ls-tree", "-r", "--name-only", ref, "--", f"{wf_dir}/"], stderr="devnull"
    )
    for f in listing.out.split("\n"):
        if not f.endswith((".yml", ".yaml")):
            continue
        blob = git_blob(ctx, "show", f"{ref}:{f}", cwd=ctx.root, stderr="devnull")
        if on_block_declares_pr_trigger(workflow_on_block(blob.out)):
            return True
    return False

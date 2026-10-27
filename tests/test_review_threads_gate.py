"""Tests for the review-threads merge gate — agent-tools#65 (stale-green re-evaluation).

The shipped `ci/review-threads/workflow.yml` is copied verbatim by rig into every consumer,
so a trigger/tamper-resistance bug here is a bug in every rigged repo's required check.

#65: the gate only fired on PR-lifecycle events (opened/synchronize/...), so a review thread
ADDED AFTER an earlier green check left the required `review-threads` status DISHONESTLY green
— a PR could be merged via the GitHub UI over a fresh unresolved thread. The fix re-runs the
gate when a review/review-comment appears (the moment an unresolved thread is created) while
keeping the `pull_request_target` tamper-resistance intact.

These assertions pin:
  • the re-evaluation triggers (`pull_request_review`, `pull_request_review_comment`) exist,
  • the deliberately-EXCLUDED events stay out (each is a false-coverage no-op — see below),
  • the checkout pins `ref` to the trusted PR BASE branch (the review/comment triggers default
    to the PR MERGE ref, which contains PR-head edits to the gate script),
  • PR-number resolution covers every trigger that reaches the job.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_review_threads_gate.py -q
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

# `yaml` is imported per-test via importorskip (NOT at module level) so the string/structural
# checks below still RUN where the dependency-free `uv run --with pytest` env lacks PyYAML;
# only the safe_load parse-tests skip when it's absent.
REPO_ROOT = Path(__file__).resolve().parent.parent
RT_WF = REPO_ROOT / "ci" / "review-threads" / "workflow.yml"
RT_SH = REPO_ROOT / "ci" / "review-threads" / "review-threads.sh"
RT_PY = REPO_ROOT / "ci" / "review-threads" / "review_threads.py"
RT_README = REPO_ROOT / "ci" / "review-threads" / "README.md"


def _on_block(wf: dict) -> dict:
    """PyYAML parses a bare `on:` key as the boolean True; tolerate either spelling."""
    return wf[True] if True in wf else wf["on"]


def _executable_lines(text: str) -> str:
    """Non-comment lines, so an assertion can't be fooled by a pattern quoted in a `#` comment."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# ---------------------------------------------------------------------------
# Triggers: the re-evaluation events that close the stale-green gap (#65).
# ---------------------------------------------------------------------------

def test_workflow_parses():
    yaml = pytest.importorskip("yaml")
    yaml.safe_load(RT_WF.read_text())


def test_keeps_pull_request_lifecycle_trigger():
    """Don't break the original path: the lifecycle gate (open/synchronize/...) stays."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_target" in on, "must keep the tamper-resistant lifecycle trigger"
    types = on["pull_request_target"]["types"]
    for t in ("opened", "synchronize", "reopened"):
        assert t in types, f"pull_request_target must still fire on {t}"


def test_reruns_when_a_review_is_submitted():
    """A submitted review is when review threads get added — the gate must re-count then."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_review" in on, "must re-run on pull_request_review (#65 stale-green)"
    types = on["pull_request_review"]["types"]
    assert "submitted" in types, "a new review is added via a 'submitted' review"
    # edited/dismissed change which threads exist or their state → re-evaluate.
    for t in ("edited", "dismissed"):
        assert t in types, f"pull_request_review must fire on {t}"


def test_reruns_when_a_review_comment_is_created():
    """A created review comment is a freshly-opened thread — the dangerous add-after-green case."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_review_comment" in on, "must re-run on pull_request_review_comment (#65)"
    types = on["pull_request_review_comment"]["types"]
    for t in ("created", "edited", "deleted"):
        assert t in types, f"pull_request_review_comment must fire on {t}"


def test_workflow_dispatch_still_present_with_required_pr_number():
    """Manual re-check stays — and it must demand a pr_number (no PR in a manual payload)."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "workflow_dispatch" in on
    assert on["workflow_dispatch"]["inputs"]["pr_number"]["required"] is True


# ---------------------------------------------------------------------------
# Deliberately EXCLUDED events — each is a false-coverage no-op for this gate.
# Pinned so a future agent doesn't "close the gap" by adding a trigger that never
# updates the required status.
# ---------------------------------------------------------------------------

def test_does_not_trigger_on_issue_comment():
    """issue_comment runs on the DEFAULT-branch SHA, so its check-run never attaches to the PR
    head where branch protection looks — it would re-run but never update the required status.
    Listing it is false coverage; keep it out."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "issue_comment" not in on, (
        "issue_comment re-runs on the default-branch SHA and can't update the PR's required "
        "status — a false-coverage no-op; do not add it"
    )


def test_does_not_trigger_on_pull_request_review_thread():
    """pull_request_review_thread (resolved/unresolved) is a WEBHOOK event, NOT a supported
    GitHub Actions trigger — listing it would silently never fire."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_review_thread" not in on, (
        "pull_request_review_thread is not a valid Actions trigger (webhook-only); it would "
        "never fire — do not add it"
    )


# ---------------------------------------------------------------------------
# Tamper-resistance: run the TRUSTED base copy of the gate script, never the PR's.
# ---------------------------------------------------------------------------

def test_checkout_pins_trusted_base_ref():
    """The review/comment triggers default to the PR MERGE ref (refs/pull/N/merge), which
    contains the PR's edits to review-threads.sh. The checkout must pin `ref` to the trusted
    PR BASE branch so the base copy of the script runs under every trigger."""
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(RT_WF.read_text())
    step = next(
        s for s in wf["jobs"]["review-threads"]["steps"] if str(s.get("uses", "")).startswith(
            "actions/checkout"
        )
    )
    assert step["with"]["ref"] == "${{ github.event.pull_request.base.ref }}", (
        "checkout must pin to the trusted PR base branch (base.ref), not the default merge ref"
    )


def test_checkout_never_uses_pr_head_ref():
    """Hard rule: NEVER checkout the PR head/merge under the privileged trigger — that runs
    PR-controlled code. Asserted against executable (non-comment) lines so the documented
    prohibition in the header doesn't trip the check."""
    code = _executable_lines(RT_WF.read_text())
    for forbidden in ("head.sha", "head.ref", "/merge", "pull_request.merge_commit_sha"):
        assert forbidden not in code, f"checkout must not reference {forbidden} (PR-controlled)"


def test_still_runs_the_vendored_gate_script():
    """The job runs the catalog script (the same one ci/ship reuses), not an inline reimpl."""
    code = _executable_lines(RT_WF.read_text())
    assert "bash ci/review-threads/review-threads.sh" in code


def test_token_stays_read_only():
    """No write scope is needed to COUNT threads; keep the privileged-trigger token minimal."""
    yaml = pytest.importorskip("yaml")
    perms = yaml.safe_load(RT_WF.read_text())["permissions"]
    assert perms.get("contents") == "read"
    assert perms.get("pull-requests") == "read"
    assert "write" not in set(perms.values()), "review-threads needs no write scope"


# ---------------------------------------------------------------------------
# PR-number resolution per event — every trigger that reaches the JOB must resolve a PR.
# ---------------------------------------------------------------------------

def test_pr_number_resolution_covers_every_trigger():
    """Trace the PR_NUMBER expression against each trigger's payload shape:

      • pull_request_target / pull_request_review / pull_request_review_comment
            -> all carry github.event.pull_request.number  (first operand)
      • workflow_dispatch
            -> no pull_request in the payload -> inputs.pr_number  (fallback operand)

    issue_comment (which would need github.event.issue.number) is intentionally NOT a trigger
    here (see test_does_not_trigger_on_issue_comment), so the expression needs no issue branch.
    """
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(RT_WF.read_text())
    step = next(
        s for s in wf["jobs"]["review-threads"]["steps"] if "review-threads.sh" in str(s.get("run", ""))
    )
    expr = step["env"]["PR_NUMBER"]
    assert "github.event.pull_request.number" in expr, "must read the PR number from the event"
    assert "inputs.pr_number" in expr, "manual runs must fall back to the pr_number input"

    # Every trigger that actually reaches the job is covered by one of those two operands.
    on = _on_block(wf)
    pr_object_events = {"pull_request_target", "pull_request_review", "pull_request_review_comment"}
    for ev in pr_object_events:
        assert ev in on, f"{ev} carries github.event.pull_request — must be a trigger to be covered"
    assert "workflow_dispatch" in on, "the inputs.pr_number fallback only applies on a manual run"


def test_gate_script_reads_pr_number_env():
    """The env-resolved PR number must actually feed the script (PR_NUMBER or $1)."""
    text = RT_PY.read_text()
    assert "PR_NUMBER" in text, "script must consume the PR_NUMBER env the workflow sets"


def test_readme_documents_the_excluded_events():
    """Doc/behaviour drift guard: the README must explain why issue_comment / review_thread
    are out, so the exclusion reads as deliberate, not forgotten."""
    text = RT_README.read_text()
    assert "issue_comment" in text
    assert "pull_request_review_thread" in text
    assert "#65" in text


# ---------------------------------------------------------------------------
# Run coalescing: the new triggers fan out (1 review + N comments per submit), so the
# workflow must dedupe per PR.
# ---------------------------------------------------------------------------

def test_concurrency_coalesces_runs_per_pr():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(RT_WF.read_text())
    conc = wf.get("concurrency")
    assert conc, "expected a top-level concurrency block to coalesce the fan-out runs"
    assert "github.event.pull_request.number" in conc["group"], "group must key per PR"
    assert conc["cancel-in-progress"] is True


# ---------------------------------------------------------------------------
# Behavioral: drive the SHIPPED review-threads.sh with a stubbed `gh` so the real control
# flow (PR-number requirement, page summing, fail-closed) and the real jq selection logic
# (isResolved / isOutdated / IGNORE_OUTDATED) are exercised — not just YAML structure.
# ---------------------------------------------------------------------------

def _stub_gh(bin_dir: Path, *, stdout: str = "0", exit_code: int = 0, jq_json: str | None = None) -> None:
    """Write a fake `gh` onto PATH.

    - Default mode: echo `stdout` (a per-page count, possibly multi-line) and exit `exit_code`
      — exercises the script's summing + fail-closed branch without touching the network.
    - jq mode (`jq_json` set): extract the script's OWN `--jq` filter from argv and run the
      canned `jq_json` through the real `jq`, so the shipped selection logic is what runs.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if jq_json is None:
        script = f"#!/usr/bin/env bash\nprintf '%s\\n' {_shq(stdout)}\nexit {exit_code}\n"
    else:
        # Pull the value after `--jq` out of the args, then feed canned JSON to real jq.
        script = (
            "#!/usr/bin/env bash\n"
            "filter=''\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = "--jq" ]; then filter="$2"; shift 2; continue; fi\n'
            "  shift\n"
            "done\n"
            f"printf '%s' {_shq(jq_json)} | jq \"$filter\"\n"
        )
    gh = bin_dir / "gh"
    gh.write_text(script)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _shq(s: str) -> str:
    """Single-quote a string for safe embedding in the generated bash stub."""
    return "'" + s.replace("'", "'\\''") + "'"


def _run_gate(bin_dir: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    # The provider is auto-picked from the ambient CI server: on a Forgejo/Gitea runner
    # GITHUB_SERVER_URL is the Forgejo host, which would route these gh-stub tests to the
    # Forgejo API. Drop the ambient CI context so each test chooses its provider explicitly.
    for var in ("GITHUB_SERVER_URL", "GITHUB_API_URL", "GITHUB_REPOSITORY", "GH_REPO",
                "REVIEW_THREADS_PROVIDER", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(var, None)
    env["PATH"] = f"{bin_dir}{os.pathsep}" + env["PATH"]
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(RT_SH), *args], capture_output=True, text=True, env=env, timeout=30
    )


def _threads_json(*threads: dict) -> str:
    import json

    nodes = ",".join(
        json.dumps({"isResolved": t["resolved"], "isOutdated": t.get("outdated", False)})
        for t in threads
    )
    return (
        '{"data":{"repository":{"pullRequest":{"reviewThreads":{'
        '"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[' + nodes + "]}}}}}"
    )


def test_script_requires_a_pr_number(tmp_path: Path):
    """No PR number (arg or env) -> usage error exit 2, before any gh call."""
    res = _run_gate(tmp_path / "bin")
    assert res.returncode == 2, res.stderr
    assert "Usage" in res.stderr


def test_script_passes_with_zero_unresolved(tmp_path: Path):
    _stub_gh(tmp_path / "bin", stdout="0")
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 0, res.stderr
    assert "PASS" in res.stdout


def test_script_fails_with_unresolved_threads(tmp_path: Path):
    _stub_gh(tmp_path / "bin", stdout="2")
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1
    assert "unresolved review thread" in res.stderr


def test_script_sums_unresolved_across_pages(tmp_path: Path):
    """--paginate yields one count per page; the script must sum them (2 + 1 = 3)."""
    _stub_gh(tmp_path / "bin", stdout="2\n1")
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1
    assert "3 unresolved" in res.stderr


def test_script_fails_closed_on_gh_error(tmp_path: Path):
    """A gh/API error must FAIL (exit 1), never silently pass — an unresolved PR could merge."""
    _stub_gh(tmp_path / "bin", stdout="", exit_code=4)
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1
    assert "failing closed" in res.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_script_jq_counts_outdated_unresolved_by_default(tmp_path: Path):
    """The shipped default jq counts an unresolved thread even when GitHub marked it OUTDATED:
    1 resolved + 1 unresolved(outdated) + 1 unresolved -> 2 unresolved -> FAIL."""
    json_payload = _threads_json(
        {"resolved": True},
        {"resolved": False, "outdated": True},
        {"resolved": False, "outdated": False},
    )
    _stub_gh(tmp_path / "bin", jq_json=json_payload)
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "2 unresolved" in res.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_script_jq_ignore_outdated_skips_outdated(tmp_path: Path):
    """IGNORE_OUTDATED=1 drops the outdated-but-unresolved thread -> only 1 counts -> FAIL,
    and the same payload PASSES when the only unresolved thread is outdated."""
    payload = _threads_json(
        {"resolved": False, "outdated": True},
        {"resolved": False, "outdated": False},
    )
    _stub_gh(tmp_path / "bin", jq_json=payload)
    res = _run_gate(tmp_path / "bin", "123", env_extra={"IGNORE_OUTDATED": "1"})
    assert res.returncode == 1
    assert "1 unresolved" in res.stderr

    only_outdated = _threads_json({"resolved": False, "outdated": True})
    _stub_gh(tmp_path / "bin", jq_json=only_outdated)
    res2 = _run_gate(tmp_path / "bin", "123", env_extra={"IGNORE_OUTDATED": "1"})
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "PASS" in res2.stdout


# ---------------------------------------------------------------------------
# Forgejo provider: on a Forgejo/Gitea Actions runner there is no gh and no GraphQL, so the
# script reads the Forgejo REST API with stdlib python3 and applies the SAME conversation rule
# as ci/ship's Forgejo provider (keyed by path + line across reviews and commits, as Forgejo's UI; a
# conversation is resolved only when its FIRST comment has `resolver`). Driven against a local
# fake API so the real script control flow and HTTP paths run.
# ---------------------------------------------------------------------------


class _FakeForgejo:
    """A tiny threaded Forgejo API serving /repos/o/r/pulls/7/reviews[/<id>/comments]."""

    def __init__(self, reviews: list[dict], *, status: int = 0, page_cap: int = 50,
                 total_header: bool = True, token: str | None = None, redirect: bool = False):
        import http.server
        import json
        import threading
        from urllib.parse import parse_qs, urlparse

        self.requests: list[str] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):  # keep pytest output clean
                pass

            def _send(self, code: int, body: object = None, headers: dict | None = None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def _rejects(self) -> bool:
                """Send the configured failure response (redirect / status / bad token); True if sent."""
                if redirect:
                    self._send(302, headers={"Location": "http://example.invalid/"})
                elif status:
                    self._send(status, {"message": "boom"})
                elif token is not None and self.headers.get("Authorization") != f"token {token}":
                    self._send(401, {"message": "unauthorized"})
                else:
                    return False
                return True

            def _reviews_page(self, query: str):
                q = parse_qs(query)
                page = int(q.get("page", ["1"])[0])
                limit = min(int(q.get("limit", ["50"])[0]), page_cap)
                chunk = reviews[(page - 1) * limit: page * limit]
                headers = {"X-Total-Count": str(len(reviews))} if total_header else {}
                return self._send(200, [{"id": r["id"], "state": "COMMENT"} for r in chunk], headers)

            def do_GET(self):  # noqa: N802 - http.server API
                fake.requests.append(self.path)
                url = urlparse(self.path)
                if self._rejects():
                    return None
                prefix = "/api/v1/repos/o/r/pulls/7/reviews"
                if url.path == prefix:
                    return self._reviews_page(url.query)
                if url.path.startswith(prefix + "/") and url.path.endswith("/comments"):
                    rid = int(url.path.split("/")[-2])
                    review = next(r for r in reviews if r["id"] == rid)
                    return self._send(200, [dict(c, pull_request_review_id=rid) for c in review["comments"]])
                return self._send(404, {"message": "not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def forgejo():
    servers: list[_FakeForgejo] = []

    def start(reviews: list[dict], **kw) -> _FakeForgejo:
        srv = _FakeForgejo(reviews, **kw)
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.close()


def _forgejo_env(srv: _FakeForgejo, **extra: str) -> dict[str, str]:
    env = {
        "GITHUB_SERVER_URL": srv.url,
        "GITHUB_API_URL": f"{srv.url}/api/v1",
        "GITHUB_REPOSITORY": "o/r",
        "GH_TOKEN": "job-token",
    }
    env.update(extra)
    return env


def _c(cid: int, *, path: str = "a.py", pos: int = 3, commit: str = "c1", resolved: bool = False) -> dict:
    return {"id": cid, "path": path, "position": pos, "original_position": 0,
            "commit_id": commit, "original_commit_id": commit,
            "resolver": {"login": "rev"} if resolved else None}


needs_python3 = pytest.mark.skipif(shutil.which("python3") is None, reason="python3 not on PATH")


@needs_python3
def test_forgejo_is_auto_selected_off_github_and_needs_no_gh(tmp_path: Path, forgejo):
    """A Forgejo job (GITHUB_SERVER_URL not github.com) must not need the gh CLI at all."""
    srv = forgejo([])
    res = _run_gate(tmp_path / "empty-bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "PASS" in res.stdout
    assert "gh CLI not found" not in res.stderr
    assert any(p.startswith("/api/v1/repos/o/r/pulls/7/reviews") for p in srv.requests)


@needs_python3
def test_forgejo_counts_unresolved_conversations(tmp_path: Path, forgejo):
    srv = forgejo([
        {"id": 1, "comments": [_c(10, resolved=True), _c(11, path="b.py")]},
        {"id": 2, "comments": [_c(20, path="c.py", pos=9)]},
    ])
    res = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "2 unresolved" in res.stderr


@needs_python3
def test_forgejo_conversation_is_resolved_through_its_first_comment(tmp_path: Path, forgejo):
    """A reply after the resolving first comment does not re-open the conversation."""
    srv = forgejo([{"id": 1, "comments": [_c(10, resolved=True), _c(11)]}])
    res = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 0, res.stdout + res.stderr


@needs_python3
def test_forgejo_groups_a_conversation_by_path_and_line_like_its_ui(tmp_path: Path, forgejo):
    """Forgejo shows every comment on a path + line as ONE conversation, whatever review or
    commit it came with, and "Resolve conversation" marks only its first comment. A reply posted
    inside a new review, or a later comment on the same line, joins that conversation — keying
    by review/commit left such groups unresolvable forever (two reviews on one line: rig-cli#482)."""
    srv = forgejo([
        {"id": 1, "comments": [_c(10, commit="old", resolved=True)]},
        {"id": 2, "comments": [_c(20, commit="new")]},  # a reply in another review, after a push
    ])
    res = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 0, res.stdout + res.stderr

    srv = forgejo([
        {"id": 1, "comments": [_c(10)]},
        {"id": 2, "comments": [_c(20)]},  # the rig-cli#482 shape: one line, two reviews
    ])
    res = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "1 unresolved" in res.stderr


@needs_python3
def test_forgejo_lineless_comment_is_its_own_conversation(tmp_path: Path, forgejo):
    srv = forgejo([{"id": 1, "comments": [_c(10, pos=0, resolved=True), _c(11, pos=0)]}])
    res = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "1 unresolved" in res.stderr


@needs_python3
def test_forgejo_reads_every_page_even_below_the_requested_limit(tmp_path: Path, forgejo):
    """A server whose page cap is below the requested limit (and sends no X-Total-Count) must
    not truncate the read: the unresolved thread sits on the LAST page."""
    reviews = [{"id": i, "comments": []} for i in range(1, 7)]
    reviews[-1]["comments"] = [_c(60)]
    srv = forgejo(reviews, page_cap=5, total_header=False)
    res = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "1 unresolved" in res.stderr


@needs_python3
def test_forgejo_sends_the_job_token(tmp_path: Path, forgejo):
    srv = forgejo([], token="job-token")
    ok = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv, GH_TOKEN="wrong"))
    assert bad.returncode == 1
    assert "failing closed" in bad.stderr


@needs_python3
@pytest.mark.parametrize("kw", [{"status": 500}, {"redirect": True}])
def test_forgejo_api_error_or_redirect_fails_closed(tmp_path: Path, forgejo, kw):
    srv = forgejo([], **kw)
    res = _run_gate(tmp_path / "bin", "7", env_extra=_forgejo_env(srv))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "failing closed" in res.stderr


def test_github_server_keeps_the_gh_path(tmp_path: Path):
    """On github.com (or with no CI context at all) the GraphQL/gh path is unchanged."""
    _stub_gh(tmp_path / "bin", stdout="0")
    res = _run_gate(tmp_path / "bin", "7", env_extra={"GITHUB_SERVER_URL": "https://github.com"})
    assert res.returncode == 0, res.stderr
    assert "PASS" in res.stdout


def test_provider_override_forces_github(tmp_path: Path):
    """GitHub Enterprise Server: its GITHUB_SERVER_URL is not github.com, so the override pins gh."""
    _stub_gh(tmp_path / "bin", stdout="1")
    res = _run_gate(tmp_path / "bin", "7", env_extra={
        "GITHUB_SERVER_URL": "https://ghe.example.com", "REVIEW_THREADS_PROVIDER": "github"})
    assert res.returncode == 1
    assert "1 unresolved" in res.stderr

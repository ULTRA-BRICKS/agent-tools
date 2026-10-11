"""Tests for ship.sh's external-review EVIDENCE policy (agent-tools#879).

An owner listed in the trusted machine config's `ci.items.ship.external_review_evidence_owners`
gets a PR with ZERO GitHub-side reviews through the external-review gate without the Telegram
hatch when — and only when — CI is green (or the hatch-gated --skip-ci path), the review quorum
was met, the acceptance gate fully accepted the ticket, and there are no unresolved review
threads or unaddressed PR comments. Any miss is named and falls back to the unchanged
hatch-or-refuse path.

Hermetic, same shape as tests/test_ship_ticket_gates.py (whose fake `task` and acceptance
payloads this reuses): a real temp git repo, a fake `gh` (which evaluates ship's own `-q`
comment filter with the real jq against a fixture), a fake `review`, a fake `task`, and a
machine policy file under a temp XDG_CONFIG_HOME.

    uv run --with pytest python -m pytest tests/test_ship_external_review_evidence.py -q
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from test_ship_ticket_gates import _ACCEPTED, _FAKE_TASK, _OPTED_OUT, _SHIP, _audit, _merged, _sh, repo  # noqa: F401 - `repo` is a fixture

_FAKE_GH = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  repo) echo "${SHIP_TEST_REPO_NWO:-acme/widgets}" ;;
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        args="$*"
        if printf '%s' "$args" | grep -q -- '--json reviews'; then
          echo "${SHIP_TEST_REVIEW_COUNT:-0}"
        elif printf '%s' "$args" | grep -q -- '--json author,comments'; then
          q=""
          while [ "$#" -gt 0 ]; do [ "$1" = "-q" ] && { q="$2"; break; }; shift; done
          printf '%s' "${SHIP_TEST_PR_COMMENTS_JSON}" | jq -r "$q"
        elif printf '%s' "$args" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s' "$args" | grep -q statusCheckRollup; then
          printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
        elif printf '%s' "$args" | grep -q -- '--json title,body'; then
          printf '%s\\t%s\\n' "feat: the thing" "Refs HYP-931"
        elif printf '%s' "$args" | grep -q -- '--json title'; then
          echo "feat: the thing"
        elif printf '%s' "$args" | grep -q -- '--json body'; then
          echo "Refs HYP-931"
        elif printf '%s' "$args" | grep -q -- '--json baseRefName'; then
          echo main
        else
          echo '[]'
        fi ;;
      diff) echo "src/a.py" ;;
      merge) printf 'merge %s\\n' "$*" >> "${GH_LOG}"; echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api) echo "${SHIP_TEST_UNRESOLVED:-0}" ;;
  *) : ;;
esac
"""

# review-cli's real `--check --json` keys; capacity-check answers nothing (not all exhausted).
_FAKE_REVIEW = """\
#!/usr/bin/env bash
[ "$1" = "task" ] || exit 0
printf '{"task_code":"%s","passed_iterations":3,"distinct_models_passed":3,"models":["a","b","c"],"distinct_roles_passed":3,"roles":["backend","security","qa"],"min_iter":3,"min_roles":3,"passed":true,"identity_verification":"repo_only"}\\n' "$2"
"""

# The PR author is `me`. Addressed: a comment the author answered later, and a hidden one.
_ADDRESSED_COMMENTS = {
    "author": {"login": "me"},
    "comments": [
        {"author": {"login": "bot"}, "createdAt": "2026-10-01T00:00:00Z", "isMinimized": False},
        {"author": {"login": "me"}, "createdAt": "2026-10-01T01:00:00Z", "isMinimized": False},
        {"author": {"login": "someone"}, "createdAt": "2026-10-02T00:00:00Z", "isMinimized": True},
    ],
}
_ONE_UNADDRESSED = {
    "author": {"login": "me"},
    "comments": _ADDRESSED_COMMENTS["comments"]
    + [{"author": {"login": "someone"}, "createdAt": "2026-10-03T00:00:00Z", "isMinimized": False}],
}


def _policy(tmp_path: Path, evidence_owners: list[str]) -> Path:
    xdg = tmp_path / "xdg"
    rig = xdg / "rig"
    rig.mkdir(parents=True)
    for d in (xdg, rig):
        d.chmod(0o700)
    cfg = rig / "config.yaml"
    cfg.write_text(json.dumps({"ci": {"items": {"ship": {"external_review_evidence_owners": evidence_owners}}}}))
    cfg.chmod(0o600)
    return xdg


def _run(main: Path, tmp_path: Path, *, owners=("acme",), env=None, args=()):
    if not shutil.which("jq"):
        pytest.skip("jq required")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("gh", _FAKE_GH), ("review", _FAKE_REVIEW), ("task", _FAKE_TASK)):
        (bindir / name).write_text(body, encoding="utf-8")
        (bindir / name).chmod(0o755)
    e = dict(os.environ)
    e.pop("RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW", None)
    e.pop("RIG_HATCH_REQUEST_SHIP_SKIP_CI", None)
    e.pop("REVIEW_TASK_CODE", None)  # an ambient review session's code would replace HYP-931
    e.update({
        "PATH": f"{bindir}{os.pathsep}{e['PATH']}",
        "XDG_CONFIG_HOME": str(_policy(tmp_path, list(owners))),
        "SHIP_TEST_BRANCH": "hyp-931-fix-thing",
        "SHIP_DEFAULT_BRANCH": "main",
        "SHIP_MAIN_CHECKOUT": str(main),
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DISABLE_REVIEW_QUORUM": "0",
        "SHIP_ACCEPTANCE_GATE": "1",
        "SHIP_MAGIC_CLOSE_GATE": "1",
        "SHIP_TASK_NOTIFY_ENABLED": "0",
        "TASK_LOG": str(tmp_path / "task.log"),
        "GH_LOG": str(tmp_path / "gh.log"),
        "SHIP_AUDIT_FILE": str(tmp_path / "audit.jsonl"),
        "SHIP_TEST_TASK_GATE_JSON": _ACCEPTED,
        "SHIP_TEST_TASK_GATE_EXIT": "0",
        "SHIP_TEST_PR_COMMENTS_JSON": json.dumps(_ADDRESSED_COMMENTS),
    })
    if env:
        e.update(env)
    return _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *args, cwd=main, env=e)


def test_zero_review_pr_with_all_evidence_ships_without_the_hatch(repo, tmp_path):
    r = _run(repo, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "external-review gate PASSED on evidence (Rig policy for acme/widgets)" in r.stdout
    assert "RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW" not in r.stderr
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "external-review")
    assert line["decision"] == "external-review:authorized:evidence"
    assert line["override_reason"] == "ci=green quorum=HYP-931 acceptance=HYP-931"


def test_one_unaddressed_comment_falls_back_to_the_hatch(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_COMMENTS_JSON": json.dumps(_ONE_UNADDRESSED)})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Rig evidence policy for acme/widgets NOT satisfied" in r.stderr
    assert "1 unaddressed PR comment(s)" in r.stderr
    assert "Refusing: PR #1 has ZERO GitHub-side reviews" in r.stderr
    assert "RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW" in r.stderr
    assert not _merged(tmp_path)
    (line,) = _audit(tmp_path, "external-review")
    assert line["decision"] == "external-review:refused"


def test_unreadable_comments_fail_closed(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_COMMENTS_JSON": "not json"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not read the PR comments" in r.stderr
    assert not _merged(tmp_path)


def test_post_merge_acceptance_opt_out_is_not_proven_acceptance(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": _OPTED_OUT})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "acceptance is not fully proven" in r.stderr
    assert "Refusing: PR #1 has ZERO GitHub-side reviews" in r.stderr
    assert not _merged(tmp_path)


def test_quorum_not_met_by_review_cli_is_not_evidence(repo, tmp_path):
    # The test-only quorum off-switch stands in for every non-authorizing quorum outcome.
    r = _run(repo, tmp_path, env={"SHIP_TEST_DISABLE_REVIEW_QUORUM": "1"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "the review quorum was not met on review-cli's own counts" in r.stderr
    assert not _merged(tmp_path)


def test_unresolved_thread_still_refuses(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_UNRESOLVED": "1"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "1 unresolved review thread(s)" in r.stderr
    assert not _merged(tmp_path)


def test_skip_ci_counts_for_ci_but_its_own_hatch_still_gates_the_merge(repo, tmp_path):
    r = _run(repo, tmp_path, args=("--skip-ci",))
    assert "external-review gate PASSED on evidence" in r.stdout, r.stdout + r.stderr
    assert r.returncode == 1
    assert "Refusing: --skip-ci is a BLIND admin-merge" in r.stderr
    assert not _merged(tmp_path)


def test_owner_not_listed_keeps_the_immediate_refusal(repo, tmp_path):
    r = _run(repo, tmp_path, owners=("someone-else",))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "defers the decision" not in r.stdout
    assert "Refusing: PR #1 has ZERO GitHub-side reviews" in r.stderr
    assert "acceptance gate" not in r.stdout  # refused at the gate itself, before the ticket gates
    assert not _merged(tmp_path)


def test_a_real_review_needs_no_evidence(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_REVIEW_COUNT": "1", "SHIP_TEST_PR_COMMENTS_JSON": json.dumps(_ONE_UNADDRESSED)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "external-review gate OK — PR #1 has 1 GitHub review(s)." in r.stdout
    assert _merged(tmp_path)


def test_missing_pr_author_is_a_failed_read_not_zero(repo, tmp_path):
    payload = {"author": None, "comments": [{"author": None, "createdAt": "2026-10-01T00:00:00Z"}]}
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_COMMENTS_JSON": json.dumps(payload)})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not read the PR comments" in r.stderr
    assert not _merged(tmp_path)


def test_comment_without_timestamp_counts_as_unaddressed(repo, tmp_path):
    payload = {"author": {"login": "me"}, "comments": [
        {"author": {"login": "someone"}, "createdAt": None},
        {"author": {"login": "me"}, "createdAt": "2026-10-01T00:00:00Z"},
    ]}
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_COMMENTS_JSON": json.dumps(payload)})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "1 unaddressed PR comment(s)" in r.stderr
    assert not _merged(tmp_path)

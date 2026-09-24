"""Secret-safe display for the shared hatch helper (UB-25 coordinator follow-up #2).

A raw API command sent to the hatch can carry a credential — an Authorization header, `curl -u`,
a `?token=` query value, an inline `GITEA_TOKEN=... cmd`. The Telegram preview and the durable
`overrides.log` line must show the command with those VALUES redacted, while approval identity
(the operation fingerprint, the inline-hatch parser) stays bound to the ORIGINAL full command.

Every secret below is a synthetic marker (`SYNTHETIC_*`), never a real-looking credential.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import agenttools_hatch_escalation as hatch

_TOKEN = "SYNTHETIC_TOKEN_123"
_PASS = "SYNTHETIC_PASS_456"
_ISSUES = "https://forge.example/api/v1/repos/org/repo/issues"
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    clean_home = tmp_path / "_clean_home"
    clean_home.mkdir()
    monkeypatch.setattr(hatch, "resolve_home", lambda: str(clean_home))


def _capturing_tg_ctl(tmp_path: Path, capture: Path) -> Path:
    tg_ctl = tmp_path / "trusted" / "tg-ctl"
    tg_ctl.parent.mkdir(parents=True, exist_ok=True)
    tg_ctl.write_text(f"#!/bin/sh\ncat > {capture}\n" + _ALLOW_REPLY_SH)
    tg_ctl.chmod(0o755)
    return tg_ctl


# Every credential below is a SYNTHETIC_* marker interpolated into a credential-SHAPED command;
# the per-line `gitleaks:allow` marks each as a known-synthetic fixture for the secret scanner.
_SECRET_COMMANDS = [
    f"curl -XPOST -H 'Authorization: token {_TOKEN}' {_ISSUES} -d title=x",  # gitleaks:allow
    f'curl -X POST -H "Authorization: Bearer {_TOKEN}" {_ISSUES}',  # gitleaks:allow
    f"curl -X POST -H 'authorization:bearer {_TOKEN}' {_ISSUES}",  # gitleaks:allow
    f"curl -X POST -H 'X-Gitea-Token: {_TOKEN}' {_ISSUES}",  # gitleaks:allow
    f"curl -X POST -H 'PRIVATE-TOKEN: {_TOKEN}' {_ISSUES}",  # gitleaks:allow
    f"http POST {_ISSUES} Authorization:'token {_TOKEN}' title=x",  # gitleaks:allow
    f"curl -u alice:{_PASS} -X POST {_ISSUES}",  # gitleaks:allow
    f"curl -ualice:{_PASS} -X POST {_ISSUES}",  # gitleaks:allow
    f"curl --user alice:{_PASS} -X POST {_ISSUES}",  # gitleaks:allow
    f"curl --user=alice:{_PASS} -X POST {_ISSUES}",  # gitleaks:allow
    f"http -a alice:{_PASS} POST {_ISSUES} title=x",  # gitleaks:allow
    f"curl -X POST '{_ISSUES}?token={_TOKEN}&state=open'",  # gitleaks:allow
    f"curl -X POST '{_ISSUES}?access_token={_TOKEN}'",  # gitleaks:allow
    f"curl -X POST https://alice:{_PASS}@forge.example/api/v1/repos/org/repo/issues",  # gitleaks:allow
    f"GITEA_TOKEN={_TOKEN} fj issue create t --body x",  # gitleaks:allow
    f"env GH_TOKEN='{_TOKEN}' gh api repos/org/repo/issues -f title=x",  # gitleaks:allow
    f"fj auth add-token --token {_TOKEN} forge.example",  # gitleaks:allow
    f"tea login add --token={_TOKEN}",  # gitleaks:allow
    f"curl -X POST {_ISSUES} -d '{{\"title\":\"x\",\"token\":\"{_TOKEN}\"}}'",  # gitleaks:allow
    f"curl -X POST {_ISSUES} -d '{{\"password\": \"{_PASS}\"}}'",  # gitleaks:allow
    f"curl --oauth2-bearer {_TOKEN} -X POST {_ISSUES} -d title=x",  # gitleaks:allow
    f"wget --http-password {_PASS} --post-data=title=x {_ISSUES}",  # gitleaks:allow
    f"wget --proxy-password={_PASS} {_ISSUES}",  # gitleaks:allow
    f"curl -H 'Cookie: sid={_TOKEN}; csrf={_PASS}' -X POST {_ISSUES}",  # gitleaks:allow
    f"curl --cookie 'sid={_TOKEN}; csrf={_PASS}' -X POST {_ISSUES}",  # gitleaks:allow
]


@pytest.mark.parametrize("command", _SECRET_COMMANDS)
def test_redact_for_display_removes_secret_values(command):
    shown = hatch.redact_for_display(command)
    assert _TOKEN not in shown and _PASS not in shown, shown
    assert "[REDACTED]" in shown


def test_redaction_keeps_the_command_intelligible():
    shown = hatch.redact_for_display(
        f"curl -XPOST -H 'Authorization: token {_TOKEN}' '{_ISSUES}?token={_TOKEN}&state=open' -d title=x"  # gitleaks:allow
    )
    assert shown.startswith("curl -XPOST -H 'Authorization: token [REDACTED]'")  # gitleaks:allow
    assert f"{_ISSUES}?token=[REDACTED]&state=open" in shown
    assert shown.endswith("-d title=x")
    assert hatch.redact_for_display(f"curl -u alice:{_PASS} x").startswith("curl -u alice:[REDACTED]")  # gitleaks:allow


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard HEAD~3",
        'RIG_HATCH_REQUEST_BLOCK_RAW_TICKET="Tracker migration backfill." fj issue create t',
        f"curl {_ISSUES}?state=open&page=2",
        "gh api repos/org/repo/issues -f title='Token handling is broken'",
        "fj issue comment 5 'rotate the password policy doc'",
        "",
    ],
)
def test_redaction_leaves_non_secret_text_untouched(command):
    assert hatch.redact_for_display(command) == command


def test_question_preview_is_redacted():
    question = hatch._question(
        "block-raw-ticket",
        "Tracker migration backfill approved by the CTO.",
        {"command": f"curl -XPOST -H 'Authorization: token {_TOKEN}' {_ISSUES}", "hook": "x",  # gitleaks:allow
         "target": f"{_ISSUES}?token={_TOKEN}"},
        "/tmp",
        branch="main",
        session_id="pid:1",
    )
    assert _TOKEN not in question
    assert "Authorization: token [REDACTED]" in question


@pytest.mark.parametrize("offset", [-12, -4, 0, 4])
def test_question_redacts_before_truncating_a_long_command(offset):
    """A secret straddling the preview's 1000-char cap must not leave its prefix behind."""
    head = f"curl -X POST {_ISSUES} -d '{{\"title\":\""
    pad = "x" * (1000 - len(head) - len('","token":"') + offset)
    command = f"{head}{pad}\",\"token\":\"{_TOKEN}\"}}'"
    question = hatch._question(
        "block-raw-ticket", "Tracker migration backfill.", {"command": command}, "/tmp",
        branch="main", session_id="pid:1",
    )
    assert "SYNTHETIC_TOK" not in question
    assert "SYNTHETIC" not in question


def test_end_to_end_preview_and_audit_log_are_redacted_but_approval_is_bound_to_original(tmp_path):
    capture = tmp_path / "button_request.json"
    tg_ctl = _capturing_tg_ctl(tmp_path, capture)
    log_path = tmp_path / "overrides.log"
    env_var = hatch.hatch_env_var("block-raw-ticket")
    command = f"GITEA_TOKEN={_TOKEN} curl -XPOST -H 'Authorization: token {_TOKEN}' {_ISSUES} -d title=x"  # gitleaks:allow

    result = hatch.request_hatch_approval(
        "block-raw-ticket",
        {"hook": "block-raw-ticket", "command": command},
        cwd=str(tmp_path),
        env={env_var: "Tracker migration backfill approved by the CTO."},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
        overrides_log_path=log_path,
    )

    assert result.approved is True
    sent = capture.read_text()
    assert _TOKEN not in sent
    request = json.loads(sent)
    assert "[REDACTED]" in request["question"]
    assert request["operation"]["fingerprint"] == hatch._operation_fingerprint(
        "block-raw-ticket", {"hook": "block-raw-ticket", "command": command}
    )
    logged = log_path.read_text()
    assert _TOKEN not in logged
    assert "[REDACTED]" in json.loads(logged.splitlines()[0])["command"]


def test_commands_with_identical_redacted_preview_keep_distinct_approval_identity():
    """Redaction is display-only: two commands that differ only in the secret render the same
    preview but must never share an approval (fingerprint) identity."""
    first = f"curl -XPOST -H 'Authorization: token {_TOKEN}' {_ISSUES}"  # gitleaks:allow
    second = f"curl -XPOST -H 'Authorization: token {_TOKEN}_OTHER' {_ISSUES}"  # gitleaks:allow
    assert hatch.redact_for_display(first) == hatch.redact_for_display(second)
    assert hatch._operation_fingerprint("block-raw-ticket", {"command": first}) != (
        hatch._operation_fingerprint("block-raw-ticket", {"command": second})
    )


def test_inline_hatch_value_is_still_parsed_from_the_original_command(tmp_path):
    capture = tmp_path / "button_request.json"
    tg_ctl = _capturing_tg_ctl(tmp_path, capture)
    command = (
        f'RIG_HATCH_REQUEST_BLOCK_RAW_TICKET="Tracker migration backfill." '
        f"GITEA_TOKEN={_TOKEN} fj issue create t --body x"
    )
    result = hatch.request_hatch_approval(
        "block-raw-ticket",
        {"command": command},
        cwd=str(tmp_path),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
        overrides_log_path=tmp_path / "overrides.log",
    )
    assert result.approved is True
    assert _TOKEN not in capture.read_text()
    assert "Tracker migration backfill." in json.loads(capture.read_text())["question"]

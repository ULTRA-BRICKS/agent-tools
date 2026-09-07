"""block-secrets-write — the pre-write secret scan.

Loads the hook by path (each agent-hook is a standalone script, not a package) and drives
`main()` with a `pre-write` event, the way the bridges do. The first regression here is the
codex-session refusal of an English audit report (an `apply_patch` adding a Markdown file whose
prose quoted a request URL `…/redeem?token=ultra-invalid-proof`): a URL query parameter is not
a secret assignment. The generic-assignment pattern must still catch real assignments.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from codex_private_refusals import (
    BLOCK_SECRETS_WRITE_AUDIT_MD,
    BLOCK_SECRETS_WRITE_AUDIT_MD_PATH,
)

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks" / "block-secrets-write" / "block_secrets_write.py"
)
_spec = importlib.util.spec_from_file_location("block_secrets_write", _HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _run(content: str, path: str, monkeypatch) -> tuple[str, int]:
    event = {"args": {"path": path, "content": content}, "cwd": "/tmp"}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def test_audit_report_quoting_a_url_token_parameter_is_allowed(monkeypatch):
    """The exact refused apply_patch content (codex, 2026-09-05T17:52Z)."""
    assert "?token=" in BLOCK_SECRETS_WRITE_AUDIT_MD  # the premise: the match was a URL param
    out, code = _run(BLOCK_SECRETS_WRITE_AUDIT_MD, BLOCK_SECRETS_WRITE_AUDIT_MD_PATH, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("content", [
    "see https://example.test/redeem?token=ultra-invalid-proof-value for the flow",
    "GET /api?access_key=not-a-real-key-marker&x=1",
    "curl 'https://h/x?a=1&client_secret=redeem-flow-proof-marker'",
])
def test_url_query_slug_parameter_is_not_an_assignment(content, monkeypatch):
    out, code = _run(content, "notes/audit.md", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("content", [
    'const endpoint = "https://api.example.test/redeem?token=AbCdEfGhIjKlMnOpQrStUvWx"',  # gitleaks:allow
    "GET /api?access_key=a1b2c3d4e5f6g7h8i9j0&x=1",  # gitleaks:allow
    "https://h/x?a=1&client_secret=abcdefghijklmnopqrstuvwxyz",  # a bare word, not a slug
    "https://h/x?token=Ultra-Invalid-Proof",                     # mixed case is not a slug
])
def test_credential_shaped_query_value_is_still_blocked(content, monkeypatch):
    """The carve-out is the SLUG shape only (review on the bundle): a real-looking value in a
    URL written into source is still a generic assignment."""
    out, code = _run(content, "src/client.ts", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("content", [
    'API_KEY = "sk_live_9f8e7d6c5b4a3f2e1d0c"',  # gitleaks:allow
    "token: AbCdEfGhIjKlMnOpQrStUvWx",  # gitleaks:allow
    "export CLIENT_SECRET='zX9yW8vU7tS6rQ5pO4nM3lK2'",  # gitleaks:allow
    "password = q1w2e3r4t5y6u7i8o9p0",  # gitleaks:allow
    # the same key=value shape OUTSIDE a URL query (no `?`/`&` before it) is still an assignment
    "token=ultra-invalid-proof-that-is-long-enough",  # gitleaks:allow
])
def test_real_assignment_is_still_blocked(content, monkeypatch):
    out, code = _run(content, "src/config.ts", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "generic api/secret token assignment" in json.loads(out)["message"]


@pytest.mark.parametrize("content", [
    "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n",  # gitleaks:allow
    "aws: AKIAABCDEFGHIJKLMNOP",  # gitleaks:allow
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",  # gitleaks:allow
    "https://hooks/?t=xoxb-1234567890-abcdefghij",  # gitleaks:allow
])
def test_specific_secret_shapes_are_unaffected(content, monkeypatch):
    """Narrowing the generic pattern must not touch the shape-specific ones (a slack token in a
    URL is still a slack token)."""
    out, code = _run(content, "src/x.ts", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_placeholder_and_example_files_still_pass(monkeypatch):
    out, code = _run('API_KEY = "YOUR_KEY_HERE_xxxxxxxxxx"', "src/x.ts", monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    out, code = _run('token = "abcdefghijklmnopqrstuvwxyz"', ".env.example", monkeypatch)
    assert code == 0 and _decision(out) == "allow"

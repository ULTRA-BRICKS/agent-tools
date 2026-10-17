"""Tests for the model-freshness checker (lib/checker/model_freshness.py).

Hermetic: provider endpoints are mocked via the injected `pollers` hook, `gh` via the
injected `gh_available` hook — no network, no real `gh`. The real manifest
(lib/contracts/models.yaml) is loaded for the happy-path/validation tests; synthetic
manifests (tmp_path) cover the malformed/invalid cases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The checker lives at lib/checker/; tests run from the repo root. Add lib/ to the path so
# `from checker.model_freshness import ...` resolves the package the cron also runs.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "lib"))

from checker import model_freshness as mf  # noqa: E402

yaml = pytest.importorskip("yaml")

MANIFEST = _REPO_ROOT / "lib" / "contracts" / "models.yaml"
SCHEMA = _REPO_ROOT / "lib" / "contracts" / "models.schema.json"


@pytest.fixture(autouse=True)
def _no_real_harness_catalogs(monkeypatch):
    """run() reads the machine's harness catalogs (omp, ~/.codex, ~/.claude, models.dev) by
    default; keep every test hermetic unless it injects its own sources."""
    monkeypatch.setattr(mf, "HARNESS_SOURCES", {})
    monkeypatch.setattr(mf, "FALLBACK_SOURCES", {})


# ── manifest load + validate (against the REAL shipped manifest) ────────────────────────
def test_real_manifest_loads_and_validates():
    m = mf.load_manifest(MANIFEST)
    assert m.version == 1
    assert m.models, "manifest must ship at least one model"
    assert mf.validate_manifest(m) == [], "shipped manifest must be valid"


def test_vision_role_resolves_to_a_vision_capable_model():
    """#3681: the `vision` role MUST point at a vision-capable entry."""
    m = mf.load_manifest(MANIFEST)
    target = m.roles.get("vision")
    assert target, "manifest must define a `vision` role"
    entry = m.entry(target)
    assert entry is not None
    assert "vision" in entry.capabilities


def test_vision_tags_on_real_manifest():
    """#3681: vision only on models with real image input (models.dev modalities)."""
    m = mf.load_manifest(MANIFEST)
    deepseek = m.entry("deepseek/deepseek-v4-pro")
    kimi = m.entry("moonshotai/Kimi-K3")
    turbo = m.entry("kimi-k2p6-turbo")
    assert deepseek is not None and kimi is not None and turbo is not None
    assert "vision" not in deepseek.capabilities, "deepseek-v4-pro is text-only, no vision"
    assert "vision" in kimi.capabilities, "Kimi-K3 takes images"
    assert "vision" in turbo.capabilities, "kimi-k2p6-turbo has vision"


def test_kimi_code_k3_pin_on_real_manifest():
    """The k3 pin: kimi-code:latest resolves to it, and it really is vision-capable."""
    m = mf.load_manifest(MANIFEST)
    assert m.aliases["kimi-code:latest"] == "k3"
    entry = m.entry("k3")
    assert entry is not None and entry.provider == "kimi-code"
    assert "vision" in entry.capabilities  # omp catalog: images yes
    # Kimi docs: set the context-window field to 1048576 for k3's full up-to-1M context.
    assert entry.context == 1048576


def test_fallback_chain_ends_with_omp_k3():
    """The resilience order's LAST step is the omp+k3 daily-driver harness."""
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    chain = data["fallback_chain"]
    assert chain[-1] == {"harness": "omp", "model": "k3", "notation": "omp:k3"}


def _write_manifest(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_validate_flags_vision_role_to_non_vision_model(tmp_path: Path):
    data = {
        "version": 1,
        "models": [
            {"id": "code-only", "provider": "commandcode", "capabilities": ["code"]},
        ],
        "roles": {"vision": "code-only"},
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("violates #3681" in p for p in problems)


def test_validate_flags_role_pointing_at_unknown_id(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "real", "provider": "openai", "capabilities": ["code"]}],
        "roles": {"architect": "does-not-exist"},
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("not a concrete model id" in p for p in problems)


def test_validate_flags_provider_latest_mismatch(tmp_path: Path):
    data = {
        "version": 1,
        "models": [
            {"id": "a", "provider": "openai", "capabilities": ["code"]},
            {"id": "b", "provider": "zai", "capabilities": ["code"]},
        ],
        "aliases": {"openai:latest": "b"},  # b is a zai model — mismatch
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("openai:latest" in p and "zai" in p for p in problems)


def test_validate_flags_unknown_capability(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "x", "provider": "openai", "capabilities": ["telepathy"]}],
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("unknown capabilities" in p for p in problems)


def test_load_manifest_rejects_empty_models(tmp_path: Path):
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "models": []}), encoding="utf-8")
    with pytest.raises(mf.ManifestError):
        mf.load_manifest(p)


def test_load_manifest_context_bool_not_coerced_to_int(tmp_path: Path):
    """`context: true` must NOT silently become 1 (isinstance(True, int) is True)."""
    data = {"version": 1, "models": [
        {"id": "x", "provider": "openai", "capabilities": ["code"], "context": True},
    ]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    assert m.entry("x").context is None  # the bool is rejected, not coerced


# ── version comparison ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("gpt-5.6", "gpt-5.5", True),
        ("gpt-5.5", "gpt-5.5", False),
        ("gpt-5.4", "gpt-5.5", False),
        ("glm-5.3", "glm-5.2", True),
        ("claude-opus-4-9", "claude-opus-4-8", True),
        # Anthropic writes minor versions with a hyphen: these are one family (#934)…
        ("claude-fable-5-1", "claude-fable-5", True),
        ("claude-opus-5", "claude-opus-4-8", True),
        ("claude-opus-5-5", "claude-opus-5", True),
        ("claude-sonnet-5", "claude-sonnet-5-5", False),
        # …but a different VARIANT is never auto-proposed.
        ("gpt-6.1-sol", "gpt-5.5", False),
        ("moonshotai/Kimi-K3", "moonshotai/Kimi-K2.7-Code", False),
        # different family is never "newer" — guards against cross-family proposals
        ("gemini-3.0", "gpt-5.5", False),
        ("claude-fable-6", "claude-opus-4-8", False),
        # DATED / build-suffixed snapshots the real endpoints return (the bug review caught):
        # a newer-version dated snapshot IS a bump for the bare pin…
        ("claude-opus-4-9-20260201", "claude-opus-4-8", True),
        ("gemini-2.5-flash-002", "gemini-2.4-flash", True),
        ("gpt-5.6-2026-01-01", "gpt-5.5", True),
        # …but a re-dated SAME version is NOT newer (no proposal churn on republish)…
        ("claude-opus-4-8-20260201", "claude-opus-4-8", False),
        ("gemini-2.5-flash-002", "gemini-2.5-flash", False),
        ("gpt-5.5-preview", "gpt-5.5", False),
        # …and an OLDER dated snapshot is never newer.
        ("claude-opus-4-7-20260201", "claude-opus-4-8", False),
        # a 5-6 digit snapshot must not extend a hyphen-joined version past its pin
        ("claude-opus-4-8-250902", "claude-opus-4-8", False),
        ("claude-opus-4-9-250902", "claude-opus-4-8", True),
        # kimi-code k3: a next-major IS a bump…
        ("k4", "k3", True),
        # …but k3-256k is a CONTEXT VARIANT of the same generation (`-256k` is not a
        # stripped snapshot suffix, so it lands in a different family) — never proposed.
        ("k3-256k", "k3", False),
        # …and the endpoint's task aliases are a different family entirely.
        ("kimi-for-coding", "k3", False),
        ("kimi-for-coding-highspeed", "k3", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert mf.is_newer(candidate, current) is expected


def test_strip_snapshot_suffix():
    assert mf.strip_snapshot_suffix("claude-opus-4-8-20260101") == "claude-opus-4-8"
    assert mf.strip_snapshot_suffix("gemini-2.5-flash-002") == "gemini-2.5-flash"
    assert mf.strip_snapshot_suffix("gpt-5.5-2026-01-01") == "gpt-5.5"
    assert mf.strip_snapshot_suffix("gpt-5.6-preview") == "gpt-5.6"
    assert mf.strip_snapshot_suffix("gpt-5.5") == "gpt-5.5"  # no suffix → unchanged


def test_model_family_strips_version():
    assert mf.model_family("gpt-5.5") == mf.model_family("gpt-5.6")
    assert mf.model_family("gpt-5.5") != mf.model_family("gemini-2.5-flash")
    # a dated snapshot shares the family of its bare pin (the whole point)
    assert mf.model_family("claude-opus-4-8-20260101") == mf.model_family("claude-opus-4-9")
    # a hyphen-joined version is ONE run: Opus 4.8 and Opus 5 share a family (#934)
    assert mf.model_family("claude-opus-4-8") == mf.model_family("claude-opus-5")


def test_compute_proposals_picks_newest_dated_snapshot(tmp_path: Path):
    """The endpoint returns dated snapshots; the proposal must pick the newest real version."""
    data = {"version": 1, "models": [
        {"id": "claude-opus-4-8", "provider": "anthropic", "capabilities": ["vision", "code"]},
    ]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"anthropic": mf.PollResult("anthropic", True, model_ids=[
        "claude-opus-4-8-20260101",   # same version, re-dated → not a bump
        "claude-opus-4-9-20260201",   # newer → the bump
        "claude-opus-4-7-20251201",   # older
    ])}
    proposals = mf.compute_proposals(m, polls)
    assert len(proposals) == 1
    assert proposals[0].new_id == "claude-opus-4-9-20260201"


# ── proposal computation (mocked endpoints) ─────────────────────────────────────────────
def _fake_poller(provider: str, ids: list[str]):
    def _poll(timeout: float):
        return mf.PollResult(provider, True, model_ids=ids)
    return _poll


def _skip_poller(provider: str, reason: str = "no key"):
    def _poll(timeout: float):
        return mf.PollResult(provider, False, skipped=reason)
    return _poll


def test_compute_proposals_finds_newer(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["vision", "code"]}],
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"openai": mf.PollResult("openai", True, model_ids=["gpt-5.4", "gpt-5.6", "gpt-5.5"])}
    proposals = mf.compute_proposals(m, polls)
    assert len(proposals) == 1
    assert proposals[0].new_id == "gpt-5.6"
    assert proposals[0].current_id == "gpt-5.5"
    # capabilities are copied from the current pin (unverified)
    assert proposals[0].inferred_capabilities == ("vision", "code")


def test_compute_proposals_none_when_current(tmp_path: Path):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"openai": mf.PollResult("openai", True, model_ids=["gpt-5.5", "gpt-4.1"])}
    assert mf.compute_proposals(m, polls) == []


def test_compute_proposals_skips_unpolled_provider(tmp_path: Path):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"openai": mf.PollResult("openai", False, skipped="no key")}
    assert mf.compute_proposals(m, polls) == []


# ── run(): report fallback when gh is absent ────────────────────────────────────────────
def test_run_writes_report_when_no_gh(tmp_path: Path, monkeypatch):
    data = {
        "version": 1,
        "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}],
    }
    manifest = _write_manifest(tmp_path, data)
    reports = tmp_path / "reports"
    monkeypatch.setattr(mf, "REPORTS_DIR", reports)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: False,
    )
    assert result.used_gh is False
    assert result.report_path is not None and result.report_path.is_file()
    body = result.report_path.read_text()
    assert "gpt-5.5" in body and "gpt-5.6" in body
    assert len(result.proposals) == 1


def test_run_report_records_skipped_provider(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "glm-5.2", "provider": "zai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    monkeypatch.setattr(mf, "REPORTS_DIR", tmp_path / "reports")
    result = mf.run(
        manifest_path=manifest,
        pollers={"zai": _skip_poller("zai", "no ZAI_API_KEY")},
        gh_available=lambda: False,
    )
    body = result.report_path.read_text()
    assert "no ZAI_API_KEY" in body
    assert result.proposals == []
    # the report must NOT claim "all current" when a provider was skipped (incomplete coverage)
    assert "INCOMPLETE" in body
    assert "Every polled provider pin is current" not in body


def test_run_report_all_current_only_when_all_polled(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    monkeypatch.setattr(mf, "REPORTS_DIR", tmp_path / "reports")
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.5", "gpt-4.1"])},  # polled, current
        gh_available=lambda: False,
    )
    body = result.report_path.read_text()
    assert "Every polled provider pin is current" in body


def test_run_dry_run_no_gh_writes_nothing(tmp_path: Path, monkeypatch):
    """--dry-run must NEVER write — including the report path when gh is absent (the bug)."""
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    reports = tmp_path / "reports"
    monkeypatch.setattr(mf, "REPORTS_DIR", reports)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: False,
        dry_run=True,
    )
    assert result.report_path is None
    assert not reports.exists(), "dry-run must not create the reports dir / a report file"
    assert any("dry-run" in a and "would write" in a for a in result.actions)
    assert len(result.proposals) == 1  # still computed


def test_run_invalid_manifest_raises(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "code-only", "provider": "openai", "capabilities": ["code"]}],
        "roles": {"vision": "code-only"},
    }
    manifest = _write_manifest(tmp_path, data)
    with pytest.raises(mf.ManifestError):
        mf.run(manifest_path=manifest, pollers={}, gh_available=lambda: False)


# ── gh path: idempotency + dry-run, with subprocess mocked ──────────────────────────────
def test_run_gh_dry_run_no_duplicate(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)

    # an OPEN PR already exists for this bump → propose_via_gh must skip (idempotent)
    monkeypatch.setattr(mf, "_open_pr_exists", lambda proposal: True)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: True,
        dry_run=True,
    )
    assert result.used_gh is True
    assert any("already exists" in a for a in result.actions)


def test_run_gh_dry_run_would_open(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    monkeypatch.setattr(mf, "_open_pr_exists", lambda proposal: False)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: True,
        dry_run=True,
    )
    assert any("would open PR" in a for a in result.actions)


def test_proposal_slug_is_stable_and_safe():
    p = mf.Proposal("commandcode", "moonshotai/Kimi-K2.7-Code", "moonshotai/Kimi-K2.8-Code", ("code",))
    slug = p.slug
    assert "/" not in slug
    assert slug == mf.Proposal("commandcode", "moonshotai/Kimi-K2.7-Code", "moonshotai/Kimi-K2.8-Code", ("code",)).slug


# ── manifest rewriting (the surgical PR diff) ───────────────────────────────────────────
def test_rewrite_manifest_text_swaps_only_the_id():
    text = "models:\n  - id: gpt-5.5\n    provider: openai\n  - id: glm-5.2\n    provider: zai\n"
    out = mf.rewrite_manifest_text(text, "gpt-5.5", "gpt-5.6")
    assert "id: gpt-5.6" in out
    assert "id: glm-5.2" in out  # untouched
    assert "gpt-5.5" not in out


def test_rewrite_manifest_text_handles_quotes():
    text = 'models:\n  - id: "gpt-5.5"\n    provider: openai\n'
    out = mf.rewrite_manifest_text(text, "gpt-5.5", "gpt-5.6")
    assert 'id: "gpt-5.6"' in out


def test_rewrite_manifest_text_noop_when_absent():
    text = "models:\n  - id: gpt-5.5\n"
    assert mf.rewrite_manifest_text(text, "not-here", "x") == text


def test_rewrite_manifest_text_new_id_with_backslash_not_interpreted():
    """`new_id` from an endpoint must be inserted VERBATIM (no re.sub escape interpretation)."""
    text = "models:\n  - id: gpt-5.5\n"
    # an id containing a backslash + a group-ref-looking sequence — a replacement STRING would
    # try to expand \g<1>/\1 and corrupt the output; the callable inserts it literally.
    weird = r"weird\g<1>\1-id"
    out = mf.rewrite_manifest_text(text, "gpt-5.5", weird)
    assert weird in out
    assert "gpt-5.5" not in out


# ── key harvesting ──────────────────────────────────────────────────────────────────────
def test_resolve_key_prefers_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert mf.resolve_key(("OPENAI_API_KEY",)) == "sk-env"


def test_resolve_key_reads_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('OPENAI_API_KEY="sk-fromfile"\n', encoding="utf-8")
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(env_file))
    assert mf.resolve_key(("OPENAI_API_KEY",)) == "sk-fromfile"


def test_resolve_key_none_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("NOPE_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(tmp_path / "missing.env"))
    assert mf.resolve_key(("NOPE_API_KEY",)) is None


# ── endpoint parsers ────────────────────────────────────────────────────────────────────
def test_ids_from_openai_list():
    data = {"data": [{"id": "a"}, {"id": "b"}, {"nope": 1}]}
    assert mf._ids_from_openai_list(data) == ["a", "b"]


def test_ids_from_gemini_list():
    data = {"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/gemini-3.0"}]}
    assert mf._ids_from_gemini_list(data) == ["gemini-2.5-flash", "gemini-3.0"]


# ── kimi-code poller ────────────────────────────────────────────────────────────────────
def test_poll_kimi_code_skips_without_key(tmp_path: Path, monkeypatch):
    """No KIMI_API_KEY (env or .env fallback) and no omp OAuth login → a clean skip, never a crash."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: None)
    result = mf._poll_kimi_code(1.0)
    assert result.provider == "kimi-code"
    assert result.ok is False
    assert "KIMI_API_KEY" in result.skipped
    assert result.error == ""


def test_poll_kimi_code_uses_omp_oauth_token_without_api_key(tmp_path: Path, monkeypatch):
    """No KIMI_API_KEY but an omp kimi-code OAuth login → the stored access token polls."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: "oauth-tok")
    calls = []
    monkeypatch.setattr(
        mf,
        "_http_get_json",
        lambda url, headers, timeout: calls.append((url, headers)) or {"data": []},
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert calls == [
        ("https://api.kimi.com/coding/v1/models", {"Authorization": "Bearer oauth-tok"})
    ]


def test_poll_kimi_code_api_key_beats_omp_oauth(tmp_path: Path, monkeypatch):
    """Precedence: an explicit KIMI_API_KEY wins over the omp OAuth credential."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: "oauth-tok")
    calls = []
    monkeypatch.setattr(
        mf,
        "_http_get_json",
        lambda url, headers, timeout: calls.append(headers) or {"data": []},
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert calls == [{"Authorization": "Bearer sk-kimi"}]


def test_omp_kimi_code_oauth_token_reads_agent_db(tmp_path: Path, monkeypatch):
    """The resolver reads provider=kimi-code/credential_type=oauth from omp's agent.db."""
    import sqlite3

    db_dir = tmp_path / ".omp" / "agent"
    db_dir.mkdir(parents=True)
    db = sqlite3.connect(db_dir / "agent.db")
    db.execute(
        "CREATE TABLE auth_credentials"
        " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
        " disabled_cause TEXT, updated_at INTEGER)"
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
        (json.dumps({"access": "  tok-123  "}),),
    )
    # a disabled / wrong-provider / non-oauth row must NOT be picked
    db.execute(
        "INSERT INTO auth_credentials VALUES (2, 'kimi-code', 'oauth', ?, 'revoked', 20)",
        (json.dumps({"access": "tok-disabled"}),),
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (3, 'openai', 'oauth', ?, NULL, 30)",
        (json.dumps({"access": "tok-other"}),),
    )
    db.commit()
    db.close()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    assert mf._omp_kimi_code_oauth_token() == "tok-123"


def test_omp_kimi_code_oauth_token_missing_db_is_none(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    assert mf._omp_kimi_code_oauth_token() is None


def test_omp_kimi_code_oauth_token_malformed_row_is_none(tmp_path: Path, monkeypatch):
    """not-json, valid JSON of the wrong shape, and a null access must ALL resolve to None
    (never raise, never produce a `Bearer None`)."""
    import sqlite3

    db_dir = tmp_path / ".omp" / "agent"
    db_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    for payload in ("not-json", "[]", "null", json.dumps({"access": None})):
        db_file = db_dir / "agent.db"
        db_file.unlink(missing_ok=True)
        db = sqlite3.connect(db_file)
        db.execute(
            "CREATE TABLE auth_credentials"
            " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
            " disabled_cause TEXT, updated_at INTEGER)"
        )
        db.execute(
            "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
            (payload,),
        )
        db.commit()
        db.close()
        assert mf._omp_kimi_code_oauth_token() is None, payload


def test_omp_token_cli_output_is_used(monkeypatch):
    """Primary path: `omp token kimi-code` stdout is the token (refresh-aware resolver)."""
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        assert cmd == ["omp", "token", "kimi-code"]
        return _sp.CompletedProcess(cmd, 0, stdout="fresh-tok\n", stderr="")

    monkeypatch.setattr(mf.subprocess, "run", fake_run)
    assert mf._omp_token_cli() == "fresh-tok"


def test_omp_token_cli_failure_falls_back_to_db(tmp_path: Path, monkeypatch):
    """omp CLI missing/failing → the read-only agent.db peek still resolves the token."""
    import sqlite3

    db_dir = tmp_path / ".omp" / "agent"
    db_dir.mkdir(parents=True)
    db = sqlite3.connect(db_dir / "agent.db")
    db.execute(
        "CREATE TABLE auth_credentials"
        " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
        " disabled_cause TEXT, updated_at INTEGER)"
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
        (json.dumps({"access": "db-tok"}),),
    )
    db.commit()
    db.close()
    monkeypatch.setenv("HOME", str(tmp_path))

    def boom(cmd, **kwargs):
        raise FileNotFoundError("omp")

    monkeypatch.setattr(mf.subprocess, "run", boom)
    assert mf._omp_kimi_code_oauth_token() == "db-tok"


def test_cross_origin_redirect_strips_authorization():
    """A 30x to a different origin must NOT carry the caller's Authorization header."""
    import urllib.request

    handler = mf._NoCrossOriginAuthRedirect()
    req = urllib.request.Request(
        "https://api.kimi.com/coding/v1/models",
        headers={"Authorization": "Bearer secret"},
    )
    new = handler.redirect_request(
        req, None, 302, "Found", {}, "http://evil.example.test/models"
    )
    assert new is not None
    assert "Authorization" not in new.headers
    assert "Authorization" not in new.unredirected_hdrs


def test_same_origin_redirect_keeps_authorization():
    import urllib.request

    handler = mf._NoCrossOriginAuthRedirect()
    req = urllib.request.Request(
        "https://api.kimi.com/coding/v1/models",
        headers={"Authorization": "Bearer secret"},
    )
    new = handler.redirect_request(
        req, None, 301, "Moved", {}, "https://api.kimi.com/coding/v2/models"
    )
    assert new is not None
    assert new.headers.get("Authorization") == "Bearer secret"


def test_omp_kimi_code_oauth_token_honors_agent_dir_env(tmp_path: Path, monkeypatch):
    """omp can relocate its agent dir (OMP_CODING_AGENT_DIR / PI_CODING_AGENT_DIR) — the
    resolver must follow the same env vars instead of only looking in ~/.omp/agent."""
    import sqlite3

    agent_dir = tmp_path / "custom-agent"
    agent_dir.mkdir()
    db = sqlite3.connect(agent_dir / "agent.db")
    db.execute(
        "CREATE TABLE auth_credentials"
        " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
        " disabled_cause TEXT, updated_at INTEGER)"
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
        (json.dumps({"access": "tok-relocated"}),),
    )
    db.commit()
    db.close()
    monkeypatch.setenv("HOME", str(tmp_path / "no-omp-here"))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    monkeypatch.setenv("OMP_CODING_AGENT_DIR", str(agent_dir))
    assert mf._omp_kimi_code_oauth_token() == "tok-relocated"
    monkeypatch.delenv("OMP_CODING_AGENT_DIR")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    assert mf._omp_kimi_code_oauth_token() == "tok-relocated"


def test_poll_kimi_code_oauth_token_never_leaves_canonical_endpoint(monkeypatch):
    """A custom KIMI_CODE_BASE_URL must NOT receive the harvested omp OAuth token —
    endpoint overrides require an explicitly supplied KIMI_API_KEY."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: "oauth-tok")
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "http://evil.example.test/v1")
    calls = []
    monkeypatch.setattr(
        mf, "_http_get_json", lambda url, headers, timeout: calls.append(url) or {"data": []}
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is False
    assert "KIMI_API_KEY" in result.skipped
    assert calls == [], "no request may be made with the OAuth token to a custom base"


def test_poll_kimi_code_custom_base_ok_with_explicit_key(monkeypatch):
    """The same custom base IS allowed when the caller supplied their own KIMI_API_KEY."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "https://kimi.example.test/v1")
    calls = []
    monkeypatch.setattr(
        mf,
        "_http_get_json",
        lambda url, headers, timeout: calls.append((url, headers)) or {"data": []},
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert calls == [
        ("https://kimi.example.test/v1/models", {"Authorization": "Bearer sk-kimi"})
    ]


def test_poll_kimi_code_parses_openai_list(monkeypatch):
    """With a key, GET <base>/models and parse the OpenAI-compatible id list."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.delenv("KIMI_CODE_BASE_URL", raising=False)
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers))
        return {"data": [{"id": "k3"}, {"id": "k3-256k"}, {"id": "kimi-for-coding"}]}

    monkeypatch.setattr(mf, "_http_get_json", fake_get)
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert result.model_ids == ["k3", "k3-256k", "kimi-for-coding"]
    assert calls == [
        ("https://api.kimi.com/coding/v1/models", {"Authorization": "Bearer sk-kimi"})
    ]


def test_poll_kimi_code_honors_base_url_override(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "https://kimi.example.test/v1/")
    calls = []
    monkeypatch.setattr(
        mf, "_http_get_json", lambda url, headers, timeout: calls.append(url) or {"data": []}
    )
    mf._poll_kimi_code(1.0)
    assert calls == ["https://kimi.example.test/v1/models"]  # trailing slash stripped


def test_poll_kimi_code_ignores_kimi_base_url(monkeypatch):
    """KIMI_BASE_URL is the direct-API moonshot var, NOT the coding-endpoint override —
    setting it must not redirect this poller (only KIMI_CODE_BASE_URL does)."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    monkeypatch.delenv("KIMI_CODE_BASE_URL", raising=False)
    calls = []
    monkeypatch.setattr(
        mf, "_http_get_json", lambda url, headers, timeout: calls.append(url) or {"data": []}
    )
    mf._poll_kimi_code(1.0)
    assert calls == ["https://api.kimi.com/coding/v1/models"]


def test_poll_kimi_code_http_error_is_error_not_crash(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")

    def boom(url, headers, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(mf, "_http_get_json", boom)
    result = mf._poll_kimi_code(1.0)
    assert result.ok is False
    assert "connection refused" in result.error


def test_kimi_code_poller_registered():
    assert mf.POLLERS["kimi-code"] is mf._poll_kimi_code


# ── schema conformance: the manifest validates against models.schema.json ───────────────
def test_manifest_matches_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)


def test_schema_enum_matches_known_capabilities():
    """The checker's KNOWN_CAPABILITIES must match the schema's capability enum (no drift)."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    enum = schema["$defs"]["capability"]["enum"]
    assert set(enum) == set(mf.KNOWN_CAPABILITIES)


# ── main() exit codes + structured errors (#89: adopt agenttools_errors) ────────────────
# main() is the cron entrypoint; a calling script branches on its exit code, so the code per
# failure class is a contract. These cover the boundary the unit tests above never touched.
import agenttools_errors as errs  # noqa: E402  (lib/ is on sys.path; sibling of checker)


def _invalid_manifest(tmp_path: Path) -> Path:
    """A manifest that loads but violates a cross-reference invariant (vision→non-vision)."""
    data = {
        "version": 1,
        "models": [{"id": "code-only", "provider": "openai", "capabilities": ["code"]}],
        "roles": {"vision": "code-only"},
    }
    return _write_manifest(tmp_path, data)


def test_main_validate_invalid_manifest_exits_usage_not_internal(tmp_path, monkeypatch, capsys):
    """A manifest that violates an invariant is a CONFIG error → exit 2 (EXIT_USAGE/CONFIG),
    NOT 1 (EXIT_INTERNAL is reserved for unexpected bugs/tracebacks)."""
    manifest = _invalid_manifest(tmp_path)
    monkeypatch.setattr(mf, "load_manifest", _load_factory(manifest))

    code = mf.main(["--validate"])
    # An invariant violation raises ConfigError → EXIT_CONFIG. (EXIT_USAGE == EXIT_CONFIG == 2,
    # but assert against the constant the code actually raises so this can't silently lie if
    # agenttools_errors ever splits the two.)
    assert code == errs.EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert "why:" in err and "fix:" in err  # structured 3-part block, not a bare line


def test_main_malformed_manifest_exits_usage(tmp_path, monkeypatch, capsys):
    """An unparseable / non-mapping manifest → structured config error, exit 2."""
    bad = tmp_path / "models.yaml"
    bad.write_text("just a string, not a mapping\n", encoding="utf-8")
    monkeypatch.setattr(mf, "load_manifest", _load_factory(bad))

    code = mf.main([])  # live run path; load_manifest raises before any network
    assert code == errs.EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert "why:" in err and "fix:" in err


def test_main_missing_pyyaml_exits_missing_dep(monkeypatch, capsys):
    """PyYAML absent is a missing-DEPENDENCY failure → exit 127 + an install hint,
    distinct from a malformed-config (2)."""
    def _raise_missing_dep(*a, **k):
        # MissingYamlError is exactly what load_manifest() raises when `import yaml` fails.
        raise mf.MissingYamlError(
            "PyYAML is required to read the manifest but is not installed. "
            "Install it: `python3 -m pip install --user pyyaml`."
        )
    monkeypatch.setattr(mf, "load_manifest", _raise_missing_dep)

    code = mf.main(["--validate"])
    assert code == errs.EXIT_MISSING_DEP == 127
    out = capsys.readouterr()
    blob = out.err + out.out
    assert "install" in blob.lower()


def test_main_validate_ok_exits_zero(monkeypatch, capsys):
    """The happy path stays byte-compatible: a valid manifest → exit 0, human summary."""
    monkeypatch.setattr(mf, "load_manifest", _load_factory(MANIFEST))
    code = mf.main(["--validate"])
    assert code == errs.EXIT_OK == 0
    assert "manifest OK" in capsys.readouterr().out


def test_main_run_path_manifest_error_exits_config(monkeypatch, capsys):
    """The SECOND ManifestError mapping — a fault surfacing from run() (not the initial
    load_manifest) — must also render structured + exit 2. run() reloads the manifest, so a
    ManifestError from there is the live-run analogue of the validate-path config error."""
    monkeypatch.setattr(mf, "load_manifest", _load_factory(MANIFEST))  # initial load is fine

    def _boom(*a, **k):
        raise mf.ManifestError("manifest invalid:\n  - vision role points at a non-vision model")
    monkeypatch.setattr(mf, "run", _boom)

    code = mf.main([])  # live-run path (no --validate)
    assert code == errs.EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert "why:" in err and "fix:" in err


def test_main_unexpected_bug_propagates_not_swallowed(monkeypatch):
    """Contract (README): an UNEXPECTED exception is a bug → guard() must let it propagate
    (traceback + exit 1), NOT diagnose it as a config error. A regression where guard starts
    swallowing arbitrary exceptions would otherwise pass unnoticed."""
    def _bug(*a, **k):
        raise RuntimeError("something genuinely unexpected broke")
    monkeypatch.setattr(mf, "load_manifest", _bug)

    with pytest.raises(RuntimeError, match="genuinely unexpected"):
        mf.main(["--validate"])


def _load_factory(path: Path):
    """Return a load_manifest replacement that ALWAYS reads `path` (main() calls it with no
    arg, so the default-path plumbing can't be redirected otherwise). Delegates to the real
    parser by importing the module's function object captured before patching."""
    real = _REAL_LOAD_MANIFEST

    def _load_manifest(p: Path = path) -> "mf.Manifest":  # noqa: F821
        return real(path)

    return _load_manifest


# Capture the genuine parser once, before any test monkeypatches mf.load_manifest.
_REAL_LOAD_MANIFEST = mf.load_manifest


# ── keyless harness catalogs (#934) ─────────────────────────────────────────────────────
def test_omp_catalog_groups_by_provider(monkeypatch):
    import subprocess as _sp

    payload = {"models": [
        {"provider": "anthropic", "id": "claude-opus-5-5"},
        {"provider": "anthropic", "id": "claude-opus-5-5"},  # duplicate collapses
        {"provider": "openai-codex", "id": "gpt-6.1-sol"},  # mapped onto the manifest name
        {"provider": "anthropic"},  # malformed rows are skipped
    ]}

    def fake_run(cmd, **kwargs):
        assert cmd == ["omp", "models", "--json"]
        return _sp.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(mf.subprocess, "run", fake_run)
    assert mf._omp_catalog(5) == {"anthropic": ["claude-opus-5-5"], "openai": ["gpt-6.1-sol"]}


@pytest.mark.parametrize(
    "behaviour,reason",
    [("missing", "not installed"), ("exit", "exited 1"), ("garbage", "invalid JSON")],
)
def test_omp_catalog_unavailable(monkeypatch, behaviour, reason):
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        if behaviour == "missing":
            raise FileNotFoundError("omp")
        if behaviour == "exit":
            return _sp.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return _sp.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    monkeypatch.setattr(mf.subprocess, "run", fake_run)
    with pytest.raises(mf.CatalogUnavailable, match=reason):
        mf._omp_catalog(5)


def test_codex_catalog_reads_models_cache(tmp_path: Path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6.1-sol", "priority": 1}, {"slug": "gpt-6-luna"}, {"display_name": "no slug"},
    ]}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert mf._codex_catalog(5) == {"openai": ["gpt-6.1-sol", "gpt-6-luna"]}


def test_codex_catalog_missing_file_is_unavailable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(mf.CatalogUnavailable, match="not found"):
        mf._codex_catalog(5)


def test_codex_catalog_older_than_a_week_is_unavailable(tmp_path: Path, monkeypatch):
    """An old cache must not stand in for the account's current list (review finding):
    the provider then falls through to models.dev."""
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-01-02T03:04:05.123456789Z", "models": [{"slug": "gpt-6-sol"}],
    }))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(mf.CatalogUnavailable, match="stale"):
        mf._codex_catalog(5)


def test_codex_catalog_fresh_timestamp_is_accepted(tmp_path: Path, monkeypatch):
    now = mf._dt.datetime.now(mf._dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": now, "models": [{"slug": "gpt-6.1-sol"}],
    }))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert mf._codex_catalog(5) == {"openai": ["gpt-6.1-sol"]}


def test_claude_catalog_skips_a_broken_or_stale_file_and_keeps_the_good_one(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache" / "model-catalog"
    cache.mkdir(parents=True)
    (cache / "a-truncated.json").write_text('{"catalog": ')
    (cache / "b-old.json").write_text(json.dumps({
        "fetchedAt": 1_700_000_000_000, "catalog": {"config": {"models": [{"id": "claude-opus-4-8"}]}},
    }))
    (cache / "c-good.json").write_text(json.dumps({"catalog": {"config": {"models": [{"id": "claude-opus-5-5"}]}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert mf._claude_catalog(5) == {"anthropic": ["claude-opus-5-5"]}


def test_claude_catalog_reads_models_and_alias_table(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache" / "model-catalog"
    cache.mkdir(parents=True)
    (cache / "org-cc.json").write_text(json.dumps({"catalog": {"config": {
        "models": [{"id": "claude-opus-5-5"}, {"id": "claude-fable-5"}],
        "provider_alias_targets": {
            "fable": {"default": "claude-fable-5-1", "per_provider": {"gateway": "claude-fable-5"}},
            "opus": {"default": "claude-opus-5"},
        },
    }}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert mf._claude_catalog(5) == {
        "anthropic": ["claude-opus-5-5", "claude-fable-5", "claude-fable-5-1", "claude-opus-5"]
    }


def test_claude_catalog_absent_is_unavailable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(mf.CatalogUnavailable, match="no Claude Code model catalog"):
        mf._claude_catalog(5)


@pytest.mark.parametrize("payload", [
    {"catalog": None},
    {"catalog": {"config": "not a mapping"}},
    {"catalog": {"config": {"models": "nope", "provider_alias_targets": ["not", "a", "map"]}}},
    ["not", "a", "mapping"],
    # an out-of-range fetch time is an unknown age, not a crash
    {"fetchedAt": 1e100, "catalog": None},
])
def test_malformed_claude_catalog_is_a_recorded_error_not_a_crash(tmp_path: Path, monkeypatch, payload):
    """One malformed optional cache must not abort the daily run (review finding)."""
    cache = tmp_path / "cache" / "model-catalog"
    cache.mkdir(parents=True)
    (cache / "bad-cc.json").write_text(json.dumps(payload))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    [catalog] = mf.load_catalogs({"claude": mf._claude_catalog}, 5)
    assert catalog.ids == {} and "lists no models" in catalog.error


def test_codex_catalog_impossible_timestamp_is_unknown_age(tmp_path: Path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-99-17T00:00:00Z", "models": [{"slug": "gpt-6.1-sol"}],
    }))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert mf._codex_catalog(5) == {"openai": ["gpt-6.1-sol"]}


def test_models_dev_catalog_maps_provider_names(monkeypatch):
    seen = {}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        return {
            "google": {"models": {"gemini-3.8-flash": {}}},
            "zai-coding-plan": {"models": {"glm-5.3": {}}},
            "xai": {"models": {"grok-4.7": {}}},
            "broken": "not a mapping",
        }

    monkeypatch.delenv("MODELS_DEV_URL", raising=False)
    monkeypatch.setattr(mf, "_http_get_json", fake_get)
    assert mf._models_dev_catalog(5) == {
        "gemini": ["gemini-3.8-flash"], "zai": ["glm-5.3"], "xai": ["grok-4.7"],
    }
    assert seen["url"] == mf.MODELS_DEV_URL


def test_models_dev_catalog_network_error_is_unavailable(monkeypatch):
    def fake_get(url, headers, timeout):
        raise mf.urllib.error.URLError("403")

    monkeypatch.setattr(mf, "_http_get_json", fake_get)
    with pytest.raises(mf.CatalogUnavailable, match="models.dev unreachable"):
        mf._models_dev_catalog(5)


def test_catalog_ids_outside_the_model_id_shape_are_dropped(monkeypatch):
    """models.dev ids reach the markdown report; markup or spaces must never be echoed."""
    def fake_get(url, headers, timeout):
        return {"openai": {"models": {"gpt-9-<img src=x>": {}, "gpt 9": {}, "gpt-6.1-sol": {}}}}

    monkeypatch.setattr(mf, "_http_get_json", fake_get)
    assert mf._models_dev_catalog(5) == {"openai": ["gpt-6.1-sol"]}


def test_run_stale_local_cache_falls_through_to_models_dev(tmp_path: Path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-01-02T03:04:05Z", "models": [{"slug": "gpt-6-sol"}],
    }))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    data = {"version": 1, "models": [{"id": "gpt-6-sol", "provider": "openai", "capabilities": ["code"]}]}
    result = mf.run(
        manifest_path=_write_manifest(tmp_path, data),
        pollers={"openai": _skip_poller("openai", "no OPENAI_API_KEY")},
        gh_available=lambda: False,
        dry_run=True,
        harness_sources={"codex": mf._codex_catalog},
        fallback_sources={"models.dev": lambda timeout: {"openai": ["gpt-6-sol", "gpt-6.1-sol"]}},
    )
    assert result.polls["openai"].sources == ["models.dev"]
    assert [p.new_id for p in result.proposals] == ["gpt-6.1-sol"]


def test_http_get_json_sends_a_user_agent(monkeypatch):
    """Cloudflare-fronted hosts 403 urllib's default UA (models.dev, commandcode)."""
    import io

    captured = {}

    class FakeOpener:
        def open(self, req, timeout):
            captured["ua"] = req.get_header("User-agent")
            captured["auth"] = req.get_header("Authorization")
            return io.BytesIO(b'{"data": []}')

    monkeypatch.setattr(mf.urllib.request, "build_opener", lambda *handlers: FakeOpener())
    mf._http_get_json("https://example.test/models", {"Authorization": "Bearer k"}, 5)
    assert captured["ua"] == mf.USER_AGENT
    assert captured["auth"] == "Bearer k"


def test_run_polls_anthropic_and_openai_keyless_through_harness_catalogs(tmp_path: Path):
    """The OAuth-only machine: both keyed pollers skip, the harness catalogs answer."""
    data = {"version": 1, "models": [
        {"id": "claude-fable-5", "provider": "anthropic", "capabilities": ["vision", "code"]},
        {"id": "gpt-6-sol", "provider": "openai", "capabilities": ["code"]},
    ]}
    fallback_calls = []

    def models_dev(timeout):
        fallback_calls.append(timeout)
        return {}

    result = mf.run(
        manifest_path=_write_manifest(tmp_path, data),
        pollers={
            "anthropic": _skip_poller("anthropic", "no ANTHROPIC_API_KEY"),
            "openai": _skip_poller("openai", "no OPENAI_API_KEY"),
        },
        gh_available=lambda: False,
        dry_run=True,
        harness_sources={
            "omp": lambda timeout: {"anthropic": ["claude-fable-5", "claude-opus-5-5"]},
            "codex": lambda timeout: {"openai": ["gpt-6.1-sol", "gpt-6-sol"]},
            "claude": lambda timeout: {"anthropic": ["claude-fable-5-1"]},
        },
        fallback_sources={"models.dev": models_dev},
    )
    assert result.polls["anthropic"].ok and result.polls["anthropic"].sources == ["omp", "claude"]
    assert result.polls["openai"].ok and result.polls["openai"].sources == ["codex"]
    assert fallback_calls == [], "models.dev is fetched only when no local catalog answers"
    assert {(p.current_id, p.new_id) for p in result.proposals} == {
        ("claude-fable-5", "claude-fable-5-1"),
        ("gpt-6-sol", "gpt-6.1-sol"),
    }


def test_run_lists_pins_whose_successor_is_a_different_variant(tmp_path: Path, monkeypatch):
    """gpt-5.5 -> gpt-6.1-sol is never auto-proposed, but the stale pin must not be silent."""
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    monkeypatch.setattr(mf, "REPORTS_DIR", tmp_path / "reports")
    result = mf.run(
        manifest_path=_write_manifest(tmp_path, data),
        pollers={"openai": _fake_poller("openai", ["gpt-5.5", "gpt-6-luna", "gpt-6.1-sol"])},
        gh_available=lambda: True,
    )
    assert result.proposals == []
    assert result.pin_variants == {"gpt-5.5": ["gpt-6.1-sol", "gpt-6-luna"]}
    assert "`gpt-5.5`: `gpt-6.1-sol`, `gpt-6-luna`" in result.report_path.read_text()


def test_run_falls_back_to_models_dev_once(tmp_path: Path):
    data = {"version": 1, "models": [
        {"id": "gemini-3.7-flash", "provider": "gemini", "capabilities": ["code"]},
        {"id": "glm-5.2", "provider": "zai", "capabilities": ["code"]},
    ]}
    calls = []

    def models_dev(timeout):
        calls.append(timeout)
        return {"gemini": ["gemini-3.8-flash"], "zai": ["glm-5.3"]}

    result = mf.run(
        manifest_path=_write_manifest(tmp_path, data),
        pollers={"gemini": _skip_poller("gemini"), "zai": _skip_poller("zai")},
        gh_available=lambda: False,
        dry_run=True,
        harness_sources={},
        fallback_sources={"models.dev": models_dev},
    )
    assert len(calls) == 1
    assert result.polls["gemini"].sources == ["models.dev"]
    assert {p.new_id for p in result.proposals} == {"gemini-3.8-flash", "glm-5.3"}


def test_merge_poll_keeps_the_keyed_skip_reason_when_nothing_answers():
    api = mf.PollResult("anthropic", False, skipped="no ANTHROPIC_API_KEY")
    merged = mf.merge_poll(api, [mf.HarnessCatalog("codex", ids={"openai": ["gpt-6.1-sol"]}),
                                 mf.HarnessCatalog("omp", error="omp is not installed")])
    assert merged is api


def test_an_empty_keyed_answer_still_falls_back_to_models_dev():
    """`{"data": []}` from a keyed endpoint is not coverage (review finding)."""
    calls = []

    def models_dev(timeout):
        calls.append(timeout)
        return {"openai": ["gpt-6.1-sol"]}

    catalogs = mf.CatalogSet([], {"models.dev": models_dev}, 5)
    merged = catalogs.poll(mf.PollResult("openai", True, model_ids=[]))
    assert merged.model_ids == ["gpt-6.1-sol"] and merged.sources == ["models.dev"]
    assert len(calls) == 1


def test_merge_poll_unions_api_and_catalog_ids():
    api = mf.PollResult("zai", True, model_ids=["glm-5.2"])
    merged = mf.merge_poll(api, [mf.HarnessCatalog("omp", ids={"zai": ["glm-5.2", "glm-5.3"]})])
    assert merged.model_ids == ["glm-5.2", "glm-5.3"]
    assert merged.sources == ["api", "omp"]


def test_newest_same_family_prefers_the_bare_id_on_a_tie():
    ids = ["Qwen/Qwen3.8-Max-0902", "Qwen/Qwen3.8-Max", "Qwen/Qwen3.7-Max"]
    assert mf.newest_same_family("Qwen/Qwen3.7-Max", ids) == "Qwen/Qwen3.8-Max"


def test_newer_variants_lists_the_line_successors_newest_first():
    ids = ["gpt-5.6-terra", "gpt-6-luna", "gpt-6.1-sol", "gpt-5.5-pro", "gpt-image-2"]
    assert mf.newer_variants("gpt-5.6-terra", ids) == ["gpt-6.1-sol", "gpt-6-luna"]
    assert mf.newer_variants("moonshotai/Kimi-K2.7-Code", ["moonshotai/Kimi-K3"]) == [
        "moonshotai/Kimi-K3"
    ]
    # a hyphenated SIZE (`-27B`) is not a minor version: Qwen3.8-27B is not newer than 3.8-Max
    sizes = ["Qwen/Qwen3.8-27B", "Qwen/Qwen3.8-1.7B", "Qwen/Qwen3.8-0.6B"]
    assert mf.newer_variants("Qwen/Qwen3.8-Max", sizes) == []
    assert mf.model_family("Qwen/Qwen3.8-1.7B") == "Qwen/Qwen#-#B"
    assert mf.model_family("Qwen/Qwen3.8-27B") == "Qwen/Qwen#-#B"
    assert mf.model_family("claude-3-5-sonnet") == "claude-#-sonnet"


# ── review-cli seats (#934) ─────────────────────────────────────────────────────────────
_REVIEW_CLI_CONFIG_PY = '''\
SONNET_SEAT: str = "claude:claude-sonnet-5"
# FABLE_SEAT used to be "claude:claude-fable-4"
TERRA_SEAT = "codex:gpt-5.6-terra"
GLM_SEAT = "oc:zai/glm-5.2"
MODEL_ALIASES = {
    "opus": "claude:claude-opus-4-8",
    "opus48": "claude:claude-opus-4-8",
    "sonnet5": SONNET_SEAT,
    "glm52": (
        GLM_SEAT
    ),
}
DEFAULT_MODELS = ("codex", "gemini", SONNET_SEAT)
EXOTIC = "oc:nowhere/model-1"
'''

_REVIEW_CLI_CONFIG_YAML = '''\
models:
  - "claude:claude-opus-4-8"
  - zai:glm-5.2  # bare value
# - "claude:claude-sonnet-5" commented out
board:
  - { model: "omp:anthropic/claude-sonnet-5", role: quality }
'''

_CATALOG = {
    "anthropic": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"],
    "openai": ["gpt-6-luna", "gpt-5.6-terra"],
    "zai": ["glm-5.3"],
}


def _review_cli_files(tmp_path: Path) -> "mf.ReviewCliFiles":
    code = tmp_path / "review-cli" / "reviewlib" / "config.py"
    code.parent.mkdir(parents=True)
    code.write_text(_REVIEW_CLI_CONFIG_PY)
    user = tmp_path / "config.yaml"
    user.write_text(_REVIEW_CLI_CONFIG_YAML)
    return mf.ReviewCliFiles(code_defaults=code, user_config=user)


def test_scan_review_cli_names_stale_code_default_and_config_seats(tmp_path: Path):
    files = _review_cli_files(tmp_path)
    report = mf.scan_review_cli(files, lambda provider: _CATALOG.get(provider, []))
    stale = {(pin.path.name, pin.line, pin.seat, pin.new_seat) for pin in report.stale if pin.newer}
    assert stale == {
        ("config.py", 1, "claude:claude-sonnet-5", "claude:claude-sonnet-5-5"),
        ("config.py", 4, "oc:zai/glm-5.2", "oc:zai/glm-5.3"),
        ("config.py", 6, "claude:claude-opus-4-8", "claude:claude-opus-5-5"),
        ("config.yaml", 2, "claude:claude-opus-4-8", "claude:claude-opus-5-5"),
        ("config.yaml", 3, "zai:glm-5.2", "zai:glm-5.3"),
        ("config.yaml", 6, "omp:anthropic/claude-sonnet-5", "omp:anthropic/claude-sonnet-5-5"),
    }
    # the version-pinned alias (line 7), the comments and bare `codex` are not seats to bump
    assert not any(pin.line == 7 and pin.path.name == "config.py" for pin in report.stale)
    variants = [pin for pin in report.stale if not pin.newer]
    assert [(pin.seat, pin.variants) for pin in variants] == [("codex:gpt-5.6-terra", ["gpt-6-luna"])]
    assert report.unchecked == ["oc:nowhere/model-1"]


def test_review_cli_patch_bumps_only_code_default_same_family_seats(tmp_path: Path):
    files = _review_cli_files(tmp_path)
    report = mf.scan_review_cli(files, lambda provider: _CATALOG.get(provider, []))
    patch = mf.review_cli_patch(report)
    assert patch.startswith("--- a/reviewlib/config.py\n+++ b/reviewlib/config.py\n")
    added = [line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert added == [
        '+SONNET_SEAT: str = "claude:claude-sonnet-5-5"',
        '+GLM_SEAT = "oc:zai/glm-5.3"',
        '+    "opus": "claude:claude-opus-5-5",',
        # a version-pinned alias pointing at a bumped constant keeps its release
        '+    "sonnet5": "claude:claude-sonnet-5",',
        # …also when the alias value is wrapped over several lines
        '+        "oc:zai/glm-5.2"',
    ]
    # the patch applies cleanly in the review-cli checkout
    import subprocess as _sp

    root = files.code_defaults.parent.parent
    _sp.run(["git", "init", "-q"], cwd=root, check=True)
    applied = _sp.run(["git", "apply", "-"], cwd=root, input=patch, text=True, capture_output=True)
    assert applied.returncode == 0, applied.stderr
    text = files.code_defaults.read_text()
    assert '"opus48": "claude:claude-opus-4-8"' in text  # pinned alias untouched
    assert files.user_config.read_text() == _REVIEW_CLI_CONFIG_YAML  # user config never patched


def test_review_cli_patch_empty_when_nothing_is_stale(tmp_path: Path):
    files = _review_cli_files(tmp_path)
    report = mf.scan_review_cli(files, lambda provider: [])
    assert mf.review_cli_patch(report) == ""


def test_scan_review_cli_unparseable_python_is_unchecked_not_a_crash(tmp_path: Path):
    files = _review_cli_files(tmp_path)
    files.code_defaults.write_text('SONNET_SEAT = "claude:claude-sonnet-5"\nthis is not python(\n')
    report = mf.scan_review_cli(files, lambda provider: _CATALOG.get(provider, []))
    assert any("not parseable" in entry for entry in report.unchecked)
    assert all(pin.path == files.user_config for pin in report.stale)
    assert mf.review_cli_patch(report) == ""


def test_unreadable_review_cli_config_does_not_cost_the_run(tmp_path: Path, monkeypatch):
    files = _review_cli_files(tmp_path)
    files.user_config.write_bytes(b"models:\n  - \"claude:claude-opus-4-8\"\n\xff\xfe\n")
    monkeypatch.setattr(mf, "REPORTS_DIR", tmp_path / "reports")
    data = {"version": 1, "models": [{"id": "glm-5.2", "provider": "zai", "capabilities": ["code"]}]}
    result = mf.run(
        manifest_path=_write_manifest(tmp_path, data),
        pollers={"zai": _fake_poller("zai", ["glm-5.3"])},
        gh_available=lambda: False,
        dry_run=True,
        review_cli=files,
    )
    assert [p.new_id for p in result.proposals] == ["glm-5.3"]
    assert any("unreadable" in entry for entry in result.review_cli.unchecked)


def test_seat_ids_with_suffixes_are_read_whole(tmp_path: Path):
    """`:free` / `@x` suffixes are part of the id in both file formats (review finding)."""
    files = _review_cli_files(tmp_path)
    files.code_defaults.write_text('FREE_SEAT = "oc:openrouter/vendor/model-1:free"\n')
    files.user_config.write_text('models:\n  - oc:openrouter/vendor/model-1:free\n')
    catalog = {"openrouter": ["vendor/model-2", "vendor/model-2:free"]}
    report = mf.scan_review_cli(files, lambda provider: catalog.get(provider, []))
    assert sorted((pin.path.name, pin.new_seat) for pin in report.stale) == [
        ("config.py", "oc:openrouter/vendor/model-2:free"),
        ("config.yaml", "oc:openrouter/vendor/model-2:free"),
    ]


def test_main_dry_run_never_writes_a_patch_file(tmp_path: Path, monkeypatch, capsys):
    files = _review_cli_files(tmp_path)
    monkeypatch.setattr(mf, "REVIEW_CLI_CONFIG", files.user_config)
    monkeypatch.setattr(mf, "_gh_available", lambda: False)
    monkeypatch.setattr(mf, "POLLERS", {})
    monkeypatch.setattr(mf, "HARNESS_SOURCES", {"claude": lambda timeout: {"anthropic": _CATALOG["anthropic"]}})
    target = tmp_path / "out.patch"
    root = str(tmp_path / "review-cli")
    assert mf.main(["--dry-run", "--review-cli-root", root, "--review-cli-patch", str(target)]) == 0
    assert not target.exists()
    assert "dry-run: would write review-cli patch" in capsys.readouterr().out


def test_main_rejects_json_with_patch_on_stdout(capsys):
    with pytest.raises(SystemExit) as exc:
        mf.main(["--json", "--review-cli-patch", "-"])
    assert exc.value.code == 2
    assert "drop --json" in capsys.readouterr().err


def test_main_no_patch_file_when_nothing_is_stale(tmp_path: Path, monkeypatch, capsys):
    files = _review_cli_files(tmp_path)
    monkeypatch.setattr(mf, "REVIEW_CLI_CONFIG", files.user_config)
    monkeypatch.setattr(mf, "_gh_available", lambda: False)
    monkeypatch.setattr(mf, "POLLERS", {})
    monkeypatch.setattr(mf, "REPORTS_DIR", tmp_path / "reports")
    target = tmp_path / "out.patch"
    root = str(tmp_path / "review-cli")
    assert mf.main(["--review-cli-root", root, "--review-cli-patch", str(target)]) == 0
    assert not target.exists()
    assert "no patch written" in capsys.readouterr().out


def test_find_review_cli_files_from_explicit_root(tmp_path: Path, monkeypatch):
    files = _review_cli_files(tmp_path)
    monkeypatch.setattr(mf, "REVIEW_CLI_CONFIG", files.user_config)
    found = mf.find_review_cli_files(tmp_path / "review-cli")
    assert found == files
    assert mf.find_review_cli_files(tmp_path / "missing").code_defaults is None


def test_run_reports_stale_review_cli_seats_and_writes_the_patch(tmp_path: Path, monkeypatch):
    """Even with gh available (models.yaml bumps go out as PRs) the review-cli findings,
    which have no PR path here, land in the dated report plus a patch file."""
    files = _review_cli_files(tmp_path)
    reports = tmp_path / "reports"
    monkeypatch.setattr(mf, "REPORTS_DIR", reports)
    data = {"version": 1, "models": [
        {"id": "claude-opus-5-5", "provider": "anthropic", "capabilities": ["code"]},
    ]}
    result = mf.run(
        manifest_path=_write_manifest(tmp_path, data),
        pollers={"anthropic": _skip_poller("anthropic")},
        gh_available=lambda: True,
        harness_sources={"claude": lambda timeout: {"anthropic": _CATALOG["anthropic"]}},
        fallback_sources={"models.dev": lambda timeout: _CATALOG},
        review_cli=files,
    )
    assert result.proposals == []
    body = result.report_path.read_text()
    assert "STALE `claude:claude-sonnet-5` -> `claude:claude-sonnet-5-5`" in body
    assert "newer variants for `codex:gpt-5.6-terra`: gpt-6-luna" in body
    patches = list(reports.glob("*-review-cli.patch"))
    assert len(patches) == 1 and "claude:claude-opus-5-5" in patches[0].read_text()


def test_main_review_cli_patch_to_stdout(tmp_path: Path, monkeypatch, capsys):
    files = _review_cli_files(tmp_path)
    monkeypatch.setattr(mf, "REVIEW_CLI_CONFIG", files.user_config)
    monkeypatch.setattr(mf, "_gh_available", lambda: False)
    monkeypatch.setattr(mf, "POLLERS", {})
    monkeypatch.setattr(mf, "HARNESS_SOURCES", {"claude": lambda timeout: {"anthropic": _CATALOG["anthropic"]}})
    code = mf.main([
        "--dry-run", "--review-cli-root", str(tmp_path / "review-cli"), "--review-cli-patch", "-",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("--- a/reviewlib/config.py")
    assert '+SONNET_SEAT: str = "claude:claude-sonnet-5-5"' in out

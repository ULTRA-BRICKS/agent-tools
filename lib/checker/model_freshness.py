#!/usr/bin/env python3
"""model-freshness — propose model-version bumps for lib/contracts/models.yaml.

Accessed via: the daily cron rig provisions (`rig` installs a launchd plist on macOS / a
  crontab line on Linux that runs `python3 .../lib/checker/model_freshness.py`). Also
  runnable by hand and imported by tests (`from checker.model_freshness import run`).

What it does (semi-automatic, PROPOSE-never-merge):
  1. Reads lib/contracts/models.yaml — the current per-provider pins.
  2. For each provider that exposes a model-list endpoint (OpenAI /v1/models,
     Anthropic /v1/models, Google generativelanguage, commandcode + z.ai
     OpenAI-compatible /models), polls it using a key harvested from the existing
     CLI/env configs (env vars first, then the same `.env` fallback files review-cli
     reads). A provider whose key is ABSENT is skipped — never a crash.
  2b. Merges in the KEYLESS harness catalogs this machine's logins already cache
     (`omp models --json`, codex's models_cache.json, Claude Code's model-catalog cache
     with its alias table) and, for a provider none of those cover, the keyless
     https://models.dev/api.json. On an OAuth-only machine (every login a subscription,
     no API keys) these are what reach anthropic and openai at all (#934).
  3. When the live endpoint advertises a NEWER version than the manifest pins for that
     provider/role, it PROPOSES a bump: opens a PR against agent-tools editing
     models.yaml (via `gh`) with the diff + the new model's advertised capabilities;
     OR, if `gh`/auth is absent, writes a dated report to lib/checker/reports/.
  3b. Unless --no-review-cli, also checks review-cli's code-default seats (reviewlib/config.py
     of the checkout `review` runs from) and the user's ~/.config/review-cli/config.yaml
     against the same catalogs, reports every stale seat, and can emit a `git apply`-able
     patch for the code defaults (--review-cli-patch). review-cli's board never reads
     models.yaml, so a models.yaml bump alone would never reach a review.
  4. Idempotent: if a proposal PR for the same bump is already open, it does not open a
     duplicate; the report path is deterministic per (provider, new id) so a re-run
     overwrites rather than piling up.

Stdlib-first: only the standard library is imported at module load. `yaml` is imported
lazily inside the manifest loader (the manifest is YAML); `urllib.request` does the HTTP.
No third-party HTTP client, no provider SDKs.

Safety posture: a newer model can REGRESS (a "turbo" variant may drop vision, a point
release may be cheaper-but-worse). So the checker never edits the manifest in place and
never merges — a human confirms every bump. The capabilities of the proposed model are
surfaced in the proposal so the human can see, e.g., that the candidate lost `vision`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# The cron runs this file directly (`python3 .../lib/checker/model_freshness.py`), which puts
# `lib/checker/` — not `lib/` — on sys.path, so a sibling package (`agenttools_errors`) won't
# resolve without help. Put `lib/` on the path so the shared error layer imports both when run
# as a script AND when imported as `checker.model_freshness` (the test/cron-as-module path).
_LIB_ROOT = Path(__file__).resolve().parent.parent
if str(_LIB_ROOT) not in sys.path:
    # append, not insert(0): the lib/ packages are uniquely named (agenttools_*), so appending
    # resolves them without risking shadowing a same-named stdlib/site module at the front.
    sys.path.append(str(_LIB_ROOT))

# Shared error/exit-code layer (error-system v2). agenttools_errors is itself stdlib-only, so
# a top-level import keeps this module's "stdlib-first at import" invariant — `--help` stays
# fast/offline.
from agenttools_errors import (  # noqa: E402  (after the sys.path bootstrap above)
    EXIT_OK,
    AgentToolError,
    ConfigError,
    MissingDepError,
    guard,
)

# ── paths ──────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_CONTRACTS = _HERE.parent / "contracts"
MANIFEST_PATH = _CONTRACTS / "models.yaml"
SCHEMA_PATH = _CONTRACTS / "models.schema.json"
REPORTS_DIR = _HERE / "reports"
# The agent-tools checkout root (lib/checker/.. -> lib/.. -> repo root). `gh` runs here.
REPO_ROOT = _HERE.parent.parent

# The capability set the manifest may use (mirrors models.schema.json `capability` enum —
# kept in sync by the schema-conformance test).
KNOWN_CAPABILITIES = ("vision", "code", "reasoning", "tools", "embeddings", "audio")

# Sentinel woven into a proposal PR title/branch so a re-run can find an existing one and
# stay idempotent (no duplicate PRs for the same bump).
PROPOSAL_MARKER = "model-freshness"


# ── key resolution (harvest from existing CLI/env config — never hardcode) ──────────────
# The same `.env` fallback files review-cli reads, so a key configured once for review is
# reused here. Env vars always win; files are a fallback. NEVER hardcode a key.
def _fallback_files() -> tuple[Path, ...]:
    """The .env fallback files to search, computed LAZILY (so a test's HOME monkeypatch
    takes effect, and ``MODEL_FRESHNESS_ENV_FILE`` can override for ad-hoc use)."""
    override = os.environ.get("MODEL_FRESHNESS_ENV_FILE")
    if override:
        return (Path(override),)
    return (
        Path.home() / ".config" / "review-cli" / ".env",
        Path.home() / ".config" / "rig" / ".env",
    )


def _read_env_key(env_file: Path, var: str) -> str | None:
    """Read VAR=value from a flat .env file (quotes stripped). None on miss/unreadable."""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{var}="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def resolve_key(env_names: tuple[str, ...]) -> str | None:
    """Resolve a provider key from the env (in order), then the .env fallback files.

    Key-name-first precedence (mirrors review-cli): an env var beats every file, and among
    the files the first accepted name wins regardless of which file it lives in. Returns
    None when no name resolves anywhere — the caller SKIPS that provider, never crashes.
    """
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    files = _fallback_files()
    for name in env_names:
        for path in files:
            key = _read_env_key(path, name)
            if key:
                return key
    return None


# ── version parsing / comparison ────────────────────────────────────────────────────────
# A "version" is the numeric run extracted from a model id within the SAME family. We never
# compare across families (gpt-5.5 vs gemini-2.5 is meaningless); the checker only asks
# "is there a NEWER version of the family this provider's pin belongs to?".
#
# Endpoints commonly return DATED / build-suffixed snapshots: Anthropic `/v1/models` gives
# `claude-opus-4-8-20260101`, Gemini `gemini-2.5-flash-002`, OpenAI `gpt-5.5-2026-01-01`.
# A naive "strip all digit runs" makes those a DIFFERENT family than the bare pin, so no bump
# would ever be proposed for those providers (the checker's whole point). So we FIRST strip a
# trailing date/build suffix, THEN derive the family skeleton + the comparable version tuple.
# A version is a run of digits joined by `.` OR `-`: OpenAI/z.ai write `gpt-6.1`, `glm-5.3`,
# Anthropic writes the same thing as `claude-opus-5-5` (Opus 5.5) and `claude-fable-5-1`
# (Fable 5.1). Treating the hyphen form as one version keeps `claude-fable-5` ->
# `claude-fable-5-1` and `claude-opus-4-8` -> `claude-opus-5` in ONE family; with plain
# `\d+` runs they had different segment counts and no Claude point release was ever proposed.
# A hyphen joins only a WHOLE numeric segment: in `Qwen3.8-27B` / `Qwen3.8-1.7B` / `k3-256k`
# the `27B` / `1.7B` / `256k` is a size, not a minor version, so the version stays `3.8` / `3`.
# (The lookahead also refuses `.` so `-1.7B` cannot backtrack to a partial `-1`.)
_VERSION_PATTERN = r"\d+(?:\.\d+)*(?:-\d+(?:\.\d+)*(?![A-Za-z0-9.]))*"
_VERSION_RE = re.compile(_VERSION_PATTERN)
# A trailing snapshot/date/build suffix to ignore when deriving the family + version:
#   -YYYY-MM-DD, or a 3-8 digit run (-YYYYMMDD, -YYMMDD, a 3-4 digit build like Gemini's
#   -002), -preview / -latest / -exp markers. Anchored at end; only the FIRST match is
#   stripped. Every length up to 8 matters: with hyphen-joined versions, an unstripped
#   `claude-opus-4-8-250902` would read as version (4, 8, 250902) and outrank its own pin.
_SNAPSHOT_SUFFIX_RE = re.compile(
    r"(?:-(?:\d{4}-\d{2}-\d{2}|\d{3,8})|-(?:preview|latest|exp|beta|alpha)(?:-[0-9a-z]+)?)$",
    re.IGNORECASE,
)


def strip_snapshot_suffix(model_id: str) -> str:
    """Drop a trailing dated/build/preview snapshot suffix from an id.

    `claude-opus-4-8-20260101` -> `claude-opus-4-8`, `gemini-2.5-flash-002` -> `gemini-2.5-flash`,
    `gpt-5.5-2026-01-01` -> `gpt-5.5`, `gpt-5.6-preview` -> `gpt-5.6`. Applied once; an id with
    no such suffix is returned unchanged. This is what lets a dated endpoint snapshot match the
    bare pin's family and so be considered as a bump.
    """
    return _SNAPSHOT_SUFFIX_RE.sub("", model_id)


def model_family(model_id: str) -> str:
    """The family skeleton of a model id: snapshot-suffix stripped, then every numeric run
    replaced by a single ``#`` placeholder.

    `gpt-5.5` -> `gpt-#`, `gpt-5.6` -> `gpt-#`, `claude-opus-4-8` -> `claude-opus-#`,
    `claude-opus-4-8-20260101` -> `claude-opus-#`, `gemini-2.5-flash-002` -> `gemini-#-flash`,
    `moonshotai/Kimi-K2.7-Code` -> `moonshotai/Kimi-K#-Code`. Two ids share a family iff this
    skeleton matches — so only same-family ids are ever version-compared, and a dated snapshot
    matches its bare pin. A hyphen-joined version is one run, so `claude-opus-4-8`,
    `claude-opus-5` and `claude-opus-5-5` share the family `claude-opus-#`. A different
    VARIANT suffix (`gpt-5.5` vs `gpt-6.1-sol`, `Kimi-K2.7-Code` vs `Kimi-K3`) is a different
    family and never auto-proposed; `newer_variants` surfaces those for a human instead.
    """
    return _VERSION_RE.sub("#", strip_snapshot_suffix(model_id))


def version_tuple(model_id: str) -> tuple[int, ...]:
    """The comparable version tuple of an id: snapshot-suffix stripped, then ALL numeric runs
    in order.

    `gpt-5.5` -> (5, 5); `claude-opus-4-8` -> (4, 8); `claude-opus-4-8-20260101` -> (4, 8)
    (the date suffix is dropped first, so a re-dated SAME version doesn't read as newer);
    `gemini-2.5-flash-002` -> (2, 5). An id with no digits sorts lowest (`()`). Stays
    consistent with `model_family` (both strip the snapshot suffix first), so within a family
    this tuple orders the ids. Comparison across families is meaningless and guarded by
    `is_newer`.
    """
    base = strip_snapshot_suffix(model_id)
    return tuple(int(run) for run in re.findall(r"\d+", base))


def is_newer(candidate: str, current: str) -> bool:
    """True iff `candidate` is the SAME family as `current` but a strictly higher version.

    Cross-family ids (different skeletons) are never "newer" — that prevents proposing
    `gemini-2.5` as a bump for `gpt-5.5`, and prevents a different VARIANT (`gpt-6.1-sol` for
    `gpt-5.5`) from being auto-proposed (a human picks it). A dated snapshot of the SAME version
    (`gpt-5.5-20260101` vs pin `gpt-5.5`) is NOT newer (same version tuple after stripping the
    date) — so re-dated republishes of the current model don't churn proposals. A genuinely
    newer same-family release (`gpt-5.6`, `gpt-5.6-20260201`) IS newer.
    """
    if model_family(candidate) != model_family(current):
        return False
    return version_tuple(candidate) > version_tuple(current)


def newest_same_family(current: str, model_ids: list[str]) -> str | None:
    """The newest id in `model_ids` that `is_newer` than `current`, else None.

    On a version tie the bare id beats a dated snapshot (`Qwen3.8-Max` over
    `Qwen3.8-Max-0902`): the bare id is the one that keeps tracking the release.
    """
    newer = [mid for mid in model_ids if is_newer(mid, current)]
    if not newer:
        return None
    return max(newer, key=lambda mid: (version_tuple(mid), mid == strip_snapshot_suffix(mid)))


_LINE_RE = re.compile(rf"^(?P<line>.*?)(?P<version>{_VERSION_PATTERN})")


def _line_and_version(model_id: str) -> tuple[str, tuple[int, ...]] | None:
    """(text before the first version, that version) — `gpt-6.1-sol` -> ('gpt-', (6, 1))."""
    match = _LINE_RE.match(strip_snapshot_suffix(model_id))
    if match is None:
        return None
    return match["line"], tuple(int(run) for run in re.findall(r"\d+", match["version"]))


def newer_variants(current: str, model_ids: list[str], limit: int = 5) -> list[str]:
    """Newer ids of the same model LINE but a different variant — never auto-proposed.

    `gpt-5.6-terra` has no same-family successor, yet `gpt-6-luna` / `gpt-6.1-sol` exist;
    `moonshotai/Kimi-K2.7-Code` is followed by `moonshotai/Kimi-K3`. Which variant replaces
    the pin is a human choice (cost tier, vision, code-specialisation), so these are listed
    next to the pin, newest first, instead of being proposed.
    """
    current_lv = _line_and_version(current)
    if current_lv is None:
        return []
    family = model_family(current)
    found: list[tuple[tuple[int, ...], str]] = []
    for mid in model_ids:
        lv = _line_and_version(mid)
        if lv is None or model_family(mid) == family or lv[0] != current_lv[0]:
            continue
        if lv[1] > current_lv[1] and all(mid != seen for _, seen in found):
            found.append((lv[1], mid))
    found.sort(key=lambda item: item[0], reverse=True)
    return [mid for _, mid in found[:limit]]


# ── manifest model ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ModelEntry:
    id: str
    provider: str
    capabilities: tuple[str, ...]
    context: int | None = None
    notes: str = ""


@dataclass
class Manifest:
    version: int
    models: list[ModelEntry]
    roles: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    def by_provider(self, provider: str) -> list[ModelEntry]:
        return [m for m in self.models if m.provider == provider]

    def providers(self) -> list[str]:
        seen: list[str] = []
        for m in self.models:
            if m.provider not in seen:
                seen.append(m.provider)
        return seen

    def entry(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)


class ManifestError(ValueError):
    """The manifest is malformed or violates a cross-reference invariant."""


class MissingYamlError(ManifestError):
    """PyYAML — the checker's one runtime dep — is not installed.

    A *subclass* of ManifestError so existing ``except ManifestError`` (in ``run()`` and the
    tests) still catches it, but a distinct type so ``main()`` can map it to the
    missing-dependency exit code (127) + an install hint instead of a config error (2).
    """


def load_manifest(path: Path = MANIFEST_PATH) -> Manifest:
    """Parse + lightly-structure models.yaml. Lazy yaml import (manifest is the only YAML).

    Raises ManifestError on a non-mapping / missing-required shape — fail loud, since a
    silently-empty manifest would make the checker "find" everything as new.
    """
    try:
        import yaml  # lazy: keeps the module stdlib-only at IMPORT time
    except ImportError as exc:
        # PyYAML is the checker's one runtime dep. The cron runs `python3 model_freshness.py`,
        # so a machine without it must get a clear, actionable error — not a raw traceback.
        # MissingYamlError (a ManifestError subclass) lets main() exit 127 with an install hint.
        raise MissingYamlError(
            "PyYAML is required to read the manifest but is not installed. "
            "Install it: `python3 -m pip install --user pyyaml` (or via your package "
            "manager). rig provisions it as a dependency (`rig doctor`)."
        ) from exc

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {path} must be a mapping")
    models_raw = raw.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ManifestError("manifest `models:` must be a non-empty list")
    models: list[ModelEntry] = []
    for entry in models_raw:
        if not isinstance(entry, dict):
            raise ManifestError(f"model entry not a mapping: {entry!r}")
        mid = entry.get("id")
        provider = entry.get("provider")
        caps = entry.get("capabilities")
        if not isinstance(mid, str) or not mid.strip():
            raise ManifestError(f"model entry missing 'id': {entry!r}")
        if not isinstance(provider, str) or not provider.strip():
            raise ManifestError(f"model {mid!r} missing 'provider'")
        if not isinstance(caps, list) or not caps:
            raise ManifestError(f"model {mid!r} missing 'capabilities'")
        ctx = entry.get("context")
        models.append(
            ModelEntry(
                id=mid.strip(),
                provider=provider.strip(),
                capabilities=tuple(str(c) for c in caps),
                # `isinstance(True, int)` is True, so guard against `context: true` silently
                # becoming 1 — only a real (non-bool) int is a context window.
                context=int(ctx) if isinstance(ctx, int) and not isinstance(ctx, bool) else None,
                notes=str(entry.get("notes", "")),
            )
        )
    roles = {str(k): str(v) for k, v in (raw.get("roles") or {}).items()}
    aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}
    return Manifest(
        version=int(raw.get("version", 1)),
        models=models,
        roles=roles,
        aliases=aliases,
    )


def validate_manifest(manifest: Manifest) -> list[str]:
    """Return a list of human-readable problems ([] == valid).

    Enforces the cross-references JSON-Schema cannot:
      - every role/alias target is a concrete id present in `models:`;
      - the `vision` role/alias resolves ONLY to a vision-capable entry (#3681);
      - capabilities are drawn from the known set;
      - `<provider>:latest` points at an entry of THAT provider.
    """
    problems: list[str] = []
    ids = {m.id for m in manifest.models}
    for m in manifest.models:
        unknown = set(m.capabilities) - set(KNOWN_CAPABILITIES)
        if unknown:
            problems.append(f"model {m.id!r}: unknown capabilities {sorted(unknown)}")

    def _check_pointer(label: str, key: str, target: str) -> None:
        if target not in ids:
            problems.append(f"{label} {key!r} -> {target!r} which is not a concrete model id")
            return
        if key == "vision" or key.endswith(":vision"):
            entry = manifest.entry(target)
            if entry and "vision" not in entry.capabilities:
                problems.append(
                    f"{label} 'vision' -> {target!r} but that model is NOT vision-capable "
                    f"(capabilities: {list(entry.capabilities)}) — violates #3681"
                )
        if key.endswith(":latest"):
            provider = key.split(":", 1)[0]
            entry = manifest.entry(target)
            if entry and entry.provider != provider:
                problems.append(
                    f"{label} {key!r} -> {target!r} but that model's provider is "
                    f"{entry.provider!r}, not {provider!r}"
                )

    for key, target in manifest.roles.items():
        _check_pointer("role", key, target)
    for key, target in manifest.aliases.items():
        _check_pointer("alias", key, target)
    return problems


# ── provider polling (OpenAI-compatible /models, Anthropic, Google) ─────────────────────
@dataclass
class PollResult:
    provider: str
    ok: bool
    model_ids: list[str] = field(default_factory=list)
    skipped: str = ""  # reason, when ok is False and it's a skip (no key) not an error
    error: str = ""
    # Where the ids came from: "api" (the keyed endpoint) and/or harness catalog names
    # ("omp", "codex", "claude", "models.dev"). Empty for a failed poll.
    sources: list[str] = field(default_factory=list)


class _NoCrossOriginAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Strip Authorization when a redirect crosses to a different origin.

    urllib's default redirect handler re-sends every original header to the redirect
    target — a provider key (or the harvested omp OAuth token) would leak to any host the
    endpoint 30x's to. Same-origin redirects keep the header; cross-origin drops it (the
    request then fails auth honestly instead of exfiltrating the credential).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        from urllib.parse import urlsplit  # lazy: only needed on an actual redirect

        old, new_origin = urlsplit(req.full_url), urlsplit(new.full_url)
        if (old.scheme, old.hostname, old.port) != (
            new_origin.scheme,
            new_origin.hostname,
            new_origin.port,
        ):
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


# Cloudflare-fronted hosts (models.dev, api.commandcode.ai) answer urllib's default
# `Python-urllib/x.y` User-Agent with HTTP 403, so every request names this checker.
USER_AGENT = "agent-tools-model-freshness (+https://git.hyperide.ai/ultrabricks/agent-tools)"


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers}, method="GET")
    opener = urllib.request.build_opener(_NoCrossOriginAuthRedirect())
    with opener.open(req, timeout=timeout) as resp:  # noqa: S310 — fixed https hosts
        body = resp.read().decode("utf-8", "replace")
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


def _ids_from_openai_list(data: dict[str, Any]) -> list[str]:
    """OpenAI-compatible /models returns {data: [{id: ...}, ...]}."""
    out: list[str] = []
    for item in data.get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            out.append(item["id"])
    return out


def _ids_from_gemini_list(data: dict[str, Any]) -> list[str]:
    """Google generativelanguage returns {models: [{name: 'models/gemini-...'}, ...]}."""
    out: list[str] = []
    for item in data.get("models", []) or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item["name"].split("/")[-1])
    return out


# Per-provider polling config: env key names + how to build the request + parse the ids.
# A provider absent here has no model-list endpoint (or we don't poll it) — it's skipped.
def _poll_openai(timeout: float) -> PollResult:
    key = resolve_key(("OPENAI_API_KEY",))
    if not key:
        return PollResult("openai", False, skipped="no OPENAI_API_KEY")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
    try:
        data = _http_get_json(f"{base}/v1/models", {"Authorization": f"Bearer {key}"}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("openai", False, error=str(exc))
    return PollResult("openai", True, model_ids=_ids_from_openai_list(data))


def _poll_anthropic(timeout: float) -> PollResult:
    key = resolve_key(("ANTHROPIC_API_KEY",))
    if not key:
        return PollResult("anthropic", False, skipped="no ANTHROPIC_API_KEY")
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        data = _http_get_json(f"{base}/v1/models", headers, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("anthropic", False, error=str(exc))
    return PollResult("anthropic", True, model_ids=_ids_from_openai_list(data))


def _poll_gemini(timeout: float) -> PollResult:
    key = resolve_key(("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    if not key:
        return PollResult("gemini", False, skipped="no GEMINI_API_KEY")
    base = os.environ.get(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    try:
        # Send the key as a HEADER (x-goog-api-key), not a `?key=` query param — a URL with
        # the key embedded can leak into an error string / log. Google accepts both.
        data = _http_get_json(f"{base}/v1beta/models", {"x-goog-api-key": key}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("gemini", False, error=str(exc))
    return PollResult("gemini", True, model_ids=_ids_from_gemini_list(data))


def _poll_commandcode(timeout: float) -> PollResult:
    key = resolve_key(("COMMANDCODE_API_KEY",))
    if not key:
        return PollResult("commandcode", False, skipped="no COMMANDCODE_API_KEY")
    base = os.environ.get(
        "COMMANDCODE_BASE_URL", "https://api.commandcode.ai/provider/v1"
    ).rstrip("/")
    try:
        data = _http_get_json(f"{base}/models", {"Authorization": f"Bearer {key}"}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("commandcode", False, error=str(exc))
    return PollResult("commandcode", True, model_ids=_ids_from_openai_list(data))


def _poll_zai(timeout: float) -> PollResult:
    key = resolve_key(("ZAI_API_KEY", "ZHIPU_API_KEY"))
    if not key:
        return PollResult("zai", False, skipped="no ZAI_API_KEY")
    base = os.environ.get(
        "ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4"
    ).rstrip("/")
    try:
        data = _http_get_json(f"{base}/models", {"Authorization": f"Bearer {key}"}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("zai", False, error=str(exc))
    return PollResult("zai", True, model_ids=_ids_from_openai_list(data))


def _omp_kimi_code_oauth_token() -> str | None:
    """Best-effort: a fresh OAuth access token for kimi-code from the local omp install.

    The Kimi-for-Coding endpoint is OAuth-backed, so on a machine whose only Kimi
    credential is the omp login the poller can still run instead of skipping. Primary path
    is `omp token kimi-code` — omp's own resolver, which handles refresh and profiles.
    Fallback is a READ-ONLY peek at omp's agent.db for when the CLI can't run (e.g. cron
    PATH). Any failure returns None and the caller falls through to the usual skip. Never
    raises, never refreshes by itself — an expired stored token surfaces as the endpoint's
    401 error.
    """
    return _omp_token_cli() or _omp_token_agent_db()


def _omp_token_cli() -> str | None:
    """`omp token kimi-code` — the canonical, refresh-aware resolver. None on any failure."""
    try:
        res = subprocess.run(
            ["omp", "token", "kimi-code"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    token = res.stdout.strip()
    return token or None


def _omp_agent_db() -> Path:
    """omp's agent.db location, honoring the agent-dir env vars omp itself reads."""
    for var in ("OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR"):
        override = os.environ.get(var, "").strip()
        if override:
            return Path(override).expanduser() / "agent.db"
    return Path.home() / ".omp" / "agent" / "agent.db"


def _omp_token_agent_db() -> str | None:
    """READ-ONLY peek at omp's agent.db (fallback when the omp CLI can't run).

    Reads the newest enabled kimi-code oauth row; any failure (missing db, schema drift,
    lock, malformed row) returns None. Never raises.
    """
    import sqlite3  # lazy: stdlib, but only this one resolver needs it

    db_path = _omp_agent_db()
    try:
        if not db_path.is_file():
            return None
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            row = db.execute(
                "SELECT data FROM auth_credentials"
                " WHERE provider = 'kimi-code' AND credential_type = 'oauth'"
                " AND disabled_cause IS NULL"
                " ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        finally:
            db.close()
        if not row:
            return None
        data = json.loads(row[0])
        if not isinstance(data, dict):
            return None
        token = data.get("access")
        return token.strip() if isinstance(token, str) and token.strip() else None
    except (OSError, sqlite3.Error, ValueError):
        return None


# The ONLY origin the harvested omp OAuth token may be sent to. A custom
# KIMI_CODE_BASE_URL requires an explicit KIMI_API_KEY — otherwise one env var could
# redirect a credential the user never handed this script to an arbitrary host.
_KIMI_CODE_CANONICAL_BASE = "https://api.kimi.com/coding/v1"


def _poll_kimi_code(timeout: float) -> PollResult:
    key = resolve_key(("KIMI_API_KEY",))
    from_oauth = key is None
    if from_oauth:
        key = _omp_kimi_code_oauth_token()
    if not key:
        return PollResult(
            "kimi-code", False, skipped="no KIMI_API_KEY or omp kimi-code OAuth login"
        )
    # KIMI_CODE_BASE_URL is the OAuth-managed coding endpoint override; KIMI_BASE_URL is a
    # DIFFERENT var (direct-API moonshot.ai) and must not redirect this poller.
    base = os.environ.get("KIMI_CODE_BASE_URL", _KIMI_CODE_CANONICAL_BASE).rstrip("/")
    if from_oauth and base != _KIMI_CODE_CANONICAL_BASE:
        return PollResult(
            "kimi-code",
            False,
            skipped="custom KIMI_CODE_BASE_URL requires an explicit KIMI_API_KEY"
            " (the omp OAuth token is only sent to the canonical endpoint)",
        )
    try:
        data = _http_get_json(f"{base}/models", {"Authorization": f"Bearer {key}"}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("kimi-code", False, error=str(exc))
    return PollResult("kimi-code", True, model_ids=_ids_from_openai_list(data))


# fireworks has no stable public model-list contract here; it routes through commandcode in
# practice, so we don't poll it directly (skipped with a clear reason).
def _poll_fireworks(timeout: float) -> PollResult:
    return PollResult("fireworks", False, skipped="no direct model-list endpoint (routed)")


POLLERS: dict[str, Callable[[float], PollResult]] = {
    "openai": _poll_openai,
    "anthropic": _poll_anthropic,
    "gemini": _poll_gemini,
    "commandcode": _poll_commandcode,
    "zai": _poll_zai,
    "kimi-code": _poll_kimi_code,
    "fireworks": _poll_fireworks,
}


# ── keyless harness catalogs (#934) ─────────────────────────────────────────────────────
# Every login on an OAuth-only machine (Claude Code, Codex/ChatGPT, omp) is a subscription,
# so the keyed pollers above skip anthropic/openai there. The harnesses already cache the
# catalog their account can use; reading those needs no key. models.dev is the keyless
# network fallback for a provider no local harness lists.
#
# A catalog names providers its own way; this maps those names onto the manifest/seat
# provider they describe. Unmapped names pass through (xai, opencode-go, … are only used by
# the review-cli seat scan).
_CATALOG_PROVIDER_NAMES = {
    "openai-codex": "openai",
    "google": "gemini",
    "google-gemini-cli": "gemini",
    "zai-coding-plan": "zai",
    "kimi-code-plan-global": "kimi-code",
    "fireworks-ai": "fireworks",
}
MODELS_DEV_URL = "https://models.dev/api.json"


class CatalogUnavailable(Exception):
    """A harness catalog is not present on this machine (or could not be read)."""


@dataclass
class HarnessCatalog:
    name: str
    ids: dict[str, list[str]] = field(default_factory=dict)  # provider -> model ids
    error: str = ""


# Catalog ids reach the report, the --json output and the review-cli patch; anything outside
# this shape (markup, whitespace, quotes) is dropped rather than echoed.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
# A harness cache older than this no longer says what is current: it is reported unavailable
# so the provider falls through to models.dev instead of pinning the run to an old list.
CACHE_MAX_AGE = _dt.timedelta(days=7)


def _group_by_provider(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for provider, model_id in pairs:
        if not _MODEL_ID_RE.match(model_id):
            continue
        bucket = grouped.setdefault(_CATALOG_PROVIDER_NAMES.get(provider, provider), [])
        if model_id not in bucket:
            bucket.append(model_id)
    return grouped


def _check_cache_age(path: Path, fetched_at: _dt.datetime | None) -> None:
    """Raise CatalogUnavailable when a cache's own fetch time is older than CACHE_MAX_AGE.

    A cache without a readable fetch time is accepted: its age is unknown, not known-old.
    """
    if fetched_at is None:
        return
    age = _dt.datetime.now(_dt.timezone.utc) - fetched_at
    if age > CACHE_MAX_AGE:
        raise CatalogUnavailable(f"{path} was fetched {age.days} days ago (stale)")


def _parse_rfc3339(value: Any) -> _dt.datetime | None:
    """`2026-10-16T23:48:41.452593879Z` -> aware UTC datetime (seconds precision).

    None for anything unreadable, including impossible dates: an unreadable timestamp means
    an unknown age, never a crash of the whole run.
    """
    stamp = re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value) if isinstance(value, str) else None
    try:
        return _dt.datetime.strptime(stamp.group(0), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt.timezone.utc
        ) if stamp else None
    except ValueError:
        return None


def _from_epoch_ms(value: Any) -> _dt.datetime | None:
    """Epoch milliseconds -> aware UTC datetime; None for a non-number or out-of-range value."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return _dt.datetime.fromtimestamp(value / 1000, _dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogUnavailable(f"{path} not found") from exc
    except (OSError, ValueError) as exc:
        raise CatalogUnavailable(f"cannot read {path}: {exc}") from exc


def _omp_catalog(timeout: float) -> dict[str, list[str]]:
    """`omp models --json`: every model of every provider omp is logged into."""
    try:
        res = subprocess.run(
            ["omp", "models", "--json"], capture_output=True, text=True, timeout=max(timeout, 60)
        )
    except FileNotFoundError as exc:
        raise CatalogUnavailable("omp is not installed") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CatalogUnavailable(f"omp models failed: {exc}") from exc
    if res.returncode != 0:
        raise CatalogUnavailable(f"omp models exited {res.returncode}: {res.stderr.strip()[:200]}")
    try:
        data = json.loads(res.stdout)
    except ValueError as exc:
        raise CatalogUnavailable(f"omp models printed invalid JSON: {exc}") from exc
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise CatalogUnavailable("omp models --json has no `models` list")
    return _group_by_provider([
        (m["provider"], m["id"])
        for m in models
        if isinstance(m, dict) and isinstance(m.get("provider"), str) and isinstance(m.get("id"), str)
    ])


def _codex_catalog(timeout: float) -> dict[str, list[str]]:
    """`$CODEX_HOME/models_cache.json`: the codex CLI's per-account model catalog."""
    home = os.environ.get("CODEX_HOME", "").strip()
    path = (Path(home).expanduser() if home else Path.home() / ".codex") / "models_cache.json"
    data = _read_json_file(path)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise CatalogUnavailable(f"{path} has no `models` list")
    _check_cache_age(path, _parse_rfc3339(data.get("fetched_at")))
    return _group_by_provider([
        ("openai", m["slug"]) for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)
    ])


def _claude_catalog(timeout: float) -> dict[str, list[str]]:
    """Claude Code's model-catalog cache: the account's models plus the CLI alias table.

    `<config dir>/cache/model-catalog/*.json` holds `catalog.config.models[].id` and
    `provider_alias_targets.<alias>.default` — what `claude --model fable|opus|sonnet`
    resolves to for a first-party login.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    files = sorted((root / "cache" / "model-catalog").glob("*.json"))
    if not files:
        raise CatalogUnavailable(f"no Claude Code model catalog under {root / 'cache' / 'model-catalog'}")
    pairs: list[tuple[str, str]] = []
    problems: list[str] = []
    for path in files:
        # One unreadable or stale file (a crashed half-write, an old org's cache) must not
        # hide the good ones next to it.
        try:
            data = _read_json_file(path)
            fetched_ms = data.get("fetchedAt") if isinstance(data, dict) else None
            _check_cache_age(path, _from_epoch_ms(fetched_ms))
        except CatalogUnavailable as exc:
            problems.append(str(exc))
            continue
        catalog = data.get("catalog") if isinstance(data, dict) else None
        config = catalog.get("config") if isinstance(catalog, dict) else None
        if not isinstance(config, dict):
            continue
        models = config.get("models")
        for model in models if isinstance(models, list) else ():
            if isinstance(model, dict) and isinstance(model.get("id"), str):
                pairs.append(("anthropic", model["id"]))
        targets = config.get("provider_alias_targets")
        for target in targets.values() if isinstance(targets, dict) else ():
            if isinstance(target, dict) and isinstance(target.get("default"), str):
                pairs.append(("anthropic", target["default"]))
    if not pairs:
        detail = f" ({'; '.join(problems)})" if problems else ""
        raise CatalogUnavailable(f"Claude Code model catalog under {root} lists no models{detail}")
    return _group_by_provider(pairs)


def _models_dev_catalog(timeout: float) -> dict[str, list[str]]:
    """https://models.dev/api.json — keyless, `{provider: {models: {id: {...}}}}`."""
    try:
        data = _http_get_json(os.environ.get("MODELS_DEV_URL", MODELS_DEV_URL), {}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CatalogUnavailable(f"models.dev unreachable: {exc}") from exc
    pairs: list[tuple[str, str]] = []
    for provider, entry in data.items():
        models = entry.get("models") if isinstance(entry, dict) else None
        if isinstance(models, dict):
            pairs.extend((provider, model_id) for model_id in models)
    if not pairs:
        raise CatalogUnavailable("models.dev returned no models")
    return _group_by_provider(pairs)


# Local catalogs: read on every run. FALLBACK catalogs: fetched only when no local source
# (and no keyed endpoint) covers a provider that needs checking.
HARNESS_SOURCES: dict[str, Callable[[float], dict[str, list[str]]]] = {
    "omp": _omp_catalog,
    "codex": _codex_catalog,
    "claude": _claude_catalog,
}
FALLBACK_SOURCES: dict[str, Callable[[float], dict[str, list[str]]]] = {
    "models.dev": _models_dev_catalog,
}


def load_catalogs(
    sources: dict[str, Callable[[float], dict[str, list[str]]]], timeout: float
) -> list[HarnessCatalog]:
    """Read each catalog source; an unavailable one is recorded with its reason, never raised."""
    catalogs: list[HarnessCatalog] = []
    for name, source in sources.items():
        try:
            catalogs.append(HarnessCatalog(name, ids=source(timeout)))
        except CatalogUnavailable as exc:
            catalogs.append(HarnessCatalog(name, error=str(exc)))
    return catalogs


def merge_poll(api: PollResult, catalogs: list[HarnessCatalog]) -> PollResult:
    """Union a keyed poll with every catalog that lists its provider.

    The result is ok when ANY source listed ids; it is the unchanged keyed result (its skip or
    error reason intact) when none did. A keyed endpoint that answered with an empty list
    counts as no source, so the catalogs (and the fallback) still get their turn.
    """
    ids = list(api.model_ids) if api.ok else []
    sources = ["api"] if ids else []
    for catalog in catalogs:
        listed = catalog.ids.get(api.provider)
        if listed:
            ids.extend(mid for mid in listed if mid not in ids)
            sources.append(catalog.name)
    if not sources:
        return api
    return PollResult(api.provider, True, model_ids=ids, sources=sources)


class CatalogSet:
    """The local catalogs plus the fallback ones, fetched at most once and only on demand."""

    def __init__(
        self,
        local: list[HarnessCatalog],
        fallback_sources: dict[str, Callable[[float], dict[str, list[str]]]],
        timeout: float,
    ) -> None:
        self.local = local
        self._fallback_sources = fallback_sources
        self._timeout = timeout
        self._fallback: list[HarnessCatalog] | None = None

    def fallback(self) -> list[HarnessCatalog]:
        if self._fallback is None:
            self._fallback = load_catalogs(self._fallback_sources, self._timeout)
        return self._fallback

    def poll(self, api: PollResult) -> PollResult:
        merged = merge_poll(api, self.local)
        return merged if merged.model_ids else merge_poll(api, self.fallback())

    def all(self) -> list[HarnessCatalog]:
        return self.local + (self._fallback or [])


# ── proposal computation ────────────────────────────────────────────────────────────────
@dataclass
class Proposal:
    provider: str
    current_id: str
    new_id: str
    # capabilities we can INFER for the new id: by default the same as the current pin's
    # (the endpoint rarely advertises capability flags). The human verifies — this is why
    # bumps are proposed, never auto-merged: a "turbo" variant can silently drop vision.
    inferred_capabilities: tuple[str, ...]
    note: str = ""

    @property
    def slug(self) -> str:
        """A filesystem/branch-safe slug unique per (provider, new id) — drives idempotency."""
        raw = f"{self.provider}-{self.new_id}"
        return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-").lower()


def compute_proposals(manifest: Manifest, polls: dict[str, PollResult]) -> list[Proposal]:
    """For each provider's current pins, find the newest same-family id the live endpoint
    advertises that is newer than the pin. One proposal per (provider, current pin) that has
    a strictly-newer live sibling.
    """
    proposals: list[Proposal] = []
    for provider in manifest.providers():
        poll = polls.get(provider)
        if poll is None or not poll.ok:
            continue
        for pin in manifest.by_provider(provider):
            best = newest_same_family(pin.id, poll.model_ids)
            if best is None:
                continue
            proposals.append(
                Proposal(
                    provider=provider,
                    current_id=pin.id,
                    new_id=best,
                    inferred_capabilities=pin.capabilities,
                    note=(
                        "capabilities copied from the current pin and NOT verified against the "
                        "new model — confirm vision/context before merging (a point/turbo "
                        "release can regress)."
                    ),
                )
            )
    return proposals


def pin_variants(manifest: Manifest, polls: dict[str, PollResult]) -> dict[str, list[str]]:
    """pin id -> newer VARIANTS of its line, for every pin with no same-family successor.

    `gpt-5.5` has no `gpt-5.6`/`gpt-6` but `gpt-6.1-sol`, `gpt-6-luna` exist: a stale pin that
    `compute_proposals` must not auto-bump (a human picks the variant), but must not hide
    either. Reported next to the proposals, never opened as a PR.
    """
    found: dict[str, list[str]] = {}
    for provider in manifest.providers():
        poll = polls.get(provider)
        if poll is None or not poll.ok:
            continue
        for pin in manifest.by_provider(provider):
            if newest_same_family(pin.id, poll.model_ids) is None:
                variants = newer_variants(pin.id, poll.model_ids)
                if variants:
                    found[pin.id] = variants
    return found



# ── review-cli seats (#934) ─────────────────────────────────────────────────────────────
# review-cli does not read models.yaml for its default board, so its seats are checked on
# their own: the code defaults in <checkout>/reviewlib/config.py and the user's
# ~/.config/review-cli/config.yaml. A seat is `<transport>:<model>`; router transports
# (oc/opencode/omp) carry `<provider>/<model>`.
_SEAT_TRANSPORTS = {
    "claude": "anthropic",
    "codex": "openai",
    "commandcode": "commandcode",
    "zai": "zai",
    "gemini": "gemini",
}
_ROUTER_TRANSPORTS = ("oc", "opencode", "omp")
# The model part uses the catalog id alphabet (`:free`, `@latest`, `+x` suffixes included) and
# must end at a value boundary, so a YAML scan never truncates `vendor/m-1:free` to `vendor/m-1`.
_SEAT_RE = re.compile(
    r"(?<![\w/.:-])(?P<transport>%s):(?P<model>[A-Za-z0-9](?:[A-Za-z0-9._/:@+-]*[A-Za-z0-9])?)"
    r"(?=$|[\s\"',}\]])"
    % "|".join((*_SEAT_TRANSPORTS, *_ROUTER_TRANSPORTS))
)
REVIEW_CLI_CONFIG = Path.home() / ".config" / "review-cli" / "config.yaml"


@dataclass(frozen=True)
class ReviewCliFiles:
    """The two places review-cli pins models. Either may be None (not found)."""

    code_defaults: Path | None  # <checkout>/reviewlib/config.py
    user_config: Path | None  # ~/.config/review-cli/config.yaml


@dataclass
class SeatPin:
    path: Path
    line: int
    seat: str
    provider: str
    model: str
    newer: str | None = None
    variants: list[str] = field(default_factory=list)

    @property
    def new_seat(self) -> str | None:
        return self.seat[: -len(self.model)] + self.newer if self.newer else None


@dataclass
class ReviewCliReport:
    files: ReviewCliFiles
    stale: list[SeatPin] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)  # seats whose provider no catalog lists


def find_review_cli_files(root: Path | None = None) -> ReviewCliFiles:
    """Locate review-cli's code defaults and user config.

    The checkout is `root` when given, else the one the installed `review` resolves into
    (the symlink install points into the checkout), else an importable `reviewlib`.
    """
    import shutil

    code: Path | None = None
    if root is not None:
        candidate = root.expanduser() / "reviewlib" / "config.py"
        code = candidate if candidate.is_file() else None
    else:
        exe = shutil.which("review")
        for parent in Path(exe).resolve().parents if exe else ():
            if (parent / "reviewlib" / "config.py").is_file():
                code = parent / "reviewlib" / "config.py"
                break
        if code is None:
            import importlib.util

            spec = importlib.util.find_spec("reviewlib")
            origin = Path(spec.origin).parent / "config.py" if spec and spec.origin else None
            code = origin if origin and origin.is_file() else None
    user = REVIEW_CLI_CONFIG if REVIEW_CLI_CONFIG.is_file() else None
    return ReviewCliFiles(code_defaults=code, user_config=user)


def _seat_provider(transport: str, model: str) -> tuple[str, str] | None:
    """(catalog provider, bare model id) for a seat, None when it names no model."""
    if transport in _ROUTER_TRANSPORTS:
        if "/" not in model:
            return None
        provider, bare = model.split("/", 1)
        return _CATALOG_PROVIDER_NAMES.get(provider, provider), bare
    return _SEAT_TRANSPORTS[transport], model


@dataclass(frozen=True)
class _SeatLiteral:
    """A seat string literal in review-cli's config.py, located by the AST (1-based line,
    UTF-8 byte columns as `ast` reports them)."""

    line: int
    col: int
    end_col: int
    seat: str


@dataclass
class _PythonSeats:
    live: list[_SeatLiteral]  # every seat literal a bump may rewrite
    # Names referenced inside a version-pinned alias value (`"fable5": FABLE_SEAT`):
    # (line, col, end_col, name).
    pinned_names: list[tuple[int, int, int, str]]
    constants: dict[str, str]  # NAME -> seat for `NAME = "seat"` / `NAME: T = "seat"`


def _python_seats(text: str) -> _PythonSeats:
    """Seat literals of a Python file, read structurally.

    A dict entry whose KEY carries a digit (`"opus48": ...`, `"grok-4.6": ...`) is a
    deliberately version-pinned alias: its value — however it is wrapped or split over lines
    — keeps its release, so its literals are not `live` and the constant names it uses are
    listed in `pinned_names`. Comments never reach the AST. Raises SyntaxError.
    """
    import ast  # lazy: only the review-cli check parses Python

    tree = ast.parse(text)
    pinned_nodes: set[int] = set()
    pinned_names: list[tuple[int, int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and re.search(r"\d", key.value):
                for sub in ast.walk(value):
                    pinned_nodes.add(id(sub))
                    if isinstance(sub, ast.Name):
                        pinned_names.append((sub.lineno, sub.col_offset, sub.end_col_offset, sub.id))
    live: list[_SeatLiteral] = []
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.lineno == node.end_lineno
            and _SEAT_RE.fullmatch(node.value)
            and id(node) not in pinned_nodes
        ):
            live.append(_SeatLiteral(node.lineno, node.col_offset, node.end_col_offset, node.value))
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and _SEAT_RE.fullmatch(value.value)
        ):
            constants[target.id] = value.value
    live.sort(key=lambda lit: (lit.line, lit.col))
    return _PythonSeats(live, pinned_names, constants)


def _seat_lines(path: Path) -> list[tuple[int, str, str, str]]:
    """(line number, seat, transport, model part) for every live seat literal in `path`.

    Python: the string literals `_python_seats` finds (never comments, never version-pinned
    alias values). YAML: quoted or bare values, with `# comments` cut off. Raises
    SyntaxError for a Python file that does not parse.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        found = []
        for lit in _python_seats(text).live:
            match = _SEAT_RE.fullmatch(lit.seat)
            found.append((lit.line, lit.seat, match["transport"], match["model"]))
        return found
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        line = re.split(r"\s#", line, maxsplit=1)[0]
        for match in _SEAT_RE.finditer(line):
            found.append((number, match.group(0), match["transport"], match["model"]))
    return found


def scan_review_cli(files: ReviewCliFiles, ids_for: Callable[[str], list[str]]) -> ReviewCliReport:
    """Every review-cli seat whose model has a newer same-family id in the catalogs.

    A seat with no same-family successor still lists newer VARIANTS of its line
    (`gpt-5.6-terra` -> `gpt-6-luna`) when any exist; those are reported, never patched.
    """
    report = ReviewCliReport(files=files)
    for path in (files.code_defaults, files.user_config):
        if path is None:
            continue
        try:
            seats = _seat_lines(path)
        except SyntaxError as exc:
            report.unchecked.append(f"{path} (not parseable: {exc.msg}, line {exc.lineno})")
            continue
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # An unreadable optional config must not cost the run its models.yaml results.
            report.unchecked.append(f"{path} (unreadable: {exc})")
            continue
        for number, seat, transport, model_part in seats:
            target = _seat_provider(transport, model_part)
            if target is None:
                continue
            provider, model = target
            ids = ids_for(provider)
            if not ids:
                if seat not in report.unchecked:
                    report.unchecked.append(seat)
                continue
            newer = newest_same_family(model, ids)
            variants = [] if newer else newer_variants(model, ids)
            if newer or variants:
                report.stale.append(SeatPin(path, number, seat, provider, model, newer, variants))
    return report


def review_cli_patch(report: ReviewCliReport) -> str:
    """A `git apply`-able diff (run in the review-cli checkout) bumping the code defaults.

    Only reviewlib/config.py is patched — the user's config.yaml is theirs and is only
    reported. Only same-family successors are applied; variant changes need a human. Comments
    and version-pinned alias values (`"opus48": ...`) keep their release: a pinned alias that
    points at a bumped constant (`"fable5": FABLE_SEAT`) gets the constant's OLD literal, so
    bumping the constant does not move the alias with it. Empty string when nothing changes.
    """
    import difflib

    path = report.files.code_defaults
    if path is None:
        return ""
    bumps = {pin.seat: pin.new_seat for pin in report.stale if pin.path == path and pin.new_seat}
    if not bumps:
        return ""
    text = path.read_text(encoding="utf-8")
    seats = _python_seats(text)
    # Split on "\n" only, as the AST counts lines (str.splitlines also breaks on \x0c etc.).
    lines = [line for line in re.split(r"(?<=\n)", text) if line]
    # (line, col, end_col, replacement source), columns in UTF-8 bytes as the AST gives them.
    edits: list[tuple[int, int, int, str]] = []
    for lit in seats.live:
        new = bumps.get(lit.seat)
        source = lines[lit.line - 1].encode("utf-8")[lit.col : lit.end_col].decode("utf-8")
        if new and source[:1] in ("'", '"') and source[1:-1] == lit.seat:
            edits.append((lit.line, lit.col, lit.end_col, f"{source[0]}{new}{source[0]}"))
    for line, col, end_col, name in seats.pinned_names:
        old = seats.constants.get(name)
        if old in bumps:
            edits.append((line, col, end_col, f'"{old}"'))
    new_lines = list(lines)
    for line, col, end_col, replacement in sorted(edits, reverse=True):
        raw = new_lines[line - 1].encode("utf-8")
        new_lines[line - 1] = (raw[:col] + replacement.encode("utf-8") + raw[end_col:]).decode("utf-8")
    rel = "reviewlib/config.py"
    return "".join(difflib.unified_diff(lines, new_lines, f"a/{rel}", f"b/{rel}"))


# ── manifest rewriting (for the proposed PR diff) ───────────────────────────────────────
# One pattern for the bare and the quoted (`"`/`'`) forms: the optional quote is captured in
# groups 1 and 2 so the same quote style is preserved on the new id.
_ID_LINE_RE = re.compile(r'(\bid:\s*["\']?)%s(["\']?\s*$)', re.MULTILINE)


def rewrite_manifest_text(text: str, current_id: str, new_id: str) -> str:
    """Return `text` with the first `id: <current_id>` line repointed to `new_id`.

    A minimal, surgical edit — it only swaps the id in place so the proposal diff is small
    and reviewable. Quoted and bare forms are both handled. It does NOT touch capabilities
    (the human edits those if the new model differs) — surfacing the unverified-capabilities
    caveat in the proposal body is the safety mechanism, not a silent capability rewrite.

    `new_id` comes from a provider's endpoint, so it is inserted via a CALLABLE replacement
    (not a replacement STRING) — a `re.sub` replacement string would interpret `\\g<…>`/`\\1`
    backslash escapes in the id and corrupt the manifest. The callable inserts `new_id`
    verbatim.
    """
    pat = re.compile(_ID_LINE_RE.pattern % re.escape(current_id), re.MULTILINE)
    new_text, n = pat.subn(lambda m: f"{m.group(1)}{new_id}{m.group(2)}", text, count=1)
    return new_text if n else text


# ── proposing: gh PR, else a dated report ───────────────────────────────────────────────
def _gh_available() -> bool:
    import shutil

    if not shutil.which("gh"):
        return False
    try:
        res = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _proposal_branch(proposal: Proposal) -> str:
    return f"{PROPOSAL_MARKER}/{proposal.slug}"


def _open_pr_exists(proposal: Proposal) -> bool:
    """True iff an OPEN PR for this exact bump already exists (idempotency guard).

    Matches on the proposal branch name (head) — deterministic per (provider, new id), so a
    re-run that finds the same newer model won't open a duplicate PR.
    """
    branch = _proposal_branch(proposal)
    try:
        res = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--head", branch, "--json", "number"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        return False
    return isinstance(data, list) and len(data) > 0


def _proposal_body(proposal: Proposal, poll: PollResult | None) -> str:
    caps = ", ".join(proposal.inferred_capabilities)
    advertised = ""
    if poll and proposal.new_id in poll.model_ids:
        advertised = f"\nThe provider's live model list advertises `{proposal.new_id}`."
    return (
        f"## Proposed model bump — `{proposal.provider}`\n\n"
        f"The daily model-freshness checker found a newer model for `{proposal.provider}`.\n\n"
        f"| | model id |\n|---|---|\n"
        f"| current pin | `{proposal.current_id}` |\n"
        f"| proposed | `{proposal.new_id}` |\n"
        f"{advertised}\n\n"
        f"**Capabilities (copied from the current pin, UNVERIFIED for the new model):** {caps}\n\n"
        f"> {proposal.note}\n\n"
        f"This is a SEMI-automatic proposal. A newer model can regress (a turbo/point release "
        f"may drop `vision` or shrink context), so this is **not** auto-merged — review the new "
        f"model's real capabilities, fix the `capabilities:`/`context:` if they changed, then "
        f"merge.\n"
    )


def propose_via_gh(
    proposal: Proposal, poll: PollResult | None, *, dry_run: bool, manifest_path: Path = MANIFEST_PATH
) -> str:
    """Open a PR repointing the manifest id. Returns a status string. Idempotent.

    Creates a fresh branch off HEAD, applies the surgical id swap to ``manifest_path``,
    commits, pushes, and opens a PR — unless an open PR for this exact bump already exists
    (then it's a no-op). ``manifest_path`` is threaded from ``run()`` so the file actually
    rewritten is the one the proposals were computed from (not a stale module global).
    """
    if _open_pr_exists(proposal):
        return f"skip: open PR already exists for {proposal.slug}"
    branch = _proposal_branch(proposal)
    title = f"chore(models): bump {proposal.provider} {proposal.current_id} -> {proposal.new_id} ({PROPOSAL_MARKER})"
    body = _proposal_body(proposal, poll)
    if dry_run:
        return f"dry-run: would open PR on branch {branch}: {title}"

    text = manifest_path.read_text(encoding="utf-8")
    new_text = rewrite_manifest_text(text, proposal.current_id, proposal.new_id)
    if new_text == text:
        return f"skip: could not locate id '{proposal.current_id}' in manifest to rewrite"

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT)
        )

    base = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    if _git("checkout", "-B", branch).returncode != 0:
        return f"error: could not create branch {branch}"
    try:
        manifest_path.write_text(new_text, encoding="utf-8")
        _git("add", str(manifest_path.relative_to(REPO_ROOT)))
        if _git("commit", "-m", title).returncode != 0:
            return f"error: commit failed for {proposal.slug}"
        if _git("push", "-u", "origin", branch, "--force-with-lease").returncode != 0:
            return f"error: push failed for {branch}"
        res = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", branch],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
    finally:
        _git("checkout", base)
    if res.returncode != 0:
        return f"error: gh pr create failed for {proposal.slug}: {res.stderr.strip()}"
    return f"opened PR for {proposal.slug}: {res.stdout.strip()}"


def write_report(
    proposals: list[Proposal],
    polls: dict[str, PollResult],
    review_cli: ReviewCliReport | None = None,
    variants: dict[str, list[str]] | None = None,
) -> Path:
    """Write a dated markdown report of all proposals (the gh-absent fallback) and of the
    stale review-cli seats; a non-empty review-cli patch lands next to it as
    `<date>-review-cli.patch`.

    The path is dated per-run (one report per day); content is deterministic for a given set
    of proposals so a same-day re-run overwrites rather than accumulating noise.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"{today}-model-freshness.md"
    lines = [
        f"# Model-freshness report — {today}",
        "",
        "Proposed models.yaml bumps that were not opened as PRs (no `gh` or auth) are",
        "recorded here. Apply a bump by editing `lib/contracts/models.yaml` after verifying",
        "the new model's real capabilities.",
        "",
    ]
    if not proposals:
        # Be honest about coverage: "all current" is only true if every provider was actually
        # polled. If some were skipped (no key) or errored, say so — a missing key must not
        # masquerade as "current".
        if all(pr.ok for pr in polls.values()):
            lines.append("**No newer models found.** Every polled provider pin is current.")
        else:
            ok = sum(1 for pr in polls.values() if pr.ok)
            lines.append(
                f"**No bumps from the {ok} provider(s) successfully polled.** "
                "Coverage is INCOMPLETE — some providers were skipped or errored (see below); "
                "this is NOT a clean bill of health for them."
            )
    else:
        lines.append("## Proposed bumps")
        lines.append("")
        for p in proposals:
            caps = ", ".join(p.inferred_capabilities)
            lines.append(f"- **{p.provider}**: `{p.current_id}` -> `{p.new_id}`")
            lines.append(f"  - capabilities (copied from current pin, UNVERIFIED): {caps}")
            lines.append(f"  - {p.note}")
    if variants:
        lines.append("")
        lines.append("## Pins with newer variants (not proposed — a human picks the variant)")
        lines.append("")
        for pin, newer in variants.items():
            lines.append(f"- `{pin}`: {', '.join(f'`{mid}`' for mid in newer)}")
    skipped = [pr for pr in polls.values() if not pr.ok]
    if skipped:
        lines.append("")
        lines.append("## Providers skipped / errored")
        lines.append("")
        for pr in skipped:
            reason = pr.skipped or pr.error or "unknown"
            lines.append(f"- **{pr.provider}**: {reason}")
    if review_cli is not None:
        lines.append("")
        lines.extend(_review_cli_lines(review_cli))
        patch = review_cli_patch(review_cli)
        if patch:
            patch_path = REPORTS_DIR / f"{today}-review-cli.patch"
            patch_path.write_text(patch, encoding="utf-8")
            lines.append("")
            lines.append(
                f"Same-family bumps of the code defaults: `git apply {patch_path}` in the "
                "review-cli checkout, then ship it through review-cli's own PR flow."
            )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _review_cli_lines(report: ReviewCliReport) -> list[str]:
    """Markdown/plain lines naming every stale review-cli seat."""
    lines = ["## review-cli seats"]
    for label, path in (
        ("code defaults", report.files.code_defaults),
        ("user config", report.files.user_config),
    ):
        lines.append(f"- {label}: {path if path else 'not found'}")
    if not report.stale:
        lines.append("- every checked seat is current")
    for pin in report.stale:
        where = f"{pin.path}:{pin.line}"
        if pin.newer:
            lines.append(f"- STALE `{pin.seat}` -> `{pin.new_seat}` ({where})")
        else:
            lines.append(
                f"- newer variants for `{pin.seat}`: {', '.join(pin.variants)} "
                f"(human choice, not patched; {where})"
            )
    if report.unchecked:
        lines.append(f"- unchecked (no catalog lists the provider): {', '.join(report.unchecked)}")
    return lines


# ── top-level run ───────────────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    proposals: list[Proposal]
    polls: dict[str, PollResult]
    actions: list[str] = field(default_factory=list)
    report_path: Path | None = None
    used_gh: bool = False
    catalogs: list[HarnessCatalog] = field(default_factory=list)
    review_cli: ReviewCliReport | None = None
    pin_variants: dict[str, list[str]] = field(default_factory=dict)


def run(
    *,
    manifest_path: Path = MANIFEST_PATH,
    timeout: float = 20.0,
    dry_run: bool = False,
    force_report: bool = False,
    pollers: dict[str, Callable[[float], PollResult]] | None = None,
    gh_available: Callable[[], bool] | None = None,
    harness_sources: dict[str, Callable[[float], dict[str, list[str]]]] | None = None,
    fallback_sources: dict[str, Callable[[float], dict[str, list[str]]]] | None = None,
    review_cli: ReviewCliFiles | None = None,
) -> RunResult:
    """Poll providers, compute proposals, and PROPOSE (PR via gh, else a dated report).

    Hooks (`pollers`, `gh_available`, `harness_sources`, `fallback_sources`) are injected for
    tests — production passes None and the real implementations are used. `review_cli`
    names the review-cli files to check; None skips that check.
    """
    manifest = load_manifest(manifest_path)
    problems = validate_manifest(manifest)
    if problems:
        raise ManifestError("manifest invalid:\n  - " + "\n  - ".join(problems))

    pollers = pollers or POLLERS
    catalogs = CatalogSet(
        load_catalogs(HARNESS_SOURCES if harness_sources is None else harness_sources, timeout),
        FALLBACK_SOURCES if fallback_sources is None else fallback_sources,
        timeout,
    )
    polls: dict[str, PollResult] = {}
    for provider in manifest.providers():
        poller = pollers.get(provider)
        api = poller(timeout) if poller else PollResult(provider, False, skipped="no poller")
        polls[provider] = catalogs.poll(api)

    proposals = compute_proposals(manifest, polls)
    result = RunResult(proposals=proposals, polls=polls, pin_variants=pin_variants(manifest, polls))

    if review_cli is not None:

        seen: dict[str, list[str]] = {}

        def ids_for(provider: str) -> list[str]:
            if provider not in seen:
                known = polls.get(provider)
                seen[provider] = catalogs.poll(
                    known if known is not None else PollResult(provider, False, skipped="not in manifest")
                ).model_ids
            return seen[provider]

        result.review_cli = scan_review_cli(review_cli, ids_for)
    result.catalogs = catalogs.all()
    review_cli_stale = bool(result.review_cli and result.review_cli.stale)

    gh_ok = (gh_available or _gh_available)() and not force_report
    result.used_gh = gh_ok
    if gh_ok:
        for proposal in proposals:
            status = propose_via_gh(
                proposal, polls.get(proposal.provider), dry_run=dry_run, manifest_path=manifest_path
            )
            result.actions.append(status)
        if not proposals:
            result.actions.append("no newer models found")
    if dry_run and not gh_ok:
        # --dry-run must NEVER write (the CLI contract). In the gh-absent branch that means
        # NOT writing the dated report either — just record what would happen.
        result.actions.append(
            f"dry-run: would write a report with {len(proposals)} proposal(s) (no gh)"
        )
    elif not dry_run and (not gh_ok or review_cli_stale or result.pin_variants):
        # review-cli seats and pin variants have no PR path, so they always land in the
        # report, even when the models.yaml bumps went out as PRs.
        path = write_report(
            [] if gh_ok else proposals, polls, result.review_cli, result.pin_variants
        )
        result.report_path = path
        result.actions.append(f"wrote report {path}")
    return result


def _print_human(result: RunResult) -> None:
    print(f"model-freshness: polled {len(result.polls)} provider(s)")
    for prov, poll in sorted(result.polls.items()):
        if poll.ok:
            print(f"  ✔ {prov}: {len(poll.model_ids)} models (via {', '.join(poll.sources)})")
        else:
            print(f"  · {prov}: skipped ({poll.skipped or poll.error})")
    for catalog in result.catalogs:
        if catalog.error:
            print(f"  · catalog {catalog.name}: unavailable ({catalog.error})")
    if result.proposals:
        print(f"\nproposed {len(result.proposals)} bump(s):")
        for p in result.proposals:
            print(f"  ▸ {p.provider}: {p.current_id} -> {p.new_id}  [{', '.join(p.inferred_capabilities)}]")
    elif all(pr.ok for pr in result.polls.values()):
        print("\nno newer models found — every polled provider pin is current")
    else:
        ok = sum(1 for pr in result.polls.values() if pr.ok)
        print(f"\nno bumps from the {ok} provider(s) polled — coverage INCOMPLETE "
              "(some providers skipped/errored; not a clean bill of health)")
    if result.pin_variants:
        print("\npins with newer variants (not proposed — a human picks the variant):")
        for pin, newer in result.pin_variants.items():
            print(f"  ▸ {pin}: {', '.join(newer)}")
    if result.review_cli is not None:
        print()
        for line in _review_cli_lines(result.review_cli):
            print(f"  {line}")
    for action in result.actions:
        print(f"  {action}")


def _review_cli_json(report: ReviewCliReport) -> dict[str, Any]:
    return {
        "code_defaults": str(report.files.code_defaults) if report.files.code_defaults else None,
        "user_config": str(report.files.user_config) if report.files.user_config else None,
        "stale": [
            {"file": str(pin.path), "line": pin.line, "seat": pin.seat, "proposed": pin.new_seat,
             "variants": pin.variants}
            for pin in report.stale
        ],
        "unchecked": report.unchecked,
    }


def _manifest_error(exc: ManifestError) -> AgentToolError:
    """Map a raw ManifestError to the shared structured error (WHAT/WHY/HOW + exit code).

    A missing PyYAML is a missing-DEPENDENCY failure (127 + install hint); any other manifest
    fault is a CONFIG error (2). The message already carries the actionable detail, so it goes
    into ``what``; ``why``/``fix`` add the class-specific guidance.
    """
    if isinstance(exc, MissingYamlError):
        return MissingDepError(
            what="cannot read the model manifest — PyYAML is not installed",
            why="model_freshness needs PyYAML to parse lib/contracts/models.yaml",
            fix="install PyYAML, then re-run the checker",
            install="python3 -m pip install --user pyyaml",
        )
    return ConfigError(
        what=f"model manifest is invalid: {exc}",
        why="lib/contracts/models.yaml is malformed or violates a cross-reference invariant",
        fix="fix the manifest (run with --validate to list every problem), then re-run",
    )


def _run_cli(args: argparse.Namespace) -> int:
    """The checker body, raising structured agenttools_errors on failure (rendered by guard)."""
    try:
        manifest = load_manifest()
    except ManifestError as exc:
        raise _manifest_error(exc) from exc

    if args.validate:
        problems = validate_manifest(manifest)
        if problems:
            raise ConfigError(
                what="manifest INVALID:\n  - " + "\n  - ".join(problems),
                why="lib/contracts/models.yaml violates a cross-reference invariant a schema can't express",
                fix="fix each problem listed above, then re-run --validate",
            )
        print(f"manifest OK — {len(manifest.models)} models, {len(manifest.roles)} roles, "
              f"{len(manifest.aliases)} aliases")
        return EXIT_OK

    review_cli = None
    if not args.no_review_cli:
        root = Path(args.review_cli_root) if args.review_cli_root else None
        review_cli = find_review_cli_files(root)

    try:
        result = run(
            dry_run=args.dry_run, force_report=args.report, timeout=args.timeout, review_cli=review_cli
        )
    except ManifestError as exc:
        raise _manifest_error(exc) from exc

    if args.review_cli_patch:
        patch = review_cli_patch(result.review_cli) if result.review_cli else ""
        if args.review_cli_patch == "-":
            sys.stdout.write(patch)
            return EXIT_OK
        if not patch:
            result.actions.append("no stale review-cli code-default seats; no patch written")
        elif args.dry_run:
            # --dry-run never writes; `--review-cli-patch -` is the dry-run way to get the patch.
            result.actions.append(f"dry-run: would write review-cli patch {args.review_cli_patch}")
        else:
            Path(args.review_cli_patch).write_text(patch, encoding="utf-8")
            result.actions.append(f"wrote review-cli patch {args.review_cli_patch}")

    if args.json:
        print(json.dumps(
            {
                "proposals": [
                    {"provider": p.provider, "current": p.current_id, "new": p.new_id,
                     "capabilities": list(p.inferred_capabilities)}
                    for p in result.proposals
                ],
                "pin_variants": result.pin_variants,
                "polls": {k: {"ok": v.ok, "count": len(v.model_ids), "sources": v.sources,
                              "skipped": v.skipped, "error": v.error}
                          for k, v in result.polls.items()},
                # Per catalog: how many ids it lists for each manifest provider (models.dev
                # covers hundreds of providers; only the ones the manifest pins matter here).
                "catalogs": {c.name: {"providers": {p: len(ids) for p, ids in c.ids.items()
                                                    if p in result.polls},
                                      "error": c.error}
                             for c in result.catalogs},
                "review_cli": _review_cli_json(result.review_cli) if result.review_cli else None,
                "actions": result.actions,
                "report": str(result.report_path) if result.report_path else None,
                "used_gh": result.used_gh,
            },
            indent=2,
        ))
    else:
        _print_human(result)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="model-freshness",
        description="Propose model-version bumps for lib/contracts/models.yaml (PROPOSE, never merge).",
    )
    parser.add_argument("--validate", action="store_true", help="validate the manifest and exit")
    parser.add_argument("--dry-run", action="store_true", help="poll + compute, but never open a PR / write a report")
    parser.add_argument("--report", action="store_true", help="force the dated-report path even if gh is available")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request HTTP timeout seconds")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary instead of human text")
    parser.add_argument(
        "--review-cli-root",
        metavar="DIR",
        help="review-cli checkout whose reviewlib/config.py seats to check "
        "(default: the checkout the installed `review` resolves into)",
    )
    parser.add_argument(
        "--no-review-cli", action="store_true", help="skip checking review-cli's seats"
    )
    parser.add_argument(
        "--review-cli-patch",
        metavar="FILE",
        help="write a `git apply`-able patch bumping review-cli's stale code-default seats to "
        "FILE (`-` = stdout, patch only, also under --dry-run; a FILE is not written under "
        "--dry-run or when nothing is stale); the user's config.yaml is reported, never patched",
    )
    args = parser.parse_args(argv)
    if args.review_cli_patch == "-" and args.json:
        parser.error("--review-cli-patch - prints only the patch; drop --json or write the patch to a FILE")
    # guard() renders any structured AgentToolError as the 3-part block (what/why/fix) on
    # stderr and returns its stable exit code; an unexpected bug propagates → traceback + 1.
    return guard(lambda: _run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())

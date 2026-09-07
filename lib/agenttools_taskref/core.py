"""Core implementation of backend-aware task-code validation.

WHAT THIS FILE IS
    The engine behind ``agenttools_taskref.validate``. Given a candidate code (the string an
    agent typed after ``--task`` / into ``$REVIEW_TASK_CODE``) and a repo root, it:

    1. Loads the repo's tracker backend selection the same way task-cli's own
       ``tasklib/config.py`` does — defaults, then a ``rig.yaml`` ``task:`` block overlay,
       then a native ``task.yaml`` (later layers win; see :func:`load_repo_task_config`).
    2. For ``backend: github-issues``, parses the candidate's shape (bare number, ``#N``, or
       an explicit ``owner/repo#N`` cross-repo form) and confirms the number actually
       resolves via ``gh api`` — a numeric-shaped string that 404s is rejected just like a
       free-text slug.
    3. For ``backend: linear``, checks the candidate carries the repo's actual configured
       team prefix (``task.yaml``'s ``linear.team`` / ``rig.yaml``'s ``task.team``).

WHY "NO task.yaml" ISN'T THE FAILURE CASE
    task-cli itself works with **zero** config files on a normal GitHub repo (defaults to
    ``backend: github-issues``, ``github.repo: auto`` resolved from the git ``origin``
    remote) — this is exactly why ``task read <n> -C <a-repo-with-no-task.yaml>`` works today
    in this very repo. So an absent ``task.yaml``/``rig.yaml`` is NOT itself a validation
    failure; the failure case this library actually surfaces is "unresolvable": a
    ``github-issues`` repo with no git ``origin`` (so ``auto`` has nothing to resolve), or a
    ``linear`` repo with no configured team. Both are mirrored, message-for-message where
    practical, from ``tasklib/config.py`` and ``tasklib/backends/github_issues.py``.

SCOPE, DELIBERATELY NARROWED FROM task-cli's FULL CASCADE
    This mirrors only the two layers that decide "what does a real code look like for this
    repo" (``rig.yaml`` task: block, repo ``task.yaml``) — not task-cli's global
    ``~/.config/task-cli/config.yaml`` layer or its ``--config``/env-var overrides, which are
    machine-local and irrelevant to "is this a real, committed-repo-shaped ticket reference".
    A Linear existence check (an actual API round trip) is also out of scope: AC3 only
    requires the team-prefix match, and unlike GitHub there is no ubiquitous, already-trusted
    CLI (``gh``) to shell out to for it — see :func:`_validate_linear`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

VALID_BACKENDS = frozenset({"github-issues", "linear"})

# A GitHub issue/PR reference: optionally `owner/repo#`, then an optional bare `#`, then digits.
# `owner`/`repo` are RFC-ish GitHub path segments (alnum, `.`/`_`/`-`, not leading/trailing `-`).
_GITHUB_CODE_RE = re.compile(
    r"^(?:(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)#)?"
    r"#?(?P<num>[0-9]+)$"
)
# A Linear/Jira-style key: 2+ letters, a hyphen, digits. Team prefixes are short (HYP, GH, ...).
_LINEAR_CODE_RE = re.compile(r"^(?P<prefix>[A-Za-z]{2,10})-(?P<num>[0-9]+)$")

_GIT_TIMEOUT_S = 5
_GH_TIMEOUT_S = 15

_GUIDANCE = (
    'Use `task find "<query>"` to locate the real ticket, or `task new` to create one.'
)


class TaskRefError(RuntimeError):
    """A malformed ``task.yaml`` (fail-closed) — ``validate()`` catches this internally."""


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating one candidate task code against one repo's backend."""

    ok: bool
    code: str  # the caller's original candidate, unchanged
    canonical: str | None  # normalized, resolvable form (set only when ok)
    url: str | None  # a real, checkable URL (github only; linear leaves this None — see module docstring)
    backend: str | None  # "github-issues" | "linear" | None (backend unresolvable at all)
    reason: str  # human-readable explanation, success or rejection

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class RepoTaskConfig:
    """The resolved backend selection for one repo — the fields `validate()` actually needs."""

    backend: str
    github_repo: str  # "owner/name" or "auto"
    linear_team: str  # e.g. "HYP", "" if unset
    sources: tuple[str, ...]  # provenance, e.g. ("builtin-defaults", "task.yaml")


# ── config loading (mirrors tasklib/config.py's backend/github.repo/linear.team layers) ────


def load_repo_task_config(repo_root: Path | str) -> RepoTaskConfig:
    """Cascade-load ``backend`` + ``github.repo``/``linear.team`` for ``repo_root``.

    Layer order (later wins), matching ``tasklib/config.py``'s own precedence for these same
    keys: built-in defaults -> ``rig.yaml`` ``task:`` block -> repo ``task.yaml``. Never
    returns ``None`` — a repo with no config files at all still resolves to the same
    zero-config default task-cli itself uses (``backend: github-issues``,
    ``github.repo: auto``).

    Raises :class:`TaskRefError` only for a genuinely malformed ``task.yaml`` (fail-closed,
    like task-cli's own ``config.validate``) or an invalid ``rig.yaml`` ``task:`` block. A
    ``rig.yaml`` that fails to parse for an UNRELATED reason is tolerated (foreign file, same
    as task-cli's own ``_ignore_rig`` fallback) — it just contributes nothing.
    """
    repo_root = Path(repo_root)
    backend, github_repo, linear_team = "github-issues", "auto", ""
    sources = ["builtin-defaults"]

    rig_data = _load_rig_task_block(repo_root / "rig.yaml")
    if rig_data is not None:
        backend, github_repo, linear_team = _apply_rig_task_block(
            rig_data, backend, github_repo, linear_team
        )
        sources.append("rig.yaml")

    task_data = _load_yaml_mapping(repo_root / "task.yaml", tolerant=False)
    if task_data:
        backend, github_repo, linear_team = _apply_task_yaml(
            task_data, backend, github_repo, linear_team
        )
        sources.append("task.yaml")

    if backend not in VALID_BACKENDS:
        raise TaskRefError(f"backend must be one of {sorted(VALID_BACKENDS)}, got {backend!r}")
    return RepoTaskConfig(backend, github_repo, linear_team, tuple(sources))


def _load_rig_task_block(path: Path) -> dict | None:
    """The ``task:`` block from ``rig.yaml``, or ``None`` if absent/foreign-unreadable."""
    data = _load_yaml_mapping(path, tolerant=True)
    if not data:
        return None
    block = data.get("task")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise TaskRefError(f"'task' block in {path} must be a mapping, got {type(block).__name__}")
    return block


def _load_yaml_mapping(path: Path, *, tolerant: bool) -> dict | None:
    """Parse ``path`` as a YAML mapping. Missing file -> ``None`` always.

    ``tolerant=True`` (rig.yaml, a foreign file owned by rig-cli) swallows any parse/shape
    problem and returns ``None`` instead of raising — a malformed rig.yaml must not break every
    task-code validation in the repo. ``tolerant=False`` (task.yaml, task-cli's own file) raises
    :class:`TaskRefError` on the same problems, matching task-cli's fail-closed contract.
    """
    if not path.is_file():
        return None
    try:
        import yaml  # lazy: keeps import-time stdlib-only

        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - see tolerant contract above
        if tolerant:
            return None
        raise TaskRefError(f"cannot read/parse {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        if tolerant:
            return None
        raise TaskRefError(f"{path} must be a YAML mapping, got {type(data).__name__}")
    return data


def _apply_rig_task_block(
    block: dict, backend: str, github_repo: str, linear_team: str
) -> tuple[str, str, str]:
    """Translate a ``rig.yaml`` ``task:`` block's flat aliases + nested passthrough onto the
    three fields this library cares about. Mirrors ``tasklib.config.rig_task_overlay``'s
    ``team``/``repo`` aliases, narrowed to just what ``validate()`` needs."""
    if "backend" in block:
        backend = str(block["backend"])
    linear = block.get("linear")
    if "team" in block:
        linear_team = str(block["team"])
    elif isinstance(linear, dict) and "team" in linear:
        linear_team = str(linear["team"])
    github = block.get("github")
    if "repo" in block:
        github_repo = str(block["repo"])
    elif isinstance(github, dict) and "repo" in github:
        github_repo = str(github["repo"])
    return backend, github_repo, linear_team


def _apply_task_yaml(
    data: dict, backend: str, github_repo: str, linear_team: str
) -> tuple[str, str, str]:
    """Apply a native ``task.yaml``'s ``backend``/``github.repo``/``linear.team``."""
    if "backend" in data:
        backend = str(data["backend"])
    github = data.get("github")
    if isinstance(github, dict) and "repo" in github:
        github_repo = str(github["repo"])
    linear = data.get("linear")
    if isinstance(linear, dict) and "team" in linear:
        linear_team = str(linear["team"])
    return backend, github_repo, linear_team


# ── github.repo -> owner/repo resolution (mirrors backends/github_issues.py) ───────────────


def _resolve_github_owner_repo(repo_spec: str, repo_root: Path) -> tuple[str, str] | None:
    """Resolve ``owner/repo``. ``auto`` reads the git ``origin`` remote of ``repo_root``.
    Returns ``None`` when unresolvable (malformed spec, no git, no origin, unparseable
    remote) — mirrors ``tasklib.backends.github_issues._resolve_repo``'s failure cases."""
    if repo_spec and repo_spec != "auto":
        if "/" not in repo_spec:
            return None
        owner, repo = repo_spec.split("/", 1)
        return (owner, repo) if owner and repo else None
    return _git_origin_owner_repo(repo_root)


def _git_origin_owner_repo(repo_root: Path) -> tuple[str, str] | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return _parse_github_remote(out.stdout.strip())


def _parse_github_remote(url: str) -> tuple[str, str] | None:
    cleaned = url[: -len(".git")] if url.endswith(".git") else url
    if cleaned.startswith("git@") and ":" in cleaned:
        path = cleaned.split(":", 1)[1]
    elif "github.com/" in cleaned:
        path = cleaned.split("github.com/", 1)[1]
    else:
        return None
    parts = path.strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


# ── candidate shape parsing ─────────────────────────────────────────────────────────────────


def _parse_github_code(code: str) -> tuple[str | None, str | None, int] | None:
    """``(owner, repo, number)`` for a github-shaped candidate (``owner``/``repo`` are ``None``
    for a same-repo bare/``#N`` form), or ``None`` when the candidate isn't github-shaped at
    all (a free-text slug, a placeholder like ``CODE``, an empty/zero number, ...)."""
    match = _GITHUB_CODE_RE.match(code)
    if not match:
        return None
    try:
        number = int(match.group("num"))
    except ValueError:
        return None
    if number <= 0:
        return None
    return match.group("owner"), match.group("repo"), number


# ── github existence check (shells out to `gh`, the ecosystem's already-trusted GitHub CLI) ─


def _check_github_issue(owner: str, repo: str, number: int) -> tuple[bool | None, str, str | None]:
    """Whether ``owner/repo#number`` really resolves. Returns ``(exists, message, html_url)``:

    - ``exists=True``  — confirmed real; ``html_url`` is GitHub's own canonical link (already
      the correct ``/issues/N`` or ``/pull/N`` form — GitHub issues and PRs share one number
      space and this one endpoint covers both).
    - ``exists=False`` — confirmed 404: the number does not exist in this repo.
    - ``exists=None``  — could NOT be verified (``gh`` missing, not authenticated, network
      failure). Distinct from ``False`` on purpose: an unverifiable candidate must not be
      told "that ticket doesn't exist, try `task find`" — it might be perfectly real.
    """
    argv = ["gh", "api", f"repos/{owner}/{repo}/issues/{number}", "--jq", ".html_url"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=_GH_TIMEOUT_S, check=False)
    except FileNotFoundError:
        return None, "gh CLI not found on PATH; cannot verify GitHub issue/PR numbers", None
    except subprocess.SubprocessError as exc:
        return None, f"gh api call failed: {exc}", None

    if out.returncode == 0:
        return True, "resolved via gh api", (out.stdout.strip().strip('"') or None)

    stderr = (out.stderr or "").strip()
    if "HTTP 404" in stderr or "Not Found" in stderr:
        return False, f"{owner}/{repo}#{number} does not exist", None
    return None, f"could not verify via gh api ({stderr[:200] or 'unknown error'})", None


# ── per-backend validation ──────────────────────────────────────────────────────────────────


def _validate_github(candidate: str, cfg: RepoTaskConfig, repo_root: Path) -> ValidationResult:
    parsed = _parse_github_code(candidate)
    if parsed is None:
        return ValidationResult(
            False, candidate, None, None, "github-issues",
            f"{candidate!r} is not a GitHub issue/PR reference (expected a bare number, "
            f"'#N', or 'owner/repo#N'). {_GUIDANCE}",
        )
    owner, repo, number = parsed
    cross_repo = owner is not None
    if not cross_repo:
        resolved = _resolve_github_owner_repo(cfg.github_repo, repo_root)
        if resolved is None:
            return ValidationResult(
                False, candidate, None, None, "github-issues",
                "cannot resolve github repo: no 'origin' remote and no github.repo set in "
                "task.yaml (task-cli's own error for this case)",
            )
        owner, repo = resolved

    exists, detail, html_url = _check_github_issue(owner, repo, number)
    if exists is not True:
        reason = f"{detail}. {_GUIDANCE}" if exists is False else detail
        return ValidationResult(False, candidate, None, None, "github-issues", reason)

    canonical = f"{owner}/{repo}#{number}" if cross_repo else str(number)
    return ValidationResult(True, candidate, canonical, html_url, "github-issues", detail)


def _validate_linear(candidate: str, cfg: RepoTaskConfig) -> ValidationResult:
    team = cfg.linear_team.strip()
    if not team:
        return ValidationResult(
            False, candidate, None, None, "linear",
            "linear backend requires a team key (task-cli's own error) -- set linear.team in "
            "task.yaml, or the 'team' shorthand in rig.yaml's task: block",
        )
    match = _LINEAR_CODE_RE.match(candidate)
    if not match:
        return ValidationResult(
            False, candidate, None, None, "linear",
            f"{candidate!r} is not a Linear ticket reference (expected 'PREFIX-N'). {_GUIDANCE}",
        )
    prefix, num = match.group("prefix"), match.group("num")
    if prefix.upper() != team.upper():
        return ValidationResult(
            False, candidate, None, None, "linear",
            f"{candidate!r} does not carry this repo's configured team prefix ({team!r}); "
            f"expected something like '{team.upper()}-{num}'.",
        )
    return ValidationResult(
        True, candidate, f"{team.upper()}-{int(num)}", None, "linear",
        "team prefix matches this repo's configured linear.team (existence not checked "
        "against the Linear API — see module docstring)",
    )


# ── public entry point ──────────────────────────────────────────────────────────────────────


def validate(code: str, repo_root: Path | str) -> ValidationResult:
    """Validate ``code`` against ``repo_root``'s configured tracker backend.

    Never raises: a malformed ``task.yaml``/``rig.yaml`` becomes an ``ok=False`` result with
    the parse error as the reason, exactly like every other rejection.
    """
    repo_root = Path(repo_root)
    candidate = str(code).strip()
    if not candidate:
        return ValidationResult(False, str(code), None, None, None, "task code is empty")

    try:
        cfg = load_repo_task_config(repo_root)
    except TaskRefError as exc:
        return ValidationResult(False, candidate, None, None, None, str(exc))

    if cfg.backend == "github-issues":
        return _validate_github(candidate, cfg, repo_root)
    return _validate_linear(candidate, cfg)


__all__ = [
    "RepoTaskConfig",
    "TaskRefError",
    "ValidationResult",
    "VALID_BACKENDS",
    "load_repo_task_config",
    "validate",
]

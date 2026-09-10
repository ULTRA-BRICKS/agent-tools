"""agenttools_taskref — shared, backend-aware task-code validation.

review-cli's ``--task CODE`` / ``$REVIEW_TASK_CODE`` (and any other ``--task``-shaped
consumer in this ecosystem) used to accept ANY free-form string with zero validation. A live
sweep of ``~/.config/review-cli/run-stats.jsonl`` found thousands of invented,
non-resolvable codes recorded as if they were real ticket references — bare placeholders
like ``"CODE"``/``"TEST-1"``, free-text slugs like ``"ambiguous-create-dedup"`` — none of
which trace back to an actual GitHub issue or Linear ticket (agent-tools#607).

A repo's own ``task.yaml`` (or ``rig.yaml`` ``task:`` block) already declares what a REAL
code looks like for that repo — ``backend: github-issues`` with a resolvable
``github.repo``, or ``backend: linear`` with a team prefix. This library reads that same
config the way task-cli's own ``tasklib/config.py`` does, and validates a candidate code
against it:

    from pathlib import Path
    from agenttools_taskref import validate

    result = validate("607", Path("."))
    if not result.ok:
        raise SystemExit(f"error: {result.reason}")
    print(result.canonical, result.url)

See ``core.py``'s module docstring for the full design (why an absent ``task.yaml`` is NOT
itself a failure, the github ``gh api`` existence check, the linear prefix-only check).
"""

from __future__ import annotations

from .core import (
    VALID_BACKENDS,
    RepoTaskConfig,
    TaskRefError,
    ValidationResult,
    load_repo_task_config,
    validate,
)

__all__ = [
    "VALID_BACKENDS",
    "RepoTaskConfig",
    "TaskRefError",
    "ValidationResult",
    "load_repo_task_config",
    "validate",
]

__version__ = "0.1.0"

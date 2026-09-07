"""The per-harness delegation-recipe needles the hook tests assert on (agent-tools#573).

One place for the substring that identifies EACH harness's recipe in a refusal — a launcher
path or the CC Agent-tool phrase — so a changed launcher path or recipe wording is fixed in one
spot instead of four (review finding, #573 round 2). The recipes themselves live in
``agenttools_hatch_escalation._DELEGATION_RECIPES``.
"""

from __future__ import annotations

RECIPE_NEEDLE: dict[str, str] = {
    "claude-code": 'subagent_type: "fork"',
    "codex": "~/.agents/skills/rig-detached-codex/rig-detached-codex",
    "opencode": "~/.agents/skills/rig-detached-opencode/rig-detached-opencode",
    "omp": "~/.agents/skills/rig-detached-omp/rig-detached-omp",
}

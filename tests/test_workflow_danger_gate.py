"""Tests for the workflow-danger-gate agent-hook (pre-workflow).

The harness may auto-allow the Workflow tool so ultracode can spawn without a
prompt. This gate is the script-level backstop: destructive commands in the
executable JS are blocked; JS comments and agent() prompt strings that merely
*mention* those commands stay allowed.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_workflow_danger_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "workflow-danger-gate"
    / "workflow_danger_gate.py"
)
_spec = importlib.util.spec_from_file_location("workflow_danger_gate", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _run(event, monkeypatch) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = gate.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _event(script: str) -> dict:
    return {"args": {"script": script}, "cwd": "/repo", "tool": "Workflow"}


ULTRACODE = """
const a = await agent({
  prompt: "Implement the fix in a worktree. Never run gh pr merge; use gh ship.",
});
return a;
"""


def test_ultracode_agent_dispatch_is_allowed(monkeypatch):
    out, _err, code = _run(_event(ULTRACODE), monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_agent_prompt_mentioning_danger_is_allowed(monkeypatch):
    """A prompt that *forbids* a raw merge must not trip the gate — that is the
    ultracode path Alex asked to keep working."""
    script = (
        "return await agent({ prompt: 'do not gh pr merge or git reset --hard; "
        "use gh ship' });"
    )
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "script,rule",
    [
        ("await $`gh pr merge 12 --admin`;", "raw-pr-merge"),
        ("await $`git reset --hard HEAD`;", "reset-hard"),
        ("await $`git push --force origin main`;", "force-push"),
        ("await $`git push -f origin main`;", "force-push"),
        ("await $`git clean -fd`;", "clean-force"),
        ("await $`sudo rm -rf /tmp/x`;", "sudo-rm"),
        ("await $`git commit --no-verify -m x`;", "no-verify"),
        ("await $`screencapture /tmp/x.png`;", "screencapture"),
        ('exec("gh pr merge 1");', "raw-pr-merge"),
        ("await $`gh -R o/r pr merge 12`;", "raw-pr-merge"),
        ("await $`gh --repo o/r pr merge 12`;", "raw-pr-merge"),
        ("await $`gh pr --repo o/r merge 12`;", "raw-pr-merge"),
        ("await $`gh pr -R o/r merge 12`;", "raw-pr-merge"),
        ("await $`git commit -n -m x`;", "no-verify"),
        ("await $`sudo -n rm -rf /tmp/x`;", "sudo-rm"),
        ("await $`sudo /bin/rm -rf /tmp/x`;", "sudo-rm"),
    ],
)
def test_executable_danger_is_blocked(monkeypatch, script, rule):
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 10
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert rule in payload["message"]


def test_force_with_lease_is_not_a_force_push(monkeypatch):
    out, _err, code = _run(_event("await $`git push --force-with-lease`;"), monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_line_comment_mentioning_danger_is_allowed(monkeypatch):
    script = "// never gh pr merge or git reset --hard from a Workflow\nreturn 1;\n"
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_comment_mentioning_danger_is_allowed(monkeypatch):
    script = "/* git push --force and sudo rm are blocked in executable JS */\nreturn 1;\n"
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_comment_plus_real_danger_still_blocks(monkeypatch):
    script = "// allowed comment: gh pr merge\nawait $`gh pr merge 1`;\n"
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 10
    assert _decision(out) == "block"


def test_unreadable_script_fails_closed(monkeypatch):
    """No script body to inspect → deny. A missing payload must not auto-run."""
    out, _err, code = _run({"args": {}, "cwd": "/repo", "tool": "Workflow"}, monkeypatch)
    assert code == 10
    assert _decision(out) == "block"


def test_script_path_is_read_and_gated(tmp_path, monkeypatch):
    path = tmp_path / "wf.js"
    path.write_text("await $`gh pr merge 9`;\n", encoding="utf-8")
    out, _err, code = _run(
        {"args": {"scriptPath": str(path)}, "cwd": str(tmp_path), "tool": "Workflow"},
        monkeypatch,
    )
    assert code == 10
    assert _decision(out) == "block"


def test_danger_inside_agent_call_expression_is_blocked(monkeypatch):
    """Arguments of agent() still evaluate. A $ template next to a prompt must not
    be blanked away with the prompt string."""
    script = (
        'await agent({ prompt: "do the thing", '
        "_: await $`gh pr merge 12 --admin` });"
    )
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 10
    assert "raw-pr-merge" in json.loads(out)["message"]


def test_agent_substring_in_string_does_not_swallow_later_danger(monkeypatch):
    script = 'const _ = "agent(";\nawait $`gh pr merge 1`;\nconst __ = ")";\n'
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 10
    assert "raw-pr-merge" in json.loads(out)["message"]


def test_git_clean_df_cluster_is_blocked(monkeypatch):
    out, _err, code = _run(_event("await $`git clean -df`;"), monkeypatch)
    assert code == 10
    assert "clean-force" in json.loads(out)["message"]


def test_git_push_uf_cluster_is_blocked(monkeypatch):
    out, _err, code = _run(_event("await $`git push -uf origin main`;"), monkeypatch)
    assert code == 10
    assert "force-push" in json.loads(out)["message"]


def test_git_c_reset_hard_is_blocked(monkeypatch):
    out, _err, code = _run(_event("await $`git -C /worktree reset --hard HEAD`;"), monkeypatch)
    assert code == 10
    assert "reset-hard" in json.loads(out)["message"]


def test_line_separator_does_not_hide_danger(monkeypatch):
    script = "//\u2028await $`gh pr merge 12 --admin`\n"
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 10
    assert "raw-pr-merge" in json.loads(out)["message"]


def test_prompt_template_interpolation_still_scanned(monkeypatch):
    """`${exec(...)}` inside a prompt template still evaluates. Blanking the
    whole template would hide it."""
    script = "return await agent({ prompt: `hi ${exec('git reset --hard HEAD')}` });"
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 10
    assert "reset-hard" in json.loads(out)["message"]


def test_backslash_newline_force_push_is_blocked(monkeypatch):
    script = "await $`git push \\\n--force origin main`;"
    out, _err, code = _run(_event(script), monkeypatch)
    assert code == 10
    assert "force-push" in json.loads(out)["message"]


def test_strip_js_comments_keeps_code():
    src = "const x = 1; // gh pr merge\n/* git reset --hard */\nconst y = 2;\n"
    stripped = gate.strip_js_comments(src)
    assert "gh pr merge" not in stripped
    assert "git reset --hard" not in stripped
    assert "const x = 1;" in stripped
    assert "const y = 2;" in stripped


def test_strip_js_comments_does_not_eat_url_in_string():
    src = 'const u = "https://example.com/path"; // trailing\n'
    stripped = gate.strip_js_comments(src)
    assert "https://example.com/path" in stripped
    assert "trailing" not in stripped


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))

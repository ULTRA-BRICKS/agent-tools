"""Tests for the block-unlanded-removal agent-hook (pre-bash).

The hook is a thin, fail-closed shim: a cheap prefilter decides whether a bash command could
remove a worktree / delete a branch, and only then does it shell out to
`rig worktree guard --cwd <cwd>` (command on stdin), which is the ONLY brain. These tests use a
FAKE `rig` executable placed first on PATH — never the real one — that records its argv/stdin
and prints canned JSON.

Run from the repo root::

    python -m pytest tests/test_block_unlanded_removal.py -q
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-unlanded-removal"
    / "block_unlanded_removal.py"
)
_DESCRIPTOR = _HOOK.parent / "block-unlanded-removal.pre-bash.json"

BLOCK_EXIT_CODE = 10

_FAKE_RIG = """#!/bin/sh
# Fake rig: records argv + stdin, prints canned stdout, honours exit/sleep knobs.
printf '%s\\n' "$*" >> "$FAKE_RIG_LOG.argv"
cat > "$FAKE_RIG_LOG.stdin"
printf 'called\\n' >> "$FAKE_RIG_LOG"
if [ -n "$FAKE_RIG_SLEEP" ]; then sleep "$FAKE_RIG_SLEEP"; fi
if [ -f "$FAKE_RIG_STDOUT_FILE" ]; then cat "$FAKE_RIG_STDOUT_FILE"; fi
exit "${FAKE_RIG_EXIT:-0}"
"""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        rig = self.bin / "rig"
        rig.write_text(_FAKE_RIG)
        rig.chmod(rig.stat().st_mode | stat.S_IXUSR)
        self.log = tmp_path / "rig.log"
        self.stdout_file = tmp_path / "rig.stdout"
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()

    def canned(self, payload: object | str) -> None:
        self.stdout_file.write_text(payload if isinstance(payload, str) else json.dumps(payload))

    def calls(self) -> int:
        return self.log.read_text().count("called") if self.log.exists() else 0

    def argv(self) -> str:
        return Path(f"{self.log}.argv").read_text().strip()

    def stdin(self) -> str:
        return Path(f"{self.log}.stdin").read_text()

    def run(
        self,
        command: str,
        *,
        path: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[dict, int, str]:
        event = {"cwd": str(self.cwd), "args": {"command": command}}
        env = {
            **os.environ,
            "PATH": path if path is not None else f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_RIG_LOG": str(self.log),
            "FAKE_RIG_STDOUT_FILE": str(self.stdout_file),
            **(extra_env or {}),
        }
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return json.loads(proc.stdout), proc.returncode, proc.stderr


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _assert_deny(out: dict, code: int) -> str:
    assert code == BLOCK_EXIT_CODE
    assert out["decision"] == "block"
    return out["message"]


# ── descriptor ───────────────────────────────────────────────────────────────────────────────

def test_descriptor_shape() -> None:
    d = json.loads(_DESCRIPTOR.read_text())
    assert d["id"] == "block-unlanded-removal"
    assert d["point"] == "pre-bash"
    assert d["priority"] == 41
    assert d["on_error"] == "closed"
    assert d["timeout_ms"] >= 130_000  # must outlast the 120s rig guard budget
    assert d["cmd"].endswith("agent-hooks/block-unlanded-removal/block_unlanded_removal.py")
    assert d["cmd"].startswith("/ABSOLUTE/PATH/TO/")


# ── prefilter: rig is NOT spawned for unrelated commands ─────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "git branch",
        "git branch -a",
        "git branch --list 'feat/*'",
        "git branch -m old new",
        "git push origin main",
        "git push -u origin feat/x",
        "git worktree add .worktrees/x feat/x",
        "git worktree list",
        "rig worktree create foo --from origin/main",
        "echo hello && git log --oneline",
        "echo $HOME",
        "git push --dry-run origin main",
        "git push --progress origin main",
        "echo ${HOME}/x && ls $PWD",
    ],
)
def test_prefilter_miss_does_not_spawn_rig(h: Harness, command: str) -> None:
    h.canned({"decision": "deny", "reason": "must not be consulted", "targets": []})
    out, code, _ = h.run(command)
    assert code == 0 and out["decision"] == "allow"
    assert h.calls() == 0


def test_empty_command_allows_without_rig(h: Harness) -> None:
    out, code, _ = h.run("   ")
    assert code == 0 and out["decision"] == "allow"
    assert h.calls() == 0


# ── prefilter: rig IS spawned for removal-shaped commands ────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "git worktree remove .worktrees/x",
        "git worktree rm .worktrees/x",
        "git worktree remove --force /tmp/x",
        "git -C /repo worktree remove x",
        "rig worktree remove x",
        "git branch -d feat/x",
        "git branch -D feat/x",
        "git branch --delete feat/x",
        "git branch --delete --force feat/x",
        "git branch -fd feat/x",
        "git branch $'-D' x",
        'git branch $"-D" x',
        "git${IFS}branch${IFS}-D${IFS}x",
        "git worktree${IFS}remove /p",
        "git $IFS branch -D x",
        "git branch --d x",
        "D=D; git branch -$D topic",
        "git branch {-D,x}",
        "git br$(true)anch -D topic",
        "B=bran; git -C . ${B}ch -D topic",
        "git -c a=b bran{ch,} -D topic",
        "D=D; git branch ';' -$D topic",
        "git branch -\\\nD x",
        "git bra -D x",
        "git br -D x",
        "git w remove /p",
        "git wo rm /p",
        "git pus --delete origin x",
        "git work remove /p",
        "git update-r -d refs/heads/x",
        "git update-ref refs/heads/topic ''",
        "git ${X:-branch} -D topic",
        "git bran{ch,} -D topic",
        "git br$*anch -D topic",
        "git -C $DIR branch -D x",
        "git push origin \"$BRANCH\"",
        "X=; git br${X}anch -D topic",
        "X=; git wor${X}ktree remove /p",
        "D=D; git bra''nch -$D topic",
        "git worktree {rm,--force} /p",
        "git worktree `echo remove` /p",
        "git branch `printf %sD -` x",
        "git branch $'-\\x44' x",
        "git push --m backup",
        "git update-ref --stdin",
        "git push origin --d x",
        "git branch -df feat/x",
        "git branch -Df feat/x",
        "git branch -dr origin/x",
        "git branch --del feat/x",
        "git push -df origin feat/x",
        "git push origin --dele feat/x",
        "git push --mirr backup",
        "git push --pru origin",
        "git update-ref -d refs/heads/x",
        "git update-ref --delete refs/heads/x",
        "git branch feat/y -D",
        "git push origin --delete feat/x",
        "git push origin :feat/x",
        "git push origin +:feat/x",
        "git push --mirror backup",
        "git push --prune origin",
        "git push -d origin feat/x",
        "git push origin feat/y :feat/x",
        "cd /repo && git worktree remove x",
        'g""it branch -D x',
        "g''it branch -D x",
        "git wor\\ktree remove x",
    ],
)
def test_prefilter_match_spawns_rig(h: Harness, command: str) -> None:
    h.canned({"decision": "pass", "reason": "", "targets": []})
    out, code, _ = h.run(command)
    assert code == 0 and out["decision"] == "allow"
    assert h.calls() == 1


# ── contract with rig: argv + stdin ──────────────────────────────────────────────────────────

def test_stdin_is_exact_command_and_cwd_is_payload_cwd(h: Harness) -> None:
    h.canned({"decision": "pass", "reason": "", "targets": []})
    command = "git branch -D 'feat/ü x'\ngit worktree remove \"a b\""
    h.run(command)
    assert h.stdin() == command
    assert h.argv() == f"worktree guard --cwd {h.cwd}"


# ── decision passthrough ─────────────────────────────────────────────────────────────────────

def test_pass_is_silent_allow(h: Harness) -> None:
    h.canned({"decision": "pass", "reason": "not a removal", "targets": []})
    out, code, _ = h.run('git commit -m "docs: git worktree remove"')
    assert code == 0
    assert out["decision"] == "allow"
    assert "message" not in out
    assert h.calls() == 1  # prefilter is deliberately loose; rig decides


def test_allow_passes_through(h: Harness) -> None:
    h.canned(
        {
            "decision": "allow",
            "reason": "all targets landed",
            "targets": [{"kind": "worktree", "target": "/r/.worktrees/x", "safe": True}],
        }
    )
    out, code, _ = h.run("git worktree remove .worktrees/x")
    assert code == 0 and out["decision"] == "allow"


def test_deny_surfaces_rig_reason_and_unsafe_targets(h: Harness) -> None:
    reason = "worktree has unpushed commits\nrescue: git diff --binary HEAD > ~/.cache/r/x.patch"
    h.canned(
        {
            "decision": "deny",
            "reason": reason,
            "targets": [
                {"kind": "worktree", "target": "/r/.worktrees/x", "safe": False},
                {"kind": "local-branch", "target": "done-branch", "safe": True},
            ],
        }
    )
    out, code, _ = h.run("git worktree remove .worktrees/x")
    msg = _assert_deny(out, code)
    assert reason in msg
    assert "/r/.worktrees/x" in msg  # unsafe target named
    assert "rig worktree verdict" in msg  # sanctioned way to inspect


def test_deny_without_reason_still_denies(h: Harness) -> None:
    h.canned({"decision": "deny", "targets": []})
    out, code, _ = h.run("git branch -D x")
    _assert_deny(out, code)


def test_no_env_escape_hatch(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "nope", "targets": []})
    out, code, _ = h.run(
        "git branch -D x",
        extra_env={"RIG_HATCH_REQUEST_BLOCK_UNLANDED_REMOVAL": "I really want to"},
    )
    _assert_deny(out, code)


# ── fail closed ──────────────────────────────────────────────────────────────────────────────

def test_rig_missing_denies(h: Harness, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    out, code, _ = h.run("git branch -D x", path=str(empty))
    msg = _assert_deny(out, code)
    assert "rig" in msg and "PATH" in msg
    assert h.calls() == 0


def test_rig_missing_but_prefilter_miss_allows(h: Harness, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    out, code, _ = h.run("git status", path=str(empty))
    assert code == 0 and out["decision"] == "allow"


def test_rig_nonzero_exit_denies_even_with_allow_json(h: Harness) -> None:
    h.canned({"decision": "allow", "reason": "ok", "targets": []})
    out, code, _ = h.run("git branch -D x", extra_env={"FAKE_RIG_EXIT": "3"})
    msg = _assert_deny(out, code)
    assert "exit" in msg and "3" in msg


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json at all",
        "[]",
        "null",
        '"allow"',
        "{}",
        '{"decision": "maybe", "reason": "?"}',
        '{"decision": 1}',
        '{"decision": "allow"} trailing garbage',
    ],
)
def test_unparseable_or_foreign_output_denies(h: Harness, stdout: str) -> None:
    h.canned(stdout)
    out, code, _ = h.run("git branch -D x")
    _assert_deny(out, code)


def test_rig_hang_denies_after_timeout(h: Harness) -> None:
    h.canned({"decision": "allow", "reason": "late", "targets": []})
    out, code, _ = h.run(
        "git branch -D x",
        extra_env={"FAKE_RIG_SLEEP": "30", "AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S": "1"},
    )
    msg = _assert_deny(out, code)
    assert "timed out" in msg


def test_garbage_timeout_env_falls_back_to_default_and_still_works(h: Harness) -> None:
    h.canned({"decision": "allow", "reason": "ok", "targets": []})
    out, code, _ = h.run(
        "git branch -D x", extra_env={"AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S": "banana"}
    )
    assert code == 0 and out["decision"] == "allow"


# ── event parsing: malformed payload on a removal-shaped command can't fail open ─────────────

def test_unparseable_event_denies(h: Harness, tmp_path: Path) -> None:
    env = {**os.environ, "PATH": f"{h.bin}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.run(
        [sys.executable, str(_HOOK)], input="not json", capture_output=True, text=True, env=env
    )
    assert proc.returncode == BLOCK_EXIT_CODE
    assert json.loads(proc.stdout)["decision"] == "block"


def test_command_fallback_fields(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "nope", "targets": []})
    env = {
        **os.environ,
        "PATH": f"{h.bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RIG_LOG": str(h.log),
        "FAKE_RIG_STDOUT_FILE": str(h.stdout_file),
    }
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"cwd": str(h.cwd), "command": "git branch -D x"}),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == BLOCK_EXIT_CODE


def test_non_string_command_denies(h: Harness) -> None:
    for payload in (
        {"cwd": str(h.cwd), "args": {"command": ["bash", "-c", "git branch -D x"]}},
        {"cwd": str(h.cwd), "args": {"command": {"a": 1}}},
        {"cwd": str(h.cwd), "args": "git branch -D x"},
    ):
        proc = subprocess.run(
            [sys.executable, str(_HOOK)], input=json.dumps(payload), capture_output=True, text=True
        )
        assert proc.returncode == BLOCK_EXIT_CODE, payload


def test_unhashable_decision_denies_with_message(h: Harness) -> None:
    h.canned('{"decision": []}')
    out, code, _ = h.run("git branch -D x")
    assert "unrecognized verdict" in _assert_deny(out, code)


def test_falsy_non_string_command_denies(h: Harness) -> None:
    for cmd in (0, [], False, {}):
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps({"cwd": str(h.cwd), "args": {"command": cmd}}),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == BLOCK_EXIT_CODE, cmd


def test_empty_primary_command_falls_back_to_next_field(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "nope", "targets": []})
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"cwd": str(h.cwd), "args": {"command": "", "cmd": "git branch -D x"}}),
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{h.bin}{os.pathsep}{os.environ['PATH']}",
             "FAKE_RIG_LOG": str(h.log), "FAKE_RIG_STDOUT_FILE": str(h.stdout_file)},
    )
    assert proc.returncode == BLOCK_EXIT_CODE


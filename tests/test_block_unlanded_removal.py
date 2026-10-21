"""Tests for the block-unlanded-removal agent-hook (pre-bash).

The hook is a thin, fail-closed shim: a cheap prefilter decides whether a bash command could
remove a worktree / delete a branch / clobber a branch / recursively delete files, and only then
does it shell out to the PINNED rig (`--rig <absolute path>`, written into the descriptor by the
`rig apply` that provisioned the hook) as `rig worktree guard --cwd <cwd>` (command on stdin),
which is the ONLY brain. These tests use a FAKE rig executable at an absolute path passed as
`--rig` — never the real one — that records its argv/stdin and prints canned JSON. A hostile
`rig` that always answers "allow" sits first on PATH in every run to prove PATH is never consulted.

Run from the repo root::

    python3 -m pytest -q tests/test_block_unlanded_removal.py -p no:cacheprovider
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
# Fake rig: records argv + stdin, prints canned stdout, honours exit/sleep/version knobs.
printf '%s\\n' "$*" >> "$FAKE_RIG_LOG.all"
if [ "$1" = "--version" ]; then
  if [ -n "$FAKE_RIG_VERSION_SLEEP" ]; then sleep "$FAKE_RIG_VERSION_SLEEP"; fi
  printf '%s\\n' "${FAKE_RIG_VERSION-rig 0.64.0}"
  exit "${FAKE_RIG_VERSION_EXIT:-0}"
fi
printf '%s\\n' "$*" >> "$FAKE_RIG_LOG.argv"
cat > "$FAKE_RIG_LOG.stdin"
printf 'called\\n' >> "$FAKE_RIG_LOG"
if [ -n "$FAKE_RIG_SLEEP" ]; then sleep "$FAKE_RIG_SLEEP"; fi
if [ -f "$FAKE_RIG_STDOUT_FILE" ]; then cat "$FAKE_RIG_STDOUT_FILE"; fi
exit "${FAKE_RIG_EXIT:-0}"
"""

# Always answers allow, to anything. Must never run: the hook may not resolve rig through PATH.
_HOSTILE_RIG = """#!/bin/sh
printf 'shadow\\n' >> "$SHADOW_LOG"
echo 'rig 9.9.9'
echo '{"decision": "allow", "reason": "hostile", "targets": []}'
"""

_STRICT = "git branch -D x"
_LOOSE = 'git commit -m "$(cat <<\'EOF\'\nfeat: heredoc message\nEOF\n)"'


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.rig_dir = tmp_path / "pinned"
        self.rig = _write_exec(self.rig_dir / "rig", _FAKE_RIG)
        self.shadow_dir = tmp_path / "shadow"
        _write_exec(self.shadow_dir / "rig", _HOSTILE_RIG)
        self.shadow_log = tmp_path / "shadow.log"
        self.log = tmp_path / "rig.log"
        self.stdout_file = tmp_path / "rig.stdout"
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()

    def canned(self, payload: object | str) -> None:
        self.stdout_file.write_text(payload if isinstance(payload, str) else json.dumps(payload))

    def calls(self) -> int:
        """Number of `rig worktree guard` invocations (not `--version` probes)."""
        return self.log.read_text().count("called") if self.log.exists() else 0

    def spawns(self) -> list[str]:
        """argv of EVERY invocation of the pinned rig, `--version` probes included."""
        path = Path(f"{self.log}.all")
        return path.read_text().splitlines() if path.exists() else []

    def argv(self) -> str:
        return Path(f"{self.log}.argv").read_text().strip()

    def stdin(self) -> str:
        return Path(f"{self.log}.stdin").read_text()

    def shadow_ran(self) -> bool:
        return self.shadow_log.exists()

    def pin(self) -> list[str]:
        return ["--rig", str(self.rig)]

    def run_event(
        self,
        event: object,
        *,
        rig_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        proc_cwd: Path | None = None,
    ) -> tuple[dict, int, str]:
        env = {
            **os.environ,
            "PATH": f"{self.shadow_dir}{os.pathsep}{os.environ['PATH']}",
            "SHADOW_LOG": str(self.shadow_log),
            "FAKE_RIG_LOG": str(self.log),
            "FAKE_RIG_STDOUT_FILE": str(self.stdout_file),
            **(extra_env or {}),
        }
        proc = subprocess.run(
            [sys.executable, str(_HOOK), *(self.pin() if rig_args is None else rig_args)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=env,
            cwd=proc_cwd,
            timeout=60,
        )
        return json.loads(proc.stdout), proc.returncode, proc.stderr

    def run(self, command: str, **kwargs: object) -> tuple[dict, int, str]:
        return self.run_event({"cwd": str(self.cwd), "args": {"command": command}}, **kwargs)


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _assert_deny(out: dict, code: int) -> str:
    assert code == BLOCK_EXIT_CODE
    assert out["decision"] == "block"
    return out["message"]


def _answers(h: Harness, decision: str = "pass") -> None:
    h.canned({"decision": decision, "reason": "", "targets": []})


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


def test_descriptor_carries_the_rig_pin_placeholder() -> None:
    d = json.loads(_DESCRIPTOR.read_text())
    assert d["args"] == ["--rig", "/ABSOLUTE/PATH/TO/rig"]


# ── prefilter: rig is NOT spawned for unrelated commands ─────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "git branch",
        "git branch -a",
        "git branch --list 'feat/*'",
        "git branch -vv",
        "git branch -r --list 'origin/*'",
        "git branch --show-current",
        "git branch -a --sort=-committerdate",
        "git branch --merged main",
        "git branch --contains abc123",
        "git branch --color=always",
        "git branch --column",
        "git branch --format='%(refname:short)'",
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
        # new-shape near misses
        "git checkout -b feat/x",
        "git checkout main",
        "git checkout -- file.txt",
        "git checkout --orphan fresh",
        "git switch -c feat/x",
        "git switch --create feat/x",
        "git switch main",
        "git switch --detach HEAD~1",
        "rm x",
        "rm -f x",
        "rm -i x",
        "rm -d empty",
        "rm --force x",
        "rmdir x",
        "rmdir -p a/b",
        "rmdir -r x",
        "form -r x",
        "arm -rf x",
        "perform -r x",
        "alarm -R x",
        "docker run --rm -it img",
        "docker run --rm -r img",
        "docker run --rm -e A=$B img",
        "perform $X",
        "arm ${Y}",
        "rmdir $DIR",
        "echo rm",
        "grep -r term .",
        "ls -R",
        "ls -C /tmp",
        "tar -C /tmp -xf x.tar",
        "cc -B /lib x.c",
        "chmod -B x y",
        "bash -c 'echo hi'",
        "sort -C data.txt",
        "ssh -C host true",
        "git branch --create-reflog x",
    ],
)
def test_prefilter_miss_does_not_spawn_rig(h: Harness, command: str) -> None:
    h.canned({"decision": "deny", "reason": "must not be consulted", "targets": []})
    out, code, _ = h.run(command)
    assert code == 0 and out["decision"] == "allow"
    assert h.spawns() == []  # no version probe, no guard: rig is not spawned at all


def test_prefilter_miss_allows_even_without_any_pin(h: Harness) -> None:
    out, code, _ = h.run("git status", rig_args=[])
    assert code == 0 and out["decision"] == "allow"
    assert h.spawns() == []


def test_empty_command_allows_without_rig(h: Harness) -> None:
    out, code, _ = h.run("   ")
    assert code == 0 and out["decision"] == "allow"
    assert h.spawns() == []


# ── prefilter: STRICT shapes (removal-shaped on their face) ──────────────────────────────────

_OLD_STRICT = [
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
    "git br$(true)anch -D topic",
    "git -c a=b bran{ch,} -D topic",
    "B=bran; git -C . ${B}ch -D topic",
    "git bran{ch,} -D topic",
    "git br$*anch -D topic",
    "X=; git br${X}anch -D topic",
    "X=; git wor${X}ktree remove /p",
    "git branch -\\\nD x",
    "git bra -D x",
    "git br -D x",
    "git w remove /p",
    "git wo rm /p",
    "git pus --delete origin x",
    "git work remove /p",
    "git update-r -d refs/heads/x",
    "git update-ref refs/heads/topic ''",
    "git update-ref --stdin",
    "git -C $DIR branch -D x",
    "git push --m backup",
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
]

# Matched ONLY by the expansion-syntax clause: the shell may be assembling the destructive word.
_OLD_LOOSE = [
    "D=D; git branch -$D topic",
    "git branch {-D,x}",
    "git branch $'-\\x44' x",
    "git worktree `echo remove` /p",
    "git branch `printf %sD -` x",
    "D=D; git branch ';' -$D topic",
    "git ${X:-branch} -D topic",
    "git push origin \"$BRANCH\"",
    "D=D; git bra''nch -$D topic",
    "git worktree {rm,--force} /p",
    _LOOSE,
]

# `rm` whose recursive flag may be assembled by the shell (review finding): LOOSE like the git ones.
_NEW_LOOSE = [
    "R=r; rm -$R x",
    "rm -${F} x",
    "rm {-r,x}",
    "rm \"$tmp\"",
    "F=-rf; rm $F dir",
    "rm `echo -$X` x",
    "sudo rm $(cat flags) x",
]

_NEW_STRICT = [
    # (a) git branch force / move / copy
    "git branch -f x HEAD~1",
    "git branch --force x HEAD~1",
    "git branch -M old new",
    "git branch -C src dst",
    "git branch -m old new",
    "git branch -c src dst",
    "git branch -fm old new",
    "git branch --forc x y",
    "git branch --mov old new",
    "git branch --move old new",
    "git branch --cop src dst",
    "git branch --copy src dst",
    "git branch x y -f",
    "git bra -f x y",
    "git -C /r branch -M old new",
    # (b) checkout -B / switch -C
    "git checkout -B x origin/x",
    "git checkout -fB x origin/x",
    "git chec -B x",
    "git -C /r checkout -B x origin/main",
    "git switch -C x origin/x",
    "git switch --force-create x origin/x",
    "git switch --force-c x",
    "git sw -C x",
    # (c) rm with a recursive flag, as a command word
    "rm -rf x",
    "rm -r x",
    "rm -R x",
    "rm -fr x",
    "rm -Rf x",
    "rm -f -r x",
    "rm x -rf",
    "rm --recursive x",
    "rm --rec x",
    "sudo rm -r x",
    "xargs rm -rf",
    "find . -name '*.pyc' | xargs rm -rf",
    "\\rm -rf x",
    "command rm -rf x",
    "env rm -rf x",
    "/bin/rm -rf x",
    "'rm' -rf x",
    "r\\m -rf x",
    "echo ok; rm -rf x",
    "git rm -r x",
    "$(command -v rm) -rf x",
]


@pytest.mark.parametrize("command", _OLD_STRICT + _OLD_LOOSE + _NEW_STRICT + _NEW_LOOSE)
def test_prefilter_match_spawns_rig(h: Harness, command: str) -> None:
    _answers(h)
    out, code, _ = h.run(command)
    assert code == 0 and out["decision"] == "allow"
    assert h.calls() == 1


@pytest.mark.parametrize("command", _OLD_STRICT + _NEW_STRICT)
def test_strict_shape_is_denied_by_a_stale_rig(h: Harness, command: str) -> None:
    out, code, _ = h.run(command, extra_env={"FAKE_RIG_VERSION": "rig 0.63.0"})
    assert "older than 0.64.0" in _assert_deny(out, code)
    assert h.calls() == 0


@pytest.mark.parametrize("command", _OLD_LOOSE + _NEW_LOOSE)
def test_loose_shape_is_allowed_by_a_stale_rig(h: Harness, command: str) -> None:
    out, code, err = h.run(command, extra_env={"FAKE_RIG_VERSION": "rig 0.63.0"})
    assert code == 0 and out["decision"] == "allow"
    assert "older than 0.64.0" in err
    assert h.calls() == 0


# ── contract with rig: argv + stdin ──────────────────────────────────────────────────────────

def test_stdin_is_exact_command_and_cwd_is_payload_cwd(h: Harness) -> None:
    _answers(h)
    command = "git branch -D 'feat/ü x'\ngit worktree remove \"a b\""
    h.run(command)
    assert h.stdin() == command
    assert h.argv() == f"worktree guard --cwd {h.cwd}"


def test_version_probe_then_guard_on_the_pinned_binary(h: Harness) -> None:
    _answers(h)
    h.run(_STRICT)
    assert h.spawns() == ["--version", f"worktree guard --cwd {h.cwd}"]


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


def test_current_rig_denies_a_loose_command_too(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "nope", "targets": []})
    out, code, _ = h.run(_LOOSE)
    _assert_deny(out, code)
    assert h.calls() == 1


def test_no_env_escape_hatch(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "nope", "targets": []})
    out, code, _ = h.run(
        "git branch -D x",
        extra_env={"RIG_HATCH_REQUEST_BLOCK_UNLANDED_REMOVAL": "I really want to"},
    )
    _assert_deny(out, code)


# ── pinned rig: the only rig the hook will ever run ──────────────────────────────────────────

def test_pinned_rig_is_used_not_a_rig_earlier_on_path(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "pinned says no", "targets": []})
    out, code, _ = h.run(_STRICT)  # hostile `rig` (always allow) is first on PATH
    assert "pinned says no" in _assert_deny(out, code)
    assert not h.shadow_ran()


@pytest.mark.parametrize("command", [_STRICT, _LOOSE])
def test_missing_pin_never_falls_back_to_rig_on_path(h: Harness, command: str) -> None:
    out, code, _ = h.run(command, rig_args=[])
    assert "rig apply commit" in _assert_deny(out, code)
    assert not h.shadow_ran()
    assert h.spawns() == []


_NO_PIN = "passes no `--rig <absolute path>`"
_NOT_ABSOLUTE = "is not absolute"
_NOT_A_FILE = "does not exist or is not a regular file"
_NOT_EXECUTABLE = "is not executable"


def _bad_pins(h: Harness, tmp_path: Path) -> dict[str, tuple[list[str], str]]:
    """label -> (hook argv, the problem the deny message must name)."""
    not_exec = tmp_path / "plain" / "rig"
    not_exec.parent.mkdir()
    not_exec.write_text(_FAKE_RIG)
    not_exec.chmod(0o644)
    return {
        "absent": ([], _NO_PIN),
        "no value": (["--rig"], _NO_PIN),
        "empty value": (["--rig", ""], _NOT_ABSOLUTE),
        "relative path": (["--rig", "rig"], _NOT_ABSOLUTE),
        "dot-relative path": (["--rig", "./rig"], _NOT_ABSOLUTE),
        "nonexistent": (["--rig", str(tmp_path / "nope" / "rig")], _NOT_A_FILE),
        "directory": (["--rig", str(h.rig_dir)], _NOT_A_FILE),
        "not executable": (["--rig", str(not_exec)], _NOT_EXECUTABLE),
        "equals form": ([f"--rig={h.rig}"], _NO_PIN),
        "wrong option name": (["--rigg", str(h.rig)], _NO_PIN),
        "short option": (["-r", str(h.rig)], _NO_PIN),
        "extra trailing arg": (["--rig", str(h.rig), "--extra"], _NO_PIN),
        "extra leading arg": (["--extra", "--rig", str(h.rig)], _NO_PIN),
        "duplicate": (["--rig", str(h.rig), "--rig", str(h.rig)], _NO_PIN),
        "placeholder never substituted": (["--rig", "/ABSOLUTE/PATH/TO/rig"], _NOT_A_FILE),
    }


@pytest.mark.parametrize("command", [_STRICT, _LOOSE])
def test_unusable_pin_denies_both_classes(h: Harness, tmp_path: Path, command: str) -> None:
    for label, (rig_args, problem) in _bad_pins(h, tmp_path).items():
        out, code, _ = h.run(command, rig_args=rig_args, proc_cwd=h.rig_dir)
        msg = _assert_deny(out, code)
        assert problem in msg, label
        assert "rig apply commit" in msg, label
    assert h.spawns() == []
    assert not h.shadow_ran()


def test_relative_pin_is_not_resolved_against_cwd(h: Harness) -> None:
    # `./rig` exists relative to the hook's cwd; it must still be refused as non-absolute.
    out, code, _ = h.run(_STRICT, rig_args=["--rig", "rig"], proc_cwd=h.rig_dir)
    _assert_deny(out, code)
    assert h.spawns() == []


def test_symlinked_pin_to_an_executable_is_accepted(h: Harness, tmp_path: Path) -> None:
    link = tmp_path / "link-to-rig"
    link.symlink_to(h.rig)
    _answers(h)
    out, code, _ = h.run(_STRICT, rig_args=["--rig", str(link)])
    assert code == 0 and out["decision"] == "allow"
    assert h.calls() == 1


# ── version gate (MIN_RIG_VERSION 0.64.0, integer-tuple compare) ─────────────────────────────

@pytest.mark.parametrize("version", ["0.63.0", "0.63.9", "0.9.0", "0.1.0", "0.0.1"])
def test_stale_rig_denies_strict_with_upgrade_message(h: Harness, version: str) -> None:
    out, code, _ = h.run(_STRICT, extra_env={"FAKE_RIG_VERSION": f"rig {version}"})
    msg = _assert_deny(out, code)
    assert version in msg
    assert "older than 0.64.0" in msg
    assert "worktree guard" in msg
    assert "rig apply commit" in msg
    assert h.calls() == 0


@pytest.mark.parametrize("version", ["0.63.0", "0.9.0"])
def test_stale_rig_allows_loose_heredoc_commit_with_warning(h: Harness, version: str) -> None:
    out, code, err = h.run(_LOOSE, extra_env={"FAKE_RIG_VERSION": f"rig {version}"})
    assert code == 0 and out["decision"] == "allow"
    assert "older than 0.64.0" in err and version in err
    assert h.calls() == 0


@pytest.mark.parametrize(
    "version", ["0.64.0", "0.64.1", "0.65.0", "0.100.0", "0.1000.0", "1.0.0", "10.0.0", "0.64.0-dev"]
)
def test_current_rig_is_consulted(h: Harness, version: str) -> None:
    _answers(h)
    for command in (_STRICT, _LOOSE):
        out, code, err = h.run(command, extra_env={"FAKE_RIG_VERSION": f"rig {version}"})
        assert code == 0 and out["decision"] == "allow"
        assert "older" not in err
    assert h.calls() == 2


def test_numeric_not_lexical_compare_0_100_vs_0_9(h: Harness) -> None:
    # lexically "0.100.0" < "0.64.0" and "0.9.0" > "0.64.0"; numerically the opposite.
    h.canned({"decision": "deny", "reason": "guard ran", "targets": []})
    out, code, _ = h.run(_STRICT, extra_env={"FAKE_RIG_VERSION": "rig 0.100.0"})
    assert "guard ran" in _assert_deny(out, code)
    out, code, _ = h.run(_STRICT, extra_env={"FAKE_RIG_VERSION": "rig 0.9.0"})
    assert "older than 0.64.0" in _assert_deny(out, code)


@pytest.mark.parametrize(
    "version",
    ["", "garbage", "rig", "rig vNext", "rig 0.64", "0.64.0", "rig 0.64.0.1", "rig a.b.c", "rigx 0.99.0", "rig -1.0.0"],
)
@pytest.mark.parametrize("command", [_STRICT, _LOOSE])
def test_unverifiable_version_fails_closed_for_both_classes(h: Harness, command: str, version: str) -> None:
    out, code, _ = h.run(command, extra_env={"FAKE_RIG_VERSION": version})
    msg = _assert_deny(out, code)
    assert "version" in msg and "rig apply commit" in msg
    assert h.calls() == 0


@pytest.mark.parametrize("command", [_STRICT, _LOOSE])
def test_failing_version_probe_fails_closed(h: Harness, command: str) -> None:
    out, code, _ = h.run(command, extra_env={"FAKE_RIG_VERSION_EXIT": "2"})
    _assert_deny(out, code)
    assert h.calls() == 0


@pytest.mark.parametrize("command", [_STRICT, _LOOSE])
def test_hanging_version_probe_fails_closed(h: Harness, command: str) -> None:
    out, code, _ = h.run(
        command,
        extra_env={
            "FAKE_RIG_VERSION_SLEEP": "30",
            "AGENT_TOOLS_REMOVAL_GUARD_VERSION_TIMEOUT_S": "1",
        },
    )
    assert "timed out" in _assert_deny(out, code)


def test_stale_rig_with_prefilter_miss_is_silent(h: Harness) -> None:
    out, code, err = h.run("git status", extra_env={"FAKE_RIG_VERSION": "rig 0.1.0"})
    assert code == 0 and out["decision"] == "allow"
    assert err == "" and h.spawns() == []


# ── fail closed (old behaviours, now through the pinned rig) ─────────────────────────────────

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

def _raw(h: Harness, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_HOOK), *h.pin()], input=stdin, capture_output=True, text=True, timeout=60
    )


def test_unparseable_event_denies(h: Harness) -> None:
    proc = _raw(h, "not json")
    assert proc.returncode == BLOCK_EXIT_CODE
    assert json.loads(proc.stdout)["decision"] == "block"


def test_command_fallback_fields(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "nope", "targets": []})
    out, code, _ = h.run_event({"cwd": str(h.cwd), "command": "git branch -D x"})
    assert "nope" in _assert_deny(out, code)


def test_non_string_command_denies(h: Harness) -> None:
    for payload in (
        {"cwd": str(h.cwd), "args": {"command": ["bash", "-c", "git branch -D x"]}},
        {"cwd": str(h.cwd), "args": {"command": {"a": 1}}},
        {"cwd": str(h.cwd), "args": "git branch -D x"},
    ):
        assert _raw(h, json.dumps(payload)).returncode == BLOCK_EXIT_CODE, payload


def test_unhashable_decision_denies_with_message(h: Harness) -> None:
    h.canned('{"decision": []}')
    out, code, _ = h.run("git branch -D x")
    assert "unrecognized verdict" in _assert_deny(out, code)


def test_falsy_non_string_command_denies(h: Harness) -> None:
    for cmd in (0, [], False, {}):
        payload = {"cwd": str(h.cwd), "args": {"command": cmd}}
        assert _raw(h, json.dumps(payload)).returncode == BLOCK_EXIT_CODE, cmd


def test_empty_primary_command_falls_back_to_next_field(h: Harness) -> None:
    h.canned({"decision": "deny", "reason": "nope", "targets": []})
    out, code, _ = h.run_event(
        {"cwd": str(h.cwd), "args": {"command": "", "cmd": "git branch -D x"}}
    )
    assert "nope" in _assert_deny(out, code)

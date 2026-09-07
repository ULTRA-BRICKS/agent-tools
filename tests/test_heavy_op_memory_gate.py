"""Tests for the heavy-op-memory-gate agent-hook (pre-bash, hard block).

Prevents STARTING a heavy operation (extension rebuild, multi-model `review` pass,
build/test suite) while the machine's real memory pressure (macOS jetsam level via
`sysctl kern.memorystatus_vm_pressure_level`) is at WARN-or-worse — added after the
2026-08-27/28 incident where concurrent heavy agent work drove free memory to ~65MB
on a 24GB machine. Deliberately stateless (no counter/lock/queue): every call
re-reads pressure fresh. See the hook module's own docstring for the full rationale,
including why this is NOT a concurrency semaphore and carries no hatch-escalation.

Detection is TOKEN-based (see `classify_heavy_operation` / `_tokenize`), not a raw
substring search — an earlier regex-over-raw-string version was caught in review
hard-blocking `git commit -m "make all tests green"` (the word "make" trapped
inside a quoted commit message). This suite's false-positive tests pin that fix.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_heavy_op_memory_gate.py -q
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
    / "heavy-op-memory-gate"
    / "heavy_op_memory_gate.py"
)
_spec = importlib.util.spec_from_file_location("heavy_op_memory_gate", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# ── classify_heavy_operation — pure detection ─────────────────────────────────────

@pytest.mark.parametrize("command", [
    "./vscode-extension/hypercanvas-preview/build-and-install.sh",
    "./vscode-extension/hypercanvas-preview/build-and-install.sh patch",
    "npx @vscode/vsce package --out out.vsix",
    "pnpm dlx @vscode/vsce package",
    "vsce package",
    "review diff -C /repo",
    "review quorum \"is this safe\" -C /repo",
    "review brainstorm \"topic\" -C /repo",
    "review just-ask \"q\" -C /repo",
    "npm test",
    "npm run build",
    "pnpm build",
    "yarn test",
    "bun test",
    "cargo test",
    "go build ./...",
    "make test",
    "make all",
    "rake build",
    "mvn verify",
    "gradle package",
    "playwright test",
    "npx playwright test e2e/",
    "pytest tests/",
    "vitest run",
    "jest --coverage",
    "cypress run",
])
def test_classifies_heavy_operations(command):
    assert hook.classify_heavy_operation(command) is not None, command


@pytest.mark.parametrize("command", [
    "git status",
    "ls -la",
    "npm --version",
    "review --help",
    "cat package.json",
    "npm run lint",
])
def test_allows_non_heavy_commands(command):
    assert hook.classify_heavy_operation(command) is None, command


@pytest.mark.parametrize("command", [
    "review diff --help",
    "review diff -h",
    "review quorum --help",
    "review brainstorm --help",
    "review just-ask --help",
    'review diff --help 2>&1 | grep -iE -A3 "role" | head -60',
    "vsce package --help",
    "./vscode-extension/hypercanvas-preview/build-and-install.sh --help",
])
def test_help_only_invocation_of_a_heavy_command_is_not_heavy(command):
    """Real-incident regression (2026-09, `landing` session): `review diff
    --help` was hard-blocked by this gate for 19+ hours even though it never
    starts a multi-model pass — it just prints usage and exits. `-h`/`--help`
    short-circuits a real CLI to that behavior regardless of which heavy
    subcommand comes before it, so it must never classify as heavy."""
    assert hook.classify_heavy_operation(command) is None, command


def test_help_flag_in_a_later_unrelated_pipe_segment_does_not_exempt_a_real_heavy_call():
    """The false-NEGATIVE guard on the fix above: `-h` belongs to `grep`, not
    `review`, in this pipeline — the `review diff` segment itself requests no
    help, so it must still classify as heavy. Scoping the exemption to the
    matched invocation's OWN shell segment (not "anywhere in the command") is
    what keeps this the safe direction — see `_segment_requests_help`."""
    command = "review diff -C . | grep -h pattern"
    assert hook.classify_heavy_operation(command) == "multi-model review-cli pass"


@pytest.mark.parametrize("command", [
    'git commit -m "make all tests green"',
    'gh pr comment 12 --body "gradle verify failed"',
    'task new --title "fix go build flake"',
    'tg "review diff finished, all clear"',
])
def test_heavy_words_trapped_inside_a_quoted_argument_never_match(command):
    """The exact false-positive class review caught before this hook shipped:
    a heavy-operation word is one substring of a LARGER quoted shlex token
    (the commit message / PR body / title), never a bare token by itself."""
    assert hook.classify_heavy_operation(command) is None, command


def test_bare_unquoted_tokens_are_still_flagged_documented_edge_case():
    """`echo review diff` has "review" and "diff" as genuinely separate, bare
    argv tokens — token-level detection can't distinguish that from a real
    `review diff` invocation without full command-position parsing (which this
    hook deliberately doesn't do — see module docstring). Different failure
    class from the quoted-argument bug above: this is unquoted-token ambiguity,
    accepted at the same posture the sibling enforce-timeout-on-bash accepts."""
    assert hook.classify_heavy_operation("echo review diff") is not None


def test_unparseable_command_falls_back_to_whitespace_split_not_raw_substring():
    """Unbalanced quotes make shlex.split raise; the fallback must stay
    token-shaped (whitespace split), not regress to a raw substring search."""
    unparseable = 'echo "unterminated quote review diff'
    # whitespace-split tokens: ['echo', '"unterminated', 'quote', 'review', 'diff']
    # "review" and "diff" ARE separate whitespace tokens here, so this still
    # matches — the guarantee is "no crash + still token-based", not "never
    # matches on malformed input".
    assert hook.classify_heavy_operation(unparseable) is not None


# ── read_pressure_level — platform DISPATCH (not fallback-chain) ─────────────────

def test_read_pressure_level_dispatches_to_macos_reader_on_darwin():
    calls = []
    hook.read_pressure_level(
        macos_reader=lambda: (calls.append("macos"), 4)[1],
        linux_reader=lambda: (calls.append("linux"), 1)[1],
        platform_name="Darwin",
    )
    assert calls == ["macos"]


def test_read_pressure_level_dispatches_to_linux_reader_on_linux():
    calls = []
    hook.read_pressure_level(
        macos_reader=lambda: (calls.append("macos"), 4)[1],
        linux_reader=lambda: (calls.append("linux"), 2)[1],
        platform_name="Linux",
    )
    assert calls == ["linux"]


def test_read_pressure_level_does_not_cross_fall_back():
    """A None from the platform's own reader is NOT chased with the other
    platform's reader — dispatch is exclusive, by design (see docstring)."""
    result = hook.read_pressure_level(macos_reader=lambda: None, linux_reader=lambda: 2, platform_name="Darwin")
    assert result is None


def test_read_pressure_level_none_when_reader_unavailable():
    assert hook.read_pressure_level(macos_reader=lambda: None, linux_reader=lambda: None, platform_name="Darwin") is None


def test_linux_pressure_reader_maps_avg10_to_levels():
    def make_reader(text):
        return lambda path: text

    critical_text = "some avg10=75.00 avg60=50.00 avg300=20.00 total=123\nfull avg10=10.00\n"
    warn_text = "some avg10=25.00 avg60=10.00 avg300=5.00 total=123\nfull avg10=1.00\n"
    normal_text = "some avg10=2.00 avg60=1.00 avg300=0.50 total=123\nfull avg10=0.00\n"

    assert hook._read_linux_pressure_level(read_text=make_reader(critical_text)) == 4
    assert hook._read_linux_pressure_level(read_text=make_reader(warn_text)) == 2
    assert hook._read_linux_pressure_level(read_text=make_reader(normal_text)) == 1


def test_linux_pressure_reader_none_on_missing_file():
    def raiser(path):
        raise OSError("no such file")

    assert hook._read_linux_pressure_level(read_text=raiser) is None


def test_linux_pressure_reader_none_on_unparseable_content():
    assert hook._read_linux_pressure_level(read_text=lambda p: "garbage, no psi fields here") is None


# ── macOS reader — the one function that actually runs in production ─────────────

class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_macos_reader_parses_a_valid_level():
    assert hook._read_macos_pressure_level(run=lambda *a, **k: _FakeCompletedProcess("2\n")) == 2


def test_macos_reader_none_on_nonzero_returncode():
    assert hook._read_macos_pressure_level(run=lambda *a, **k: _FakeCompletedProcess("", returncode=1)) is None


def test_macos_reader_none_on_unparseable_stdout():
    assert hook._read_macos_pressure_level(run=lambda *a, **k: _FakeCompletedProcess("not-a-number\n")) is None


def test_macos_reader_none_when_sysctl_missing():
    def raiser(*a, **k):
        raise FileNotFoundError("sysctl not found")

    assert hook._read_macos_pressure_level(run=raiser) is None


# ── _block_at_level — env override, restricted to the real jetsam levels ─────────

def test_block_at_level_default_when_unset(monkeypatch):
    monkeypatch.delenv("RIG_HEAVY_OP_BLOCK_AT_LEVEL", raising=False)
    assert hook._block_at_level() == hook.DEFAULT_BLOCK_AT_LEVEL


@pytest.mark.parametrize("value", [1, 2, 4])
def test_block_at_level_accepts_real_jetsam_levels(monkeypatch, value):
    monkeypatch.setenv("RIG_HEAVY_OP_BLOCK_AT_LEVEL", str(value))
    assert hook._block_at_level() == value


@pytest.mark.parametrize("value", ["3", "5", "999", "0", "-1"])
def test_block_at_level_rejects_out_of_range_values(monkeypatch, value):
    """A value outside {1,2,4} would make `level < block_at` true for every
    real reading — a silent total bypass of the documented no-bypass contract.
    Must fall back to the safe default, not honor the out-of-range value."""
    monkeypatch.setenv("RIG_HEAVY_OP_BLOCK_AT_LEVEL", value)
    assert hook._block_at_level() == hook.DEFAULT_BLOCK_AT_LEVEL


def test_block_at_level_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("RIG_HEAVY_OP_BLOCK_AT_LEVEL", "not-a-number")
    assert hook._block_at_level() == hook.DEFAULT_BLOCK_AT_LEVEL


# ── decide — the pure decision core (takes a LABEL, not a raw command) ───────────

def test_allows_when_label_is_none_regardless_of_pressure():
    decision, message = hook.decide(None, level=4, block_at=2)
    assert decision == "allow"
    assert message is None


def test_allows_a_heavy_label_when_pressure_is_normal():
    decision, _message = hook.decide("build/test suite", level=1, block_at=2)
    assert decision == "allow"


def test_allows_a_heavy_label_when_pressure_signal_is_unavailable():
    """None must fail OPEN, never be treated as 'critical' or 'normal'."""
    decision, _message = hook.decide("multi-model review-cli pass", level=None, block_at=2)
    assert decision == "allow"


def test_blocks_a_heavy_label_at_the_warn_threshold():
    decision, message = hook.decide("multi-model review-cli pass", level=2, block_at=2)
    assert decision == "block"
    assert "review-cli" in message.lower()
    assert "no bypass" in message.lower()


def test_blocks_a_heavy_label_at_critical():
    decision, message = hook.decide("VS Code extension rebuild/package", level=4, block_at=2)
    assert decision == "block"
    assert "extension" in message.lower()


def test_does_not_block_below_a_raised_threshold():
    """A custom, stricter block_at=4 should allow a merely-warn (level=2) machine."""
    decision, _ = hook.decide("build/test suite", level=2, block_at=4)
    assert decision == "allow"


def test_block_message_does_not_hardcode_a_specific_person():
    """This ships to every repo/machine in the catalog — must stay generic."""
    _decision, message = hook.decide("build/test suite", level=4, block_at=2)
    assert "alex" not in message.lower()


# ── main() end-to-end wiring (stdin/stdout protocol, env override) ────────────────

def _run(command, monkeypatch, *, macos_level, env: dict | None = None) -> tuple[str, str, int]:
    monkeypatch.setattr(hook, "_read_macos_pressure_level", lambda run=None: macos_level)
    monkeypatch.setattr(hook, "_read_linux_pressure_level", lambda read_text=None: None)
    monkeypatch.setattr(hook.platform, "system", lambda: "Darwin")
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": {"command": command}})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("RIG_HEAVY_OP_BLOCK_AT_LEVEL", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def test_main_blocks_heavy_op_under_warn_pressure(monkeypatch):
    out, _err, code = _run("review diff -C /repo", monkeypatch, macos_level=2)
    assert code == hook.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert payload["hook_api"] == hook.HOOK_API


def test_main_allows_heavy_op_under_normal_pressure(monkeypatch):
    out, _err, code = _run("review diff -C /repo", monkeypatch, macos_level=1)
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_main_allows_light_command_under_critical_pressure(monkeypatch):
    out, _err, code = _run("git status", monkeypatch, macos_level=4)
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_main_never_reads_pressure_for_a_non_heavy_command(monkeypatch):
    """Perf/correctness fix: a light command must not spawn `sysctl` at all."""
    read_calls = []
    monkeypatch.setattr(hook, "read_pressure_level", lambda *a, **k: (read_calls.append(1), None)[1])
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": {"command": "git status"}})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    code = hook.main()
    assert code == 0
    assert read_calls == []


def test_main_reads_pressure_for_a_heavy_command(monkeypatch):
    read_calls = []
    monkeypatch.setattr(hook, "read_pressure_level", lambda *a, **k: (read_calls.append(1), 1)[1])
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": {"command": "npm test"}})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    code = hook.main()
    assert code == 0
    assert read_calls == [1]


def test_main_respects_env_override_for_block_threshold(monkeypatch):
    # block_at=4 (critical only) — a merely-warn (2) machine should now ALLOW.
    out, _err, code = _run("npm test", monkeypatch, macos_level=2, env={"RIG_HEAVY_OP_BLOCK_AT_LEVEL": "4"})
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_main_invalid_env_override_falls_back_to_default(monkeypatch):
    out, _err, code = _run("npm test", monkeypatch, macos_level=2, env={"RIG_HEAVY_OP_BLOCK_AT_LEVEL": "not-a-number"})
    assert code == hook.BLOCK_EXIT_CODE  # default threshold (2) still applies


def test_main_out_of_range_env_override_cannot_bypass_a_critical_machine(monkeypatch):
    """The bypass this closes: RIG_HEAVY_OP_BLOCK_AT_LEVEL=999 used to make
    even a critical (4) machine ALLOW. Must still BLOCK at the safe default."""
    out, _err, code = _run("npm test", monkeypatch, macos_level=4, env={"RIG_HEAVY_OP_BLOCK_AT_LEVEL": "999"})
    assert code == hook.BLOCK_EXIT_CODE
    assert json.loads(out)["decision"] == "block"


def test_main_fails_open_on_unparseable_stdin(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_main_applies_to_orchestrator_and_subagent_alike(monkeypatch):
    """No agent_id gating — unlike subagent-no-bg-longproc, this fires for BOTH."""
    monkeypatch.setattr(hook, "_read_macos_pressure_level", lambda run=None: 4)
    monkeypatch.setattr(hook, "_read_linux_pressure_level", lambda read_text=None: None)
    monkeypatch.setattr(hook.platform, "system", lambda: "Darwin")
    for agent_id in (None, "sub-1"):
        args = {"command": "npm run build"}
        if agent_id is not None:
            args["agent_id"] = agent_id
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args})))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        code = hook.main()
        assert code == hook.BLOCK_EXIT_CODE, f"agent_id={agent_id!r}"


def test_main_non_string_args_does_not_crash(monkeypatch):
    """A harness that ever sends `args` as a list/int instead of a dict must
    still fail open, not raise — mirrors the unparseable-stdin guarantee."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": ["not", "a", "dict"]})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"

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
import os
import subprocess
import sys
import time
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


@pytest.fixture(autouse=True)
def _isolated_gpu_slots_and_thresholds(tmp_path, monkeypatch):
    """Never probe the developer's real machine-wide GPU slots, and never let an ambient
    threshold override change what a test exercises."""
    monkeypatch.setenv("RIG_GPU_SLOT_DIR", str(tmp_path / "gpu-slots"))
    monkeypatch.setenv("RIG_GPU_SLOTS", "2")
    for name in ("RIG_GPU_SLOT_FD", "RIG_HEAVY_OP_MAX_WIRED_PCT", "RIG_HEAVY_OP_MIN_AVAILABLE_MB"):
        monkeypatch.delenv(name, raising=False)


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


@pytest.mark.parametrize("command", [
    "review diff -C . | grep -h pattern",
    "review diff -C . & grep -h pattern",
    "review diff -C . |& grep -h pattern",
])
def test_help_flag_in_a_later_unrelated_pipe_segment_does_not_exempt_a_real_heavy_call(command):
    """The false-NEGATIVE guard on the fix above: `-h` belongs to `grep`, not
    `review`, in these pipelines — the `review diff` segment itself requests no
    help, so it must still classify as heavy. Scoping the exemption to the
    matched invocation's OWN shell segment (not "anywhere in the command") is
    what keeps this the safe direction — see `_segment_requests_help`.

    `&` (backgrounds the preceding command) and `|&` (bash's stdout+stderr
    pipe) are the two boundary operators a code-review pass (P2) caught
    missing from the first version of this fix — `review diff -C . & grep -h
    pattern` would have silently exempted the backgrounded `review diff`
    without them."""
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


# ── GPU model loads (agent-tools#817) — each one wires GPU memory the jetsam level never sees ──

@pytest.mark.parametrize("command", [
    # The 2026-09-29 panic's exact shape: path-qualified whisper-cli with a ggml model.
    "/Users/ultra/xp/whisper.cpp/build/bin/whisper-cli -m /Users/ultra/xp/whisper.cpp/models/ggml-large-v3-turbo.bin -f /tmp/a.wav",
    "stt /tmp/a.wav",
    # One agent command, many model loads — the loop the panic's subagents ran.
    'for i in $(seq 1 40); do say "phrase $i" -o /tmp/$i.aiff; stt /tmp/$i.aiff; done',
    "for f in *.wav\ndo\n  stt \"$f\"\ndone",
    "cd /tmp && stt a.wav",
    "whisper-server -m models/ggml-base.en.bin --port 8080",
    "llama-cli -m ~/models/qwen2.5-7b-instruct-q4_k_m.gguf -p hi",
    "/opt/homebrew/bin/llama-server --model ~/models/x.gguf",
    "mlx_lm.generate --model mlx-community/Llama-3.2-3B-Instruct-4bit --prompt hi",
    "python3 -m mlx_lm generate --model mlx-community/x",
    "mlx_whisper a.wav --model mlx-community/whisper-large-v3-turbo",
    "uv run python -m mlx_whisper a.wav",
    # Match by the model FILE, not only by a known binary name.
    "./main -m models/ggml-large-v3-turbo.bin -f a.wav",
    "python3 infer.py --weights=/models/model.safetensors",
    "ls models/ && ./server -m models/q.gguf",
    "bash -lc 'stt /tmp/a.wav'",
    "nohup stt a.wav > out.txt 2>&1 &",
    # Process substitution opens a subshell, not a redirect: its first word is a command.
    "diff <(stt a.wav) expected.txt",
    "cat <(stt a.wav)",
    # Command substitution runs its body first, even as an argument of a file utility
    # or inside double quotes.
    'echo "$(stt a.wav)"',
    "echo `stt a.wav`",
    'text="$(stt a.wav)"',
    "printf '%s\\n' \"$(whisper-cli -m ggml.bin -f a.wav)\"",
    'echo "$(cat "$(stt a.wav)")"',
    # The substitution runs in the calling shell, before the wrapper takes a slot.
    'agenttools-gpu-slot run -- echo "$(stt a.wav)"',
    # A comment ends at its newline; the next line is a new command.
    "ls models/ # listing first\n./main -m models/ggml-base.bin -f a.wav",
    # A quoted `#` is an argument, not a comment.
    './main --prompt "#1 take" -m models/ggml-base.bin -f a.wav',
    # Shell options before -c must not hide the script.
    "bash -o pipefail -c 'stt a.wav'",
    "sh -eu -c 'whisper-cli -m x.bin -f a.wav'",
    # A heredoc handed to a shell is a script: its lines run.
    "bash <<'EOF'\nfor f in *.wav; do stt \"$f\"; done\nEOF",
    "cat <<'EOF' | sh\nstt a.wav\nEOF",
    # A load after a heredoc's terminator is a command again.
    "cat > notes.md <<'EOF'\nnotes\nEOF\nstt a.wav",
    # An unquoted delimiter expands the body like a double-quoted string: its substitutions
    # run, markdown backticks included.
    "cat > pr.md <<EOF\nRun `stt a.wav` to reproduce\nEOF",
    "cat <<EOF\nresult: $(whisper-cli -m ggml.bin -f a.wav)\nEOF",
    # A here-string or an arithmetic shift is not a heredoc opener: the lines after it are
    # commands, not a body.
    "read -r ans <<< yes\nstt a.wav",
    "cat <<<README\nwhisper-cli -m ggml.bin -f a.wav",
    "size=$((1<<shift))\nwhisper-cli -m ggml-large-v3-turbo.bin -f a.wav",
    "(( mask = 1<<bits ))\nstt a.wav",
    # `-h` as an option's VALUE (a prompt) is not a request for help.
    'llama-cli -m ~/x.gguf -p "-h"',
    "whisper-cli -m x.bin --prompt --help -f a.wav",
    # `find -exec` runs its command: the exec'd load still counts.
    "find . -name '*.wav' -exec stt {} \\;",
    "find . -name '*.wav' -execdir whisper-cli -m /m/x.bin -f {} +",
])
def test_classifies_gpu_model_loads(command):
    assert hook.classify_heavy_operation(command) == hook.GPU_MODEL_LABEL, command


@pytest.mark.parametrize("command", [
    "ls -la ~/xp/whisper.cpp/models/*.bin",
    "shasum -a 256 ggml-large-v3-turbo.bin",
    "curl -L -o models/ggml-base.bin https://example.invalid/ggml-base.bin",
    "rm -f /tmp/q.gguf",
    "cd ~/.bin",
    "cd ~/xp/stt-cli && git status",
    "stt --help",
    "whisper-cli -h",
    "echo stt a.wav",
    'git commit -m "wire stt through the gpu slot pool"',
    "ls models/ # next: whisper-cli -m models/x.bin",
    "pip install mlx_whisper",
    "python3 train.py --epochs 3 # uses weights.bin later",
    "grep -c token models/ggml-base.bin",
    "diff a.gguf b.gguf",
    "echo '$(stt a.wav)'",
    'echo "\\$(stt a.wav)"',
    "which stt whisper-cli",
    "type whisper-server",
    "man whisper-cli",
    # A heredoc body is data on stdin, not commands: a PR body or notes file that mentions
    # a model run loads nothing.
    "cat > /tmp/notes.md <<'EOF'\nThe stt run hung\nwhisper-cli -m ggml.bin -f a.wav\nEOF",
    "gh pr create --title t --body \"$(cat <<'EOF'\nstt a.wav hangs\nEOF\n)\"",
    "ssh box <<EOF\nstt a.wav\nEOF",
    "cat > pr.md <<'EOF'\nRun `stt a.wav` to reproduce\nEOF",
    # Process control and inspection never load a model: blocking them while every slot is
    # taken would block the remediation for a runaway load.
    "pkill -f whisper-cli",
    "pgrep -fl whisper-server",
    "killall whisper-cli",
    "kill $(pgrep -f whisper-cli)",
    "ps aux | grep whisper-cli",
    # File utilities and build tools: a model-file-shaped name is only a file here.
    "find models -name '*.gguf'",
    "gcc -o fw.bin fw.c",
    "objdump -d fw.bin",
    "dd if=firmware.bin of=/tmp/copy.bin",
    "tree node_modules/.bin",
    # `-h`/`--help` given to the model command itself.
    "whisper-cli -m x.bin -h",
    "sudo stt --help",
])
def test_non_loading_commands_mentioning_models_are_not_gpu_loads(command):
    assert hook.classify_heavy_operation(command) != hook.GPU_MODEL_LABEL, command


@pytest.mark.parametrize("command", [
    "python3 /opt/agent-tools/lib/agenttools_gpu_slots run -- whisper-cli -m ggml.bin -f a.wav",
    "agenttools-gpu-slot run --timeout 600 -- stt a.wav",
    "for f in *.wav; do python3 -m agenttools_gpu_slots run -- stt \"$f\"; done",
    "agenttools-gpu-slot run -- bash -c 'stt a.wav && stt b.wav'",
    'out="$(agenttools-gpu-slot run -- stt a.wav)"',
    "agenttools-gpu-slot run -- bash -c 'text=$(stt a.wav); echo \"$text\"'",
])
def test_gpu_loads_through_the_slot_wrapper_are_labelled_slot_gated(command):
    assert hook.classify_heavy_operation(command) == hook.GPU_MODEL_SLOTTED_LABEL, command


def test_one_unwrapped_load_next_to_a_wrapped_one_is_still_ungated():
    command = "agenttools-gpu-slot run -- stt a.wav; stt b.wav"
    assert hook.classify_heavy_operation(command) == hook.GPU_MODEL_LABEL


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


# ── memory snapshot — wired share + free/reclaimable pages (agent-tools#817) ─────
# GPU buffers (Metal) are WIRED: neither pageable nor compressible, so the jetsam level stays
# "normal" while wired memory eats the whole machine. The 2026-09-29 panic had 95.7% wired,
# 75 MB free, and `memoryPressure: false`.

_MEMSIZE_24_GIB = 25_769_803_776  # the panic machine's hw.memsize ("25.77 GB")

# Page counts copied from the panic report's memoryStatus (16 KiB pages).
_PANIC_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     4610.
Pages active:                                   2223.
Pages inactive:                                 2000.
Pages speculative:                               227.
Pages throttled:                                   0.
Pages wired down:                            1504714.
Pages purgeable:                                   0.
"Translation faults":                      605230114.
Pages copy-on-write:                        26533417.
File-backed pages:                              1100.
Anonymous pages:                                3350.
Pages stored in compressor:                   630564.
Pages occupied by compressor:                  52000.
"""

# A busy but healthy 24 GiB Mac: only ~94 MB free (macOS keeps free low on purpose), but
# ~6 GB inactive and ~3 GB of file cache the OS reclaims on demand, and normal wired share.
_HEALTHY_LOW_FREE_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     6000.
Pages active:                                 396467.
Pages inactive:                               385793.
Pages speculative:                             10964.
Pages throttled:                                   0.
Pages wired down:                             294412.
Pages purgeable:                                6516.
File-backed pages:                            205253.
Anonymous pages:                              587971.
"""

# Wired share is moderate (40%), but free + reclaimable is only ~226 MB.
_LOW_AVAILABLE_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     4000.
Pages active:                                 900000.
Pages inactive:                                10000.
Pages speculative:                               500.
Pages wired down:                             629146.
Pages purgeable:                                   0.
"""


def _fake_macos_run(vm_stat_text: str):
    def run(argv, **_kwargs):
        if argv == ["vm_stat"]:
            return _FakeCompletedProcess(vm_stat_text)
        raise AssertionError(f"unexpected command {argv}")

    return run


def _snapshot(vm_stat_text: str):
    snap = hook._read_macos_memory_snapshot(
        run=_fake_macos_run(vm_stat_text), physical_memory=lambda: _MEMSIZE_24_GIB
    )
    assert snap is not None
    return snap


def test_macos_snapshot_reads_the_panic_page_counts():
    snap = _snapshot(_PANIC_VM_STAT)
    assert snap.total_bytes == _MEMSIZE_24_GIB
    assert snap.wired_bytes == 1_504_714 * 16384
    # free + inactive + speculative + purgeable: the ticket's "only 112 MB here".
    assert snap.available_bytes == (4610 + 2000 + 227 + 0) * 16384
    assert round(snap.wired_pct, 1) == 95.7


def test_macos_snapshot_none_when_vm_stat_is_unreadable():
    def run(argv, **_kwargs):
        return _FakeCompletedProcess("", returncode=1)

    ram = lambda: _MEMSIZE_24_GIB  # noqa: E731
    assert hook._read_macos_memory_snapshot(run=run, physical_memory=ram) is None
    assert hook._read_macos_memory_snapshot(run=_fake_macos_run("garbage\n"), physical_memory=ram) is None


def test_macos_snapshot_none_when_physical_memory_is_unreadable():
    snap = hook._read_macos_memory_snapshot(run=_fake_macos_run(_PANIC_VM_STAT), physical_memory=lambda: None)
    assert snap is None


def test_slow_path_subprocess_timeouts_fit_inside_the_hook_timeout():
    """The dispatcher kills a hook at its descriptor's timeout_ms and then applies
    on_error=open, i.e. ALLOWS. So every subprocess the hook may run before deciding
    must fit in that budget together, or a slow, memory-starved machine (exactly when
    this gate matters) turns into a silent allow."""
    timeouts = []

    def run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        return _FakeCompletedProcess(_PANIC_VM_STAT if argv == ["vm_stat"] else "1\n")

    hook._read_macos_pressure_level(run=run)
    hook._read_macos_memory_snapshot(run=run, physical_memory=lambda: _MEMSIZE_24_GIB)
    descriptor = json.loads(_HOOK.with_name("heavy-op-memory-gate.pre-bash.json").read_text())
    assert sum(timeouts) < descriptor["timeout_ms"] / 1000


_MEMINFO = """\
MemTotal:       16384000 kB
MemFree:          200000 kB
MemAvailable:    6000000 kB
Buffers:           10000 kB
Cached:          5000000 kB
Unevictable:       30000 kB
Mlocked:           30000 kB
SUnreclaim:       100000 kB
KernelStack:       20000 kB
PageTables:        50000 kB
"""


def test_linux_snapshot_reads_proc_meminfo():
    snap = hook._read_linux_memory_snapshot(read_text=lambda path: _MEMINFO)
    assert snap is not None
    assert snap.total_bytes == 16384000 * 1024
    assert snap.available_bytes == 6000000 * 1024
    # Unevictable (mlocked included) + unreclaimable slab + kernel stacks + page tables.
    assert snap.wired_bytes == (30000 + 100000 + 20000 + 50000) * 1024


def test_linux_snapshot_none_without_memavailable_or_file():
    assert hook._read_linux_memory_snapshot(read_text=lambda path: "MemTotal: 100 kB\n") is None

    def raiser(path):
        raise OSError("no /proc")

    assert hook._read_linux_memory_snapshot(read_text=raiser) is None


def test_read_memory_snapshot_dispatches_by_platform():
    mac, lin = object(), object()
    assert hook.read_memory_snapshot(macos_reader=lambda: mac, linux_reader=lambda: lin, platform_name="Darwin") is mac
    assert hook.read_memory_snapshot(macos_reader=lambda: mac, linux_reader=lambda: lin, platform_name="Linux") is lin


@pytest.mark.parametrize("raw", ["97", "1", "99"])
def test_max_wired_pct_env_override(monkeypatch, raw):
    monkeypatch.setenv("RIG_HEAVY_OP_MAX_WIRED_PCT", raw)
    assert hook._max_wired_pct() == int(raw)


@pytest.mark.parametrize("raw", ["100", "150", "0", "-5", "lots"])
def test_max_wired_pct_rejects_values_that_would_disable_or_break_the_check(monkeypatch, raw):
    monkeypatch.setenv("RIG_HEAVY_OP_MAX_WIRED_PCT", raw)
    assert hook._max_wired_pct() == hook.DEFAULT_MAX_WIRED_PCT


def test_min_available_mb_env_override(monkeypatch):
    monkeypatch.setenv("RIG_HEAVY_OP_MIN_AVAILABLE_MB", "100")
    assert hook._min_available_mb() == 100


@pytest.mark.parametrize("raw", ["0", "-1", "none"])
def test_min_available_mb_rejects_values_that_would_disable_the_check(monkeypatch, raw):
    monkeypatch.setenv("RIG_HEAVY_OP_MIN_AVAILABLE_MB", raw)
    assert hook._min_available_mb() == hook.DEFAULT_MIN_AVAILABLE_MB


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


def test_the_2026_09_29_panic_snapshot_blocks_at_normal_jetsam_level():
    decision, message = hook.decide(hook.GPU_MODEL_LABEL, level=1, block_at=2, memory=_snapshot(_PANIC_VM_STAT))
    assert decision == "block"
    assert "wired" in message.lower()
    assert "95.7%" in message


def test_healthy_snapshot_with_low_free_but_large_inactive_and_file_cache_allows():
    decision, message = hook.decide(
        "build/test suite", level=1, block_at=2, memory=_snapshot(_HEALTHY_LOW_FREE_VM_STAT)
    )
    assert (decision, message) == ("allow", None)


def test_low_free_plus_reclaimable_blocks_even_with_a_moderate_wired_share():
    decision, message = hook.decide("build/test suite", level=1, block_at=2, memory=_snapshot(_LOW_AVAILABLE_VM_STAT))
    assert decision == "block"
    assert "reclaimable" in message.lower()


def test_both_signals_unavailable_fails_open():
    assert hook.decide(hook.GPU_MODEL_LABEL, level=None, block_at=2, memory=None) == ("allow", None)


def test_memory_snapshot_blocks_even_when_the_pressure_signal_is_unavailable():
    decision, _ = hook.decide("build/test suite", level=None, block_at=2, memory=_snapshot(_PANIC_VM_STAT))
    assert decision == "block"


# ── main() end-to-end wiring (stdin/stdout protocol, env override) ────────────────

def _run(command, monkeypatch, *, macos_level, memory=None, env: dict | None = None) -> tuple[str, str, int]:
    monkeypatch.setattr(hook, "_read_macos_memory_snapshot", lambda run=None: memory)
    monkeypatch.setattr(hook, "_read_linux_memory_snapshot", lambda read_text=None: None)
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
    monkeypatch.setattr(hook, "read_memory_snapshot", lambda *a, **k: None)
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


# ── main() — wired-memory exhaustion and the machine-wide GPU slot pool (agent-tools#817) ──

def test_main_blocks_the_2026_09_29_panic_snapshot_at_normal_pressure(monkeypatch):
    out, _err, code = _run("stt /tmp/a.wav", monkeypatch, macos_level=1, memory=_snapshot(_PANIC_VM_STAT))
    assert code == hook.BLOCK_EXIT_CODE
    assert json.loads(out)["decision"] == "block"


def test_main_memory_thresholds_are_env_overridable(monkeypatch):
    # 95.7% wired < 97 and 112 MB available > 100 MB: the loosened thresholds let it through.
    out, _err, code = _run(
        "stt /tmp/a.wav",
        monkeypatch,
        macos_level=1,
        memory=_snapshot(_PANIC_VM_STAT),
        env={"RIG_HEAVY_OP_MAX_WIRED_PCT": "97", "RIG_HEAVY_OP_MIN_AVAILABLE_MB": "100"},
    )
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_main_allows_a_heavy_op_on_a_healthy_low_free_machine(monkeypatch):
    out, _err, code = _run("npm test", monkeypatch, macos_level=1, memory=_snapshot(_HEALTHY_LOW_FREE_VM_STAT))
    assert code == 0


_SLOTS_LIB = _HOOK.parents[2] / "lib" / "agenttools_gpu_slots"
_FAKE_MODEL = "import time; print('ready', flush=True); time.sleep(120)"


@pytest.fixture
def gpu_slot_holders():
    """Start N real holder processes through the slot wrapper (a fake model that just sleeps),
    each in its own process, exactly like N agents' whisper runs."""
    procs = []

    def start(count: int) -> list:
        for _ in range(count):
            proc = subprocess.Popen(
                [sys.executable, str(_SLOTS_LIB), "run", "--timeout", "0", "--", sys.executable, "-c", _FAKE_MODEL],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(proc)
            assert proc.stdout.readline().strip() == b"ready", proc.stderr.read().decode()
        return procs

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


_PANIC_COMMAND = "/Users/ultra/xp/whisper.cpp/build/bin/whisper-cli -m models/ggml-large-v3-turbo.bin -f /tmp/a.wav"


def test_main_refuses_a_gpu_load_at_once_while_all_slots_are_taken(monkeypatch, gpu_slot_holders):
    holders = gpu_slot_holders(2)
    started = time.monotonic()
    out, _err, code = _run(_PANIC_COMMAND, monkeypatch, macos_level=1, memory=_snapshot(_HEALTHY_LOW_FREE_VM_STAT))
    elapsed = time.monotonic() - started
    assert code == hook.BLOCK_EXIT_CODE
    message = json.loads(out)["message"]
    assert "2 of 2" in message
    assert f"pid={holders[0].pid}" in message
    assert "agenttools_gpu_slots run --" in message  # the actionable way to queue instead
    assert elapsed < 1.0, "the hook must refuse immediately, never wait for a slot"


def test_pre_bash_hook_process_refuses_without_waiting_while_all_slots_are_taken(tmp_path, gpu_slot_holders):
    """The real hook executable, as a harness runs it: separate process, JSON on stdin,
    exit 10 well inside the descriptor's 5 s timeout."""
    gpu_slot_holders(2)
    env = dict(os.environ)
    started = time.monotonic()
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"args": {"command": "stt /tmp/a.wav"}}),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    elapsed = time.monotonic() - started
    assert res.returncode == hook.BLOCK_EXIT_CODE, res.stderr
    assert "GPU model slots" in json.loads(res.stdout)["message"]
    assert elapsed < 3.0


def test_main_allows_a_gpu_load_when_a_slot_is_free_and_the_probe_keeps_nothing(monkeypatch, gpu_slot_holders):
    gpu_slot_holders(1)
    out, _err, code = _run(_PANIC_COMMAND, monkeypatch, macos_level=1, memory=_snapshot(_HEALTHY_LOW_FREE_VM_STAT))
    assert code == 0
    slots_lib = hook._load_gpu_slots()
    assert slots_lib.free_slot_count(slots_lib.slot_dir(), 2) == 1


def test_main_lets_a_slot_wrapped_gpu_load_queue_while_all_slots_are_taken(monkeypatch, gpu_slot_holders):
    """The wrapper WAITS for a slot, so the hook must not refuse it — that is the queueing path."""
    gpu_slot_holders(2)
    command = f"python3 {_SLOTS_LIB} run -- {_PANIC_COMMAND}"
    out, _err, code = _run(command, monkeypatch, macos_level=1, memory=_snapshot(_HEALTHY_LOW_FREE_VM_STAT))
    assert code == 0


def test_main_still_memory_gates_a_slot_wrapped_gpu_load(monkeypatch):
    command = f"python3 {_SLOTS_LIB} run -- {_PANIC_COMMAND}"
    out, _err, code = _run(command, monkeypatch, macos_level=1, memory=_snapshot(_PANIC_VM_STAT))
    assert code == hook.BLOCK_EXIT_CODE


def test_main_does_not_apply_gpu_slots_to_non_gpu_heavy_ops(monkeypatch, gpu_slot_holders):
    gpu_slot_holders(2)
    out, _err, code = _run("npm test", monkeypatch, macos_level=1, memory=_snapshot(_HEALTHY_LOW_FREE_VM_STAT))
    assert code == 0

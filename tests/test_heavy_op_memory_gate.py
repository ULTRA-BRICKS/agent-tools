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
    # No developer install (~/.local/bin/review, ~/.agents) may decide what a test sees.
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    for name in (
        "RIG_GPU_SLOT_FD", "RIG_HEAVY_OP_MAX_WIRED_PCT", "RIG_HEAVY_OP_MIN_AVAILABLE_MB",
        "RIG_HEAVY_OP_MAX_PSI_SOME10", "RIG_HEAVY_OP_MIN_SWAP_FREE_PCT", "RIG_HEAVY_OP_MAX_LIVE",
        "RIG_HEAVY_OP_HEADROOM_MB", "RIG_HEAVY_OP_COST_REVIEW_MB", "RIG_HEAVY_OP_COST_DOCKER_MB",
        "RIG_HEAVY_OP_COST_SUITE_MB", "REVIEW_ADMISSION",
    ):
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


# ── runpod-run offload: the suite after `--` runs on a Runpod pod, not here ──────────

_RUNPOD_RUN_HEADER = (
    "#!/usr/bin/env bash\n"
    "# runpod-run — run one heavy command (test suite, build, benchmark) on a Runpod pod\n"
)


@pytest.fixture
def offload_script(tmp_path, monkeypatch):
    """A real-looking runpod-run install: the skill script, a `~/.local/bin` symlink to it,
    and that dir on PATH (as on a rig-managed machine). Returns the skill script's path."""
    home = tmp_path / "home"
    skill = home / ".agents" / "skills" / "runpod-offload" / "runpod-run"
    skill.parent.mkdir(parents=True)
    skill.write_text(_RUNPOD_RUN_HEADER + "exec ssh pod \"$@\"\n")
    skill.chmod(0o755)
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)  # the review_cli fixture may have made it
    (local_bin / "runpod-run").symlink_to(skill)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{local_bin}:{os.environ.get('PATH', '')}")
    return skill


@pytest.mark.parametrize("command", [
    "runpod-run -- bun test ./tests",
    "runpod-run --ttl 30 -- bun test ./tests",
    "~/.local/bin/runpod-run --cpu cpu3c-8 -- pytest -q",
    "{skill} --gpu 'NVIDIA GeForce RTX 4090' -- python bench.py",
    "RUNPOD_RUN_STATE=/tmp/rr runpod-run --ttl 30 -- npm test",
    "CI=1 timeout -k 30 3700 runpod-run -- cargo test --workspace",
    "nice -n 5 runpod-run -- make all",
    "cd /repo && runpod-run --ttl 30 -- bun test ./tests 2>&1 | tail -50",
    "runpod-run -- bash -c 'bun test && pytest'",
    "runpod-run -- stt a.wav",
    # Re-pointing syntax in the REMOTE command runs on the pod, not here (review finding).
    "runpod-run -- bash -c 'export PATH=/opt/bin:$PATH && make test'",
    "runpod-run --ttl 30 -- bash -c 'f(){ make \"$@\"; }; f test'",
    "runpod-run -- env PATH=/opt/bin bun test",
])
def test_runpod_run_offloads_are_not_local_heavy_operations(command, offload_script):
    command = command.replace("{skill}", str(offload_script))
    assert hook.classify_heavy_operation(command) is None, command


@pytest.mark.parametrize("command", [
    "runpod-run -- bun test ./tests && bun test",
    "bun test; runpod-run --ttl 30 -- bun test",
    "runpod-run -- true | pytest -q",
    "runpod-run -- echo \"$(npm test)\"",
    "runpod-run -- echo `pytest`",
    "runpod-run -- x; review diff -C .",
    "echo runpod-run && bun test",  # runpod-run as an operand is not the program
])
def test_heavy_commands_chained_around_runpod_run_still_run_locally(command, offload_script):
    assert hook.classify_heavy_operation(command) is not None, command


def test_help_flag_of_runpod_run_does_not_exempt_a_chained_heavy_command(offload_script):
    assert hook.classify_heavy_operation("runpod-run --help; review diff -C .") is not None
    assert hook.classify_heavy_operation("runpod-run -- ls -h; review diff -C .") is not None


@pytest.mark.parametrize("command", [
    'function runpod-run { shift; "$@"; }; runpod-run -- bun test ./tests',
    # zsh expands the function NAME (review finding): any function definition disqualifies.
    'dash=-; function runpod${dash}run(){ shift; "$@"; }; runpod-run -- bun test ./tests',
    'dash=-; runpod${dash}run () { shift; "$@"; }; runpod-run -- bun test ./tests',
    "eval 'f(){ :; }'; runpod-run -- bun test ./tests",
    # Sourced shell code is invisible to the hook and may define the function (review finding).
    "source /tmp/evil; runpod-run -- bun test ./tests",
    ". /tmp/evil && runpod-run -- bun test ./tests",
    # zsh: the `path` array and the `functions` table re-point a name too (review finding).
    "path=(/tmp/evil $path); runpod-run -- bun test ./tests",
    "functions[runpod-run]='shift; \"$@\"'; runpod-run -- bun test ./tests",
    "export HOME=/tmp/evil; ~/.local/bin/runpod-run -- bun test ./tests",
    "sudo runpod-run -- bun test ./tests",  # resolved through sudo's PATH, not ours (review finding)
    "command -p runpod-run -- bun test ./tests",  # the shell's default PATH (review finding)
    "builtin source /tmp/evil; runpod-run -- bun test ./tests",
    "path[1]=/tmp/evil; runpod-run -- bun test ./tests",  # zsh, element-wise (review finding)
    "PATH[0]=/tmp/evil; runpod-run -- bun test ./tests",
    # zsh re-points via declaration/array builtins and fpath too (review finding).
    "set -A path /tmp/evil $path; runpod-run -- bun test ./tests",
    "typeset path=(/tmp/evil); runpod-run -- bun test ./tests",
    "typeset -U path; path+=/tmp/evil; runpod-run -- bun test ./tests",
    "declare -x PATH; runpod-run -- bun test ./tests",
    "local path; runpod-run -- bun test ./tests",
    "readonly 'PATH'=/tmp/evil; runpod-run -- bun test ./tests",
    "unset path; runpod-run -- bun test ./tests",
    "path+=/tmp/evil; runpod-run -- bun test ./tests",
    "fpath=(/tmp/evil $fpath); runpod-run -- bun test ./tests",
    "FPATH=/tmp/evil runpod-run -- bun test ./tests",
    'runpod-run() { shift; "$@"; }; runpod-run -- bun test ./tests',
    "'runpod-run'() { shift; \"$@\"; }; runpod-run -- bun test ./tests",  # the shell removes the quotes
    "alias runpod-run='env'; runpod-run -- bun test ./tests",
    "alias 'runpod-run'=env; runpod-run -- bun test ./tests",
    "PATH=/tmp/evil:$PATH runpod-run -- bun test ./tests",
    "PATH=/tmp/evil:$PATH; runpod-run -- bun test ./tests",  # an earlier segment re-points it
    "export PATH=/tmp/evil:$PATH && runpod-run -- bun test ./tests",
    "hash -p /tmp/evil/runpod-run runpod-run; runpod-run -- bun test ./tests",
    "./runpod-run -- bun test ./tests",  # relative: whatever sits in the cwd
    "{fake} -- bun test ./tests",  # named runpod-run, but not the installed script
    "{forged} -- bun test ./tests",  # carries the real header, still not the installed script
    "{symlink} -- bun test ./tests",  # a runpod-run symlink to python
])
def test_spoofed_runpod_run_is_not_exempt(command, offload_script, tmp_path):
    """Review findings: the name alone, or the script's header, is spoofable — each of these
    runs the suite locally."""
    fake = tmp_path / "evil" / "runpod-run"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nshift; exec \"$@\"\n")
    forged = tmp_path / "forged" / "runpod-run"
    forged.parent.mkdir()
    forged.write_text(_RUNPOD_RUN_HEADER + "shift; exec \"$@\"\n")
    symlink = tmp_path / "evil2" / "runpod-run"
    symlink.parent.mkdir()
    symlink.symlink_to(sys.executable)
    for name, path in (("{fake}", fake), ("{forged}", forged), ("{symlink}", symlink)):
        command = command.replace(name, str(path))
    assert hook.classify_heavy_operation(command) is not None, command


def test_the_runpod_run_on_path_must_be_the_installed_script(monkeypatch, tmp_path):
    forged = tmp_path / "runpod-run"
    forged.write_text(_RUNPOD_RUN_HEADER + "shift; exec \"$@\"\n")
    forged.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert hook.classify_heavy_operation("runpod-run -- bun test ./tests") is not None


def test_the_checkout_copy_of_runpod_run_is_trusted():
    """The skill script in this hook's own agent-tools checkout is the other trusted install."""
    script = _HOOK.parents[2] / "skills" / "universal" / "runpod-offload" / "runpod-run"
    assert hook.classify_heavy_operation(f"{script} -- bun test ./tests") is None


def _register_worktree(repo: Path, worktree: Path, name: str = "feat") -> None:
    """What `git worktree add` records: `<repo>/.git/worktrees/<name>/gitdir` → `<worktree>/.git`,
    and `<worktree>/.git` → `gitdir: <repo>/.git/worktrees/<name>`."""
    entry = repo / ".git" / "worktrees" / name
    entry.mkdir(parents=True)
    (entry / "gitdir").write_text(f"{worktree / '.git'}\n")
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {entry}\n")


def test_install_and_worktrees_from_a_linked_worktree(tmp_path):
    """Review finding: a root that is itself a linked worktree (`.git` is a file) still reaches
    its main checkout and its sibling worktrees."""
    main, first, second = tmp_path / "main", tmp_path / "wt" / "a", tmp_path / "wt" / "b"
    _register_worktree(main, first, "a")
    _register_worktree(main, second, "b")
    assert hook._install_and_worktrees(first) == {main, first, second}
    assert hook._install_and_worktrees(main) == {main, first, second}
    assert hook._install_and_worktrees(tmp_path / "plain") == {tmp_path / "plain"}


def test_a_registered_worktree_copy_of_runpod_run_is_trusted(monkeypatch, tmp_path):
    checkout = tmp_path / "agent-tools"
    worktree = checkout / ".worktrees" / "feat"
    worktree_script = worktree / "skills" / "universal" / "runpod-offload" / "runpod-run"
    worktree_script.parent.mkdir(parents=True)
    worktree_script.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(hook, "__file__", str(checkout / "agent-hooks" / "heavy-op-memory-gate" / "heavy_op_memory_gate.py"))
    command = f"{worktree_script} -- bun test ./tests"
    assert hook.classify_heavy_operation(command) is not None, "a directory under .worktrees/ is not a worktree"
    _register_worktree(checkout, worktree)
    assert hook.classify_heavy_operation(command) is None
    other = tmp_path / "elsewhere" / ".worktrees" / "feat" / "skills" / "universal" / "runpod-offload" / "runpod-run"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    assert hook.classify_heavy_operation(f"{other} -- bun test ./tests") is not None


def test_runpod_run_not_on_path_is_not_exempt(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert hook.classify_heavy_operation("runpod-run -- bun test ./tests") is not None


# ── review-cli handoff: review-cli admits its own runs (review-cli#614) ───────────────

_FAKE_ADMISSION = '''\
import os
from types import SimpleNamespace


def load_settings(config=None, env=None):
    env = os.environ if env is None else env
    if env.get("FAKE_ADMISSION_RAISE"):
        raise RuntimeError("broken config")
    return SimpleNamespace(enabled=env.get("REVIEW_ADMISSION", "1") not in ("0", "false", "off", "no"))
'''


def _review_checkout(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "reviewlib").mkdir()
    (root / "bin" / "review").write_text("#!/usr/bin/env python3\n")
    (root / "bin" / "review").chmod(0o755)
    (root / "reviewlib" / "__init__.py").write_text("")
    (root / "reviewlib" / "admission.py").write_text(_FAKE_ADMISSION)
    return root


@pytest.fixture
def review_cli(tmp_path, monkeypatch):
    """An installed review-cli with the admission controller: a checkout whose
    `reviewlib.admission.load_settings()` answers from REVIEW_ADMISSION, the `~/.local/bin/review`
    symlink to its `bin/review`, that dir on PATH. Returns the root. The hook imports
    `reviewlib` from it, so `sys.path` and `sys.modules` are restored afterwards."""
    home = tmp_path / "home"
    root = _review_checkout(home / "work" / "review-cli")
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    (local_bin / "review").symlink_to(root / "bin" / "review")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{local_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sys, "path", list(sys.path))
    stale = [name for name in sys.modules if name == "reviewlib" or name.startswith("reviewlib.")]
    for name in stale:
        monkeypatch.delitem(sys.modules, name)
    yield root
    for name in [name for name in sys.modules if name == "reviewlib" or name.startswith("reviewlib.")]:
        del sys.modules[name]


@pytest.mark.parametrize("command", [
    "review diff --staged --task 633",
    "review quorum --task 1 'q'",
    "timeout 1800 review diff --staged -C .",
    "~/.local/bin/review brainstorm x",
    "{root}/bin/review just-ask q",
    "cd /repo && review diff --staged 2>&1 | tail -40",
])
def test_installed_review_cli_with_admission_takes_the_review(command, review_cli):
    assert hook.review_cli_handoff(command.replace("{root}", str(review_cli))) is not None, command


def test_a_registered_review_cli_worktree_takes_the_review(review_cli):
    worktree = _review_checkout(review_cli / ".worktrees" / "feat")
    command = f"{worktree}/bin/review diff --staged"
    assert hook.review_cli_handoff(command) is None, "a directory under .worktrees/ is not a worktree"
    _register_worktree(review_cli, worktree)
    assert hook.review_cli_handoff(command) == ""


def test_review_handoff_keeps_other_heavy_work_in_the_command(review_cli):
    rest = hook.review_cli_handoff("review diff --staged && bun test")
    assert rest == "bun test"
    assert hook.classify_heavy_operation(rest) == "build/test suite"


@pytest.mark.parametrize("command", [
    "review() { shift; \"$@\"; }; review diff",
    "function review { :; }; review diff",
    "r=review; function ${r} { :; }; review diff",
    "alias 'review'=env; review diff",
    "PATH=/tmp/evil:$PATH; review diff",
    "set -A path /tmp/evil $path; review diff",  # zsh (review finding)
    "typeset path=(/tmp/evil); review diff",
    "path+=/tmp/evil; review diff",
    "hash -p /tmp/evil/review review; review diff",
    "./bin/review diff",  # relative
    "{fake}/bin/review diff",  # a look-alike checkout with an admission controller, not the installed one
    "python3 {root}/bin/review diff",  # not the program position
    "REVIEW_ADMISSION=0 review diff",
    "env REVIEW_ADMISSION=false review diff",
    "export REVIEW_ADMISSION=off; review diff",
    "REVIEW_ADMISSION=$OFF review diff",  # expands to anything; only a literal true is trusted
    "REVIEW_ADMISSION=\"$(cat flag)\" review diff",
    "REVIEW_ADMISSION=1#x review diff",  # not a literal true to review-cli's parser
    "source ~/.zshrc; review diff",
    "HOME=/tmp/other review diff",  # would read another config than the one asked
    "sudo review diff",  # another user's PATH and config (review finding)
    "env review diff",
    "XDG_CONFIG_HOME=/tmp/x review diff",
])
def test_review_is_not_handed_off_when_it_may_not_be_admitted(command, review_cli, tmp_path):
    fake = _review_checkout(tmp_path / "fake-review-cli")
    command = command.replace("{fake}", str(fake)).replace("{root}", str(review_cli))
    assert hook.review_cli_handoff(command) is None, command


def test_review_cli_without_the_admission_controller_is_not_handed_off(review_cli):
    (review_cli / "reviewlib" / "admission.py").unlink()
    assert hook.review_cli_handoff("review diff --staged") is None


def test_review_cli_decides_whether_admission_is_on(review_cli, monkeypatch):
    """review-cli's own `load_settings()` answers (its YAML and env semantics); any failure
    there means no handoff (review finding: a regex over its config missed valid YAML)."""
    monkeypatch.setenv("REVIEW_ADMISSION", "0")
    assert hook.review_cli_handoff("review diff") is None
    monkeypatch.delenv("REVIEW_ADMISSION")
    monkeypatch.setenv("FAKE_ADMISSION_RAISE", "1")
    assert hook.review_cli_handoff("review diff") is None
    monkeypatch.delenv("FAKE_ADMISSION_RAISE")
    assert hook.review_cli_handoff("review diff") == ""


@pytest.mark.parametrize("command", [
    "REVIEW_ADMISSION=1 review diff",
    "export REVIEW_ADMISSION=on; review diff",
    "review diff; export REVIEW_ADMISSION=0",
    "unset REVIEW_ADMISSION; review diff",
    "env -u REVIEW_ADMISSION review diff",
    "env -i review diff",
    "REVIEW_ADMISSION_HEADROOM=0 review diff",
    "REVIEW_X=1 review diff",
])
def test_a_command_that_may_change_the_reviews_environment_is_not_handed_off(command, review_cli):
    """review-cli is asked with this hook's environment; a command that may give the review
    another one is gated here as before (review findings: export order, `unset`, `env -u`)."""
    assert hook.review_cli_handoff(command) is None, command


def test_runpod_run_remote_tail_does_not_stop_a_review_handoff(review_cli, offload_script):
    command = "runpod-run -- bash -c 'export PATH=/opt/bin:$PATH && make test' && review diff --staged"
    rest = hook.review_cli_handoff(command)
    assert rest is not None and hook.classify_heavy_operation(rest) is None


def test_review_cli_output_never_reaches_the_protocol_stdout(review_cli, monkeypatch):
    admission = review_cli / "reviewlib" / "admission.py"
    admission.write_text("print('noise')\n" + _FAKE_ADMISSION)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert hook.review_cli_handoff("review diff") == ""
    assert out.getvalue() == ""


def test_no_installed_review_cli_means_no_handoff(tmp_path):
    fake = _review_checkout(tmp_path / "rc")
    assert hook.review_cli_handoff(f"{fake}/bin/review diff") is None


# ── read_pressure_level — platform DISPATCH (not fallback-chain) ─────────────────

def test_read_pressure_level_dispatches_to_macos_reader_on_darwin():
    calls = []
    hook.read_pressure_level(
        macos_reader=lambda: (calls.append("macos"), 4)[1],
        platform_name="Darwin",
    )
    assert calls == ["macos"]


def test_read_pressure_level_is_none_on_linux():
    """Linux has no jetsam level; its pressure signal is the raw PSI reading."""
    calls = []
    assert hook.read_pressure_level(macos_reader=lambda: (calls.append("macos"), 4)[1], platform_name="Linux") is None
    assert calls == []


def test_read_pressure_level_none_when_reader_unavailable():
    assert hook.read_pressure_level(macos_reader=lambda: None, platform_name="Darwin") is None


def test_linux_psi_reader_returns_some_avg10():
    text = "some avg10=25.50 avg60=10.00 avg300=5.00 total=123\nfull avg10=1.00\n"
    assert hook._read_linux_psi_some10(read_text=lambda path: text) == 25.5


def test_linux_psi_reader_none_on_missing_file():
    def raiser(path):
        raise OSError("no such file")

    assert hook._read_linux_psi_some10(read_text=raiser) is None


def test_linux_psi_reader_none_on_unparseable_content():
    assert hook._read_linux_psi_some10(read_text=lambda p: "garbage, no psi fields here") is None


def test_read_linux_psi_some10_is_linux_only(monkeypatch):
    monkeypatch.setattr(hook, "_read_linux_psi_some10", lambda read_text=None: 42.0)
    assert hook.read_linux_psi_some10(platform_name="Darwin") is None
    assert hook.read_linux_psi_some10(platform_name="Linux") == 42.0


def test_psi_above_threshold_blocks_and_below_allows():
    decision, message = hook.decide("build/test suite", level=None, block_at=2, psi_some10=12.0, max_psi_some10=10.0)
    assert decision == "block"
    assert "PSI some avg10" in message
    assert hook.decide("build/test suite", level=None, block_at=2, psi_some10=9.9, max_psi_some10=10.0) == ("allow", None)


def test_max_psi_env_override_and_rejection(monkeypatch):
    monkeypatch.setenv("RIG_HEAVY_OP_MAX_PSI_SOME10", "2.5")
    assert hook._max_psi_some10() == 2.5
    for raw in ("0", "100", "lots", "-1"):
        monkeypatch.setenv("RIG_HEAVY_OP_MAX_PSI_SOME10", raw)
        assert hook._max_psi_some10() == hook.DEFAULT_MAX_PSI_SOME10


def _swap_snapshot(available_gib, swap_free_gib, swap_total_gib=16):
    gib = 1024**3
    return hook.MemorySnapshot(
        48 * gib, 2 * gib, int(available_gib * gib),
        swap_total_bytes=swap_total_gib * gib, swap_free_bytes=int(swap_free_gib * gib),
    )


def test_full_swap_with_plenty_of_ram_allows():
    """2026-10-14: swap 83% full of stale pages while MemAvailable was 37 GB and PSI 0 — the
    unconditioned swap check blocked an idle machine."""
    snap = _swap_snapshot(30, 1.6)  # swap 90% full
    assert hook.decide("build/test suite", level=None, block_at=2, memory=snap, min_available_mb=8192, live_runners=[]) == ("allow", None)


def test_full_swap_blocks_without_a_live_runner_scan():
    """Review finding: with the scan unavailable the budget knows no reservations, so a roomy
    budget proves nothing — the swap check stays unconditional, as before the budget."""
    decision, message = hook.decide("build/test suite", level=None, block_at=2, memory=_swap_snapshot(30, 1.6), min_available_mb=8192)
    assert decision == "block"
    assert "swap is 90% full" in message and "no live-runner scan is available" in message


def test_full_swap_blocks_when_ram_is_short():
    """2026-10-11: swap had been 100% full all night when RAM ran short — nowhere to go."""
    decision, message = hook.decide(
        "build/test suite", level=None, block_at=2, memory=_swap_snapshot(6, 1.6), min_available_mb=4096, live_runners=[]
    )
    assert decision == "block"
    assert "swap is 90% full" in message
    assert "Memory budget: 6144 MB available − 0 MB reserved for 0 live runner(s) − 3072 MB" in message


def test_swap_is_checked_below_twice_the_headroom_even_when_the_budget_itself_passes():
    # 10 GiB − 3 GiB suite = 7168 MB left: above the 4096 MB headroom, below 2 x 4096.
    decision, message = hook.decide(
        "build/test suite", level=None, block_at=2, memory=_swap_snapshot(10, 1.6), min_available_mb=4096, live_runners=[]
    )
    assert decision == "block"
    assert "swap is 90% full" in message and "budget does not cover" not in message
    healthy = _swap_snapshot(10, 8)
    assert hook.decide("build/test suite", level=None, block_at=2, memory=healthy, min_available_mb=4096, live_runners=[]) == ("allow", None)


def test_no_swap_skips_the_swap_check():
    gib = 1024**3
    snap = hook.MemorySnapshot(48 * gib, 2 * gib, 30 * gib)  # swap fields default to 0
    assert hook.decide("build/test suite", level=None, block_at=2, memory=snap, min_available_mb=8192) == ("allow", None)


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
    assert hook._min_available_mb(platform_name="Darwin") == 100
    assert hook._min_available_mb(platform_name="Linux") == 100


@pytest.mark.parametrize("raw", ["0", "-1", "none"])
def test_min_available_mb_rejects_values_that_would_disable_the_check(monkeypatch, raw):
    monkeypatch.setenv("RIG_HEAVY_OP_MIN_AVAILABLE_MB", raw)
    assert hook._min_available_mb(platform_name="Darwin") == hook.DEFAULT_MIN_AVAILABLE_MB


def test_min_available_mb_linux_default_is_8gib_or_a_fifth_of_ram():
    """2026-10-11: the 512 MB floor never fired on the 48 GB box (MemAvailable bottomed at
    ~1.9 GB while it thrashed). 20% of 48 GB ≈ 9.6 GB; 8 GiB on anything smaller than 40 GB."""
    gib = 1024**3
    big = hook.MemorySnapshot(48 * gib, 0, 48 * gib)
    small = hook.MemorySnapshot(16 * gib, 0, 16 * gib)
    assert hook._min_available_mb(big, platform_name="Linux") == 48 * 1024 * 20 // 100
    assert hook._min_available_mb(small, platform_name="Linux") == 8192
    assert hook._min_available_mb(None, platform_name="Linux") == 8192
    assert hook._min_available_mb(big, platform_name="Darwin") == hook.DEFAULT_MIN_AVAILABLE_MB


# ── live heavy runners — a /proc snapshot, attributed to omp sessions ─────────────

def _table(rows):
    """{pid: (ppid, comm, argv)} → the scanner's table shape."""
    return {pid: hook._ProcInfo(ppid, comm, argv) for pid, (ppid, comm, argv) in rows.items()}


_LIVE_TABLE = _table({
    1: (0, "systemd", ["/sbin/init"]),
    100: (1, "omp", ["omp"]),  # root session A
    101: (100, "omp", ["omp", "__omp_worker_daemon_broker"]),  # a worker daemon, not a session
    110: (100, "bash", ["bash", "-c", "uv run pytest tests/"]),
    111: (110, "uv", ["uv", "run", "pytest", "tests/"]),  # launcher: collapses under …
    112: (111, "python", ["/repo/.venv/bin/python", "/repo/.venv/bin/pytest", "tests/"]),  # … the runner root
    113: (112, "python", ["/repo/.venv/bin/python", "-c", "xdist worker"]),
    200: (1, "omp", ["omp", "-r"]),  # root session B
    201: (200, "omp", ["omp", "--subagent"]),  # a subagent's omp under session B
    210: (201, "python", ["python3", "/x/review-cli/.venv/bin/review", "diff", "-C", "."]),
    211: (210, "node", ["node", "/home/u/.local/bin/claude", "-p", "review this"]),
    212: (210, "codex", ["codex", "exec", "review this"]),
    300: (1, "bash", ["bash"]),
    301: (300, "node", ["node", "/home/u/.local/bin/claude"]),  # interactive claude: not a seat
    302: (300, "bun", ["bun", "test", "./tests"]),  # no omp ancestor
    303: (300, "grep", ["grep", "-rn", "vitest.config", "src"]),  # `vitest.config` is not `vitest`
    304: (300, "node", ["node", "/repo/node_modules/.bin/tsc", "-b"]),
    305: (300, "docker", ["docker", "buildx", "build", "."]),
})


@pytest.fixture
def _fake_proc(tmp_path, monkeypatch):
    """Point the scanner's `/proc` at an empty dir so a test pid never resolves to a real
    process's cwd/statm on the developer's machine."""
    monkeypatch.setattr(hook, "_PROC", tmp_path / "proc")


def test_scan_collapses_launcher_trees_and_attributes_sessions(_fake_proc):
    sessions = {100: {"pid": 100, "cwd": "/home/u/repo-a", "title": "A: fix tests"}}
    runners = hook.scan_live_runners(table=_LIVE_TABLE, pss=lambda pid: pid * 10)
    by_pid = {r.pid: r for r in runners}
    # 112/113 collapse under the `uv run pytest` launcher 111; the review's seat CLIs 211/212
    # collapse into the review process 210.
    assert sorted(by_pid) == [111, 210, 302, 304, 305]
    assert by_pid[111].label == "pytest" and by_pid[111].pss_mb == 1110 + 1120 + 1130, "the whole tree's PSS"
    assert by_pid[111].omp_pid == 100 and by_pid[111].session == "", "titles are resolved later"
    assert by_pid[210].pss_mb == 2100 + 2110 + 2120
    assert by_pid[302].label == "bun test" and by_pid[302].omp_pid is None
    assert by_pid[304].label == "tsc --build"
    assert by_pid[305].label == "docker build"
    described = {r.pid: r for r in hook.describe_live_runners(runners, sessions=lambda: sessions)}
    assert described[111].session == 'omp 100 (/home/u/repo-a) "A: fix tests"'
    assert described[302].session == ""


def test_scan_review_seats_collapse_into_one_review_attributed_to_the_root_omp_session(_fake_proc):
    runners = hook.describe_live_runners(hook.scan_live_runners(table=_LIVE_TABLE, pss=lambda pid: 0), sessions=lambda: {})
    reviews = [r for r in runners if r.label.startswith("review")]
    assert [(r.pid, r.label) for r in reviews] == [(210, "review (2 seat(s): claude, codex)")]
    # The subagent's omp (201) is passed over for the ROOT session (200); with no tg-ctl entry
    # and no /proc cwd for it, the pid alone is shown.
    assert reviews[0].session == "omp 200"
    assert not any(r.pid == 301 for r in runners), "an interactive claude is not a review seat"


def test_review_without_seats_is_not_a_runner(_fake_proc):
    table = _table({
        1: (0, "systemd", ["/sbin/init"]),
        10: (1, "python", ["python3", "/x/review-cli/.venv/bin/review", "task", "705", "--check"]),
    })
    assert hook.scan_live_runners(table=table, pss=lambda pid: 0) == []


def test_scan_excludes_its_own_ancestry(monkeypatch):
    table = _table({
        1: (0, "systemd", ["/sbin/init"]),
        50: (1, "python", ["python", "-m", "pytest", "tests/test_heavy_op_memory_gate.py"]),
        51: (50, "python", ["python", "hook.py"]),
    })
    monkeypatch.setattr(hook.os, "getpid", lambda: 51)
    assert hook.scan_live_runners(table=table, pss=lambda pid: 0) == []


def test_pss_reads_smaps_rollup_and_rss_reads_statm(_fake_proc, tmp_path):
    rollup = tmp_path / "proc" / "77" / "smaps_rollup"
    rollup.parent.mkdir(parents=True)
    rollup.write_text("55d0-7ffd ---p 00000000 00:00 0  [rollup]\nRss:  4194304 kB\nPss:  1048576 kB\n")
    assert hook._pss_mb(77) == 1024
    assert hook._pss_mb(78) is None, "unreadable (another user's process) or gone"
    statm = tmp_path / "proc" / "78" / "statm"
    statm.parent.mkdir(parents=True)
    pages = 2 * 1024 * 1024 * 1024 // os.sysconf("SC_PAGE_SIZE")
    statm.write_text(f"999999 {pages} 0 0 0 0 0\n")
    assert hook._rss_mb(78) == 2048
    assert hook._rss_mb(79) == 0, "gone"


def test_tree_footprint_never_sums_fallback_rss(_fake_proc):
    """Review findings: summed RSS double-counts pages forked workers share, which would shrink
    the reservation; where PSS is unreadable (another user's tree) the largest single RSS
    stands in instead of 0 (which would reserve the full cost) or the RSS sum."""
    table = _table({
        1: (0, "systemd", ["/sbin/init"]),
        700: (1, "pytest", ["pytest", "-n", "2"]),
        701: (700, "python", ["python", "-c", "xdist worker"]),
        702: (700, "python", ["python", "-c", "xdist worker"]),
    })
    unreadable = hook.scan_live_runners(table=table, pss=lambda pid: None, rss=lambda pid: 2048)
    assert [r.pss_mb for r in unreadable] == [2048], "max RSS, not 3 x 2048"
    readable = hook.scan_live_runners(table=table, pss=lambda pid: 700, rss=lambda pid: 2048)
    assert [r.pss_mb for r in readable] == [2100]
    mixed = hook.scan_live_runners(table=table, pss=lambda pid: None if pid == 702 else 100, rss=lambda pid: 1500)
    assert [r.pss_mb for r in mixed] == [1500]


def test_describe_skips_the_session_lookup_without_omp_ancestors():
    calls = []
    runners = [hook.LiveRunner(302, 0, "bun test", "", None)]
    assert hook.describe_live_runners(runners, sessions=lambda: calls.append(1) or {}) == runners
    assert calls == []


def test_runner_label_examples():
    assert hook._runner_label(["make", "-j8", "check"]) == "make check"
    assert hook._runner_label(["cargo", "test", "--workspace"]) == "cargo test"
    assert hook._runner_label(["go", "test", "./..."]) == "go test"
    assert hook._runner_label(["/usr/bin/python3", "-m", "mypy", "src"]) == "mypy"
    assert hook._runner_label(["node", "/r/node_modules/.bin/jest"]) == "jest"
    assert hook._runner_label(["uv", "run", "--with", "pytest", "pytest", "tests/"]) == "pytest"
    assert hook._runner_label(["timeout", "600", "uv", "run", "pytest"]) == "pytest"
    assert hook._runner_label(["/repo/.venv/bin/python", "/repo/.venv/bin/pytest", "-q"]) == "pytest"
    assert hook._runner_label(["cargo", "build"]) is None
    assert hook._runner_label(["bun", "run", "dev"]) is None
    assert hook._runner_label(["node", "/r/node_modules/.bin/tsc", "--noEmit"]) is None


@pytest.mark.parametrize("argv", [
    ["grep", "pytest", "README.md"],
    ["echo", "mypy"],
    ["vim", "tests/test_pytest.py"],
    ["bash", "-c", "grep -rn vitest src"],  # the script is one word, not an executable position
    ["git", "commit", "-m", "pytest"],
    ["tail", "-f", "jest.log"],
])
def test_runner_label_ignores_runner_names_in_operand_positions(argv):
    """Review finding: a long-lived `grep pytest` or `echo mypy` must not count toward the limit."""
    assert hook._runner_label(argv) is None, argv


def test_scan_does_not_count_runpod_run_trees(_fake_proc, offload_script):
    """`runpod-run -- bun test` names a runner in its argv, but the suite runs on the pod;
    the local process and its ssh child are not heavy runners."""
    table = _table({
        1: (0, "systemd", ["/sbin/init"]),
        100: (1, "omp", ["omp"]),
        400: (100, "runpod-run", ["bash", str(offload_script), "--ttl", "30", "--", "bun", "test", "./tests"]),
        401: (400, "ssh", ["ssh", "-p", "22", "runner@1.2.3.4", "bash -c x runpod-run /workspace/r 3600s bun test ./tests"]),
        402: (400, "bun", ["bun", "test", "./tests"]),  # even a stray child is the offload's
        410: (100, "bash", ["bash", "./runpod-run", "--", "pytest", "-q"]),  # relative: not pinnable, so counted
        411: (410, "python", ["python", "-m", "pytest", "-q"]),
        500: (100, "bun", ["bun", "test", "./tests"]),  # a real local suite still counts
    })
    assert [r.pid for r in hook.scan_live_runners(table=table, pss=lambda pid: 0)] == [411, 500]


def test_scan_counts_a_process_merely_named_runpod_run(_fake_proc, tmp_path):
    """Review finding: a spoofed `runpod-run` (here a pass-through shim) runs the suite locally."""
    fake = tmp_path / "evil" / "runpod-run"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexec \"$@\"\n")
    table = _table({
        1: (0, "systemd", ["/sbin/init"]),
        600: (1, "runpod-run", ["sh", str(fake), "--", "bun", "test", "./tests"]),
        601: (600, "bun", ["bun", "test", "./tests"]),
    })
    assert [r.pid for r in hook.scan_live_runners(table=table, pss=lambda pid: 0)] == [601]


@pytest.mark.parametrize("argv", [
    ["bash", "{skill}", "--", "bun", "test"],
    ["/usr/bin/env", "bash", "{local_bin}", "--ttl", "30", "--", "pytest"],
    ["timeout", "3700", "{skill}", "--", "pytest"],
])
def test_is_offload_process_matches_the_real_runpod_run_in_executable_position(argv, offload_script):
    local_bin = Path(os.environ["HOME"]) / ".local" / "bin" / "runpod-run"
    argv = [word.format(skill=offload_script, local_bin=local_bin) for word in argv]
    assert hook._is_offload_process(hook._ProcInfo(1, "bash", argv))


def test_is_offload_process_ignores_runpod_run_as_an_operand(offload_script):
    assert not hook._is_offload_process(hook._ProcInfo(1, "grep", ["grep", str(offload_script), "README.md"]))
    assert not hook._is_offload_process(hook._ProcInfo(1, "pytest", ["pytest", "tests/test_runpod-run.py"]))


def _runners(n, label="pytest", pss_mb=None):
    return [
        hook.LiveRunner(1000 + i, 500 + i if pss_mb is None else pss_mb, label, f"omp {10 + i} (~/repo-{i})", 10 + i)
        for i in range(n)
    ]


_GIB_MB = 1024


def _avail(gib):
    return hook.MemorySnapshot(48 * 1024**3, 2 * 1024**3, int(gib * 1024**3))


def test_budget_allows_a_review_next_to_two_runners_with_plenty_of_ram():
    """2026-10-14: 23 GB available, PSI 0, two runners live — the fixed cap of 2 blocked this."""
    assert hook.decide(
        "multi-model review-cli pass", level=None, block_at=2, memory=_avail(23), min_available_mb=9830,
        psi_some10=0.0, live_runners=_runners(2),
    ) == ("allow", None)


def test_budget_blocks_a_suite_while_four_reviews_have_not_grown_yet():
    """Four reviews whose seats still sit at 300 MB each reserve 4 x 2.7 GiB — with the suite's
    own 3 GiB, more than the 10 GiB available, whatever their current footprint says."""
    decision, message = hook.decide(
        "test-suite runner", level=None, block_at=2, memory=_avail(10), min_available_mb=4096,
        psi_some10=0.0, live_runners=_runners(4, label="review (4 seat(s): claude, codex)", pss_mb=300),
    )
    assert decision == "block"
    arithmetic = (
        "10240 MB available − 11088 MB reserved for 4 live runner(s) − 3072 MB for this "
        "test-suite runner = -3920 MB left (headroom 4096 MB)"
    )
    assert f"the memory budget does not cover it: {arithmetic}" in message
    assert f"\nMemory budget: {arithmetic}." in message
    assert "  - pid 1000  300 MB  review (4 seat(s): claude, codex)  (reserves 2772 MB)  ← omp 10 (~/repo-0)" in message
    assert "hard ceiling" not in message


def test_budget_costs_by_class():
    costs = hook.CostTable()
    # Measured: review-cli's sampler, 294 runs, whole review tree peak p95 1125 MB, max 3195 MB.
    assert hook._expected_cost_mb("multi-model review-cli pass", costs) == 3 * _GIB_MB
    assert hook._expected_cost_mb("review (1 seat(s): codex)", costs) == 3 * _GIB_MB
    assert hook._expected_cost_mb("docker build", costs) == 8 * _GIB_MB
    for label in ("pytest", "bun test", "tsc --build", "build/test suite", hook.GPU_MODEL_LABEL):
        assert hook._expected_cost_mb(label, costs) == 3 * _GIB_MB, label
    assert hook._reserved_mb(hook.LiveRunner(1, 5000, "pytest", ""), costs) == 0, "a runner past its estimate reserves nothing"


def test_budget_env_overrides(monkeypatch):
    monkeypatch.setenv("RIG_HEAVY_OP_COST_REVIEW_MB", "4096")
    monkeypatch.setenv("RIG_HEAVY_OP_COST_SUITE_MB", "0")  # would disable the reservation: default
    monkeypatch.setenv("RIG_HEAVY_OP_HEADROOM_MB", "2048")
    assert hook._cost_table() == hook.CostTable(4096, hook.DEFAULT_COST_DOCKER_MB, hook.DEFAULT_COST_SUITE_MB)
    assert hook._headroom_mb() == 2048


def test_budget_needs_a_live_runner_scan():
    """macOS (no scan): the budget would be a far higher floor than its own thresholds."""
    assert hook.decide("multi-model review-cli pass", level=1, block_at=2, memory=_avail(6)) == ("allow", None)


def test_live_runner_ceiling_blocks_even_with_plenty_of_ram():
    decision, message = hook.decide("test-suite runner", level=None, block_at=2, memory=_avail(200), live_runners=_runners(5))
    assert decision == "block"
    assert "5 heavy runner(s) are already live on the machine (hard ceiling 5" in message
    assert "budget does not cover" not in message
    assert message.endswith(
        "Heavy runners live right now (5; ceiling 5):\n"
        "  - pid 1000  500 MB  pytest  (reserves 2572 MB)  ← omp 10 (~/repo-0)\n"
        "  - pid 1001  501 MB  pytest  (reserves 2571 MB)  ← omp 11 (~/repo-1)\n"
        "  - pid 1002  502 MB  pytest  (reserves 2570 MB)  ← omp 12 (~/repo-2)\n"
        "  - pid 1003  503 MB  pytest  (reserves 2569 MB)  ← omp 13 (~/repo-3)\n"
        "  - pid 1004  504 MB  pytest  (reserves 2568 MB)  ← omp 14 (~/repo-4)"
    )
    assert "runpod-run" in message
    assert hook.decide("test-suite runner", level=None, block_at=2, memory=_avail(200), live_runners=_runners(4)) == ("allow", None)


def test_live_runners_are_listed_when_another_signal_blocks():
    assert hook.decide("test-suite runner", level=None, block_at=2, live_runners=_runners(1)) == ("allow", None)
    decision, message = hook.decide("test-suite runner", level=None, block_at=2, live_runners=_runners(1), psi_some10=50.0)
    assert decision == "block"
    assert "Heavy runners live right now (1; ceiling 5):\n  - pid 1000  500 MB  pytest" in message


def test_max_live_env_override_and_rejection(monkeypatch):
    monkeypatch.setenv("RIG_HEAVY_OP_MAX_LIVE", "4")
    assert hook._max_live() == 4
    monkeypatch.setenv("RIG_HEAVY_OP_MAX_LIVE", "0")
    assert hook._max_live() == hook.DEFAULT_MAX_LIVE


def _crowded_full_swap(available_gib):
    gib = 1024**3
    return hook.MemorySnapshot(48 * gib, 2 * gib, int(available_gib * gib), swap_total_bytes=16 * gib, swap_free_bytes=0)


def test_review_admission_skips_budget_swap_and_ceiling():
    """review-cli queues on those itself (review-cli#614): six live reviews, a full swap and a
    budget far below zero do not block a review it admits."""
    runners = _runners(6, label="review (4 seat(s): claude, codex)", pss_mb=300)
    kwargs = dict(level=None, block_at=2, memory=_crowded_full_swap(12), min_available_mb=9830, psi_some10=0.0, live_runners=runners)
    assert hook.decide(hook.REVIEW_LABEL, review_admission=True, **kwargs) == ("allow", None)
    decision, message = hook.decide(hook.REVIEW_LABEL, **kwargs)
    assert decision == "block" and "budget does not cover" in message and "hard ceiling" in message


@pytest.mark.parametrize("signal", ["available", "psi", "level"])
def test_review_admission_still_blocks_on_emergency_signals(signal):
    kwargs = dict(level=None, block_at=2, memory=_crowded_full_swap(30), min_available_mb=9830, psi_some10=0.0, live_runners=[])
    if signal == "available":
        kwargs["memory"] = _crowded_full_swap(6)
    elif signal == "psi":
        kwargs["psi_some10"] = 25.0
    else:
        kwargs["level"] = 4
    decision, message = hook.decide(hook.REVIEW_LABEL, review_admission=True, **kwargs)
    assert decision == "block", signal
    assert "review-cli's admission controller would queue this review" in message
    assert "swap is" not in message and "Memory budget" not in message


def test_live_runners_guarded_fails_open(monkeypatch):
    def boom():
        raise RuntimeError("proc unreadable")

    monkeypatch.setattr(hook, "scan_live_runners", boom)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert hook._live_runners_guarded(platform_name="Linux") is None
    assert hook._live_runners_guarded(platform_name="Darwin") is None


def test_unreadable_proc_is_a_failed_scan_not_an_empty_one(monkeypatch, tmp_path):
    """Review finding: an empty table read as "nothing running" would skip the swap check's
    no-scan fallback."""
    monkeypatch.setattr(hook, "_PROC", tmp_path / "no-proc")
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert hook._live_runners_guarded(platform_name="Linux") is None


def test_real_proc_scan_is_fast_and_does_not_crash():
    """The real `/proc` read (no mocks) must stay well under the hook's budget."""
    if not Path("/proc/self/stat").exists():
        pytest.skip("no /proc")
    started = time.monotonic()
    runners = hook.scan_live_runners()
    assert time.monotonic() - started < 0.3
    assert all(isinstance(r, hook.LiveRunner) for r in runners)


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
    monkeypatch.setattr(hook, "_read_linux_psi_some10", lambda read_text=None: None)
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
    monkeypatch.setattr(hook, "_read_linux_psi_some10", lambda read_text=None: None)
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


def test_main_allows_runpod_run_under_critical_pressure(monkeypatch, offload_script):
    out, _err, code = _run("runpod-run --ttl 30 -- bun test ./tests", monkeypatch, macos_level=4)
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_main_still_blocks_a_local_suite_chained_after_runpod_run(monkeypatch, offload_script):
    out, _err, code = _run("runpod-run -- bun test ./tests && bun test", monkeypatch, macos_level=4)
    assert code == hook.BLOCK_EXIT_CODE
    assert json.loads(out)["decision"] == "block"


def _run_linux(command, monkeypatch, *, memory, runners, psi=0.0):
    monkeypatch.setattr(hook, "read_memory_snapshot", lambda *a, **k: memory)
    monkeypatch.setattr(hook, "_live_runners_guarded", lambda *a, **k: runners)
    monkeypatch.setattr(hook, "describe_live_runners", lambda rs, *a, **k: rs)
    monkeypatch.setattr(hook, "read_linux_psi_some10", lambda *a, **k: psi)
    monkeypatch.setattr(hook, "read_pressure_level", lambda *a, **k: None)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": {"command": command}})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    hook.main()
    return json.loads(out.getvalue())


def test_main_hands_an_installed_review_to_review_cli(monkeypatch, review_cli):
    crowded = dict(memory=_crowded_full_swap(20), runners=_runners(6, label="review (2 seat(s): claude, codex)", pss_mb=300))
    assert _run_linux("review diff --staged --task 633", monkeypatch, **crowded)["decision"] == "allow"
    # Other heavy work in the same command, or a review not handed off, is gated as before.
    assert _run_linux("review diff --staged && bun test", monkeypatch, **crowded)["decision"] == "block"
    # Started together, neither gate reserves the other (review finding): no handoff then.
    roomy = dict(memory=_crowded_full_swap(30), runners=_runners(5, label="review (2 seat(s): claude, codex)", pss_mb=300))
    assert _run_linux("review diff --staged & bun test", monkeypatch, **roomy)["decision"] == "block", "the ceiling still applies"
    assert _run_linux("REVIEW_ADMISSION=0 review diff --staged", monkeypatch, **crowded)["decision"] == "block"
    assert _run_linux("bun test", monkeypatch, **crowded)["decision"] == "block"
    assert _run_linux("review diff --staged", monkeypatch, memory=_crowded_full_swap(4), runners=[])["decision"] == "block"

"""Tests for agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py.

Covers:
  - True positives: `gh pr merge <N>` is blocked
  - The argv-parse FP fix: commands whose body/args contain "gh pr merge" as TEXT
    (e.g. `gh pr create --body "...gh pr merge..."`) are ALLOWED
  - Sanctioned paths: `gh ship`, `pr-ship.sh`, `ship.sh` are allowed
  - Shell chains: `gh pr merge` behind `&&`/`;`/`||` is caught
  - Inline env assignments before `gh` are stripped
  - The DEAD self-service escape hatch (`ALLOW_RAW_PR_MERGE` / `# no-ship-guard:` no longer
    allow) and the external Telegram hatch (`RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE`)
  - Fail-closed: unbalanced quotes and malformed event both block

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_block_raw_pr_merge.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-raw-pr-merge"
    / "block_raw_pr_merge.py"
)
_spec = importlib.util.spec_from_file_location("block_raw_pr_merge", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

_HATCH_ENV = "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE"


def _run(
    command: str,
    monkeypatch,
    env: dict | None = None,
    cwd=None,
) -> tuple[str, str, int]:
    """Run the hook with a `pre-bash` event carrying `command`.  Returns (stdout, stderr, exit)."""
    event = {"args": {"command": command}}
    if cwd is not None:
        event["cwd"] = str(cwd)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Clear escape-hatch env so ambient values don't leak into tests.
    for k in ("ALLOW_RAW_PR_MERGE", "ALLOW_RAW_PR_MERGE_REASON",
              "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# ── True positives — should BLOCK ──────────────────────────────────────────────────────────

def test_block_basic_gh_pr_merge(monkeypatch):
    out, _err, code = _run("gh pr merge 123", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_with_squash(monkeypatch):
    out, _err, code = _run("gh pr merge 5 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_admin(monkeypatch):
    out, _err, code = _run("gh pr merge 42 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_with_leading_env(monkeypatch):
    """A VAR=value prefix before `gh` must not prevent detection."""
    out, _err, code = _run("GH_TOKEN=secret gh pr merge 7 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "env GH_TOKEN=secret gh pr merge 7 --squash",
        "command gh pr merge 7 --squash",
        "exec gh pr merge 7 --squash",
        "nice gh pr merge 7 --squash",
        "sudo gh pr merge 7 --squash",
        "watch gh pr merge 7 --squash",
        "parallel gh pr merge ::: 7 --squash",
    ],
)
def test_block_gh_pr_merge_with_launch_wrappers(command, monkeypatch):
    """Common command-launch wrappers must not hide a direct raw merge."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_and(monkeypatch):
    """A `gh pr merge` after `&&` in a chain must still be blocked."""
    out, _err, code = _run("echo done && gh pr merge 8 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_semicolon(monkeypatch):
    out, _err, code = _run("echo done; gh pr merge 9", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_or(monkeypatch):
    out, _err, code = _run("false || gh pr merge 10", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_after_bare_newline(monkeypatch):
    """A bare newline is a command separator, not whitespace that can hide a second command."""
    out, _err, code = _run("echo done\ngh pr merge 11 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize("command", ["gh pr merg\\\ne 5", "g\\\nh pr merge 5"])
def test_block_raw_merge_with_line_continuation_inside_word(monkeypatch, command):
    """Shell removes backslash-newline without adding whitespace, so split words still execute."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_hash_comment_before_newline_does_not_swallow_following_merge(monkeypatch):
    """A line comment ends at newline; the merge command on the next line must still block."""
    out, _err, code = _run("echo done # comment mentions gh pr merge 12\ngh pr merge 12", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo $(true)# ; gh pr merge 12",
        "cat <(true)# ; gh pr merge 12",
    ],
)
def test_hash_after_substitution_word_does_not_swallow_following_merge(monkeypatch, command):
    """A `#` after command/process substitution is inside a word, not a line comment."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_arithmetic_shift_is_not_heredoc_opener(monkeypatch):
    """The `<<` in arithmetic expansion is not a heredoc that can hide a later merge line."""
    out, _err, code = _run("echo $((1 << 4))\ngh pr merge 13", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_arithmetic_command_shift_is_not_heredoc_opener(monkeypatch):
    """The `<<` in an arithmetic command is not a heredoc opener either."""
    out, _err, code = _run("(( 1 << 4 ))\ngh pr merge 16", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_continued_heredoc_opener_does_not_skip_logical_line_merge(monkeypatch):
    """A heredoc body starts after the continued logical command, not after the first line."""
    command = "cat <<EOF && \\\ngh pr merge 14\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_comment_after_closing_paren_hides_heredoc_text_not_following_merge(monkeypatch):
    """A comment can start after `)`, so `<<EOF` in that comment must not start heredoc skipping."""
    command = "(echo ok)# <<EOF\ngh pr merge 15"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "bash <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash <<'EOF'\ngh pr merge 123 --admin\nEOF",
        "bash --noprofile --norc <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash -O extglob <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash -o pipefail <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash -euo pipefail <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash 2>/dev/null <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash &>/dev/null <<EOF\ngh pr merge 123 --admin\nEOF",
        "sh <<EOF\ngh pr merge 123 --admin\nEOF",
        "/bin/zsh <<EOF\ngh pr merge 123 --admin\nEOF",
        "env bash <<EOF\ngh pr merge 123 --admin\nEOF",
        "command bash <<EOF\ngh pr merge 123 --admin\nEOF",
        "sudo sh <<EOF\ngh pr merge 123 --admin\nEOF",
        "timeout 30 bash <<EOF\ngh pr merge 123 --admin\nEOF",
        "nohup bash <<EOF\ngh pr merge 123 --admin\nEOF",
        "setsid bash <<EOF\ngh pr merge 123 --admin\nEOF",
        "stdbuf -oL bash <<EOF\ngh pr merge 123 --admin\nEOF",
        "<<EOF bash\ngh pr merge 123 --admin\nEOF",
        "cat <<EOF | bash\ngh pr merge 123 --admin\nEOF",
        "cat <<EOF |& bash\ngh pr merge 123 --admin\nEOF",
        "bash <<< 'gh pr merge 123 --admin'",
        "bash <<- EOF\ngh pr merge 123 --admin\nEOF",
        "bash -s -- arg <<EOF\ngh pr merge 123 --admin\nEOF",
        "sh -s arg <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash -s foo#bar <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash -s arg <<< 'gh pr merge 123 --admin'",
        "ash <<EOF\ngh pr merge 123 --admin\nEOF",
        "csh <<EOF\ngh pr merge 123 --admin\nEOF",
        "mksh <<EOF\ngh pr merge 123 --admin\nEOF",
        "ksh93 <<EOF\ngh pr merge 123 --admin\nEOF",
        "busybox sh <<EOF\ngh pr merge 123 --admin\nEOF",
        "bash5 /dev/stdin <<EOF\ngh pr merge 123 --admin\nEOF",
    ],
)
def test_shell_interpreter_heredoc_body_is_executable(command, monkeypatch):
    """A heredoc body fed to a shell interpreter is executable shell, not inert stdin text."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_later_stdin_redirect_makes_shell_heredoc_body_inert(monkeypatch):
    """A later stdin redirect replaces an earlier heredoc body for the same shell command."""
    command = "bash <<EOF <script.sh\ngh pr merge 123 --admin\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_later_shell_heredoc_overrides_earlier_stdin_redirect(monkeypatch):
    """When the heredoc is the last stdin redirect, the shell executes its body."""
    command = "bash <script.sh <<EOF\ngh pr merge 123 --admin\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo $(gh pr merge 17)",
        "x=$(gh pr merge 17)",
        "echo `gh pr merge 17`",
        "cat <(gh pr merge 17)",
        "cat >(gh pr merge 17)",
        'echo "run: `gh pr merge 17`"',
        "case x in x) gh pr merge 17;; esac",
        "case y in x) true;; y) gh pr merge 17;; esac",
        "case y in x) case z in z) true;; esac;; y) gh pr merge 17;; esac",
        "case x in x) GH_TOKEN=secret gh pr merge 17;; esac",
        "case x in x) ( gh pr merge 17 );; esac",
        "case x in x) bash -c 'gh pr merge 17';; esac",
        "case x in x) eval 'gh pr merge 17';; esac",
        "case x in x) trap 'gh pr merge 17' EXIT;; esac",
        "echo ok;(gh pr merge 17)",
        "echo ok&&(gh pr merge 17)",
        "(echo ok)&&(gh pr merge 17)",
        "if gh pr merge 17; then true; fi",
        "if true; then gh pr merge 17; fi",
        "if false; then true; else gh pr merge 17; fi",
        "if false; then true; elif true; then gh pr merge 17; fi",
        "while gh pr merge 17; do true; done",
        "while true; do gh pr merge 17; done",
        "until gh pr merge 17; do true; done",
        "until false; do gh pr merge 17; done",
        "time gh pr merge 17",
        "time -o log gh pr merge 17",
        "time ! gh pr merge 17",
        "time GH_TOKEN=x gh pr merge 17",
        "time -p GH_TOKEN=x gh pr merge 17",
        "command time gh pr merge 17",
        "env time gh pr merge 17",
        "/usr/bin/time gh pr merge 17",
        "coproc gh pr merge 17",
        "bash -c 'gh pr merge 17'",
        'bash -c "if true; then gh pr merge 17; fi"',
        'bash -c "$(printf \'gh pr merge 17\')"',
        'bash -c "`printf \'gh pr merge 17\'`"',
        "sh -c \"$(cat <<EOF\ngh pr merge 17\nEOF\n)\"",
        'sh -c "gh pr merge 17"',
        "env bash -c 'gh pr merge 17'",
        "command bash -c 'gh pr merge 17'",
        "bash -O extglob -c 'gh pr merge 17'",
        "bash -euo pipefail -c 'gh pr merge 17'",
        "bash -co 'gh pr merge 17'",
        "bash -oc 'gh pr merge 17'",
        "ash -c 'gh pr merge 17'",
        "csh -c 'gh pr merge 17'",
        "mksh -c 'gh pr merge 17'",
        "busybox sh -c 'gh pr merge 17'",
        "bash -c $'gh pr merge 17'",
        'eval "gh pr merge 17"',
        'eval "if true; then gh pr merge 17; fi"',
        'eval "while true; do gh pr merge 17; done"',
        "eval $'gh pr merge 17'",
        "trap 'gh pr merge 17' EXIT",
        'trap "$(printf \'gh pr merge 17\')" EXIT',
        "trap `printf 'gh pr merge 17'` EXIT",
        'eval "$(printf \'gh pr merge 17\')"',
        'command eval "$(printf \'gh pr merge 17\')"',
        "command eval 'gh pr merge 17'",
        "env eval 'gh pr merge 17'",
        "sudo eval 'gh pr merge 17'",
        "builtin eval 'gh pr merge 17'",
        "builtin command gh pr merge 17",
        "builtin exec gh pr merge 17",
        "builtin command bash -c 'gh pr merge 17'",
        "builtin trap 'gh pr merge 17' EXIT",
        "sudo -U guest gh pr merge 17",
        "sudo --other-user guest gh pr merge 17",
        "sudo -s 'gh pr merge 17'",
        "sudo -i 'gh pr merge 17'",
        "exec -a merged gh pr merge 17",
        "exec -c gh pr merge 17",
        "exec -l gh pr merge 17",
        'flock -c "gh pr merge 17" /tmp/lock',
        'flock --command "gh pr merge 17" /tmp/lock',
        'runuser -u root -c "gh pr merge 17"',
        'runuser --user root --command "gh pr merge 17"',
        'su -c "gh pr merge 17"',
        'su root -c "gh pr merge 17"',
        "env -S 'GH_TOKEN=x gh pr merge 17'",
        "env -iS 'GH_TOKEN=x gh pr merge 17'",
        "env -S'GH_TOKEN=x gh pr merge 17'",
        "env -iS'GH_TOKEN=x gh pr merge 17'",
        "env --split-string='GH_TOKEN=x gh pr merge 17'",
        "nohup gh pr merge 17",
        "setsid gh pr merge 17",
        "unbuffer -p gh pr merge 17",
        "stdbuf -oL gh pr merge 17",
        "echo 17 | xargs gh pr merge",
        "printf 'pr merge 17\n' | xargs gh",
        "printf 'merge 17\n' | xargs gh pr",
        "printf '17\n' | xargs gh pr merge",
        "printf 'gh pr merge 17\n' | xargs sh -c",
        "printf 'gh pr merge 17\n' | xargs bash -c",
        "printf 'gh pr merge 17\n' | xargs -IX sh -c X",
        "printf '17\n' | xargs -I{} gh pr merge {}",
        "xargs -a prs.txt gh pr merge",
        "xargs -d '\n' gh pr merge",
        "xargs -J {} gh pr merge {}",
        "printf '17\n' | xargs -J{} gh pr merge {}",
        "$'gh' pr merge 17",
        "$'gh' $'pr' $'merge' 17",
        'echo "deploy() { $(gh pr merge 17); }"',
        'printf "x() { `gh pr merge 17`; }"',
        "echo `foo # bar`; gh pr merge 17",
        "echo $( ( gh pr merge 17 ) )",
        "echo $(bash <<EOF\ngh pr merge 17\nEOF\n)",
        "cat <(bash <<EOF\ngh pr merge 17\nEOF\n)",
    ],
)
def test_nested_executable_shell_contexts_block(command, monkeypatch):
    """Raw merges inside command substitutions, process substitutions, and case arms execute."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_xargs_static_producer_unbalanced_merge_hint_fails_closed(monkeypatch):
    """Unparseable static xargs input keeps the same unbounded fail-closed merge hint."""
    command = "printf 'gh" + (" " * 250) + "pr merge 17 \"unterminated\\n' | xargs sh -c"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "printf 'gh pr merge 17\n' | bash",
        "printf -- 'gh pr merge 17\n' | bash",
        "printf '%s\n' 'gh pr merge 17' | bash",
        "printf 'gh pr %s\n' merge | bash",
        "printf '%1$s\n' 'gh pr merge 17' | bash",
        "printf '%s\n' foo 'gh pr merge 17' | bash",
        "printf 'gh pr %cerge 17\n' m | bash",
        "printf '%*s\n' 1 'gh pr merge 17' | bash",
        "printf 'gh\\tpr\\tmerge\\n' | bash",
        "echo 'gh pr merge 17' | sh",
        "echo -e 'gh\\tpr\\tmerge' | bash",
        "echo -ne 'gh\\tpr\\tmerge\\n' | bash",
        "printf 'gh pr merge 17\n' | bash -c 'bash'",
        "printf 'gh pr merge 17\n' | bash -c 'source /dev/stdin'",
        "printf 'gh pr merge 17\n' | bash /dev/stdin",
        "printf 'gh pr merge 17\n' | sh /dev/fd/0",
        "printf 'gh pr merge 17\n' | source /dev/stdin",
        "printf 'gh pr merge 17\n' | builtin source /dev/stdin",
        "printf 'gh pr merge 17\n' | . /dev/fd/0",
        "cat <<< 'gh pr merge 17' | source /proc/self/fd/0",
        "printf 'gh pr merge 17\n' | grep gh | bash",
        "printf 'gh pr merge 17\n' |& bash",
        "cat <<< 'gh pr merge 17' | bash",
        'bash -c "$(printf -- \'gh pr merge 17\')"',
    ],
)
def test_static_pipeline_text_into_shell_blocks(command, monkeypatch):
    """Static script text piped into a stdin-reading shell is executable shell."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "bash < <(printf 'gh pr merge 17\n')",
        "bash < <(printf -- 'gh pr merge 17\n')",
        "bash <(printf 'gh pr merge 17\n')",
        "source <(printf 'gh pr merge 17\n')",
        "bash /dev/stdin < <(printf 'gh pr merge 17\n')",
        "bash < <(cat <<EOF\ngh pr merge 17\nEOF\n)",
        "source <(cat <<EOF\ngh pr merge 17\nEOF\n)",
    ],
)
def test_static_process_substitution_text_into_shell_blocks(command, monkeypatch):
    """Static process-substitution output consumed by a shell is executable shell."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_later_stdin_redirect_makes_process_substitution_inert(monkeypatch):
    """A later stdin redirect replaces an earlier process substitution for the shell."""
    command = "bash < <(printf 'gh pr merge 17\n') <script.sh"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_later_process_substitution_overrides_earlier_stdin_redirect(monkeypatch):
    """When process substitution is the last stdin redirect, the shell executes it."""
    command = "bash <script.sh < <(printf 'gh pr merge 17\n')"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "bash script.sh <(echo gh pr merge 17)",
        "bash -c true <(echo gh pr merge 17)",
    ],
)
def test_process_substitution_as_shell_argument_is_not_executable_shell_input(command, monkeypatch):
    """A process substitution argument is not shell source when the shell already has a script."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_static_pipeline_upstream_filter_is_conservatively_blocked(monkeypatch):
    """Any static upstream merge text before a shell is blocked; filters are not modeled."""
    command = 'echo "gh pr merge" | wc -l | bash'
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_dynamic_pipeline_into_shell_without_merge_text_allows(monkeypatch):
    """Only modeled static producers are inspected; dynamic script sources are out of scope."""
    command = "curl https://example.invalid/install.sh | bash"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_first_heredoc_in_pipeline_is_not_shell_stdin_when_shell_has_own_heredoc(monkeypatch):
    """A downstream shell with its own stdin redirection does not consume upstream heredoc text."""
    command = "cat <<A | bash <<B\ngh pr merge 17\nA\necho ok\nB"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_second_heredoc_in_pipeline_blocks_when_owned_by_shell(monkeypatch):
    """A heredoc owned by the shell component is executable shell."""
    command = "cat <<A | bash <<B\ntext\nA\ngh pr merge 17\nB"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_static_pipeline_text_not_shell_stdin_when_shell_has_own_heredoc(monkeypatch):
    """Static upstream text is not shell stdin when the shell reads a heredoc instead."""
    command = "printf 'gh pr merge 17\n' | bash <<EOF\necho ok\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_pipeline_shell_with_fd3_then_stdin_redirect_does_not_consume_upstream(monkeypatch):
    """A non-stdin redirect before a stdin redirect should not hide the later stdin redirect."""
    command = "printf 'gh pr merge 17\n' | bash 3<file <input"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_static_pipeline_printf_data_line_with_merge_words_allows(monkeypatch):
    """Rendered printf output that is not a gh command line should not block."""
    command = "printf 'Total: %s items\n' 'gh pr merge' | bash"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_here_string_pipeline_static_script_only_for_cat(monkeypatch):
    """A here-string is producer stdin; only cat forwards it unchanged to the shell."""
    command = "rev <<< 'gh pr merge 17' | bash"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_arithmetic_adjacent_hash_is_not_line_comment(monkeypatch):
    """A `#` attached to arithmetic text is part of a word, not a line comment."""
    out, _err, code = _run("echo $((1))# literal ; gh pr merge 17", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_escaped_backticks_inside_double_quotes_are_literal_text(monkeypatch):
    """Backslash-escaped backticks inside double quotes do not execute command substitution."""
    out, _err, code = _run('echo "run: \\`gh pr merge 17\\`"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_safe_ansi_c_quoted_text_with_escaped_quote_allows(monkeypatch):
    """ANSI-C quotes can contain escaped quotes without creating parser failure false positives."""
    out, _err, code = _run("echo $'don\\'t run gh pr merge'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_ansi_c_quoted_escaped_quote_does_not_hide_following_command(monkeypatch):
    """ANSI-C escaped quotes must not leave line parsing in a fake single-quote state."""
    out, _err, code = _run("echo $'don\\'t'\ngh pr merge 17", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_ansi_c_hex_escape_with_one_digit_blocks(monkeypatch):
    """ANSI-C hex escapes may contain one digit; decode them before tokenizing."""
    out, _err, code = _run("bash -c $'gh\\x20pr\\x20merge 17'", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_ansi_c_unicode_escape_blocks(monkeypatch):
    """ANSI-C unicode escapes are executable text and must not hide `gh`."""
    out, _err, code = _run("bash -c $'\\u0067h pr merge 17'", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_ansi_c_octal_escape_blocks(monkeypatch):
    """ANSI-C octal escapes are decoded as base 8 before tokenizing shell text."""
    out, _err, code = _run(r"bash -c $'\147h pr merge 17'", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_invalid_ansi_c_unicode_escape_does_not_crash(monkeypatch):
    """Out-of-range unicode escapes are left undecoded instead of raising from chr()."""
    out, _err, code = _run("echo $'\\U00110000 gh pr merge 17'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_ansi_c_control_escape_blocks(monkeypatch):
    """Bash decodes `\\cI` to a tab, so token separators hidden this way still execute."""
    out, _err, code = _run("bash -c $'gh\\cIpr\\cImerge 17'", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_shell_c_heredoc_body_can_be_inert_stdin(monkeypatch):
    """For `bash -c`, the `-c` string is executable shell and heredoc stdin can be data."""
    command = "bash -c 'cat >/tmp/msg' <<EOF\ngh pr merge 17\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'bash' <<EOF\ngh pr merge 17\nEOF",
        "bash -c 'sh' <<EOF\ngh pr merge 17\nEOF",
        "bash -c 'source /dev/stdin' <<EOF\ngh pr merge 17\nEOF",
        "bash -c '. /dev/stdin' <<EOF\ngh pr merge 17\nEOF",
        "bash /dev/stdin <<EOF\ngh pr merge 17\nEOF",
        "sh /proc/self/fd/0 <<EOF\ngh pr merge 17\nEOF",
        "bash-5.1 <<EOF\ngh pr merge 17\nEOF",
    ],
)
def test_shell_c_heredoc_body_blocks_when_c_script_executes_stdin(monkeypatch, command):
    """A `bash -c` payload can still execute heredoc stdin as shell source."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_non_stdin_heredoc_fd_to_shell_is_inert(monkeypatch):
    """`2<<EOF` feeds fd 2, not shell stdin, so its body is not executable shell."""
    command = "bash 2<<EOF\ngh pr merge 17\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_non_stdin_heredoc_fd_before_pipeline_shell_is_inert(monkeypatch):
    """A fd-2 heredoc on an upstream command is not the script piped into the shell."""
    command = "cat 2<<EOF | bash\ngh pr merge 17\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_explicit_stdin_heredoc_fd_to_shell_blocks(monkeypatch):
    """`0<<EOF` is still shell stdin and must be inspected."""
    command = "bash 0<<EOF\ngh pr merge 17\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_heredoc_body_is_not_executable_for_later_unrelated_shell_command(monkeypatch):
    """A later shell command after `;` does not receive the preceding heredoc body."""
    command = "cat <<EOF; bash\ngh pr merge 17\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_nested_heredoc_body_inside_shell_heredoc_can_be_inert_stdin(monkeypatch):
    """A shell script can contain its own heredoc whose body is data, not executable shell."""
    command = "bash <<EOF\ncat <<INNER\ngh pr merge 17\nINNER\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_second_heredoc_on_same_line_can_be_executable_shell(monkeypatch):
    """Each heredoc opener must be classified by its own command span, not the first one."""
    command = "cat <<A; bash <<B\ntext\nA\ngh pr merge 17\nB"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_second_heredoc_in_pipeline_can_be_inert_when_owned_by_cat(monkeypatch):
    """A shell in an earlier pipeline component must not make a later cat heredoc executable."""
    command = "bash <<A | cat <<B\necho ok\nA\ngh pr merge 17\nB"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_unclosed_shell_heredoc_body_is_still_executable(monkeypatch):
    """Shells execute an unterminated heredoc body up to EOF, so the hook must inspect it."""
    command = "sh <<EOF\ngh pr merge 17"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_function_definition_with_raw_merge_body_is_not_executed(monkeypatch):
    """Defining a helper function is not itself a merge execution."""
    for command in (
        'deploy() { gh pr merge "$1"; }',
        'deploy() {\n  gh pr merge "$1"\n}',
    ):
        out, _err, code = _run(command, monkeypatch)
        assert code == 0, command
        assert _decision(out) == "allow"


def test_function_definition_with_case_arm_raw_merge_body_is_not_executed(monkeypatch):
    """A case arm inside an inert function definition is not a currently executing case arm."""
    for command in (
        'deploy(){ case x in x) gh pr merge "$1";; esac; }',
        "function deploy { case x in x) gh pr merge 1;; esac; }",
        "function deploy() { case x in x) gh pr merge 1;; esac; }",
        "deploy() ( case x in x) gh pr merge 1;; esac )",
    ):
        out, _err, code = _run(command, monkeypatch)
        assert code == 0, command
        assert _decision(out) == "allow"


def test_function_definition_with_nested_raw_merge_body_is_not_executed(monkeypatch):
    """Nested executable shell in a function body is inert until the function is called."""
    for command in (
        'deploy() { echo "$(gh pr merge 1)"; }',
        'function deploy() { echo "$(gh pr merge 1)"; }',
    ):
        out, _err, code = _run(command, monkeypatch)
        assert code == 0, command
        assert _decision(out) == "allow"


def test_called_function_with_merge_text_literal_allows(monkeypatch):
    """Calling a function that only prints merge instructions is not a raw merge execution."""
    command = 'warn(){ echo "run: cd /x && gh pr merge 1"; }; warn'
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_nested_inert_function_call_is_not_executed(monkeypatch):
    """A wrapper function calling a tainted function is inert until the wrapper is called."""
    command = "foo(){ gh pr merge 1; }; bar(){ foo; }"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "source",
    [
        "deploy(){ gh pr merge 1; }",
        "deploy() { gh pr merge 1; }",
        "function deploy { gh pr merge 1; }",
        "function deploy() { gh pr merge 1; }",
    ],
)
def test_token_function_definition_shape_handles_expanded_parentheses(source):
    """Token-level function recognition must match shlex punctuation-expanded tokens."""
    tokens = hook._source_tokens(source)
    end = hook._function_definition_end(tokens, 0)
    assert end == len(tokens)
    assert hook._tokens_without_function_definitions(tokens) == []


def test_function_redefinition_after_clean_call_is_not_executed(monkeypatch):
    """Function bodies use the definition current at the call site, not later redefinitions."""
    command = "deploy(){ echo ok; }; deploy; deploy(){ gh pr merge 1; }"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_inert_function_with_quoted_brace_body_is_not_executed(monkeypatch):
    """Quoted braces inside a function body must not terminate the function parser early."""
    command = 'deploy(){ echo "}"; gh pr merge 1; }'
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_deep_nested_merge_context_fails_closed():
    """Deep recursive shell constructs fail closed instead of recursing without bound."""
    command = "$(" * (hook._MAX_DETECTION_DEPTH + 2) + "gh pr merge 1" + ")" * (hook._MAX_DETECTION_DEPTH + 2)
    assert hook._command_contains_gh_pr_merge(command) is None


def test_deep_nested_shell_heredoc_source_reduction_fails_closed():
    """Executable heredoc body reduction shares the recursion budget."""
    command = ("bash <<E\n" * (hook._MAX_DETECTION_DEPTH + 2)) + "gh pr merge 1"
    assert hook._command_contains_gh_pr_merge(command) is None


def test_shell_c_source_reduction_shares_recursion_budget(monkeypatch):
    """Shell `-c` helpers must not reset depth while preparing nested executable source."""
    monkeypatch.setattr(hook, "_MAX_DETECTION_DEPTH", 0)
    command = "bash -c 'bash <<E\ngh pr merge 1\nE'"
    assert hook._command_contains_gh_pr_merge(command) is None


def test_detection_node_budget_fails_closed_for_merge_like_input(monkeypatch):
    """The detector has a shared work budget, not only a recursion-depth budget."""
    monkeypatch.setattr(hook, "_DETECTION_NODE_BUDGET", 1)
    assert hook._command_contains_gh_pr_merge("echo $(gh pr merge 1)") is None


def test_detector_budget_failure_blocks_with_generic_reason(monkeypatch):
    """Budget exhaustion is a fail-closed parser limit, not necessarily an unbalanced quote."""
    monkeypatch.setattr(hook, "_DETECTION_NODE_BUDGET", 1)
    out, _err, code = _run("echo $(gh pr merge 1)", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "could not fully inspect" in out
    assert "unbalanced quotes" not in out


def test_over_budget_padded_raw_merge_fails_closed(monkeypatch):
    """Over-budget commands with ordered gh/pr/merge tokens block regardless of token distance."""
    command = "gh" + (" " * (hook._MAX_COMMAND_CHARS + 10)) + "pr merge 1"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_over_budget_quoted_merge_text_allows(monkeypatch):
    """Parseable quoted text remains inert even when the command exceeds the scan budget."""
    command = 'echo "' + ("x" * hook._MAX_COMMAND_CHARS) + ' gh pr merge 1"'
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_over_budget_gh_pr_create_body_merge_text_allows(monkeypatch):
    """A large PR body mentioning the forbidden command is still text, not a merge."""
    command = "gh pr create --body " + shlex.quote(("x" * hook._MAX_COMMAND_CHARS) + " gh pr merge 1")
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_over_budget_gh_pr_create_body_with_shell_words_allows(monkeypatch):
    """Oversize prose should not fail closed just because words contain shell substrings."""
    body = ("bash users should finish this before ship " * 2000) + " gh pr merge 1"
    command = "gh pr create --body " + shlex.quote(body)
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_over_budget_called_function_with_raw_merge_blocks(monkeypatch):
    """Oversize parseable commands still detect called tainted shell functions."""
    command = "echo " + ("x" * hook._MAX_COMMAND_CHARS) + "; f(){ gh pr merge 1; }; f"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo {filler}; printf 'gh pr merge 1\\n' | bash",
        "echo {filler}; bash < <(printf 'gh pr merge 1\\n')",
        "bash <<EOF\n{filler}\ngh pr merge 1\nEOF",
    ],
)
def test_over_budget_executable_shell_sources_fail_closed(monkeypatch, command):
    """Oversize commands must not skip executable heredoc/pipeline/process-substitution scans."""
    command = command.format(filler="x" * (hook._MAX_COMMAND_CHARS + 10))
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_over_budget_inert_heredoc_merge_text_allows(monkeypatch):
    """Large heredoc data mentioning gh/pr/merge remains stdin text, not executable shell."""
    command = "cat <<EOF\n" + ("x" * hook._MAX_COMMAND_CHARS) + "\ngh pr merge 1\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_unexpected_detector_exception_with_merge_hint_fails_closed(monkeypatch):
    """Unexpected detector exceptions must fail closed for merge-like commands."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic detector failure")

    monkeypatch.setattr(hook, "_source_has_called_function_with_gh_pr_merge", _boom)
    assert hook._command_contains_gh_pr_merge("gh pr merge 1") is None


def test_unexpected_detector_exception_with_normalized_merge_hint_fails_closed(monkeypatch):
    """Fail-closed hints use normalized shell text, not only the raw encoded command."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic detector failure")

    monkeypatch.setattr(hook, "_source_has_called_function_with_gh_pr_merge", _boom)
    assert hook._command_contains_gh_pr_merge("bash -c $'\\x67h pr merge 1'") is None


def test_main_detector_exception_without_merge_hint_allows_with_warning(monkeypatch):
    """Detector exceptions before inner parsing do not brick unrelated commands."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic detector failure")

    monkeypatch.setattr(hook, "_command_contains_gh_pr_merge", _boom)
    out, err, code = _run("echo ok", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    assert "detector raised RuntimeError" in err


def test_unexpected_detector_exception_without_merge_hint_allows_with_warning(monkeypatch, capsys):
    """Unexpected detector failures on unrelated commands remain visible but do not block."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic detector failure")

    monkeypatch.setattr(hook, "_source_has_called_function_with_gh_pr_merge", _boom)
    assert hook._command_contains_gh_pr_merge("echo ok") is False
    assert "detector raised RuntimeError" in capsys.readouterr().err


def test_detector_smoke_inputs_are_deterministic():
    """A fixed corpus of odd shell text should not raise or vary across detector calls."""
    samples = [
        "echo ok",
        "printf '%s\n' 'gh pr merge text only'",
        "deploy(){ echo $(printf '%s' ok); }",
        "case x in x) echo ok;; esac",
        "bash -c 'cat <<EOF\ntext\nEOF'",
        "echo $'quoted\\ntext'",
    ]
    for command in samples:
        first = hook._command_contains_gh_pr_merge(command)
        second = hook._command_contains_gh_pr_merge(command)
        assert first in {True, False, None}
        assert second == first


def test_called_function_unbalanced_merge_context_fails_closed(monkeypatch):
    """Called function bodies keep fail-closed behavior for unparseable merge-like shell."""
    command = "deploy(){ echo 'unterminated gh pr merge 1; }; deploy"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "deploy(){ gh pr merge 1; }; deploy",
        "deploy(){ case x in x) gh pr merge 1;; esac; }; deploy",
        'deploy(){ echo "$(gh pr merge 1)"; }; deploy',
        "deploy(){ bash -c 'gh pr merge 1'; }; deploy",
        "deploy(){ gh pr merge 1; }; if deploy; then true; fi",
        "deploy(){ gh pr merge 1; }; time deploy",
        "deploy(){ gh pr merge 1; }; case x in x) deploy;; esac",
        "deploy(){ gh pr merge 1; }; x=$(deploy)",
        "deploy(){ gh pr merge 1; }; echo $(deploy)",
        "deploy(){ gh pr merge 1; }; `deploy`",
        "deploy(){ gh pr merge 1; }; cat <(deploy)",
        "foo(){ gh pr merge 1; }; bar(){ foo; }; bar",
        "deploy(){ gh pr merge 1; }; deploy; deploy(){ echo ok; }",
        "deploy(){ gh pr merge 1; }; eval deploy",
        'deploy(){ echo "}"; gh pr merge 1; }; deploy',
        "deploy(){\n  gh pr merge 1\n}\ndeploy",
        "( deploy(){ gh pr merge 1; }; deploy )",
        "{ deploy(){ gh pr merge 1; }; deploy; }",
        "if true; then deploy(){ gh pr merge 1; }; deploy; fi",
        "case x in x) deploy(){ gh pr merge 1; }; deploy;; esac",
    ],
)
def test_function_definition_called_in_same_command_blocks(monkeypatch, command):
    """Defining then invoking a function with a raw merge executes the merge in this command."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo $(gh pr merge 17 --body 'unclosed)",
        "echo `gh pr merge 17 --body 'unclosed`",
        "cat <(gh pr merge 17 --body 'unclosed)",
    ],
)
def test_nested_unbalanced_merge_context_fails_closed(command, monkeypatch):
    """A parse failure inside executable nested shell must keep the hook fail-closed."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── The FP fix — commands with "gh pr merge" as text must be ALLOWED ───────────────────────

def test_allow_gh_pr_create_with_merge_in_body(monkeypatch):
    """THE KEY BUG FIX: gh pr create whose --body contains 'gh pr merge' must NOT be blocked."""
    cmd = 'gh pr create --title "My PR" --body "After review, run gh pr merge 5 to land it"'
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_comment_with_merge_in_body(monkeypatch):
    """gh pr comment whose body text includes 'gh pr merge' must be allowed."""
    cmd = "gh pr comment 5 --body 'use gh pr merge to land this'"
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_echo_with_merge_text(monkeypatch):
    """An echo command printing 'gh pr merge' must not trigger the block."""
    out, _err, code = _run('echo "remember to gh pr merge 123"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_grep_for_merge_string(monkeypatch):
    """grep searching for 'gh pr merge' in a file must not be blocked."""
    out, _err, code = _run("grep -r 'gh pr merge' .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_merge_literal_inside_heredoc_body(monkeypatch):
    """A heredoc body is stdin text for `cat`, not shell code to inspect as a command."""
    command = "cat <<'EOF'\ngh pr merge 123\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\n$(gh pr merge 123)\nEOF",
        "cat <<EOF\n`gh pr merge 123`\nEOF",
        "cat <<EOF\ncat <(gh pr merge 123)\nEOF",
        "echo ok # $(gh pr merge 123)",
        "echo ok # `gh pr merge 123`",
        "echo ok # cat <(gh pr merge 123)",
    ],
)
def test_allow_nested_merge_text_inside_inert_heredoc_or_comment(monkeypatch, command):
    """Nested-shell syntax in inert stdin/comment text must not be treated as executable."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_merge_literal_in_rsh_stdin(monkeypatch):
    """`rsh` is not a local shell interpreter that executes heredoc stdin as shell code."""
    command = "rsh host <<'EOF'\ngh pr merge 123\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_merge_literal_inside_heredoc_after_multiline_quote(monkeypatch):
    """A heredoc after a closing multiline quote still has an inert body for non-shell commands."""
    command = 'echo "multi\nline" <<EOF\ngh pr merge 99\nEOF'
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_merge_literal_across_quoted_newlines(monkeypatch):
    """Quoted newlines stay inside one argument and must not become command separators."""
    command = 'printf "before\ngh pr merge 123\nafter"'
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Sanctioned paths must be ALLOWED ───────────────────────────────────────────────────────

def test_allow_gh_ship(monkeypatch):
    out, _err, code = _run("gh ship 123", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_view(monkeypatch):
    out, _err, code = _run("gh pr view 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_create(monkeypatch):
    out, _err, code = _run("gh pr create --title 'feat: add X'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_list(monkeypatch):
    out, _err, code = _run("gh pr list", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_checkout(monkeypatch):
    out, _err, code = _run("gh pr checkout 7", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_ship_sh(monkeypatch):
    out, _err, code = _run("./ship.sh 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pr_ship_sh(monkeypatch):
    out, _err, code = _run("./pr-ship.sh 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── REGRESSION: the OLD self-service escape hatch is DEAD ────────────────────────────────────

def test_env_override_no_longer_allows(monkeypatch):
    """REGRESSION: `ALLOW_RAW_PR_MERGE=1` (+ `_REASON`) used to allow a raw merge — it must NO
    LONGER; the merge still BLOCKs."""
    out, _err, code = _run(
        "gh pr merge 5 --squash",
        monkeypatch,
        {"ALLOW_RAW_PR_MERGE": "1", "ALLOW_RAW_PR_MERGE_REASON": "CI provider outage"},
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_inline_sentinel_no_longer_allows(monkeypatch):
    """REGRESSION: an inline `# no-ship-guard: <reason>` sentinel no longer allows — still BLOCK."""
    out, _err, code = _run(
        "gh pr merge 5 --admin  # no-ship-guard: hotfix during provider outage",
        monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── external Telegram hatch escalation (replaces the OLD self-service escape hatch) ──────────
def test_hatch_unset_blocks_with_howto(monkeypatch):
    out, _err, code = _run("gh pr merge 5", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert _HATCH_ENV in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` is an invalid request: deny WITHOUT contacting Telegram (the fake would touch a
    marker — it must not exist)."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run("gh pr merge 5", monkeypatch, {_HATCH_ENV: "1"}, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\n" 'printf "approved by Telegram tap\\n"\n' "exit 0\n",
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "gh pr merge 5", monkeypatch,
        {_HATCH_ENV: "Ship gate is down; manual verify done, hotfix must land."}, cwd=tmp_path,
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_justification_exit1_blocks_citing_denied(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "gh pr merge 5", monkeypatch,
        {_HATCH_ENV: "Ship gate is down; manual verify done, hotfix must land."}, cwd=tmp_path,
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── Path-qualified and subshell forms — should BLOCK ───────────────────────────────────────

def test_block_path_qualified_gh(monkeypatch):
    """/opt/homebrew/bin/gh pr merge 5 must still be blocked (basename check)."""
    out, _err, code = _run("/opt/homebrew/bin/gh pr merge 5 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh -R owner/repo pr merge 5",
        "gh --repo owner/repo pr merge 5",
        "gh --repo=owner/repo pr merge 5",
        "gh --hostname github.com pr merge 5",
        "gh --config-dir /tmp/gh pr merge 5",
        "gh --verbose pr merge 5",
        "gh pr --repo owner/repo merge 5",
    ],
)
def test_block_gh_pr_merge_with_inherited_repo_flags(monkeypatch, command):
    """GitHub CLI inherited repo flags do not make a raw PR merge safe."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "3>&1 gh pr merge 5",
        "GH_TOKEN=x 3>&1 gh pr merge 5",
        "exec 3>&1 gh pr merge 5",
        "gh pr merge 5 --body -h",
        "gh pr merge 5 --body --help",
        "gh pr merge 5 -b --help",
        "gh pr merge 5 -F --help",
        "gh pr merge 5 --body-file --help",
        "gh pr merge 5 -t --help",
        "gh pr merge 5 -A --help",
        "gh pr merge 5 -A -h",
        "gh pr merge 5 --author-email --help",
        "sudo gh --repo owner/repo pr merge 5",
    ],
)
def test_block_raw_merge_with_redirections_and_flag_values(monkeypatch, command):
    """Redirections and help-looking flag values do not hide a live raw merge."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh --help pr merge 5",
        "gh --version pr merge 5",
        "gh pr --help merge 5",
        "gh pr merge --help 5",
        "gh pr merge -h 5",
        "gh -- pr merge 5",
    ],
)
def test_allow_gh_help_and_version_forms(monkeypatch, command):
    """GitHub CLI help/version flags do not execute a raw merge."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_subshell_gh_pr_merge(monkeypatch):
    """( gh pr merge 5 ) — subshell grouping must not bypass detection."""
    out, _err, code = _run("( gh pr merge 5 )", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_brace_group_gh_pr_merge(monkeypatch):
    """{ gh pr merge 5; } — brace grouping must not bypass detection."""
    out, _err, code = _run("{ gh pr merge 5; }", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "sudo -u root gh pr merge 5",
        "sudo -u root -- gh pr merge 5",
        "doas -u root gh pr merge 5",
        "sudo -b gh pr merge 5",
        "command -p gh pr merge 5",
        "nice --adjustment=5 gh pr merge 5",
        "env -S 'gh pr merge 5'",
        "env --split-string='gh pr merge 5'",
        "timeout 30 gh pr merge 5",
        "unbuffer gh pr merge 5",
        "ionice -c 2 gh pr merge 5",
        "taskset 0x1 gh pr merge 5",
        "taskset -c 0 gh pr merge 5",
        "chrt 10 gh pr merge 5",
        "chrt -f 50 gh pr merge 5",
        "flock /tmp/l gh pr merge 5",
        "flock -w 5 /tmp/l gh pr merge 5",
        "setpriv --reuid 1000 gh pr merge 5",
        "setpriv --ptracer any gh pr merge 5",
        "runuser -u root gh pr merge 5",
        "runuser --user root -- gh pr merge 5",
        "sudo --chdir /tmp gh pr merge 5",
        "sudo --chdir=/tmp gh pr merge 5",
        "bash -euo pipefail -c 'gh pr merge 5'",
        "command bash -euo pipefail -c 'gh pr merge 5'",
        "watch -n 1 gh pr merge 5",
        "watch --interval=1 gh pr merge 5",
        "parallel --will-cite gh pr merge ::: 5",
        "parallel -j 1 gh pr merge ::: 5",
    ],
)
def test_block_wrapper_gh_pr_merge_with_options(monkeypatch, command):
    """Common command wrappers and their options still execute the wrapped raw merge."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Fail-closed paths ──────────────────────────────────────────────────────────────────────

def test_unbalanced_quotes_merge_blocks(monkeypatch):
    """Unbalanced quote on a command containing a merge pattern → fail closed (block)."""
    out, _err, code = _run("gh pr merge 5 --body 'unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_allows(monkeypatch):
    """Unbalanced quote on an unrelated command → allow (not a merge attempt)."""
    out, _err, code = _run("grep won't file", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    assert _err == ""


def test_malformed_event_blocks(monkeypatch):
    """A JSON parse error on the event → fail closed."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


def test_non_object_event_blocks(monkeypatch):
    """Valid JSON with the wrong event shape still fails closed."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


def test_non_object_args_event_does_not_crash(monkeypatch):
    """A malformed nested args field should fall back to the top-level command contract."""
    out, err = io.StringIO(), io.StringIO()
    event = {"args": "invalid", "command": "gh pr merge 1"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


def test_missing_cwd_does_not_crash_initialization(monkeypatch):
    """A deleted process cwd should not prevent the hook from emitting a decision."""
    def _missing_cwd():
        raise FileNotFoundError("cwd removed")

    monkeypatch.setattr(hook.os, "getcwd", _missing_cwd)
    out, _err, code = _run("echo ok", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_empty_command_allows(monkeypatch):
    """An empty command string has no segments → nothing to block."""
    out, _err, code = _run("", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── `\`-newline line continuation must not hide the merge (Codex review on #231) ────────────


def test_line_continuation_does_not_hide_merge():
    """shlex (whitespace_split=False) emits a standalone `\\n` token for a `\\`-newline line
    continuation. After the leading `VAR=` assignment is stripped that token would become
    argv[0], so `_is_gh_pr_merge` saw `argv[0] == '\\n'` and reported NO merge → the raw merge was
    ALLOWED with no approval. Detection must survive the continuation."""
    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="x" \\\n  gh pr merge 123 --admin'
    assert hook._command_contains_gh_pr_merge(command) is True


def test_hatch_inline_line_continuation_reaches_tg_ctl(tmp_path, monkeypatch):
    """End-to-end for the exact README inline-hatch shape (`VAR="why" \\`+newline+`gh pr merge`):
    it must be DETECTED as a raw merge AND route the inline justification to tg-ctl (env var NOT
    exported). If the `\\`-newline still hid the merge, the hook would `emit("allow")` WITHOUT
    ever calling the mocked tg-ctl, so the question file would not be written."""
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f'printf "%s" "$2" > "{question}"\nexit 0\n')
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down, manual verify done" \\\n'
        '  gh pr merge 123 --admin'
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)  # env NOT set — inline only
    assert code == 0 and _decision(out) == "allow"
    assert "ship gate down, manual verify done" in question.read_text()


def test_hatch_inline_assignment_on_merge_segment_reaches_tg_ctl(tmp_path, monkeypatch):
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f'printf "%s" "$2" > "{question}"\nexit 0\n')
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down, manual verify done" '
        "gh pr merge 123 --admin"
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    text = question.read_text()
    assert "ship gate down, manual verify done" in text
    assert command in text


def test_hatch_inline_assignment_inside_group_reaches_tg_ctl(tmp_path, monkeypatch):
    """Leading grouping tokens must not hide an inline hatch assignment."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = '( RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" gh pr merge 2 )'
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_empty_inline_hatch_assignment_does_not_request_tg_ctl(tmp_path, monkeypatch):
    """An empty inline hatch value is invalid and must not call the approval channel."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE= gh pr merge 2"
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in out
    assert "is blank" in out
    assert not marker.exists()


def test_empty_environment_hatch_assignment_does_not_request_tg_ctl(tmp_path, monkeypatch):
    """A present blank process env hatch is invalid, matching inline blank handling."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "gh pr merge 2",
        monkeypatch,
        env={"RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE": ""},
        cwd=tmp_path,
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in out
    assert "is blank" in out
    assert not marker.exists()


def test_hatch_inline_assignment_on_unrelated_segment_does_not_approve(tmp_path, monkeypatch):
    """Inline VAR=x only applies to its shell segment, not a later raw merge command."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" echo hi; gh pr merge 2'
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" not in out
    assert not marker.exists()


def test_hatch_inline_assignment_does_not_approve_later_raw_merge(tmp_path, monkeypatch):
    """Inline approval for one raw-merge segment must not carry a second raw merge."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1; gh pr merge 2'
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


@pytest.mark.parametrize(
    "command",
    [
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1; printf "gh pr merge 2\\n" | bash',
        'printf "gh pr merge 2\\n" | bash; RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" printf "gh pr merge 1\\n" | bash; printf "gh pr merge 2\\n" | bash',
        'printf "gh pr merge 1\\n" | bash; RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" printf "gh pr merge 2\\n" | bash',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1; f(){ gh pr merge 2; }; f',
        'f(){ gh pr merge 2; }; RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" f; gh pr merge 1',
        'f(){ gh pr merge 1; }; g(){ gh pr merge 2; }; RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" f; g',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" bash <<EOF\ngh pr merge 1\nEOF\ngh pr merge 2',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1 $(gh pr merge 2)',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1 `gh pr merge 2`',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1 "$(gh pr merge 2)"',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1 "`gh pr merge 2`"',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1 <(gh pr merge 2)',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" gh pr merge 1 >(gh pr merge 2)',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" bash -c "gh pr merge 1; gh pr merge 2"',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="approve" eval "gh pr merge 1; gh pr merge 2"',
    ],
)
def test_hatch_inline_assignment_does_not_approve_other_merge_sources(tmp_path, monkeypatch, command):
    """Inline approval must not carry unapproved raw merge sources."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_inline_assignment_on_nested_merge_segment_reaches_tg_ctl(tmp_path, monkeypatch):
    """Inline approval applies to the same simple command even when the merge is nested."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" echo $(gh pr merge 2)'
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


@pytest.mark.parametrize(
    "command",
    [
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" echo "$(gh pr merge 2)"',
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" echo "`gh pr merge 2`"',
    ],
)
def test_hatch_inline_assignment_on_quoted_nested_merge_reaches_tg_ctl(tmp_path, monkeypatch, command):
    """Quoted command substitutions still execute and can use the one-source inline hatch."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_inline_assignment_on_shell_heredoc_reaches_tg_ctl(tmp_path, monkeypatch):
    """Inline approval on the shell heredoc command applies to its executable body."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" bash <<EOF\n'
        "gh pr merge 2\n"
        "EOF"
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_inline_assignment_after_here_string_still_matches_heredoc(tmp_path, monkeypatch):
    """A preceding here-string is not a heredoc and must not shift inline hatch body ownership."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'cat <<< "gh pr merge text"; RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why bash <<EOF\n'
        "gh pr merge 2\n"
        "EOF"
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_inline_assignment_on_pipeline_to_shell_reaches_tg_ctl(tmp_path, monkeypatch):
    """Inline approval applies to the pipeline group that statically feeds a raw merge to shell."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why printf 'gh pr merge 2\\n' | bash"
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


@pytest.mark.parametrize(
    "command",
    [
        "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why bash < <(printf 'gh pr merge 2\\n')",
        "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why source <(printf 'gh pr merge 2\\n')",
        "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why bash <(printf 'gh pr merge 2\\n')",
    ],
)
def test_hatch_inline_assignment_on_process_substitution_to_shell_reaches_tg_ctl(
    tmp_path,
    monkeypatch,
    command,
):
    """Inline approval applies when a process substitution statically feeds shell input."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_inline_assignment_on_called_function_reaches_tg_ctl(tmp_path, monkeypatch):
    """Inline approval applies when the approved segment calls a tainted shell function."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = "f(){ gh pr merge 2; }; RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why f"
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


@pytest.mark.parametrize(
    "command",
    [
        "case x in x) RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why gh pr merge 2;; esac",
        "case x in x) RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why bash -c 'gh pr merge 2';; esac",
    ],
)
def test_hatch_inline_assignment_on_case_arm_reaches_tg_ctl(tmp_path, monkeypatch, command):
    """Inline approval applies inside the case arm that executes the raw merge."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_inline_assignment_on_other_heredoc_segment_does_not_approve(tmp_path, monkeypatch):
    """An inline hatch on a cat heredoc must not approve a later shell heredoc body."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=why cat <<A; bash <<B\n"
        "text\n"
        "A\n"
        "gh pr merge 2\n"
        "B"
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" not in out
    assert not marker.exists()


def test_hatch_process_env_takes_precedence_over_inline_assignment(tmp_path, monkeypatch):
    """The shared hatch contract gives the real environment precedence over inline assignments."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" gh pr merge 2'
    out, _err, code = _run(
        command,
        monkeypatch,
        env={"RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE": ""},
        cwd=tmp_path,
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in out
    assert "is blank" in out
    assert not marker.exists()


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 1; gh pr merge 2",
        "gh pr merge 1 && gh pr merge 2",
    ],
)
def test_hatch_process_env_does_not_approve_multiple_merge_sources(tmp_path, monkeypatch, command):
    """A process env hatch request is still scoped to exactly one executable raw-merge source."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        command,
        monkeypatch,
        env={"RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE": "ship gate down"},
        cwd=tmp_path,
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block", (
        "multi-merge commands must fail closed even with a global hatch request"
    )
    assert "hatch escalation denied" not in out
    assert not marker.exists(), "multi-merge commands must not contact tg approval"


def test_hatch_duplicate_inline_assignments_use_last_value(tmp_path, monkeypatch):
    """Shell inline assignments are applied left-to-right, so the last value is authoritative."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE= '
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" gh pr merge 2'
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_duplicate_inline_assignments_blank_last_denies(tmp_path, monkeypatch):
    """A later blank inline assignment overrides an earlier valid hatch request."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" '
        "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE= gh pr merge 2"
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in out
    assert "is blank" in out
    assert not marker.exists()


def test_hatch_exception_after_merge_detection_blocks(monkeypatch):
    """Once a merge is detected, hatch parsing failures must not escape through the host."""
    def _boom(_command):
        raise RuntimeError("synthetic hatch failure")

    monkeypatch.setattr(hook, "_hatch_env_for_command", _boom)
    out, err, code = _run("gh pr merge 2", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation check failed" in out
    assert "synthetic hatch failure" in err


def test_unset_hatch_keeps_normal_raw_merge_block(monkeypatch):
    """No hatch request should fall through to the normal block, not a synthetic blank denial."""
    out, _err, code = _run("gh pr merge 2", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" not in out
    assert "blank" not in out


def test_hatch_inline_assignment_inside_heredoc_body_is_ignored(tmp_path, monkeypatch):
    """Heredoc text must not supply the hatch approval for a later executable raw merge."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        "cat <<EOF\n"
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="from heredoc" gh pr merge 1\n'
        "EOF\n"
        "gh pr merge 2"
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_inline_assignment_inside_nested_heredoc_body_is_ignored(tmp_path, monkeypatch):
    """Raw hatch fallback must skip heredoc bodies even when that body contains heredoc syntax."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        "cat <<OUT\n"
        "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE=from_heredoc bash <<EOF\n"
        "gh pr merge 1\n"
        "EOF\n"
        "OUT\n"
        "gh pr merge 2"
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_inline_parser_is_local_not_private_helper(tmp_path, monkeypatch):
    """Inline hatch parsing must not depend on the shared module's private parser."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    monkeypatch.delattr(hook.hatch_escalation, "_parse_inline_hatch_value", raising=False)

    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down" gh pr merge 2'
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)

    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_env_var_uses_shared_helper_when_present(monkeypatch):
    """The hook uses the shared hatch env API when present without requiring it to exist."""
    monkeypatch.setattr(
        hook.hatch_escalation,
        "hatch_env_var",
        lambda hook_id: f"RIG_HATCH_REQUEST_{hook_id.upper().replace('-', '_')}",
        raising=False,
    )
    assert hook._hatch_env_var() == _HATCH_ENV


def test_hatch_env_var_falls_back_when_shared_helper_is_absent(monkeypatch):
    """Older hatch libraries without hatch_env_var still use the documented env name."""
    monkeypatch.delattr(hook.hatch_escalation, "hatch_env_var", raising=False)
    assert hook._hatch_env_var() == _HATCH_ENV


def test_hatch_env_var_falls_back_when_shared_helper_raises(monkeypatch):
    """A broken optional hatch_env_var helper should not block unrelated commands."""
    def _boom(_hook_id):
        raise TypeError("old helper shape")

    monkeypatch.setattr(hook.hatch_escalation, "hatch_env_var", _boom, raising=False)
    assert hook._hatch_env_var() == _HATCH_ENV


def test_hatch_request_fallback_temporarily_sets_process_env(monkeypatch):
    """Older hatch libraries without env= still receive sanitized inline hatch via os.environ."""
    monkeypatch.delenv(_HATCH_ENV, raising=False)

    class _Result:
        should_stop = True
        approved = True
        reason = "approved"

    def _legacy_request(_hook_id, _context, *, cwd, command):
        assert cwd == "/repo"
        assert command == "gh pr merge 2"
        assert os.environ[_HATCH_ENV] == "why"
        return _Result()

    monkeypatch.setattr(hook.hatch_escalation, "request_hatch_approval", _legacy_request)
    result = hook._request_hatch_approval(
        {"hook": "block-raw-pr-merge", "command": "gh pr merge 2"},
        cwd="/repo",
        hatch_env={_HATCH_ENV: "why"},
        env_var=_HATCH_ENV,
        command="gh pr merge 2",
    )
    assert result.approved is True
    assert _HATCH_ENV not in os.environ


def test_hatch_request_env_signature_receives_env_only_when_present(monkeypatch):
    """Newer hatch libraries get env= for sanitized hatch data, but never env=None."""
    sentinel = object()
    calls = []

    class _Result:
        should_stop = True
        approved = True
        reason = "approved"

    def _request(_hook_id, _context, *, cwd, command, env=sentinel):
        calls.append(env)
        return _Result()

    monkeypatch.setattr(hook.hatch_escalation, "request_hatch_approval", _request)
    hook._request_hatch_approval(
        {"hook": "block-raw-pr-merge", "command": "gh pr merge 2"},
        cwd="/repo",
        hatch_env={_HATCH_ENV: "why"},
        env_var=_HATCH_ENV,
        command="gh pr merge 2",
    )
    hook._request_hatch_approval(
        {"hook": "block-raw-pr-merge", "command": "gh pr merge 2"},
        cwd="/repo",
        hatch_env=None,
        env_var=_HATCH_ENV,
        command="gh pr merge 2",
    )

    assert calls[0][_HATCH_ENV] == "why"
    assert calls[1] is sentinel


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))

"""Clean-room proof for the agents-hooks/v1 -> omp (oh-my-pi) extension bridge.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_omp_hook_bridge.py -q

omp loads Bun-executed TS/JS extension modules that register `pi.on(eventName, handler)`
handlers. The rig-provisioned extension (`extension.ts`) calls this Python dispatcher with
omp's `tool_call` / `tool_result` payloads so installed `agents-hooks/v1` descriptors can
run for omp too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import shlex

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import omp_hook_bridge.dispatch as dispatch  # noqa: E402

BLOCK_RAW_PR_MERGE = _REPO / "agent-hooks" / "block-raw-pr-merge" / "block_raw_pr_merge.py"
BLOCK_SECRETS_WRITE = _REPO / "agent-hooks" / "block-secrets-write" / "block_secrets_write.py"
BACKGROUND_SUBAGENT_GATE = _REPO / "agent-hooks" / "background-subagent-gate" / "background_subagent_gate.py"
WORKTREE_ONLY_WRITES = _REPO / "agent-hooks" / "worktree-only-writes" / "worktree_only_writes.py"
NO_LONG_INLINE_PROCESS = _REPO / "agent-hooks" / "no-long-inline-process" / "no_long_inline_process.py"
REQUIRE_REVIEW_BEFORE_COMMIT = _REPO / "agent-hooks" / "require-review-before-commit" / "require_review.py"

_EXTENSION_TS = _REPO / "lib" / "omp_hook_bridge" / "extension.ts"


def _install_descriptor(
    hooks_dir: Path,
    *,
    hook_id: str,
    point: str,
    cmd: Path,
    on_error: str = "closed",
    priority: int = 10,
) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "id": hook_id,
        "point": point,
        "cmd": str(cmd),
        "priority": priority,
        "timeout_ms": 3000,
        "on_error": on_error,
        "description": f"test descriptor for {hook_id}",
    }
    path = hooks_dir / f"{hook_id}.{point}.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _run_dispatch(event_name: str, omp_event: dict, *, hooks_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["OMP_HOOKS_DIR"] = str(hooks_dir)
    # Hermetic file log: bridge diagnostics must never touch process stderr (TUI
    # noise), so they go to this file; default it into the test sandbox unless
    # the caller overrides it explicitly.
    env.setdefault("HOOKS_DEBUG_LOG", str(Path(hooks_dir).parent / "hooks-debug.log"))
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "omp_hook_bridge", event_name],
        input=json.dumps(omp_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# --- point_for_event -----------------------------------------------------------------------


def test_point_for_event_maps_omp_tools_to_v1_points():
    assert dispatch.point_for_event("tool_call", "bash") == "pre-bash"
    assert dispatch.point_for_event("tool_call", "edit") == "pre-write"
    assert dispatch.point_for_event("tool_call", "write") == "pre-write"
    assert dispatch.point_for_event("tool_call", "apply_patch") == "pre-write"
    assert dispatch.point_for_event("tool_call", "notebook") == "pre-write"
    assert dispatch.point_for_event("tool_call", "task") == "pre-agent"
    assert dispatch.point_for_event("tool_result", "edit") == "post-write"
    assert dispatch.point_for_event("tool_result", "write") == "post-write"
    assert dispatch.point_for_event("tool_result", "apply_patch") == "post-write"
    assert dispatch.point_for_event("tool_result", "bash") is None
    assert dispatch.point_for_event("tool_result", "task") is None
    assert dispatch.point_for_event("tool_call", "read") is None
    assert dispatch.point_for_event("unknown_event", "bash") is None


def test_point_for_event_maps_eval_and_hub_to_v1_points():
    """agent-tools#614: `eval` (a Python/JS code cell that can write files via
    `Path(...).write_text(...)`/`Bun.write(...)`) and `hub` (whose `op: "start"` launches a
    long-running background process) were absent from every branch, so every installed
    pre-write/pre-bash descriptor silently never ran for either tool call."""
    assert dispatch.point_for_event("tool_call", "eval") == "pre-write"
    assert dispatch.point_for_event("tool_call", "hub") == "pre-bash"
    assert dispatch.point_for_event("tool_result", "eval") == "post-write"
    assert dispatch.point_for_event("tool_result", "hub") is None


def test_point_for_event_is_case_insensitive_and_tolerates_missing_tool():
    assert dispatch.point_for_event("tool_call", "BASH") == "pre-bash"
    assert dispatch.point_for_event("tool_call", None) is None


# --- to_v1_event: bash -----------------------------------------------------------------------


def test_to_v1_event_carries_bash_command_and_metadata():
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "gh pr merge 42 --admin"},
        "cwd": "/repo",
        "toolCallId": "call_1",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["hook_api"] == "agents-hooks/v1"
    assert v1["event_id"] == "call_1"
    assert v1["tool"] == "bash"
    assert v1["point"] == "pre-bash"
    assert v1["command"] == "gh pr merge 42 --admin"
    assert v1["cwd"] == "/repo"
    assert v1["args"]["command"] == "gh pr merge 42 --admin"


def test_to_v1_event_tags_harness_omp():
    """agent-tools#533: every v1 event this bridge produces carries the top-level `harness`
    tag `"omp"`, unconditionally — a module constant, never derived from `omp_event`. Same
    non-forgeable signal as `codex_hook_bridge.HARNESS` / `opencode_hook_bridge.HARNESS`,
    letting orchestrator-stays-thin exempt the whole harness."""
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git status", "harness": "claude-code"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["harness"] == "omp"
    assert dispatch.HARNESS == "omp"


def test_to_v1_event_defaults_cwd_and_event_id_when_absent():
    v1 = dispatch.to_v1_event({"event": "tool_call", "toolName": "bash", "input": {"command": "ls"}}, point="pre-bash")
    assert v1["event_id"] == ""
    assert v1["cwd"] == os.getcwd()


# --- to_v1_event: hub -------------------------------------------------------------------------


def test_to_v1_event_synthesizes_command_for_hub_start():
    """agent-tools#614: `hub op: "start"` launches `application`+`args` as a long-running
    process, the same shape a raw `bash` command call would need `no-long-inline-process` to
    classify — synthesize an argv string from those fields since `hub` has no `command` key."""
    omp_event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "start", "application": "npm", "args": ["run", "test"], "name": "test-runner"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["command"] == "npm run test"
    assert v1["args"]["command"] == "npm run test"


def test_to_v1_event_hub_non_start_op_synthesizes_no_command():
    """Every `hub` op besides `start` (`send`, `list`, `jobs`, ...) manipulates or inspects an
    EXISTING process/job and launches nothing new, so no command is synthesized for it."""
    omp_event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "send", "to": "Peer", "message": "status?"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["command"] == ""
    assert "command" not in v1["args"]


# --- to_v1_event: hub cwd forwarding -----------------------------------------------------------


def test_to_v1_event_hub_absolute_cwd_overrides_session_cwd():
    """Security-review finding: a `hub start` launched with its OWN `cwd` different from the
    session's must be judged against that cwd, not the session's -- otherwise a cwd-sensitive
    pre-bash hook (e.g. a devserver-primary-directory guard) never sees where the process
    actually runs."""
    omp_event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "start", "application": "npm", "args": ["run", "dev"], "cwd": "/other/dir"},
        "cwd": "/session/dir",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["cwd"] == "/other/dir"


def test_to_v1_event_hub_relative_cwd_resolved_against_session_cwd():
    """`hub`'s own docs: cwd defaults to the session directory -- a RELATIVE `input.cwd` is
    resolved against the session cwd, the same relative-path convention
    `worktree_only_writes.py`'s `_target_dir` already uses for a write's target path."""
    omp_event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "start", "application": "npm", "args": ["run", "dev"], "cwd": "sub/dir"},
        "cwd": "/session/dir",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["cwd"] == "/session/dir/sub/dir"


def test_to_v1_event_hub_relative_cwd_collapses_dotdot_traversal():
    """Review finding on agent-tools#614: `os.path.join` does not collapse `..` segments, so an
    un-normalized join of a traversal-laden `input.cwd` (`/session/dir/../../tmp/evil`) still
    textually starts with the session dir -- a cwd-sensitive hook that prefix-matches the
    reported cwd instead of resolving it would misjudge where the process actually launches.
    `os.path.normpath` must collapse the traversal before it is reported."""
    omp_event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "start", "application": "npm", "args": ["run", "dev"], "cwd": "../../tmp/evil"},
        "cwd": "/session/dir",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["cwd"] == "/tmp/evil"


def test_to_v1_event_hub_without_cwd_falls_back_to_session_cwd():
    omp_event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "start", "application": "npm", "args": ["run", "dev"]},
        "cwd": "/session/dir",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["cwd"] == "/session/dir"


def test_to_v1_event_non_hub_tool_cwd_is_unaffected_by_hub_forwarding():
    """The hub-specific cwd override must not leak into any other tool's own `input.cwd`-shaped
    argument (bash has no such field, but a forged one must not be honored either)."""
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "ls", "cwd": "/should-not-matter"},
        "cwd": "/session/dir",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["cwd"] == "/session/dir"


# --- to_v1_event: eval -------------------------------------------------------------------------


def test_to_v1_event_extracts_write_target_from_eval_code():
    """agent-tools#614: an eval cell writing a file via `Path(...).write_text(...)` must be
    normalized onto `args.file_path`/`path`/`content` the same as a real `write` tool call."""
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "from pathlib import Path\nPath('src/secret.py').write_text('API_KEY = abcd1234')\n",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/secret.py"
    assert v1["args"]["path"] == "src/secret.py"
    assert v1["args"]["content"] == "API_KEY = abcd1234"


def test_to_v1_event_eval_write_target_handles_escaped_quotes_and_string_prefix():
    """Security-review finding on agent-tools#614's first version: a naive quote-matching regex
    truncated at an escaped inner quote, or missed a raw-string-prefixed literal entirely --
    either way the real secret token never reached `block-secrets-write`'s scanner."""
    secret = "abcd1234abcd1234"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": 'Path("src/secret.py").write_text("API_KEY = \\"' + secret + '\\"")',
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/secret.py"
    assert secret in v1["args"]["content"]


def test_to_v1_event_eval_write_target_aggregates_content_across_multiple_writes_to_same_path():
    """Security-review finding on agent-tools#614's first version: only the FIRST matched write
    call's content was captured, so a harmless decoy write before the real secret write hid the
    secret from `block-secrets-write` entirely. Every matched write call's content targeting the
    SAME path must be aggregated so a later secret is still visible (a multi-DIFFERENT-path call
    fans out into separate events instead -- see
    `test_eval_multi_target_write_fans_out_one_event_per_scraped_path`)."""
    secret = "abcd1234abcd1234"
    code = "Path(\'src/secret.py\').write_text(\'decoy\')\nPath(\'src/secret.py\').write_text(\'API_KEY = " + secret + "\')\n"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert secret in v1["args"]["content"]


def test_eval_multi_target_write_fans_out_one_event_per_scraped_path():
    """Security-review finding: reporting only the FIRST scraped path let a harmless first
    write's path decide `block-secrets-write`'s allowlist verdict for the WHOLE aggregated
    body, hiding a real secret under a later, different path. A genuinely multi-target eval
    call must fan out one v1 event per path, with that path's OWN content only."""
    secret = "abcd1234abcd1234"
    code = "Path('README.md').write_text('ok')\nPath('src/secret.py').write_text('API_KEY = " + secret + "')\n"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert len(events) == 2
    by_path = {e["args"]["file_path"]: e["args"]["content"] for e in events}
    assert by_path["README.md"] == "ok"
    assert by_path["src/secret.py"] == "API_KEY = " + secret


def test_eval_forged_file_paths_does_not_wipe_scraped_content():
    """Security-review finding: a forged `file_paths` list riding along in an eval call's raw
    args triggered the multi-path fan-out, which then re-derived content from `base["command"]`
    (empty for eval) instead of the real scraped secret -- wiping it to "" and letting it
    through. The scrape's OWN path count must decide fan-out, never a forged raw key, and
    `command` must never carry the eval code (closing the same hole via a different field)."""
    secret = "abcd1234abcd1234"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "Path('src/secret.py').write_text('API_KEY = " + secret + "')",
            "file_paths": ["a.py", "b.py"],
            "command": "a.example",
        },
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert len(events) == 1
    assert events[0]["args"]["file_path"] == "src/secret.py"
    assert secret in events[0]["args"]["content"]
    assert events[0]["command"] == ""


def test_to_v1_event_eval_clears_forged_path_when_no_write_found():
    """A stray/forged `path`/`file_path` key riding along in an eval call's raw args (omp's
    `eval` schema has neither) must not survive into `args` when no real write is recognized --
    otherwise a model could set an ALLOW_PATH-matching fake path to defeat block-secrets-write's
    path-based allowlist while still not writing anything the scrape can see."""
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": "print('no write here')", "path": "fixtures/safe.example"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert "file_path" not in v1["args"]
    assert "path" not in v1["args"]


def test_to_v1_event_eval_recognizes_fs_write_file_sync():
    """Security-review finding: JS `fs.writeFileSync(<path>, <content>)` -- a very common Node/Bun
    write idiom -- was entirely unrecognized, letting a secret written this way through
    `block-secrets-write` unexamined."""
    secret = "abcd1234abcd1234"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "js",
            "code": "fs.writeFileSync('src/secret.js', 'API_KEY = " + secret + "')",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/secret.js"
    assert secret in v1["args"]["content"]


def test_to_v1_event_eval_recognizes_backtick_string_without_path_content_swap():
    """Security-review finding: a backtick (JS template literal) second argument to
    `Bun.write(path, `content`)` was not a recognized quote form, so the dedicated pattern
    failed and the path-less bare-write fallback matched `.write(` inside `Bun.write(` instead
    -- capturing the PATH as "content" (a swap, not merely a miss) while the real secret went
    unexamined. Backtick strings must parse like any other literal."""
    secret = "abcd1234abcd1234"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "js",
            "code": "Bun.write('src/secret.js', `API_KEY = " + secret + "`)",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/secret.js"
    assert secret in v1["args"]["content"]
    assert v1["args"]["content"] != "src/secret.js"


def test_eval_bare_write_never_swaps_path_for_content_on_unparseable_bun_write():
    """A `Bun.write(path, content)` call whose SECOND argument still fails to parse for some
    other reason (e.g. a genuinely non-literal expression) must be left entirely unrecognized
    -- never let the path-less fallback capture the FIRST argument (the path) as if it were the
    written content."""
    code = "Bun.write('src/secret.js', someExpression())"

    buckets = dispatch._eval_content_buckets(code)

    assert "src/secret.js" not in buckets
    assert "src/secret.js" not in "".join(buckets.values())


def test_to_v1_event_eval_recognizes_fs_write_file_without_sync_and_no_prefix():
    """Security-review finding: only `fs.writeFileSync` was recognized; `fs.writeFile` (no
    Sync), `fs.appendFileSync`, and a no-`fs.`-prefix `writeFileSync` (a destructured import)
    are all the same single-call, literal-argument write idiom and must be recognized too."""
    secret = "abcd1234abcd1234"
    for code in (
        "fs.writeFile('src/secret.js', 'API_KEY = " + secret + "')",
        "fs.appendFileSync('src/secret.js', 'API_KEY = " + secret + "')",
        "writeFileSync('src/secret.js', 'API_KEY = " + secret + "')",
    ):
        omp_event = {
            "event": "tool_call",
            "toolName": "eval",
            "input": {"language": "js", "code": code},
            "cwd": "/repo",
        }
        v1 = dispatch.to_v1_event(omp_event, point="pre-write")
        assert v1["args"]["file_path"] == "src/secret.js", code
        assert secret in v1["args"]["content"], code


def test_to_v1_event_eval_decodes_hex_escaped_secret():
    """Security-review finding: matching was escape-AWARE (an embedded `\\"` no longer
    truncates the literal early) but the captured body was never DECODED, so an encoded secret
    (`\\x61bcd1234...`) never matched `block-secrets-write`'s plain-token value class even
    though the ACTUAL bytes written to disk are the decoded, matchable secret."""
    secret = "abcd1234abcd1234"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "Path('src/secret.py').write_text('API_KEY = \\x61bcd1234abcd1234')",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert secret in v1["args"]["content"]


def test_eval_open_write_concatenated_path_falls_through_to_pathless_bucket():
    """Security-review finding: `open(<literal> + <expr>, "w").write(<literal>)` let the
    dedicated open() pattern's permissive `[^)]*` tail capture just the FIRST literal as "path"
    even though Python actually opens a DIFFERENT (concatenated) path -- mislabeling the secret
    under a possibly-`ALLOW_PATH`-allowlisted decoy and, because the match still "succeeds",
    blocking the path-less fallback from independently seeing the same content. A path built
    from concatenation (or `os.path.join(...)`) must fail this pattern entirely and fall
    through to the path-less bucket instead."""
    secret = "abcd1234abcd1234"
    for code in (
        "open('foo.example' + '/../src/secret.py', 'w').write('API_KEY = " + secret + "')",
        "open(os.path.join('foo.example', '..', 'src/secret.py'), 'w').write('API_KEY = " + secret + "')",
    ):
        buckets = dispatch._eval_content_buckets(code)
        assert "foo.example" not in buckets, code
        assert secret in "".join(buckets.values()), code


def test_to_v1_event_eval_code_with_no_recognized_write_is_not_normalized():
    """Honest limitation: a write built from a variable/expression (not a literal argument to
    `Path(...).write_text(...)`/`Bun.write(...)`) is invisible to the heuristic scrape — no
    `file_path`/`content` is fabricated."""
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": "p = get_path()\np.write_text(get_content())\n"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert "file_path" not in v1["args"]
    assert v1["args"]["content"] == ""


# --- to_v1_event: forged agent identity ------------------------------------------------------


def test_to_v1_event_drops_forged_agent_identity_from_tool_args():
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "agent": "general",
            "task": "inspect the entire repository and fix the issue",
            "name": "worker-1",
            "agent_id": "forged",
            "agentId": "forged-camel",
            "agentType": "worker",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-agent")

    assert v1["point"] == "pre-agent"
    assert v1["args"]["task"] == "inspect the entire repository and fix the issue"
    assert v1["args"]["name"] == "worker-1"
    assert v1["args"]["subagent_type"] == "general"
    assert "agent_id" not in v1["args"]
    assert "agentId" not in v1["args"]
    assert "agentType" not in v1["args"]
    # `agent` itself is a forged-identity key and is stripped too — the normalized
    # `subagent_type` copy above is what a subagent-aware hook should read instead.
    assert "agent" not in v1["args"]


def test_to_v1_event_nested_payload_session_file_beats_forged_args_agent_id(monkeypatch):
    """agent-tools#743 (RC-1) trust boundary: with the sessionManager-sourced nested
    sessionFile on the payload, the injected identity is the transcript's child stem — a
    model/tool-supplied `agent_id` in the SAME call's raw args is still stripped first
    (agent-tools#573) and can neither override nor shadow it."""
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m x", "agent_id": "forged", "agentId": "forged-camel"},
        "cwd": "/repo",
        "sessionFile": _OMP_NESTED_SESSION,
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert v1["args"]["agent_id"] == "ThinNotSub"
    assert "agentId" not in v1["args"]


@pytest.fixture(autouse=True)
def _no_host_agent_markers(monkeypatch):
    """Module-wide: a rig-dispatched test run (this process) may carry RIG_AGENT_ID /
    RIG_DETACHED_AGENT; strip them for every test so in-process to_v1_event asserts never
    see an injected agent_id. Subprocess tests pass markers explicitly via `env=`."""
    _clear_agent_env_markers(monkeypatch)


def _clear_agent_env_markers(monkeypatch) -> None:
    """Drop the detached-agent env markers — mandatory before any to_v1_event test,
    because a rig-dispatched omp test run (the very process pytest lives in) carries
    them and the dispatcher injects args.agent_id from process env."""
    monkeypatch.delenv("RIG_AGENT_ID", raising=False)
    monkeypatch.delenv("RIG_DETACHED_AGENT", raising=False)
    monkeypatch.delenv("PI_SESSION_FILE", raising=False)


def test_to_v1_event_injects_agent_id_from_env_marker(monkeypatch):
    """agent-tools#573: the ONE trusted subagent identity source for omp — ported unchanged
    from `opencode_hook_bridge._detached_agent_id` (agent-tools#476) — is the launcher
    process environment, since omp's own tool_call/tool_result events expose none."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "probe")
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["args"]["agent_id"] == "probe"


def test_to_v1_event_without_env_marker_has_no_agent_id(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert "agent_id" not in v1["args"]


_OMP_NESTED_SESSION = (
    "/tmp/omp-sessions/-xp-rig-cli/"
    "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e/ThinNotSub.jsonl"
)
_OMP_MAIN_SESSION = (
    "/tmp/omp-sessions/-xp-rig-cli/"
    "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e.jsonl"
)


def test_to_v1_event_injects_agent_id_from_nested_pi_session_file(monkeypatch):
    """agent-tools#634: a native omp Task/eval child has nested PI_SESSION_FILE and no
    RIG_AGENT_ID. The bridge must inject args.agent_id so EVERY subagent-aware hook
    (not only orchestrator-stays-thin) classifies the worker."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("PI_SESSION_FILE", _OMP_NESTED_SESSION)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert v1["args"]["agent_id"] == "ThinNotSub"


def test_to_v1_event_main_pi_session_file_has_no_agent_id(monkeypatch):
    """The MAIN omp session file is not a child — do not inject agent_id."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("PI_SESSION_FILE", _OMP_MAIN_SESSION)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_injects_agent_id_from_payload_session_file(monkeypatch):
    """agent-tools#647: Node has no nested PI_SESSION_FILE. extension.ts must put the
    child's getSessionFile() path on the payload; to_v1_event injects args.agent_id
    from that nested path even when the process env is the MAIN session (or empty)."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("PI_SESSION_FILE", _OMP_MAIN_SESSION)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
        "sessionFile": _OMP_NESTED_SESSION,
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert v1["args"]["agent_id"] == "ThinNotSub"


def test_to_v1_event_injects_agent_id_from_is_main_session_false_with_no_session_file(monkeypatch):
    """Live finding (2026-09-18): a genuinely nested Task/eval child's OWN session can still
    be in-memory — not yet persisted to disk — for its first few tool calls (SessionManager
    mints getSessionId() synchronously at creation, but getSessionFile() stays undefined
    until the session is actually written to disk). With no session file at all, the
    path-shape check has nothing to walk, and the child was silently classified as the
    orchestrator. extension.ts's `isMainSession` (a per-process "first session seen is root"
    latch on the always-available getSessionId()) is a second, file-independent signal for
    exactly this gap: with isMainSession=False and no sessionFile, args.agent_id must still
    be injected."""
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
        "sessionId": "child-session-id-2",
        "isMainSession": False,
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert v1["args"]["agent_id"] == "omp-nested:child-session-id-2"


def test_to_v1_event_is_main_session_true_has_no_agent_id(monkeypatch):
    """The genuine root/orchestrator session — even one still in-memory with no session file
    yet — must NOT be exempted: isMainSession=True must not inject an agent_id."""
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
        "sessionId": "root-session-id",
        "isMainSession": True,
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_without_is_main_session_key_has_no_agent_id_when_no_session_file(monkeypatch):
    """Backward compatibility: an event with no `isMainSession` key at all (an older bridge
    build, or an existing test fixture) falls through to the pre-fix behavior exactly — no
    agent_id when there is no session file to walk, even with a sessionId present."""
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
        "sessionId": "some-session-id",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_is_main_session_never_overrides_positive_nested_file_check(monkeypatch):
    """A positive file-shape nested result always wins — isMainSession is consulted ONLY
    when the file-based check found nothing, never used to second-guess a real hit."""
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
        "sessionFile": _OMP_NESTED_SESSION,
        "sessionId": "whatever",
        "isMainSession": True,  # would (wrongly) say "root" if it were consulted first
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert v1["args"]["agent_id"] == "ThinNotSub"


def test_to_v1_event_is_main_session_false_never_overrides_present_main_shaped_file(monkeypatch):
    """Review finding (2026-09-18): a PRESENT-but-flat (main-shaped) `sessionFile` is real,
    positive file evidence that this is NOT a child — just as authoritative as a present
    NESTED file, and must not be second-guessed by a contradictory `isMainSession: false`
    (e.g. a stale root latch in a reused process). Only the complete ABSENCE of file evidence
    (no `sessionFile`, no env `PI_SESSION_FILE`) reaches the `isMainSession` fallback."""
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
        "sessionFile": _OMP_MAIN_SESSION,
        "sessionId": "sess-main",
        "isMainSession": False,  # contradicts the file evidence — must be ignored
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_env_marker_wins_over_forged_agent_id(monkeypatch):
    """Strip-then-inject order: a forged args.agent_id is dropped FIRST, then the env
    marker (the only authority) supplies the identity — never the payload."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "probe")
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "agent": "general",
            "task": "do work",
            "agent_id": "forged",
            "agentId": "forged-camel",
            "agentType": "worker",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-agent")

    assert v1["args"]["agent_id"] == "probe"
    assert "agentId" not in v1["args"]
    assert "agentType" not in v1["args"]
    assert "agent" not in v1["args"]


def test_to_v1_event_detached_marker_without_id_yields_anonymous_identity(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_DETACHED_AGENT", "1")
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["args"]["agent_id"] == "detached"


def test_to_v1_event_blank_env_marker_is_not_an_identity(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "   ")
    monkeypatch.setenv("RIG_DETACHED_AGENT", "")
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert "agent_id" not in v1["args"]


def test_omp_bridge_env_marker_makes_nontrivial_task_subagent_exempt(tmp_path, monkeypatch):
    """End-to-end pre-agent pipeline: with the launcher-set env marker present, the real
    background-subagent-gate descriptor sees a trusted agent_id and allows a dispatch that
    would otherwise need to prove triviality or backgrounding."""
    _clear_agent_env_markers(monkeypatch)
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir, hook_id="background-subagent-gate", point="pre-agent",
        cmd=BACKGROUND_SUBAGENT_GATE, on_error="closed",
    )
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "agent": "general-purpose",
            "task": "x" * 400,  # clearly non-trivial, not inherently background
        },
        "cwd": "/repo",
    }
    env = dict(os.environ)
    env["OMP_HOOKS_DIR"] = str(hooks_dir)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["RIG_AGENT_ID"] = "LauncherChild"
    proc = subprocess.run(
        [sys.executable, "-m", "omp_hook_bridge", "tool_call"],
        input=json.dumps(omp_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""  # empty stdout == allow


def test_to_v1_event_task_normalization_does_not_clobber_existing_subagent_type():
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {"agent": "general", "subagent_type": "fork", "task": "do work"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-agent")
    assert v1["args"]["subagent_type"] == "fork"


def test_task_normalization_maps_task_to_prompt_and_marks_background():
    """omp's item shape has `task` (not `prompt`) and NO per-call background lever — the
    pre-agent gate reads `prompt`/`description` for triviality and `run_in_background` for
    shape, so without this mapping every omp dispatch is judged non-trivial + foreground."""
    args = {"agent": "scout", "task": "inspect the repo\nthen fix the bug"}
    dispatch._normalize_task_args(args)
    assert args["prompt"] == "inspect the repo\nthen fix the bug"
    assert args["task"] == "inspect the repo\nthen fix the bug"  # raw field kept
    assert args["subagent_type"] == "scout"
    assert args["run_in_background"] is True


def test_task_normalization_batch_shape_joins_items_and_takes_first_agent():
    args = {
        "context": "shared background",
        "tasks": [
            {"name": "a", "agent": "scout", "task": "look at src/"},
            {"name": "b", "task": "look at tests/"},
        ],
    }
    dispatch._normalize_task_args(args)
    assert args["prompt"] == "look at src/\nlook at tests/"
    assert args["subagent_type"] == "scout"
    assert args["run_in_background"] is True
    assert args["context"] == "shared background"  # raw batch fields kept
    assert len(args["tasks"]) == 2


def test_task_normalization_never_clobbers_explicit_prompt_or_background():
    args = {"task": "x", "prompt": "explicit", "run_in_background": False}
    dispatch._normalize_task_args(args)
    assert args["prompt"] == "explicit"
    assert args["run_in_background"] is False


def test_task_normalization_is_inert_for_non_task_args():
    args = {"command": "ls"}
    dispatch._normalize_task_args(args)
    assert args == {"command": "ls"}


# --- to_v1_event: write -----------------------------------------------------------------------


def test_to_v1_event_normalizes_simple_write_path_and_content():
    omp_event = {
        "event": "tool_call",
        "toolName": "write",
        "input": {"path": "src/new.py", "content": "print('hi')\n"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/new.py"
    assert v1["args"]["path"] == "src/new.py"
    assert v1["args"]["content"] == "print('hi')\n"


def test_to_v1_event_write_missing_content_becomes_empty_string():
    omp_event = {
        "event": "tool_call",
        "toolName": "write",
        "input": {"path": "src/new.py"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["content"] == ""


def test_to_v1_event_write_post_execution_does_not_require_content():
    omp_event = {
        "event": "tool_result",
        "toolName": "write",
        "input": {"path": "src/new.py", "content": "print('hi')\n"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="post-write")
    assert v1["args"]["file_path"] == "src/new.py"
    # post-write never runs the pre-write-only empty-string coercion — whatever `content` was
    # already present in the raw tool args (copied verbatim into `args`) passes through as-is.
    assert v1["args"]["content"] == "print('hi')\n"


# --- to_v1_event: edit (hashline) --------------------------------------------------------------


_SINGLE_SECTION_HASHLINE = (
    "*** Begin Patch\n"
    "[greet.py#A1B2]\n"
    "PUT 1*:\n"
    "+@cache\n"
    "+def greet(name):\n"
    "+    print(f\"Hi, {name}\")\n"
    "*** End Patch\n"
)


def test_to_v1_event_hashline_single_section_extracts_path_and_content():
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _SINGLE_SECTION_HASHLINE},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "greet.py"
    assert v1["args"]["path"] == "greet.py"
    assert v1["args"]["file_paths"] == ["greet.py"]
    assert "@cache" in v1["args"]["content"]
    assert "def greet(name):" in v1["args"]["content"]
    assert v1["command"] == _SINGLE_SECTION_HASHLINE


_MULTI_SECTION_HASHLINE = (
    "*** Begin Patch\n"
    "[greet.py#A1B2]\n"
    "CUT 1* @fn\n"
    "[lib/greet.py#3C4D]\n"
    "PUT <1 @fn\n"
    "*** End Patch\n"
)


def test_to_v1_event_hashline_multi_section_extracts_every_path():
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _MULTI_SECTION_HASHLINE},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_paths"] == ["greet.py", "lib/greet.py"]
    # Two paths and no move target: `_apply_paths` deliberately sets no single `file_path`/
    # `path` in that case (only `file_paths`) — a path-based hook must consult `file_paths`
    # for a multi-path event, not assume a single-file shape.
    assert "file_path" not in v1["args"]
    assert "path" not in v1["args"]


_MULTI_FILE_HASHLINE = (
    "*** Begin Patch\n"
    "[a.py#AAAA]\n"
    "PUT >$:\n"
    "+print('a')\n"
    "[b.py#BBBB]\n"
    "PUT >$:\n"
    "+print('b')\n"
    "*** End Patch\n"
)


def test_hashline_fan_out_splits_multi_file_content_per_path():
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _MULTI_FILE_HASHLINE},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert [e["args"]["file_path"] for e in events] == ["a.py", "b.py"]
    assert [e["args"]["content"] for e in events] == ["print('a')", "print('b')"]
    assert all("file_paths" not in e["args"] for e in events)
    # agent-tools#533: the harness tag must survive the per-file clone.
    assert all(e["harness"] == "omp" for e in events)


def test_hashline_fan_out_ignores_a_forged_patch_key(monkeypatch):
    """#556 review finding: on an `edit` call a stray/forged `patch` key in the raw args
    must NOT shadow the real hashline `input` when fanning out per-path content — otherwise
    every per-path `content` comes back "" and a secret-scanning pre-write hook sees nothing."""
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _MULTI_FILE_HASHLINE, "patch": "x"},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert [e["args"]["file_path"] for e in events] == ["a.py", "b.py"]
    assert [e["args"]["content"] for e in events] == ["print('a')", "print('b')"]


def test_hashline_rem_and_mv_sections_still_surface_their_paths():
    patch = "*** Begin Patch\n[old.py#DEAD]\nMV new.py\n*** End Patch\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_paths"] == ["old.py"]


def test_hashline_repeated_same_path_sections_accumulate_content():
    """Codex PR-thread P1: a later benign section for `a.py` must not ERASE an earlier
    section's rows — otherwise a secret in section 1 hides behind section 3 and a content
    scanner never sees it."""
    patch = (
        "[a.py#AAAA]\nPUT >$:\n+API_KEY = 'abcd1234abcd1234'\n"
        "[b.py#BBBB]\nPUT >$:\n+print('b')\n"
        "[a.py#AAAA]\nPUT <1:\n+import os\n"
    )
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": "/repo"}
    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")
    by_path = {e["args"]["file_path"]: e["args"]["content"] for e in events}
    assert by_path["a.py"] == "API_KEY = 'abcd1234abcd1234'\nimport os"
    assert by_path["b.py"] == "print('b')"
    assert dispatch._hashline_file_paths(patch) == ["a.py", "b.py"]


def test_edit_parsed_truth_overwrites_stray_content_and_forged_file_path():
    """Sonnet review: the parsed patch is the truth about what gets written. A stray
    `content: ""` or a forged `file_path` riding along in the raw args must not shadow it
    (`block-secrets-write` reads `content`, `worktree-only-writes` reads `file_path`)."""
    patch = "[src/x.py#A1B2]\nPUT >$:\n+API_KEY = 'abcd1234abcd1234'\n"
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": patch, "content": "", "file_path": "/tmp/elsewhere.py", "path": "/tmp/elsewhere.py"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["content"] == "API_KEY = 'abcd1234abcd1234'"
    assert v1["args"]["file_path"] == "src/x.py"
    assert v1["args"]["path"] == "src/x.py"


def test_edit_multi_path_drops_forged_single_file_path():
    patch = "[a.py#AAAA]\nPUT >$:\n+a\n[b.py#BBBB]\nPUT >$:\n+b\n"
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": patch, "file_path": "decoy.py", "file_paths": ["decoy.py"]},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_paths"] == ["a.py", "b.py"]
    assert "file_path" not in v1["args"]
    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")
    assert [e["args"]["file_path"] for e in events] == ["a.py", "b.py"]


# --- to_v1_event: edit (non-hashline `replace` / `patch` modes) ------------------------------


def test_edit_replace_mode_maps_path_and_replacement_text():
    """Codex review P1: omp can run `edit` in `replace` mode (no `[PATH#TAG]` section); such
    a call must still surface its path and its replacement text as `content`, or a secret
    written through it reaches `block-secrets-write` as `content == ""`."""
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"path": "src/config.py", "oldText": "x = 1", "newText": "API_KEY = 'abcd1234abcd1234'"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "src/config.py"
    assert v1["args"]["path"] == "src/config.py"
    assert v1["args"]["content"] == "API_KEY = 'abcd1234abcd1234'"


def test_edit_patch_mode_maps_unified_diff_path_and_added_rows():
    diff = "--- a/src/config.py\n+++ b/src/config.py\n@@ -1 +1 @@\n-x = 1\n+API_KEY = 'abcd1234abcd1234'\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"patch": diff}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "src/config.py"
    assert v1["args"]["content"] == "API_KEY = 'abcd1234abcd1234'"


def test_omp_bridge_blocks_secret_write_via_edit_replace_mode(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"path": "src/secret.py", "oldText": "x", "newText": "API_KEY = 'abcd1234abcd1234'\n"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "block"


def test_task_normalization_not_applied_to_non_task_tools_with_task_key():
    """Sonnet review: a bash call whose args happen to carry a `task` key must not get the
    pre-agent `run_in_background` field injected into its pre-bash event."""
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "sleep 300 &", "task": "background this", "agent": "x"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert "run_in_background" not in v1["args"]
    assert "prompt" not in v1["args"]
    assert "subagent_type" not in v1["args"]


# --- to_v1_event: apply_patch (classic envelope) ------------------------------------------------


_APPLY_PATCH_ENVELOPE = (
    "*** Begin Patch\n"
    "*** Add File: src/new.py\n"
    "+API_KEY = 'abcd1234abcd1234'\n"
    "*** Update File: src/existing.py\n"
    "+print('changed')\n"
    "*** End Patch\n"
)


@pytest.mark.parametrize("field", ["patch", "patchText", "input"])
def test_to_v1_event_apply_patch_tries_all_three_field_names(field):
    omp_event = {
        "event": "tool_call",
        "toolName": "apply_patch",
        "input": {field: _APPLY_PATCH_ENVELOPE},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["command"] == _APPLY_PATCH_ENVELOPE
    assert v1["args"]["patch"] == _APPLY_PATCH_ENVELOPE
    assert v1["args"]["patchText"] == _APPLY_PATCH_ENVELOPE
    assert v1["args"]["file_paths"] == ["src/new.py", "src/existing.py"]
    assert "API_KEY = 'abcd1234abcd1234'" in v1["args"]["content"]


def test_to_v1_event_apply_patch_move_includes_source_and_target():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "-old()\n"
        "+new()\n"
        "*** End Patch\n"
    )
    omp_event = {"event": "tool_call", "toolName": "apply_patch", "input": {"patch": patch}, "cwd": "/repo"}

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_paths"] == ["src/old.py", "src/new.py"]
    assert v1["args"]["file_path"] == "src/new.py"
    assert v1["args"]["path"] == "src/new.py"


def test_apply_patch_fan_out_splits_multi_file_content():
    omp_event = {
        "event": "tool_call",
        "toolName": "apply_patch",
        "input": {"patch": _APPLY_PATCH_ENVELOPE},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert [e["args"]["file_path"] for e in events] == ["src/new.py", "src/existing.py"]
    assert "API_KEY = 'abcd1234abcd1234'" in events[0]["args"]["content"]
    assert events[1]["args"]["content"] == "print('changed')"


# --- hooks_dir() env precedence --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_omp_dir_overrides(monkeypatch):
    """Every hooks_dir() precedence test below starts from a clean env: a developer/CI shell
    exporting any of these (OMP_CODING_AGENT_DIR is checked FIRST) would otherwise fail them."""
    for name in ("OMP_HOOKS_DIR", "OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_hooks_dir_default(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path(os.path.expanduser("~/.omp/agent/hooks"))


def test_hooks_dir_pi_config_dir_renames_dotomp(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-work")
    assert dispatch.hooks_dir() == Path(os.path.expanduser("~/.omp-work/agent/hooks"))


def test_hooks_dir_pi_coding_agent_dir_is_full_override(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/agent/root")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path("/custom/agent/root/hooks")


def test_hooks_dir_pi_coding_agent_dir_wins_over_pi_config_dir(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/agent/root")
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-work")
    assert dispatch.hooks_dir() == Path("/custom/agent/root/hooks")


def test_hooks_dir_omp_hooks_dir_overrides_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("OMP_HOOKS_DIR", str(tmp_path))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/agent/root")
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-work")
    assert dispatch.hooks_dir() == tmp_path


def test_hooks_dir_pi_coding_agent_dir_relative_is_home_anchored(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "relative/agent")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path(os.path.expanduser("~/relative/agent/hooks"))


def test_hooks_dir_pi_coding_agent_dir_expands_dollar_var(monkeypatch):
    """rig-cli's own `riglib.harness_skills.omp_agent_root` (which this is ported from)
    resolves both env vars via `expand_user_path`, i.e. `expanduser(expandvars(path))` — a
    `$VAR` reference, not just `~`, must resolve to the SAME real path rig installs the
    descriptor dir and extension symlink into, or this dispatcher silently loads zero
    descriptors from an unexpanded literal path segment and allows everything."""
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_HOME_FOR_TEST", "/custom/home")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "$PI_HOME_FOR_TEST/omp-agent")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path("/custom/home/omp-agent/hooks")


def test_hooks_dir_pi_config_dir_expands_dollar_var(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.delenv("OMP_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_HOME_FOR_TEST", "/custom/home")
    monkeypatch.setenv("PI_CONFIG_DIR", "$PI_HOME_FOR_TEST/.omp-work")
    assert dispatch.hooks_dir() == Path("/custom/home/.omp-work/agent/hooks")


def test_hooks_dir_omp_coding_agent_dir_wins_over_pi_coding_agent_dir(monkeypatch):
    """omp's primary override is the OMP-prefixed var; `PI_CODING_AGENT_DIR` is the legacy
    name. This repo's own omp resolver (`lib/checker/model_freshness.py::_omp_agent_db`)
    checks OMP first — the bridge must agree or a relocated omp loads zero descriptors."""
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("OMP_CODING_AGENT_DIR", "/omp/primary")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/pi/legacy")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path("/omp/primary/hooks")


@pytest.mark.parametrize(
    ("var", "expected"),
    [
        ("OMP_CODING_AGENT_DIR", "~/custom-agent/hooks"),
        ("PI_CODING_AGENT_DIR", "~/custom-agent/hooks"),
        ("PI_CONFIG_DIR", "~/custom-agent/agent/hooks"),
    ],
)
def test_hooks_dir_relative_dollar_var_expansion_is_anchored_expanded(monkeypatch, var, expected):
    """A `$VAR` that expands to a RELATIVE path must be home-anchored AFTER expansion — the
    raw override string would leave a literal `$PROFILE_ROOT` path segment behind and the
    bridge would read a directory that does not exist (Codex/Sonnet review finding)."""
    for name in ("OMP_HOOKS_DIR", "OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROFILE_ROOT", "custom-agent")
    monkeypatch.setenv(var, "$PROFILE_ROOT")
    assert dispatch.hooks_dir() == Path(os.path.expanduser(expected))
    assert "$" not in str(dispatch.hooks_dir())


# --- main() fail-open behavior -----------------------------------------------------------------


def test_main_fails_open_on_malformed_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _FakeStdin("{not json"))
    rc = dispatch.main(["tool_call"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_fails_open_when_dispatch_raises(monkeypatch):
    def _boom(_event_name, _event):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch, "dispatch", _boom)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({"event": "tool_call", "toolName": "bash", "input": {}})))
    rc = dispatch.main(["tool_call"])
    assert rc == 0


def test_main_fails_open_when_agent_hooks_v1_import_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OMP_HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.syspath_prepend(str(tmp_path))  # no real agent_hooks_v1 reachable here
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({
        "event": "tool_call", "toolName": "bash", "input": {"command": "ls"},
    })))
    real_import = __import__

    def _blocked_import(name, *args, **kwargs):
        if name == "agent_hooks_v1":
            raise ImportError("simulated missing agent_hooks_v1")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocked_import)
    rc = dispatch.main(["tool_call"])
    assert rc == 0


def test_main_allows_when_no_result(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OMP_HOOKS_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({
        "event": "tool_call", "toolName": "read", "input": {"path": "x"},
    })))
    rc = dispatch.main([])
    assert rc == 0
    assert capsys.readouterr().out == ""


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# --- end-to-end: real descriptors ---------------------------------------------------------------


def test_omp_bridge_blocks_raw_pr_merge_with_real_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-raw-pr-merge", point="pre-bash", cmd=BLOCK_RAW_PR_MERGE)
    event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "gh pr merge 42 --admin"},
        "cwd": str(tmp_path),
        "toolCallId": "call_1",
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "gh ship" in out["reason"]


def test_omp_bridge_nontrivial_task_dispatch_passes_real_background_subagent_gate(tmp_path):
    """End-to-end bridge -> real `background-subagent-gate` (the Codex review's P1): an omp
    `task` call has no `prompt` and no per-call background flag, so an unnormalized event was
    judged non-trivial + foreground and BLOCKED every omp dispatch. The normalized event must
    pass on omp's own background-job contract."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="background-subagent-gate",
        point="pre-agent",
        cmd=BACKGROUND_SUBAGENT_GATE,
        on_error="open",
    )
    event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "context": "shared background for both workers",
            "tasks": [
                {"agent": "general", "task": "Inspect the provisioning code, implement the bridge,\nrun tests, and report."},
                {"agent": "scout", "task": "Audit every consumer hook for the fields it reads."},
            ],
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_omp_bridge_blocks_secret_write_via_write_tool(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    event = {
        "event": "tool_call",
        "toolName": "write",
        "input": {"path": "src/secret.py", "content": "API_KEY = 'abcd1234abcd1234'\n"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_blocks_secret_write_via_edit_hashline(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    patch = "*** Begin Patch\n[src/secret.py#A1B2]\nPUT >$:\n+API_KEY = 'abcd1234abcd1234'\n*** End Patch\n"
    event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": patch},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_blocks_secret_write_via_eval_tool(tmp_path):
    """agent-tools#614: the reported live bypass -- an agent blocked from writing a secret via
    `edit`/`write` routes the same write through an `eval` cell instead. Real
    `block-secrets-write` descriptor must fire against it, not silently pass through unexamined."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    code = (
        "from pathlib import Path\n"
        "Path('src/secret.py').write_text(\"API_KEY = '" + secret + "'\\n\")\n"
    )
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_blocks_secret_write_via_eval_tool_after_decoy_write(tmp_path):
    """Security-review regression on agent-tools#614's first version: a harmless decoy
    `Path(...).write_text(...)` call before the real secret-writing call must not hide the
    secret from the real `block-secrets-write` descriptor."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    code = (
        "from pathlib import Path\n"
        "Path('README.md').write_text('ok')\n"
        "Path('src/secret.py').write_text(\"API_KEY = '" + secret + "'\\n\")\n"
    )
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_blocks_secret_write_via_eval_tool_after_allowlisted_decoy_path(tmp_path):
    """Security-review regression: an ALLOW_PATH-matching decoy write (e.g. a `.example` file)
    scraped as the FIRST path must not exempt a later, different, real secret write from
    `block-secrets-write` -- the multi-target fan-out must give each path its own verdict."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    code = (
        "from pathlib import Path\n"
        "Path('fixtures/ok.example').write_text('ok')\n"
        "Path('src/secret.py').write_text(\"API_KEY = '" + secret + "'\")\n"
    )
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_pathless_write_gets_own_event_when_combined_with_real_paths():
    """Security-review finding: fanning out over REAL scraped paths only (ignoring a path-less
    bare `.write(...)` match) silently DROPPED that match's content entirely whenever the same
    eval cell also had 2+ named `Path(...).write_text(...)` calls. The path-less bucket must
    become its OWN event (with no `file_path`/`path` set) so its content is never lost."""
    secret = "abcd1234abcd1234"
    code = (
        "Path('README.md').write_text('ok')\n"
        "Path('src/foo.py').write_text('print(1)')\n"
        "with open('src/secret.py', 'w') as f:\n"
        "    f.write('API_KEY = " + secret + "')\n"
    )
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert len(events) == 3
    pathless = [e for e in events if not e["args"].get("file_path")]
    assert len(pathless) == 1
    assert secret in pathless[0]["args"]["content"]
    assert "file_path" not in pathless[0]["args"]
    assert "path" not in pathless[0]["args"]


def test_eval_pathless_secret_not_hidden_by_single_allowlisted_decoy_path():
    """Security-review finding: with exactly ONE scraped real path (no multi-path fan-out
    trigger under the old real-paths-only logic) plus a path-less secret, the real path's
    content ('ok') and the secret both used to collapse into ONE event under that single path
    -- if that path happened to match `block-secrets-write`'s `ALLOW_PATH` allowlist, the
    secret was never scanned at all. Two distinct buckets (one real path + one path-less) must
    still fan out into two separate events."""
    secret = "abcd1234abcd1234"
    code = (
        "Path('fixtures/ok.example').write_text('ok')\n"
        "with open('src/secret.py', 'w') as f:\n"
        "    f.write('API_KEY = " + secret + "')\n"
    )
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert len(events) == 2
    by_path = {e["args"].get("file_path"): e["args"]["content"] for e in events}
    assert by_path["fixtures/ok.example"] == "ok"
    assert secret in by_path[None]


def test_omp_bridge_blocks_secret_write_via_eval_tool_bare_write_after_path_decoys(tmp_path):
    """Security-review regression: a bare `.write(...)` secret combined with two harmless
    `Path(...).write_text(...)` decoys must still reach `block-secrets-write` as its own event,
    not be dropped by a real-paths-only fan-out."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    code = (
        "Path('README.md').write_text('ok')\n"
        "with open('src/secret.py', 'w') as f:\n"
        "    f.write(\"API_KEY = '" + secret + "'\")\n"
    )
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_blocks_secret_write_via_eval_tool_backtick_bun_write(tmp_path):
    """Security-review regression: a JS backtick-quoted `Bun.write` secret must reach the real
    `block-secrets-write` descriptor, not be swapped for the harmless path string."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "js",
            "code": "Bun.write('src/secret.js', `API_KEY = " + secret + "`)",
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_blocks_long_process_via_hub_start(tmp_path):
    """agent-tools#614: the other reported live bypass -- a long review/test run dispatched via
    `hub start` instead of `bash` never reached `no-long-inline-process`. Real descriptor must
    fire against a hub-start-shaped event launching a long-running command."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir, hook_id="no-long-inline-process", point="pre-bash", cmd=NO_LONG_INLINE_PROCESS
    )
    event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "start", "application": "npm", "args": ["run", "test"], "name": "test-runner"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_shell_commands_scrapes_python_subprocess_git_argv():
    """agent-tools#692: literal subprocess argv lists become shell-style commands."""
    code = (
        "import subprocess\n"
        "subprocess.run(['git', 'add', '-A'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'fix merge'], check=True)\n"
    )
    assert dispatch._eval_shell_commands({"language": "py", "code": code}) == [
        shlex.join(["git", "add", "-A"]),
        shlex.join(["git", "commit", "-m", "fix merge"]),
    ]


def test_eval_shell_commands_scrapes_python_os_system_and_js_bun_shell():
    code_py = "import os\nos.system('git commit -m \"ship\"')\n"
    assert dispatch._eval_shell_commands({"language": "py", "code": code_py}) == [
        'git commit -m "ship"',
    ]
    code_js = "await Bun.$`git push origin HEAD`.quiet()\n"
    assert dispatch._eval_shell_commands({"language": "js", "code": code_js}) == [
        "git push origin HEAD",
    ]


def test_v1_events_for_dispatch_eval_pre_bash_fans_out_commands():
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "import subprocess\nsubprocess.run(['git', 'add', '.'])\nsubprocess.run(['git', 'commit', '-m', 'x'])",
        },
        "cwd": "/repo",
        "toolCallId": "c_eval_git",
    }
    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-bash")
    assert [e["command"] for e in events] == [shlex.join(["git", "add", "."]), shlex.join(["git", "commit", "-m", "x"])]
    assert all(e["point"] == "pre-bash" for e in events)


def _init_git_repo(repo: Path, rel: str = "src/change.py") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"], check=True, capture_output=True)
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('change')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True, capture_output=True)


def test_omp_bridge_blocks_eval_git_commit_via_require_review(tmp_path, monkeypatch):
    """agent-tools#692: eval subprocess git commit must hit require-review-before-commit."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    marker = tmp_path / "review-marker"
    monkeypatch.setenv("REVIEW_MARKER", str(marker))
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="require-review-before-commit",
        point="pre-bash",
        cmd=REQUIRE_REVIEW_BEFORE_COMMIT,
        on_error="open",
    )
    code = (
        "import subprocess\n"
        "subprocess.run(['git', 'commit', '-m', 'merge resolution'], check=True)\n"
    )
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(repo),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_bash_git_commit_and_eval_subprocess_equally_blocked(tmp_path, monkeypatch):
    """Regression: bash and eval subprocess paths must both reach the same pre-bash gate."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    marker = tmp_path / "review-marker"
    monkeypatch.setenv("REVIEW_MARKER", str(marker))
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="require-review-before-commit",
        point="pre-bash",
        cmd=REQUIRE_REVIEW_BEFORE_COMMIT,
        on_error="open",
    )
    bash_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'via bash'"},
        "cwd": str(repo),
    }
    eval_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "import subprocess\nsubprocess.run(['git', 'commit', '-m', 'via eval'], check=True)",
        },
        "cwd": str(repo),
    }

    bash_proc = _run_dispatch("tool_call", bash_event, hooks_dir=hooks_dir)
    eval_proc = _run_dispatch("tool_call", eval_event, hooks_dir=hooks_dir)

    assert json.loads(bash_proc.stdout)["decision"] == "block"
    assert json.loads(eval_proc.stdout)["decision"] == "block"


def test_omp_bridge_dispatches_post_write_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    log_path = tmp_path / "seen.json"
    script = tmp_path / "record_hook.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(log_path)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    _install_descriptor(hooks_dir, hook_id="recorder", point="post-write", cmd=script, on_error="open")
    event = {
        "event": "tool_result",
        "toolName": "write",
        "input": {"path": "src/new.py", "content": "print(1)\n"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_result", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    seen = json.loads(log_path.read_text(encoding="utf-8"))
    assert seen["point"] == "post-write"
    assert seen["args"]["file_path"] == "src/new.py"


def test_omp_bridge_allows_when_hooks_dir_missing(tmp_path):
    event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "ls"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("tool_call", event, hooks_dir=tmp_path / "nope")
    assert proc.returncode == 0
    assert proc.stdout == ""


# --- worktree-only-writes consuming an omp-produced pre-write event -----------------------------


def test_worktree_only_writes_reads_omp_edit_file_path(tmp_path, monkeypatch):
    """worktree-only-writes only reads args.file_path/path/notebook_path — prove the omp
    bridge's hashline path extraction actually feeds it correctly end to end."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    patch = "*** Begin Patch\n[foo.py#A1B2]\nPUT >$:\n+print(1)\n*** End Patch\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": str(repo)}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "foo.py"

    proc = sp.run(
        [sys.executable, str(WORKTREE_ONLY_WRITES)],
        input=json.dumps(v1),
        capture_output=True,
        text=True,
        env={**{k: v for k, v in os.environ.items() if k != "RIG_WORKTREE_ONLY"}, "HOME": str(tmp_path)},
        timeout=10,
    )
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert proc.returncode == 10


def test_worktree_only_writes_relative_hashline_path_blocks_despite_sibling_worktree_match(tmp_path):
    """agent-tools#615/#613: a relative hashline `[foo.py#TAG]` path is exactly the bare
    string the bridge extracts (`_hashline_file_paths` never resolves it to an absolute path).
    cwd is main; a different worktree happens to track a file at the same relative path.
    Guessing that as the real target was reverted as exploitable (see the sibling
    hook-level test); this proves the safe BLOCK verdict holds through the full bridge chain."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    feat = tmp_path / "feat"
    sp.run(["git", "worktree", "add", "-q", "-b", "feat/x", str(feat)], cwd=repo, check=True)
    (feat / "new_helper.py").write_text("x\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=feat, check=True)
    sp.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "add helper"], cwd=feat, check=True)

    patch = "[new_helper.py#A1B2]\nPUT >$:\n+y\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": str(repo)}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "new_helper.py"  # still bare/relative — the bridge does not resolve it

    proc = sp.run(
        [sys.executable, str(WORKTREE_ONLY_WRITES)],
        input=json.dumps(v1),
        capture_output=True,
        text=True,
        env={**{k: v for k, v in os.environ.items() if k != "RIG_WORKTREE_ONLY"}, "HOME": str(tmp_path)},
        timeout=10,
    )
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert proc.returncode == 10


@pytest.mark.parametrize("omp_input,tool,expected", [
    ({"path": "agent://all", "content": "serialize bun test runs"}, "write", "allow"),
    ({"path": "local://report.md", "content": "# findings"}, "write", "allow"),
    ({"path": "proc://bg_388/kill"}, "write", "allow"),
    ({"language": "py", "code": "import urllib.request\nprint(urllib.request.urlopen(u).read())"}, "eval", "allow"),
    ({"path": "notes.md", "content": "x"}, "write", "block"),
    ({"language": "py", "code": "open('x.txt', 'w').write('a')"}, "eval", "block"),
])
def test_worktree_only_writes_omp_internal_uris_and_eval_end_to_end(tmp_path, omp_input, tool, expected):
    """agent-tools#828: through the real bridge → hook chain from an enrolled main checkout,
    harness URIs and a write-free eval cell are allowed; a real file write is still blocked."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    omp_event = {"event": "tool_call", "toolName": tool, "input": omp_input, "cwd": str(repo)}
    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")
    assert len(events) == 1
    proc = sp.run(
        [sys.executable, str(WORKTREE_ONLY_WRITES)],
        input=json.dumps(events[0]),
        check=False,
        capture_output=True,
        text=True,
        env={**{k: v for k, v in os.environ.items() if k != "RIG_WORKTREE_ONLY"}, "HOME": str(tmp_path)},
        timeout=10,
    )
    assert json.loads(proc.stdout)["decision"] == expected
    assert proc.returncode == (0 if expected == "allow" else 10)


# --- extension.ts loads without a syntax error --------------------------------------------------


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_parses_with_bun():
    """A broken extension.ts would silently no-op every omp session (the extension loader
    just records a load error and continues) — cheap insurance that it at least parses."""
    proc = subprocess.run(
        ["bun", "build", "--no-bundle", str(_EXTENSION_TS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_registers_tool_call_and_blocks_via_stub_dispatcher(tmp_path):
    """Functional smoke test: load the real extension.ts under bun with a stub `pi`/`ctx`,
    point OMP_HOOK_BRIDGE_PYTHON at a canned stub dispatcher, and confirm the exported
    factory registers `tool_call`/`tool_result` and translates an explicit block decision
    into `{block: true, reason}` — proving the extension's wiring, not just its syntax."""
    stub = tmp_path / "stub_dispatcher.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'block', 'reason': 'stub blocked it'}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    driver = tmp_path / "driver.ts"
    driver.write_text(
        "const mod = await import(process.env.EXTENSION_PATH!);\n"
        "const handlers: Record<string, Function> = {};\n"
        "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
        "mod.default(pi);\n"
        "const ctx = { cwd: '/tmp', hasUI: false };\n"
        "const result = await handlers['tool_call']({ toolName: 'bash', input: { command: 'ls' }, toolCallId: 'c1' }, ctx);\n"
        "console.log(JSON.stringify(result ?? null));\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["OMP_HOOK_BRIDGE_PYTHON"] = str(stub)

    proc = subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=30)

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"block": True, "reason": "stub blocked it"}


def _run_identity_driver(
    tmp_path: Path,
    ctx_ts: str,
    dump_keys: str,
    env_extra: dict[str, str] | None = None,
    *,
    dump_full_payload: bool = False,
) -> dict:
    """Common shape of the identity-forwarding tests below: load the real extension.ts
    under bun with a stub `pi`, a caller-supplied ctx literal (`ctx_ts`), and a dump-style
    stub dispatcher that records either the chosen env/payload fields (`dump_keys`, Python
    dict-literal source reading `payload`/`os.environ`) or the whole payload, then allows
    the call. Returns the recorded dict."""
    dump = tmp_path / "dump.json"
    stub = tmp_path / "stub_dispatcher.py"
    recorded = "payload" if dump_full_payload else "{" + dump_keys + "}"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        f"path = {str(dump)!r}\n"
        f"open(path, 'w').write(json.dumps({recorded}))\n"
        "print(json.dumps({'decision': 'allow'}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    driver = tmp_path / "driver.ts"
    driver.write_text(
        "const mod = await import(process.env.EXTENSION_PATH!);\n"
        "const handlers: Record<string, Function> = {};\n"
        "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
        "mod.default(pi);\n"
        + ctx_ts
        + "await handlers['tool_call']("
        "{ toolName: 'bash', input: { command: 'git commit -m x' }, toolCallId: 'c1' }, ctx);\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["OMP_HOOK_BRIDGE_PYTHON"] = str(stub)
    env.update(env_extra or {})
    proc = subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(dump.read_text())


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_forwards_getSessionFile_into_dispatcher_env(tmp_path):
    """agent-tools#647: omp tool ctx exposes getSessionFile()/getAgentId() methods, not
    ctx.agentId. The extension must put the nested transcript on PI_SESSION_FILE for the
    dispatcher AND on payload.sessionFile / payload.agentId."""
    got = _run_identity_driver(
        tmp_path,
        "const nested = process.env.NESTED_SESSION!;\n"
        "const ctx = {\n"
        "  cwd: '/tmp', hasUI: false,\n"
        "  agentId: '', sessionFile: '   ',\n"
        "  getSessionFile: () => nested,\n"
        "  getAgentId: () => 'ThinNotSub',\n"
        "};\n",
        "'env_pi': os.environ.get('PI_SESSION_FILE'), "
        "'sessionFile': payload.get('sessionFile'), "
        "'agentId': payload.get('agentId')",
        {"NESTED_SESSION": _OMP_NESTED_SESSION},
    )
    assert got["env_pi"] == _OMP_NESTED_SESSION
    assert got["sessionFile"] == _OMP_NESTED_SESSION
    assert got["agentId"] == "ThinNotSub"



@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_forwards_rig_agent_id_into_dispatcher_env(tmp_path):
    """agent-tools#681: RIG_AGENT_ID must reach the dispatcher process env so hooks
    that read process env (orchestrator-stays-thin tier 2) classify native subagents."""
    got = _run_identity_driver(
        tmp_path,
        "const nested = process.env.NESTED_SESSION!;\n"
        "const ctx = {\n"
        "  cwd: '/tmp', hasUI: false,\n"
        "  getSessionFile: () => nested,\n"
        "  getAgentId: () => 'ThinNotSub',\n"
        "};\n",
        "'env_rig': os.environ.get('RIG_AGENT_ID'), "
        "'agentId': payload.get('agentId')",
        {"NESTED_SESSION": _OMP_NESTED_SESSION},
    )
    assert got["env_rig"] == "ThinNotSub"
    assert got["agentId"] == "ThinNotSub"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_reads_session_manager_when_toplevel_ctx_identity_is_blank(tmp_path):
    """agent-tools#743 (RC-1): omp's REAL ExtensionContext has NO top-level
    getSessionFile()/getAgentId()/sessionFile/agentId at all (oh-my-pi#11214) — identity lives
    on ctx.sessionManager. For a native Task/eval child that manager is the CHILD's own, so
    getSessionFile() returns the nested transcript; the extension must forward it onto
    payload.sessionFile AND the dispatcher env PI_SESSION_FILE so dispatch.py's
    _OMP_SESSION_DIR classification sees the child. The resolved session id must ALSO reach
    the dispatcher env as RIG_SESSION_ID (RC-2: orchestrator-stays-thin's per-session
    warn/block markers read it from the hook process env chain)."""
    got = _run_identity_driver(
        tmp_path,
        # The oh-my-pi#11214 shape: no top-level identity members AT ALL, only sessionManager.
        "const nested = process.env.NESTED_SESSION!;\n"
        "const ctx = {\n"
        "  cwd: '/tmp', hasUI: false,\n"
        "  sessionManager: {\n"
        "    getSessionFile: () => nested,\n"
        "    getSessionId: () => 'sess-child-1',\n"
        "  },\n"
        "};\n",
        "'env_pi': os.environ.get('PI_SESSION_FILE'), "
        "'env_rig_session': os.environ.get('RIG_SESSION_ID'), "
        "'sessionFile': payload.get('sessionFile'), "
        "'sessionId': payload.get('sessionId'), "
        "'agentId': payload.get('agentId')",
        {"NESTED_SESSION": _OMP_NESTED_SESSION},
    )
    assert got["env_pi"] == _OMP_NESTED_SESSION
    assert got["sessionFile"] == _OMP_NESTED_SESSION
    assert got["sessionId"] == "sess-child-1"
    assert got["env_rig_session"] == "sess-child-1"  # RC-2: session-keyed hook state needs it in env
    assert got["agentId"] == ""  # omp ctx has no agent id; the dispatcher derives it from the path


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_session_manager_main_session_file_stays_orchestrator(tmp_path, monkeypatch):
    """agent-tools#743 (RC-1): the SAME sessionManager source on the MAIN session returns the
    FLAT transcript (.../<session-id>.jsonl — parent is the project slug, not a session dir).
    The extension forwards it, and the dispatcher's path-shape classification must yield NO
    agent_id — the main thread stays governed as the orchestrator."""
    payload = _run_identity_driver(
        tmp_path,
        "const mainFile = process.env.MAIN_SESSION!;\n"
        "const ctx = {\n"
        "  cwd: '/tmp', hasUI: false,\n"
        "  sessionManager: { getSessionFile: () => mainFile, getSessionId: () => 'sess-main' },\n"
        "};\n",
        "",
        {"MAIN_SESSION": _OMP_MAIN_SESSION},
        dump_full_payload=True,
    )
    assert payload["sessionFile"] == _OMP_MAIN_SESSION
    # The dispatcher's own classification of that payload: flat main path → no agent_id.
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("PI_SESSION_FILE", _OMP_MAIN_SESSION)  # mirror the dispatcher's spawned env
    v1 = dispatch.to_v1_event(payload, point="pre-bash")
    assert "agent_id" not in v1["args"]


def _run_sequential_identity_driver(tmp_path: Path, steps: list[str]) -> list[dict]:
    """Drive MULTIPLE sequential `tool_call`/`session_shutdown` handler invocations against
    the SAME extension.ts module instance in one bun process — proving `latchRootSessionId`'s
    cross-call, cross-session statefulness (agent-tools review finding, 2026-09-18): a single
    `_run_identity_driver` call can only ever exercise ONE `tool_call`, which can never
    observe a per-process latch. Each string in `steps` is a JS statement invoking the
    driver-provided `await dumpToolCall(ctxLiteral)` (records that call's payload, in order)
    or `await shutdown(ctxLiteral)` (fires `session_shutdown` for that ctx, no payload).
    Returns the dumped `tool_call` payloads in call order."""
    dump_dir = tmp_path / "dumps"
    dump_dir.mkdir()
    stub = tmp_path / "stub_dispatcher.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        f"d = pathlib.Path({str(dump_dir)!r})\n"
        "(d / f\"{payload.get('toolCallId', 'x')}.json\").write_text(json.dumps(payload))\n"
        "print(json.dumps({'decision': 'allow'}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    driver = tmp_path / "driver.ts"
    driver.write_text(
        "const mod = await import(process.env.EXTENSION_PATH!);\n"
        "const handlers: Record<string, Function> = {};\n"
        "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
        "mod.default(pi);\n"
        "let callSeq = 0;\n"
        "const dumpToolCall = async (ctx: unknown) => {\n"
        "  const n = ++callSeq;\n"
        "  await handlers['tool_call'](\n"
        "    { toolName: 'bash', input: { command: 'git commit -m x' }, toolCallId: `c${n}` }, ctx);\n"
        "};\n"
        "const shutdown = async (ctx: unknown) => { await handlers['session_shutdown']({}, ctx); };\n"
        + "\n".join(steps),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["OMP_HOOK_BRIDGE_PYTHON"] = str(stub)
    proc = subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    calls = sorted(dump_dir.glob("c*.json"), key=lambda p: int(p.stem[1:]))
    return [json.loads(f.read_text()) for f in calls]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_is_main_session_true_across_repeat_calls_from_same_session(tmp_path):
    """The root latch must not just fire once: the SAME session calling a SECOND time must
    still compare equal to the id it latched on its own first call."""
    payloads = _run_sequential_identity_driver(
        tmp_path,
        [
            "const ctx = { cwd: '/tmp', hasUI: false,"
            " sessionManager: { getSessionId: () => 'root-sess' } };\n"
            "await dumpToolCall(ctx);\n"
            "await dumpToolCall(ctx);\n"
            "await dumpToolCall(ctx);\n",
        ],
    )
    assert [p.get("isMainSession") for p in payloads] == [True, True, True]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_is_main_session_false_for_later_different_session_same_process(tmp_path):
    """Live finding (2026-09-18): a Task/eval child dispatched from an already-running
    orchestrator shares the SAME Node process. The orchestrator's OWN first tool_call latches
    the root; the child's differently-id'd session, calling afterward in the SAME process,
    must compare false against it — the exact signal `dispatch.py` uses to exempt an
    in-memory (no session file yet) child. Would FAIL (both `true`, or the child's session
    wrongly becoming root) if the latch didn't persist across calls or compared the wrong way."""
    payloads = _run_sequential_identity_driver(
        tmp_path,
        [
            "const parent = { cwd: '/tmp', hasUI: false,"
            " sessionManager: { getSessionId: () => 'root-sess' } };\n"
            "const child = { cwd: '/tmp', hasUI: false,"
            " sessionManager: { getSessionId: () => 'child-sess' } };\n"
            "await dumpToolCall(parent);\n"
            "await dumpToolCall(child);\n"
            "await dumpToolCall(child);\n",
        ],
    )
    assert [p.get("isMainSession") for p in payloads] == [True, False, False]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_is_main_session_absent_when_session_id_is_blank(tmp_path):
    """No `sessionManager`/blank `getSessionId()` at all — stay silent (no key on the
    payload) rather than guess either way; dispatch.py's file-shape check alone governs."""
    payloads = _run_sequential_identity_driver(
        tmp_path,
        [
            "const ctx = { cwd: '/tmp', hasUI: false };\n"
            "await dumpToolCall(ctx);\n",
        ],
    )
    assert "isMainSession" not in payloads[0]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_session_shutdown_of_root_lets_next_session_relatch(tmp_path):
    """Self-healing (agent-tools review finding, 2026-09-18): a process that goes on to host a
    genuinely SECOND, unrelated top-level session (session reuse, an ACP host, a future
    same-process `/new`-style flow) must not keep comparing that new session against a root
    that has already ended. `session_shutdown` for the CURRENT root clears the latch, so the
    NEXT session's first call correctly becomes the new root instead of scoring `false`
    forever against a session that no longer exists — the exact wrong-direction failure mode
    (exempting the wrong session) an independent review flagged against a latch with no
    clearing path at all."""
    payloads = _run_sequential_identity_driver(
        tmp_path,
        [
            "const first = { cwd: '/tmp', hasUI: false,"
            " sessionManager: { getSessionId: () => 'first-root' } };\n"
            "const second = { cwd: '/tmp', hasUI: false,"
            " sessionManager: { getSessionId: () => 'second-root' } };\n"
            "await dumpToolCall(first);\n"
            "await shutdown(first);\n"
            "await dumpToolCall(second);\n",
        ],
    )
    assert [p.get("isMainSession") for p in payloads] == [True, True]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_session_shutdown_of_non_root_child_does_not_clear_latch(tmp_path):
    """Only the ROOT's own shutdown may clear the latch: a CHILD ending mid-session (its task
    finished, its own `session_shutdown` fires) must not reset root identity out from under
    the still-running orchestrator or its other siblings."""
    payloads = _run_sequential_identity_driver(
        tmp_path,
        [
            "const root = { cwd: '/tmp', hasUI: false,"
            " sessionManager: { getSessionId: () => 'root-sess' } };\n"
            "const child = { cwd: '/tmp', hasUI: false,"
            " sessionManager: { getSessionId: () => 'child-sess' } };\n"
            "await dumpToolCall(root);\n"
            "await dumpToolCall(child);\n"
            "await shutdown(child);\n"
            "await dumpToolCall(root);\n",
        ],
    )
    assert [p.get("isMainSession") for p in payloads] == [True, False, True]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_fails_open_when_dispatcher_python_is_missing(tmp_path):
    """The ONE deliberate divergence from opencode's plugin: a broken/missing dispatcher on
    the `tool_call` (pre-execution) path must resolve to "allow" (undefined), never throw."""
    driver = tmp_path / "driver.ts"
    driver.write_text(
        "const mod = await import(process.env.EXTENSION_PATH!);\n"
        "const handlers: Record<string, Function> = {};\n"
        "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
        "mod.default(pi);\n"
        "const ctx = { cwd: '/tmp', hasUI: false };\n"
        "const result = await handlers['tool_call']({ toolName: 'bash', input: { command: 'ls' }, toolCallId: 'c1' }, ctx);\n"
        "console.log(JSON.stringify(result ?? null));\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["OMP_HOOK_BRIDGE_PYTHON"] = str(tmp_path / "does-not-exist-python3")

    proc = subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=30)

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result is None


# --- the dispatcher runs ASYNCHRONOUSLY (agent-tools#558) ----------------------------------------
#
# omp is single-threaded: a SYNCHRONOUS child-process spawn in a `tool_call` handler freezes the
# whole TUI (no repaints, no keypresses) for as long as the dispatcher runs — up to the full
# Telegram-approval window of a hatch-capable descriptor. The handler must therefore await the
# dispatcher instead, which omp supports: its `emitToolCall` awaits whatever the handler returns.
#
# These drivers stub `pi`/`ctx` and point OMP_HOOK_BRIDGE_PYTHON at a canned dispatcher, so they
# need neither omp nor the real Python dispatcher.

_DRIVER_PROLOGUE = (
    "const mod = await import(process.env.EXTENSION_PATH!);\n"
    "const handlers: Record<string, Function> = {};\n"
    "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
    "mod.default(pi);\n"
    "const ctx = { cwd: '/tmp', hasUI: false };\n"
    "const event = { toolName: 'bash', input: { command: 'ls' }, toolCallId: 'c1' };\n"
)


def _write_stub_dispatcher(tmp_path: Path, body: str) -> Path:
    """A stub that stands in for `python3 -m omp_hook_bridge`: it ignores argv, drains stdin
    (the extension always writes a JSON payload there), then does whatever `body` says. `body`
    may call `ready()` once its setup (a pid file, a signal handler) is in place — the driver
    side waits for that marker (`_WAIT_READY`) before acting, because a fixed delay raced
    python's startup whenever this suite ran alongside other work on the machine."""
    stub = tmp_path / "stub_dispatcher.py"
    ready = tmp_path / "stub.ready"
    stub.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys, time\n"
        f"def ready():\n    pathlib.Path({str(ready)!r}).touch()\n"
        "sys.stdin.read()\n" + body,
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


# Driver-side: poll for the stub's readiness marker (see `_write_stub_dispatcher`).
_WAIT_READY = (
    "const { existsSync } = await import('node:fs');\n"
    "const waitReady = async () => { const p = process.env.STUB_READY!; const t0 = Date.now();\n"
    "  while (!existsSync(p)) { if (Date.now() - t0 > 20000) throw new Error('stub never became ready');\n"
    "    await new Promise((r) => setTimeout(r, 25)); } };\n"
)
# A budget long enough that the stub's setup is in place before it fires, even under load.
_BUDGET_MS = 3000


def _run_bun_driver(tmp_path: Path, source: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    driver = tmp_path / "driver.ts"
    driver.write_text(_DRIVER_PROLOGUE + source, encoding="utf-8")
    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["STUB_READY"] = str(tmp_path / "stub.ready")
    # Hermetic file log: extension diagnostics must never touch process stderr
    # (TUI noise), so they go to this file; default it into the test sandbox
    # unless the caller overrides it explicitly.
    env.setdefault("HOOKS_DEBUG_LOG", str(tmp_path / "extension-debug.log"))
    env.update(env_overrides)
    return subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=60)


def _debug_log_text(tmp_path: Path, name: str = "extension-debug.log") -> str:
    """Extension file-log content for the hermetic default path in `_run_bun_driver`."""
    log = tmp_path / name
    return log.read_text(encoding="utf-8") if log.is_file() else ""


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_tool_call_does_not_block_the_event_loop(tmp_path):
    """agent-tools#558 (the whole point): while the dispatcher runs, omp's event loop must keep
    turning. A timer armed BEFORE the handler is awaited fires DURING a 1.5 s dispatch; with the
    old synchronous spawn it could not fire until the child had already exited."""
    stub = _write_stub_dispatcher(tmp_path, "time.sleep(1.5)\n")
    proc = _run_bun_driver(
        tmp_path,
        "let ticked = false;\n"
        "const timer = setTimeout(() => { ticked = true; }, 200);\n"
        "const result = await handlers['tool_call'](event, ctx);\n"
        "clearTimeout(timer);\n"
        "console.log(JSON.stringify({ ticked, result: result ?? null }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ticked"] is True, "the omp event loop was blocked for the whole dispatch"
    assert out["result"] is None


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_async_path_still_propagates_block_and_allow(tmp_path):
    """The asynchronous path must carry BOTH decisions through unchanged: an explicit block
    decision becomes `{block, reason}`, a silent exit-0 dispatcher stays "no opinion" (allow)."""
    blocker = _write_stub_dispatcher(
        tmp_path, "print(json.dumps({'decision': 'block', 'reason': 'stub blocked it'}))\n"
    )
    allower = tmp_path / "allow_dispatcher.py"
    allower.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n", encoding="utf-8")
    allower.chmod(0o755)

    driver = "console.log(JSON.stringify((await handlers['tool_call'](event, ctx)) ?? null));\n"
    blocked = _run_bun_driver(tmp_path, driver, {"OMP_HOOK_BRIDGE_PYTHON": str(blocker)})
    assert blocked.returncode == 0, blocked.stderr
    assert json.loads(blocked.stdout.strip().splitlines()[-1]) == {
        "block": True,
        "reason": "stub blocked it",
    }

    allowed = _run_bun_driver(tmp_path, driver, {"OMP_HOOK_BRIDGE_PYTHON": str(allower)})
    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(allowed.stdout.strip().splitlines()[-1]) is None


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_async_path_fails_open_when_the_dispatcher_crashes(tmp_path):
    """Fail-OPEN is unchanged on the asynchronous path: a dispatcher that dies with a traceback
    on stderr and a non-zero exit resolves to "allow" (undefined) and never throws — a thrown
    handler error is how omp BLOCKS a call, which would brick a live session."""
    stub = _write_stub_dispatcher(tmp_path, "sys.stderr.write('boom\\n')\nsys.exit(1)\n")
    proc = _run_bun_driver(
        tmp_path,
        "let threw: string | null = null;\n"
        "let result: unknown = 'unset';\n"
        "try { result = (await handlers['tool_call'](event, ctx)) ?? null; }\n"
        "catch (error: any) { threw = String(error?.message ?? error); }\n"
        "console.log(JSON.stringify({ threw, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"threw": None, "result": None}
    # The dispatcher's own stderr never reaches the terminal — it is captured to
    # the file log, tagged per line with the event and the tool-call id, so two
    # overlapping dispatches stay attributable.
    assert proc.stderr == ""
    assert "omp-hook-bridge[tool_call c1] boom" in _debug_log_text(tmp_path)


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_honors_the_dispatcher_timeout_budget(tmp_path):
    """The timeout budget survives the rewrite: OMP_HOOK_BRIDGE_TIMEOUT_MS bounds the wait, the
    child is killed rather than left running, and the timeout resolves to "allow" (fail-open).
    Reading the env var per call — not once at module load — is what makes this testable."""
    stub = _write_stub_dispatcher(tmp_path, "time.sleep(30)\n")
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": "500"},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert out["elapsedMs"] < 10000, f"the 500 ms budget was not honored: {out['elapsedMs']} ms"
    assert proc.stderr == ""
    assert "timed out" in _debug_log_text(tmp_path)


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_tool_result_awaits_the_dispatcher(tmp_path):
    """The post-write path must await too. If that `await` is ever dropped, `result` is a
    pending Promise, `result?.block` is undefined, and the "would have blocked" line silently
    stops being logged — a failure no `tool_call` test would notice."""
    stub = _write_stub_dispatcher(
        tmp_path, "print(json.dumps({'decision': 'block', 'reason': 'post-write says no'}))\n"
    )
    proc = _run_bun_driver(
        tmp_path,
        "await handlers['tool_result']({ toolName: 'write', input: { path: '/tmp/x' } }, ctx);\n"
        "console.log(JSON.stringify('done'));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    logged = _debug_log_text(tmp_path)
    assert "post-write hook would have blocked" in logged
    assert "post-write says no" in logged


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_explains_a_slow_tool_call_but_stays_quiet_on_tool_result(tmp_path):
    """A long wait is now silent by itself (the freeze used to be its own explanation), so the
    bridge says once, in the file log, why the call is being held and which omp setting decides how
    long it may be held. Only for `tool_call`: that is the event omp can refuse. A slow
    `tool_result` is nobody's problem — the write already landed — so it stays quiet."""
    stub = _write_stub_dispatcher(tmp_path, "time.sleep(1.5)\n")
    env = {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_NOTICE_MS": "300"}

    called = _run_bun_driver(
        tmp_path,
        "await handlers['tool_call'](event, ctx);\n",
        {**env, "HOOKS_DEBUG_LOG": str(tmp_path / "called-debug.log")},
    )
    assert called.returncode == 0, called.stderr
    assert called.stderr == ""
    called_log = _debug_log_text(tmp_path, "called-debug.log")
    assert "holding this call for" in called_log
    assert "extensionHandlers.toolCallTimeoutMs" in called_log

    result = _run_bun_driver(
        tmp_path,
        "await handlers['tool_result']({ toolName: 'write', input: { path: '/tmp/x' } }, ctx);\n",
        {**env, "HOOKS_DEBUG_LOG": str(tmp_path / "result-debug.log")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "holding this call for" not in _debug_log_text(tmp_path, "result-debug.log")


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_runs_overlapping_dispatches_independently(tmp_path):
    """The synchronous spawn serialized every dispatch as a side effect of blocking the loop;
    awaiting means two tool calls can now be in flight at once. Each must get its own child and
    its own decision — and the pair must take about as long as ONE dispatch, not two."""
    stub = tmp_path / "concurrent_dispatcher.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "payload = json.loads(sys.stdin.read())\n"
        "time.sleep(1.0)\n"
        "if payload['input'].get('command') == 'blocked':\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'no'}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const [a, b] = await Promise.all([\n"
        "  handlers['tool_call']({ toolName: 'bash', input: { command: 'blocked' } }, ctx),\n"
        "  handlers['tool_call']({ toolName: 'bash', input: { command: 'allowed' } }, ctx),\n"
        "]);\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, a: a ?? null, b: b ?? null }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["a"] == {"block": True, "reason": "no"}
    assert out["b"] is None
    assert out["elapsedMs"] < 1900, f"the two dispatches ran serially: {out['elapsedMs']} ms"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_timeout_sends_sigterm_first_and_lets_the_dispatcher_clean_up(tmp_path):
    """On a timeout the child gets SIGTERM first (what the old synchronous spawn sent), so a
    dispatcher that cleans up after itself still can: the stub traps SIGTERM, reports it on
    stderr and exits on its own — the bridge must then NOT wait out the SIGKILL grace."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "import signal\n"
        "def bye(*_a):\n"
        "    sys.stderr.write('stub: got SIGTERM, cleaning up\\n')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": str(_BUDGET_MS)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert proc.stderr == ""
    logged = _debug_log_text(tmp_path)
    assert "stub: got SIGTERM, cleaning up" in logged
    assert "timed out" in logged
    # budget + the dispatcher's own exit; well under budget + the 2000 ms SIGKILL grace.
    assert out["elapsedMs"] < _BUDGET_MS + 1500, f"the bridge waited out the SIGKILL grace: {out['elapsedMs']} ms"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_timeout_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    """A dispatcher that ignores SIGTERM is still ended: SIGKILL follows after the grace, so the
    handler settles (fail-open, "allow") instead of hanging on a child that never exits."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": str(_BUDGET_MS)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert proc.stderr == ""
    assert "timed out" in _debug_log_text(tmp_path)
    # Must outlast budget + the 2000 ms grace (SIGTERM alone did nothing), never the 30 s sleep.
    assert _BUDGET_MS + 2000 <= out["elapsedMs"] < 15000, f"SIGKILL did not follow the grace: {out['elapsedMs']} ms"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_reaps_in_flight_dispatcher_when_the_process_exits(tmp_path):
    """omp can now EXIT mid-dispatch (the loop is no longer frozen), and the budget timers are
    unref'd, so nothing would end the child: the extension must SIGTERM every in-flight dispatcher
    on process exit instead of orphaning one that may be holding a Telegram hatch open."""
    pid_file = tmp_path / "stub.pid"
    stub = _write_stub_dispatcher(
        tmp_path,
        "import os\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "ready()\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        _WAIT_READY + "void handlers['tool_call'](event, ctx);\n"
        "await waitReady();\n"
        "process.exit(0);\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert "in-flight dispatcher" in _debug_log_text(tmp_path)
    pid = int(pid_file.read_text())
    assert _pid_gone_within(pid, seconds=3), f"dispatcher {pid} was orphaned by the exiting host"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_reaps_in_flight_dispatcher_on_session_shutdown(tmp_path):
    """omp's own teardown path: a `session_shutdown` handler ends the running dispatcher too, so
    the awaited `tool_call` settles (fail-open) instead of outliving the session."""
    stub = _write_stub_dispatcher(tmp_path, "ready()\ntime.sleep(30)\n")
    proc = _run_bun_driver(
        tmp_path,
        _WAIT_READY + "const pending = handlers['tool_call'](event, ctx);\n"
        "await waitReady();\n"
        "const shutdownAt = Date.now();\n"
        "void handlers['session_shutdown']({}, ctx);\n"
        "const result = (await pending) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - shutdownAt, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert out["elapsedMs"] < 1000, f"the dispatcher outlived the shutdown's SIGTERM: {out['elapsedMs']} ms"
    assert proc.stderr == ""
    logged = _debug_log_text(tmp_path)
    assert "session shutdown" in logged
    assert "terminated by SIGTERM" in logged


def _pid_gone_within(pid: int, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_session_shutdown_escalates_to_sigkill(tmp_path):
    """Shutdown must not trust SIGTERM alone (the timeout path never did): a dispatcher that
    ignores it is SIGKILLed after the 1 s shutdown grace — inside omp's 2 s shutdown-handler cap —
    instead of surviving omp as an orphan holding a hatch open."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "ready()\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        _WAIT_READY + "const pending = handlers['tool_call'](event, ctx);\n"
        "await waitReady();\n"
        "const shutdownAt = Date.now();\n"
        "void handlers['session_shutdown']({}, ctx);\n"
        "const result = (await pending) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - shutdownAt, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert 1000 <= out["elapsedMs"] < 4000, f"SIGKILL did not follow the shutdown grace: {out['elapsedMs']} ms"
    assert proc.stderr == ""
    assert "terminated by SIGKILL" in _debug_log_text(tmp_path)


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_stop_reaches_the_dispatcher_process_group(tmp_path):
    """A hook script forked by the dispatcher (same process group) goes down with it: the child
    is spawned as a group leader and signalled as a group, so a budget timeout does not leave the
    dispatcher's own helpers running after the dispatcher itself is gone."""
    grandchild_pid = tmp_path / "grandchild.pid"
    stub = _write_stub_dispatcher(
        tmp_path,
        "import subprocess\n"
        "helper = subprocess.Popen(['sleep', '30'])\n"
        f"pathlib.Path({str(grandchild_pid)!r}).write_text(str(helper.pid))\n"
        "helper.wait()\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify(result));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": str(_BUDGET_MS)},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) is None
    assert _pid_gone_within(int(grandchild_pid.read_text()), seconds=3), "the forked helper outlived the dispatcher"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_caps_dispatcher_stdout(tmp_path):
    """A dispatcher that never stops writing stdout must not grow omp's memory until it dies (the
    old spawnSync had a bounded buffer): past 1 MiB the child is stopped and the call resolves
    fail-open, promptly, with the reason in the file log."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "sys.stdout.write('x' * (2 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert out["elapsedMs"] < 8000, f"the overflowing dispatcher was not stopped: {out['elapsedMs']} ms"
    assert proc.stderr == ""
    assert "output limit" in _debug_log_text(tmp_path)


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_flushes_a_stderr_line_that_never_ends(tmp_path):
    """An unterminated stderr stream is captured to the file log in bounded pieces (64 KiB)
    rather than held in memory waiting for a newline; each piece still carries the call tag."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "sys.stderr.write('y' * (200 * 1024))\n"
        "sys.stderr.flush()\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "console.log(JSON.stringify((await handlers['tool_call'](event, ctx)) ?? null));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout.strip().splitlines()[-1]) is None
    assert proc.stderr == ""
    tagged = [
        line
        for line in _debug_log_text(tmp_path).splitlines()
        if "omp-hook-bridge[tool_call c1] y" in line
    ]
    assert len(tagged) >= 3, f"expected the 200 KiB line in bounded pieces, got {len(tagged)} piece(s)"
    assert all(len(line) <= 64 * 1024 + 64 for line in tagged)
    assert sum(line.count("y") for line in tagged) == 200 * 1024

# --- Review-quorum hardening fixes on agent-tools#614 (PR #621) ----------------------------


def test_eval_bare_write_recognizes_fs_named_and_fs_suffixed_receivers():
    """Security-review finding: the old `(?<!fs)` lookbehind excluded ANY receiver merely
    ENDING in the two letters `f`/`s` (no identifier-boundary check) -- a plain Python file
    handle named `fs` (`fs = open(...); fs.write(<value>)`) or any OTHER receiver ending in
    "fs" (`myfs.write(<value>)`) produced an empty scraped bucket, hiding the written value
    from `block-secrets-write` entirely. Neither call matches `_EVAL_FS_WRITE` (which requires
    a literal `File` suffix), so there was never a genuine path/content swap risk to guard
    against for either receiver -- the exclusion was pure over-block."""
    marker = "abcd1234abcd1234"
    fs_named = f"fs = open('x', 'w')\nfs.write('{marker}')\n"
    fs_suffixed = f"myfs.write('{marker}')\n"

    assert dispatch._eval_content_buckets(fs_named) == {"": marker}
    assert dispatch._eval_content_buckets(fs_suffixed) == {"": marker}


def test_eval_bare_write_still_excludes_whole_word_bun_receiver():
    """The narrowed `(?<!\\bBun)` lookbehind must still exclude a call whose receiver is
    EXACTLY `Bun` (the genuine `Bun.write(path, content)` swap-risk case) while no longer
    over-excluding a receiver that merely ends in those letters."""
    marker = "abcd1234abcd1234"
    whole_bun = f"Bun.write('src/target.js', `{marker}`)\n"
    suffixed_bun = f"myBun.write('{marker}')\n"

    # The whole-word `Bun.write` call IS still recognized -- by its own dedicated two-literal
    # pattern (`_EVAL_BUN_WRITE`), which the bare fallback's exclusion is there to protect.
    assert dispatch._eval_content_buckets(whole_bun) == {"src/target.js": marker}
    # A `Bun`-suffixed receiver is not excluded and falls through to the path-less bucket.
    assert dispatch._eval_content_buckets(suffixed_bun) == {"": marker}


def test_omp_bridge_blocks_secret_write_via_eval_tool_fs_named_file_handle(tmp_path):
    """End-to-end security-review regression: a credential written through a plain Python
    file handle literally named `fs` must still reach the real `block-secrets-write`
    descriptor."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    marker = "abcd1234abcd1234"
    payload = "API_KEY = '" + marker + "'"
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": "fs = open('src/target.py', 'w')\nfs.write(\"" + payload + "\")\n"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_literal_does_not_catastrophically_backtrack_on_unterminated_backslash_run():
    """Security-review finding (ReDoS): the old `(?:\\\\.|(?!closing).)*?` body alternative let
    a lone backslash be consumed either via `\\\\.` or via the bare `.` fallback, and that
    ambiguity's backtracking on a long run of backslashes before an unterminated quote was
    catastrophic (verified separately: 35 backslashes already took over a second, and each +5
    backslashes roughly doubled the time). Excluding `\\\` from the plain alternative removes
    the ambiguity; a MUCH larger adversarial payload must still resolve in well under 2s."""
    payload = "Path(\"" + ("\\\\" * 5000)  # opening quote, 5000 backslashes, never closed

    start = time.monotonic()
    result = dispatch._eval_content_buckets(payload)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"took {elapsed}s -- catastrophic backtracking regressed"
    assert result == {}  # never terminated -- honestly unrecognized, not a hang


def test_to_v1_event_hub_cwd_override_is_case_insensitive_for_tool_name():
    """Not a live bug (verified against the actual code, disputed from the review-quorum
    finding list): `_cwd`'s `_tool_name(event) == "hub"` check already compares against
    `_tool_name`'s OWN lowercased return value (`_tool_name` is `str(...).lower()`), so a
    `toolName` of `"HUB"`/`"Hub"` already normalizes to `"hub"` before the comparison runs --
    there is no bare case-sensitive comparison bypassing the cwd override. This regression
    test pins that already-correct behavior."""
    omp_event = {
        "event": "tool_call",
        "toolName": "HUB",
        "input": {"op": "start", "application": "npm", "args": ["run", "dev"], "cwd": "/other/dir"},
        "cwd": "/session/dir",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["cwd"] == "/other/dir"



def test_to_v1_event_hub_non_start_op_ignores_cwd_override():
    """Security-review finding: `_cwd`'s hub-cwd override applied to EVERY `hub` op, not just
    `op: "start"` -- contradicting its own docstring (which only documents `op: "start"`
    launching a process in a different directory) and inconsistent with `_hub_command`'s own
    `op == "start"` gate (a non-`start` op synthesizes no command at all). A `hub list`/`hub
    logs`/`hub send` call inspects or manipulates an EXISTING process; it launches nothing and
    has no "where does this run" semantics, so a `cwd` field riding along on one of those ops
    (forged, or simply scoping a `logs`/`list` call to a directory) must not silently override
    the reported v1 event cwd for a cwd-sensitive pre-bash hook."""
    omp_event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "list", "cwd": "/other/dir"},
        "cwd": "/session/dir",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["cwd"] == "/session/dir"


def test_eval_unescape_decodes_js_code_point_brace_escape():
    """Security-review finding: a JS `\\u{HEX}` Unicode CODE-POINT escape is pure ASCII, so it
    passes the `encode("ascii")` gate, but Python's `unicode_escape` codec only understands the
    fixed-width `\\uHHHH` form and raises on the brace form -- the old code then returned the
    ENTIRE ORIGINAL, still-escaped string on that decode error, so a value written via
    `\\u{61}`-style escapes stayed in non-matching escaped form for the scanner while the REAL
    decoded value landed on disk."""
    decoded = dispatch._eval_unescape(r"\u{61}bcd1234abcd1234")

    assert decoded == "abcd1234abcd1234"


def test_eval_unescape_code_point_widening_never_narrows_on_non_ascii_result():
    """A code point above 0x7F makes the widened text non-ASCII, so the subsequent
    `unicode_escape` pass is skipped (same ASCII-only guard as before) -- the widened text
    (with the real character substituted in) must still be returned, never the raw
    still-escaped original and never an empty string."""
    decoded = dispatch._eval_unescape(r"prefix-\u{1F600}-abcd1234abcd1234")

    assert decoded == "prefix-\U0001F600-abcd1234abcd1234"


def test_omp_bridge_blocks_secret_write_via_eval_tool_unicode_brace_escape(tmp_path):
    """End-to-end security-review regression: a credential written via a JS `\\u{HEX}`
    code-point escape must still reach the real `block-secrets-write` descriptor."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    # \u{41}\u{50}\u{49}_\u{4b}\u{45}\u{59} decodes to "API_KEY", followed by ` = ` and a
    # 16+ char value -- escaped-only (non-matching) before this fix, a real credential-shaped
    # assignment once decoded.
    escaped = r"\u{41}\u{50}\u{49}_\u{4b}\u{45}\u{59} = 'abcd1234abcd1234'"
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "js", "code": "Bun.write('src/target.js', `" + escaped + "`)"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_to_v1_event_eval_recognizes_writelines_list_argument():
    """Security-review finding: `.writelines(...)` takes a LIST of strings, but the old
    pattern required a bare string literal immediately after the opening paren -- a real
    `open(path, 'w').writelines([...])` call matched NOTHING and its content (including any
    embedded credential) was invisible to every scan."""
    marker = "abcd1234abcd1234"
    code = "open('out.txt', 'w').writelines(['line1\\n', '" + marker + "\\n'])\n"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "out.txt"
    assert marker in v1["args"]["content"]


def test_omp_bridge_blocks_secret_write_via_eval_tool_writelines_list(tmp_path):
    """End-to-end security-review regression: a credential inside a `.writelines([...])` list
    argument must still reach the real `block-secrets-write` descriptor."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    marker = "abcd1234abcd1234"
    payload = "API_KEY = '" + marker + "'"
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "open('src/target.py', 'w').writelines(['ok\\n', '" + payload + "\\n'])\n",
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_to_v1_event_hub_start_command_synthesis_normalizes_malformed_fields():
    """Security-review finding: `_hub_command` failed OPEN (synthesized an empty command,
    sailing past command-keyed hooks like `no-long-inline-process`) on a case-varied `op`
    (`"Start"`), a non-string `application`, or `args` passed as a bare string instead of a
    list -- asymmetric with the eval path's own fail-CLOSED philosophy. All three malformed
    shapes must still synthesize a non-empty command."""
    case_varied = dispatch.to_v1_event(
        {
            "event": "tool_call",
            "toolName": "hub",
            "input": {"op": "Start", "application": "npm", "args": ["run", "test"]},
            "cwd": "/repo",
        },
        point="pre-bash",
    )
    assert case_varied["command"] == "npm run test"

    non_string_application = dispatch.to_v1_event(
        {
            "event": "tool_call",
            "toolName": "hub",
            "input": {"op": "start", "application": 123, "args": ["--flag"]},
            "cwd": "/repo",
        },
        point="pre-bash",
    )
    assert non_string_application["command"] == "123 --flag"

    non_list_args = dispatch.to_v1_event(
        {
            "event": "tool_call",
            "toolName": "hub",
            "input": {"op": "start", "application": "npm", "args": "run test"},
            "cwd": "/repo",
        },
        point="pre-bash",
    )
    assert non_list_args["command"] != ""
    assert "run test" in non_list_args["command"]


def test_omp_bridge_blocks_long_process_via_hub_start_case_varied_op(tmp_path):
    """End-to-end security-review regression: a `hub` call with a case-varied `op` (`"Start"`)
    must still reach `no-long-inline-process` the same as a lowercase `"start"` does."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir, hook_id="no-long-inline-process", point="pre-bash", cmd=NO_LONG_INLINE_PROCESS
    )
    event = {
        "event": "tool_call",
        "toolName": "hub",
        "input": {"op": "Start", "application": "npm", "args": ["run", "test"], "name": "test-runner"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_to_v1_event_eval_fstring_path_with_interpolation_is_not_a_confident_literal():
    """Security-review finding: an f-string/template-literal PATH containing a real
    interpolation (`Path(f"{p}/x.py")`) must NOT be reported as a resolved literal path -- the
    raw un-interpolated template text (`{p}/x.py`) is not what Python actually opens. It must
    fall through to the path-less bucket instead, same as any other variable-built path."""
    marker = "abcd1234abcd1234"
    code = 'Path(f"{p}/x.py").write_text("' + marker + '")\n'
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert "file_path" not in v1["args"]
    assert "path" not in v1["args"]
    assert v1["args"]["content"] == marker


def test_to_v1_event_eval_fstring_path_without_interpolation_is_still_confident():
    """An f-string PATH with no real interpolation (only doubled `{{`/`}}` escaped-literal
    braces, or none at all) is a perfectly good literal and must still resolve normally --
    the fix must not regress the common `f"..."`-prefixed-but-static case."""
    marker = "abcd1234abcd1234"
    code = 'Path(f"static{{lit}}.py").write_text("' + marker + '")\n'
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "static{{lit}}.py"
    assert v1["args"]["content"] == marker


def test_omp_bridge_blocks_secret_write_via_eval_tool_interpolated_fstring_path(tmp_path):
    """End-to-end security-review regression: an interpolated f-string path must not let a
    fabricated ALLOW_PATH-style literal decide the verdict -- the content still reaches
    `block-secrets-write` via the path-less bucket."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    marker = "abcd1234abcd1234"
    payload = "API_KEY = '" + marker + "'"
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": 'Path(f"{base}/x.py").write_text("' + payload + '")\n'},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


# --- Round 2: further review-quorum hardening on agent-tools#614 (PR #621, second pass) -----


def test_eval_content_captures_full_string_concatenation_not_just_first_chunk():
    """Security-review finding (round 2): every content-capturing write pattern only ever
    captured a SINGLE quoted-literal chunk immediately after the opening paren/comma, with
    nothing requiring the argument to end there -- Python's bare-adjacency implicit
    concatenation or a JS `+`-joined concatenation both fold into ONE value at parse/runtime,
    but the old scrape silently dropped everything after the FIRST chunk (missing the actual
    secret). Both join forms must now concatenate every chunk's DECODED text."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    bare_adjacency = "Path('src/secret.py').write_text(\"" + prefix + "\" \"" + secret + "\")"
    plus_joined = "Bun.write('src/secret.js', \"" + prefix + "\" + \"" + secret + "\")"

    bare_buckets = dispatch._eval_content_buckets(bare_adjacency)
    plus_buckets = dispatch._eval_content_buckets(plus_joined)

    assert bare_buckets["src/secret.py"] == prefix + secret
    assert plus_buckets["src/secret.js"] == prefix + secret


def test_omp_bridge_blocks_secret_write_via_eval_tool_concatenated_content(tmp_path):
    """End-to-end security-review regression: a credential split across a bare-adjacency
    string concatenation must still reach the real `block-secrets-write` descriptor."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "Path('src/secret.py').write_text(\"" + prefix + "\" \"" + secret + "\")",
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_unescape_decodes_escape_even_with_unrelated_non_ascii_elsewhere():
    """Security-review finding (round 2): `_eval_unescape`'s old whole-string `encode("ascii")`
    gate was all-or-nothing -- a single non-ASCII character ANYWHERE in the literal (even a
    trailing emoji with no relation to the escape sequence carrying the actual secret) skipped
    decoding the ENTIRE string, leaving a hex-escaped secret undecoded and therefore unscanned.
    Decoding must happen per maximal-ASCII run so an unrelated non-ASCII character elsewhere in
    the same literal can no longer hide an escaped secret."""
    secret = "abcd1234abcd1234"
    text = "API_KEY = \\x61" + secret[1:] + " " + "\U0001F600"

    decoded = dispatch._eval_unescape(text)

    assert secret in decoded
    assert "\U0001F600" in decoded  # the unrelated non-ASCII character is preserved, not dropped


def test_omp_bridge_blocks_secret_write_via_eval_tool_hex_escape_with_trailing_emoji(tmp_path):
    """End-to-end security-review regression: a hex-escaped credential must still reach
    `block-secrets-write` even when the same literal also contains an unrelated non-ASCII
    character (previously enough to disable decoding for the WHOLE literal)."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "Path('src/secret.py').write_text('API_KEY = \\x61" + secret[1:] + " \U0001F600')",
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_write_text_recognizes_data_keyword_argument():
    """Security-review finding (round 2): pathlib's real `write_text(data=...)` /
    `write_bytes(data=...)` keyword-argument form (`data` is the actual parameter name both
    methods accept) was entirely unrecognized by the old positional-argument-only patterns --
    content silently became empty and no scan happened at all."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "

    text_buckets = dispatch._eval_content_buckets(
        "Path('src/secret.py').write_text(data='" + prefix + secret + "')"
    )
    bytes_buckets = dispatch._eval_content_buckets(
        "Path('src/secret.py').write_bytes(data=b'" + prefix + secret + "')"
    )

    assert text_buckets["src/secret.py"] == prefix + secret
    assert bytes_buckets["src/secret.py"] == prefix + secret


def test_omp_bridge_blocks_secret_write_via_eval_tool_write_text_data_kwarg(tmp_path):
    """End-to-end security-review regression: a credential written via pathlib's real
    `write_text(data=...)` keyword form must still reach `block-secrets-write`."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "Path('src/secret.py').write_text(data='" + prefix + secret + "')",
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_write_call_anchors_require_identifier_boundary():
    """Security-review finding (round 2): none of the positive scrape-pattern anchors
    (`Path\\(`, `open\\(`, `Bun\\.write\\(`, the `fs...File(?:Sync)?\\(` family) required a
    leading identifier/word boundary, so a decoy receiver like `myopen(...)` matched MID-
    IDENTIFIER as if it were a real call to the builtin `open(` -- misrouting a secret write
    under whatever decoy text followed in the call AND suppressing the path-less fallback that
    would otherwise have caught it as unrecognized. `myPath`/`myopen` decoys have a path-less
    fallback pattern to catch their content instead; the `fs...File(?:Sync)?` family has no
    such fallback (`_EVAL_BARE_WRITE` only covers bare `.write`/`.write_text`), so a decoy like
    `xwriteFileSync(` must land at honest NON-recognition (no bucket at all, matching the
    established `Bun.write` swap-prevention precedent) rather than a decoy-path match."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "

    my_path = dispatch._eval_content_buckets("myPath('decoy.example').write_text('" + prefix + secret + "')")
    my_open = dispatch._eval_content_buckets(
        "myopen('decoy.example', 'w').write('" + prefix + secret + "')"
    )
    x_write_file_sync = dispatch._eval_content_buckets(
        "xwriteFileSync('decoy.example', '" + prefix + secret + "')"
    )

    for buckets, label in ((my_path, "myPath"), (my_open, "myopen")):
        assert "decoy.example" not in buckets, (label, buckets)
        assert secret in "".join(buckets.values()), (label, buckets)

    # No dedicated pattern AND no fallback covers `writeFileSync(` -- honest non-recognition,
    # never a decoy-path match that would hide the secret behind `decoy.example`'s verdict.
    assert x_write_file_sync == {}


def test_omp_bridge_blocks_secret_write_via_eval_tool_decoy_receiver_mid_identifier(tmp_path):
    """End-to-end security-review regression: `myopen('decoy.example', 'w').write(<secret>)`
    must not let the decoy `.example`-suffixed (ALLOW_PATH-shaped) text decide the verdict --
    the secret content still reaches `block-secrets-write` via the path-less fallback."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "myopen('decoy.example', 'w').write('" + prefix + secret + "')",
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_confident_path_is_escape_decoded_before_allowlist_comparison():
    """Security-review finding (round 2): the captured PATH literal was reported VERBATIM
    (never escape-decoded), unlike content which IS decoded -- a path with escaped `..`
    traversal (`\\x2e\\x2e` decodes to `..`) reported the raw backslash-escaped text as the
    scraped `file_path`, not the string Python actually opens at runtime. A prefix-shaped
    allowlist check against the raw text cannot see the `..` traversal the ACTUAL write
    targets. The scraped path must be escape-decoded the same way content already is."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    code = "Path('fixtures/\\x2e\\x2e/src/secret.py').write_text('" + prefix + secret + "')"

    buckets = dispatch._eval_content_buckets(code)

    assert "fixtures/../src/secret.py" in buckets
    assert buckets["fixtures/../src/secret.py"] == prefix + secret
    assert "fixtures/\\x2e\\x2e/src/secret.py" not in buckets


def test_omp_bridge_worktree_only_writes_sees_escape_decoded_traversal_path():
    """End-to-end security-review regression: a path-based policy hook must be handed the
    DECODED path -- containing the real `..` traversal -- not the raw escaped source text, so
    a path-based verdict is computed against what Python actually opens."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    code = "Path('fixtures/\\x2e\\x2e/src/secret.py').write_text('" + prefix + secret + "')"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "fixtures/../src/secret.py"


def test_eval_unescape_does_not_double_decode_widened_backslash_escape():
    """Security-review finding (round 3): the old two-pass design widened `\\u{HEX}` to a real
    character FIRST, then ran the WHOLE widened string through Python's `unicode_escape` codec
    -- widening `\\u{5c}` (hex `5c` = backslash) produced a literal backslash character, and
    because the very next source character happened to be `n`, the SUBSEQUENT `unicode_escape`
    pass saw that now-adjacent backslash + `n` as a BRAND NEW `\\n` escape and turned it into
    an actual newline -- even though the real JS/Python value of `\\u{5c}n` is the two literal
    characters backslash-then-`n`, never a newline. A single-pass, one-token-at-a-time decode
    must never let a widened character seed a second escape sequence."""
    decoded = dispatch._eval_unescape(r"\u{5c}n")

    assert decoded == "\\n"
    assert decoded != "\n"


def test_omp_bridge_eval_content_reports_widened_backslash_literally_not_as_newline():
    """End-to-end security-review regression: content scraped through the full `to_v1_event`
    pipeline for a `\\u{5c}n`-style escape must carry the literal backslash-then-`n` two
    characters the real JS runtime produces, not a double-decoded newline that would let the
    scanned content diverge from what the write actually puts on disk."""
    secret = "abcd1234abcd1234"
    code = "Bun.write('out.txt', 'prefix" + r"\u{5c}n" + secret + "')"
    omp_event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "js", "code": code},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["content"] == "prefix\\n" + secret
    assert v1["args"]["content"] != "prefix\n" + secret


def test_eval_confident_path_honors_raw_string_no_escape_semantics():
    """Security-review finding (round 3): a RAW-STRING path (`r"..."`) never interprets its own
    escape sequences at runtime, so `Path(r"secret\\x2eexample")` opens the literal path
    `secret\\x2eexample` -- decoding it here the same way a normal string is decoded would
    report the DECODED `secret.example` instead, which a suffix-anchored allowlist pattern
    like `.example$` matches even though the real (undecoded) path never would."""
    secret = "abcd1234abcd1234"
    code = 'Path(r"secret\\x2eexample").write_text("' + secret + '")'

    buckets = dispatch._eval_content_buckets(code)

    assert "secret\\x2eexample" in buckets
    assert buckets["secret\\x2eexample"] == secret
    assert "secret.example" not in buckets


def test_omp_bridge_blocks_secret_write_via_eval_tool_raw_string_path_not_decoded(tmp_path):
    """End-to-end security-review regression: a raw-string path whose escape-decoded form
    would falsely match `block-secrets-write`'s `.example$` allowlist must be judged on its
    real, undecoded text instead -- the secret write must still be blocked."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    code = 'Path(r"secret\\x2eexample").write_text("' + prefix + secret + '")'
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_write_text_recognizes_data_keyword_after_other_keyword_argument():
    """Security-review finding (round 3): pathlib resolves keyword arguments by name, not
    position, so `write_text(encoding="utf-8", data=secret)` is exactly as valid a call as
    `write_text(data=secret, encoding="utf-8")` -- round 2's fix only tolerated `data=` as the
    FIRST token after `(`, so any OTHER keyword argument appearing before it made the whole
    call fail to match and silently dropped the content to an empty bucket."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "

    buckets = dispatch._eval_content_buckets(
        "Path('src/secret.py').write_text(encoding='utf-8', data='" + prefix + secret + "')"
    )

    assert buckets["src/secret.py"] == prefix + secret


def test_omp_bridge_blocks_secret_write_via_eval_tool_data_kwarg_after_other_keyword(tmp_path):
    """End-to-end security-review regression: `write_text(encoding=..., data=<secret>)` --
    `data=` as a NON-FIRST keyword argument -- must still reach `block-secrets-write`."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {
            "language": "py",
            "code": "Path('src/secret.py').write_text(encoding='utf-8', data='" + prefix + secret + "')",
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_bun_write_falls_through_to_pathless_bucket_on_unresolvable_path():
    """Security-review finding (round 3): unlike `Path(...)`/`open(...)`, whose dedicated
    patterns fall through to the path-less bare-write fallback whenever the path argument
    isn't a confidently-resolved literal, `_EVAL_BUN_WRITE`/`_EVAL_FS_WRITE` required BOTH
    arguments to be a recognized string literal -- a computed/concatenated first argument
    (`Bun.write('a' + 'b', secret)`) made the WHOLE call fail to match and silently dropped
    the second argument's content too, instead of scanning it path-lessly the way `Path`/`open`
    already do."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "

    buckets = dispatch._eval_content_buckets("Bun.write('a' + 'b', '" + prefix + secret + "')")

    assert buckets[""] == prefix + secret


def test_eval_fs_write_file_falls_through_to_pathless_bucket_on_unresolvable_path():
    """Sibling of the `Bun.write` round-3 finding: `fs.writeFileSync(<computed>, secret)` must
    likewise fall through to the path-less bucket rather than dropping the content."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "

    buckets = dispatch._eval_content_buckets(
        "fs.writeFileSync(dir + name, '" + prefix + secret + "')"
    )

    assert buckets[""] == prefix + secret


def test_omp_bridge_blocks_secret_write_via_eval_tool_bun_write_unresolvable_path(tmp_path):
    """End-to-end security-review regression: `Bun.write('a' + 'b', <secret>)` -- an
    unresolvable, string-concatenated first argument -- must still reach `block-secrets-write`
    via the new path-less fallback rather than silently dropping the secret content."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "js", "code": "Bun.write('a' + 'b', '" + prefix + secret + "')"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_builtin_write_recognizes_bare_receiverless_global_and_excludes_decoys():
    """Security-review finding (agent-tools#614): the Eval tool's own documented global
    builtin -- `write(path, content) -> str`, the FIRST function listed in its prelude --
    matched NONE of the existing patterns, every one of which requires a `Path(`/`open(`/
    `Bun.`/`fs.`-prefixed call or a dotted `.write(...)` receiver call. A bare
    `write("/etc/cron.d/job", <secret>)` call in an eval cell was a TOTAL blind spot -- it
    matched nothing at all, not even the path-less fallback, so the secret never reached
    `block-secrets-write`. `_EVAL_BUILTIN_WRITE` closes this while requiring the ABSENCE of
    a preceding `.` or identifier character immediately before `write(`, so it recognizes
    the bare builtin call but never a dotted receiver call (already handled by
    `_EVAL_BARE_WRITE`) or a decoy like `mywrite(...)`."""
    marker = "abcd1234abcd1234"
    prefix = "API_KEY = "

    builtin_buckets = dispatch._eval_content_buckets(
        'write("/etc/cron.d/job", "' + prefix + marker + '")'
    )
    assert builtin_buckets == {"/etc/cron.d/job": prefix + marker}

    # Sibling dotted-receiver call must still resolve via its EXISTING pattern, unchanged --
    # no double-count, no regression.
    dotted_buckets = dispatch._eval_content_buckets('f.write("' + prefix + marker + '")')
    assert dotted_buckets == {"": prefix + marker}

    # A decoy receiver-less-looking call that is actually a different identifier
    # (`mywrite(...)`) must NOT match the new builtin pattern.
    assert dispatch._EVAL_BUILTIN_WRITE.search(
        'mywrite("a", "' + prefix + marker + '")'
    ) is None


def test_eval_builtin_write_falls_through_to_pathless_bucket_on_unresolvable_path():
    """Security-review finding (round 5): unlike `Path(...)`/`open(...)`, whose dedicated
    patterns fall through to the path-less bare-write fallback whenever the path argument
    isn't a confidently-resolved literal, `_EVAL_BUILTIN_WRITE` required BOTH arguments to be a
    recognized string literal -- a computed/unresolvable first argument
    (`write(some_computed_path_var, secret)`) made the WHOLE call fail to match and silently
    dropped the second argument's content too, instead of scanning it path-lessly the way
    `Bun.write`/`fs.writeFile*` already do (round 3)."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "

    buckets = dispatch._eval_content_buckets(
        "write(some_computed_path_var, '" + prefix + secret + "')"
    )

    assert buckets[""] == prefix + secret


def test_omp_bridge_blocks_secret_write_via_eval_tool_builtin_write_unresolvable_path(tmp_path):
    """End-to-end security-review regression (round 5): `write(<computed>, <secret>)` -- an
    unresolvable, non-literal first argument to the Eval tool's own builtin `write(...)` --
    must still reach `block-secrets-write` via the path-less fallback rather than silently
    dropping the secret content."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "js", "code": "write(some_computed_path_var, '" + prefix + secret + "')"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_open_write_nested_call_in_mode_tail_does_not_hide_secret_behind_decoy_path():
    """Security-review finding (round 5): `_EVAL_OPEN_WRITE`'s permissive `,[^)]*` mode/tail let
    a NESTED call expression sitting in the tail position -- not a plain scalar mode/encoding
    argument -- get silently swallowed: `open("foo.example", open("src/secret.py",
    "w").write("API_KEY=..."))` matched the OUTER `open("foo.example", ...)` call, with the
    tail's `[^)]*` consuming up to the inner `open(...)` call's own closing paren and the
    pattern's `.write(` anchor landing on the NESTED call's `.write(` -- attributing the whole
    secret to the OUTER call's allowlist-friendly decoy path `foo.example` and never scanning
    it under `src/secret.py` (or path-lessly) at all. This is a genuine ALLOWLIST BYPASS, not
    merely an under-scan: `block-secrets-write` would `ALLOW_PATH`-exempt `foo.example` and
    never see the secret under any bucket. The fix must not let the secret vanish behind
    `foo.example` -- it must land under its real path (`src/secret.py`) or, failing that, a
    path-less bucket."""
    secret = "abcd1234abcd1234"
    code = 'open("foo.example", open("src/secret.py","w").write("API_KEY = ' + secret + '"))'

    buckets = dispatch._eval_content_buckets(code)

    assert "foo.example" not in buckets, buckets
    assert secret in "".join(buckets.values()), buckets


def test_omp_bridge_blocks_secret_write_via_eval_tool_nested_call_decoy_path(tmp_path):
    """End-to-end security-review regression (round 5): the nested-call-in-the-mode-tail
    allowlist bypass above must not let `block-secrets-write` `ALLOW_PATH`-exempt the write --
    a real secret nested inside a decoy `open("foo.example", ...)` call must still block."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    code = 'open("foo.example", open("src/secret.py","w").write("API_KEY = ' + secret + '"))'
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


# --- Round 6: further review-quorum hardening on agent-tools#614 (PR #621, round 6 pass) ---


def test_eval_kwarg_value_recognizes_call_expression_before_data_kwarg():
    """Security-review finding (round 6): `_EVAL_KWARG_VALUE` -- what `_EVAL_OTHER_KWARG`
    recognizes as ANOTHER keyword argument's value sitting before `data=` -- only matched a
    quoted literal, a bare identifier, or a number. A CALL EXPRESSION there
    (`write_text(encoding=locale.getpreferredencoding(), data="API_KEY=...")`) matched none of
    its alternatives, so `_EVAL_OTHER_KWARG` failed to consume that keyword pair at all, which
    made the whole `_EVAL_DATA_KWARG_PREFIX` fail to reach the (correctly-positioned) `data=`
    marker -- the call went entirely unrecognized (content silently dropped, `buckets == {}`),
    contradicting this module's own "`data=` recognized regardless of position" claim (round 3).
    A new alternative recognizing a call expression (with up to one level of nested parens,
    mirroring `_EVAL_ARG_SKIP`'s own tolerance) closes it."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    code = (
        "Path('src/secret.py').write_text(encoding=locale.getpreferredencoding(), data='"
        + prefix + secret + "')"
    )

    buckets = dispatch._eval_content_buckets(code)

    assert buckets == {"src/secret.py": prefix + secret}


def test_omp_bridge_blocks_secret_write_via_eval_tool_kwarg_call_expression_before_data(tmp_path):
    """End-to-end security-review regression (round 6): a call-expression-valued keyword
    argument (`encoding=locale.getpreferredencoding()`) sitting before `data=` must not make
    the whole `write_text(...)` call go unrecognized -- `block-secrets-write` must still see
    and block the secret."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    code = (
        "Path('src/secret.py').write_text(encoding=locale.getpreferredencoding(), data='"
        + prefix + secret + "')"
    )
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_writelines_list_does_not_truncate_at_bracket_inside_quoted_item():
    """Security-review finding (round 6): `_EVAL_OPEN_WRITELINES_LIST`'s old `[^\\]]*` content
    scan stopped at the FIRST `]`, including one sitting INSIDE a quoted list item's own string
    body -- `.writelines(["]", "API_KEY=..."])` truncated `content` to just the opening quote of
    the first item, silently dropping the real secret in the second item entirely. The new
    `_EVAL_WRITELINES_LIST_BODY` fragment treats each quoted chunk atomically so an embedded `]`
    inside quotes no longer ends the scan early."""
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    code = 'open("src/secret.py","w").writelines(["]", "' + prefix + secret + '"])'

    buckets = dispatch._eval_content_buckets(code)

    assert prefix + secret in buckets["src/secret.py"]


def test_omp_bridge_blocks_secret_write_via_eval_tool_writelines_bracket_in_quoted_item(tmp_path):
    """End-to-end security-review regression (round 6): a `]` embedded inside an earlier
    `.writelines([...])` list item's own quotes must not truncate the scan before it reaches a
    later item's real secret -- `block-secrets-write` must still see and block it."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    prefix = "API_KEY = "
    code = 'open("src/secret.py","w").writelines(["]", "' + prefix + secret + '"])'
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "js", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_unescape_decodes_python_eight_digit_unicode_escape():
    """Security-review finding (round 6): Python's 8-digit code-point escape (`\\UHHHHHHHH`,
    distinct from the JS `\\u{HEX}` form and the 4-digit `\\uHHHH` form) was not recognized as
    its own token by `_EVAL_ESCAPE_TOKEN` -- it fell through to the generic `\\.` catch-all,
    which consumes only the leading `\\U` (two characters), leaving the 8 hex digits behind as
    untouched literal text. `\\U00000041` (`chr(0x41)` == `"A"`) prepended to `PI_KEY = ...`
    must now decode to `API_KEY = ...` -- a real secret pattern (the `block-secrets-write`
    value class) the undecoded raw text does not contain at all."""
    raw = "\\U00000041PI_KEY = abcd1234abcd1234"

    decoded = dispatch._eval_unescape(raw)

    assert decoded == "API_KEY = abcd1234abcd1234"
    assert "API_KEY" not in raw
    assert "API_KEY" in decoded


def test_omp_bridge_blocks_secret_write_via_eval_tool_eight_digit_unicode_escape(tmp_path):
    """End-to-end security-review regression (round 6): a secret hidden behind a Python 8-digit
    `\\UHHHHHHHH` escape (`\\U00000041PI_KEY=...` decodes to `API_KEY=...`) must still reach
    `block-secrets-write` via the decoded content, not the raw undecoded text."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    code = "Path('src/secret.py').write_text('\\U00000041PI_KEY = " + secret + "')"
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_eval_open_mode_tail_rejects_subscript_call_decoy_not_just_bare_identifier_call():
    """Security-review finding (round 6): round 5's `_EVAL_OPEN_MODE_TAIL` guard only rejected
    a tail beginning with the SHAPE `identifier(` -- a decoy like `open("foo.example",
    [open][0]("src/secret.py","w").write("API_KEY=..."))` (subscript-then-call, `[open][0](...)`)
    begins with `[`, not an identifier character, so it didn't match that shape and the tail was
    NOT rejected: the outer match succeeded and the secret was misattributed to the allowlist-
    friendly decoy path `foo.example` again -- the identical bypass class as round 5's own
    finding, via a different call shape. The guard must now reject ANY `(`/`[` in the tail."""
    secret = "abcd1234abcd1234"
    code = 'open("foo.example", [open][0]("src/secret.py","w").write("API_KEY = ' + secret + '"))'

    buckets = dispatch._eval_content_buckets(code)

    assert "foo.example" not in buckets, buckets
    assert secret in "".join(buckets.values()), buckets


def test_omp_bridge_blocks_secret_write_via_eval_tool_subscript_call_decoy_path(tmp_path):
    """End-to-end security-review regression (round 6): the subscript-then-call
    (`[open][0](...)`) nested-call-in-the-mode-tail allowlist bypass above must not let
    `block-secrets-write` `ALLOW_PATH`-exempt the write -- a real secret nested inside a decoy
    `open("foo.example", ...)` call must still block."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    secret = "abcd1234abcd1234"
    code = 'open("foo.example", [open][0]("src/secret.py","w").write("API_KEY = ' + secret + '"))'
    event = {
        "event": "tool_call",
        "toolName": "eval",
        "input": {"language": "py", "code": code},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"

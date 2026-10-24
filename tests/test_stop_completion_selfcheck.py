"""Tests for the stop-completion-selfcheck agent-hook (stop, fail-OPEN completion nudge).

The hook blocks the agent's Stop exactly once per session, picking ONE of three prompt
variants from the turn's own transcript (best-effort; any read/parse trouble falls back to
FULL, the pre-existing static-text behavior):

  FULL  — no transcript_path, or the transcript is unreadable/unparseable, or the turn made
          at least one tool_use call. Unchanged from before this hook learned to read
          transcripts.
  LIGHT — a pure text-only turn (no tool_use), no hedge-and-defer phrase detected.
  HEDGE — a pure text-only turn whose reply matched a hedge pattern ("I can check...", or
          its Russian equivalent "могу поискать" / "I can search") — quotes the offending
          phrase back in the block message.

The marker-based single-block-then-allow loop-prevention is unchanged and re-tested here
since the prompt-selection logic sits inside the same blocking branch.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_stop_completion_selfcheck.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "stop-completion-selfcheck"
    / "stop_selfcheck.py"
)
_spec = importlib.util.spec_from_file_location("stop_selfcheck", _HOOK)
assert _spec and _spec.loader
selfcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selfcheck)


def _run(event: dict, monkeypatch) -> tuple[dict, int]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = selfcheck.main()
    return json.loads(out.getvalue()), code


@pytest.fixture(autouse=True)
def _isolated_marker_dir(tmp_path, monkeypatch):
    marker_dir = tmp_path / "selfcheck"
    monkeypatch.setattr(selfcheck, "MARKER_DIR", marker_dir)
    # FIRINGS_LOG is a module-level constant derived from MARKER_DIR at import time, not
    # re-derived when MARKER_DIR is patched — isolate it explicitly so no test writes into
    # (or reads a stale) real ~/.cache/agent-tools/selfcheck/firings.jsonl.
    monkeypatch.setattr(selfcheck, "FIRINGS_LOG", marker_dir / "firings.jsonl")


def _write_transcript(tmp_path, records: list[dict]) -> str:
    path = tmp_path / "transcript.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return str(path)


def _user_msg(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _assistant_tool_use(name: str = "Bash") -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": {}}]},
    }


def _tool_result_user() -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


# ---------------------------------------------------------------------------
# Loop prevention (pre-existing behavior, re-pinned)
# ---------------------------------------------------------------------------


def test_first_stop_blocks_second_stop_allows(monkeypatch):
    event = {"event_id": "sess-1"}
    out1, code1 = _run(event, monkeypatch)
    assert out1["decision"] == "block"
    assert code1 == selfcheck.BLOCK_EXIT_CODE

    out2, code2 = _run(event, monkeypatch)
    assert out2["decision"] == "allow"
    assert code2 == 0


# ---------------------------------------------------------------------------
# No transcript_path at all → FULL (unchanged pre-existing behavior)
# ---------------------------------------------------------------------------


def test_no_transcript_path_falls_back_to_full_prompt(monkeypatch):
    event = {"event_id": "sess-no-transcript"}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


def test_unreadable_transcript_path_falls_back_to_full_prompt(monkeypatch, tmp_path):
    event = {
        "event_id": "sess-missing-file",
        "args": {"transcript_path": str(tmp_path / "does-not-exist.jsonl")},
    }
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


# ---------------------------------------------------------------------------
# Tool calls happened this turn → FULL, even with transcript_path present
# ---------------------------------------------------------------------------


def test_turn_with_tool_use_gets_full_prompt(monkeypatch, tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("please check something"),
            _assistant_tool_use("Bash"),
            _tool_result_user(),
            _assistant_text("Done, here's what I found."),
        ],
    )
    event = {"event_id": "sess-tool-use", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


# ---------------------------------------------------------------------------
# Pure text turn, no hedge → LIGHT
# ---------------------------------------------------------------------------


def test_pure_text_turn_no_hedge_gets_light_prompt(monkeypatch, tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("what is 2+2?"),
            _assistant_text("It's 4."),
        ],
    )
    event = {"event_id": "sess-light", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.LIGHT_PROMPT


def test_a_completed_work_closer_is_not_a_hedge(monkeypatch, tmp_path):
    """Regression: `let me know if you('?d| would)? like` used to have an OPTIONAL
    'd/would group, so a normal closer on FINISHED work ("the summary is written to
    docs/x.md; let me know if you like it") false-positived into a HEDGE block accusing
    the agent of deferring instead of doing. The group must be mandatory."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("write a summary to docs/x.md"),
            _assistant_text("Done — the summary is written to docs/x.md. Let me know if you like it."),
        ],
    )
    event = {"event_id": "sess-closer-not-hedge", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.LIGHT_PROMPT


# ---------------------------------------------------------------------------
# Pure text turn with a hedge-and-defer phrase → HEDGE, quoting the offer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "I don't have exact data on this, but I can check the logs if you want.",
        "Точных данных нет, но могу поискать в интернете.",
        "Want me to look into the config for you?",
        "Should I investigate the error further?",
    ],
)
def test_hedge_and_defer_reply_gets_hedge_prompt(monkeypatch, tmp_path, reply):
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("why does this happen?"),
            _assistant_text(reply),
        ],
    )
    # A stable id, not hash(reply): hash() is PYTHONHASHSEED-randomized and unnecessary
    # here anyway — the autouse `_isolated_marker_dir` fixture already isolates markers
    # per parametrized call via `tmp_path`, so uniqueness doesn't depend on this id.
    event = {"event_id": f"sess-hedge-{reply[:12]!r}", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] != selfcheck.FULL_PROMPT
    assert out["message"] != selfcheck.LIGHT_PROMPT
    assert "do it now" in out["message"]
    # The prompt's most distinctive feature is quoting the actual offending phrase back —
    # assert on it directly (not just the boilerplate "do it now" above) so a regression
    # that empties the quote or breaks the slice in _select_prompt still fails a test.
    match = selfcheck._HEDGE_RE.search(reply)
    assert match is not None, "test setup sanity: `reply` must actually match the hedge regex"
    assert match.group() in out["message"]


def test_hedge_phrase_inside_a_tool_use_turn_still_gets_full_prompt(monkeypatch, tmp_path):
    """A hedge phrase only matters when NOTHING was actually done this turn — if a tool
    call also happened, treat it like any other worked turn (FULL), not a deferred one."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("why does this happen?"),
            _assistant_text("Let me check that for you."),
            _assistant_tool_use("Bash"),
            _tool_result_user(),
            _assistant_text("Found it, here's why."),
        ],
    )
    event = {"event_id": "sess-hedge-with-tool", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


# ---------------------------------------------------------------------------
# Transcript boundary: only the turn SINCE the last real user message counts
# ---------------------------------------------------------------------------


def test_only_current_turn_is_classified_not_earlier_turns(monkeypatch, tmp_path):
    """An earlier turn's tool_use must not leak FULL into a later, pure-text-only turn."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("first, do something"),
            _assistant_tool_use("Bash"),
            _tool_result_user(),
            _assistant_text("done with that"),
            _user_msg("now just tell me a fact"),
            _assistant_text("Here's the fact."),
        ],
    )
    event = {"event_id": "sess-turn-boundary", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.LIGHT_PROMPT


def test_no_user_boundary_within_the_scanned_window_falls_back_to_full(monkeypatch, tmp_path):
    """If the reverse scan runs off the front of the scanned window without ever finding
    the real user message that started the turn, we can't be sure we saw the WHOLE turn
    (it may genuinely be a long turn spanning more records than TRANSCRIPT_TAIL_LINES, or
    the byte-bounded read cut it off) — that's the same "no reliable signal" situation as
    an unreadable transcript, so it must fail closed to FULL, not default to LIGHT."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("this user message is outside the scanned window"),
            _assistant_tool_use("Bash"),  # would force FULL if seen — but it won't be
            _tool_result_user(),
            _assistant_text("Here's what I found."),
        ],
    )
    # Shrink the window so the scan only ever sees the last 2 records (never reaching the
    # user message above) — simulates a turn longer than TRANSCRIPT_TAIL_LINES.
    monkeypatch.setattr(selfcheck, "TRANSCRIPT_TAIL_LINES", 2)
    event = {"event_id": "sess-no-boundary-in-window", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


def test_synthetic_tool_result_user_record_does_not_end_the_turn(monkeypatch, tmp_path):
    """A tool_result is wrapped as a role:"user" message by CC/Anthropic's API — it must
    not be mistaken for a genuine new human message that would truncate the turn scan."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("investigate this"),
            _assistant_tool_use("Bash"),
            _tool_result_user(),
            _assistant_tool_use("Read"),
            _tool_result_user(),
            _assistant_text("Here's what I found."),
        ],
    )
    event = {"event_id": "sess-synthetic-boundary", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


def test_user_record_mixing_tool_result_and_text_does_not_end_the_turn(monkeypatch, tmp_path):
    """The Anthropic message shape permits a user record with BOTH a tool_result block
    AND a text block (e.g. injected system-reminder-style text riding alongside a real
    tool result, without CC setting isMeta). A tool_result sibling must still mark it as
    turn-continuation regardless of the accompanying text — otherwise the scan would stop
    there, never see the earlier tool_use, and misclassify a worked turn as LIGHT."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("investigate this"),
            _assistant_tool_use("Bash"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": "ok"},
                        {"type": "text", "text": "<system-reminder>some injected note</system-reminder>"},
                    ],
                },
            },
            _assistant_text("Here's what I found."),
        ],
    )
    event = {"event_id": "sess-mixed-tool-result-text", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


def test_transcript_path_also_read_from_top_level_event(monkeypatch, tmp_path):
    """cc_hook_bridge forwards it top-level (parallel to `command`/`cwd`), not under
    `args` — the hook must accept either shape."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("what is 2+2?"),
            _assistant_text("It's 4."),
        ],
    )
    event = {"event_id": "sess-top-level-path", "transcript_path": transcript}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.LIGHT_PROMPT


# ---------------------------------------------------------------------------
# Cooldown fix (agent-tools#529): the marker is NOT consumed on allow, so a burst of
# stops within the TTL window is capped at exactly one block, not one per stop.
# ---------------------------------------------------------------------------


def test_repeated_stops_within_ttl_block_only_once(monkeypatch):
    """Regression for the observed real-world pattern: a long-lived watch/poll loop ends
    many turns in a row with a Stop, sometimes under a minute apart. Before agent-tools#529
    this re-blocked on almost every single one because the marker was deleted on allow;
    now it must block once and then allow every subsequent stop until the TTL expires."""
    event = {"event_id": "sess-burst"}
    out1, code1 = _run(event, monkeypatch)
    assert out1["decision"] == "block" and code1 == selfcheck.BLOCK_EXIT_CODE

    for _ in range(5):
        out, code = _run(event, monkeypatch)
        assert out["decision"] == "allow" and code == 0


def test_marker_file_survives_an_allowed_stop(monkeypatch):
    """The marker must still exist on disk after an allow — that's what makes the burst
    test above work; asserted directly so a future change can't silently reintroduce the
    consume-on-allow bug even if it happens to pass the burst test by luck."""
    event = {"event_id": "sess-marker-survives"}
    _run(event, monkeypatch)  # first stop: block, writes the marker
    marker = selfcheck.marker_file(selfcheck.session_id(event))
    assert marker.exists()
    _run(event, monkeypatch)  # second stop: allow
    assert marker.exists(), "marker must not be deleted on allow (agent-tools#529)"


def test_stop_after_ttl_expiry_blocks_again(monkeypatch):
    event = {"event_id": "sess-ttl-expiry"}
    _run(event, monkeypatch)  # block, writes marker
    marker = selfcheck.marker_file(selfcheck.session_id(event))
    # Backdate the marker past the TTL to simulate the cooldown window elapsing.
    old = time.time() - selfcheck.MARKER_TTL_S - 1
    os.utime(marker, (old, old))
    out, code = _run(event, monkeypatch)
    assert out["decision"] == "block" and code == selfcheck.BLOCK_EXIT_CODE


@pytest.mark.parametrize("raw_ttl", ["0", "-5", "-1"])
def test_ttl_zero_or_negative_env_is_clamped_not_forever_blocking(monkeypatch, raw_ttl):
    """`fresh()` treats a marker as fresh when `age <= MARKER_TTL_S`. An unclamped
    `SELFCHECK_TTL_S=0` (or a negative value) would mean NO marker is ever fresh, so every
    single stop re-blocks forever with no way out short of the kill switch — a genuine
    foot-gun, since `0` reads like "no cooldown" but means the opposite.

    `MARKER_TTL_S` is computed once at MODULE IMPORT time from the real env var, so
    pinning this requires actually re-importing the module with the env patched — a plain
    `monkeypatch.setattr(selfcheck, "MARKER_TTL_S", 0)` would bypass the clamp entirely and
    prove nothing about it."""
    monkeypatch.setenv("SELFCHECK_TTL_S", raw_ttl)
    spec = importlib.util.spec_from_file_location("stop_selfcheck_ttl_reload", _HOOK)
    assert spec and spec.loader
    reloaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reloaded)
    assert reloaded.MARKER_TTL_S >= 1, "SELFCHECK_TTL_S<=0 must be clamped, not read literally"


# ---------------------------------------------------------------------------
# Kill switch (agent-tools#529)
# ---------------------------------------------------------------------------


def test_disable_env_var_always_allows_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(selfcheck, "DISABLE_ENV", "1")
    event = {"event_id": "sess-killswitch-env"}
    out, code = _run(event, monkeypatch)
    assert out["decision"] == "allow" and code == 0
    marker = selfcheck.marker_file(selfcheck.session_id(event))
    assert not marker.exists()
    assert not selfcheck.FIRINGS_LOG.exists()


def test_disabled_sentinel_file_always_allows(monkeypatch, tmp_path):
    (selfcheck.MARKER_DIR).mkdir(parents=True, exist_ok=True)
    (selfcheck.MARKER_DIR / "DISABLED").write_text("")
    event = {"event_id": "sess-killswitch-file"}
    out, code = _run(event, monkeypatch)
    assert out["decision"] == "allow" and code == 0
    # Symmetric with the env-var kill switch test above: no marker, no log row — the
    # DISABLED file itself is expected to exist (we just created it), everything else
    # this invocation would otherwise write must not.
    marker = selfcheck.marker_file(selfcheck.session_id(event))
    assert not marker.exists()
    assert not selfcheck.FIRINGS_LOG.exists()


# ---------------------------------------------------------------------------
# Usefulness logging (agent-tools#529)
# ---------------------------------------------------------------------------


def test_firings_log_records_block_then_allow(monkeypatch, tmp_path):
    log_path = tmp_path / "firings.jsonl"
    monkeypatch.setattr(selfcheck, "FIRINGS_LOG", log_path)
    event = {"event_id": "sess-log"}
    _run(event, monkeypatch)
    _run(event, monkeypatch)
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["decision"] == "block"
    assert rows[0]["prompt_variant"] == "full"
    assert rows[1]["decision"] == "allow"
    assert rows[1]["prompt_variant"] is None
    for row in rows:
        assert row["session"] == selfcheck.session_id(event)
        assert isinstance(row["hook_ms"], (int, float))


def test_firings_log_records_the_harness_the_stop_came_from(monkeypatch, tmp_path):
    """agent-tools#1003: the omp main session silently had no Stop hook for weeks because a
    firing row did not say which harness produced it; the harness is now part of the row."""
    log_path = tmp_path / "firings.jsonl"
    monkeypatch.setattr(selfcheck, "FIRINGS_LOG", log_path)
    _run({"event_id": "sess-omp", "harness": "omp"}, monkeypatch)
    _run({"event_id": "sess-unknown"}, monkeypatch)
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert rows[0]["harness"] == "omp"
    assert rows[1]["harness"] is None


def test_firings_log_write_failure_does_not_break_the_hook(monkeypatch, tmp_path):
    """Logging is best-effort: an unwritable log path must not change the decision.

    Scoped to ONLY the firings-log write (a file sitting where the log's parent
    directory needs to be, so `.parent.mkdir()` fails with a plain OSError) — must not
    also break the unrelated marker-directory write main() does first.
    """
    blocker = tmp_path / "blocker-not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(selfcheck, "FIRINGS_LOG", blocker / "firings.jsonl")
    event = {"event_id": "sess-log-fail"}
    out, code = _run(event, monkeypatch)
    assert out["decision"] == "block" and code == selfcheck.BLOCK_EXIT_CODE


# ---------------------------------------------------------------------------
# Marker sweep (agent-tools#529): removing consume-on-allow meant something else has to
# garbage-collect old markers, or every session that ever stops leaves a file behind
# forever.
# ---------------------------------------------------------------------------


def test_stale_markers_are_swept_on_the_next_block(monkeypatch):
    # Build the markers via marker_file(), not a hand-picked filename: _sweep_stale_markers
    # globs "*.done" (matching marker_file()'s own suffix). Hardcoding ".done" here would
    # pass even if the two silently drifted apart — the exact regression this sweep exists
    # to prevent (a naming mismatch would mean it never removes a REAL marker, and
    # MARKER_DIR would grow forever with the test still green).
    selfcheck.MARKER_DIR.mkdir(parents=True, exist_ok=True)
    stale = selfcheck.marker_file(selfcheck.session_id({"event_id": "some-other-old-session"}))
    stale.write_text("old")
    old = time.time() - selfcheck.MARKER_TTL_S - 1
    os.utime(stale, (old, old))

    fresh_marker = selfcheck.marker_file(selfcheck.session_id({"event_id": "some-other-fresh-session"}))
    fresh_marker.write_text("fresh")

    _run({"event_id": "sess-triggers-sweep"}, monkeypatch)  # a block runs the sweep

    assert not stale.exists(), "a marker older than the TTL must be swept on the next block"
    assert fresh_marker.exists(), "a marker still within the TTL must survive the sweep"


# ---------------------------------------------------------------------------
# Real CC transcript shapes the earlier tests didn't cover (review finding: string-form
# user content is the COMMON real shape and had zero coverage; isMeta/isSidechain records
# must not be mistaken for a real user turn boundary; a malformed trailing line is
# expected, not fatal).
# ---------------------------------------------------------------------------


def test_string_form_user_and_assistant_content_is_classified_correctly(monkeypatch, tmp_path):
    """Real CC/Anthropic-API messages commonly carry `content` as a plain string, not the
    list-of-blocks form every other test in this file uses — that's the production path
    for `_message_content`'s `isinstance(content, str)` branches and had no coverage."""
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "what is 2+2?"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "It's 4."}},
        ],
    )
    event = {"event_id": "sess-string-content", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.LIGHT_PROMPT


def test_meta_record_between_tool_use_and_reply_does_not_hide_the_tool_use(monkeypatch, tmp_path):
    """A record CC marks isMeta (e.g. a slash-command wrapper) sitting between an earlier
    tool call and this turn's text-only reply must not be mistaken for the real user-turn
    boundary — if it were, the reverse scan would stop there and never see the tool_use
    earlier in the same turn, wrongly classifying a worked turn as LIGHT."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("investigate this"),
            _assistant_tool_use("Bash"),
            _tool_result_user(),
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": [{"type": "text", "text": "<command-name>/compact</command-name>"}]},
            },
            _assistant_text("Here's what I found."),
        ],
    )
    event = {"event_id": "sess-meta-mid-turn", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


def test_sidechain_record_does_not_end_the_turn(monkeypatch, tmp_path):
    """isSidechain marks subagent traffic riding in the same transcript file — also not a
    real user-turn boundary."""
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("investigate this"),
            _assistant_tool_use("Bash"),
            _tool_result_user(),
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": [{"type": "text", "text": "subagent chatter"}]},
            },
            _assistant_text("Here's what I found."),
        ],
    )
    event = {"event_id": "sess-sidechain-mid-turn", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


def test_malformed_trailing_line_is_skipped_not_fatal(monkeypatch, tmp_path):
    """`_read_tail_records`'s docstring promises a partially-written last line (the
    transcript can be mid-append when Stop fires) is skipped, not fatal — pin it."""
    transcript_path = tmp_path / "transcript.jsonl"
    with open(transcript_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_user_msg("what is 2+2?")) + "\n")
        fh.write(json.dumps(_assistant_text("It's 4.")) + "\n")
        fh.write('{"type": "assistant", "message": {"role": "assistant", "conte')  # truncated, no trailing newline
    event = {"event_id": "sess-truncated-line", "args": {"transcript_path": str(transcript_path)}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.LIGHT_PROMPT


def test_tail_lines_zero_disables_scanning_instead_of_reading_everything(monkeypatch, tmp_path):
    """`SELFCHECK_TRANSCRIPT_TAIL_LINES=0` must mean "scan nothing" (this hook's own
    convention: 0 means the minimum/disabled state, as SELFCHECK_TTL_S=0 does for the
    cooldown) — NOT `list[-0:]`'s silent "the whole file", which would be the opposite of
    an operator trying to shrink or disable the scan. With nothing scanned, classification
    fails closed to FULL, same as a missing/unreadable transcript."""
    monkeypatch.setattr(selfcheck, "TRANSCRIPT_TAIL_LINES", 0)
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("what is 2+2?"),
            _assistant_text("It's 4."),
        ],
    )
    event = {"event_id": "sess-tail-zero", "args": {"transcript_path": transcript}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT


def test_read_tail_records_is_byte_bounded_not_whole_file(monkeypatch, tmp_path):
    """`_read_tail_records` must read a BOUNDED window from the end of the file, not the
    whole file — regression for a naive `deque(fh, maxlen=N)`/`readlines()[-n:]` approach,
    both of which still iterate and decode every byte of a huge transcript just to keep a
    small tail. Pin it by using a byte budget far smaller than the file and confirming the
    correct (tail) records still come back."""
    # Small enough to force a real seek (the file is ~40KB), generous enough to comfortably
    # hold the final two real lines (~207 bytes together) plus part of a padding line, so
    # a boundary off-by-one can't accidentally drop a needed line via the leading-partial-
    # line trim.
    monkeypatch.setattr(selfcheck, "TAIL_READ_BYTES", 400)
    transcript_path = tmp_path / "transcript.jsonl"
    with open(transcript_path, "w", encoding="utf-8") as fh:
        for i in range(200):
            fh.write(json.dumps(_assistant_text(f"padding line number {i} " * 5)) + "\n")
        fh.write(json.dumps(_user_msg("what is 2+2?")) + "\n")
        fh.write(json.dumps(_assistant_text("It's 4.")) + "\n")

    size = transcript_path.stat().st_size
    assert size > 200 * 10, "test setup sanity: the file must be much bigger than the byte budget"

    records = selfcheck._read_tail_records(str(transcript_path), 500)
    assert records, "a byte-bounded read of a real tail must still return something"
    # The last two real records (the user question and its answer) must be present and in
    # order — the tail read reached far enough back despite the tiny byte budget clipping
    # most of the padding lines before them.
    assert records[-2]["message"]["content"][0]["text"] == "what is 2+2?"
    assert records[-1]["message"]["content"][0]["text"] == "It's 4."


def test_nel_byte_inside_a_json_string_does_not_split_the_record(monkeypatch, tmp_path):
    """Regression: splitting on `str.splitlines()` breaks on NEL (U+0085)/U+2028/U+2029 in
    addition to `\\n`/`\\r` — all legal INSIDE a JSON string value (JSON only requires
    escaping control chars below U+0020, and CC's own JS-side JSON.stringify does not
    escape NEL/U+2028/U+2029 either). A tool_result embedding a raw NEL byte (common in
    cp1252-derived text) would otherwise split one JSON line into two fragments that both
    fail to parse, silently dropping the record — if it was the turn's only tool_use, a
    worked turn would be misclassified LIGHT/HEDGE instead of FULL.

    Written with ``ensure_ascii=False`` (unlike `_write_transcript`'s default) so the NEL
    character lands in the file LITERALLY, the same way CC's Node-based JSON.stringify
    would write it — Python's default `json.dumps(ensure_ascii=True)` would instead escape
    it to the harmless ASCII text ``\\u0085``, which doesn't reproduce the bug at all.
    """
    records = [
        _user_msg("investigate this"),
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"text": "line one\x85line two"}},
                ],
            },
        },
        _tool_result_user(),
        _assistant_text("Here's what I found."),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    with open(transcript_path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    event = {"event_id": "sess-nel-byte", "args": {"transcript_path": str(transcript_path)}}
    out, code = _run(event, monkeypatch)
    assert code == selfcheck.BLOCK_EXIT_CODE
    assert out["message"] == selfcheck.FULL_PROMPT

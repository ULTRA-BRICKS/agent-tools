"""Tests for the decision-request-format agent-hook (pre-bash, send-time escalation-format gate).

Graduated verdict: a genuinely BARE escalation (`tg --tag decision|problem|question` whose body
has no table and none of Context/Options/Recommendation) is BLOCKED (exit 10, the send is
stopped); a PARTIAL body (some structure) gets an exit-0 advisory nudge; a COMPLETE body sends
silently. A pros/cons table (markdown OR HTML) always passes — the lenient-true-positive
guarantee that protects the human's only comms channel from a false block.

Covers: TRIGGER detection (parsed not raw-matched, through wrappers and pipelines), the
bare→block / partial→advise / complete→silent grading, the table-always-passes leniency,
NON-TRIGGER cases (other tag / no tag / non-tg / flag-inside-a-string), the DEAD self-service
escape hatch (`ALLOW_RAW_DECISION_REQUEST` / `# decision-request-ok:`), the external Telegram
hatch (`RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT`) forcing a bare body through, and FAIL-OPEN on
a malformed event (a crash never wedges a send).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_decision_request_format.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shlex
import sys
from pathlib import Path

import pytest


def _q(text: str) -> str:
    """Shell-quote a body (may contain newlines, `|`, quotes) so it survives as one `tg` argv."""
    return shlex.quote(text)

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "decision-request-format"
    / "decision_request_format.py"
)
_spec = importlib.util.spec_from_file_location("decision_request_format", _HOOK)
assert _spec and _spec.loader
drf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drf)


def _run(command, monkeypatch, *, env: dict | None = None, raw_event=None, cwd=None):
    if raw_event is not None:
        event = raw_event
    else:
        event = {"args": {"command": command}}
        if cwd is not None:
            event["cwd"] = str(cwd)
    payload = event if isinstance(event, str) else json.dumps(event)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("ALLOW_RAW_DECISION_REQUEST", raising=False)
    monkeypatch.delenv("RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = drf.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _message(out: str) -> str | None:
    return json.loads(out).get("message")


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the real hookSpecificOutput shape the helper parses
# (`decision.behavior == "allow"`) — printing arbitrary text and exiting 0 no longer approves.
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


# A body that mentions none of the three dimensions and has no table — the bare "A or B?".
# This is the ONLY class that BLOCKS.
_BARE = "should we go with A or B?"
# A body that hits all three markers (context + options/pros-cons + recommendation).
_COMPLETE = (
    "Context: the loader in app.py:42. Options: keep sync (simple, blocks the UI) vs "
    "async (faster, more complex). Recommendation: go async."
)
# A structured escalation whose Options dimension is a markdown pros/cons TABLE — must PASS.
_MD_TABLE = (
    "Context: the loader in app.py:42.\n"
    "| Option | Pros | Cons |\n"
    "| --- | --- | --- |\n"
    "| sync | simple | blocks UI |\n"
    "| async | fast | complex |\n"
    "Recommendation: go async."
)
# An HTML table with NONE of the marker keywords — so the test proves `_has_table` (HTML) alone
# prevents a block; without it this body (3 markers missing, no table) would be bare → blocked.
_HTML_TABLE = (
    "<table><tr><th>keep</th><th>drop</th></tr>"
    "<tr><td>fast but risky</td><td>slow but safe</td></tr></table>"
)
# A PARTIAL body: some structure (one marker) but not complete — must ADVISE, never block.
_PARTIAL = "Recommendation: I'd go with A."


def _assert_blocked(out: str, code: int) -> str:
    assert code == drf.BLOCK_EXIT_CODE, f"expected BLOCK exit {drf.BLOCK_EXIT_CODE}, got {code}"
    assert _decision(out) == "block"
    msg = _message(out)
    assert msg is not None
    return msg


# ── a genuinely BARE escalation → BLOCK (through every wrapper / pipeline form) ────────────

@pytest.mark.parametrize("command", [
    f'tg --tag decision "{_BARE}"',
    f'tg --tag=decision "{_BARE}"',
    f'tg --tag decision --title "{_BARE}" "more body"',
    f'env TG_AI_MODEL=claude tg --tag decision "{_BARE}"',
    f'TG_AI_MODEL=claude tg --tag decision "{_BARE}"',   # bare assignment (skill's OWN form)
    f'A=1 B=2 tg --tag decision "{_BARE}"',              # several bare assignments
    f'TG_AI_MODEL=claude timeout 30 tg --tag decision "{_BARE}"',  # assignment + wrapper
    f'timeout 30 tg --tag decision "{_BARE}"',
    f'timeout -s KILL 30 tg --tag decision "{_BARE}"',   # wrapper flag w/ separate value
    f'nice -n 10 tg --tag decision "{_BARE}"',           # nice -n with a separate value
    f'env -u FOO tg --tag decision "{_BARE}"',           # env -u FOO (flag + its value)
    f'build_msg | tg --tag decision "{_BARE}"',
    f'/usr/local/bin/tg --tag decision "{_BARE}"',
])
def test_bare_body_is_blocked(command, monkeypatch):
    out, _, code = _run(command, monkeypatch)
    msg = _assert_blocked(out, code)
    # The block message must be actionable: name the skill and show the pros/cons table skeleton.
    assert "decision-request-discipline" in msg
    assert "pros/cons table" in msg


# ── a bare escalation whose body carries a shell OPERATOR inside its quotes → still BLOCK ──
# The command splitter must be quote-aware. A raw split on `|` / `&&` / `;` tore the quoted body
# apart (`"A | B?"` → `"A ` + ` B?"`), leaving an unbalanced segment that failed to tokenize, so
# the escalation went unseen and the #12 bare-question block was bypassed for the common
# "A vs B" wording. (Codex P2 on PR #281.)
@pytest.mark.parametrize("command", [
    'tg --tag question "should we go with A | B?"',          # single pipe inside the body
    'tg --tag decision "keep sync || go async?"',            # `||` inside the body
    'tg --tag problem "run migrate && deploy, or roll back?"',  # `&&` inside the body
    'tg --tag question "do A; then B?"',                     # `;` inside the body
    'TG_AI_MODEL=claude tg --tag decision "ship A | ship B?"',  # assignment + piped body
    'echo x | tg --tag question "pick A | B?"',              # real pipeline AND a piped body
])
def test_bare_body_with_quoted_operator_is_blocked(command, monkeypatch):
    out, _, code = _run(command, monkeypatch)
    msg = _assert_blocked(out, code)
    assert "decision-request-discipline" in msg


# The advisory message names the missing markers as a `missing <A>, <B> (of the three…` list,
# then goes on to a static reminder. A test that just asserts `missing_label in msg` is vacuous
# — "Context"/"Options"/"Recommendation" all appear somewhere in the static text regardless of
# what was detected. We assert against the DYNAMIC list only (the part between "missing " and
# the "(of the three" qualifier): the present markers must NOT appear there, the absent one must.
_MISSING_LIST = re.compile(r"missing ([^(]+)\(of the three")


def _named_missing(msg: str) -> set[str]:
    m = _MISSING_LIST.search(msg)
    assert m, f"advisory did not contain a `missing …` list: {msg!r}"
    return {part.strip() for part in m.group(1).split(",")}


@pytest.mark.parametrize("body,absent,present", [
    ("Options: A (fast) vs B (safe). Recommendation: A.", "Context", ("Options", "Recommendation")),
    ("Context: app.py. Recommendation: pick A.", "Options", ("Context", "Recommendation")),
    ("Context: app.py. Options: A (fast) vs B (safe).", "Recommendation", ("Context", "Options")),
])
def test_each_missing_marker_named(body, absent, present, monkeypatch):
    out, _, _ = _run(f'tg --tag decision "{body}"', monkeypatch)
    msg = _message(out)
    assert msg is not None
    named = _named_missing(msg)
    assert absent in named, f"the absent marker {absent!r} should be listed; got {named}"
    for marker in present:
        assert marker not in named, f"the present marker {marker!r} must not be listed; got {named}"


def test_missing_markers_unit_is_dynamic():
    """Directly exercise the detector so the marker logic is verified, not just the message."""
    assert drf._missing_markers("nothing here") == ["Context", "Options", "Recommendation"]
    assert drf._missing_markers(_COMPLETE) == []
    assert drf._missing_markers("Options: A vs B. Recommendation: A.") == ["Context"]


@pytest.mark.parametrize("body", [
    "| Option | Pros | Cons |\n| --- | --- | --- |\n| A | x | y |",  # markdown table
    "| a | b |\n| c | d |",                                          # two piped rows, no delim
    "<table><tr><td>A</td></tr></table>",                            # HTML table
    "<TABLE>",                                                       # HTML, case-insensitive
    "col1 | col2\n:--- | :---\nx | y",                               # delimiter row, aligned
])
def test_has_table_detects_real_tables(body):
    """Unit-level proof of the table detector, independent of the command-splitting boundary
    (a markdown table full of `|` is torn by the raw command split, so `_has_table` is the
    only place its detection is verified). Over-detection is the SAFE direction."""
    assert drf._has_table(body) is True


@pytest.mark.parametrize("body", [
    "should we go with A or B?",              # bare prose
    "Context: app.py:42. Recommendation: A.",  # structure but no table
    "a single | pipe in prose",               # one pipe, one line → not a table
    "",                                        # empty
])
def test_has_table_rejects_non_tables(body):
    assert drf._has_table(body) is False


def test_is_bare_only_when_no_structure_at_all():
    """`_is_bare` — the ONLY block trigger — is True solely for a body with no table AND all
    three markers missing. A table, or ANY one marker, makes it non-bare (→ never blocks)."""
    bare = "should we go with A or B?"
    assert drf._is_bare(bare, drf._missing_markers(bare)) is True
    # any single marker → not bare
    partial = "Recommendation: A."
    assert drf._is_bare(partial, drf._missing_markers(partial)) is False
    # a markdown table with none of the marker keywords → not bare (table satisfies structure)
    table = "| x | y |\n| --- | --- |\n| 1 | 2 |"
    assert drf._is_bare(table, drf._missing_markers(table)) is False
    # an HTML table → not bare
    html = "<table><tr><td>a</td></tr></table>"
    assert drf._is_bare(html, drf._missing_markers(html)) is False


@pytest.mark.parametrize("ref", ["app.py:42", "src/loader.ts:128"])
def test_file_line_ref_counts_as_context(ref):
    # The skill's format point 1 prescribes a `file:line` code ref AS the Context. A body that
    # gives a ref instead of the literal word "context" must NOT be flagged missing Context.
    assert "Context" not in drf._missing_markers(f"{ref} — Options: A vs B. Recommendation: A.")


@pytest.mark.parametrize("nonref", ["localhost:8080", "12:30", "a:5"])
def test_bare_host_port_or_time_is_not_context(nonref):
    # A bare `host:port` or `HH:MM` must NOT be mistaken for a file ref — otherwise the most
    # valuable marker (Context) gets silenced for a body that has no real context. The `.ext`
    # requirement in the regex is what makes this hold.
    assert "Context" in drf._missing_markers(f"{nonref} — should we pick A or B?")


def test_second_tg_in_pipeline_is_inspected(monkeypatch):
    # The FIRST `tg` segment is a non-decision send (--tag report) → returns None; the loop must
    # continue to the SECOND `tg --tag decision` and inspect IT. This is the only non-trivial
    # segment-walking branch — without the continuation, the decision request would be missed.
    out, _, _ = _run(
        f'tg --tag report "build green" ; tg --tag decision "{_BARE}"', monkeypatch
    )
    assert _message(out) is not None


def test_empty_body_with_decision_tag_is_blocked(monkeypatch):
    # `tg --tag decision` with no positional text and no --title → body == "" → bare → BLOCK
    # (no crash on the empty string).
    out, _, code = _run('tg --tag decision', monkeypatch)
    _assert_blocked(out, code)


# ── #12: problem / question are the SAME escalation shape → each is GATED ──────────────────

@pytest.mark.parametrize("tag", ["decision", "problem", "question"])
def test_all_escalation_tags_block_a_bare_body(tag, monkeypatch):
    """decision/problem/question all route a structured escalation to the human, so a bare body
    (no structure) is BLOCKED for EACH — an agent can't dodge the gate by picking `problem`/
    `question` instead of `decision` (agent-tools#213/#12)."""
    out, _, code = _run(f'tg --tag {tag} "{_BARE}"', monkeypatch)
    msg = _assert_blocked(out, code)
    # The block message names the actual tag used, not a hardcoded "decision".
    assert f"--tag {tag}" in msg


@pytest.mark.parametrize("tag", ["decision", "problem", "question"])
def test_all_escalation_tags_complete_body_silent(tag, monkeypatch):
    out, _, code = _run(f'tg --tag {tag} "{_COMPLETE}"', monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert _message(out) is None


# ── LENIENCY: a structured escalation must always PASS (the false-block guard) ─────────────

@pytest.mark.parametrize("tag", ["decision", "problem", "question"])
def test_markdown_table_body_passes_silently(tag, monkeypatch):
    """A pros/cons markdown table + context + recommendation is a complete escalation → send
    silently. The human's channel must NEVER be blocked for a properly-formatted message."""
    out, _, code = _run(f"tg --tag {tag} {_q(_MD_TABLE)}", monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert _message(out) is None


@pytest.mark.parametrize("tag", ["decision", "problem", "question"])
def test_html_table_body_is_never_blocked(tag, monkeypatch):
    """A rich HTML `<table>` escalation must not be blocked — table detection covers HTML too."""
    out, _, code = _run(f"tg --tag {tag} {_q(_HTML_TABLE)}", monkeypatch)
    assert code != drf.BLOCK_EXIT_CODE and _decision(out) == "allow"


def test_partial_body_advises_but_never_blocks(monkeypatch):
    """A body with SOME structure (one marker, here a Recommendation) is not bare → it gets a
    non-blocking advisory nudge and still sends. Only a genuinely-bare body blocks (err toward
    passing when ambiguous)."""
    out, _, code = _run(f"tg --tag decision {_q(_PARTIAL)}", monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    msg = _message(out)
    assert msg is not None and "self-check" in msg


def test_partial_body_never_contacts_tg_ctl(tmp_path, monkeypatch):
    """`_decide` short-circuits the hatch for a non-bare body, so a PARTIAL body must never open a
    Telegram round-trip — the human is only ever asked to approve a genuinely-BARE force-through,
    never a non-blocking nudge. A marker-writing fake tg-ctl that is never invoked proves it."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(drf.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _, code = _run(f"tg --tag decision {_q(_PARTIAL)}", monkeypatch, cwd=tmp_path,
                        env={_HATCH_ENV: "should not be consulted for a partial body"})
    assert code == 0 and _decision(out) == "allow"
    assert not marker.exists()


def test_table_alone_satisfies_options_and_passes(monkeypatch):
    """A table whose cells contain none of the marker keywords must pass — a table IS the
    Options/pros-cons structure. It gets a nudge only for the OTHER dimensions (Context /
    Recommendation) and must NOT be told to add a pros/cons table it already has."""
    body = "| left | right |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    assert "Options" not in drf._missing_markers(body)  # the table satisfies Options
    out, _, code = _run(f"tg --tag question {_q(body)}", monkeypatch)
    assert code != drf.BLOCK_EXIT_CODE and _decision(out) == "allow"


def test_html_table_with_context_and_recommendation_is_complete_silent(monkeypatch):
    """Context + an HTML pros/cons table (= Options) + Recommendation is a COMPLETE escalation →
    silent allow. Proves the table counts as Options at the send-path level (HTML has no `|`, so
    it is not torn by the command splitter and exercises the full decision path)."""
    body = ("Context: app.py:42. <table><tr><td>keep</td><td>drop</td></tr></table> "
            "Recommendation: keep.")
    out, _, code = _run(f"tg --tag decision {_q(body)}", monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert _message(out) is None


def test_block_message_points_at_pros_cons_table_and_skill(monkeypatch):
    """#12: the block message must steer the agent to the escalation format so it can immediately
    re-send correctly — read the skill, send a pros/cons table + context + recommendation."""
    out, _, code = _run(f'tg --tag problem "{_BARE}"', monkeypatch)
    msg = _assert_blocked(out, code)
    assert "decision-request-discipline" in msg
    assert "pros/cons table" in msg
    assert "Recommendation" in msg and "Context" in msg


# ── TRIGGER + complete body → silent allow ──────────────────────────────────────────────

def test_complete_body_allows_silently(monkeypatch):
    out, _, code = _run(f'tg --tag decision "{_COMPLETE}"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    assert _message(out) is None


# ── NON-TRIGGER → plain allow, no message ───────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    'tg --tag report "build green"',                       # a non-escalation tag
    'tg --tag answer "yes, done"',                          # a non-escalation tag
    'tg "just a status note"',                              # no tag at all
    'echo "use --tag decision next time"',                 # the flag inside an echo string
    'tg "remember to --tag decision later"',               # body text, no actual flag
    'git commit -m "add tg --tag decision hook"',          # the flag inside a commit message
    'ls -la /opt/tg/decision',                             # a path that merely contains tg
])
def test_non_trigger_plain_allow(command, monkeypatch):
    out, _, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    assert _message(out) is None


def test_value_flag_value_not_treated_as_body(monkeypatch):
    # A value-flag's VALUE must not be collected as the message body. Here the ONLY text that
    # would satisfy markers sits in --reply-to's value; the real body is a bare "A or B?". With
    # --tag decision present, the advisory must STILL fire (proving the value was skipped, not
    # mistaken for the body that has the markers). A vacuous version without --tag decision
    # would return None regardless of value-flag handling.
    out, _, _ = _run(
        'tg --tag decision --reply-to "Context: Options: Recommendation:" "A or B?"', monkeypatch
    )
    msg = _message(out)
    assert msg is not None  # the markers in --reply-to's value did NOT silence the advisory


def test_value_flag_before_real_body_with_decision_tag(monkeypatch):
    # `--photo x.png` value skipped; the real positional body carries all markers → silent allow.
    out, _, _ = _run(
        'tg --tag decision --photo /tmp/x.png '
        '"Context: app.py. Options: A vs B. Recommendation: A."', monkeypatch
    )
    assert _message(out) is None


def test_title_value_participates_in_marker_check(monkeypatch):
    # The body is assembled from --title PLUS positionals, so markers split across the two must
    # all count. Here Context lives in --title and Options/Recommendation in the positional →
    # a complete body → silent allow. (A naive impl that ignored --title would flag Context.)
    out, _, _ = _run(
        'tg --tag decision --title "Context: app.py:42" "Options: A vs B. Recommendation: A."',
        monkeypatch,
    )
    assert _message(out) is None
    # And the inverse: markers ONLY in --title, a bare positional → still complete via --title.
    out2, _, _ = _run(
        'tg --tag decision --title '
        '"Context: app.py. Options: A vs B. Recommendation: A." "ping"',
        monkeypatch,
    )
    assert _message(out2) is None


# ── REGRESSION: the OLD self-service silence is DEAD ──────────────────────────────────────

def test_env_override_no_longer_bypasses_block(monkeypatch):
    """REGRESSION: `ALLOW_RAW_DECISION_REQUEST=1` was a self-service silence — it must NOT bypass
    the block; a bare body is still BLOCKED despite it."""
    out, _, code = _run(
        f'tg --tag decision "{_BARE}"', monkeypatch,
        env={"ALLOW_RAW_DECISION_REQUEST": "1"},
    )
    _assert_blocked(out, code)


def test_inline_sentinel_no_longer_bypasses_block(monkeypatch):
    """REGRESSION: a `# decision-request-ok: <reason>` inline sentinel no longer bypasses."""
    out, _, code = _run(
        f'tg --tag decision "{_BARE}"  # decision-request-ok: terse follow-up', monkeypatch
    )
    _assert_blocked(out, code)


# ── external Telegram hatch: force a BARE body through with a written justification ────────
_HATCH_ENV = "RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT"


def test_hatch_unset_still_blocks(tmp_path, monkeypatch):
    out, _, code = _run(f'tg --tag decision "{_BARE}"', monkeypatch, cwd=tmp_path)
    _assert_blocked(out, code)


def test_hatch_bare_flag_still_blocks_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` is an invalid request: denied WITHOUT contacting Telegram → the block stands."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")  # would allow if called
    monkeypatch.setattr(drf.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _, code = _run(f'tg --tag decision "{_BARE}"', monkeypatch, cwd=tmp_path,
                        env={_HATCH_ENV: "1"})
    _assert_blocked(out, code)
    assert not marker.exists()  # no Telegram round-trip for a bare flag


def test_hatch_justification_forces_bare_through(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 0 (the human approved) forces the bare send THROUGH
    → allow (exit 0), and the tg-ctl round-trip actually happened."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(drf.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _, code = _run(f'tg --tag decision "{_BARE}"', monkeypatch, cwd=tmp_path,
                        env={_HATCH_ENV: "Terse follow-up to an already-detailed thread."})
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_justification_denied_still_blocks(tmp_path, monkeypatch):
    """A written justification but tg-ctl exit 1 (the human declined / timed out) → the bare send
    stays BLOCKED."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(drf.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _, code = _run(f'tg --tag decision "{_BARE}"', monkeypatch, cwd=tmp_path,
                        env={_HATCH_ENV: "Terse follow-up to an already-detailed thread."})
    _assert_blocked(out, code)


# ── FAIL-OPEN ───────────────────────────────────────────────────────────────────────────

def test_malformed_event_allows(monkeypatch):
    out, err, code = _run(None, monkeypatch, raw_event="not json{")
    assert code == 0
    assert _decision(out) == "allow"
    assert "fail-open" in err


def test_unparseable_command_allows(monkeypatch):
    # An unbalanced quote in a `tg --tag decision` segment → shlex raises → that segment is
    # skipped → plain allow (fail-open at the parse level, no crash).
    out, _, code = _run('tg --tag decision "unterminated', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── CLOSED BOUNDARY: a pipe/semicolon char INSIDE the quoted body ────────────────────────
# The command is now split QUOTE-AWARELY (`_split_top_level_segments`), so a separator inside a
# quoted body (`"use A | B?"`) no longer tears the segment mid-quote. Once the #12 hard-block
# exists, the old miss was not merely a lost advisory — a bare escalation that used a pipe to
# separate the options slipped past the block entirely. These bare bodies now BLOCK like any
# other bare escalation (Codex P2 on PR #281). The general fix is pinned by
# `test_bare_body_with_quoted_operator_is_blocked`; this keeps the historical commands explicit.

@pytest.mark.parametrize("command", [
    'tg --tag decision "should we use A | B?"',     # `|` inside the quoted body
    'tg --tag decision "do X; then Y?"',            # `;` inside the quoted body
])
def test_separator_inside_quoted_body_is_now_seen_and_blocked(command, monkeypatch):
    out, _, code = _run(command, monkeypatch)
    _assert_blocked(out, code)

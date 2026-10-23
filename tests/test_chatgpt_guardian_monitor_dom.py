"""Monitor and live DOM scripts against sanitized shapes of the actual ChatGPT page.

The fixtures contain structure only: no real conversation text, ids or account data.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CHAT = "https://chatgpt.com/c/" + ID
# The exact two-letter Guardian message, spelled with escapes to keep this file ASCII.
GO = "\u0433\u043e"


def turn(key, state, user="Task", assistant="markdown", started=True, failed=False):
    """One wrapper holds the user bubble and the assistant side, as on the page."""
    body = {
        "markdown": '<div role="group" aria-label="Assistant" data-markdown-copy>Answer</div>',
        "subagent": '<section data-testid="chatgpt-subagent-activity">Agents</section>',
        "legacy": '<div data-message-author-role="assistant">Answer</div>',
        "empty": '<div data-message-author-role="assistant"></div>',
        None: "",
    }[assistant]
    # `True` is the legacy activity header; "canvas" is the current collapsed
    # thinking button, which has no div.group/activity-header at all.
    header = {
        False: "",
        True: '<div class="group/activity-header"><span>Thinking failed</span></div>',
        "canvas": '<button type="button" aria-expanded="false"><svg></svg><span>Thinking failed</span></button>',
    }[failed]
    marker = "<span hidden data-chatgpt-agent-turn-start></span>" if started else ""
    return (
        f'<div data-turn-key="{key}"><div data-content-search-turn-key="k">{body}'
        f'<div><div data-talvt-turn-state="{state}">{header}'
        f'<div data-chatgpt-search-unit-key="fallback-turn-0:0:user" data-chatgpt-search-message-ids="{key}">'
        f'<div><div data-user-message-bubble="true">{user}</div></div></div>{marker}'
        "</div></div></div></div>"
    )


T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
T3 = "33333333-3333-4333-8333-333333333333"
RUNNING_TURNS = (
    turn(T1, "complete")
    + turn(T2, "complete", assistant="subagent")
    + turn(T3, "in_progress")
)


def root(value=ID, inner=RUNNING_TURNS, style=""):
    return (
        f'<div data-chatgpt-conversation-selection-target="{value}" '
        f'data-thread-find-target="conversation" style="{style}">{inner}</div>'
    )


COMPOSER = '<div contenteditable="true" data-composer-markdown role="textbox" aria-label="Ask ChatGPT"><p><br></p></div>'
CHROME = (
    '<button aria-label="Open profile menu">Account</button>'
    '<button aria-label="Select ChatGPT model" data-codex-intelligence-trigger="true" '
    'data-selected-reasoning-effort="medium">Pro</button>'
)


def page(main_html, stop=True, extra=""):
    stop_button = '<button aria-label="Stop">Stop</button>' if stop else ""
    return (
        f'<meta charset="utf-8"><title>Task</title>{CHROME}'
        f"<main>{main_html}{COMPOSER}{stop_button}</main>{extra}"
    )


FAILED_THEN = turn(T1, "cancelled", assistant=None, failed=True)


def go_case(response):
    return page(root(inner=FAILED_THEN + response), stop=False)


CASES = {
    "uuid-root": page(root()),
    "legacy-root": page(
        root(
            "true",
            turn(T1, "complete", assistant="legacy", started=False)
            + turn(T2, "complete", assistant="legacy", started=False),
        ),
        stop=False,
    ),
    "mismatch": page(root(OTHER_ID)),
    "duplicate": page(root() + root()),
    "legacy-plus-other-id": page(root("true") + root(OTHER_ID)),
    "floating-main-root": page(root(), extra=f"<main>{root(OTHER_ID, '')}</main>"),
    "no-root": page("<div>No conversation</div>"),
    "hidden-root": page(root(style="display:none")),
    # The current page keeps a hidden clone of the right conversation next to the live one.
    "hidden-clone-then-visible": page(
        root(inner=turn(T1, "complete"), style="display:none") + root()
    ),
    "hidden-wrong-id-sibling": page(
        root(OTHER_ID, inner="", style="display:none") + root()
    ),
    "aria-hidden-ancestor-clone": page(
        '<div aria-hidden="true">'
        + root(inner=turn(T1, "complete"))
        + "</div>"
        + root()
    ),
    "two-visible-one-matching": page(root(ID) + root("true")),
    "two-visible-wrong-and-right": page(root(OTHER_ID) + root(ID)),
    "all-hidden": page(root(style="display:none") + root(style="visibility:hidden")),
    "project-hidden-clone": page(root(style="display:none") + root()),
    "floating-composer-and-stop": page(
        root(inner=turn(T1, "complete")),
        stop=False,
        extra=f'<main>{COMPOSER}<button aria-label="Stop">Stop</button></main>',
    ),
    "go-completed": go_case(turn(T2, "complete", user=GO)),
    "go-running-with-assistant": go_case(turn(T2, "in_progress", user=GO)),
    "go-in-progress-without-assistant": go_case(
        turn(T2, "in_progress", user=GO, assistant=None, started=False)
    ),
    "go-not-yet-rendered": go_case(""),
    "other-message-after-failure": go_case(turn(T2, "complete", user="Another task")),
    "empty-legacy-assistant-generating": go_case(
        turn(T2, "in_progress", user=GO, assistant="empty", started=False)
    ),
}


def canvas_page(latest, older="", stop=False, extra=""):
    return page(root(inner=older + latest), stop=stop, extra=extra)


# A markdown body quoting the status text, even inside a collapsed button, is message content.
QUOTED = (
    "<p><span>Thinking failed</span></p>"
    '<button type="button" aria-expanded="false"><span>Thinking failed</span></button>'
)
CANVAS_LATEST = turn(T2, "cancelled", assistant=None, failed="canvas")
QUOTING_OLDER = turn(T1, "complete").replace("Answer", QUOTED)


def dialog(body, attrs='role="dialog"'):
    return f"<div {attrs}>{body}</div>"


ACTIVITY_BODY = (
    '<h2>Activity</h2><div role="tablist"><button role="tab">Subagents</button>'
    '<button role="tab">Scheduled</button><button role="tab">Outputs</button></div>'
)
COUNTER_ACTIVITY_BODY = (
    '<h2>Activity</h2><div role="tablist"><button role="tab">Subagents3 used</button>'
    '<button role="tab">Scheduled</button><button role="tab">Outputs</button></div>'
)
FAILED_CANVAS = canvas_page(CANVAS_LATEST, QUOTING_OLDER)


def with_dialog(body, **kw):
    return canvas_page(CANVAS_LATEST, QUOTING_OLDER, extra=dialog(body, **kw))


CASES.update(
    {
        "canvas-failed": FAILED_CANVAS,
        "legacy-failed-latest": canvas_page(
            turn(T2, "cancelled", assistant=None, failed=True)
        ),
        "canvas-older-only": canvas_page(
            turn(T3, "complete"), turn(T2, "cancelled", assistant=None, failed="canvas")
        ),
        "canvas-quoted-only": canvas_page(
            turn(T2, "cancelled", assistant="markdown").replace("Answer", QUOTED)
        ),
        "canvas-expanded": canvas_page(
            CANVAS_LATEST.replace('aria-expanded="false"', 'aria-expanded="maybe"')
        ),
        "canvas-expanded-true": canvas_page(
            CANVAS_LATEST.replace('aria-expanded="false"', 'aria-expanded="true"')
        ),
        "canvas-expanded-true-in-subagent": canvas_page(
            turn(T2, "cancelled", assistant="subagent").replace(
                "Agents",
                '<button aria-expanded="true"><span>Thinking failed</span></button>',
            )
        ),
        "canvas-expanded-true-running": canvas_page(
            CANVAS_LATEST.replace('aria-expanded="false"', 'aria-expanded="true"'),
            stop=True,
        ),
        "activity-dialog-counter": with_dialog(COUNTER_ACTIVITY_BODY),
        "activity-dialog-counter-captcha": with_dialog(
            COUNTER_ACTIVITY_BODY
            + '<iframe src="https://challenges.cloudflare.com/x" width="40" height="40"></iframe>'
        ),
        "activity-heading-only": with_dialog("<h2>Activity</h2>"),
        "activity-counter-only": with_dialog(
            "<h2>Activity</h2><button>Subagents3 used</button>"
        ),
        "random-counter-dialog": with_dialog(
            "<h2>Rename chat</h2><button>Subagents3 used</button>"
            "<button>Scheduled</button><button>Outputs</button>"
        ),
        "counter-non-numeric": with_dialog(
            "<h2>Activity</h2><button>Subagentsx used</button>"
            "<button>Scheduled</button><button>Outputs</button>"
        ),
        "canvas-hidden": canvas_page(
            CANVAS_LATEST.replace('type="button"', 'type="button" hidden')
        ),
        "canvas-in-user-bubble": canvas_page(
            turn(
                T2,
                "cancelled",
                assistant=None,
                user=(
                    '<button type="button" aria-expanded="false"><span>Thinking failed</span></button>'
                ),
            )
        ),
        "canvas-duplicate-status": canvas_page(
            CANVAS_LATEST.replace(
                "</button>",
                '</button><button type="button" aria-expanded="false"><span>Thinking failed</span></button>',
            )
        ),
        "canvas-not-cancelled": canvas_page(
            turn(T2, "unknown-state", assistant=None, failed="canvas")
        ),
        "canvas-substring-label": canvas_page(
            CANVAS_LATEST.replace(
                "<span>Thinking failed</span>", "<span>Thinking failed twice</span>"
            )
        ),
        "canvas-duplicate-turn-key": canvas_page(CANVAS_LATEST, turn(T2, "complete")),
        "activity-dialog": with_dialog(ACTIVITY_BODY),
        "activity-dialog-with-captcha": with_dialog(
            ACTIVITY_BODY
            + '<iframe src="https://challenges.cloudflare.com/x" width="40" height="40"></iframe>'
        ),
        "activity-dialog-with-password": with_dialog(
            ACTIVITY_BODY + '<input type="password" aria-label="Password">'
        ),
        "login-dialog": with_dialog(
            '<h2>Log in or sign up</h2><input type="email" aria-label="Email address">'
            "<button>Continue</button>"
        ),
        "verify-human-dialog": with_dialog(
            "<h2>Verify you are human</h2><button>Verify</button>"
        ),
        "unknown-dialog": with_dialog('<h2>Rename chat</h2><input aria-label="Name">'),
        "activity-lookalike-dialog": with_dialog(
            "<h2>Activity</h2><button>Subagents</button><button>Scheduled</button>"
        ),
        "hidden-dialog": with_dialog(
            "<h2>Rename chat</h2>", attrs='role="dialog" style="display:none"'
        ),
        "login-link": canvas_page(
            CANVAS_LATEST, QUOTING_OLDER, extra='<a href="/auth/login">Log in</a>'
        ),
        "signed-out": canvas_page(CANVAS_LATEST, QUOTING_OLDER).replace(
            '<button aria-label="Open profile menu">Account</button>', ""
        ),
        "signed-out-auth-ui": canvas_page(
            CANVAS_LATEST,
            QUOTING_OLDER,
            extra='<a href="/auth/login">Log in</a>',
        ).replace('<button aria-label="Open profile menu">Account</button>', ""),
    }
)
# Pages whose conversation root must be accepted by both scripts.
ACCEPTED = {
    "uuid-root",
    "legacy-root",
    "hidden-clone-then-visible",
    "hidden-wrong-id-sibling",
    "aria-hidden-ancestor-clone",
    "project-hidden-clone",
}
URLS = {
    "project-hidden-clone": "https://chatgpt.com/g/g-p-" + "a" * 32 + "-proj/c/" + ID
}
ROOT_REFUSALS = {
    "mismatch": "root-attribute-mismatch",
    "duplicate": "root-ambiguous",
    "legacy-plus-other-id": "root-ambiguous",
    "floating-main-root": "root-ambiguous",
    "no-root": "root-missing",
    "hidden-root": "root-not-visible",
    "two-visible-one-matching": "root-ambiguous",
    "two-visible-wrong-and-right": "root-ambiguous",
    "all-hidden": "root-not-visible",
}


async def evaluate_all():
    from agenttools_chatgpt.live_browser import LiveAdapter
    from agenttools_chatgpt.monitor_observer import DOM_SCRIPT
    from playwright.async_api import async_playwright

    results = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=shutil.which("google-chrome")
        )
        try:
            context = await browser.new_context()
            for name, html in CASES.items():
                page_ = await context.new_page()

                def fulfill(route, request, body=html):
                    return route.fulfill(body=body, content_type="text/html")

                await page_.route("**/*", fulfill)
                await page_.goto(URLS.get(name, CHAT))
                live = await page_.evaluate(LiveAdapter(page_).script, {})
                results[name] = {
                    "monitor": await page_.evaluate(DOM_SCRIPT),
                    "live": live,
                }
                await page_.close()
        finally:
            await browser.close()
    return results


@pytest.fixture(scope="module")
def results():
    pytest.importorskip("playwright.async_api")
    if not shutil.which("google-chrome"):
        pytest.skip("Local fixture Chrome required")
    return asyncio.run(evaluate_all())


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_conversation_id_and_legacy_roots_are_accepted(results, name):
    monitor = results[name]["monitor"]
    assert "error" not in monitor
    assert monitor["conversation_id"] == ID
    assert monitor["root_match"] == (
        "legacy" if name == "legacy-root" else "conversation-id"
    )
    assert results[name]["live"]["turn"], "live recovery DOM must verify the same root"


@pytest.mark.parametrize("name,detail", sorted(ROOT_REFUSALS.items()))
def test_unverified_roots_are_refused_with_specific_detail(results, name, detail):
    monitor = results[name]["monitor"]
    assert monitor["error"] == "conversation-root-unverified"
    assert monitor["error_detail"] == detail
    assert "turns" not in monitor
    assert results[name]["live"]["turn"] == ""
    assert results[name]["live"]["ambiguous"] is True


def test_actual_running_shape_is_read_without_author_role_markers(results):
    monitor = results["uuid-root"]["monitor"]
    assert [t["assistant_state"] for t in monitor["turns"]] == [
        "complete",
        "complete",
        "in_progress",
    ]
    # Markdown, subagent activity and the agent-turn marker identify the assistant side.
    assert [t["assistant_present"] for t in monitor["turns"]] == [True, True, True]
    assert all(t["assistant_started"] for t in monitor["turns"])
    assert not any(t["user_go"] for t in monitor["turns"])
    assert monitor["running"] is True and monitor["status"] == "working"
    assert monitor["stop_visible"] is True


def test_legacy_assistant_shape_remains_supported(results):
    turns = results["legacy-root"]["monitor"]["turns"]
    assert [t["assistant_present"] for t in turns] == [True, True]
    assert [t["assistant_started"] for t in turns] == [True, True]


def test_stop_button_outside_verified_conversation_is_not_running(results):
    monitor = results["floating-composer-and-stop"]["monitor"]
    assert "error" not in monitor
    assert monitor["running"] is False and monitor["stop_visible"] is False
    # The secondary composer makes recovery refuse instead of guessing a target.
    assert results["floating-composer-and-stop"]["live"]["turn"] == ""


@pytest.mark.parametrize(
    "name,expected",
    [
        ("go-completed", "completed"),
        ("go-running-with-assistant", "running"),
        ("go-in-progress-without-assistant", "unconfirmed"),
        ("go-not-yet-rendered", "unconfirmed"),
        ("other-message-after-failure", "unconfirmed"),
        ("empty-legacy-assistant-generating", "running"),
    ],
)
def test_real_page_turns_correlate_failure_go_and_next_assistant(
    results, name, expected
):
    from agenttools_chatgpt.monitor import project_snapshot

    now = 1792254000.0
    sample = dict(results[name]["monitor"], chat=CHAT, checked_at=now)
    attempt = {
        "chat": CHAT,
        "turn": T1,
        "generation": T1,
        "wall": now - 30,
        "state": "sent",
    }
    row = project_snapshot(
        "glide.vc",
        {"watch:" + CHAT: "1"},
        [attempt],
        [sample],
        None,
        {"connection": "connected", "heartbeat_at": now - 5},
        now,
    )["sessions"][0]
    assert row["continuation"] == expected


@pytest.mark.parametrize(
    "name,shape",
    [
        ("canvas-failed", "collapsed-button"),
        ("canvas-expanded-true", "expanded-button"),
        ("legacy-failed-latest", "activity-header"),
    ],
)
def test_current_and_legacy_failure_shapes_are_detected_by_both_scripts(
    results, name, shape
):
    monitor, live = results[name]["monitor"], results[name]["live"]
    assert monitor["status"] == "Thinking failed"
    assert monitor["failure_evidence"] == shape
    assert live["status"] == "Thinking failed"
    assert live["active"] is True and live["ambiguous"] is False
    assert live["challenge"] is False
    assert "failure:" + shape in live["evidence"].split(";")


@pytest.mark.parametrize(
    "name",
    [
        "canvas-older-only",
        "canvas-quoted-only",
        "canvas-expanded",
        "canvas-expanded-true-in-subagent",
        "canvas-hidden",
        "canvas-in-user-bubble",
        "canvas-substring-label",
        "canvas-not-cancelled",
        "canvas-duplicate-turn-key",
        "canvas-duplicate-status",
    ],
)
def test_failure_is_only_the_exact_newest_unique_cancelled_turn_status(results, name):
    monitor, live = results[name]["monitor"], results[name]["live"]
    assert monitor["status"] != "Thinking failed"
    assert not (live["status"] == "Thinking failed" and live["active"])


def test_two_status_nodes_are_ambiguous_with_diagnostic_evidence(results):
    monitor, live = (
        results["canvas-duplicate-status"]["monitor"],
        results["canvas-duplicate-status"]["live"],
    )
    assert monitor["failure_evidence"] == "ambiguous-status"
    assert live["ambiguous"] is True
    assert "failure:ambiguous-status" in live["evidence"].split(";")


def test_non_cancelled_turn_reports_why_it_is_not_a_failure(results):
    assert (
        results["canvas-not-cancelled"]["monitor"]["failure_evidence"]
        == "not-cancelled"
    )


def test_monitor_reports_failure_while_unresolved_send_stays_the_recovery_blocker(
    results,
):
    from agenttools_chatgpt.monitor import project_snapshot

    now = 1792254000.0
    sample = dict(results["canvas-failed"]["monitor"], chat=CHAT, checked_at=now)
    attempt = {
        "chat": CHAT,
        "turn": T1,
        "generation": T1,
        "wall": now - 30,
        "state": "uncertain",
    }
    snapshot = project_snapshot(
        "glide.vc",
        {"watch:" + CHAT: "1"},
        [attempt],
        [sample],
        None,
        {"connection": "connected", "heartbeat_at": now - 5},
        now,
    )
    row = snapshot["sessions"][0]
    assert row["status"] == "Thinking failed"
    assert row["reason"] == "unresolved-send"
    assert row["failure_evidence"] == "collapsed-button"
    assert [
        e["kind"] for e in snapshot["events"] if e["kind"] == "thinking-failed"
    ] == ["thinking-failed"]


def test_benign_activity_dialog_is_not_a_challenge_and_does_not_block_recovery(results):
    live = results["activity-dialog"]["live"]
    assert live["challenge"] is False and live["ambiguous"] is False
    assert live["status"] == "Thinking failed" and live["active"] is True
    assert "dialog:activity" in live["evidence"].split(";")


def test_counter_bearing_activity_dialog_is_benign(results):
    live = results["activity-dialog-counter"]["live"]
    assert live["challenge"] is False and live["ambiguous"] is False
    assert live["status"] == "Thinking failed" and live["active"] is True
    assert "dialog:activity" in live["evidence"].split(";")


def test_missing_profile_menu_alone_is_unknown_identity_not_a_challenge(results):
    live = results["signed-out"]["live"]
    assert live["challenge"] is False
    assert live["ambiguous"] is True
    assert "identity:unknown" in live["evidence"].split(";")
    assert not any(e.startswith("challenge:") for e in live["evidence"].split(";"))


def test_expanded_failure_button_keeps_every_other_gate(results):
    live = results["canvas-expanded-true"]["live"]
    assert live["active"] is True and live["ambiguous"] is False
    assert results["canvas-expanded-true"]["monitor"]["status"] == "Thinking failed"
    running = results["canvas-expanded-true-running"]
    assert running["monitor"]["status"] != "Thinking failed"
    assert running["live"]["running"] is True


@pytest.mark.parametrize(
    "name,evidence",
    [
        ("activity-dialog-with-captcha", "challenge:captcha-frame"),
        ("activity-dialog-with-password", "challenge:auth-dialog"),
        ("login-dialog", "challenge:auth-dialog"),
        ("verify-human-dialog", "challenge:auth-dialog"),
        ("login-link", "challenge:login-link"),
        ("signed-out-auth-ui", "challenge:login-link"),
        ("activity-dialog-counter-captcha", "challenge:captcha-frame"),
    ],
)
def test_positive_login_or_captcha_evidence_is_still_a_challenge(
    results, name, evidence
):
    live = results[name]["live"]
    assert live["challenge"] is True
    assert evidence in live["evidence"].split(";")


@pytest.mark.parametrize(
    "name",
    [
        "unknown-dialog",
        "activity-lookalike-dialog",
        "activity-heading-only",
        "activity-counter-only",
        "random-counter-dialog",
        "counter-non-numeric",
    ],
)
def test_unknown_blocking_modal_prevents_sends_without_claiming_a_challenge(
    results, name
):
    live = results[name]["live"]
    assert live["challenge"] is False
    assert live["ambiguous"] is True
    assert "dialog:unknown" in live["evidence"].split(";")


def test_hidden_dialog_is_ignored(results):
    live = results["hidden-dialog"]["live"]
    assert live["challenge"] is False and live["ambiguous"] is False


@pytest.mark.parametrize(
    "name,latched",
    [
        ("activity-dialog", False),
        ("activity-dialog-counter", False),
        ("unknown-dialog", False),
        ("signed-out", False),
        ("login-dialog", True),
        ("signed-out-auth-ui", True),
        ("activity-dialog-counter-captcha", True),
        ("activity-dialog-with-captcha", True),
    ],
)
def test_only_challenges_latch_a_durable_human_stop(results, tmp_path, name, latched):
    from agenttools_chatgpt.core import Guardian, Store
    from agenttools_chatgpt.live_browser import LiveAdapter

    observed = LiveAdapter._parse_observation(results[name]["live"])
    store = Store(tmp_path / "state.sqlite", account="glide.vc")
    Guardian(store, apply=True).record_safety(observed)
    controls = store.controls()
    assert ("stop:" + observed.chat in controls) is latched
    if latched:
        assert controls["stop:" + observed.chat] == "human challenge"

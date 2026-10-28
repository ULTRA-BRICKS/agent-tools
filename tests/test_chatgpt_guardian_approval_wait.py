"""A CURRENT native platform approval card must not pin the single-tab queue (Guardian 0.3.16).

Two layers, both read-only and synthetic (no live account, no transcript, no click on the card):

* real DOM: the observed approval structure (a `data-codex-approval-surface` div holding a form
  with permission buttons, inside the latest `in_progress` turn, no Stop, no draft) served to the
  real `live_dom.js` in an isolated Chromium. Every refusal case is the same page with ONE thing
  changed, so a red result names the guard that failed.
* pure fakes: the guards on `Observation`, the observation parser and the fair A/B/C queue of the
  single-tab patrol. The patrol fakes raise on any page access beyond navigation, so a click,
  accept, deny or send would fail loudly.
"""

import asyncio
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import approval, live, rotation
from agenttools_chatgpt.approval import ApprovalObservation
from agenttools_chatgpt.continuation import observation_refusal
from agenttools_chatgpt.core import Observation
from guardian_patrol_fakes import CHATS, URLS, WALL0, advance_to_due, attempts, make_rig

PROJECT = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OLD_TURN = "11111111-1111-4111-8111-111111111111"
NEW_TURN = "22222222-2222-4222-8222-222222222222"
REASONING = "SECRET-REASONING-TEXT-NEVER-STORED"

CARD = (
    '<div data-codex-approval-surface="true"><form onsubmit="return false">'
    "<p>Allow this tool call?</p>"
    '<button type="button">Allow once</button><button type="button">Always allow</button>'
    '<button type="button">Deny</button></form></div>'
)
THINKING = f'<div data-talvt-turn-state="in_progress"><span>{REASONING}</span></div>'
ANSWER = (
    '<div data-talvt-turn-state="complete"></div><div data-markdown-copy>Done</div>'
)
COMPOSER = (
    '<div contenteditable="true" data-composer-markdown role="textbox" '
    'aria-label="Ask ChatGPT"><p data-empty-paragraph="true"><br></p></div>'
)


def turn(key, inner):
    return f'<div data-turn-key="{key}">{inner}</div>'


def page_html(*turns, before="", after="", composer=COMPOSER, profile=True):
    menu = '<button aria-label="Open profile menu">Account</button>' if profile else ""
    return (
        '<meta charset="utf-8"><title>ChatGPT fixture</title>'
        + menu
        + before
        + '<main><div data-chatgpt-conversation-selection-target="true" '
        'data-thread-find-target="conversation">'
        + "".join(turns)
        + "</div></main>"
        + composer
        + after
    )


def pending(card=CARD, **extra):
    return page_html(
        turn(OLD_TURN, '<div data-user-message-bubble="true">go</div>'),
        turn(NEW_TURN, THINKING + card),
        **extra,
    )


def run_dom(case, html):
    pytest.importorskip("playwright.async_api")

    async def scenario():
        from agenttools_chatgpt.live_browser import LiveAdapter
        from playwright.async_api import Error, async_playwright

        async with async_playwright() as pw:
            chrome = shutil.which("google-chrome")
            try:
                browser = await pw.chromium.launch(
                    headless=True, **({"executable_path": chrome} if chrome else {})
                )
            except Error as exc:
                pytest.skip("no isolated Chromium available: " + str(exc)[:80])
            try:
                page = await (await browser.new_context()).new_page()
                await page.route(
                    "**/*",
                    lambda route: route.fulfill(body=html, content_type="text/html"),
                )
                await page.goto(PROJECT)
                await case(page, LiveAdapter(page))
            finally:
                await browser.close()

    asyncio.run(scenario())


def observe(html):
    seen = {}

    async def case(page, adapter):
        seen["o"] = await adapter.observe()

    run_dom(case, html)
    return seen["o"]


def assert_card_blocks(o):
    assert o.running is False
    assert approval.flagged(o) and approval.pending(o)
    assert o.turn_state == "in_progress"  # the raw state stays recorded
    assert o.turn == NEW_TURN and o.chat and not o.ambiguous
    assert (o.status, o.active, o.draft, o.challenge, o.quota) == (
        "",
        False,
        False,
        False,
        False,
    )


def assert_protected(o):
    assert o.running is True
    assert not approval.flagged(o) and not approval.pending(o)


def passive(html):
    """What `pm monitor` records for this page: the real passive metadata script."""
    seen = {}

    async def case(page, adapter):
        from agenttools_chatgpt import scripts

        seen["s"] = await page.evaluate(scripts.load("monitor_dom.js"))

    run_dom(case, html)
    return seen["s"]


def assert_passive_working(s):
    assert s["running"] is True and s["status"] == "working"
    assert "approval_wait" not in s


# --- real DOM: when a card counts ---------------------------------------------------------------


def test_the_current_native_card_without_stop_or_draft_is_permission_blocked():
    o = observe(pending())
    assert_card_blocks(o)
    assert not o.safe_failure() and not o.operator_candidate()
    assert not o.operator_ready() and not live.structural_failure(o)


def test_no_error_status_and_no_false_complete_for_a_pending_card():
    o = observe(pending())
    assert o.status == "" and o.turn_state != "complete" and not o.active
    assert observation_refusal(o.chat, o.turn, o) != ""


def test_a_card_of_the_previous_turn_never_blocks_the_new_answer():
    html = page_html(
        turn(OLD_TURN, THINKING.replace("in_progress", "complete") + CARD),
        turn(NEW_TURN, THINKING),
    )
    assert_protected(observe(html))


def test_a_card_from_an_older_turn_beside_a_complete_answer_is_a_plain_complete_turn():
    o = observe(page_html(turn(OLD_TURN, THINKING + CARD), turn(NEW_TURN, ANSWER)))
    assert o.running is False and not approval.flagged(o)
    assert o.turn_state == "complete" and o.turn == NEW_TURN


@pytest.mark.parametrize(
    "wrapper",
    [
        '<div data-user-message-bubble="true">{}</div>',
        "<blockquote>{}</blockquote>",
        '<div data-markdown-copy="true">{}</div>',
        '<div data-message-author-role="user">{}</div>',
        "<pre><code>{}</code></pre>",
    ],
)
def test_a_lookalike_card_in_a_bubble_or_quote_is_not_counted(wrapper):
    assert_protected(observe(pending(card=wrapper.format(CARD))))


def test_a_lookalike_does_not_make_the_one_real_card_ambiguous():
    quote = f"<blockquote>{CARD}</blockquote>"
    assert_card_blocks(observe(pending(card=CARD + quote)))


@pytest.mark.parametrize(
    "hidden",
    [
        f'<div style="display:none">{CARD}</div>',
        f"<div hidden>{CARD}</div>",
        f'<div aria-hidden="true">{CARD}</div>',
        f'<div style="visibility:hidden">{CARD}</div>',
    ],
)
def test_hidden_responsive_copies_are_not_counted_and_do_not_break_uniqueness(hidden):
    assert_card_blocks(observe(pending(card=CARD + hidden)))
    assert_protected(observe(pending(card=hidden)))


def test_two_visible_cards_are_ambiguous_and_stay_protected():
    assert_protected(observe(pending(card=CARD + CARD)))


def test_a_visible_card_in_an_older_turn_plus_the_current_one_is_ambiguous():
    html = page_html(
        turn(OLD_TURN, THINKING.replace("in_progress", "complete") + CARD),
        turn(NEW_TURN, THINKING + CARD),
    )
    assert_protected(observe(html))


def test_a_card_without_a_form_with_a_visible_button_is_not_a_native_card():
    bare = '<div data-codex-approval-surface="true"><p>Allow?</p></div>'
    assert_protected(observe(pending(card=bare)))
    empty = '<div data-codex-approval-surface="true"><form></form></div>'
    assert_protected(observe(pending(card=empty)))


def test_a_visible_stop_wins_and_the_card_never_releases_a_generator():
    html = pending(after='<button aria-label="Stop">Stop</button>')
    assert_protected(observe(html))


def test_a_draft_wins():
    draft = COMPOSER.replace(
        "<p data-empty-paragraph", "<p>half a thought</p><p data-empty-paragraph"
    )
    o = observe(pending(composer=draft))
    assert o.draft is True and not approval.flagged(o)


def test_a_challenge_wins():
    captcha = '<iframe src="https://challenges.cloudflare.com/x" width="40" height="40"></iframe>'
    o = observe(pending(after=captcha))
    assert o.challenge is True and not approval.flagged(o)


def test_a_login_link_or_auth_dialog_wins():
    link = '<a href="/auth/login">Log in</a>'
    o = observe(pending(after=link))
    assert o.challenge is True and not approval.flagged(o)
    dialog = '<div role="dialog"><h2>Log in</h2><input type="password"></div>'
    o = observe(pending(after=dialog))
    assert o.challenge is True and not approval.flagged(o)


def test_a_quota_alert_wins():
    alert = '<div role="alert">GPT-6 usage limit reached</div>'
    o = observe(pending(after=alert))
    assert o.quota is True and not approval.flagged(o)
    sol = '<div role="alert">GPT-5.6 Sol quota exhausted</div>'
    o = observe(pending(after=sol))
    assert o.status == "Sol quota exhausted" and not approval.flagged(o)
    assert o.running is True


def test_an_unknown_dialog_or_missing_identity_is_uncertain_and_wins():
    o = observe(pending(after='<div role="dialog"><h2>Something new</h2></div>'))
    assert o.ambiguous is True and not approval.flagged(o)
    o = observe(pending(profile=False))
    assert o.ambiguous is True and not approval.flagged(o)


def test_a_plain_in_progress_turn_without_a_card_stays_a_generator():
    html = page_html(turn(OLD_TURN, "<div></div>"), turn(NEW_TURN, THINKING))
    assert_protected(observe(html))


def test_pending_then_a_real_new_answer_retracts_the_block():
    seen = []

    async def case(page, adapter):
        seen.append(await adapter.observe())
        await page.evaluate(
            """(key) => {
              const root = document.querySelector('[data-thread-find-target]');
              root.querySelector('[data-codex-approval-surface]').remove();
              root.insertAdjacentHTML('beforeend',
                `<div data-turn-key="${key}"><div data-talvt-turn-state="complete"></div></div>`);
            }""",
            "33333333-3333-4333-8333-333333333333",
        )
        seen.append(await adapter.observe())

    run_dom(case, pending())
    before, after = seen
    assert_card_blocks(before)
    assert not approval.flagged(after) and after.running is False
    assert after.turn_state == "complete" and after.turn != before.turn


def test_the_card_going_away_with_the_same_turn_still_generating_is_a_generator_again():
    seen = []

    async def case(page, adapter):
        seen.append(await adapter.observe())
        await page.evaluate(
            "document.querySelector('[data-codex-approval-surface]').remove()"
        )
        seen.append(await adapter.observe())

    run_dom(case, pending())
    assert approval.flagged(seen[0])
    assert_protected(seen[1])


def test_the_observation_carries_flags_and_ids_only_never_reasoning_text():
    o = observe(pending())
    assert_card_blocks(o)
    blob = json.dumps(asdict(o))
    assert REASONING not in blob and "Allow this tool call" not in blob
    assert "Always allow" not in blob


# --- the observation contract -------------------------------------------------------------------

CHAT = "https://chatgpt.com/c/" + OLD_TURN
SAFE = Observation(
    CHAT,
    NEW_TURN,
    NEW_TURN,
    status="Thinking failed",
    active=True,
    verified=True,
    model="Astra",
    ambiguous=False,
    turn_state="cancelled",
)
READY = replace(SAFE, status="", active=False, turn_state="complete")
# What the page script reports while a card waits: the in_progress marker makes it "running".
STRUCTURAL = Observation(
    CHAT, NEW_TURN, NEW_TURN, running=True, ambiguous=False, turn_state="in_progress"
)
FACTS = {"card": True, "stop": False, "turn": NEW_TURN}


def waiting(**extra):
    """A native card pending, built the way the adapter builds it (per test)."""
    return replace(
        ApprovalObservation(**{**READY.__dict__, "approval_wait": True}), **extra
    )


def test_approval_wait_defaults_to_false_and_a_plain_observation_is_never_approval():
    assert ApprovalObservation("", "", "").approval_wait is False
    assert not approval.flagged(Observation("", "", "", running=True))
    assert not approval.pending(READY) and not approval.pending(Observation("", "", ""))
    assert SAFE.safe_failure() and READY.operator_ready()
    control = ApprovalObservation(**{**SAFE.__dict__, "approval_wait": False})
    assert control.safe_failure() and not approval.pending(control)


def test_the_merge_turns_the_running_card_page_into_the_effective_blocked_state():
    merged = approval.merge(STRUCTURAL, FACTS)
    assert isinstance(merged, ApprovalObservation) and approval.pending(merged)
    assert merged.running is False and merged.approval_wait is True
    assert merged.turn_state == "in_progress"  # the raw state stays recorded
    assert merged.identity == STRUCTURAL.identity


@pytest.mark.parametrize(
    "facts",
    [
        {**FACTS, "card": False},
        {**FACTS, "stop": True},
        {**FACTS, "turn": OLD_TURN},
        {**FACTS, "turn": ""},
        {**FACTS, "card": "true"},
        {**FACTS, "card": 1},
        {**FACTS, "stop": None},
        {**FACTS, "turn": 5},
        {"card": True, "stop": False},
        {**FACTS, "extra": True},
        [],
        None,
        "card",
    ],
)
def test_missing_malformed_or_disagreeing_facts_are_no_evidence(facts):
    assert approval.merge(STRUCTURAL, facts) is STRUCTURAL


@pytest.mark.parametrize(
    "change",
    [
        {"draft": True},
        {"challenge": True},
        {"quota": True},
        {"ambiguous": True},
        {"status": "Thinking failed", "active": True, "turn_state": "cancelled"},
        {"status": "Sol quota exhausted"},
        {"chat": ""},
        {"turn": "", "generation": ""},
        {"running": False},
        {"turn_state": "complete"},
        {"turn_state": "unknown"},
    ],
)
def test_every_other_signal_wins_over_a_card_in_the_merge(change):
    other = replace(STRUCTURAL, **change)
    assert approval.merge(other, FACTS) is other


@pytest.mark.parametrize("running", [False, True])
def test_every_go_and_continuation_gate_vetoes_a_pending_card(running):
    failed = ApprovalObservation(
        **{**SAFE.__dict__, "approval_wait": True, "running": running}
    )
    assert not failed.safe_failure()
    card = waiting(running=running)
    assert not card.operator_candidate() and not card.operator_ready()
    assert observation_refusal(card.chat, card.turn, card) != ""


def test_a_card_always_reads_in_progress_so_no_caller_can_launder_it_into_complete():
    for state in ("complete", "cancelled", "unknown", ""):
        card = ApprovalObservation(
            CHAT, NEW_TURN, NEW_TURN, approval_wait=True, turn_state=state
        )
        assert card.turn_state == "in_progress"
    assert (
        ApprovalObservation(CHAT, NEW_TURN, NEW_TURN, turn_state="complete").turn_state
        == "complete"
    )


def test_an_explicit_continuation_request_is_still_refused_with_the_running_reason():
    card = waiting()
    assert observation_refusal(card.chat, card.turn, card) == "running"
    # Control: the very same observation without the card is continuable.
    assert observation_refusal(READY.chat, READY.turn, READY) == ""


def test_a_card_never_clears_the_other_continuation_stops():
    for stop in (
        {"challenge": True},
        {"draft": True},
        {"quota": True},
        {"ambiguous": True},
    ):
        blocked = replace(READY, **stop)
        assert observation_refusal(blocked.chat, blocked.turn, blocked) != ""
        both = waiting(**stop)
        assert observation_refusal(both.chat, both.turn, both) != ""
        assert approval.flagged(both) and not both.operator_ready()
        assert not approval.pending(both)


def test_the_effective_state_needs_identity_and_nothing_else_claiming_the_page():
    assert approval.pending(waiting())
    for stop in (
        {"running": True},
        {"draft": True},
        {"challenge": True},
        {"quota": True},
    ):
        assert not approval.pending(waiting(**stop))
    assert not approval.pending(waiting(chat=""))
    assert not approval.pending(waiting(status="Sol quota exhausted"))


class StubPage:
    """A page whose one evaluate answer is scripted; it records the script it was handed."""

    def __init__(self, raw):
        self.raw = raw
        self.scripts = []

    async def evaluate(self, script, args=None):
        self.scripts.append(script)
        return self.raw


def adapter_reads(raw):
    from agenttools_chatgpt.live_browser import LiveAdapter

    page = StubPage(raw)
    seen = asyncio.run(LiveAdapter(page, allow_active_probe=False).observe())
    return seen, page


def test_the_adapter_merges_the_structural_read_and_the_card_facts_of_one_evaluate():
    seen, page = adapter_reads({"structural": asdict(STRUCTURAL), "approval": FACTS})
    assert approval.pending(seen) and seen.running is False
    assert len(page.scripts) == 1  # one instant, one evaluate


def test_an_adapter_answer_without_card_facts_is_the_plain_read_never_approval():
    seen, _ = adapter_reads(asdict(STRUCTURAL))  # the pre-0.3.16 shape
    assert seen == STRUCTURAL and not approval.flagged(seen) and seen.running
    seen, _ = adapter_reads({"structural": asdict(STRUCTURAL)})  # half a pair
    assert seen == Observation("", "", "")  # unrecognized, hence unreadable
    seen, _ = adapter_reads({"structural": asdict(STRUCTURAL), "approval": "x"})
    assert seen.running is True and not approval.flagged(seen)


# --- fair queue: A holds a card, B and C must still be read -----------------------------------


def run(case):
    return asyncio.run(case)


def lap(rig, count):
    async def go():
        outcomes = []
        for _ in range(count):
            await advance_to_due(rig)
            report = await rig.patrol.tick()
            outcomes.append((report["health"], (report["visit"] or {}).get("outcome")))
        return outcomes

    return go()


def kinds(**named):
    return {CHATS[int(i)]: kind for i, kind in named.items()}


def row(rig, chat):
    snapshot = rig.rotation.snapshot(rig.store.controls())
    return next(r for r in snapshot["queue"] if r["chat"] == chat), snapshot


def test_b_and_c_are_not_starved_by_the_card_of_a_and_a_never_receives_anything(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path,
            monkeypatch,
            kinds=kinds(**{"0": "approval"}),
            enroll=CHATS[:3],
        )
        outcomes = await lap(rig, 6)
        assert outcomes == [
            ("connected", "permission-blocked"),
            ("connected", "complete"),
            ("connected", "complete"),
            ("connected", "permission-blocked"),
            ("connected", "complete"),
            ("connected", "complete"),
        ]
        assert [rig.world.chats[c].sends for c in CHATS[:3]] == [0, 0, 0]
        assert attempts(rig.store) == [] and rig.rotation.go is None
        assert rig.context.created == 1 and len(rig.context.pages) == 1

    run(case())


def test_the_blocked_chat_stays_queued_with_reason_and_visit_provenance(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        await lap(rig, 3)
        a, snapshot = row(rig, CHATS[0])
        assert a["state"] == "permission-blocked"
        assert a["reason"] == "pending-platform-approval"
        assert a["visits"] == 1 and a["last_visit_at"] is not None
        assert a["last_outcome"] == "permission-blocked"
        assert a["retry"] is None and a["last_ok_at"] is None
        assert snapshot["queue_size"] == 3
        assert snapshot["next_navigation_at"] > WALL0
        # Blocked is neither done nor complete nor failed.
        assert snapshot["coverage"]["complete"] is False
        assert snapshot["coverage"]["never_clean"] >= 1
        b, _ = row(rig, CHATS[1])
        assert b["state"] != "permission-blocked" and b["reason"] == ""

    run(case())


def test_the_card_is_a_neutral_visit_not_a_failure_or_a_clean_success(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        level = rig.rotation.pressure.level
        await lap(rig, 1)
        a, _ = row(rig, CHATS[0])
        assert a["retry"] is None and a["last_ok_at"] is None
        assert rig.rotation.pressure.level == level
        assert rig.rotation.cooldown is None
        assert rig.rotation.pressure.recent == []  # no success-rate sample either way

    run(case())


def test_the_tab_may_leave_a_clearly_paused_card_to_read_the_next_chat(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path,
            monkeypatch,
            kinds=kinds(**{"0": "approval"}),
            urls=(URLS[CHATS[0]],),
            enroll=CHATS[:3],
        )
        page = rig.context.pages[0]
        assert (await lap(rig, 1)) == [("connected", "permission-blocked")]
        assert page.calls == [] and page.url == URLS[CHATS[0]]
        report = (await lap(rig, 1))[0]
        assert report == ("connected", "complete")
        assert page.url == URLS[CHATS[1]] and rig.context.created == 0
        assert page.calls == ["goto"]  # only navigation: no click, accept or deny

    run(case())


@pytest.mark.parametrize(
    "overlay, blocked",
    [
        ({"running": True}, "running"),
        ({"draft": True}, "draft"),
        ({"challenge": True}, "challenge"),
        ({"turn": "", "generation": ""}, "approval-unverified"),
    ],
)
def test_stop_draft_challenge_and_missing_identity_keep_the_tab_protected(
    tmp_path, monkeypatch, overlay, blocked
):
    async def case():
        rig = make_rig(
            tmp_path,
            monkeypatch,
            kinds=kinds(**{"0": "approval"}),
            urls=(URLS[CHATS[0]],),
            enroll=CHATS[:3],
        )
        rig.world.chats[CHATS[0]].overlay = overlay
        page = rig.context.pages[0]
        await advance_to_due(rig)
        await rig.patrol.tick()  # the card's own chat is read once
        await advance_to_due(rig)
        report = await rig.patrol.tick()  # the next chat may not take the tab
        assert report["health"] == "protected-tab" and report["blocked"] == blocked
        assert page.calls == [] and page.url == URLS[CHATS[0]]
        assert sum(c.sends for c in rig.world.chats.values()) == 0

    run(case())


@pytest.mark.parametrize(
    "overlay, outcome",
    [({"quota": True}, "rate-limited"), ({"draft": True}, "draft")],
)
def test_quota_and_draft_win_the_visit_outcome_over_the_card(
    tmp_path, monkeypatch, overlay, outcome
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        rig.world.chats[CHATS[0]].overlay = overlay
        report = await lap(rig, 1)
        assert report == [("connected", outcome)]
        a, _ = row(rig, CHATS[0])
        assert a["state"] != "permission-blocked"
        assert a["reason"] != "pending-platform-approval"

    run(case())


def test_an_uncertain_card_without_identity_is_not_effective(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        rig.world.chats[CHATS[0]].overlay = {"ambiguous": True}
        outcomes = await lap(rig, 1)
        assert outcomes != [("connected", "permission-blocked")]
        a, _ = row(rig, CHATS[0])
        assert a["reason"] == ""

    run(case())


def test_an_owner_pause_wins_and_the_card_never_releases_it(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        await lap(rig, 1)
        rig.store.stop(CHATS[0], "owner holds this chat")
        outcomes = await lap(rig, 2)
        assert [o for _, o in outcomes] == ["complete", "complete"]
        a, _ = row(rig, CHATS[0])
        assert a["state"] == "paused" and a["reason"] == "owner holds this chat"
        assert rig.world.chats[CHATS[0]].sends == 0

    run(case())


def test_a_new_real_answer_retracts_the_block_on_the_next_visit(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:2]
        )
        assert (await lap(rig, 1))[0][1] == "permission-blocked"
        await lap(rig, 1)  # chat B
        rig.world.chats[CHATS[0]].kind = "complete"  # the answer arrived
        outcomes = await lap(rig, 1)
        assert outcomes == [("connected", "complete")]
        a, _ = row(rig, CHATS[0])
        assert a["state"] != "permission-blocked" and a["reason"] == ""
        assert a["last_outcome"] == "complete"

    run(case())


def test_observe_only_patrol_also_leaves_the_card_and_never_sends(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path,
            monkeypatch,
            kinds=kinds(**{"0": "approval"}),
            enroll=CHATS[:3],
            apply=False,
        )
        outcomes = await lap(rig, 3)
        assert [o for _, o in outcomes] == [
            "permission-blocked",
            "complete",
            "complete",
        ]
        assert all(c.sends == 0 for c in rig.world.chats.values())

    run(case())


def test_the_blocked_state_survives_a_restart_without_a_second_interval(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        await lap(rig, 1)
        stored = rotation.read_state(rotation.rotation_path(rig.store.path), "home")
        entry = next(e for e in stored["queue"] if e.chat == CHATS[0])
        assert entry.last_outcome == "permission-blocked" and entry.retry is None
        reopened = rotation.Rotation.read(
            rotation.rotation_path(rig.store.path), "home", clock=rig.clock.wall
        )
        view = reopened.snapshot(rig.store.controls())
        blocked = next(r for r in view["queue"] if r["chat"] == CHATS[0])
        assert blocked["reason"] == "pending-platform-approval"

    run(case())


def test_the_snapshot_holds_flags_and_ids_only(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        await lap(rig, 1)
        text = json.dumps(rig.rotation.snapshot(rig.store.controls()))
        assert REASONING not in text and "Allow this tool call" not in text

    run(case())


# --- the page script without a browser: the shared card finder on a tiny fake DOM --------------
# The server has no runnable Chromium, so the real-DOM cases above skip there. These cases run the
# REAL conversation_root.js helpers under node on a minimal element tree that answers exactly the
# queries the helpers make (tag, [attr], [attr="v"], .class, comma lists, closest, contains).

NODE_HARNESS = r"""
const fs = require('fs');
class El {
  constructor(tag, attrs = {}, kids = []) {
    this.tag = tag; this.attrs = attrs; this.children = []; this.parentElement = null;
    this.style = attrs.style || {}; this.hidden = !!attrs.hidden; this.isConnected = true;
    kids.forEach(k => this.append(k));
  }
  append(k) { k.parentElement = this; this.children.push(k); return k; }
  getAttribute(n) { return n in this.attrs ? String(this.attrs[n]) : null; }
  getClientRects() { return this.attrs.norect ? [] : [1]; }
  descendants() { return this.children.flatMap(c => [c, ...c.descendants()]); }
  querySelectorAll(sel) { return this.descendants().filter(e => e.matches(sel)); }
  contains(o) { for (let p = o; p; p = p.parentElement) if (p === this) return true; return false; }
  closest(sel) { for (let p = this; p; p = p.parentElement) if (p.matches(sel)) return p; return null; }
  matches(sel) { return sel.split(',').map(s => s.trim()).some(s => this.simple(s)); }
  simple(s) {
    let m;
    if ((m = s.match(/^\[([\w-]+)\]$/))) return m[1] in this.attrs;
    if ((m = s.match(/^\[([\w-]+)="([^"]*)"\]$/))) return this.attrs[m[1]] === m[2];
    if ((m = s.match(/^\.([\w-]+)$/))) return (this.attrs.class || '').split(' ').includes(m[1]);
    return s === this.tag;
  }
}
const h = (tag, attrs, ...kids) => new El(tag, attrs || {}, kids);
globalThis.getComputedStyle = el => ({
  display: el.style.display || 'block', visibility: el.style.visibility || 'visible',
  opacity: el.style.opacity ?? '1',
});
const card = (extra = {}) =>
  h('div', {'data-codex-approval-surface': 'true', ...extra}, h('form', {}, h('button', {})));
const build = (olderKids, latestKids) => {
  const older = h('div', {'data-turn-key': 'old'}, ...olderKids);
  const latest = h('div', {'data-turn-key': 'new'}, ...latestKids);
  const root = h('div', {}, older, latest);
  globalThis.document = h('html', {}, root);
  return {root, latest};
};
const src = fs.readFileSync(process.argv[1], 'utf8');
const api = new Function(src + '; return {platformApprovalCard};')();
const wrap = (tag, attrs) => h(tag, attrs, card());
const found = (older, latest) => { const b = build(older, latest); return api.platformApprovalCard(b.root, b.latest); };
const hidden = [{hidden: true}, {style: {display: 'none'}}, {style: {visibility: 'hidden'}},
  {'aria-hidden': 'true'}, {norect: true}];
const out = {
  baseline: found([], [card()]),
  older_only: found([card()], [h('div')]),
  older_and_latest: found([card()], [card()]),
  two_visible: found([], [card(), card()]),
  user_bubble: found([], [wrap('div', {'data-user-message-bubble': 'true'})]),
  user_role: found([], [wrap('div', {'data-message-author-role': 'user'})]),
  markdown: found([], [wrap('div', {class: 'markdown'})]),
  markdown_copy: found([], [wrap('div', {'data-markdown-copy': 'true'})]),
  quote: found([], [wrap('blockquote', {})]),
  code: found([], [wrap('pre', {})]),
  lookalike_beside_real: found([], [card(), wrap('blockquote', {})]),
  no_form: found([], [h('div', {'data-codex-approval-surface': 'true'}, h('p'))]),
  nested_form: found([], [h('div', {'data-codex-approval-surface': 'true'},
    h('div', {}, h('form', {}, h('button', {}))))]),
  no_button: found([], [h('div', {'data-codex-approval-surface': 'true'}, h('form', {}))]),
  two_forms: found([], [h('div', {'data-codex-approval-surface': 'true'},
    h('form', {}, h('button', {})), h('form', {}, h('button', {})))]),
  hidden_button: found([], [h('div', {'data-codex-approval-surface': 'true'},
    h('form', {}, h('button', {hidden: true})))]),
};
hidden.forEach((attrs, i) => {
  out['hidden_copy_beside_real_' + i] = found([], [card(), card(attrs)]);
  out['hidden_copy_alone_' + i] = found([], [card(attrs)]);
});
console.log(JSON.stringify(out));
"""

NODE_PARSE = (
    "const fs=require('fs');new Function('return ' + fs.readFileSync(0,'utf8'));"
    "console.log('ok')"
)


@pytest.fixture(scope="module")
def node_cases():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the page-script unit cases")
    root = Path(__file__).resolve().parents[1] / "lib" / "agenttools_chatgpt"
    done = subprocess.run(
        [node, "-e", NODE_HARNESS, "--", str(root / "conversation_root.js")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_card_finder_accepts_only_the_one_native_card_of_the_latest_turn(
    node_cases,
):
    assert node_cases["baseline"] is True
    assert node_cases["lookalike_beside_real"] is True
    for name in ("older_only", "older_and_latest", "two_visible", "no_form"):
        assert node_cases[name] is False, name
    for name in ("nested_form", "no_button", "two_forms", "hidden_button"):
        assert node_cases[name] is False, name


@pytest.mark.parametrize(
    "name", ["user_bubble", "user_role", "markdown", "markdown_copy", "quote", "code"]
)
def test_lookalikes_in_user_bubbles_markdown_or_quotes_are_content(node_cases, name):
    assert node_cases[name] is False


@pytest.mark.parametrize("index", range(5))
def test_hidden_copies_never_count_and_never_break_uniqueness(node_cases, index):
    assert node_cases[f"hidden_copy_beside_real_{index}"] is True
    assert node_cases[f"hidden_copy_alone_{index}"] is False


@pytest.mark.parametrize("script", ["live_dom.js", "monitor_dom.js"])
def test_the_injected_scripts_still_parse_with_the_shared_helper(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the page-script unit cases")
    from agenttools_chatgpt import scripts

    done = subprocess.run(
        [node, "-e", NODE_PARSE],
        input=scripts.load(script),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


def test_the_combined_observation_script_is_one_valid_expression():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the page-script unit cases")
    from agenttools_chatgpt import scripts

    combined = approval.observe_script(scripts.load("live_dom.js"), scripts.shared())
    done = subprocess.run(
        [node, "-e", NODE_PARSE],
        input=combined,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr
    assert combined.startswith("(args) => ({structural: ")


def test_a_queued_continuation_refused_for_a_card_is_still_a_neutral_blocked_visit(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from agenttools_chatgpt import patrol

    class Ledger:
        def __init__(self, store):
            pass

        def expire(self, now):
            pass

        def next_queued(self, now):
            return SimpleNamespace(chat=CHATS[0])

    async def refused(*args, **kwargs):
        return "request", "running"

    async def case():
        monkeypatch.setattr(patrol, "Continuations", Ledger)
        monkeypatch.setattr(patrol, "process_continuation", refused)
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        assert await lap(rig, 1) == [("connected", "permission-blocked")]
        a, _ = row(rig, CHATS[0])
        assert a["state"] == "permission-blocked" and a["last_ok_at"] is None
        assert a["retry"] is None and rig.rotation.pressure.recent == []
        assert rig.world.chats[CHATS[0]].sends == 0

    run(case())


def test_a_card_beside_a_failure_status_never_reaches_the_go_branch(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        rig.world.chats[CHATS[0]].overlay = {
            "status": "Thinking failed",
            "active": True,
            "verified": True,
            "model": "Astra",
        }
        ((_, outcome),) = await lap(rig, 1)
        assert not outcome.startswith("failed")
        assert rig.world.chats[CHATS[0]].sends == 0 and attempts(rig.store) == []

    run(case())


def test_a_real_5xx_during_a_blocked_visit_still_counts_as_transient(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=kinds(**{"0": "approval"}), enroll=CHATS[:3]
        )
        rig.rotation.sync({chat: URLS[chat] for chat in CHATS[:3]})
        entry = rig.rotation.queue[0]
        rig.rotation.begin_visit(entry)
        rig.rotation.note_server_error()
        rig.rotation.end_visit(entry, "permission-blocked", True)
        assert entry.retry is not None and entry.retry["reason"] == "http-5xx"
        assert entry.last_ok_at is None
        calm = rig.rotation.queue[1]
        rig.rotation.begin_visit(calm)
        rig.rotation.end_visit(calm, "permission-blocked", True)
        assert calm.retry is None and calm.last_ok_at is None

    run(case())


# --- the passive monitor agrees with the adapter -------------------------------------------------


def test_passive_monitor_reports_the_current_card_as_waiting_not_generating():
    s = passive(pending())
    assert s["status"] == "awaiting-approval"
    assert s["running"] is False and s["approval_wait"] is True
    assert s["turn"] == NEW_TURN and s["turns"][-1]["assistant_state"] == "in_progress"
    assert REASONING not in json.dumps(s) and "Allow" not in json.dumps(s)


@pytest.mark.parametrize(
    "html",
    [
        pending(after='<button aria-label="Stop">Stop</button>'),
        pending(
            composer=COMPOSER.replace(
                "<p data-empty-paragraph",
                "<p>half a thought</p><p data-empty-paragraph",
            )
        ),
        pending(card=f"<blockquote>{CARD}</blockquote>"),
        pending(card=f"<div hidden>{CARD}</div>"),
        page_html(
            turn(OLD_TURN, THINKING.replace("in_progress", "complete") + CARD),
            turn(NEW_TURN, THINKING),
        ),
    ],
    ids=["stop", "draft", "quoted", "hidden", "old-turn"],
)
def test_passive_monitor_keeps_every_other_page_working(html):
    assert_passive_working(passive(html))


def test_passive_monitor_leaves_an_answered_turn_complete():
    s = passive(page_html(turn(OLD_TURN, THINKING + CARD), turn(NEW_TURN, ANSWER)))
    assert s["running"] is False and s["status"] != "awaiting-approval"
    assert "approval_wait" not in s


@pytest.mark.parametrize(
    "dialog", ['<div role="dialog">x</div>', '<div role="alert">Heads up</div>']
)
def test_passive_monitor_keeps_a_page_with_another_claimant_working(dialog):
    assert_passive_working(passive(pending(after=dialog)))


def test_passive_monitor_keeps_a_page_without_a_profile_menu_working():
    html = page_html(
        turn(OLD_TURN, '<div data-user-message-bubble="true">go</div>'),
        turn(NEW_TURN, THINKING + CARD),
        profile=False,
    )
    assert_passive_working(passive(html))

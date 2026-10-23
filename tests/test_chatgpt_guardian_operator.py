"""Approved-continuation API, durable ownership and operator policy; fakes only, no browser.

The contract under test: a local trusted caller (pm) has an owner approval for ONE exact
chat+turn and posts it to the account-local authenticated control API. The API only queues.
The live loop is the sole sender and re-validates everything immediately before the click.
"""

import asyncio
import concurrent.futures
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt.continuation import (
    Continuations,
    ContinueRequest,
    RequestError,
    dispatch_continuation,
    parse_request,
)
from agenttools_chatgpt.core import Guardian, MutationRefusal, Observation, Store
from agenttools_chatgpt.service import ControlServer

CHAT = "https://chatgpt.com/c/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_CHAT = "https://chatgpt.com/c/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
TOKEN = "t" * 40
OTHER_TOKEN = "o" * 40


def request(**over) -> ContinueRequest:
    now = time.time()
    fields = {
        "request_id": str(uuid.uuid4()),
        "chat": CHAT,
        "turn": T1,
        "approved_at": now - 5,
        "expires_at": now + 600,
    }
    fields.update(over)
    return ContinueRequest(**fields)


def body(**over) -> dict:
    now = time.time()
    fields = {
        "request_id": str(uuid.uuid4()),
        "chat": CHAT,
        "expected_turn": T1,
        "approved_at": now - 5,
        "expires_at": now + 600,
    }
    fields.update(over)
    return {k: v for k, v in fields.items() if v is not ...}


def observation(**over) -> Observation:
    fields = {
        "chat": CHAT,
        "turn": T1,
        "generation": T1,
        "status": "",
        "active": False,
        "verified": True,
        "model": "Astra",
        "effort": "",
        "running": False,
        "draft": False,
        "challenge": False,
        "ambiguous": False,
        "quota": False,
        "turn_state": "complete",
    }
    fields.update(over)
    return Observation(**fields)


class FakeAdapter:
    """Scripted adapter: the last observation repeats; send is counted, never real."""

    def __init__(self, *observations, result=True):
        self.script = list(observations) or [observation()]
        self.result = result
        self.observes = 0
        self.sends: list[Observation] = []

    async def observe(self):
        self.observes += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return item() if callable(item) else item

    async def send(self, expected):
        self.sends.append(expected)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def make_store(tmp_path, account="home", enroll=CHAT):
    store = Store(tmp_path / "state.sqlite", account=account)
    if enroll:
        store.enroll(enroll)
    return store


def attempts(store):
    with store.connect() as db:
        return db.execute(
            "SELECT chat,turn,generation,state FROM attempts ORDER BY wall"
        ).fetchall()


def run(coro):
    return asyncio.run(coro)


def dispatch(store, record, adapter, **kwargs):
    return run(
        dispatch_continuation(Guardian(store, apply=True), record, adapter, **kwargs)
    )


def queued(store, req=None, **over):
    cont = Continuations(store)
    req = req or request(**over)
    accepted, receipt = cont.submit(req, time.time())
    assert accepted, receipt
    return cont, cont.get(req.request_id)


# --- request validation -----------------------------------------------------------------


@pytest.mark.parametrize("missing", list(body()))
def test_every_documented_field_is_required(missing):
    with pytest.raises(RequestError):
        parse_request(body(**{missing: ...}))


@pytest.mark.parametrize(
    "bad",
    [
        {"extra": True},
        {"approved": True},
        {"request_id": "not-a-uuid"},
        {"request_id": str(uuid.uuid4()).upper()},
        {"request_id": 5},
        {"chat": "https://evil.test/c/x"},
        {"chat": "*"},
        {"chat": CHAT + "?model=sol"},
        {"expected_turn": "turn-1"},
        {"expected_turn": ""},
        {"approved_at": True},
        {"approved_at": "1"},
        {"approved_at": float("nan")},
        {"expires_at": float("inf")},
        {"expires_at": None},
    ],
)
def test_malformed_requests_are_rejected_by_the_parser(bad):
    raw = body()
    raw.update(bad)
    with pytest.raises(RequestError):
        parse_request(raw)


def test_parser_rejects_non_objects_and_window_violations():
    for raw in ([], "x", None, 5):
        with pytest.raises(RequestError):
            parse_request(raw)
    now = time.time()
    with pytest.raises(RequestError):
        parse_request(body(approved_at=now, expires_at=now + 901))
    with pytest.raises(RequestError):
        parse_request(body(approved_at=now, expires_at=now))
    with pytest.raises(RequestError):
        parse_request(body(approved_at=now, expires_at=now - 1))
    parsed = parse_request(body(approved_at=now, expires_at=now + 900))
    assert parsed.chat == CHAT and parsed.turn == T1


# --- authenticated HTTP surface -----------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    store = make_store(tmp_path)
    woke = []
    server = ControlServer(
        store, TOKEN, 0, continuations=True, on_queued=lambda: woke.append(1)
    )
    server.start()
    base = "http://127.0.0.1:" + str(server.server.server_port)

    def call(method, path, payload=None, token=TOKEN, raw=None):
        data = (
            raw
            if raw is not None
            else (None if payload is None else json.dumps(payload).encode())
        )
        req = urllib.request.Request(base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    yield SimpleNamespace(store=store, server=server, call=call, woke=woke, base=base)
    server.close()


def rows(store):
    with store.connect() as db:
        return db.execute(
            "SELECT request_id,state,reason FROM continuations ORDER BY created_at"
        ).fetchall()


def test_continue_and_receipt_routes_require_the_bearer_token(api):
    rid = str(uuid.uuid4())
    for token in (None, "wrong" * 10, OTHER_TOKEN):
        assert (
            api.call("POST", "/continue", body(request_id=rid), token=token)[0] == 401
        )
        assert api.call("GET", "/continuations/" + rid, token=token)[0] == 401
    assert rows(api.store) == [] and not api.woke


def test_valid_request_is_queued_idempotently_and_wakes_the_loop(api):
    payload = body()
    status, reply = api.call("POST", "/continue", payload)
    assert (status, reply) == (
        202,
        {"ok": True, "request_id": payload["request_id"], "status": "queued"},
    )
    assert api.call("POST", "/continue", payload) == (status, reply)
    assert len(rows(api.store)) == 1
    status, receipt = api.call("GET", "/continuations/" + payload["request_id"])
    assert status == 200
    assert (receipt["ok"], receipt["status"], receipt["chat"]) == (True, "queued", CHAT)
    assert receipt["expected_turn"] == T1 and receipt["sent_at"] is None
    assert (
        api.woke
    )  # the live loop is notified; the handler itself never touches a browser
    assert not attempts(api.store)


@pytest.mark.parametrize(
    "bad",
    [
        {"extra": 1},
        {"request_id": "nope"},
        {"chat": "https://evil.test/c/x"},
        {"approved_at": "now"},
        {"expires_at": ...},
        {"approved_at": ...},
        {"expected_turn": ...},
        {"expires_at": time.time() + 99999},
    ],
)
def test_malformed_body_is_a_400_and_stores_nothing(api, bad):
    status, reply = api.call("POST", "/continue", body(**bad))
    assert status == 400 and reply["ok"] is False
    assert rows(api.store) == []


@pytest.mark.parametrize("raw", [b"", b"[]", b"not json", b'{"a": NaN}', b"x" * 5000])
def test_unparseable_or_unbounded_body_is_a_400(api, raw):
    assert api.call("POST", "/continue", raw=raw)[0] == 400
    assert rows(api.store) == []


def test_same_request_id_with_different_payload_conflicts_and_changes_nothing(api):
    first = body()
    assert api.call("POST", "/continue", first)[0] == 202
    for changed in (
        dict(first, expected_turn=T2),
        dict(first, expires_at=first["expires_at"] - 1),
        dict(first, chat=OTHER_CHAT),
    ):
        status, reply = api.call("POST", "/continue", changed)
        assert status == 409 and reply["error"] == "request-id-conflict"
    assert len(rows(api.store)) == 1
    stored = api.call("GET", "/continuations/" + first["request_id"])[1]
    assert (stored["status"], stored["expected_turn"]) == ("queued", T1)


def test_second_request_for_an_approved_turn_is_refused_not_queued(api):
    assert api.call("POST", "/continue", body())[0] == 202
    second = body()
    status, reply = api.call("POST", "/continue", second)
    assert (status, reply) == (
        409,
        {
            "ok": False,
            "request_id": second["request_id"],
            "status": "refused",
            "reason": "turn-already-requested",
        },
    )
    assert [r[1] for r in rows(api.store)] == ["queued", "refused"]
    # A different turn of the same chat is an independent approval.
    assert api.call("POST", "/continue", body(expected_turn=T2))[0] == 202


def test_expired_and_future_approvals_leave_a_refused_receipt(api):
    now = time.time()
    expired = body(approved_at=now - 800, expires_at=now - 10)
    future = body(approved_at=now + 3600, expires_at=now + 3700)
    for payload, reason in (
        (expired, "approval-expired"),
        (future, "approval-in-future"),
    ):
        status, reply = api.call("POST", "/continue", payload)
        assert (status, reply["status"], reply["reason"]) == (409, "refused", reason)
        receipt = api.call("GET", "/continuations/" + payload["request_id"])[1]
        assert (receipt["status"], receipt["reason"]) == ("refused", reason)
    assert not any(r[1] == "queued" for r in rows(api.store))
    # Refusals never occupy the turn: a fresh valid approval for it still queues.
    assert api.call("POST", "/continue", body())[0] == 202


def test_chat_outside_this_account_or_unwatched_is_refused(api, tmp_path):
    status, reply = api.call("POST", "/continue", body(chat=OTHER_CHAT))
    assert (status, reply["reason"]) == (409, "not-enrolled")
    api.store.set_control("watch:" + CHAT, "0")
    assert api.call("POST", "/continue", body())[1]["reason"] == "not-enrolled"
    assert not any(r[1] == "queued" for r in rows(api.store))


def test_cross_account_token_and_state_are_isolated(tmp_path):
    home = make_store(tmp_path / "home", "home")
    glide = make_store(tmp_path / "glide", "glide.vc", enroll=OTHER_CHAT)
    servers = [
        ControlServer(home, TOKEN, 0, continuations=True),
        ControlServer(glide, OTHER_TOKEN, 0, continuations=True),
    ]
    try:
        for server in servers:
            server.start()

        def post(server, token, payload):
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server.server_port}/continue",
                data=json.dumps(payload).encode(),
                method="POST",
                headers={"Authorization": "Bearer " + token},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        mine = body()
        assert (
            post(servers[1], TOKEN, mine) == 401
        )  # home token on the glide.vc account
        assert post(servers[1], OTHER_TOKEN, mine) == 409  # chat belongs to home
        assert post(servers[0], TOKEN, mine) == 202
        assert rows(glide) and all(r[1] == "refused" for r in rows(glide))
        assert [r[1] for r in rows(home)] == ["queued"]
        with pytest.raises(ValueError):
            Store(home.path, account="glide.vc")
    finally:
        for server in servers:
            server.close()


def test_continue_is_unavailable_when_no_apply_sender_exists(tmp_path):
    store = make_store(tmp_path)
    server = ControlServer(store, TOKEN, 0)  # default: no live apply loop attached
    server.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server.server_port}/continue",
            data=json.dumps(body()).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + TOKEN},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req, timeout=5)
        assert error.value.code == 409
        assert json.load(error.value)["error"] == "continuations-unavailable"
        assert rows(store) == []
    finally:
        server.close()


def test_unknown_receipt_and_bad_ids_are_rejected(api):
    assert api.call("GET", "/continuations/" + str(uuid.uuid4()))[0] == 404
    assert api.call("GET", "/continuations/not-a-uuid")[0] == 400
    assert api.call("GET", "/continuations/")[0] in (400, 404)


def test_simultaneous_callers_queue_one_request_per_turn_and_id(api):
    same_id = body()
    payloads = [same_id] * 6 + [body() for _ in range(6)]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda p: api.call("POST", "/continue", p), payloads))
    accepted = [r for r in results if r[0] == 202]
    assert {r[1]["request_id"] for r in accepted} == {
        r[0] for r in rows(api.store) if r[1] == "queued"
    }
    assert len([r for r in rows(api.store) if r[1] == "queued"]) == 1
    assert all(r[0] in (202, 409) for r in results)


def test_queue_depth_is_bounded(api):
    turns = [str(uuid.uuid4()) for _ in range(9)]
    codes = [api.call("POST", "/continue", body(expected_turn=t))[0] for t in turns]
    assert codes[:8] == [202] * 8 and codes[8] == 429
    assert len(rows(api.store)) == 8


def test_receipts_expose_no_secrets_or_message_bodies(api):
    payload = body()
    api.call("POST", "/continue", payload)
    receipt = api.call("GET", "/continuations/" + payload["request_id"])[1]
    assert set(receipt) <= {
        "ok",
        "request_id",
        "status",
        "reason",
        "chat",
        "expected_turn",
        "approved_at",
        "expires_at",
        "queued_at",
        "updated_at",
        "sent_at",
        "observed",
        "source",
    }
    assert TOKEN not in json.dumps(receipt)
    assert receipt["source"] == "operator"


def test_status_route_and_existing_controls_still_work(api):
    assert api.call("GET", "/status")[0] == 200
    assert api.call("POST", "/stop", {"chat": "*"}) == (200, {"ok": True})


# --- durable ownership and the dispatch state machine -----------------------------------------


def test_complete_turn_is_continued_exactly_once_and_recorded_as_a_real_attempt(
    tmp_path,
):
    store = make_store(tmp_path)
    original = request()
    cont, record = queued(store, original)
    adapter = FakeAdapter()
    assert dispatch(store, record, adapter) == "sent"
    assert len(adapter.sends) == 1 and adapter.sends[0].turn == T1
    assert attempts(store) == [(CHAT, T1, T1, "sent")]
    receipt = cont.receipt(record.request_id, time.time())
    assert (receipt["status"], receipt["reason"]) == ("sent", "acknowledged")
    assert receipt["sent_at"] is not None
    assert receipt["observed"] == {
        "model": "Astra",
        "effort": "",
        "turn_state": "complete",
    }
    # Replaying the consumed record or its id can never send again.
    assert dispatch(store, record, adapter) == "not-queued"
    accepted, replay = cont.submit(original, time.time())
    assert accepted and replay["status"] == "sent"
    assert len(adapter.sends) == 1


@pytest.mark.parametrize("state", ["complete", "cancelled", "unknown"])
def test_operator_policy_accepts_every_non_generating_state(tmp_path, state):
    store = make_store(tmp_path)
    _, record = queued(store)
    adapter = FakeAdapter(observation(turn_state=state))
    assert dispatch(store, record, adapter) == "sent"
    assert len(adapter.sends) == 1


def test_operator_policy_does_not_forge_or_require_thinking_failed(tmp_path):
    store = make_store(tmp_path)
    _, record = queued(store)
    adapter = FakeAdapter(observation(status="", active=False))
    assert not adapter.script[0].safe_failure()
    assert dispatch(store, record, adapter) == "sent"
    assert (
        adapter.sends[0].status == ""
    )  # the send saw the true state, never a faked failure


def test_sol_extra_high_needs_the_existing_per_chat_consent(tmp_path):
    store = make_store(tmp_path)
    _, record = queued(store)
    sol = observation(model="Sol", effort="Extra High")
    adapter = FakeAdapter(sol)
    assert dispatch(store, record, adapter) == "sol-consent-required"
    assert not adapter.sends
    store.opt_in_sol(CHAT)
    _, record = queued(store, turn=T2)
    adapter = FakeAdapter(replace_turn(sol, T2))
    assert dispatch(store, record, adapter) == "sent"


def replace_turn(obs, turn):
    from dataclasses import replace

    return replace(obs, turn=turn, generation=turn)


def prepare_blocked(name):
    """(store mutation, observation overrides, expected refusal)."""
    now = time.time()

    def insert(*rows_):
        def apply(store):
            with store.connect() as db:
                db.executemany("INSERT INTO attempts VALUES(?,?,?,?,?)", rows_)

        return apply

    return {
        "global-stop": (lambda s: s.stop(), {}, "stopped"),
        "chat-stop": (lambda s: s.stop(CHAT), {}, "stopped"),
        "unwatched": (lambda s: s.set_control("watch:" + CHAT, "0"), {}, "unwatched"),
        "quota-latched": (lambda s: s.quota(), {}, "quota"),
        "uncertain-send": (
            insert((CHAT, "old", "old", now - 7200, "uncertain")),
            {},
            "unresolved-send",
        ),
        "reserved-send": (
            insert((CHAT, "old", "old", now - 7200, "reserved")),
            {},
            "unresolved-send",
        ),
        "running": (None, {"running": True, "turn_state": "in_progress"}, "running"),
        "draft": (None, {"draft": True}, "draft-present"),
        "challenge": (None, {"challenge": True}, "challenge"),
        "ambiguous": (None, {"ambiguous": True}, "ambiguous-page"),
        "page-quota": (None, {"quota": True}, "quota"),
        "sol-quota-status": (None, {"status": "Sol quota exhausted"}, "quota"),
        "unverified-model": (
            None,
            {"verified": False, "model": ""},
            "unverified-model-effort",
        ),
        "unknown-model": (None, {"model": "Mystery"}, "unverified-model-effort"),
        "sol-without-effort": (
            None,
            {"model": "Sol", "effort": ""},
            "unverified-model-effort",
        ),
        "changed-turn": (None, {"turn": T2, "generation": T2}, "turn-changed"),
        "changed-chat": (None, {"chat": OTHER_CHAT}, "chat-changed"),
        "unverified-page": (
            None,
            {"chat": "", "turn": "", "generation": ""},
            "observation-unverified",
        ),
        "unsupported-state": (None, {"turn_state": ""}, "turn-state-unsupported"),
        "weird-state": (None, {"turn_state": "exploding"}, "turn-state-unsupported"),
        "deduplicated": (
            insert((CHAT, T1, T1, now - 7200, "resolved")),
            {},
            "deduplicated",
        ),
        "cooldown": (
            insert((CHAT, "old", "old", now - 5, "sent")),
            {},
            "cooldown",
        ),
        "hourly-limit": (
            insert(
                (CHAT, "o1", "o1", now - 3000, "sent"),
                (CHAT, "o2", "o2", now - 2000, "sent"),
                (CHAT, "o3", "o3", now - 1000, "sent"),
            ),
            {},
            "hourly-limit",
        ),
    }[name]


BLOCKED = [
    "global-stop", "chat-stop", "unwatched", "quota-latched", "uncertain-send",
    "reserved-send", "running", "draft", "challenge", "ambiguous", "page-quota",
    "sol-quota-status", "unverified-model", "unknown-model", "sol-without-effort",
    "changed-turn", "changed-chat", "unverified-page", "unsupported-state", "weird-state",
    "deduplicated", "cooldown", "hourly-limit",
]  # fmt: skip


@pytest.mark.parametrize("name", BLOCKED)
def test_every_blocked_condition_refuses_without_any_browser_send(tmp_path, name):
    mutate, overrides, reason = prepare_blocked(name)
    store = make_store(tmp_path)
    cont, record = queued(store)
    before = None
    if mutate:
        mutate(store)
    before = attempts(store)
    adapter = FakeAdapter(observation(**overrides))
    assert dispatch(store, record, adapter) == reason
    assert adapter.sends == []
    assert attempts(store) == before  # no reservation row remains for a refusal
    receipt = cont.receipt(record.request_id, time.time())
    assert (receipt["status"], receipt["reason"]) == ("refused", reason)
    assert dispatch(store, record, adapter) == "not-queued"


def test_approval_that_expires_before_dispatch_never_sends(tmp_path):
    store = make_store(tmp_path)
    now = time.time()
    cont, record = queued(store, approved_at=now - 5, expires_at=now + 30)
    adapter = FakeAdapter()
    assert (
        dispatch(store, record, adapter, clock=lambda: now + 31) == "approval-expired"
    )
    assert (
        adapter.sends == [] and adapter.observes == 0
    )  # refused before touching the page
    assert cont.receipt(record.request_id, now)["reason"] == "approval-expired"


def test_sweep_expires_stale_queue_entries_without_a_browser(tmp_path):
    store = make_store(tmp_path)
    now = time.time()
    cont, record = queued(store, approved_at=now - 5, expires_at=now + 30)
    assert cont.next_queued(now + 60) is None
    assert cont.expire(now + 60) == 1
    assert cont.receipt(record.request_id, now + 60)["status"] == "refused"
    # Reads are truthful even before the sweeper ran.
    store2 = make_store(tmp_path / "second")
    cont2, record2 = queued(store2, approved_at=now - 5, expires_at=now + 30)
    assert cont2.receipt(record2.request_id, now + 60)["reason"] == "approval-expired"


def test_wake_cuts_the_idle_sleep_but_keeps_a_minimum_gap_between_cycles():
    from agenttools_chatgpt.live import sleep_until_wake

    async def case():
        stop, wake = asyncio.Event(), asyncio.Event()
        begun = time.monotonic()
        wake.set()  # an approval was queued while the cycle ran; the last cycle was long ago
        await sleep_until_wake(stop, wake, 30, begun - 10)
        assert time.monotonic() - begun < 1 and not wake.is_set()
        begun = time.monotonic()
        wake.set()  # the cycle just started: respect the 5 second floor
        task = asyncio.ensure_future(sleep_until_wake(stop, wake, 30, begun))
        await asyncio.sleep(0.3)
        assert not task.done()
        stop.set()  # shutdown still ends the sleep immediately
        await asyncio.wait_for(task, 2)
        quiet = asyncio.Event()
        stop.clear()
        begun = time.monotonic()
        await sleep_until_wake(stop, quiet, 0.05, begun)  # plain poll timeout
        assert time.monotonic() - begun < 1

    run(case())


def test_no_browser_work_happens_without_an_authorized_queue_entry(tmp_path):
    store = make_store(tmp_path)
    cont = Continuations(store)
    assert cont.next_queued(time.time()) is None
    # A rejected/never-queued submission cannot be dispatched either.
    with pytest.raises(RequestError):
        parse_request(body(approved=True))
    assert cont.next_queued(time.time()) is None
    # The automatic path keeps refusing complete turns: no operator authority leaks into it.
    adapter = FakeAdapter()
    guardian = Guardian(store, apply=True)
    assert run(
        guardian.run_once(adapter, initial=observation(), observed_at=time.monotonic())
    ) == ("unsafe-or-not-active-failure")
    assert adapter.sends == []


def test_turn_change_after_reservation_is_refused_and_releases_the_reservation(
    tmp_path,
):
    store = make_store(tmp_path)
    cont, record = queued(store)
    adapter = FakeAdapter(observation(), observation(turn=T2, generation=T2))
    assert dispatch(store, record, adapter) == "changed-before-send"
    assert adapter.sends == [] and attempts(store) == [(CHAT, T1, T1, "canceled")]
    assert cont.receipt(record.request_id, time.time())["status"] == "refused"
    # The reservation consumed identity and budget even though nothing was clicked: a new
    # request id for the same turn, or a prompt retry on the next turn, is still refused.
    _, same_turn = queued(store)
    assert dispatch(store, same_turn, FakeAdapter()) == "deduplicated"
    _, record2 = queued(store, turn=T2)
    follow = FakeAdapter(observation(turn=T2, generation=T2))
    assert dispatch(store, record2, follow) == "cooldown"
    assert follow.sends == []


def test_stop_set_between_reservation_and_click_is_honored(tmp_path):
    store = make_store(tmp_path)
    _, record = queued(store)

    def stopping():
        store.stop(CHAT, "human stop")
        return observation()

    adapter = FakeAdapter(observation(), stopping)
    assert dispatch(store, record, adapter) == "stopped"
    assert adapter.sends == [] and attempts(store) == [(CHAT, T1, T1, "canceled")]


def test_approval_expiring_during_revalidation_is_honored(tmp_path):
    store = make_store(tmp_path)
    now = time.time()
    _, record = queued(store, approved_at=now - 5, expires_at=now + 30)
    adapter = FakeAdapter()

    def clock():  # the approval lapses once the post-reservation re-check has begun
        return now if adapter.observes < 2 else now + 40

    assert dispatch(store, record, adapter, clock=clock) == "approval-expired"
    assert adapter.sends == [] and attempts(store) == [(CHAT, T1, T1, "canceled")]


def test_failure_before_the_click_releases_but_failure_after_it_quarantines(tmp_path):
    store = make_store(tmp_path)
    cont, record = queued(store)

    def broken():
        raise ValueError("observation lost")

    adapter = FakeAdapter(observation(), broken)
    with pytest.raises(ValueError):
        dispatch(store, record, adapter)
    assert adapter.sends == [] and attempts(store) == [(CHAT, T1, T1, "canceled")]
    receipt = cont.receipt(record.request_id, time.time())
    assert (receipt["status"], receipt["reason"]) == (
        "refused",
        "interrupted-before-send",
    )
    _, again = queued(store)
    assert dispatch(store, again, FakeAdapter()) == "deduplicated"


@pytest.mark.parametrize(
    "result",
    [
        TimeoutError(),
        RuntimeError("renderer vanished"),
        asyncio.CancelledError(),
    ],
)
def test_transport_failure_during_send_is_uncertain_and_never_retried(tmp_path, result):
    store = make_store(tmp_path)
    cont, record = queued(store)
    adapter = FakeAdapter(result=result)
    with pytest.raises((TimeoutError, RuntimeError, asyncio.CancelledError)):
        dispatch(store, record, adapter)
    assert len(adapter.sends) == 1
    receipt = cont.receipt(record.request_id, time.time())
    assert (receipt["status"], receipt["reason"]) == ("uncertain", "interrupted")
    assert attempts(store) == [(CHAT, T1, T1, "uncertain")]
    # A restart, replay or fresh approval for this turn can never click again.
    restarted = Store(store.path, account="home")
    assert Continuations(restarted).next_queued(time.time()) is None
    assert Continuations(restarted).submit(request(), time.time())[1]["reason"] == (
        "turn-already-requested"
    )
    again = FakeAdapter()
    assert dispatch(restarted, record, again) == "not-queued"
    assert again.sends == []


def test_unacknowledged_send_is_uncertain_and_blocks_the_chat(tmp_path):
    store = make_store(tmp_path)
    cont, record = queued(store)
    adapter = FakeAdapter(result=False)
    assert dispatch(store, record, adapter) == "uncertain-send"
    receipt = cont.receipt(record.request_id, time.time())
    assert (receipt["status"], receipt["reason"]) == ("uncertain", "unacknowledged")
    assert attempts(store) == [(CHAT, T1, T1, "uncertain")]
    _, record2 = queued(store, turn=T2)
    assert dispatch(store, record2, FakeAdapter(replace_turn(observation(), T2))) == (
        "unresolved-send"
    )


def test_page_refusal_during_send_is_uncertain_and_records_safety(tmp_path):
    store = make_store(tmp_path)
    cont, record = queued(store)
    blocked = observation(draft=True)
    adapter = FakeAdapter(result=MutationRefusal(blocked))
    assert dispatch(store, record, adapter) == "uncertain-send"
    assert store.controls()["stop:" + CHAT] == "human draft"
    assert cont.receipt(record.request_id, time.time())["status"] == "uncertain"


def test_crash_after_reservation_is_uncertain_after_restart_and_not_resent(tmp_path):
    store = make_store(tmp_path)
    cont, record = queued(store)
    # Simulate a process killed between reservation and acknowledgement.
    assert cont.reserve(record, observation(), time.time()) == "reserved"
    shown = cont.receipt(record.request_id, time.time())
    assert (shown["status"], shown["reason"]) == ("queued", "dispatching")
    restarted = Store(store.path, account="home")
    revived = Continuations(restarted)
    assert revived.recover_interrupted() == 1
    receipt = revived.receipt(record.request_id, time.time())
    assert (receipt["status"], receipt["reason"]) == (
        "uncertain",
        "restart-interrupted",
    )
    assert attempts(restarted) == [(CHAT, T1, T1, "reserved")]  # still blocks the chat
    adapter = FakeAdapter()
    assert revived.next_queued(time.time()) is None
    assert dispatch(restarted, record, adapter) == "not-queued"
    assert adapter.sends == []


def test_dedup_and_queue_survive_restart(tmp_path):
    store = make_store(tmp_path)
    _, record = queued(store, turn=T2)
    restarted = Store(store.path, account="home")
    revived = Continuations(restarted)
    again = revived.next_queued(time.time())
    assert again is not None and again.request_id == record.request_id
    assert (
        dispatch(restarted, again, FakeAdapter(replace_turn(observation(), T2)))
        == "sent"
    )
    third = Store(store.path, account="home")
    accepted, receipt = Continuations(third).submit(request(turn=T2), time.time())
    assert not accepted and receipt["reason"] == "turn-already-requested"


def test_ownership_is_enforced_by_the_database_not_just_code(tmp_path):
    store = make_store(tmp_path)
    queued(store)
    with store.connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO continuations(request_id,chat,turn,digest,approved_at,expires_at,"
            "state,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), CHAT, T1, "d", 1, 2, "queued", "", 1, 1),
        )


# --- telemetry truthfulness ---------------------------------------------------------------


def test_monitor_distinguishes_operator_sends_and_correlates_the_following_reply(
    tmp_path,
):
    from agenttools_chatgpt.monitor import project_snapshot, read_source

    store = make_store(tmp_path)
    _, record = queued(store)
    assert dispatch(store, record, FakeAdapter()) == "sent"
    controls, history = read_source(store.path, "home")
    assert history[0]["state"] == "sent" and history[0]["source"] == "operator"
    now = time.time()
    sample = {
        "chat": CHAT,
        "title": "A task",
        "checked_at": now,
        "status": "complete",
        "running": False,
        "progress_token": "h",
        "turn": T2,
        "turns": [
            {
                "id": T1,
                "user_go": False,
                "assistant_state": "complete",
                "assistant_present": True,
            },
            {
                "id": T2,
                "user_go": True,
                "assistant_state": "complete",
                "assistant_present": True,
            },
        ],
    }
    snap = project_snapshot(
        "home",
        controls,
        history,
        [sample],
        None,
        {"connection": "connected", "heartbeat_at": now},
        now,
    )
    session = next(s for s in snap["sessions"] if s["chat"] == CHAT)
    assert session["last_go_source"] == "operator"
    assert session["last_go_state"] == "sent"
    assert (session["continuation"], session["continuation_turn"]) == ("completed", T2)
    # An earlier complete answer with no go reply is NOT a continuation.
    sample["turns"][1]["user_go"] = False
    again = project_snapshot(
        "home", controls, history, [sample], None,
        {"connection": "connected", "heartbeat_at": now}, now,
    )  # fmt: skip
    assert (
        next(s for s in again["sessions"] if s["chat"] == CHAT)["continuation"]
        == "unconfirmed"
    )


def test_automatic_attempts_stay_labelled_automatic_and_old_state_still_reads(tmp_path):
    from agenttools_chatgpt.monitor import project_snapshot, read_source

    store = make_store(tmp_path)
    with store.connect() as db:
        db.execute(
            "INSERT INTO attempts VALUES(?,?,?,?,?)",
            (CHAT, "f", "f", time.time() - 90, "sent"),
        )
    controls, history = read_source(store.path, "home")
    assert "source" not in history[0]
    now = time.time()
    snap = project_snapshot(
        "home", controls, history, [], None, {"connection": "connected"}, now
    )
    assert (
        next(s for s in snap["sessions"] if s["chat"] == CHAT)["last_go_source"]
        == "automatic"
    )
    # A pre-0.3 database has no continuations table: reading it never fails or creates one.
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as db:
        db.executescript(
            "CREATE TABLE account_scope(account TEXT); CREATE TABLE controls(key TEXT, value TEXT);"
            "CREATE TABLE attempts(chat TEXT, turn TEXT, generation TEXT, wall REAL, state TEXT);"
        )
        db.execute("INSERT INTO account_scope VALUES ('home')")
    assert read_source(legacy, "home") == ({}, [])


def test_refused_receipts_are_bounded_but_owning_rows_never_pruned(tmp_path):
    store = make_store(tmp_path)
    cont, owner = queued(store)
    now = time.time()
    with store.connect() as db:
        db.executemany(
            "INSERT INTO continuations(request_id,chat,turn,digest,approved_at,expires_at,"
            "state,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (str(uuid.uuid4()), CHAT, T2, "d", 1, 2, "refused", "x", now, now - i)
                for i in range(1005)
            ],
        )
    accepted, _ = cont.submit(request(chat=CHAT, turn=T2), now)
    assert accepted
    with store.connect() as db:
        refused = db.execute(
            "SELECT COUNT(*) FROM continuations WHERE state='refused'"
        ).fetchone()[0]
    assert refused == 1000
    assert cont.get(owner.request_id).state == "queued"


# --- approved-operator prepare-view, canceled reservations and telemetry -----------------


class ScrolledAdapter(FakeAdapter):
    """A page whose conversation is out of view until one controlled scroll is made."""

    def __init__(self, *, prepared=True, **kwargs):
        super().__init__(**kwargs)
        self.prepared = prepared
        self.prepares: list[tuple] = []
        self.scrolled = False

    async def observe(self):
        self.observes += 1
        if not self.scrolled:
            return Observation("", "", "")
        return observation()

    async def prepare_view(self, chat, turn, expires_at):
        self.prepares.append((chat, turn, expires_at))
        self.scrolled = self.prepared
        return self.prepared


def test_out_of_view_turn_gets_one_prepare_view_then_the_normal_gates_run(tmp_path):
    store = make_store(tmp_path)
    cont, record = queued(store)
    adapter = ScrolledAdapter()
    assert dispatch(store, record, adapter) == "sent"
    assert adapter.prepares == [(CHAT, T1, record.expires_at)]
    assert len(adapter.sends) == 1


def test_failed_prepare_view_refuses_without_reserving_or_sending(tmp_path):
    store = make_store(tmp_path)
    cont, record = queued(store)
    adapter = ScrolledAdapter(prepared=False)
    assert dispatch(store, record, adapter) == "view-not-ready"
    assert len(adapter.prepares) == 1 and adapter.sends == [] and attempts(store) == []


def test_prepare_view_is_not_attempted_when_the_view_is_already_readable(tmp_path):
    store = make_store(tmp_path)
    _, record = queued(store)
    adapter = ScrolledAdapter()
    adapter.scrolled = True
    assert dispatch(store, record, adapter) == "sent"
    assert adapter.prepares == []


def test_expired_approval_never_reaches_prepare_view(tmp_path):
    store = make_store(tmp_path)
    now = time.time()
    _, record = queued(store, approved_at=now - 5, expires_at=now + 30)
    adapter = ScrolledAdapter()
    assert dispatch(store, record, adapter, clock=lambda: now + 31) == "approval-expired"
    assert adapter.prepares == [] and adapter.observes == 0


def test_approval_expiring_during_prepare_view_is_refused_before_reserving(tmp_path):
    store = make_store(tmp_path)
    now = time.time()
    _, record = queued(store, approved_at=now - 5, expires_at=now + 30)
    adapter = ScrolledAdapter()

    def clock():
        return now + 40 if adapter.prepares else now

    assert dispatch(store, record, adapter, clock=clock) == "approval-expired"
    assert adapter.sends == [] and attempts(store) == []


def test_canceled_reservation_is_invisible_to_telemetry_but_stays_consumed(tmp_path):
    from agenttools_chatgpt.monitor import read_source

    store = make_store(tmp_path)
    _, record = queued(store)
    adapter = FakeAdapter(observation(), observation(turn=T2, generation=T2))
    assert dispatch(store, record, adapter) == "changed-before-send"
    _, attempt_rows = read_source(store.path, "home")
    assert attempt_rows == []  # never clicked: not a send attempt, not send-uncertain
    assert store.reserve(observation()) == "deduplicated"
    with store.connect() as db:
        assert store.blocked(observation(), db) == ""  # not an unresolved send

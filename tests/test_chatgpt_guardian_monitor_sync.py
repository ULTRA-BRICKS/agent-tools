"""A readable conversation DOM is not proof that the conversation is synchronized.

The monitor script reads the page's own Resource Timing entries (read-only) and refuses to call the
DOM fresh when the newest completed same-origin read of THIS conversation failed. Real Chromium
fixtures: sanitized page structure, real fetch entries served by routes, and synthetic entry lists
only for shapes a real request cannot produce (encoded paths, bogus statuses, foreign origins).
"""

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from test_chatgpt_guardian_monitor_dom import (
    CHAT,
    ID,
    OTHER_ID,
    T1,
    T2,
    page,
    root,
    turn,
)

ORIGIN = "https://chatgpt.com"
SINGULAR = f"{ORIGIN}/backend-api/conversation/{ID}"
PLURAL = f"{ORIGIN}/backend-api/conversations/{ID}"
OTHER_SINGULAR = f"{ORIGIN}/backend-api/conversation/{OTHER_ID}"
NOW = 1792254000.0

COMPLETE = page(root(inner=turn(T1, "complete")), stop=False)
WORKING = page(root(inner=turn(T1, "complete") + turn(T2, "in_progress")))
NO_ROOT = page("<div>No conversation</div>")
WRONG_ROOT = page(root(OTHER_ID))

REAL_QUERY = "?num_turns=40&include_has_versions=true"
ERROR_TEXT = "Could not load this ChatGPT conversation"
ERROR_UI = f'<div role="alert"><p>{ERROR_TEXT}</p><button type="button">Try again</button></div>'
LOAD_ERROR_PAGE = page(root(inner="") + ERROR_UI, stop=False)
EMPTY_PAGE = page(root(inner=""), stop=False)
QUOTED_ERROR_PAGE = page(
    root(
        inner=turn(T1, "complete", user=ERROR_TEXT, assistant="markdown").replace(
            "Answer", ERROR_TEXT
        )
    ),
    stop=False,
)
DRAFT_ERROR_PAGE = page(root(inner="")).replace("<p><br></p>", f"<p>{ERROR_TEXT}</p>")
SIDEBAR_ERROR_PAGE = page(root(inner=""), extra=f"<nav><a>{ERROR_TEXT}</a></nav>")

# name -> (html, [(url, status) real sequential fetches])
REAL = {
    "none": (COMPLETE, []),
    "singular-429": (COMPLETE, [(SINGULAR, 429)]),
    "plural-429": (COMPLETE, [(PLURAL, 429)]),
    "singular-401": (COMPLETE, [(SINGULAR, 401)]),
    "plural-403": (COMPLETE, [(PLURAL, 403)]),
    "singular-500": (COMPLETE, [(SINGULAR, 500)]),
    "plural-503": (COMPLETE, [(PLURAL, 503)]),
    "singular-200": (COMPLETE, [(SINGULAR, 200)]),
    "older-429-then-200": (COMPLETE, [(SINGULAR, 429), (SINGULAR, 200)]),
    "older-429-then-plural-200": (COMPLETE, [(SINGULAR, 429), (PLURAL, 200)]),
    "200-then-newer-429": (COMPLETE, [(SINGULAR, 200), (SINGULAR, 429)]),
    "429-200-429": (COMPLETE, [(SINGULAR, 429), (SINGULAR, 200), (PLURAL, 429)]),
    "other-chat-429": (COMPLETE, [(OTHER_SINGULAR, 429)]),
    "other-chat-newer-429-after-own-200": (
        COMPLETE,
        [(SINGULAR, 200), (OTHER_SINGULAR, 429)],
    ),
    "own-429-other-chat-200": (COMPLETE, [(SINGULAR, 429), (OTHER_SINGULAR, 200)]),
    "unrelated-telemetry-429": (
        COMPLETE,
        [
            (f"{ORIGIN}/ces/v1/t", 429),
            (f"{ORIGIN}/backend-api/conversations?offset=0&limit=28", 429),
            (f"{ORIGIN}/backend-api/conversation/{ID}/stream_status", 429),
            (f"{ORIGIN}/backend-api/conversation/{ID}/textdocs", 429),
            (f"{ORIGIN}/backend-api/me", 429),
        ],
    ),
    "working-200": (WORKING, [(SINGULAR, 200)]),
    "working-429": (WORKING, [(SINGULAR, 429)]),
    "no-root-429": (NO_ROOT, [(SINGULAR, 429)]),
    "no-root-200": (NO_ROOT, [(SINGULAR, 200)]),
    "wrong-root-429": (WRONG_ROOT, [(PLURAL, 429)]),
    "no-root-none": (NO_ROOT, []),
    # The real reads: exact path plus these query keys.
    "real-query-429": (COMPLETE, [(PLURAL + REAL_QUERY, 429)]),
    "real-query-429-then-200": (
        COMPLETE,
        [(PLURAL + REAL_QUERY, 429), (PLURAL + REAL_QUERY, 200)],
    ),
    "real-query-200-then-429": (
        COMPLETE,
        [(PLURAL + REAL_QUERY, 200), (PLURAL + REAL_QUERY, 429)],
    ),
    "mismatched-path-with-target-id-query": (
        COMPLETE,
        [
            (
                f"{ORIGIN}/backend-api/conversations/{OTHER_ID}?conversation_id={ID}&x={ID}",
                429,
            )
        ],
    ),
    "foreign-path-with-real-query": (
        COMPLETE,
        [(f"{ORIGIN}/backend-api/conversations{REAL_QUERY}&id={ID}", 429)],
    ),
    "load-error-empty": (LOAD_ERROR_PAGE, []),
    "empty-no-error": (EMPTY_PAGE, []),
    "quoted-error-in-turns": (QUOTED_ERROR_PAGE, []),
    "error-in-composer-draft": (DRAFT_ERROR_PAGE, []),
    "error-in-sidebar-title": (SIDEBAR_ERROR_PAGE, []),
}


def ent(url, status, end, start=None):
    start = end - 5 if start is None else start
    return (
        f'{{name:{json.dumps(url)},entryType:"resource",initiatorType:"fetch",'
        f"startTime:{start},responseEnd:{end},responseStatus:{status}}}"
    )


def entries(*items):
    return "[" + ",".join(items) + "]"


# name -> JS expression that replaces performance.getEntriesByType('resource')
STUB = {
    "tie-later-wins-429": entries(
        ent(SINGULAR, 200, 100, 10), ent(SINGULAR, 429, 100, 20)
    ),
    "tie-later-wins-200": entries(
        ent(SINGULAR, 429, 100, 10), ent(SINGULAR, 200, 100, 20)
    ),
    "response-end-orders-not-array": entries(
        ent(SINGULAR, 200, 300, 10), ent(SINGULAR, 429, 100, 90)
    ),
    "response-end-orders-429-last": entries(
        ent(SINGULAR, 429, 300, 10), ent(SINGULAR, 200, 100, 90)
    ),
    "incomplete-newer-does-not-clear": entries(
        ent(SINGULAR, 429, 100), ent(SINGULAR, 200, 0, 150)
    ),
    "incomplete-429-does-not-block": entries(ent(SINGULAR, 429, 0, 50)),
    "nan-end-ignored": entries(ent(SINGULAR, 429, "NaN", 50)),
    "bogus-newer-does-not-clear": entries(
        ent(SINGULAR, 429, 100), ent(SINGULAR, '"200"', 200)
    ),
    "encoded-id": entries(
        ent(f"{ORIGIN}/backend-api/conversation/" + ID.replace("-", "%2d"), 429, 100)
    ),
    "encoded-slash": entries(
        ent(f"{ORIGIN}/backend-api/conversation%2f{ID}", 429, 100)
    ),
    "dot-segments": entries(
        ent(f"{ORIGIN}/backend-api/conversation/x/../{ID}", 429, 100)
    ),
    "double-slash": entries(ent(f"{ORIGIN}/backend-api//conversation/{ID}", 429, 100)),
    "trailing-slash": entries(ent(SINGULAR + "/", 429, 100)),
    "hash": entries(ent(SINGULAR + "#x", 429, 100)),
    "id-in-query-of-other-path": entries(
        ent(f"{ORIGIN}/backend-api/me?conversation={ID}", 429, 100)
    ),
    "longer-id": entries(ent(SINGULAR + "0", 429, 100)),
    "id-prefix-suffix": entries(ent(SINGULAR + "-extra", 429, 100)),
    "shorter-id": entries(ent(SINGULAR[:-1], 429, 100)),
    "other-case": entries(
        ent(f"{ORIGIN}/backend-api/conversation/{ID.upper()}", 429, 100)
    ),
    "other-prefix": entries(
        ent(f"{ORIGIN}/backend-api/v2/conversation/{ID}", 429, 100),
        ent(f"{ORIGIN}/other/backend-api/conversation/{ID}", 429, 101),
    ),
    "foreign-origin": entries(
        ent(f"https://evil.example/backend-api/conversation/{ID}", 429, 100)
    ),
    "cdn-origin": entries(
        ent(f"https://cdn.oaistatic.com/backend-api/conversation/{ID}", 429, 100)
    ),
    "http-scheme": entries(
        ent(f"http://chatgpt.com/backend-api/conversation/{ID}", 429, 100)
    ),
    "origin-suffix": entries(
        ent(f"https://chatgpt.com.evil.example/backend-api/conversation/{ID}", 429, 100)
    ),
    "other-port": entries(
        ent(f"https://chatgpt.com:8443/backend-api/conversation/{ID}", 429, 100)
    ),
    "userinfo": entries(
        ent(f"https://chatgpt.com@evil.example/backend-api/conversation/{ID}", 429, 100)
    ),
    "scheme-relative": entries(
        ent(f"//chatgpt.com/backend-api/conversation/{ID}", 429, 100)
    ),
    "relative": entries(ent(f"/backend-api/conversation/{ID}", 429, 100)),
    "bogus-status-999": entries(ent(SINGULAR, 999, 100)),
    "bogus-status-zero": entries(ent(SINGULAR, 0, 100)),
    "bogus-status-negative": entries(ent(SINGULAR, -429, 100)),
    "bogus-status-fraction": entries(ent(SINGULAR, 429.5, 100)),
    "bogus-status-string": entries(ent(SINGULAR, '"429"', 100)),
    "bogus-status-nan": entries(ent(SINGULAR, "NaN", 100)),
    "bogus-status-null": entries(ent(SINGULAR, "null", 100)),
    "bogus-status-missing": entries(
        f'{{name:{json.dumps(SINGULAR)},entryType:"resource",startTime:1,responseEnd:100}}'
    ),
    "bogus-status-huge": entries(ent(SINGULAR, 42900, 100)),
    "garbage-entries": (
        f'[null, 7, "x", {{}}, {{name:7,responseStatus:429,responseEnd:5}}, '
        f"{ent(SINGULAR, 429, 100)}]"
    ),
    "non-blocking-statuses": entries(
        ent(SINGULAR, 404, 100), ent(SINGULAR, 304, 110), ent(PLURAL, 400, 120)
    ),
    "status-599": entries(ent(SINGULAR, 599, 100)),
    "status-600-bogus": entries(ent(SINGULAR, 600, 100)),
    "status-499": entries(ent(SINGULAR, 499, 100)),
    "status-402": entries(ent(SINGULAR, 402, 100)),
    "empty": "[]",
    "throws": "throw",
    "returns-null": "null",
    "returns-object": "({length:1})",
}

INIT = """
(() => {
  const spec = %s;
  const original = performance.getEntriesByType.bind(performance);
  performance.getEntriesByType = function (type) {
    if (type !== 'resource') return original(type);
    if (spec === 'throw') throw new Error('entries are not accessible');
    return spec;
  };
})();
"""

# Counters for every active primitive the script must never call.
SPY = """
(() => {
  window.__calls = [];
  const spy = (owner, name) => {
    const original = owner[name];
    if (typeof original !== 'function') return;
    owner[name] = function (...args) {
      window.__calls.push(name);
      return original.apply(this, args);
    };
  };
  spy(window, 'fetch');
  spy(XMLHttpRequest.prototype, 'open');
  spy(XMLHttpRequest.prototype, 'send');
  spy(navigator, 'sendBeacon');
  spy(performance, 'clearResourceTimings');
  spy(performance, 'setResourceTimingBufferSize');
  spy(history, 'pushState');
  spy(history, 'replaceState');
  spy(window, 'open');
  spy(window, 'scrollTo');
  spy(Element.prototype, 'click');
  spy(HTMLElement.prototype, 'focus');
})();
"""

# The browser's own favicon request may land in the buffer at any time; it is not the script's.
BUFFER = "performance.getEntriesByType('resource').filter(e => !e.name.endsWith('/favicon.ico')).length"

EXPECTED_ERROR = "conversation-sync-http-"


def blocked(result, code):
    return result.get("error") == EXPECTED_ERROR + str(code)


async def read_real(context, name, html, steps, dom_script):
    queue = {}
    for url, status in steps:
        queue.setdefault(url, []).append(status)

    def fulfill(route, request):
        if request.url == CHAT:
            return route.fulfill(body=html, content_type="text/html")
        statuses = queue.get(request.url)
        status = statuses.pop(0) if statuses else 200
        return route.fulfill(status=status, body="{}", content_type="application/json")

    page_ = await context.new_page()
    await page_.route("**/*", fulfill)
    await page_.add_init_script(SPY)
    await page_.goto(CHAT)
    for url, _ in steps:
        await page_.evaluate("u => fetch(u).then(r => r.text())", url)
    requests = []
    page_.on("request", lambda request: requests.append(request.url))
    before = await page_.evaluate(BUFFER)
    url_before = page_.url
    result = await page_.evaluate(dom_script)
    after = await page_.evaluate(BUFFER)
    calls = await page_.evaluate("window.__calls")
    # Only the spy bookkeeping fetches above are allowed to have happened.
    calls = calls[len(steps) :]
    passive = {
        "requests": requests,
        "buffer_before": before,
        "buffer_after": after,
        "calls": calls,
        "url_changed": page_.url != url_before,
    }
    await page_.close()
    return result, passive


async def read_stub(context, name, expression, dom_script):
    def fulfill(route, request):
        return route.fulfill(body=COMPLETE, content_type="text/html")

    page_ = await context.new_page()
    await page_.route("**/*", fulfill)
    await page_.add_init_script(
        INIT % ("'throw'" if expression == "throw" else expression)
    )
    await page_.add_init_script(SPY)
    await page_.goto(CHAT)
    requests = []
    page_.on("request", lambda request: requests.append(request.url))
    result = await page_.evaluate(dom_script)
    calls = await page_.evaluate("window.__calls")
    await page_.close()
    return result, {"requests": requests, "calls": calls}


async def flow(context, collect, dom_script):
    """collect_samples + project_snapshot across a cached complete DOM and later failures."""
    from agenttools_chatgpt.monitor import project_snapshot

    state = {"statuses": []}

    def fulfill(route, request):
        if request.url == CHAT:
            return route.fulfill(body=state["html"], content_type="text/html")
        return route.fulfill(
            status=state["statuses"].pop(0) if state["statuses"] else 200,
            body="{}",
            content_type="application/json",
        )

    async def fetch(page_, status):
        state["statuses"].append(status)
        await page_.evaluate("u => fetch(u).then(r => r.text())", SINGULAR)

    def snapshot(rows, previous, now):
        return project_snapshot(
            "home",
            {"watch:" + CHAT: "1"},
            [],
            rows,
            previous,
            {"connection": "connected", "heartbeat_at": now - 5},
            now,
        )

    def only(snap):
        return next(s for s in snap["sessions"] if s["chat"] == CHAT)

    out = {}
    for label, html in (("complete", COMPLETE), ("working", WORKING)):
        state["html"] = html
        page_ = await context.new_page()
        await page_.route("**/*", fulfill)
        await page_.goto(CHAT)
        cache = {}
        # 1. A good read: 200 for this conversation, DOM complete (or working).
        await fetch(page_, 200)
        rows = await collect([page_], None, cache, NOW)
        first = snapshot(rows, None, NOW)
        # 2. Same cached DOM, but the newest exact conversation read is now 429.
        await fetch(page_, 429)
        rows = await collect([page_], first, cache, NOW + 400)
        blocked_snap = snapshot(rows, first, NOW + 400)
        # 3. A newer 200 supersedes the older 429.
        await fetch(page_, 200)
        rows = await collect([page_], blocked_snap, cache, NOW + 800)
        cleared = snapshot(rows, blocked_snap, NOW + 800)
        # 4. A newer 429 after that 200 blocks again.
        await fetch(page_, 429)
        rows = await collect([page_], cleared, cache, NOW + 1200)
        again = snapshot(rows, cleared, NOW + 1200)
        out[label] = {
            "first": only(first),
            "blocked": only(blocked_snap),
            "blocked_rows": rows,
            "cleared": only(cleared),
            "again": only(again),
            "cache_after_block": dict(cache),
        }
        await page_.close()
    return out


async def run():
    from agenttools_chatgpt.monitor_observer import DOM_SCRIPT, collect_samples
    from playwright.async_api import async_playwright

    results, passive, flows = {}, {}, {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=shutil.which("google-chrome")
        )
        try:
            context = await browser.new_context()
            for name, (html, steps) in REAL.items():
                results[name], passive[name] = await read_real(
                    context, name, html, steps, DOM_SCRIPT
                )
            for name, expression in STUB.items():
                results["stub:" + name], passive["stub:" + name] = await read_stub(
                    context, name, expression, DOM_SCRIPT
                )
            flows = await flow(context, collect_samples, DOM_SCRIPT)
        finally:
            await browser.close()
    return results, passive, flows


@pytest.fixture(scope="module")
def outcome():
    pytest.importorskip("playwright.async_api")
    if not shutil.which("google-chrome"):
        pytest.skip("Local fixture Chrome required")
    return asyncio.run(run())


def result_of(outcome, name):
    return outcome[0][name]


def assert_complete(result):
    assert not result.get("error"), result
    assert result["status"] == "complete"
    assert result["conversation_id"] == ID


@pytest.mark.parametrize(
    "name,code",
    [
        ("singular-429", 429),
        ("plural-429", 429),
        ("singular-401", 401),
        ("plural-403", 403),
        ("singular-500", 500),
        ("plural-503", 503),
        ("200-then-newer-429", 429),
        ("429-200-429", 429),
        ("own-429-other-chat-200", 429),
    ],
)
def test_latest_failed_exact_conversation_read_is_an_explicit_error(
    outcome, name, code
):
    result = result_of(outcome, name)
    assert blocked(result, code), result
    # Not a successful observation: no status, no turns, no proof of freshness.
    for key in (
        "status",
        "running",
        "turns",
        "turn",
        "progress_token",
        "checked_at",
        "conversation_id",
    ):
        assert key not in result, key
    assert str(code) in result["error_detail"]
    assert result["conversation_sync"]["status"] == code
    assert result["conversation_sync"]["ended_ms_ago"] >= 0


def test_error_output_is_sanitized(outcome):
    # The page title is an existing, display-only field of every error row; message content is not.
    for name in ("singular-429", "plural-503", "no-root-429", "wrong-root-429"):
        text = json.dumps(result_of(outcome, name))
        for leak in (
            ID,
            OTHER_ID,
            "chatgpt.com",
            "backend-api",
            "https:",
            "?",
            "Answer",
        ):
            assert leak not in text, (name, leak, text)


@pytest.mark.parametrize(
    "name", ["older-429-then-200", "older-429-then-plural-200", "singular-200", "none"]
)
def test_older_failure_is_superseded_by_a_newer_success(outcome, name):
    assert_complete(result_of(outcome, name))


@pytest.mark.parametrize(
    "name",
    [
        "other-chat-429",
        "other-chat-newer-429-after-own-200",
        "unrelated-telemetry-429",
    ],
)
def test_other_chats_and_unrelated_telemetry_never_block(outcome, name):
    assert_complete(result_of(outcome, name))


def test_good_read_records_passive_sync_metadata(outcome):
    sync = result_of(outcome, "singular-200")["conversation_sync"]
    assert sync["status"] == 200 and sync["ended_ms_ago"] >= 0
    assert "conversation_sync" in result_of(outcome, "none")
    assert result_of(outcome, "none")["conversation_sync"].get("status") is None


def test_same_turn_working_is_never_completed_under_a_failed_sync(outcome):
    ok = result_of(outcome, "working-200")
    assert ok["status"] == "working" and ok["running"] is True
    bad = result_of(outcome, "working-429")
    assert blocked(bad, 429)
    assert "status" not in bad and "running" not in bad and "turns" not in bad


@pytest.mark.parametrize(
    "name,detail",
    [("no-root-429", "root-missing"), ("wrong-root-429", "root-attribute-mismatch")],
)
def test_unverified_root_with_failed_sync_is_helpful(outcome, name, detail):
    result = result_of(outcome, name)
    assert blocked(result, 429), result
    assert detail in result["error_detail"] and "429" in result["error_detail"]
    assert "root_candidates" in result


def test_unverified_root_without_failed_sync_keeps_the_existing_refusal(outcome):
    for name, detail in (
        ("no-root-200", "root-missing"),
        ("no-root-none", "root-missing"),
    ):
        result = result_of(outcome, name)
        assert result["error"] == "conversation-root-unverified"
        assert result["error_detail"] == detail


@pytest.mark.parametrize(
    "name",
    [
        "encoded-id",
        "encoded-slash",
        "dot-segments",
        "double-slash",
        "trailing-slash",
        "hash",
        "id-in-query-of-other-path",
        "longer-id",
        "id-prefix-suffix",
        "shorter-id",
        "other-case",
        "other-prefix",
        "foreign-origin",
        "cdn-origin",
        "http-scheme",
        "origin-suffix",
        "other-port",
        "userinfo",
        "scheme-relative",
        "relative",
        "bogus-status-999",
        "bogus-status-zero",
        "bogus-status-negative",
        "bogus-status-fraction",
        "bogus-status-string",
        "bogus-status-nan",
        "bogus-status-null",
        "bogus-status-missing",
        "bogus-status-huge",
        "status-600-bogus",
        "status-499",
        "status-402",
        "non-blocking-statuses",
        "incomplete-429-does-not-block",
        "nan-end-ignored",
        "empty",
        "throws",
        "returns-null",
        "returns-object",
    ],
)
def test_ambiguous_foreign_or_bogus_entries_do_not_block(outcome, name):
    assert_complete(result_of(outcome, "stub:" + name))


@pytest.mark.parametrize(
    "name,code",
    [
        ("tie-later-wins-429", 429),
        ("tie-later-wins-200", None),
        ("response-end-orders-429-last", 429),
        ("response-end-orders-not-array", None),
        ("incomplete-newer-does-not-clear", 429),
        ("bogus-newer-does-not-clear", 429),
        ("garbage-entries", 429),
        ("status-599", 599),
    ],
)
def test_newest_completed_relevant_entry_decides(outcome, name, code):
    result = result_of(outcome, "stub:" + name)
    if code is None:
        assert_complete(result)
    else:
        assert blocked(result, code), result


def test_dom_script_stays_passive(outcome):
    for name, info in outcome[1].items():
        scripted = [u for u in info["requests"] if not u.endswith("/favicon.ico")]
        assert scripted == [], (name, scripted)
        assert info["calls"] == [], (name, info["calls"])
    for name, info in outcome[1].items():
        if not name.startswith("stub:"):
            assert info["buffer_before"] == info["buffer_after"], name
            assert not info["url_changed"], name


def test_cached_complete_dom_with_latest_429_projects_unknown_error(outcome):
    flows = outcome[2]["complete"]
    first = flows["first"]
    assert first["observation"] == "current" and first["status"] == "complete"
    assert first["last_checked_at"] == NOW
    row = flows["blocked"]
    assert row["observation"] == "error"
    assert row["error"] == "conversation-sync-http-429"
    assert "429" in row["error_detail"]
    assert row["status"] == "unknown" and row["running"] is None
    assert row["reason"] == "observation-error"
    # The failed read neither refreshes the proof nor leaves a cached success behind.
    assert row["last_checked_at"] == NOW
    assert row["last_error_at"] == NOW + 400
    assert flows["cache_after_block"] == {}
    assert row["next_check_at"] > NOW + 400
    sample = flows["blocked_rows"][0]
    assert "checked_at" not in sample and "status" not in sample


def test_flow_older_429_then_200_clears_and_newer_429_blocks_again(outcome):
    flows = outcome[2]["complete"]
    cleared = flows["cleared"]
    assert cleared["observation"] == "current" and cleared["status"] == "complete"
    assert cleared["error"] == "" and cleared["last_checked_at"] == NOW + 800
    again = flows["again"]
    assert (
        again["observation"] == "error"
        and again["error"] == "conversation-sync-http-429"
    )
    assert again["status"] == "unknown" and again["last_checked_at"] == NOW + 800


def test_flow_working_turn_never_becomes_complete_under_429(outcome):
    flows = outcome[2]["working"]
    assert flows["first"]["status"] == "working" and flows["first"]["running"] is True
    for label in ("blocked", "again"):
        row = flows[label]
        assert row["observation"] == "error"
        assert row["status"] == "unknown" and row["running"] is None
        assert row["continuation"] != "completed"
        assert row["completion_observed_at"] is None
    assert flows["cleared"]["status"] == "working"


def test_injected_script_still_gets_the_shared_conversation_root_helper():
    from agenttools_chatgpt import scripts

    for name in ("monitor_dom.js", "live_dom.js"):
        script = scripts.load(name)
        assert "/*@conversation-root*/" not in script
        assert script.count("const conversationRoot =") == 1
        assert script.count("const thinkingFailure =") == 1


@pytest.mark.parametrize("name", ["real-query-429", "real-query-200-then-429"])
def test_real_query_keys_on_the_exact_path_block(outcome, name):
    result = result_of(outcome, name)
    assert blocked(result, 429), result
    assert "num_turns" not in json.dumps(result) and "include_has" not in json.dumps(
        result
    )


def test_real_query_latest_200_recovers(outcome):
    assert_complete(result_of(outcome, "real-query-429-then-200"))


@pytest.mark.parametrize(
    "name", ["mismatched-path-with-target-id-query", "foreign-path-with-real-query"]
)
def test_chat_id_in_a_query_never_identifies_the_chat(outcome, name):
    assert_complete(result_of(outcome, name))


def test_explicit_load_failure_ui_is_an_error_without_performance_data(outcome):
    result = result_of(outcome, "load-error-empty")
    assert result["error"] == "conversation-load-failed", result
    assert result["error_detail"] == "load-error-ui"
    assert "status" not in result and "turns" not in result


def test_empty_chat_without_error_text_keeps_compatible_behavior(outcome):
    result = result_of(outcome, "empty-no-error")
    assert not result.get("error"), result
    assert result["turns"] == []


@pytest.mark.parametrize(
    "name",
    ["quoted-error-in-turns", "error-in-composer-draft", "error-in-sidebar-title"],
)
def test_error_text_in_content_draft_or_sidebar_is_ignored(outcome, name):
    assert result_of(outcome, name).get("error") is None

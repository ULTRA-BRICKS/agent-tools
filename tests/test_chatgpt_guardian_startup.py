"""Startup restore opens only missing enrolled chats; stdlib fakes, no real browser."""

import hashlib
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import live, startup
from agenttools_chatgpt.startup import (
    Browser,
    ChromeDevTools,
    Listener,
    StartupError,
    UncertainCreation,
    profile_path,
    restore,
)

UUID = "12345678-1234-1234-1234-123456789abc"
ONE = "https://chatgpt.com/c/" + UUID
TWO = "https://chatgpt.com/c/two"
PROJECT_ONE = "https://chatgpt.com/g/g-p-" + "a" * 32 + "-proj/c/" + UUID


@pytest.fixture(autouse=True)
def ample_memory(monkeypatch):
    """Restore shares the sender's memory guard; pin the host reading."""
    monkeypatch.setattr(live, "available_memory", lambda: 8 * 1024**3)


def make_state(path, account="home", chats=(ONE, TWO), unwatched=(), consent=()):
    """Real Guardian schema: enrollment = controls watch:<chat>='1'; consent is separate."""
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE account_scope(account TEXT PRIMARY KEY);"
            "CREATE TABLE consent(chat TEXT PRIMARY KEY);"
            "CREATE TABLE controls(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        db.execute("INSERT INTO account_scope VALUES (?)", (account,))
        db.executemany(
            "INSERT INTO controls VALUES (?, '1')", [("watch:" + c,) for c in chats]
        )
        db.executemany(
            "INSERT INTO controls VALUES (?, '0')", [("watch:" + c,) for c in unwatched]
        )
        db.execute("INSERT INTO controls VALUES ('astra_quota', '1')")
        db.executemany("INSERT INTO consent VALUES (?)", [(c,) for c in consent])
    return path


class FakeProcesses:
    def __init__(self, workspace="home", home=Path("/h"), port=None, **overrides):
        port = startup.PORTS[workspace] if port is None else port
        args = (
            "/opt/google/chrome/chrome",
            "--user-data-dir=" + str(profile_path(workspace, home)),
            "--remote-debugging-port=" + str(port),
        )
        self._browsers = overrides.get("browsers", [Browser(10, args)])
        self._listeners = overrides.get("listeners", [Listener("127.0.0.1", 10)])

    def browsers(self):
        return self._browsers

    def listeners(self, port):
        return self._listeners


class FakeDevTools:
    def __init__(self, urls=(), fail_create=None):
        self.pages_now = [{"type": "page", "url": u} for u in urls]
        self.created = []
        self.fail_create = fail_create

    def pages(self, timeout):
        return list(self.pages_now)

    def create(self, url, timeout):
        self.created.append(url)
        if self.fail_create:
            raise self.fail_create
        self.pages_now.append({"type": "page", "url": url})


def run(tmp_path, devtools, *, apply=True, processes=None, **extra):
    state = extra.pop("state", None) or make_state(tmp_path / "s.sqlite")
    if "clock" not in extra:
        ticks = iter(x * 0.5 for x in range(100000))
        extra["clock"] = ticks.__next__
    return restore(
        "home",
        apply=apply,
        state=state,
        processes=processes or FakeProcesses(),
        devtools=devtools,
        home=Path("/h"),
        sleep=lambda _s: None,
        **extra,
    )


def test_existing_tabs_are_noop(tmp_path):
    dev = FakeDevTools([ONE, TWO])
    result = run(tmp_path, dev)
    assert dev.created == [] and result["opened"] == []


def test_only_missing_chat_opened_once(tmp_path):
    dev = FakeDevTools([ONE])
    result = run(tmp_path, dev)
    assert dev.created == [TWO] and result["opened"] == ["two"]


def test_project_url_counts_as_present(tmp_path):
    dev = FakeDevTools([PROJECT_ONE, TWO])
    run(tmp_path, dev)
    assert dev.created == []


def test_repeated_run_is_noop(tmp_path):
    dev = FakeDevTools([])
    state = make_state(tmp_path / "s.sqlite")
    run(tmp_path, dev, state=state)
    created = list(dev.created)
    run(tmp_path, dev, state=state)
    assert created == [ONE, TWO] and dev.created == created


def test_dry_run_mutates_nothing(tmp_path):
    dev = FakeDevTools([ONE])
    result = run(tmp_path, dev, apply=False)
    assert dev.created == [] and result["missing"] == ["two"] and result["opened"] == []


def test_watch_rows_enroll_with_empty_consent_table(tmp_path):
    state = make_state(tmp_path / "s.sqlite")
    assert startup.enrolled_chats(state, "home") == sorted([ONE, TWO])


def test_unwatched_and_consent_only_chats_not_enrolled(tmp_path):
    other = "https://chatgpt.com/c/other"
    state = make_state(
        tmp_path / "s.sqlite",
        chats=(ONE,),
        unwatched=(TWO,),
        consent=(other,),
    )
    assert startup.enrolled_chats(state, "home") == [ONE]


def test_account_mismatch_fails_closed(tmp_path):
    state = make_state(tmp_path / "s.sqlite", account="glide.vc")
    dev = FakeDevTools([])
    with pytest.raises(StartupError, match="different account"):
        run(tmp_path, dev, state=state)
    assert dev.created == []


def test_missing_database_not_created(tmp_path):
    missing = tmp_path / "none.sqlite"
    with pytest.raises(StartupError, match="does not exist"):
        run(tmp_path, FakeDevTools([]), state=missing)
    assert not missing.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"browsers": []},
        {
            "browsers": [
                Browser(
                    10,
                    (
                        "chrome",
                        "--user-data-dir=/other",
                        "--remote-debugging-port=9224",
                    ),
                )
            ]
        },
        {"listeners": []},
        {"listeners": [Listener("127.0.0.1", 99)]},
        {"listeners": [Listener("0.0.0.0", 10)]},
        {"listeners": [Listener("127.0.0.1", None)]},
        {"listeners": [Listener("127.0.0.1", None, os.getuid() + 1)]},
    ],
)
def test_no_owned_process_or_unrelated_listener_refused(tmp_path, overrides):
    dev = FakeDevTools([])
    with pytest.raises(StartupError):
        run(tmp_path, dev, processes=FakeProcesses(**overrides))
    assert dev.created == []


def test_unreadable_socket_owner_accepted_only_for_our_uid(tmp_path):
    """Live Chrome denies /proc/PID/fd, so the owner pid is None; uid is the evidence."""
    dev = FakeDevTools([ONE])
    ours = Listener("127.0.0.1", None, os.getuid())
    run(tmp_path, dev, processes=FakeProcesses(listeners=[ours]))


def test_duplicate_owned_browsers_refused(tmp_path):
    first = FakeProcesses()
    two = FakeProcesses(
        browsers=first._browsers + [Browser(11, first._browsers[0].args)]
    )
    dev = FakeDevTools([])
    with pytest.raises(StartupError, match="exactly one"):
        run(tmp_path, dev, processes=two)
    assert dev.created == []


def test_renderer_children_do_not_count_as_duplicates(tmp_path):
    base = FakeProcesses()
    child = Browser(11, base._browsers[0].args + ("--type=renderer",))
    dev = FakeDevTools([ONE, TWO])
    run(tmp_path, dev, processes=FakeProcesses(browsers=base._browsers + [child]))


def test_duplicate_existing_tabs_refused(tmp_path):
    dev = FakeDevTools([ONE, PROJECT_ONE])
    with pytest.raises(StartupError, match="duplicate"):
        run(tmp_path, dev)
    assert dev.created == []


def test_auxiliary_page_budget_refuses_restore(tmp_path):
    dev = FakeDevTools([ONE] + ["about:blank"] * 5)
    with pytest.raises(StartupError, match="auxiliary-budget"):
        run(tmp_path, dev)
    assert dev.created == []


def test_external_pages_count_against_the_auxiliary_budget(tmp_path):
    dev = FakeDevTools(
        [
            ONE,
            "https://chatgpt.com/space?tab=all",
            "https://example.com/",
            "about:blank",
        ]
    )
    with pytest.raises(StartupError, match="auxiliary-budget"):
        run(tmp_path, dev)
    assert dev.created == []


def test_conversation_budget_refuses_restore(tmp_path):
    others = [f"https://chatgpt.com/c/other-{i}" for i in range(5)]
    dev = FakeDevTools([ONE] + others)
    with pytest.raises(StartupError, match="conversation-budget"):
        run(tmp_path, dev)
    assert dev.created == []


def test_space_tab_does_not_consume_the_conversation_budget(tmp_path):
    others = [f"https://chatgpt.com/c/other-{i}" for i in range(4)]
    dev = FakeDevTools([ONE, "https://chatgpt.com/space?tab=all"] + others)
    result = run(tmp_path, dev)
    assert dev.created == [TWO] and result["opened"] == ["two"]


class LoadingTabsDevTools(FakeDevTools):
    """Every created tab still shows about:blank while the next creation is considered."""

    def create(self, url, timeout):
        self.created.append(url)
        self.pages_now.append({"type": "page", "url": "about:blank"})


def test_tabs_created_in_this_run_still_loading_count_toward_the_ceiling(tmp_path):
    others = [f"https://chatgpt.com/c/other-{i}" for i in range(4)]
    state = make_state(
        tmp_path / "s.sqlite", chats=(ONE, TWO, "https://chatgpt.com/c/three")
    )
    dev = LoadingTabsDevTools(others)
    with pytest.raises(StartupError, match="conversation-budget"):
        run(tmp_path, dev, state=state)
    assert dev.created == [ONE, "https://chatgpt.com/c/three"]


def test_a_pending_restored_tab_never_discounts_real_auxiliary_tabs(tmp_path):
    dev = LoadingTabsDevTools(
        ["https://chatgpt.com/space?tab=all", "https://example.com/"]
    )
    state = make_state(tmp_path / "s.sqlite", chats=(ONE, TWO))
    with pytest.raises(StartupError, match="auxiliary-budget"):
        run(tmp_path, dev, state=state)
    assert dev.created == [ONE]


class ConcurrentTabDevTools(FakeDevTools):
    """A created tab stays blank and an unrelated conversation tab opens at the same time."""

    def create(self, url, timeout):
        self.created.append(url)
        self.pages_now.append({"type": "page", "url": "about:blank"})
        self.pages_now.append(
            {"type": "page", "url": "https://chatgpt.com/c/opened-meanwhile"}
        )


def test_unrelated_new_conversation_does_not_release_a_pending_restored_tab(tmp_path):
    others = [f"https://chatgpt.com/c/other-{i}" for i in range(4)]
    state = make_state(tmp_path / "s.sqlite", chats=(ONE, TWO))
    dev = ConcurrentTabDevTools(others)
    with pytest.raises(StartupError, match="conversation-budget"):
        run(tmp_path, dev, state=state)
    assert dev.created == [ONE]


@pytest.mark.parametrize(
    "memory, reason", [(1024**2, "low-memory"), (None, "memory-unavailable")]
)
def test_restore_is_refused_under_the_memory_guard(
    tmp_path, monkeypatch, memory, reason
):
    monkeypatch.setattr(live, "available_memory", lambda: memory)
    dev = FakeDevTools([ONE])
    with pytest.raises(StartupError, match=reason):
        run(tmp_path, dev)
    assert dev.created == []


def test_timeout_never_retried(tmp_path):
    dev = FakeDevTools([], fail_create=UncertainCreation("timeout"))
    with pytest.raises(UncertainCreation):
        run(tmp_path, dev)
    assert dev.created == [ONE]


def test_unsettled_inventory_refuses_creation(tmp_path):
    class Churning(FakeDevTools):
        n = 0

        def pages(self, timeout):
            self.n += 1
            return [{"id": str(self.n), "type": "page", "url": "about:blank"}]

    clock = iter(range(1000)).__next__
    dev = Churning()
    with pytest.raises(StartupError, match="settle"):
        run(tmp_path, dev, clock=lambda: float(clock()), settle=3, interval=1)
    assert dev.created == []


def test_deadline_enforced(tmp_path):
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    with pytest.raises(StartupError, match="deadline"):
        run(tmp_path, FakeDevTools([]), clock=lambda: next(ticks))


def test_read_only_database_unchanged(tmp_path):
    state = make_state(tmp_path / "s.sqlite")
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    run(tmp_path, FakeDevTools([ONE]), state=state)
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
    assert not list(tmp_path.glob("s.sqlite-*"))


CHROME = "/opt/google/chrome/chrome"


def fake_proc(tmp_path, pid, cmdline: bytes, exe=CHROME):
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(cmdline)
    if exe:
        (entry / "exe").symlink_to(exe)
    return tmp_path / "proc"


def owned_args(home="/h"):
    return [
        "--user-data-dir=" + str(profile_path("home", Path(home))),
        "--remote-debugging-port=9224",
    ]


def test_proc_single_rewritten_string_cmdline_is_split(tmp_path):
    """Chrome rewrites argv into ONE NUL-terminated string holding the whole command."""
    line = " ".join([CHROME] + owned_args()) + "\0"
    root = fake_proc(tmp_path, 10, line.encode())
    found = startup.ProcFilesystem(root).browsers()
    assert found == [Browser(10, tuple([CHROME] + owned_args()))]
    assert (
        startup.verify_browser(
            "home",
            type(
                "P",
                (),
                {
                    "browsers": lambda s: found,
                    "listeners": lambda s, p: [Listener("127.0.0.1", 10)],
                },
            )(),
            Path("/h"),
        )
        == 10
    )


def test_proc_single_string_with_quoted_path(tmp_path):
    prof = "--user-data-dir=/h/with space/p"
    line = CHROME + " '" + prof + "' --remote-debugging-port=9224\0"
    root = fake_proc(tmp_path, 11, line.encode())
    (browser,) = startup.ProcFilesystem(root).browsers()
    assert browser.args == (CHROME, prof, "--remote-debugging-port=9224")


def test_proc_normal_nul_delimited_cmdline_still_works(tmp_path):
    raw = "\0".join([CHROME] + owned_args()) + "\0"
    root = fake_proc(tmp_path, 12, raw.encode())
    (browser,) = startup.ProcFilesystem(root).browsers()
    assert browser.args == tuple([CHROME] + owned_args())


@pytest.mark.parametrize("exe", ["/usr/bin/python3", "/tmp/evil/chrome-fake", None])
def test_proc_non_chrome_executable_impostor_excluded(tmp_path, exe):
    line = " ".join([CHROME] + owned_args()) + "\0"
    root = fake_proc(tmp_path, 13, line.encode(), exe=exe)
    assert startup.ProcFilesystem(root).browsers() == []


@pytest.mark.parametrize(
    "comm,argv0,accepted",
    [
        ("chrome", CHROME, True),
        ("python3", CHROME, False),
        ("chrome", "/usr/bin/python3", False),
        (None, CHROME, False),
    ],
)
def test_proc_unreadable_exe_falls_back_to_comm_and_argv0(
    tmp_path, monkeypatch, comm, argv0, accepted
):
    """A live Chrome denies /proc/PID/exe (EACCES) to its own user; any other failure refuses."""
    line = " ".join([argv0] + owned_args()) + "\0"
    root = fake_proc(tmp_path, 16, line.encode(), exe=None)
    if comm:
        (root / "16" / "comm").write_text(comm + "\n")
    real = os.readlink

    def denied(path, *a, **kw):
        if str(path).endswith("/16/exe"):
            raise PermissionError(13, "denied")
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "readlink", denied)
    assert bool(startup.ProcFilesystem(root).browsers()) is accepted


@pytest.mark.parametrize(
    "exe", ["/opt/google/chrome/chrome", "/usr/bin/chromium", "/snap/chromium/1/chrome"]
)
def test_proc_real_chrome_executables_accepted(tmp_path, exe):
    root = fake_proc(
        tmp_path, 14, (" ".join([exe] + owned_args()) + "\0").encode(), exe=exe
    )
    assert len(startup.ProcFilesystem(root).browsers()) == 1


def test_proc_renderer_not_owned_browser(tmp_path):
    line = " ".join([CHROME, "--type=renderer"] + owned_args()) + "\0"
    root = fake_proc(tmp_path, 15, line.encode())
    (renderer,) = startup.ProcFilesystem(root).browsers()
    related = [a for a in renderer.args if a.startswith("--type=")]
    assert related == ["--type=renderer"]
    with pytest.raises(startup.NotReady):
        startup.verify_browser(
            "home",
            type(
                "P",
                (),
                {"browsers": lambda s: [renderer], "listeners": lambda s, p: []},
            )(),
            Path("/h"),
        )


class Server:
    def __init__(self, handler):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def handler(status=200, body=b"[]", location=None, seen=None):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def respond(self):
            if seen is not None:
                seen.append((self.command, self.path))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_PUT = respond

    return H


def test_real_client_lists_and_creates_with_encoded_url():
    seen = []
    with Server(handler(body=b"[]", seen=seen)) as server:
        client = ChromeDevTools(server.port)
        assert client.pages(2) == []
        client.create(ONE, 2)
    assert seen == [
        ("GET", "/json/list"),
        ("PUT", "/json/new?https%3A%2F%2Fchatgpt.com%2Fc%2F" + UUID),
    ]


def test_redirect_refused_not_followed():
    seen = []
    with (
        Server(
            handler(status=302, location="http://127.0.0.1:1/x", seen=seen)
        ) as server,
        pytest.raises(StartupError, match="HTTP 302"),
    ):
        ChromeDevTools(server.port).pages(2)
    assert seen == [("GET", "/json/list")]


def test_oversized_response_refused():
    big = handler(body=b" " * (startup.MAX_BODY_BYTES + 10))
    with Server(big) as server, pytest.raises(StartupError, match="size limit"):
        ChromeDevTools(server.port).pages(2)


def test_creation_redirect_is_uncertain():
    redirect = handler(status=307, location="http://127.0.0.1:1/")
    with Server(redirect) as server, pytest.raises(UncertainCreation):
        ChromeDevTools(server.port).create(ONE, 2)


def test_creation_connection_failure_is_uncertain():
    with pytest.raises(UncertainCreation):
        ChromeDevTools(1).create(ONE, 1)


def test_main_dry_run_json_has_no_chat_text(tmp_path, capsys):
    state = make_state(tmp_path / "s.sqlite")
    code = startup.main(
        ["--workspace", "home"],
        processes=FakeProcesses(),
        devtools=FakeDevTools([ONE]),
        state=state,
        home=Path("/h"),
    )
    out = json.loads(capsys.readouterr().out)
    assert (
        code == 0 and out["ok"] and out["apply"] is False and out["missing"] == ["two"]
    )


def test_main_cdp_port_override_matches_relocated_browser(tmp_path, capsys):
    state = make_state(tmp_path / "s.sqlite")
    devtools = FakeDevTools([ONE])
    code = startup.main(
        ["--workspace", "home", "--apply", "--cdp-port", "9226"],
        processes=FakeProcesses(port=9226),
        devtools=devtools,
        state=state,
        home=Path("/h"),
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["opened"] == ["two"] and devtools.created == [TWO]


def test_default_port_refuses_relocated_browser(tmp_path, capsys):
    state = make_state(tmp_path / "s.sqlite")
    code = startup.main(
        ["--workspace", "home", "--apply"],
        processes=FakeProcesses(port=9226),
        devtools=FakeDevTools([ONE]),
        state=state,
        home=Path("/h"),
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "does not match the owned profile and port" in out["error"]


def test_main_failure_exit_code(tmp_path, capsys, monkeypatch):
    ticks = iter(x * 5.0 for x in range(1000))
    monkeypatch.setattr(startup.time, "monotonic", ticks.__next__)
    monkeypatch.setattr(startup.time, "sleep", lambda _s: None)
    code = startup.main(
        ["--workspace", "home", "--apply"],
        processes=FakeProcesses(browsers=[]),
        devtools=FakeDevTools([]),
        state=make_state(tmp_path / "s.sqlite"),
        home=Path("/h"),
    )
    assert code == 1 and json.loads(capsys.readouterr().out)["ok"] is False


def test_help_runs_offline():
    lib = Path(__file__).resolve().parents[1] / "lib"
    out = subprocess.run(
        [sys.executable, "-m", "agenttools_chatgpt.startup", "--help"],
        check=False,
        cwd=lib,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (
        out.returncode == 0 and "--workspace" in out.stdout and "--apply" in out.stdout
    )


def test_waits_for_browser_to_start_listening(tmp_path):
    class Late(FakeProcesses):
        calls = 0

        def listeners(self, port):
            self.calls += 1
            return [] if self.calls < 3 else [Listener("127.0.0.1", 10)]

    dev = FakeDevTools([ONE])
    run(tmp_path, dev, processes=Late())
    assert dev.created == [TWO]


def test_never_ready_browser_times_out_without_creation(tmp_path):
    ticks = iter(float(i) for i in range(1000)).__next__
    dev = FakeDevTools([])
    with pytest.raises(StartupError, match="not ready"):
        run(
            tmp_path,
            dev,
            processes=FakeProcesses(listeners=[]),
            clock=ticks,
            deadline=5,
        )
    assert dev.created == []


@pytest.mark.parametrize("port", ["80", "70000", "abc"])
def test_cdp_port_out_of_range_is_a_short_usage_error(port, capsys):
    with pytest.raises(SystemExit) as raised:
        startup.main(["--workspace", "home", "--cdp-port", port])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert ("port out of range" in err or "not a port number" in err) and len(err) < 600


def test_lifecycle_templates_use_only_the_documented_tokens():
    systemd = Path(__file__).resolve().parents[1] / "lib/agenttools_chatgpt/systemd"
    templates = sorted(systemd.glob("*/lifecycle.conf.template"))
    assert len(templates) == 2
    for template in templates:
        tokens = set(re.findall(r"@[A-Z_]+@", template.read_text(encoding="utf-8")))
        assert tokens == {"@VENV_PYTHON@", "@CDP_PORT@"}, template


def test_skipped_chat_is_deferred_not_opened(tmp_path):
    dev = FakeDevTools([ONE])
    result = run(tmp_path, dev, skip=frozenset([TWO]))
    assert dev.created == [] and result["opened"] == []
    assert result["deferred"] == ["two"] and result["already_open"] == 1

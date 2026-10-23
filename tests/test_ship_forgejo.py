"""Tests for the Forgejo provider of ci/ship/ship.sh (ci/ship/forgejo_provider.py).

Hermetic: a loopback fake Forgejo (synthetic API responses shaped after the Forgejo 16.0.3
Swagger) that also serves the test's bare origin repo over git's dumb HTTP protocol, so
ship.sh's own `git fetch origin` calls work against an `http://127.0.0.1:<port>/o/r.git`
origin. Pushes go to a local pushurl. A poison `gh` on PATH proves the real gh is never
reached on the Forgejo path. No real PR is ever touched.

    uv run --with pytest python -m pytest tests/test_ship_forgejo.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import base64
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "ci" / "ship" / "ship.sh"
SHIPLIB = ROOT / "ci" / "ship" / "shiplib"
PROVIDER = ROOT / "ci" / "ship" / "forgejo_provider.py"
PLACEHOLDER = "YOUR_TOKEN_HERE"  # obviously fake credential for the synthetic key store

sys.path.insert(0, str(ROOT / "ci" / "ship"))
from shiplib import accept_merge  # noqa: E402


def _load_provider():
    spec = importlib.util.spec_from_file_location("forgejo_provider", PROVIDER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


fp = _load_provider()


def _keys(host: str, value: str = PLACEHOLDER, **extra) -> str:
    return json.dumps({"hosts": {host: {"type": "Application", "name": "t", "token": value, **extra}}})


# --- fake Forgejo -----------------------------------------------------------------------------


class FakeForgejo:
    """Synthetic Forgejo API + dumb-HTTP git for one repo `o/r`, PR #1."""

    def __init__(self, bare: Path | None = None):
        self.bare = bare
        self.requests: list[dict] = []
        self.state: dict = {
            "version_status": 200, "pr": {}, "main_sha": "", "statuses": {}, "reviews": [],
            "review_comments": {}, "issue_comments": [], "timeline": [], "files": ["docs/x.md"], "commits": [],
            "branch_protection": {"enable_status_check": False, "status_check_contexts": []},
            "merge_head_override": None, "redirect_to": None, "pr_get_status_after_merge": None,
            "runs": [], "run_jobs": {},
            "workflow_dirs": [".github/workflows"],
            "workflow_contents": {".github/workflows/tests.yml": "name: tests\non: [push, pull_request]\n"},
            "branches": {}, "open_prs": [], "closed_prs": [], "delete_status": None, "pull_refs": {},
            "branch_listing": None, "branch_get_status": {},
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def host_key(self) -> str:
        return f"127.0.0.1:{self.port}"

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _dispatch(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                parts = urlsplit(self.path)
                fake.requests.append({"method": method, "path": parts.path, "query": parts.query,
                                      "auth": self.headers.get("Authorization"), "body": body})
                status, payload, ctype = fake.route(method, parts.path, parse_qs(parts.query), body)
                if status in (301, 302, 307):
                    self.send_response(status)
                    self.send_header("Location", payload)
                    self.end_headers()
                    return
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_PATCH(self):
                self._dispatch("PATCH")

            def do_DELETE(self):
                self._dispatch("DELETE")
        return H

    def route(self, method, path, query, body):  # noqa: C901 - flat routing table
        s = self.state
        page = int((query.get("page") or ["1"])[0])
        if path.startswith("/o/r.git/") and self.bare is not None:
            f = self.bare / path[len("/o/r.git/"):]
            return (200, f.read_bytes(), "application/octet-stream") if f.is_file() else (404, b"", "text/plain")
        if s["redirect_to"]:
            return 302, s["redirect_to"], "text/plain"
        if path == "/api/forgejo/v1/version":
            return s["version_status"], {"version": "16.0.3"}, "application/json"
        p = "/api/v1/repos/o/r"
        if not path.startswith(p):
            return 404, {"message": "not found"}, "application/json"
        sub = path[len(p):]

        if sub.startswith("/contents/") and method == "GET":
            item = sub[len("/contents/"):]
            if item in s["workflow_dirs"]:
                return 200, [{"type": "file", "path": f} for f in s["workflow_contents"] if f.startswith(item + "/")], "application/json"
            if item in s["workflow_contents"]:
                return 200, {"type": "file", "path": item, "encoding": "base64",
                             "content": base64.b64encode(s["workflow_contents"][item].encode()).decode()}, "application/json"
            return 404, {"message": "not found"}, "application/json"

        def listing(items):
            return 200, (items if page == 1 else []), "application/json"

        if sub == "/pulls/1" and method == "GET":
            if s["pr"].get("merged") and s["pr_get_status_after_merge"]:
                return s["pr_get_status_after_merge"], {"message": "bad gateway"}, "application/json"
            return 200, self._live(s["pr"]), "application/json"
        if sub == "/pulls/1" and method == "PATCH":
            s["pr"].update(body)
            return 201, s["pr"], "application/json"
        if sub == "/pulls/1/merge" and method == "POST":
            return self._merge(body)
        if sub == "" and method == "GET":
            return 200, {"full_name": "o/r", "default_branch": "main"}, "application/json"
        if sub == "/pulls" and method == "GET":
            return listing(s["open_prs"] if query.get("state") == ["open"] else [self._live(p) for p in s["closed_prs"]])
        m = re.fullmatch(r"/git/refs/pull/(\d+)/head", sub)
        if m:
            sha = s["pull_refs"].get(int(m.group(1)))
            if not sha:
                return 404, {"message": "ref not found"}, "application/json"
            return 200, [{"ref": f"refs/pull/{m.group(1)}/head", "object": {"sha": sha}}], "application/json"
        if sub == "/branches" and method == "GET":
            s["open_prs"] = s["open_prs"] + s.pop("late_open_prs", [])  # PRs opened while the sweep runs
            return listing(s["branch_listing"] if s["branch_listing"] is not None else list(s["branches"].values()))
        if sub.startswith("/branches/"):
            name = unquote(sub[len("/branches/"):])
            if method == "DELETE":
                if s["delete_status"]:
                    return s["delete_status"], {"message": "branch is protected"}, "application/json"
                if s["branches"].pop(name, None) is None:
                    return 404, {"message": "branch not found"}, "application/json"
                return 204, b"", "application/json"
            if name in s["branch_get_status"]:
                return s["branch_get_status"][name], {"message": "unavailable"}, "application/json"
            if name in s["branches"]:
                return 200, s["branches"][name], "application/json"
            if name != "main":
                return 404, {"message": "branch not found"}, "application/json"
            return 200, {"name": "main", "commit": {"id": s["main_sha"]}, **s["branch_protection"]}, "application/json"
        m = re.fullmatch(r"/commits/([0-9a-f]+)/status", sub)
        if m:
            sts = s["statuses"].get(m.group(1))
            return 200, {"state": "", "statuses": sts, "total_count": len(sts or [])}, "application/json"
        if sub == "/pulls/1/reviews":
            return listing(s["reviews"])
        m = re.fullmatch(r"/pulls/1/reviews/(\d+)/comments", sub)
        if m:
            return 200, s["review_comments"].get(int(m.group(1)), []), "application/json"
        if sub == "/issues/1/comments":
            return (201, {}, "application/json") if method == "POST" else listing(s["issue_comments"])
        if sub == "/issues/1/timeline":
            return listing(s["timeline"])
        if sub == "/pulls/1/commits":
            return listing(s["commits"])
        if sub == "/pulls/1/files":
            return listing([{"filename": f} for f in s["files"]])
        if sub == "/actions/runs" and method == "GET":
            runs = s["runs"]
            if "head_sha" in query:
                runs = [r for r in runs if r.get("commit_sha", query["head_sha"][0]) == query["head_sha"][0]]
            if "run_number" in query:
                runs = [r for r in runs if r.get("index_in_repo") == int(query["run_number"][0])]
            runs = sorted(runs, key=lambda r: -r["id"])
            return 200, {"total_count": len(runs), "workflow_runs": runs}, "application/json"
        m = re.fullmatch(r"/actions/runs/(\d+)", sub)
        if m:
            run = next((r for r in s["runs"] if r["id"] == int(m.group(1))), None)
            return (200, run, "application/json") if run else (404, {"message": "no run"}, "application/json")
        m = re.fullmatch(r"/actions/runs/(\d+)/jobs", sub)
        if m:
            return 200, s["run_jobs"].get(int(m.group(1)), []), "application/json"
        if sub == "/pulls/1.diff":
            return 200, b"diff --git a/docs/x.md b/docs/x.md\n+hello\n", "text/plain"
        return 404, {"message": f"no route {method} {sub}"}, "application/json"

    def _merge(self, body):
        s = self.state
        current = s["merge_head_override"] or s["pr"]["head"]["sha"]
        if body.get("head_commit_id") != current:
            return 409, {"message": "head out of date"}, "application/json"
        s["pr"].update({"merged": True, "state": "closed", "merge_commit_sha": "f" * 40})
        return 200, b"", "application/json"

    def _live(self, pr):
        """Like Forgejo: a merged PR whose head branch still exists reports that branch's LIVE tip
        as head.sha, so only refs/pull/<n>/head (pull_refs) keeps the head the merge took."""
        branch = self.state["branches"].get((pr.get("head") or {}).get("ref"))
        if not pr.get("merged") or branch is None:
            return pr
        return {**pr, "head": {**pr["head"], "sha": branch["commit"]["id"]}}
    def merges(self):
        return [r for r in self.requests if r["method"] == "POST" and r["path"].endswith("/pulls/1/merge")]


def _pr_payload(head_sha, merge_base, **over):
    pr = {
        "number": 1, "state": "open", "draft": False, "merged": False, "mergeable": True,
        "title": "docs: note (UB-25)", "body": "Refs UB-25", "html_url": "http://127.0.0.1/o/r/pulls/1",
        "created_at": "2020-01-01T00:00:00Z", "merge_base": merge_base, "merge_commit_sha": None,
        "head": {"ref": "feat", "sha": head_sha, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "sha": merge_base, "repo": {"full_name": "o/r"}},
    }
    pr.update(over)
    return pr


# --- git fixture ------------------------------------------------------------------------------


def _git(*args, cwd):
    r = subprocess.run(["git", "-c", "core.hooksPath=", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _make_repo(tmp_path: Path, port: int, bare: Path) -> tuple[Path, str, str]:
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@example.com", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "README.md").write_text("x\n")
    wf = main / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text("on:\n  pull_request:\njobs: {}\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("remote", "add", "origin", f"http://127.0.0.1:{port}/o/r.git", cwd=main)
    _git("remote", "set-url", "--push", "origin", str(bare), cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    main_sha = _git("rev-parse", "HEAD", cwd=main)
    _git("checkout", "-qb", "feat", cwd=main)
    (main / "docs").mkdir()
    (main / "docs" / "x.md").write_text("hello\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "docs: note", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    head = _git("rev-parse", "HEAD", cwd=main)
    _git("checkout", "-q", "main", cwd=main)
    _git("update-server-info", cwd=bare)
    _git("fetch", "-q", "origin", cwd=main)
    return main, main_sha, head


def _seed_green(fake: FakeForgejo, head: str, main_sha: str) -> None:
    fake.state.update({
        "pr": _pr_payload(head, main_sha), "main_sha": main_sha,
        "statuses": {head: [{"context": "ci / tests", "status": "success", "target_url": "http://ci/1",
                             "created_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-01T00:00:00Z"}]},
        "reviews": [{"id": 7, "state": "APPROVED", "user": {"login": "rev"}, "commit_id": head,
                     "comments_count": 0, "dismissed": False, "submitted_at": "2020-01-01T00:00:00Z"}],
        "commits": [{"sha": head, "commit": {"message": "docs: note\n", "committer": {"date": "2020-01-01T00:00:00Z"}}}],
        "branches": {"feat": _branch("feat", head)},
    })


def _branch(name, sha, protected=False):
    return {"name": name, "commit": {"id": sha}, "protected": protected}
@pytest.fixture
def forge(tmp_path):
    bare = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", "-q", str(bare), cwd=tmp_path)
    fake = FakeForgejo(bare)
    main, main_sha, head = _make_repo(tmp_path, fake.port, bare)
    _seed_green(fake, head, main_sha)
    keys = tmp_path / "keys.json"
    keys.write_text(_keys(fake.host_key))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    poison = bindir / "gh"
    poison.write_text(f"#!/bin/sh\necho REAL-GH-CALLED \"$@\" >> {tmp_path}/gh.log\nexit 97\n")
    poison.chmod(0o755)
    yield {"fake": fake, "main": main, "head": head, "tmp": tmp_path, "keys": keys, "bindir": bindir}
    fake.close()


def _ship_env(ctx, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SHIP_", "GH_", "RIG_HATCH_REQUEST_SHIP_"))}
    env.update({
        "PATH": f"{ctx['bindir']}{os.pathsep}{os.environ['PATH']}",
        "SHIP_FORGEJO_KEYS_FILE": str(ctx["keys"]), "SHIP_AUDIT_FILE": str(ctx["tmp"] / "audit.jsonl"),
        "SHIP_MAIN_CHECKOUT": str(ctx["main"]), "SHIP_TEST_DISABLE_REVIEW_QUORUM": "1",
        "SHIP_ACCEPTANCE_GATE": "0", "SHIP_MAGIC_CLOSE_GATE": "0", "SHIP_TASK_NOTIFY_ENABLED": "0",
        "SHIP_CI_GRACE": "0", "SHIP_CI_POLL": "1", "SHIP_CI_WAIT": "2", "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_TOKEN": "github-value-must-not-be-used", "GH_TOKEN": "github-value-must-not-be-used",
    })
    env.update(extra)
    return env


def _ship(ctx, *args, **env_extra):
    return subprocess.run(["bash", str(SHIP), "1", *args], cwd=ctx["main"], env=_ship_env(ctx, **env_extra),
                          capture_output=True, text=True, timeout=120)


def _gh_log(ctx) -> str:
    p = ctx["tmp"] / "gh.log"
    return p.read_text() if p.exists() else ""


# --- unit: origin, credentials, HTTP ----------------------------------------------------------


@pytest.mark.parametrize("url,host,slug,base", [
    ("https://alex@git.example.org/ultra/tools.git", "git.example.org", "ultra/tools", "https://git.example.org"),
    ("ssh://git@git.example.org:2222/ultra/tools.git", "git.example.org", "ultra/tools", "https://git.example.org"),
    ("git@git.example.org:ultra/tools.git", "git.example.org", "ultra/tools", "https://git.example.org"),
    ("https://git.example.org:3000/ultra/tools", "git.example.org", "ultra/tools", "https://git.example.org:3000"),
    ("http://127.0.0.1:8080/o/r.git", "127.0.0.1", "o/r", "http://127.0.0.1:8080"),
    ("http://[::1]:8080/o/r.git", "::1", "o/r", "http://[::1]:8080"),
    ("https://notgithub.com/o/r.git", "notgithub.com", "o/r", "https://notgithub.com"),
])
def test_parse_origin_variants(url, host, slug, base):
    o = fp.parse_origin(url)
    assert (o.host, o.slug, o.api_base) == (host, slug, base)


@pytest.mark.parametrize("url", [
    "https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@ssh.github.com:443/o/r.git",
    "http://git.example.org/o/r.git",
    "https://git.example.org/o/r/extra", "",
])
def test_parse_origin_refuses_github_plain_http_and_bad_paths(url):
    with pytest.raises(fp.ProviderError):
        fp.parse_origin(url)


def test_credential_is_bound_to_the_origin_host(tmp_path, monkeypatch):
    keys = tmp_path / "keys.json"
    keys.write_text(_keys("other.example"))
    monkeypatch.setenv("SHIP_FORGEJO_KEYS_FILE", str(keys))
    monkeypatch.setenv("GITHUB_TOKEN", "github-value")
    monkeypatch.setenv("GH_TOKEN", "github-value")
    monkeypatch.delenv("SHIP_FORGEJO_TOKEN", raising=False)
    origin = fp.parse_origin("https://git.example.org/o/r.git")
    assert fp.resolve_credential(origin) is None  # no fallback to another host or to GitHub
    keys.write_text(_keys("git.example.org"))
    assert fp.resolve_credential(origin).header == f"token {PLACEHOLDER}"
    monkeypatch.setenv("SHIP_FORGEJO_TOKEN", "YOUR_ENV_TOKEN_HERE")
    monkeypatch.setenv("SHIP_FORGEJO_TOKEN_HOST", "other.example")
    with pytest.raises(fp.ProviderError, match="not bound"):
        fp.resolve_credential(origin)
    monkeypatch.setenv("SHIP_FORGEJO_TOKEN_HOST", "git.example.org")
    assert fp.resolve_credential(origin).header == "token YOUR_ENV_TOKEN_HERE"


# fj stores an OAuth `expires_at` as the Rust time crate's serde tuple
# [year, ordinal_day, hour, minute, second, nanosecond, offset_h, offset_m, offset_s].
_PAST = [2020, 1, 0, 0, 0, 0, 0, 0, 0]
_FUTURE = [2999, 1, 0, 0, 0, 0, 0, 0, 0]

# Stand-in `fj`: logs argv; `refresh` rewrites the key store like the real fj after refreshing,
# `noop` exits 0 without refreshing, `fail` exits 1.
_FAKE_FJ = """
import json, os, sys
with open(os.environ["FAKE_FJ_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + chr(10))
mode = os.environ["FAKE_FJ_MODE"]
if mode == "fail":
    sys.exit(1)
if mode == "refresh":
    path = os.environ["SHIP_FORGEJO_KEYS_FILE"]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["hosts"][sys.argv[2]].update(token="YOUR_FRESH_TOKEN_HERE", expires_at=[2999, 1, 0, 0, 0, 0, 0, 0, 0])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
"""


def _oauth_keys(tmp_path, monkeypatch, expires_at, mode=None):
    """An OAuth entry for git.example.org; PATH holds only a fake fj (mode) or no fj (None)."""
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"hosts": {"git.example.org": {
        "type": "OAuth", "token": PLACEHOLDER, "refresh_token": PLACEHOLDER, "expires_at": expires_at}}}))
    monkeypatch.setenv("SHIP_FORGEJO_KEYS_FILE", str(keys))
    monkeypatch.delenv("SHIP_FORGEJO_TOKEN", raising=False)
    bindir = tmp_path / "fjbin"
    bindir.mkdir()
    log = tmp_path / "fj.log"
    if mode is not None:
        if os.name == "nt":
            script = tmp_path / "fake_fj.py"
            script.write_text(_FAKE_FJ, encoding="utf-8")
            (bindir / "fj.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            exe = bindir / "fj"
            exe.write_text(f"#!{sys.executable}\n" + _FAKE_FJ, encoding="utf-8")
            exe.chmod(0o755)
        monkeypatch.setenv("FAKE_FJ_MODE", mode)
        monkeypatch.setenv("FAKE_FJ_LOG", str(log))
    monkeypatch.setenv("PATH", str(bindir))
    return log


def _fj_calls(log):
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


@pytest.mark.parametrize("mode", [None, "fail", "noop"])
def test_expired_oauth_token_that_fj_cannot_refresh_fails_closed(tmp_path, monkeypatch, mode):
    log = _oauth_keys(tmp_path, monkeypatch, _PAST, mode)
    with pytest.raises(fp.ProviderError, match="expired") as exc:
        fp.resolve_credential(fp.parse_origin("https://git.example.org/o/r.git"))
    assert PLACEHOLDER not in str(exc.value)
    assert _fj_calls(log) == ([] if mode is None else [["-H", "git.example.org", "whoami"]])


def test_expired_oauth_token_is_refreshed_through_fj_for_the_origin_host_only(tmp_path, monkeypatch):
    log = _oauth_keys(tmp_path, monkeypatch, _PAST, "refresh")
    cred = fp.resolve_credential(fp.parse_origin("https://git.example.org/o/r.git"))
    assert cred.header == "Bearer YOUR_FRESH_TOKEN_HERE"
    assert _fj_calls(log) == [["-H", "git.example.org", "whoami"]]


def test_legacy_numeric_expiry_is_still_honoured(tmp_path, monkeypatch):
    _oauth_keys(tmp_path, monkeypatch, 1)  # unix epoch + 1s: long expired, no fj on PATH
    with pytest.raises(fp.ProviderError, match="expired"):
        fp.resolve_credential(fp.parse_origin("https://git.example.org/o/r.git"))


def test_live_oauth_token_is_used_without_running_fj(tmp_path, monkeypatch):
    log = _oauth_keys(tmp_path, monkeypatch, _FUTURE, "refresh")
    cred = fp.resolve_credential(fp.parse_origin("https://git.example.org/o/r.git"))
    assert cred.header == f"Bearer {PLACEHOLDER}"
    assert _fj_calls(log) == []


def test_unrecognised_oauth_expiry_fails_closed(tmp_path, monkeypatch):
    _oauth_keys(tmp_path, monkeypatch, {"when": "soon"}, "refresh")
    with pytest.raises(fp.ProviderError, match="unsupported format"):
        fp.resolve_credential(fp.parse_origin("https://git.example.org/o/r.git"))


def test_fj_expires_at_tuple_is_utc_with_offset(tmp_path):
    from datetime import datetime, timezone

    assert fp._fj_expires_at([2026, 268, 23, 5, 1, 0, 3, 0, 0]) == datetime(2026, 9, 25, 20, 5, 1, tzinfo=timezone.utc)
    assert fp._fj_expires_at("2026-09-25T20:05:01Z") == datetime(2026, 9, 25, 20, 5, 1, tzinfo=timezone.utc)
    assert fp._fj_expires_at([1, 2]) is None


@pytest.mark.parametrize("platform,parts", [
    ("win32", ("appdata", "forgejo-cli", "forgejo-cli", "data", "keys.json")),
    ("darwin", ("Library", "Application Support", "forgejo-cli.forgejo-cli", "keys.json")),
    ("linux", ("xdg-data", "forgejo-cli", "keys.json")),
])
def test_fj_key_store_location_per_platform(tmp_path, monkeypatch, platform, parts):
    monkeypatch.delenv("SHIP_FORGEJO_KEYS_FILE", raising=False)
    monkeypatch.setattr(fp.sys, "platform", platform)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert fp._fj_keys_path().parts[-len(parts):] == parts
def test_redirect_is_refused_and_credential_never_leaves_the_origin():
    target = FakeForgejo()
    source = FakeForgejo()
    try:
        source.state["redirect_to"] = f"http://127.0.0.1:{target.port}/api/v1/repos/o/r/pulls/1"
        api = fp.Api(fp.parse_origin(f"http://127.0.0.1:{source.port}/o/r.git"), fp.Credential("token S", "t"))
        with pytest.raises(fp.ProviderError, match="redirect"):
            api.call("GET", api.repo_path("/pulls/1"))
        assert target.requests == []
    finally:
        target.close()
        source.close()


# --- unit: evidence mapping -------------------------------------------------------------------


def _ctx_for(fake, tmp_path, state_dir=True):
    origin = fp.parse_origin(f"http://127.0.0.1:{fake.port}/o/r.git")
    sd = tmp_path / "state"
    sd.mkdir(exist_ok=True)
    return fp.Context(origin, str(sd) if state_dir else None)


def _seed(fake, head="a" * 40, base="b" * 40):
    fake.state.update({"pr": _pr_payload(head, base), "main_sha": base,
                       "commits": [{"sha": head, "commit": {"message": "m", "committer": {"date": "2020-01-01T00:00:00Z"}}}]})
    return head


@pytest.fixture
def api_fake(monkeypatch, tmp_path):
    fake = FakeForgejo()
    keys = tmp_path / "keys.json"
    keys.write_text(_keys(fake.host_key))
    monkeypatch.setenv("SHIP_FORGEJO_KEYS_FILE", str(keys))
    monkeypatch.delenv("SHIP_FORGEJO_TOKEN", raising=False)
    monkeypatch.setenv("SHIP_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    yield fake
    fake.close()


def test_status_rollup_maps_real_fields_and_fails_closed(api_fake, tmp_path):
    head = _seed(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    assert fp.pr_json(ctx.pr(1), ["statusCheckRollup"]) == {"statusCheckRollup": []}  # Forgejo null
    api_fake.state["statuses"][head] = [{"context": "lint", "status": "warning"},
                                        {"context": "docs", "status": "skipped"}]
    api_fake.state["branch_protection"] = {"enable_status_check": True, "status_check_contexts": ["ci/*", "docs"]}
    rollup = fp.pr_json(ctx.pr(1), ["statusCheckRollup"])["statusCheckRollup"]
    by_ctx = {r["context"]: r for r in rollup}
    assert by_ctx["lint"]["state"] == "FAILURE"  # warning is never a pass
    assert by_ctx["docs"]["conclusion"] == "SKIPPED"
    assert by_ctx["ci/*"]["state"] == "EXPECTED"  # required, never reported -> pending, not absent


def test_state_mergeable_behind_draft(api_fake, tmp_path):
    _seed(api_fake)
    ctx = _ctx_for(api_fake, tmp_path, state_dir=False)
    got = fp.pr_json(ctx.pr(1), ["state", "mergeable", "mergeStateStatus", "isCrossRepository"])
    assert got == {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "isCrossRepository": False}
    api_fake.state["main_sha"] = "c" * 40
    api_fake.state["pr"].update({"draft": True, "mergeable": False})
    got = fp.pr_json(ctx.pr(1), ["state", "mergeable", "mergeStateStatus"])
    assert got == {"state": "DRAFT", "mergeable": "CONFLICTING", "mergeStateStatus": "BEHIND"}


def test_reviews_exclude_pending_and_review_requests(api_fake, tmp_path):
    _seed(api_fake)
    api_fake.state["reviews"] = [{"id": 1, "state": "PENDING", "user": {"login": "a"}},
                                 {"id": 2, "state": "REQUEST_REVIEW", "user": {"login": "b"}}]
    assert fp.pr_json(_ctx_for(api_fake, tmp_path).pr(1), ["reviews"]) == {"reviews": []}
    api_fake.state["reviews"].append({"id": 3, "state": "COMMENT", "user": {"login": "c"}, "dismissed": True})
    reviews = fp.pr_json(_ctx_for(api_fake, tmp_path).pr(1), ["reviews"])["reviews"]
    assert [r["state"] for r in reviews] == ["DISMISSED"]


def test_unaddressed_comment_filter_reads_the_forgejo_shape(api_fake, tmp_path):
    """ship's external-review evidence filter (agent-tools#879), run with the real jq over what the
    adapter returns for `--json author,comments`: Forgejo has no hidden comments, so only an
    author reply after a comment addresses it."""
    import shutil
    if not shutil.which("jq"):
        pytest.skip("jq required")
    _seed(api_fake)
    api_fake.state["pr"]["user"] = {"login": "me"}
    api_fake.state["issue_comments"] = [
        {"user": {"login": "rev"}, "body": "a", "created_at": "2026-10-01T00:00:00Z"},
        {"user": {"login": "me"}, "body": "done", "created_at": "2026-10-01T01:00:00Z"},
        {"user": {"login": "rev"}, "body": "b", "created_at": "2026-10-02T00:00:00Z"},
    ]
    data = fp.pr_json(_ctx_for(api_fake, tmp_path).pr(1), ["author", "comments"])
    assert data["author"] == {"login": "me"}
    assert [c["createdAt"] for c in data["comments"]] == ["2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", "2026-10-02T00:00:00Z"]
    query = accept_merge._UNADDRESSED_JQ
    run = subprocess.run(["jq", "-r", query], input=json.dumps(data), capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "1"  # only the second `rev` comment is unanswered
THREAD_Q = ("query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name)"
            "{pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} "
            "nodes{isResolved}}}}}")


def test_threads_resolve_through_the_first_comment(api_fake, tmp_path):
    _seed(api_fake)
    api_fake.state["reviews"] = [{"id": 5, "state": "COMMENT", "user": {"login": "r"}, "comments_count": 3}]
    api_fake.state["review_comments"][5] = [
        {"id": 10, "path": "a.py", "position": 3, "original_position": 0, "resolver": {"login": "x"}},
        {"id": 11, "path": "a.py", "position": 3, "original_position": 0, "resolver": None},  # reply
        {"id": 12, "path": "b.py", "position": 1, "original_position": 0, "resolver": None},
    ]
    payload = fp.graphql_payload(_ctx_for(api_fake, tmp_path).pr(1), THREAD_Q)
    nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    assert sorted(n["isResolved"] for n in nodes) == [False, True]


def test_head_pin_refuses_a_head_that_moved(api_fake, tmp_path):
    _seed(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    api_fake.state["pr"]["head"]["sha"] = "d" * 40
    with pytest.raises(fp.ProviderError, match="head moved"):
        ctx.pr(1)


def test_merge_sends_head_commit_id_and_audits(api_fake, tmp_path):
    head = _seed(api_fake)
    fp.run_gh(_ctx_for(api_fake, tmp_path), ["pr", "merge", "1", "--squash"])
    (m,) = api_fake.merges()
    assert m["body"] == {"Do": "squash", "head_commit_id": head, "delete_branch_after_merge": False}
    assert m["auth"] == f"token {PLACEHOLDER}"
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["provider"] == "forgejo" and audit[-1]["head"] == head


def test_merge_is_audited_even_when_the_confirmation_read_fails(api_fake, tmp_path):
    """The merge already happened server-side once the POST answers 200: the audit line must be
    written before the confirmation read, and a failed read still refuses (never claims merged)."""
    head = _seed(api_fake)
    api_fake.state["pr_get_status_after_merge"] = 502
    with pytest.raises(fp.ProviderError, match="502"):
        fp.run_gh(_ctx_for(api_fake, tmp_path), ["pr", "merge", "1", "--squash"])
    assert len(api_fake.merges()) == 1
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["gate"] == "merge" and audit[-1]["head"] == head
    assert audit[-1]["decision"] == "merged:unconfirmed" and audit[-1]["merge_commit"] == ""


def test_merge_passes_subject_and_body_as_the_merge_message(api_fake, tmp_path):
    head = _seed(api_fake)
    fp.run_gh(_ctx_for(api_fake, tmp_path),
              ["pr", "merge", "1", "--squash", "--subject", "fix: clean subject", "--body", "- clean body"])
    (m,) = api_fake.merges()
    assert m["body"] == {"Do": "squash", "head_commit_id": head, "delete_branch_after_merge": False,
                         "MergeTitleField": "fix: clean subject", "MergeMessageField": "- clean body"}


def test_merge_refuses_an_author_email(api_fake, tmp_path):
    _seed(api_fake)
    with pytest.raises(fp.ProviderError, match="author-email"):
        fp.run_gh(_ctx_for(api_fake, tmp_path), ["pr", "merge", "1", "--squash", "--author-email", "a@b.c"])
    assert api_fake.merges() == []


def test_merge_without_credential_or_with_raced_head_fails(api_fake, tmp_path, monkeypatch):
    _seed(api_fake)
    monkeypatch.setenv("SHIP_FORGEJO_KEYS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(fp.ProviderError, match="credential"):
        fp.run_gh(_ctx_for(api_fake, tmp_path), ["pr", "merge", "1", "--squash"])
    assert api_fake.merges() == []
    monkeypatch.setenv("SHIP_FORGEJO_KEYS_FILE", str(tmp_path / "keys.json"))
    api_fake.state["merge_head_override"] = "e" * 40
    with pytest.raises(fp.ProviderError, match="409"):
        fp.run_gh(_ctx_for(api_fake, tmp_path), ["pr", "merge", "1", "--squash"])


# --- read-only Actions evidence for --known-flake -------------------------------------------
# Identity of a check = (server workflow_id, job name). The status context's " (<event>)" suffix
# is not identity; the base evidence must come from the EXACT base branch head.

_CAND_CTX = "tests / pytest (pull_request)"


def _run(rid, number, sha, status, workflow="tests.yml", event="push", **extra):
    return {"id": rid, "index_in_repo": number, "commit_sha": sha, "status": status,
            "workflow_id": workflow, "event": event, "trigger_event": event, **extra}


def _job(jid, rid, name, status, attempt=1, **extra):
    return {"id": jid, "run_id": rid, "name": name, "status": status, "attempt": attempt, **extra}


def _seed_flake(fake, *, base_status="failure", base_workflow="tests.yml", base_job="pytest",
                cand_job="pytest", cand_workflow="tests.yml"):
    """Candidate head H fails `tests / pytest (pull_request)` (run #7); base head B has run #5."""
    head = _seed(fake)
    fake.state["workflow_contents"].setdefault(f".github/workflows/{cand_workflow}", "name: tests\n")
    base = fake.state["main_sha"]
    fake.state["statuses"][head] = [{
        "context": _CAND_CTX, "status": "failure", "target_url": "/o/r/actions/runs/7/jobs/0",
        "created_at": "2020-01-02T00:00:00Z", "updated_at": "2020-01-02T00:00:00Z"}]
    fake.state["runs"] = [
        _run(70, 7, head, "failure", workflow=cand_workflow, event="pull_request"),
        _run(50, 5, base, base_status, workflow=base_workflow, unknown_future_field={"x": 1}),
    ]
    fake.state["run_jobs"] = {
        70: [_job(700, 70, cand_job, "failure")],
        50: [_job(500, 50, base_job, base_status, extra_field="ignored")],
    }
    return head, base


def _run_list(ctx, *extra):
    out = fp.run_gh(ctx, ["run", "list", "--branch", "main", *extra, "--status", "completed",
                          "--limit", "5", "--json", "databaseId,conclusion"])
    return json.loads(out)


def _run_view_jobs(ctx, rid):
    return json.loads(fp.run_gh(ctx, ["run", "view", str(rid), "--json", "jobs", "-q", ".jobs"]))


def test_known_flake_evidence_matches_same_workflow_and_job_on_the_base_head(api_fake, tmp_path):
    _seed_flake(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)  # ship pins the candidate head before the known-flake gate runs
    assert _run_list(ctx) == [{"databaseId": 50, "conclusion": "failure"}]
    jobs = _run_view_jobs(ctx, 50)
    assert [(j["name"], j["conclusion"]) for j in jobs] == [(_CAND_CTX, "failure")]
    assert jobs[0]["workflowId"] == "tests.yml" and jobs[0]["job"] == "pytest"  # receipt
    assert all(r["method"] == "GET" for r in api_fake.requests)  # read-only


def test_known_flake_forged_context_cannot_relabel_an_unrelated_actions_job(api_fake, tmp_path):
    _seed_flake(api_fake, cand_job="lint", base_job="lint")
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50) == []


def test_known_flake_forged_failure_cannot_claim_a_successful_candidate_job(api_fake, tmp_path):
    _seed_flake(api_fake)
    api_fake.state["run_jobs"][70][0]["status"] = "success"
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50) == []


@pytest.mark.parametrize("unsupported", ["dynamic-name", "empty-first-directory", "target-workflow"])
def test_known_flake_unsupported_actions_context_derivation_refuses(api_fake, tmp_path, unsupported):
    _seed_flake(api_fake)
    if unsupported == "dynamic-name":
        api_fake.state["workflow_contents"][".github/workflows/tests.yml"] = "name: ${{ github.ref }}\n"
    elif unsupported == "empty-first-directory":
        api_fake.state["workflow_dirs"].insert(0, ".forgejo/workflows")
    else:
        api_fake.state["runs"][0]["trigger_event"] = "pull_request_target"
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    with pytest.raises(fp.ProviderError, match="workflow|context"):
        _run_view_jobs(ctx, 50)


@pytest.mark.parametrize("over", [
    {"base_workflow": "other.yml"},  # same job name, different workflow file: no identity match
    {"base_job": "lint"},  # same workflow, different job
    {"cand_workflow": "nightly.yml"},  # the candidate's context comes from another workflow
], ids=["other-workflow", "other-job", "candidate-other-workflow"])
def test_known_flake_identity_collisions_never_match(api_fake, tmp_path, over):
    _seed_flake(api_fake, **over)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    for row in _run_list(ctx):
        assert all(j["name"] != _CAND_CTX for j in _run_view_jobs(ctx, row["databaseId"]))


def test_known_flake_cancelled_base_job_is_not_a_failure(api_fake, tmp_path):
    _seed_flake(api_fake, base_status="cancelled")
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert [(j["name"], j["conclusion"]) for j in _run_view_jobs(ctx, 50)] == [(_CAND_CTX, "cancelled")]


def test_known_flake_only_the_latest_run_and_attempt_count(api_fake, tmp_path):
    head, base = _seed_flake(api_fake)
    api_fake.state["runs"].append(_run(60, 6, base, "success"))  # newer run of the same workflow
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_list(ctx) == [{"databaseId": 60, "conclusion": "success"}]
    api_fake.state["runs"].pop()
    api_fake.state["run_jobs"][50].append(_job(501, 50, "pytest", "success", attempt=2))
    assert [(j["name"], j["conclusion"]) for j in _run_view_jobs(ctx, 50)] == [(_CAND_CTX, "success")]


def test_known_flake_missing_base_evidence_is_empty_not_confirmed(api_fake, tmp_path):
    _seed_flake(api_fake)
    api_fake.state["runs"] = [r for r in api_fake.state["runs"] if r["id"] != 50]
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_list(ctx) == []


@pytest.mark.parametrize("total", ["missing", None, "2", True, False, 2.0, -1, 0, 2],
                         ids=["missing", "null", "string", "true", "false", "float", "negative", "understated", "truncated"])
def test_known_flake_incomplete_run_counts_cannot_hide_a_newer_pass(api_fake, tmp_path, monkeypatch, total):
    _head, base = _seed_flake(api_fake)
    api_fake.state["runs"].append(_run(60, 6, base, "success"))
    api_fake.state["run_jobs"][60] = [_job(600, 60, "pytest", "success")]
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_list(ctx) == [{"databaseId": 60, "conclusion": "success"}]
    call = fp.Api.call

    def partial_runs(api, method, path, *args, **kwargs):
        data = call(api, method, path, *args, **kwargs)
        if "/actions/runs?head_sha=" in path:
            data["workflow_runs"] = [r for r in data["workflow_runs"] if r["id"] == 50]
            if total == "missing":
                data.pop("total_count")
            else:
                data["total_count"] = total
        return data

    monkeypatch.setattr(fp.Api, "call", partial_runs)
    with pytest.raises(fp.ProviderError, match="count|partial"):
        _run_list(ctx)


def test_known_flake_refuses_a_run_that_is_not_on_the_exact_base_head(api_fake, tmp_path):
    head, _base = _seed_flake(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    with pytest.raises(fp.ProviderError, match="base"):
        _run_view_jobs(ctx, 70)  # the candidate's own run is never base evidence


def test_known_flake_external_status_without_a_run_link_has_no_identity(api_fake, tmp_path):
    head, _base = _seed_flake(api_fake)
    api_fake.state["statuses"][head][0]["target_url"] = "https://ci.example/build/7"
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50) == []


@pytest.mark.parametrize("target", [
    "https://unrelated.example/foreign/repo/actions/runs/7/jobs/0",
    "/foreign/repo/actions/runs/7/jobs/0",
    "same-host-foreign-repo",
])
def test_known_flake_foreign_status_link_has_no_local_identity(api_fake, tmp_path, target):
    head, _base = _seed_flake(api_fake)
    if target == "same-host-foreign-repo":
        target = f"http://{api_fake.host_key}/foreign/repo/actions/runs/7/jobs/0"
    api_fake.state["statuses"][head][0]["target_url"] = target
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50) == []


def test_known_flake_nonadjacent_duplicate_attempts_fail_closed():
    jobs = [{"name": "pytest", "attempt": attempt} for attempt in (1, 2, 1)]
    with pytest.raises(fp.ProviderError, match="ambiguous"):
        fp._latest_attempts(jobs)


def test_known_flake_candidate_identity_is_reused_across_provider_processes(api_fake, tmp_path):
    _seed_flake(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50)
    assert _run_view_jobs(_ctx_for(api_fake, tmp_path), 50)
    candidate_reads = [r for r in api_fake.requests if r["path"].endswith("/actions/runs/70/jobs")]
    assert len(candidate_reads) == 1


def test_known_flake_changed_status_link_invalidates_identity_cache(api_fake, tmp_path):
    head, _base = _seed_flake(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50)
    api_fake.state["statuses"][head][0]["target_url"] = "https://foreign.example/o/r/actions/runs/7/jobs/0"
    assert _run_view_jobs(_ctx_for(api_fake, tmp_path), 50) == []


def test_known_flake_reused_state_refuses_moved_candidate_head(api_fake, tmp_path):
    _seed_flake(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50)
    api_fake.state["pr"]["head"]["sha"] = "c" * 40
    with pytest.raises(fp.ProviderError, match="head moved"):
        _run_view_jobs(_ctx_for(api_fake, tmp_path), 50)


def test_known_flake_identity_cache_is_bound_to_origin(api_fake, tmp_path):
    _seed_flake(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    assert _run_view_jobs(ctx, 50)
    other = FakeForgejo()
    try:
        _seed_flake(other, cand_job="lint")
        keys = tmp_path / "keys.json"
        stored = json.loads(keys.read_text())
        stored["hosts"][other.host_key] = stored["hosts"][api_fake.host_key]
        keys.write_text(json.dumps(stored))
        assert _run_view_jobs(_ctx_for(other, tmp_path), 50) == []
    finally:
        other.close()


@pytest.mark.parametrize("drop", ["workflow_id", "commit_sha", "status", "id"])
def test_known_flake_missing_required_run_field_fails_closed(api_fake, tmp_path, drop):
    _seed_flake(api_fake)
    del api_fake.state["runs"][1][drop]
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    with pytest.raises(fp.ProviderError):
        _run_list(ctx)


@pytest.mark.parametrize("argv", [
    ["run", "list", "--branch", "main", "--json", "databaseId,conclusion"],  # no --status completed
    ["run", "list", "--branch", "main", "--status", "completed", "--json", "databaseId,headSha"],
    ["run", "list", "--status", "completed", "--json", "databaseId"],  # no --branch
    ["run", "view", "50", "--json", "jobs,url"],
    ["run", "view", "50", "--log-failed"],
    ["run", "rerun", "50"],
])
def test_known_flake_unsupported_run_shapes_fail_closed(api_fake, tmp_path, argv):
    _seed_flake(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    ctx.pr(1)
    with pytest.raises(fp.ProviderError):
        fp.run_gh(ctx, argv)


def test_ship_known_flake_confirmed_on_forgejo_then_runs_the_local_gate(forge):
    fake, head = forge["fake"], forge["head"]
    base = fake.state["main_sha"]
    fake.state["statuses"][head] = [{
        "context": _CAND_CTX, "status": "failure", "target_url": "/o/r/actions/runs/7/jobs/0",
        "created_at": "2020-01-02T00:00:00Z", "updated_at": "2020-01-02T00:00:00Z"}]
    fake.state["runs"] = [_run(70, 7, head, "failure", event="pull_request"), _run(50, 5, base, "failure")]
    fake.state["run_jobs"] = {70: [_job(700, 70, "pytest", "failure")], 50: [_job(500, 50, "pytest", "failure")]}
    r = _ship(forge, "--dry-run", "--known-flake", _CAND_CTX, SHIP_LOCAL_TEST_CMD="true")
    out = r.stdout + r.stderr
    assert f"'{_CAND_CTX}' asserted via --known-flake and CONFIRMED" in out, out
    fake.state["run_jobs"][50] = [_job(500, 50, "pytest", "success")]
    r = _ship(forge, "--dry-run", "--known-flake", _CAND_CTX, SHIP_LOCAL_TEST_CMD="true")
    assert r.returncode != 0 and "could NOT be confirmed" in r.stdout + r.stderr
    assert fake.merges() == [] and _gh_log(forge) == ""
# --- head-branch deletion after a verified merge -----------------------------------------------


def _seed_merged(fake, head="a" * 40):
    _seed(fake, head)
    fake.state["pr"].update({"merged": True, "state": "closed", "merge_commit_sha": "f" * 40})
    fake.state["branches"] = {"feat": _branch("feat", head)}
    fake.state["pull_refs"] = {1: head}
    return head


def _deletes(fake):
    return [r["path"] for r in fake.requests if r["method"] == "DELETE"]


def _audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]


def test_delete_head_deletes_a_merged_branch_and_audits(api_fake, tmp_path, capsys):
    _seed_merged(api_fake)
    assert fp.delete_head(_ctx_for(api_fake, tmp_path), 1) == 0
    assert _deletes(api_fake) == ["/api/v1/repos/o/r/branches/feat"]
    assert "deleted remote branch feat (Forgejo API)" in capsys.readouterr().out
    last = _audit_lines(tmp_path)[-1]
    assert (last["gate"], last["decision"], last["branch"], last["pr"]) == ("branch-delete", "deleted", "feat", 1)


def test_delete_head_encodes_a_slashed_branch_name(api_fake, tmp_path):
    head = _seed_merged(api_fake)
    api_fake.state["pr"]["head"]["ref"] = "fix/545-x"
    api_fake.state["branches"] = {"fix/545-x": _branch("fix/545-x", head)}
    assert fp.delete_head(_ctx_for(api_fake, tmp_path), 1) == 0
    assert _deletes(api_fake) == ["/api/v1/repos/o/r/branches/fix%2F545-x"]


def _stacked(fake):
    fake.state["open_prs"] = [{"number": 9, "base": {"ref": "feat", "repo": {"full_name": "o/r"}},
                               "head": {"ref": "feat-2", "repo": {"full_name": "o/r"}}}]


def _second_pr_from_feat(fake):
    fake.state["open_prs"] = [{"number": 8, "base": {"ref": "release", "repo": {"full_name": "o/r"}},
                               "head": {"ref": "feat", "repo": {"full_name": "o/r"}}}]


@pytest.mark.parametrize("mutate,reason", [
    (lambda f: f.state["pr"].update({"merged": False}), "is not merged"),
    (lambda f: f.state["pr"]["head"].update({"repo": {"full_name": "fork/r"}}), "fork"),
    (lambda f: f.state["branches"]["feat"].update({"protected": True}), "protected"),
    (lambda f: f.state["branches"]["feat"].update({"commit": {"id": "c" * 40}}), "commits pushed after the merge"),
    (lambda f: f.state["pr"]["head"].update({"ref": "main"}), "default or base branch"),
    (_stacked, "open PR #9 still uses it"),
    (_second_pr_from_feat, "open PR #8 still uses it"),
], ids=["unmerged", "fork", "protected", "tip-moved", "default-branch", "stacked-base", "second-head"])
def test_delete_head_keeps_the_branch_with_a_reason(api_fake, tmp_path, capsys, mutate, reason):
    _seed_merged(api_fake)
    mutate(api_fake)
    assert fp.delete_head(_ctx_for(api_fake, tmp_path), 1) == 3
    assert _deletes(api_fake) == []
    assert reason in capsys.readouterr().out
    assert _audit_lines(tmp_path)[-1]["decision"] == "keep"


def test_open_pr_in_a_fork_with_the_same_branch_name_is_not_a_dependent(api_fake, tmp_path):
    _seed_merged(api_fake)
    api_fake.state["open_prs"] = [{"number": 8, "base": {"ref": "main", "repo": {"full_name": "o/r"}},
                                   "head": {"ref": "feat", "repo": {"full_name": "fork/r"}}}]
    assert fp.delete_head(_ctx_for(api_fake, tmp_path), 1) == 0
    assert _deletes(api_fake) == ["/api/v1/repos/o/r/branches/feat"]


def test_a_push_after_the_merge_is_kept_although_the_pr_reports_the_live_tip(api_fake, tmp_path, capsys):
    # Forgejo's PR payload then says head.sha == the new tip; only refs/pull/1/head is the merged head.
    _seed_merged(api_fake)
    api_fake.state["branches"]["feat"]["commit"]["id"] = "c" * 40
    assert fp.run_gh(_ctx_for(api_fake, tmp_path, state_dir=False), ["pr", "view", "1", "--json", "headRefOid",
                                                                     "-q", ".headRefOid"]).strip() == "c" * 40
    assert fp.delete_head(_ctx_for(api_fake, tmp_path, state_dir=False), 1) == 3
    assert _deletes(api_fake) == [] and "commits pushed after the merge" in capsys.readouterr().out


def test_the_head_pinned_by_the_ship_run_wins_over_refs_pull(api_fake, tmp_path):
    _seed_merged(api_fake)
    api_fake.state["pull_refs"] = {1: "c" * 40}           # even a moved ref cannot vouch for the tip
    api_fake.state["branches"]["feat"]["commit"]["id"] = "c" * 40
    ctx = _ctx_for(api_fake, tmp_path)
    Path(ctx.state_dir, "pr-1.head").write_text("a" * 40 + "\n")
    assert fp.delete_head(ctx, 1) == 3
    assert _deletes(api_fake) == []


def test_an_unreadable_merged_head_keeps_the_branch(api_fake, tmp_path, capsys):
    _seed_merged(api_fake)
    api_fake.state["pull_refs"] = {}
    assert fp.delete_head(_ctx_for(api_fake, tmp_path, state_dir=False), 1) == 3
    assert _deletes(api_fake) == [] and "could not be read" in capsys.readouterr().out


@pytest.mark.parametrize("gone", ["pr-ref", "branch-404"])
def test_delete_head_reports_an_already_gone_branch(api_fake, tmp_path, capsys, gone):
    _seed_merged(api_fake)
    if gone == "pr-ref":
        api_fake.state["pr"]["head"]["ref"] = "refs/pull/1/head"
    else:
        api_fake.state["branches"] = {}
    assert fp.delete_head(_ctx_for(api_fake, tmp_path), 1) == 0
    assert _deletes(api_fake) == []
    assert "already gone" in capsys.readouterr().out
    assert _audit_lines(tmp_path)[-1]["decision"] == "gone"


def test_delete_head_failure_raises_and_is_audited(api_fake, tmp_path):
    _seed_merged(api_fake)
    api_fake.state["delete_status"] = 403
    with pytest.raises(fp.ProviderError, match="HTTP 403"):
        fp.delete_head(_ctx_for(api_fake, tmp_path), 1)
    assert _audit_lines(tmp_path)[-1]["decision"] == "failed"


def test_delete_head_needs_a_bound_credential(api_fake, tmp_path, monkeypatch):
    _seed_merged(api_fake)
    monkeypatch.setenv("SHIP_FORGEJO_KEYS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(fp.ProviderError, match="credential"):
        fp.delete_head(_ctx_for(api_fake, tmp_path), 1)
    assert _deletes(api_fake) == []


def _merged_pr(number, ref, merged_head, repo="o/r"):
    return {"number": number, "merged": True, "head": {"ref": ref, "sha": merged_head, "repo": {"full_name": repo}},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}}}


def _seed_backlog(fake):
    fake.state["main_sha"] = "b" * 40
    fake.state["branches"] = {
        "main": _branch("main", "b" * 40, protected=True),
        "done": _branch("done", "1" * 40),             # tip == merged head -> delete
        "moved": _branch("moved", "2" * 40),           # pushed past its merged head -> never touched
        "stacked": _branch("stacked", "3" * 40),       # an open PR targets it -> keep
        "wip": _branch("wip", "4" * 40),               # no merged PR at all -> never touched
        "fix/slash": _branch("fix/slash", "5" * 40),   # slashed name -> delete
        "reused": _branch("reused", "6" * 40),         # old merged PR, name reused for new WIP -> never touched
        "twice": _branch("twice", "8" * 40),           # merged twice; tip == the OLDER merged head -> delete
    }
    fake.state["closed_prs"] = [
        _merged_pr(10, "done", "1" * 40), _merged_pr(11, "moved", "9" * 40),
        _merged_pr(12, "stacked", "3" * 40), _merged_pr(13, "fix/slash", "5" * 40),
        {"number": 14, "merged": False, "head": {"ref": "wip", "sha": "4" * 40, "repo": {"full_name": "o/r"}}},
        _merged_pr(15, "reused", "7" * 40), _merged_pr(16, "twice", "8" * 40), _merged_pr(17, "twice", "e" * 40),
    ]
    fake.state["pull_refs"] = {p["number"]: p["head"]["sha"] for p in fake.state["closed_prs"]}
    fake.state["open_prs"] = [{"number": 20, "base": {"ref": "stacked", "repo": {"full_name": "o/r"}},
                               "head": {"ref": "child", "repo": {"full_name": "o/r"}}}]


def test_prune_merged_dry_run_lists_and_never_deletes(api_fake, tmp_path, capsys):
    _seed_backlog(api_fake)
    assert fp.prune_merged(_ctx_for(api_fake, tmp_path, state_dir=False), apply=False) == 0
    out = capsys.readouterr().out
    assert "delete  done  (merged PR #10)" in out and "delete  fix/slash  (merged PR #13)" in out
    assert "delete  twice  (merged PR #16)" in out
    assert "keep    stacked: open PR #20" in out
    for untouched in ("moved", "wip", "reused", "main  "):
        assert untouched not in out
    assert "3 would delete" in out
    assert _deletes(api_fake) == []


def test_prune_merged_apply_deletes_only_the_provably_merged(api_fake, tmp_path, capsys):
    _seed_backlog(api_fake)
    assert fp.prune_merged(_ctx_for(api_fake, tmp_path, state_dir=False), apply=True) == 0
    assert sorted(_deletes(api_fake)) == ["/api/v1/repos/o/r/branches/done", "/api/v1/repos/o/r/branches/fix%2Fslash",
                                          "/api/v1/repos/o/r/branches/twice"]
    assert set(api_fake.state["branches"]) == {"main", "moved", "stacked", "wip", "reused"}
    assert "3 deleted, 1 kept, 0 failed" in capsys.readouterr().out
    decisions = [(a["branch"], a["decision"], a["source"]) for a in _audit_lines(tmp_path)]
    assert decisions == [("done", "deleted", "prune-merged"), ("stacked", "keep", "prune-merged"),
                         ("fix/slash", "deleted", "prune-merged"), ("twice", "deleted", "prune-merged")]


def test_prune_merged_dry_run_writes_no_audit(api_fake, tmp_path):
    _seed_backlog(api_fake)
    fp.prune_merged(_ctx_for(api_fake, tmp_path, state_dir=False), apply=False)
    assert not (tmp_path / "audit.jsonl").exists()


def test_prune_merged_audits_a_branch_read_failure_and_carries_on(api_fake, tmp_path, capsys):
    _seed_backlog(api_fake)
    api_fake.state["branch_get_status"] = {"done": 503}
    assert fp.prune_merged(_ctx_for(api_fake, tmp_path, state_dir=False), apply=True) == 1
    captured = capsys.readouterr()
    assert "FAILED  done" in captured.err and "2 deleted, 1 kept, 1 failed" in captured.out
    assert "/api/v1/repos/o/r/branches/done" not in _deletes(api_fake)
    assert ("done", "failed") in [(a["branch"], a["decision"]) for a in _audit_lines(tmp_path)]


def test_prune_merged_apply_reports_a_failed_delete(api_fake, tmp_path, capsys):
    _seed_backlog(api_fake)
    api_fake.state["delete_status"] = 403
    assert fp.prune_merged(_ctx_for(api_fake, tmp_path, state_dir=False), apply=True) == 1
    captured = capsys.readouterr()
    assert "FAILED  done" in captured.err and "0 deleted, 1 kept, 3 failed" in captured.out


def test_prune_merged_rereads_a_branch_that_moved_after_the_listing(api_fake, tmp_path, capsys):
    _seed_backlog(api_fake)
    api_fake.state["branch_listing"] = [_branch("done", "1" * 40)]  # stale snapshot: still the merged head
    api_fake.state["branches"]["done"]["commit"]["id"] = "c" * 40  # a push landed after the listing
    assert fp.prune_merged(_ctx_for(api_fake, tmp_path, state_dir=False), apply=True) == 0
    assert _deletes(api_fake) == []
    assert "keep    done: its tip cccccccccccc is not the merged head" in capsys.readouterr().out


def test_prune_merged_keeps_a_branch_a_pr_started_stacking_on_during_the_sweep(api_fake, tmp_path, capsys):
    _seed_backlog(api_fake)
    api_fake.state["late_open_prs"] = [{"number": 21, "base": {"ref": "done", "repo": {"full_name": "o/r"}},
                                        "head": {"ref": "done-2", "repo": {"full_name": "o/r"}}}]
    assert fp.prune_merged(_ctx_for(api_fake, tmp_path, state_dir=False), apply=True) == 0
    assert "/api/v1/repos/o/r/branches/done" not in _deletes(api_fake)
    assert "keep    done: open PR #21 still uses it" in capsys.readouterr().out


def test_cli_delete_head_and_prune_merged_entry_points(api_fake, tmp_path, monkeypatch):
    _seed_merged(api_fake)
    monkeypatch.setenv("SHIP_FORGEJO_ORIGIN_URL", f"http://127.0.0.1:{api_fake.port}/o/r.git")
    monkeypatch.delenv("SHIP_FORGEJO_STATE_DIR", raising=False)
    assert fp.main(["delete-head", "1"]) == 0
    assert fp.main(["prune-merged"]) == 0
    assert fp.main(["prune-merged", "--force"]) == 2
    assert fp.main(["delete-head", "x"]) == 2
    assert _deletes(api_fake) == ["/api/v1/repos/o/r/branches/feat"]


# --- every gh call site in ship is mapped or fails closed -------------------------------------

MAPPED = {("pr", "view"), ("pr", "diff"), ("pr", "merge"), ("pr", "comment"), ("pr", "edit"),
          ("api", "graphql"), ("repo", "view"), ("run", "list"), ("run", "view")}
# Sites the Forgejo provider deliberately does NOT map (GitHub Actions evidence): each must fail.
# An f-string site is keyed by its text with every interpolation rendered as `$<expression>`.
UNSUPPORTED_PROBES = {
    ("run", "list", "no-status"): ["run", "list", "--branch", "main"],
    ("run", "view", "no-json"): ["run", "view", "1"],
    ("api", "repos/{owner}/{repo}/actions/runs?head_sha=$head_sha&per_page=20"): ["api", "repos/o/r/actions/runs"],
    ("api", "repos/{owner}/{repo}/commits/$head_sha/check-runs?per_page=100"): ["api", "repos/o/r/commits/x/check-runs"],
    ("api", "repos/{owner}/{repo}/check-runs/$cid/annotations"): ["api", "repos/o/r/check-runs/1/annotations"],
}


def _const_str(node):
    """The literal text of a str constant / f-string (interpolations rendered `$<expr>`), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "$" + ast.unparse(part.value)
            for part in node.values
        )
    return None


def _list_prefix(node, func):
    """Leading literal items of the list a `*args` splat refers to: `*name` / `*name[1:]`, where `name`
    is assigned a list literal in the enclosing function (the way ship builds a conditional argv)."""
    skip = 0
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
        assert isinstance(node.slice.lower, ast.Constant) and node.slice.upper is None, ast.unparse(node)
        skip, node = node.slice.lower.value, node.value
    if not isinstance(node, ast.Name):
        return []
    items = []
    for stmt in ast.walk(func):
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == node.id for t in stmt.targets) \
                and isinstance(stmt.value, ast.List):
            for elt in stmt.value.elts:
                if isinstance(elt, ast.Starred):
                    break
                items.append(_const_str(elt))
            break
    return items[skip:]


def _ship_gh_calls() -> list[tuple[str, int, list]]:
    """Every `ctx.gh(...)` call in shiplib/*.py as (file, line, leading literal argv items; None where
    an item is not a literal). A `*splat` argument is resolved to the list literal it was built from."""
    calls = []
    for path in sorted(SHIPLIB.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for func in funcs:
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "gh"
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx"):
                    continue
                argv = []
                for arg in node.args:
                    if isinstance(arg, ast.Starred):
                        argv += _list_prefix(arg.value, func)
                        break
                    argv.append(_const_str(arg))
                calls.append((path.name, node.lineno, argv))
    # ast.walk visits a nested function's calls once per enclosing function: de-duplicate.
    return sorted(set((f, ln, tuple(argv)) for f, ln, argv in calls))


def _ship_gh_callsites() -> set:
    sites = set()
    for fname, line, argv in _ship_gh_calls():
        assert len(argv) >= 2 and argv[0] and argv[1], (
            f"{fname}:{line}: ctx.gh call whose command words are not literal — cannot be classified")
        sites.add((argv[0], argv[1]))
    return sites


def test_every_ship_gh_callsite_is_mapped_or_fails_closed(api_fake, tmp_path):
    sites = _ship_gh_callsites()
    assert ("pr", "merge") in sites and ("api", "graphql") in sites  # the scan actually sees ship's gh calls
    unclassified = sites - MAPPED - set(UNSUPPORTED_PROBES)
    assert not unclassified, f"new gh call site(s) in ship need a Forgejo mapping or a fail-closed probe: {unclassified}"
    _seed(api_fake)
    ctx = _ctx_for(api_fake, tmp_path)
    mutation = "query=mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
    auto_resolve = ("query=query($pr:Int!){repository{pullRequest(number:$pr){reviewThreads(first:100)"
                    "{nodes{id isResolved isOutdated comments(first:100){nodes{author{login}}}}}}}}")
    probes = [*UNSUPPORTED_PROBES.values(), ["pr", "checks", "1"], ["issue", "view", "1"],
              ["pr", "view", "1", "--json", "latestReviews"], ["pr", "view", "1"],
              ["api", "graphql", "-f", mutation, "-f", "id=x"],
              ["api", "graphql", "-F", "pr=1", "-f", auto_resolve]]
    for argv in probes:
        with pytest.raises(fp.ProviderError):
            fp.run_gh(ctx, argv)


def test_every_ship_pr_view_json_field_is_mapped():
    fields = set()
    for _fname, _line, argv in _ship_gh_calls():
        if argv[:2] == ("pr", "view") and "--json" in argv:
            spec = argv[argv.index("--json") + 1]
            fields.update(f for f in re.findall(r"[A-Za-z]+", spec) if f)
    assert {"headRefName", "statusCheckRollup", "reviews"} <= fields  # the scan actually sees ship's gh calls
    assert fields <= set(fp.FIELD_BUILDERS), sorted(fields - set(fp.FIELD_BUILDERS))


# --- integration: ship.sh against the fake Forgejo -------------------------------------------


def test_forgejo_happy_path_merges_with_pinned_head(forge):
    r = _ship(forge)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "provider: forgejo 16.0.3" in r.stdout
    (m,) = forge["fake"].merges()
    assert m["body"]["head_commit_id"] == forge["head"] and m["body"]["Do"] == "squash"
    assert "force_merge" not in m["body"]
    assert all(q["auth"] in (None, f"token {PLACEHOLDER}") for q in forge["fake"].requests)
    assert _gh_log(forge) == ""  # the real gh is never reached
    assert PLACEHOLDER not in r.stdout + r.stderr
    assert "github-value" not in json.dumps(forge["fake"].requests)


def test_forgejo_ship_deletes_the_head_branch_through_the_api(forge):
    r = _ship(forge)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[ship] deleted remote branch feat (Forgejo API)" in r.stdout
    assert _deletes(forge["fake"]) == ["/api/v1/repos/o/r/branches/feat"]
    deleted = [json.loads(line) for line in (forge["tmp"] / "audit.jsonl").read_text().splitlines()
               if '"branch-delete"' in line]
    assert [(a["decision"], a["source"]) for a in deleted] == [("deleted", "ship")]


def test_forgejo_ship_failed_branch_delete_warns_but_the_merge_stands(forge):
    forge["fake"].state["delete_status"] = 403
    r = _ship(forge)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(forge["fake"].merges()) == 1
    assert "could not delete remote branch feat through the Forgejo API" in r.stderr
    assert "prune-merged --apply" in r.stderr


def test_forgejo_ship_keeps_a_branch_another_open_pr_builds_on(forge):
    _stacked(forge["fake"])
    r = _ship(forge)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[ship] kept remote branch feat: open PR #9 still uses it as head or base" in r.stdout
    assert _deletes(forge["fake"]) == [] and "WARNING: could not delete" not in r.stderr


def test_forgejo_ship_fork_head_is_kept_and_audited_by_the_provider(forge):
    forge["fake"].state["pr"]["head"]["repo"] = {"full_name": "fork/r"}
    r = _ship(forge)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[ship] kept remote branch feat: the head is a fork" in r.stdout
    assert _deletes(forge["fake"]) == []
    kept = [json.loads(line) for line in (forge["tmp"] / "audit.jsonl").read_text().splitlines()
            if '"branch-delete"' in line]
    assert [(a["decision"], a["source"]) for a in kept] == [("keep", "ship")]
def test_forgejo_dry_run_never_merges(forge):
    r = _ship(forge, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[dry-run] gh pr merge 1 --squash" in r.stdout
    assert forge["fake"].merges() == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda s, h: s["statuses"][h].__setitem__(0, {"context": "ci", "status": "failure"}), "ci -> FAILURE"),
    (lambda s, h: s.__setitem__("reviews", []), "review"),
    (lambda s, h: s["pr"].__setitem__("draft", True), "DRAFT"),
    (lambda s, h: s.__setitem__("main_sha", "c" * 40), "BEHIND"),
    (lambda s, h: s["pr"].__setitem__("mergeable", False), "CONFLICTING"),
], ids=["failing-check", "zero-reviews", "draft", "behind", "conflicting"])
def test_forgejo_gate_refusals(forge, mutate, needle):
    mutate(forge["fake"].state, forge["head"])
    r = _ship(forge)
    assert r.returncode != 0
    assert needle in r.stdout + r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


def test_forgejo_unresolved_conversation_refuses(forge):
    s = forge["fake"].state
    s["reviews"][0]["comments_count"] = 1
    s["review_comments"][7] = [{"id": 1, "path": "docs/x.md", "position": 1, "original_position": 0, "resolver": None}]
    r = _ship(forge)
    assert r.returncode != 0 and "unresolved review thread" in r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


def test_forgejo_raced_head_is_refused_by_the_server(forge):
    forge["fake"].state["merge_head_override"] = "e" * 40
    r = _ship(forge)
    assert r.returncode != 0 and "409" in r.stderr, r.stdout + r.stderr
    audit = forge["tmp"] / "audit.jsonl"
    assert not audit.exists() or '"gate": "merge"' not in audit.read_text()


@pytest.mark.parametrize("cmd,ok", [("true", True), ("false", False)])
def test_forgejo_no_statuses_uses_the_same_local_fallback(forge, cmd, ok):
    forge["fake"].state["statuses"] = {}
    r = _ship(forge, SHIP_LOCAL_TEST_CMD=cmd)
    assert "Running local CI fallback gates" in r.stdout + r.stderr
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert len(forge["fake"].merges()) == (1 if ok else 0)


def test_forgejo_required_context_never_reported_refuses(forge):
    forge["fake"].state["branch_protection"] = {"enable_status_check": True, "status_check_contexts": ["deploy"]}
    r = _ship(forge)
    assert r.returncode != 0 and "pending check" in r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


def test_forgejo_repo_flag_is_refused(forge):
    r = _ship(forge, "--repo", "o/r")
    assert r.returncode != 0 and "GitHub-only" in r.stderr
    assert forge["fake"].merges() == [] and _gh_log(forge) == ""


def test_non_forgejo_host_is_refused_without_calling_gh(forge):
    forge["fake"].state["version_status"] = 404
    r = _ship(forge)
    assert r.returncode != 0 and "not confirmed as a Forgejo instance" in r.stderr
    assert _gh_log(forge) == ""


def test_anonymous_dry_run_reads_but_merge_needs_a_bound_credential(forge):
    missing = str(forge["tmp"] / "none.json")
    dry = _ship(forge, "--dry-run", SHIP_FORGEJO_KEYS_FILE=missing)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    r = _ship(forge, SHIP_FORGEJO_KEYS_FILE=missing)
    assert r.returncode != 0 and "needs a credential bound to" in r.stderr
    assert all(q["auth"] is None for q in forge["fake"].requests)
    assert forge["fake"].merges() == []


def test_github_origin_keeps_the_gh_path(forge):
    _git("remote", "set-url", "origin", "git@github.com:o/r.git", cwd=forge["main"])
    forge["fake"].requests.clear()
    r = _ship(forge, "--dry-run", GIT_SSH_COMMAND="false")
    assert "provider: forgejo" not in r.stdout
    assert "REAL-GH-CALLED pr view 1" in _gh_log(forge)
    assert forge["fake"].requests == []


# --- the provider-agnostic gates stay live on the Forgejo path --------------------------------

_FAKE_REVIEW = """#!/bin/sh
[ "$1" = task ] || exit 0
n="${FAKE_REVIEW_PASSED:-0}"; ok=false; [ "$n" -ge 3 ] && ok=true
printf '{"task_code":"%s","passed_iterations":%s,"total_iterations":%s,"distinct_models_passed":1,"models":["m"],"distinct_roles_passed":%s,"roles":["backend","security","architecture"],"min_roles":3,"min_iter":3,"min_models":3,"passed":%s,"identity_verification":"repo_only"}\\n' "$2" "$n" "$n" "$n" "$ok"
"""

_FAKE_TASK = """#!/bin/sh
echo "$@" >> "$FAKE_TASK_LOG"
[ "$1" = gate ] || exit 0
printf '{"id":"%s","ok":false,"state":"todo","gate_enabled":true,"post_merge_acceptance":null,"criteria":1,"below_minimum":false,"unchecked":[{"index":1,"text":"works on Forgejo"}],"proofless":[]}\\n' "$2"
exit 1
"""


def _install(ctx, name, body):
    p = ctx["bindir"] / name
    p.write_text(body)
    p.chmod(0o755)


@pytest.mark.parametrize("passed,ok", [("0", False), ("3", True)])
def test_forgejo_review_quorum_gate_is_enforced(forge, passed, ok):
    _install(forge, "review", _FAKE_REVIEW)
    env = _ship_env(forge, FAKE_REVIEW_PASSED=passed)
    env.pop("SHIP_TEST_DISABLE_REVIEW_QUORUM")
    r = subprocess.run(["bash", str(SHIP), "1"], cwd=forge["main"], env=env, capture_output=True, text=True, timeout=120)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert "UB-25" in r.stdout + r.stderr
    assert len(forge["fake"].merges()) == (1 if ok else 0)


def test_forgejo_magic_close_in_a_commit_message_refuses(forge):
    forge["fake"].state["commits"][0]["commit"]["message"] = "docs: note\n\nCloses #3\n"
    r = _ship(forge, SHIP_MAGIC_CLOSE_GATE="1")
    assert r.returncode != 0 and "Closes #3" in r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


def test_forgejo_acceptance_gate_refuses_an_unaccepted_ticket(forge):
    _install(forge, "task", _FAKE_TASK)
    log = forge["tmp"] / "task.log"
    r = _ship(forge, SHIP_ACCEPTANCE_GATE="1", FAKE_TASK_LOG=str(log))
    assert r.returncode != 0 and "works on Forgejo" in r.stdout + r.stderr, r.stdout + r.stderr
    assert "gate UB-25" in log.read_text()
    assert forge["fake"].merges() == []


# --- swallowed provider failures are still fatal (per-run receipt) ----------------------------


@pytest.mark.parametrize("extra", [(), ("--dry-run",)], ids=["merge", "dry-run"])
def test_swallowed_unsupported_call_is_refused_before_merge(forge, extra):
    # ship.sh's auto-resolve step tolerates a failed gh ("skipping; the gate below still
    # applies"); on Forgejo that query has no evidence source, so the receipt must still stop
    # the merge.
    r = _ship(forge, "--resolve-addressed-threads", *extra)
    out = r.stdout + r.stderr
    assert "skipping; the gate below still applies" in out
    assert r.returncode != 0, out
    assert "evidence is incomplete" in r.stderr and "auto-resolve is unsupported" in r.stderr
    assert forge["fake"].merges() == []
    assert "[dry-run] gh pr merge" not in r.stdout


def test_provider_crash_is_recorded_in_the_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIP_FORGEJO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHIP_FORGEJO_ORIGIN_URL", "http://127.0.0.1:9/o/r.git")  # nothing listens
    assert fp.main(["gh", "run", "list"]) == 1
    assert fp.main(["gh", "pr", "view", "1", "--json", "state"]) == 1
    lines = (tmp_path / fp.RECEIPT_NAME).read_text().splitlines()
    assert len(lines) == 2 and "unsupported" in lines[0] and "failed" in lines[1]


# --- ticket routing stays with task-cli, never the Forgejo issues API -------------------------

_FAKE_TASK_ACCEPTED = """#!/bin/sh
echo "$@" >> "$FAKE_TASK_LOG"
[ "$1" = gate ] || exit 0
printf '{"id":"%s","ok":true,"state":"in_progress","gate_enabled":true,"post_merge_acceptance":"verified after release","criteria":1,"below_minimum":false,"unchecked":[],"proofless":[]}\\n' "$2"
"""


def test_task_gate_and_mark_shipped_route_through_task_cli(forge):
    _install(forge, "task", _FAKE_TASK_ACCEPTED)
    log = forge["tmp"] / "task.log"
    r = _ship(forge, SHIP_ACCEPTANCE_GATE="1", SHIP_TASK_NOTIFY_ENABLED="1", FAKE_TASK_LOG=str(log))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    assert any(c.startswith("gate UB-25") for c in calls)
    assert any(c.startswith("mark-shipped UB-25") and "http://127.0.0.1/o/r/pulls/1" in c for c in calls)
    assert not any(c.startswith(("done", "close")) for c in calls)  # post-merge opt-out: never closed
    # The only /issues/ traffic is PR #1's own conversation reads; no ticket is read or closed there.
    issue_calls = [q for q in forge["fake"].requests if "/issues/" in q["path"]]
    assert all(q["path"].startswith("/api/v1/repos/o/r/issues/1/") and q["method"] == "GET" for q in issue_calls)
    assert not any(q["method"] == "PATCH" and "/issues/" in q["path"] for q in forge["fake"].requests)


# --- review round 1 regressions ---------------------------------------------------------------


def _threads(fake, tmp_path):
    payload = fp.graphql_payload(_ctx_for(fake, tmp_path).pr(1), THREAD_Q)
    return sorted(n["isResolved"] for n in payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"])


def test_a_conversation_spans_reviews_and_commits_on_one_line_like_the_forgejo_ui(api_fake, tmp_path):
    """Forgejo shows every comment on a path + line as ONE conversation and "Resolve conversation"
    marks only its first comment. Keying by review/commit split it into groups nobody can resolve
    (a reply posted in a new review; two reviews on one line, rig-cli#482)."""
    _seed(api_fake)
    api_fake.state["reviews"] = [{"id": 5, "state": "COMMENT", "user": {"login": "r"}, "comments_count": 1},
                                 {"id": 6, "state": "COMMENT", "user": {"login": "r"}, "comments_count": 1}]
    same_line = {"path": "a.py", "position": 3, "original_position": 0}
    api_fake.state["review_comments"][5] = [{"id": 10, **same_line, "pull_request_review_id": 5,
                                             "original_commit_id": "1" * 40, "resolver": {"login": "x"}}]
    api_fake.state["review_comments"][6] = [{"id": 20, **same_line, "pull_request_review_id": 6,
                                             "original_commit_id": "2" * 40, "resolver": None}]
    assert _threads(api_fake, tmp_path) == [True]
    api_fake.state["review_comments"][5][0]["resolver"] = None
    assert _threads(api_fake, tmp_path) == [False]


def test_lineless_comments_are_never_merged_into_one_conversation(api_fake, tmp_path):
    _seed(api_fake)
    api_fake.state["reviews"] = [{"id": 5, "state": "COMMENT", "user": {"login": "r"}, "comments_count": 2}]
    lineless = {"path": "a.py", "position": 0, "original_position": 0, "pull_request_review_id": 5}
    api_fake.state["review_comments"][5] = [{"id": 10, **lineless, "resolver": {"login": "x"}},
                                            {"id": 11, **lineless, "resolver": None}]
    assert _threads(api_fake, tmp_path) == [False, True]


def test_review_comments_are_read_even_when_comments_count_is_zero(api_fake, tmp_path):
    _seed(api_fake)
    api_fake.state["reviews"] = [{"id": 5, "state": "COMMENT", "user": {"login": "r"}, "comments_count": 0}]
    api_fake.state["review_comments"][5] = [{"id": 10, "path": "a.py", "position": 1, "original_position": 0,
                                             "pull_request_review_id": 5, "resolver": None}]
    assert _threads(api_fake, tmp_path) == [False]


def test_forgejo_owner_named_github_com_still_uses_the_provider(forge):
    port = forge["fake"].port
    _git("remote", "set-url", "origin", f"http://127.0.0.1:{port}/github.com/r.git", cwd=forge["main"])
    r = _ship(forge, "--dry-run")
    assert "provider: forgejo" in r.stdout, r.stdout + r.stderr
    assert r.returncode != 0 and _gh_log(forge) == ""  # repo github.com/r does not exist -> refused


def _fake_python(ctx, trigger: str, action: str) -> None:
    """A python3 shim that sabotages exactly one provider call and passes every other call on."""
    import shutil
    real = shutil.which("python3")
    _install(ctx, "python3", f'#!/bin/sh\ncase "$*" in *forgejo_provider.py*{trigger}*) {action} ;; esac\n'
                             f'exec "{real}" "$@"\n')


def test_provider_killed_behind_a_swallowing_call_site_refuses(forge):
    _fake_python(forge, "isOutdated", "kill -9 $$")  # auto-resolve query; ship.sh tolerates its failure
    r = _ship(forge, "--resolve-addressed-threads")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "evidence is incomplete" in r.stderr and "exit 137" in r.stderr
    assert forge["fake"].merges() == []


def test_unwritable_failure_ledger_still_refuses(forge):
    _fake_python(forge, "isOutdated", 'chmod 000 "$SHIP_FORGEJO_STATE_DIR/fatal.log"; exit 1')
    r = _ship(forge, "--resolve-addressed-threads")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "ledger is missing or could not be written" in r.stderr
    assert forge["fake"].merges() == []


def test_ledger_that_cannot_be_created_refuses_at_start(forge):
    ro = forge["tmp"] / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        r = _ship(forge, "--dry-run", TMPDIR=str(ro))
    finally:
        ro.chmod(0o700)
    assert r.returncode != 0 and "evidence ledger" in r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


# task-cli 0.18.2's real answer from a checkout whose origin it cannot route (captured live from
# ultrabricks/agent-tools on git.hyperide.ai): exit 2, empty stdout, this line on stderr.
_TASK_CLI_UNROUTABLE = """#!/bin/sh
echo "$@" >> "$FAKE_TASK_LOG"
echo "error: unrecognized github remote URL: $(git remote get-url origin)" >&2
exit 2
"""


def _path_without_task() -> str:
    return os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "task").exists())


def test_forgejo_acceptance_gate_fails_closed_when_task_cli_cannot_route(forge):
    _install(forge, "task", _TASK_CLI_UNROUTABLE)
    log = forge["tmp"] / "task.log"
    r = _ship(forge, SHIP_ACCEPTANCE_GATE="1", FAKE_TASK_LOG=str(log))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "acceptance gate could not evaluate UB-25 on the Forgejo provider" in r.stderr
    assert "unrecognized github remote URL" in r.stderr
    assert "gate UB-25" in log.read_text()
    assert forge["fake"].merges() == []


def test_forgejo_acceptance_gate_fails_closed_without_task_cli(forge):
    r = _ship(forge, SHIP_ACCEPTANCE_GATE="1", PATH=f"{forge['bindir']}{os.pathsep}{_path_without_task()}")
    assert r.returncode != 0 and "task-cli not on PATH" in r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


def _machine_policy(ctx, owners) -> str:
    """A trusted Rig machine policy file (XDG layout, owner-only permissions)."""
    root = ctx["tmp"] / "xdg"
    (root / "rig").mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    cfg = root / "rig" / "config.yaml"
    cfg.write_text(json.dumps({"ci": {"items": {"ship": {"require_external_review": False,
                                                         "external_review_owners": owners}}}}))
    cfg.chmod(0o600)
    return str(root)


@pytest.mark.parametrize("owners,ok", [(["o"], True), (["alex-mextner"], False)])
def test_forgejo_reads_the_trusted_machine_external_review_policy(forge, owners, ok):
    py = next((c for c in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")
               if Path(c).exists() and subprocess.run([c, "-I", "-c", "import yaml"], capture_output=True).returncode == 0),
              None)
    if py is None:
        pytest.skip("no python3 with PyYAML (the policy reader's dependency) on this machine")
    pybin = forge["tmp"] / "pybin"
    pybin.mkdir()
    (pybin / "python3").symlink_to(py)
    forge["fake"].state["reviews"] = []
    r = _ship(forge, XDG_CONFIG_HOME=_machine_policy(forge, owners),
              PATH=f"{forge['bindir']}{os.pathsep}{pybin}{os.pathsep}{os.environ['PATH']}")
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    if ok:
        assert "disabled by Rig machine policy for o/r" in r.stdout
    assert len(forge["fake"].merges()) == (1 if ok else 0)


# --- --task-config: route the ticket through task-cli's standard --config ---------------------
# Real task-cli 0.18.2 answers, captured read-only from this repo's cwd (no -C):
#   task gate UB-25 --config <Tasks-app task.yaml> --json  -> exit 1, the JSON below
#   task gate UB-25 --config /nonexistent/task.yaml --json -> exit 2, "error: --config file not found: ..."
#   task gate UB-25 --config <broken yaml> --json          -> exit 2, "error: invalid YAML in ..."
_UB25_NOT_ACCEPTED = json.dumps({
    "id": "UB-25", "ok": False, "state": "in-progress", "gate_enabled": True, "post_merge_acceptance": None,
    "criteria": 3, "below_minimum": False, "proofless": [],
    "unchecked": [{"index": 1, "text": "Every accessible repository ... has an explicit verified task route"},
                  {"index": 2, "text": "Normal task commands from a repository select the intended tracker"},
                  {"index": 3, "text": "direct Forgejo issue mutations through CLI and API are blocked"}]})
_UB25_POST_MERGE = json.dumps({
    "id": "UB-25", "ok": True, "state": "in-progress", "gate_enabled": True,
    "post_merge_acceptance": "verified after the ship path is live", "criteria": 3, "below_minimum": False,
    "unchecked": json.loads(_UB25_NOT_ACCEPTED)["unchecked"], "proofless": []})

_TASK_BY_CONFIG = """#!/bin/sh
printf '%s|%s\\n' "$PWD" "$*" >> "$FAKE_TASK_LOG"
case "$*" in *--config*) : ;; *) echo "error: unrecognized github remote URL: x" >&2; exit 2 ;; esac
cfg=$(printf '%s\\n' "$@" | sed -n '/^--config$/{n;p;}')
[ -f "$cfg" ] || { echo "error: --config file not found: $cfg" >&2; exit 2; }
grep -q '^backend: tasks' "$cfg" || { echo "error: invalid YAML in $cfg: while parsing a flow node" >&2; exit 2; }
case "$1" in
  gate) printf '%s\\n' "$FAKE_GATE_JSON"; exit "$FAKE_GATE_RC" ;;
  mark-shipped) echo "marked shipped"; exit 0 ;;
  *) exit 0 ;;
esac
"""


# The same shape as the real Tasks-app routing file (backend + tasks.url + tasks.team).
_ROUTING_CONFIG = "backend: tasks\ntasks:\n  url: https://tasks.example.invalid\n  team: UB\n"


def _task_config(ctx, body=_ROUTING_CONFIG, name="tasks-task.yaml") -> Path:
    cfg = ctx["tmp"] / name
    cfg.write_text(body)
    return cfg


def _ship_with_task_config(ctx, cfg, gate_json, gate_rc, *args, **env):
    _install(ctx, "task", _TASK_BY_CONFIG)
    log = ctx["tmp"] / "task.log"
    r = _ship(ctx, "--task-config", str(cfg), *args, SHIP_ACCEPTANCE_GATE="1", SHIP_TASK_NOTIFY_ENABLED="1",
              FAKE_TASK_LOG=str(log), FAKE_GATE_JSON=gate_json, FAKE_GATE_RC=str(gate_rc), **env)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


def test_task_config_routes_gate_and_mark_shipped_and_never_closes(forge):
    cfg = _task_config(forge)
    r, calls = _ship_with_task_config(forge, cfg, _UB25_POST_MERGE, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    gate = [c for c in calls if "|gate UB-25" in c]
    marked = [c for c in calls if "|mark-shipped UB-25" in c]
    assert gate and marked and all(f"--config {cfg}" in c for c in gate + marked)
    assert not any("|done" in c or "|close" in c for c in calls)  # post-merge acceptance: never closed here
    assert all(c.split("|", 1)[0] == str(forge["main"]) and " -C " not in c and "--cwd" not in c for c in calls)
    assert len(forge["fake"].merges()) == 1


def test_task_config_not_accepted_ticket_refuses(forge):
    r, calls = _ship_with_task_config(forge, _task_config(forge), _UB25_NOT_ACCEPTED, 1)
    assert r.returncode != 0 and "UB-25" in r.stderr, r.stdout + r.stderr
    assert "Normal task commands from a repository" in r.stdout + r.stderr
    assert forge["fake"].merges() == []


def test_task_config_missing_file_refuses_before_any_gate(forge):
    r, calls = _ship_with_task_config(forge, forge["tmp"] / "missing.yaml", _UB25_POST_MERGE, 0)
    assert r.returncode != 0 and "is not a readable file" in r.stderr, r.stdout + r.stderr
    assert calls == [] and forge["fake"].merges() == []
    assert not [q for q in forge["fake"].requests if q["path"].startswith("/api/")]  # refused before any API call


@pytest.mark.parametrize("path", ["C:/nowhere/task.yaml", "C:\\nowhere\\task.yaml", "\\\\srv\\share\\task.yaml"])
def test_task_config_windows_absolute_path_is_not_rebased_on_cwd(forge, path):
    r, calls = _ship_with_task_config(forge, path, _UB25_POST_MERGE, 0)
    assert r.returncode != 0 and f"--task-config '{path}' is not a readable file" in r.stderr, r.stdout + r.stderr
    assert calls == [] and forge["fake"].merges() == []
    assert not [q for q in forge["fake"].requests if q["path"].startswith("/api/")]  # refused before any API call


def test_task_config_relative_path_resolves_against_the_callers_cwd(forge):
    """ship cds into the repo root before `task gate`; a relative --task-config still names the
    file beside the caller (ship's own cwd here is the main checkout)."""
    cfg = _task_config(forge)
    (forge["main"] / "routing.yaml").write_text(cfg.read_text())
    r, calls = _ship_with_task_config(forge, "routing.yaml", _UB25_POST_MERGE, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    gate = [c for c in calls if "|gate UB-25" in c]
    assert gate and all(f"--config {forge['main'] / 'routing.yaml'}" in c for c in gate)
def test_task_config_invalid_file_refuses_before_any_gate(forge):
    r, calls = _ship_with_task_config(forge, _task_config(forge, body="backend: [\n"), _UB25_POST_MERGE, 0)
    assert r.returncode != 0 and "routing-only" in r.stderr, r.stdout + r.stderr
    assert calls == [] and forge["fake"].merges() == []


@pytest.mark.parametrize("extra", [
    "enforce:\n  acceptance_checked: false\n",
    "gate: off\n",
    "acceptance_min: 0\n",
], ids=["enforce-block", "gate-key", "policy-key"])
def test_task_config_with_policy_override_is_refused_before_any_gate(forge, extra):
    r, calls = _ship_with_task_config(forge, _task_config(forge, body=_ROUTING_CONFIG + extra), _UB25_POST_MERGE, 0)
    assert r.returncode != 0 and "routing-only" in r.stderr and "not a routing key" in r.stderr, r.stdout + r.stderr
    assert calls == [] and forge["fake"].merges() == []


def test_task_config_gate_disabled_is_not_an_accepted_ticket(forge):
    disabled = dict(json.loads(_UB25_NOT_ACCEPTED), ok=True, gate_enabled=False)
    r, calls = _ship_with_task_config(forge, _task_config(forge), json.dumps(disabled), 0)
    assert r.returncode != 0 and "did not report an enforced acceptance" in r.stderr, r.stdout + r.stderr
    assert not any("|mark-shipped" in c for c in calls)
    assert forge["fake"].merges() == []


def test_forgejo_gate_disabled_without_task_config_is_refused_too(forge):
    disabled = dict(json.loads(_UB25_NOT_ACCEPTED), ok=True, gate_enabled=False)
    fake = "#!/bin/sh\n[ \"$1\" = gate ] || exit 0\nprintf '%s\\n' \"$FAKE_GATE_JSON\"\n"
    _install(forge, "task", fake)
    r = _ship(forge, SHIP_ACCEPTANCE_GATE="1", FAKE_GATE_JSON=json.dumps(disabled))
    assert r.returncode != 0 and "did not report an enforced acceptance" in r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


_guard = importlib.util.spec_from_file_location("task_config_guard", ROOT / "ci" / "ship" / "task_config_guard.py")
tcg = importlib.util.module_from_spec(_guard)
_guard.loader.exec_module(tcg)


@pytest.mark.parametrize("text", [
    _ROUTING_CONFIG,
    "# routing for UB\nbackend: tasks\ntasks:\n  url: 'https://x.example'\n  team: UB\n",
    "backend: forgejo\nforgejo:\n  repo: ultrabricks/agent-tools\n",
])
def test_task_config_guard_accepts_routing_files(text):
    assert tcg.parse_routing(text)["backend"] in ("tasks", "forgejo")


@pytest.mark.parametrize("text", [
    "",  # no backend
    "backend: tasks\n",  # no routing block
    "backend: linear\nlinear:\n  team: X\n",
    "backend: tasks\ntasks:\n  url: u\n  team: UB\n  enforce: false\n",
    "backend: tasks\ntasks:\n  url: u\ntasks:\n  team: UB\n",  # duplicate block
    "backend: tasks\ntasks: {url: u, team: UB}\n",
    "backend: tasks\ntasks:\n\turl: u\n",
    "backend: tasks\ntasks:\n  url: &a u\n  team: *a\n",
    "backend: tasks # comment\ntasks:\n  url: u\n",
    "backend: tasks\ntasks:\n    url: u\n",
    "---\nbackend: tasks\ntasks:\n  url: u\n",
])
def test_task_config_guard_refuses_everything_else(text):
    with pytest.raises(tcg.ConfigRefused):
        tcg.parse_routing(text)


# --- evidence ledger vs. the EXIT trap (process-substitution race) ----------------------------


def test_refusal_with_a_slow_background_gh_call_keeps_the_evidence_and_prints_no_stray_errors(forge):
    # `read ... < <(gh pr view ...)` runs the wrapper in a background subshell. The provider here
    # prints its answer and then lingers, so ship refuses (BEHIND) while that call is still in
    # flight. Its ledger outcome must be captured before the trap removes the ledger.
    _fake_python(forge, "headRefName", '"$REAL_PY" "$@"; rc=$?; sleep 1; exit $rc')
    forge["fake"].state["main_sha"] = "c" * 40
    import shutil
    r = _ship(forge, "--dry-run", REAL_PY=shutil.which("python3"))
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "BEHIND" in r.stderr, out
    assert "No such file" not in out, out
    assert "evidence ledger: 1 call(s) started, 1 completed, 0 failed" in r.stderr, out


def test_call_still_in_flight_at_the_final_preflight_refuses(forge):
    # A provider call that started but never recorded completion (simulated by an extra `start`
    # line written from inside an otherwise-successful call) must fail the pre-merge check.
    _fake_python(forge, "statusCheckRollup", 'echo start >> "$SHIP_FORGEJO_STATE_DIR/calls.log"')
    r = _ship(forge)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "did not complete" in r.stderr, r.stdout + r.stderr
    assert forge["fake"].merges() == []


def test_fj_refresh_never_runs_an_fj_from_the_working_directory(tmp_path, monkeypatch):
    """ship runs inside the checkout being shipped: a PR's own fj (fj.bat on Windows, where
    shutil.which searches the working directory first) must never be the one refreshed with."""
    names = ["fj.bat", "fj.cmd", "fj.exe"] if sys.platform == "win32" else ["fj"]
    repo = tmp_path / "pr-checkout"
    repo.mkdir()
    for name in names:
        (repo / name).write_text("#!/bin/sh\necho pwned\n")
        (repo / name).chmod(0o755)
    monkeypatch.chdir(repo)
    assert fp.find_fj(os.pathsep.join(["", ".", "relative/bin"])) is None
    real = tmp_path / "bin"
    real.mkdir()
    (real / names[-1]).write_text("#!/bin/sh\n")
    (real / names[-1]).chmod(0o755)
    assert fp.find_fj(os.pathsep.join([".", str(real)])) == str(real / names[-1])

"""Tests for the Forgejo provider of ci/ship/ship.sh (ci/ship/forgejo_provider.py).

Hermetic: a loopback fake Forgejo (synthetic API responses shaped after the Forgejo 16.0.3
Swagger) that also serves the test's bare origin repo over git's dumb HTTP protocol, so
ship.sh's own `git fetch origin` calls work against an `http://127.0.0.1:<port>/o/r.git`
origin. Pushes go to a local pushurl. A poison `gh` on PATH proves the real gh is never
reached on the Forgejo path. No real PR is ever touched.

    uv run --with pytest python -m pytest tests/test_ship_forgejo.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "ci" / "ship" / "ship.sh"
PROVIDER = ROOT / "ci" / "ship" / "forgejo_provider.py"
PLACEHOLDER = "YOUR_TOKEN_HERE"  # obviously fake credential for the synthetic key store


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
            "review_comments": {}, "timeline": [], "files": ["docs/x.md"], "commits": [],
            "branch_protection": {"enable_status_check": False, "status_check_contexts": []},
            "merge_head_override": None, "redirect_to": None,
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

        def listing(items):
            return 200, (items if page == 1 else []), "application/json"

        if sub == "/pulls/1" and method == "GET":
            return 200, s["pr"], "application/json"
        if sub == "/pulls/1" and method == "PATCH":
            s["pr"].update(body)
            return 201, s["pr"], "application/json"
        if sub == "/pulls/1/merge" and method == "POST":
            return self._merge(body)
        if sub.startswith("/branches/"):
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
            return (201, {}, "application/json") if method == "POST" else listing([])
        if sub == "/issues/1/timeline":
            return listing(s["timeline"])
        if sub == "/pulls/1/commits":
            return listing(s["commits"])
        if sub == "/pulls/1/files":
            return listing([{"filename": f} for f in s["files"]])
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
    })


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


def test_expired_oauth_token_fails_closed(tmp_path, monkeypatch):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"hosts": {"git.example.org": {"type": "OAuth", "name": "x", "token": PLACEHOLDER,
                                                             "refresh_token": PLACEHOLDER, "expires_at": 1}}}))
    monkeypatch.setenv("SHIP_FORGEJO_KEYS_FILE", str(keys))
    monkeypatch.delenv("SHIP_FORGEJO_TOKEN", raising=False)
    with pytest.raises(fp.ProviderError, match="expired"):
        fp.resolve_credential(fp.parse_origin("https://git.example.org/o/r.git"))


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


# --- every gh call site in ship.sh is mapped or fails closed ----------------------------------

MAPPED = {("pr", "view"), ("pr", "diff"), ("pr", "merge"), ("pr", "comment"), ("pr", "edit"),
          ("api", "graphql"), ("repo", "view")}
# Sites the Forgejo provider deliberately does NOT map (GitHub Actions evidence): each must fail.
UNSUPPORTED_PROBES = {
    ("run", "list"): ["run", "list", "--branch", "main"],
    ("run", "view"): ["run", "view", "1", "--json", "jobs"],
    ("api", "repos/{owner}/{repo}/actions/runs?head_sha=$head_sha&per_page=20"): ["api", "repos/o/r/actions/runs"],
    ("api", "repos/{owner}/{repo}/commits/$head_sha/check-runs?per_page=100"): ["api", "repos/o/r/commits/x/check-runs"],
    ("api", "repos/{owner}/{repo}/check-runs/$id/annotations"): ["api", "repos/o/r/check-runs/1/annotations"],
}


def _ship_gh_callsites() -> set:
    sites = set()
    for line in SHIP.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        code = re.sub(r"\b(echo|printf)\b.*", "", line)
        for m in re.finditer(r'(?:^|[\s($`])gh (pr|api|run|repo|issue|auth|release|search|workflow) +"?([^\s"]+)', code):
            sites.add((m.group(1), m.group(2)))
    return sites


def test_every_ship_gh_callsite_is_mapped_or_fails_closed(api_fake, tmp_path):
    sites = _ship_gh_callsites()
    assert ("pr", "merge") in sites and ("api", "graphql") in sites  # the scan actually sees ship.sh
    unclassified = sites - MAPPED - set(UNSUPPORTED_PROBES)
    assert not unclassified, f"new gh call site(s) in ship.sh need a Forgejo mapping or a fail-closed probe: {unclassified}"
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


def _ship_code_lines() -> str:
    """ship.sh without comment lines, with backslash continuations joined."""
    lines = [ln for ln in SHIP.read_text().splitlines() if not ln.lstrip().startswith("#")]
    return re.sub(r"\\\n\s*", " ", "\n".join(lines))


def test_every_ship_pr_view_json_field_is_mapped():
    fields = set()
    for m in re.finditer(r"\bgh pr view [^\n]*?--json ([A-Za-z,]+)", _ship_code_lines()):
        fields.update(f for f in m.group(1).split(",") if f)
    assert {"headRefName", "statusCheckRollup", "reviews"} <= fields  # the scan actually sees ship.sh
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
printf '{"task_code":"%s","passed_iterations":%s,"total_iterations":%s,"distinct_models_passed":1,"models":["m"],"distinct_roles_passed":%s,"roles":["backend","security","architecture"],"min_roles":3,"min_iter":3,"min_models":3,"passed":%s}\\n' "$2" "$n" "$n" "$n" "$ok"
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


def test_older_resolved_conversation_does_not_hide_a_newer_one_on_the_same_line(api_fake, tmp_path):
    _seed(api_fake)
    api_fake.state["reviews"] = [{"id": 5, "state": "COMMENT", "user": {"login": "r"}, "comments_count": 1},
                                 {"id": 6, "state": "COMMENT", "user": {"login": "r"}, "comments_count": 1}]
    same_line = {"path": "a.py", "position": 3, "original_position": 0}
    api_fake.state["review_comments"][5] = [{"id": 10, **same_line, "pull_request_review_id": 5,
                                             "original_commit_id": "1" * 40, "resolver": {"login": "x"}}]
    api_fake.state["review_comments"][6] = [{"id": 20, **same_line, "pull_request_review_id": 6,
                                             "original_commit_id": "2" * 40, "resolver": None}]
    assert _threads(api_fake, tmp_path) == [False, True]


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

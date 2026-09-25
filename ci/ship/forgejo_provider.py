#!/usr/bin/env python3
"""Forgejo provider adapter for ci/ship/ship.sh — the gh-shaped read/merge layer.

What it is: ship.sh's gates are written against the `gh` CLI (pr view --json, pr diff, the
reviewThreads/dwell GraphQL queries, pr merge). When a checkout's `origin` is a Forgejo host,
`gh` resolves to a different remote (for UB-25 that was an archived GitHub mirror), so every
gate would read the WRONG repository. Instead of cloning ship.sh's ~4000 lines of gate logic
into a weaker parallel script, ship.sh binds a shell FUNCTION named `gh` to this module
(`python3 -I forgejo_provider.py gh ...`). The same gate code then runs unchanged against
Forgejo data. The function is local to ship.sh's own shell: child processes (task-cli,
review-cli, the hatch helpers) never see it.

How it is reached: `ship.sh` -> provider dispatch (origin host is not github.com and not a
local path) -> `forgejo_provider.py detect` (Forgejo-only `/api/forgejo/v1/version` probe) ->
every later `gh ...` inside ship.sh -> `forgejo_provider.py gh ...`.

Invariants (fail closed, never silently skip):
  * Only the gh invocations ship.sh actually makes are emulated. Anything else — `gh run`,
    `gh api repos/...`, any GraphQL mutation, an unknown `--json` field — exits non-zero with
    an actionable reason. Every ship.sh call site treats a gh failure as refuse or as
    "no extra evidence" (never as a pass); see ci/ship/README.md "Forgejo provider".
  * The PR head SHA is pinned on first read (state dir from ship.sh). Every later read
    re-checks it, CI statuses are read for the PINNED sha, and the merge sends
    `head_commit_id` (Forgejo MergePullRequestOption) so the server refuses a raced push.
  * Credentials are bound to the origin host: fj's key store entry for exactly that host,
    or SHIP_FORGEJO_TOKEN only when SHIP_FORGEJO_TOKEN_HOST names the same host. GH_TOKEN /
    GITHUB_TOKEN are never read. Redirects are refused (no Authorization leaks to another
    host). Plain http is allowed only for a loopback origin (tests).
  * Tokens are never printed.

Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GITHUB_HOSTS = {"github.com", "www.github.com", "ssh.github.com"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
PAGE_LIMIT = 50
MAX_PAGES = 200
HTTP_TIMEOUT_S = 30
# Explicit UA: CDN fronts (Cloudflare) reject the default "Python-urllib/x.y" with HTTP 403.
USER_AGENT = "agent-tools-ship-forgejo-provider/1"


class ProviderError(Exception):
    """A fail-closed refusal with an actionable message (exit code 1 from the CLI)."""


# --- origin parsing ----------------------------------------------------------------------


@dataclass(frozen=True)
class Origin:
    scheme: str  # "https" or "http" (loopback only)
    host: str  # hostname, lowercase
    port: int | None  # API port (None = scheme default); SSH ports are never reused for API
    owner: str
    repo: str

    @property
    def api_base(self) -> str:
        return f"{self.scheme}://{self.host_key}"

    @property
    def host_key(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host  # IPv6 literal
        return host if self.port is None else f"{host}:{self.port}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


_SCP_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


def _split_repo_path(path: str) -> tuple[str, str]:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) != 2:
        raise ProviderError(f"origin path '{path}' is not '<owner>/<repo>'")
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ProviderError(f"origin path '{path}' is not '<owner>/<repo>'")
    return owner, repo


def parse_origin(url: str) -> Origin:
    """Parse a git remote URL into the Forgejo API origin it is bound to."""
    url = (url or "").strip()
    if not url:
        raise ProviderError("the checkout has no 'origin' remote")
    if "://" in url:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme in ("ssh", "git+ssh"):
            api_scheme, port = "https", None
        elif scheme == "https":
            api_scheme, port = "https", parsed.port
        elif scheme == "http" and host in LOOPBACK_HOSTS:
            api_scheme, port = "http", parsed.port
        else:
            raise ProviderError(f"origin scheme '{scheme}' is not supported for Forgejo (https or ssh only)")
        owner, repo = _split_repo_path(parsed.path)
    else:
        m = _SCP_RE.match(url)
        if not m:
            raise ProviderError(f"origin '{url}' is not a recognised remote URL")
        host, api_scheme, port = m.group("host").lower(), "https", None
        owner, repo = _split_repo_path(m.group("path"))
    if not host:
        raise ProviderError(f"origin '{url}' has no host")
    if host in GITHUB_HOSTS:
        raise ProviderError("origin is github.com — the Forgejo provider must not handle it")
    return Origin(api_scheme, host, port, owner, repo)


# --- credentials (bound to the origin host) -----------------------------------------------


@dataclass(frozen=True)
class Credential:
    header: str  # full Authorization header value; never printed
    source: str  # human label, e.g. "fj key store"


def _fj_keys_path() -> Path:
    override = os.environ.get("SHIP_FORGEJO_KEYS_FILE")
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    data = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(data) / "forgejo-cli" / "keys.json"


def _credential_from_fj(origin: Origin) -> Credential | None:
    path = _fj_keys_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ProviderError(f"fj key store {path} is unreadable ({exc.__class__.__name__})") from None
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, dict):
        raise ProviderError(f"fj key store {path} has no 'hosts' map — unsupported format")
    entry = hosts.get(origin.host_key)
    if entry is None:
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("token"), str) or not entry["token"]:
        raise ProviderError(f"fj key store entry for {origin.host_key} has no token — unsupported format")
    kind = entry.get("type")
    if kind == "OAuth":
        expires = entry.get("expires_at")
        if isinstance(expires, (int, float)) and expires <= time.time():
            raise ProviderError(f"fj OAuth token for {origin.host_key} expired — run `fj -H {origin.host_key} whoami` (refreshes it) or `fj auth login`")
        return Credential(f"Bearer {entry['token']}", "fj key store (OAuth)")
    return Credential(f"token {entry['token']}", "fj key store")


def resolve_credential(origin: Origin) -> Credential | None:
    """Token for exactly this origin host, or None (anonymous reads only)."""
    env_token = os.environ.get("SHIP_FORGEJO_TOKEN", "")
    if env_token:
        bound = os.environ.get("SHIP_FORGEJO_TOKEN_HOST", "").strip().lower()
        if bound != origin.host_key:
            raise ProviderError(
                "SHIP_FORGEJO_TOKEN is set but SHIP_FORGEJO_TOKEN_HOST does not name the origin host "
                f"'{origin.host_key}' — refusing to send a token that is not bound to this host")
        return Credential(f"token {env_token}", "SHIP_FORGEJO_TOKEN")
    return _credential_from_fj(origin)


# --- HTTP (no redirects, host-bound auth) -------------------------------------------------


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        raise ProviderError(
            f"Forgejo API answered HTTP {code} redirect to {urllib.parse.urlsplit(newurl).netloc or newurl!r} — "
            "refusing to follow (credentials stay bound to the origin host). Fix the origin URL.")


_OPENER = urllib.request.build_opener(_RefuseRedirects)


class Api:
    def __init__(self, origin: Origin, credential: Credential | None):
        self.origin = origin
        self.credential = credential

    def request(self, method: str, path: str, payload: object | None, accept: str) -> tuple[int, bytes]:
        url = f"{self.origin.api_base}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", accept)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.credential is not None:
            req.add_header("Authorization", self.credential.header)
        try:
            with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b""
        except (urllib.error.URLError, OSError) as exc:
            raise ProviderError(f"Forgejo API {method} {path} failed: {exc}") from None

    def call(self, method: str, path: str, payload: object | None = None, ok=(200,)) -> object:
        status, body = self.request(method, path, payload, "application/json")
        if status not in ok:
            raise ProviderError(_http_error_text(method, path, status, body, self.credential))
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise ProviderError(f"Forgejo API {method} {path} returned non-JSON") from None

    def text(self, path: str) -> str:
        status, body = self.request("GET", path, None, "text/plain")
        if status != 200:
            raise ProviderError(_http_error_text("GET", path, status, body, self.credential))
        return body.decode("utf-8", errors="replace")

    def paged(self, path: str) -> list:
        sep = "&" if "?" in path else "?"
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            chunk = self.call("GET", f"{path}{sep}page={page}&limit={PAGE_LIMIT}")
            if chunk is None:
                return items
            if not isinstance(chunk, list):
                raise ProviderError(f"Forgejo API GET {path} did not return a list")
            items.extend(chunk)
            if len(chunk) < PAGE_LIMIT:
                return items
        raise ProviderError(f"Forgejo API GET {path} exceeded {MAX_PAGES} pages — refusing a truncated read")

    def repo_path(self, suffix: str = "") -> str:
        o = urllib.parse.quote(self.origin.owner, safe="")
        r = urllib.parse.quote(self.origin.repo, safe="")
        return f"/api/v1/repos/{o}/{r}{suffix}"


def _http_error_text(method: str, path: str, status: int, body: bytes, cred: Credential | None) -> str:
    detail = ""
    try:
        parsed = json.loads(body) if body else None
        if isinstance(parsed, dict) and parsed.get("message"):
            detail = f": {str(parsed['message'])[:300]}"
    except ValueError:
        pass
    hint = ""
    if status in (401, 403):
        hint = " — authenticate for this host with `fj auth login` (or `fj auth add-token`)" if cred is None else " — the bound credential lacks permission"
    elif status == 404 and cred is None:
        hint = " — not found anonymously; a private repo needs `fj auth login` for this host"
    return f"Forgejo API {method} {path} -> HTTP {status}{detail}{hint}"


# --- PR model with a pinned head ----------------------------------------------------------


class PullRequest:
    """One PR read with its head pinned to the first sha this ship run observed."""

    def __init__(self, api: Api, number: int, state_dir: str | None):
        self.api = api
        self.number = number
        self.raw = api.call("GET", api.repo_path(f"/pulls/{number}"))
        if not isinstance(self.raw, dict):
            raise ProviderError(f"Forgejo returned no pull request #{number}")
        self.head_sha = _require_str(self.raw.get("head"), "sha", "head.sha")
        self.pinned_sha = _pin_head(state_dir, number, self.head_sha)
        self._cache: dict[str, object] = {}

    @property
    def base_ref(self) -> str:
        return _require_str(self.raw.get("base"), "ref", "base.ref")

    def cached(self, key: str, build):
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]


def _require_str(obj: object, key: str, label: str) -> str:
    value = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(value, str) or not value:
        raise ProviderError(f"Forgejo pull request payload has no {label} — unsupported response")
    return value


def _pin_head(state_dir: str | None, number: int, head_sha: str) -> str:
    """First read pins the head; later reads refuse if the PR head moved (race check)."""
    if not state_dir:
        return head_sha
    pin = Path(state_dir) / f"pr-{number}.head"
    if pin.exists():
        pinned = pin.read_text(encoding="utf-8").strip()
        if pinned != head_sha:
            raise ProviderError(
                f"PR #{number} head moved during this ship run ({pinned[:12]} -> {head_sha[:12]}) — "
                "gates were evaluated on the old head; re-run ship")
        return pinned
    pin.write_text(head_sha + "\n", encoding="utf-8")
    return head_sha


# --- field builders (GitHub `gh pr view --json` names -> Forgejo evidence) ----------------
# Swagger (Forgejo 16.0.3) definitions used: PullRequest{state,draft,merged,mergeable,
# merge_base,head{ref,sha,repo},base{ref,repo},created_at,title,body,html_url,
# merge_commit_sha}, Branch{commit.id,enable_status_check,status_check_contexts},
# CombinedStatus{statuses[CommitStatus{context,status,target_url,created_at,updated_at}]},
# PullReview{id,state,user,commit_id,submitted_at,dismissed}, Comment{user,body},
# Commit{sha,commit{message,committer{date}}}, PullReviewComment{id,path,position,
# original_position,commit_id,original_commit_id,pull_request_review_id,resolver},
# TimelineComment{type,created_at}.


def _pr_state(pr: PullRequest) -> str:
    raw = pr.raw
    if raw.get("merged"):
        return "MERGED"
    if raw.get("state") != "open":
        return "CLOSED"
    if raw.get("draft"):
        return "DRAFT"  # ship.sh refuses anything but OPEN
    return "OPEN"


def _pr_mergeable(pr: PullRequest) -> str:
    # Forgejo reports false both for conflicts and for "not computed yet"; either way the
    # merge must not proceed, so map every non-true to CONFLICTING (ship.sh refuses it).
    return "MERGEABLE" if pr.raw.get("mergeable") is True else "CONFLICTING"


def _pr_cross_repo(pr: PullRequest) -> bool:
    head_repo = (pr.raw.get("head") or {}).get("repo") or {}
    base_repo = (pr.raw.get("base") or {}).get("repo") or {}
    head_name, base_name = head_repo.get("full_name"), base_repo.get("full_name")
    return not head_name or head_name != base_name


def _base_branch(pr: PullRequest) -> dict:
    def build():
        ref = urllib.parse.quote(pr.base_ref, safe="")
        branch = pr.api.call("GET", pr.api.repo_path(f"/branches/{ref}"))
        if not isinstance(branch, dict):
            raise ProviderError(f"Forgejo returned no branch '{pr.base_ref}'")
        return branch
    return pr.cached("base_branch", build)


def _pr_merge_state(pr: PullRequest) -> str:
    # Strict: BEHIND whenever the base moved past the PR's merge base (exact-base gate).
    base_tip = _require_str(_base_branch(pr).get("commit"), "id", "base branch commit.id")
    merge_base = pr.raw.get("merge_base")
    if not isinstance(merge_base, str) or not merge_base:
        raise ProviderError("Forgejo pull request payload has no merge_base — cannot prove the PR is up to date")
    return "CLEAN" if merge_base == base_tip else "BEHIND"


_STATUS_MAP = {
    "success": {"state": "SUCCESS"},
    "failure": {"state": "FAILURE"},
    "error": {"state": "ERROR"},
    "pending": {"state": "PENDING"},
    # Forgejo "warning" has no GitHub equivalent; fail closed (counts as a failed check).
    "warning": {"state": "FAILURE"},
    "skipped": {"state": "SKIPPED", "status": "COMPLETED", "conclusion": "SKIPPED"},
}


def _status_context(status: dict) -> dict:
    mapped = _STATUS_MAP.get(str(status.get("status", "")).lower(), {"state": "ERROR"})
    return {
        "__typename": "StatusContext",
        "context": status.get("context") or "",
        "targetUrl": status.get("target_url") or "",
        "createdAt": status.get("updated_at") or status.get("created_at"),
        **mapped,
    }


def _combined_statuses(pr: PullRequest) -> list:
    path = pr.api.repo_path(f"/commits/{pr.pinned_sha}/status")
    statuses: list = []
    for page in range(1, MAX_PAGES + 1):
        combined = pr.api.call("GET", f"{path}?page={page}&limit={PAGE_LIMIT}")
        if not isinstance(combined, dict):
            raise ProviderError("Forgejo combined status response is not an object")
        chunk = combined.get("statuses") or []  # Forgejo sends null when there are none
        statuses.extend(chunk)
        total = combined.get("total_count")
        if len(chunk) < PAGE_LIMIT or (isinstance(total, int) and len(statuses) >= total):
            return statuses
    raise ProviderError("Forgejo combined status exceeded the page cap — refusing a truncated read")


def _required_contexts(pr: PullRequest) -> list:
    branch = _base_branch(pr)
    if not branch.get("enable_status_check"):
        return []
    contexts = branch.get("status_check_contexts")
    if not isinstance(contexts, list):
        raise ProviderError("base branch requires status checks but lists no contexts — unsupported protection")
    return [c for c in contexts if isinstance(c, str) and c]


def _pr_status_rollup(pr: PullRequest) -> list:
    rollup = [_status_context(s) for s in _combined_statuses(pr) if isinstance(s, dict)]
    seen = [c["context"] for c in rollup]
    for pattern in _required_contexts(pr):
        if not any(_glob_match(pattern, ctx) for ctx in seen):
            # A required check that never reported: watched as pending, then refused.
            rollup.append({"__typename": "StatusContext", "context": pattern, "state": "EXPECTED"})
    return rollup


def _glob_match(pattern: str, value: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(value, pattern)


_REVIEW_STATE = {"APPROVED": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}


def _pr_reviews(pr: PullRequest) -> list:
    reviews = []
    for r in _raw_reviews(pr):
        state = _REVIEW_STATE.get(str(r.get("state", "")).upper())
        if state is None:
            continue  # PENDING drafts and REQUEST_REVIEW entries are not submitted reviews
        reviews.append({
            "author": {"login": ((r.get("user") or {}).get("login") or "")},
            "state": "DISMISSED" if r.get("dismissed") else state,
            "submittedAt": r.get("submitted_at"),
            "commit": {"oid": r.get("commit_id") or ""},
        })
    return reviews


def _raw_reviews(pr: PullRequest) -> list:
    return pr.cached("reviews", lambda: pr.api.paged(pr.api.repo_path(f"/pulls/{pr.number}/reviews")))


def _pr_comments(pr: PullRequest) -> list:
    comments = pr.api.paged(pr.api.repo_path(f"/issues/{pr.number}/comments"))
    return [{"author": {"login": ((c.get("user") or {}).get("login") or "")}, "body": c.get("body") or ""}
            for c in comments if isinstance(c, dict)]


def _raw_commits(pr: PullRequest) -> list:
    path = pr.api.repo_path(f"/pulls/{pr.number}/commits?verification=false&files=false")
    return pr.cached("commits", lambda: pr.api.paged(path))


def _pr_commits(pr: PullRequest) -> list:
    out = []
    for c in _raw_commits(pr):
        commit = c.get("commit") or {}
        headline, _, body = str(commit.get("message") or "").partition("\n")
        out.append({
            "oid": c.get("sha") or "",
            "messageHeadline": headline,
            "messageBody": body.lstrip("\n"),
            "committedDate": (commit.get("committer") or {}).get("date"),
        })
    return out


FIELD_BUILDERS = {
    "headRefName": lambda pr: _require_str(pr.raw.get("head"), "ref", "head.ref"),
    "headRefOid": lambda pr: pr.pinned_sha,
    "baseRefName": lambda pr: pr.base_ref,
    "state": _pr_state,
    "isDraft": lambda pr: bool(pr.raw.get("draft")),
    "mergeable": _pr_mergeable,
    "isCrossRepository": _pr_cross_repo,
    "mergeStateStatus": _pr_merge_state,
    "statusCheckRollup": _pr_status_rollup,
    "title": lambda pr: pr.raw.get("title") or "",
    "body": lambda pr: pr.raw.get("body") or "",
    "url": lambda pr: pr.raw.get("html_url") or "",
    "number": lambda pr: pr.number,
    "createdAt": lambda pr: pr.raw.get("created_at"),
    "reviews": _pr_reviews,
    "comments": _pr_comments,
    "commits": _pr_commits,
    "mergeCommit": lambda pr: ({"oid": pr.raw["merge_commit_sha"]} if pr.raw.get("merge_commit_sha") else None),
}


def pr_json(pr: PullRequest, fields: list[str]) -> dict:
    unknown = [f for f in fields if f not in FIELD_BUILDERS]
    if unknown:
        raise ProviderError(f"`gh pr view --json {','.join(unknown)}` is unsupported on the Forgejo provider")
    return {f: FIELD_BUILDERS[f](pr) for f in fields}


# --- review threads and dwell (the two GraphQL shapes ship.sh reads) ----------------------


def _conversation_key(c: dict) -> tuple:
    """Identity of one Forgejo code conversation.

    Forgejo attaches a reply to the review it answers (PullReviewComment.pull_request_review_id)
    and resolves a conversation through its first comment (`resolver`). Keying on the review,
    the commit the comment was made against, the path and the signed line keeps an older
    resolved conversation from hiding a newer unresolved one on the same line after a push. A
    comment with no line at all (file-level or unmappable) is its own conversation.
    """
    line = (c.get("position") or 0, c.get("original_position") or 0)
    if line == (0, 0):
        return ("single", c.get("id"))
    return (c.get("pull_request_review_id"), c.get("original_commit_id") or c.get("commit_id") or "",
            c.get("path") or "", line)


def _review_threads(pr: PullRequest) -> list:
    conversations: dict[tuple, list] = {}
    for review in _raw_reviews(pr):
        rid = review.get("id")
        if not isinstance(rid, int):
            raise ProviderError("Forgejo review without an id — cannot read its comments")
        # Always fetched: comments_count on the list payload is not trusted to skip a review.
        for c in pr.api.paged(pr.api.repo_path(f"/pulls/{pr.number}/reviews/{rid}/comments")):
            if not isinstance(c, dict) or not isinstance(c.get("id"), int):
                raise ProviderError("Forgejo review comment without an id — cannot place it in a conversation")
            conversations.setdefault(_conversation_key(c), []).append(c)
    resolved = []
    for comments in conversations.values():
        comments.sort(key=lambda c: c["id"])
        resolved.append(bool(comments[0].get("resolver")))
    return resolved


def _pushed_at(pr: PullRequest) -> str | None:
    """Latest head push per the PR timeline (TimelineComment type "pull_push"), if any."""
    events = pr.api.paged(pr.api.repo_path(f"/issues/{pr.number}/timeline"))
    pushes = [e.get("created_at") for e in events if isinstance(e, dict) and e.get("type") == "pull_push"]
    pushes = [p for p in pushes if isinstance(p, str) and p]
    return max(pushes) if pushes else None


def _head_committed_at(pr: PullRequest) -> str:
    for c in _raw_commits(pr):
        if c.get("sha") == pr.pinned_sha:
            date = ((c.get("commit") or {}).get("committer") or {}).get("date")
            if isinstance(date, str) and date:
                return date
    raise ProviderError("pinned head commit not found in the PR commit list — cannot date the last push")


def graphql_payload(pr: PullRequest, query: str) -> dict:
    compact = re.sub(r"\s+", "", query)
    if compact.startswith("mutation"):
        raise ProviderError(
            "GraphQL mutations (e.g. resolveReviewThread) are unsupported on the Forgejo provider — "
            "resolve the conversation in the Forgejo web UI, then re-run ship")
    if "pullRequest(number:$pr)" not in compact:
        raise ProviderError("this `gh api graphql` query shape is unsupported on the Forgejo provider")
    if "reviewThreads" in compact:
        if compact.count("reviewThreads") != 1 or "nodes{isResolved}" not in compact:
            # e.g. the auto-resolve query: isOutdated and bot authorship have no Forgejo evidence.
            raise ProviderError("review-thread fields beyond isResolved (isOutdated, comment authors) have no Forgejo "
                                "equivalent — auto-resolve is unsupported; resolve conversations in the web UI")
        threads = [{"isResolved": r} for r in _review_threads(pr)]
        node = {"reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": threads}}
    elif "committedDate" in compact and "createdAt" in compact:
        node = {
            "createdAt": pr.raw.get("created_at"),
            "commits": {"nodes": [{"commit": {"committedDate": _head_committed_at(pr), "pushedDate": _pushed_at(pr)}}]},
            # Force pushes are already `pull_push` timeline events folded into pushedDate.
            "timelineItems": {"nodes": []},
        }
    else:
        raise ProviderError("this `gh api graphql` query shape is unsupported on the Forgejo provider")
    return {"data": {"repository": {"pullRequest": node}}}


# --- write operations -----------------------------------------------------------------------


def _require_write_credential(api: Api, action: str) -> None:
    if api.credential is None:
        raise ProviderError(f"{action} needs a credential bound to {api.origin.host_key} — "
                            "run `fj auth login` for that host (no GitHub token is ever used)")


_MERGE_DO = {"--squash": "squash", "--merge": "merge", "--rebase": "rebase"}


def merge_pr(pr: PullRequest, method_flag: str, admin: bool) -> str:
    """POST /pulls/{index}/merge with head_commit_id = the pinned head (server-side race check)."""
    api = pr.api
    _require_write_credential(api, "merging")
    do = _MERGE_DO.get(method_flag)
    if do is None:
        raise ProviderError(f"merge method {method_flag!r} is unsupported (use --squash, --merge or --rebase)")
    payload = {"Do": do, "head_commit_id": pr.pinned_sha, "delete_branch_after_merge": False}
    if admin:
        payload["force_merge"] = True  # the --skip-ci admin path; already hatch-approved by ship.sh
    status, body = api.request("POST", api.repo_path(f"/pulls/{pr.number}/merge"), payload, "application/json")
    if status != 200:
        raise ProviderError(_http_error_text("POST", f"merge of PR #{pr.number}", status, body, api.credential))
    # The 200 means the merge happened server-side: a failed confirmation read must not leave
    # it unaudited, so that path writes an `unconfirmed` line before refusing.
    try:
        after = api.call("GET", api.repo_path(f"/pulls/{pr.number}"))
    except ProviderError:
        _audit_merge(pr, do, admin, "", confirmed=False)
        raise
    if not isinstance(after, dict) or not after.get("merged"):
        _audit_merge(pr, do, admin, "", confirmed=False)
        raise ProviderError(f"Forgejo accepted the merge of PR #{pr.number} but does not report it merged — check the PR")
    merge_sha = str(after.get("merge_commit_sha") or "")
    _audit_merge(pr, do, admin, merge_sha)
    return merge_sha


def _audit_merge(pr: PullRequest, do: str, admin: bool, merge_sha: str, confirmed: bool = True) -> None:
    path = Path(os.environ.get("SHIP_AUDIT_FILE") or Path.home() / ".config/agent-tools/ship-audit.jsonl")
    decision = "merged:admin" if admin else "merged"
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "gate": "merge", "provider": "forgejo",
        "decision": decision if confirmed else f"{decision}:unconfirmed", "host": pr.api.origin.host_key,
        "repo": pr.api.origin.slug, "pr": pr.number, "head": pr.pinned_sha, "method": do,
        "merge_commit": merge_sha,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError as exc:  # the merge already happened; never mask it
        print(f"[ship] forgejo: merged, but could not write the audit line to {path}: {exc}", file=sys.stderr)


def comment_pr(pr: PullRequest, body: str) -> None:
    _require_write_credential(pr.api, "commenting")
    pr.api.call("POST", pr.api.repo_path(f"/issues/{pr.number}/comments"), {"body": body}, ok=(200, 201))


def edit_pr(pr: PullRequest, title: str | None, body: str | None) -> None:
    _require_write_credential(pr.api, "editing the PR")
    payload = {k: v for k, v in (("title", title), ("body", body)) if v is not None}
    if not payload:
        raise ProviderError("`gh pr edit` without --title/--body is unsupported on the Forgejo provider")
    pr.api.call("PATCH", pr.api.repo_path(f"/pulls/{pr.number}"), payload, ok=(200, 201))


def diff_pr(pr: PullRequest, name_only: bool) -> str:
    if name_only:
        files = pr.api.paged(pr.api.repo_path(f"/pulls/{pr.number}/files"))
        names = [f.get("filename") for f in files if isinstance(f, dict) and f.get("filename")]
        return "\n".join(names) + ("\n" if names else "")
    return pr.api.text(pr.api.repo_path(f"/pulls/{pr.number}.diff"))


# --- gh-shaped CLI --------------------------------------------------------------------------


def _unsupported(argv: list[str]) -> ProviderError:
    shown = " ".join(argv[:3])
    return ProviderError(f"`gh {shown} ...` is unsupported on the Forgejo provider — this gate has no "
                         "Forgejo evidence source, so ship refuses rather than guessing")


def _take_opts(args: list[str], with_value: set[str], flags: set[str]) -> tuple[list[str], dict]:
    """Split gh-style args into positionals and options (`--k v`, `--k=v`, `-f k=v` repeated)."""
    pos: list[str] = []
    opts: dict = {"fields": []}
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, inline = a.partition("=") if a.startswith("--") else (a, "", "")
        if name in flags and not eq:
            opts[name] = True
        elif name in with_value:
            if eq:
                value = inline
            else:
                i += 1
                if i >= len(args):
                    raise ProviderError(f"`{name}` needs a value")
                value = args[i]
            if name in ("-f", "-F", "--raw-field", "--field"):
                opts["fields"].append(value)
            else:
                opts[name] = value
        elif a.startswith("-"):
            raise ProviderError(f"option `{a}` is unsupported on the Forgejo provider")
        else:
            pos.append(a)
        i += 1
    return pos, opts


def _emit(value: object, jq_expr: str | None) -> str:
    doc = json.dumps(value)
    if not jq_expr:
        return doc + "\n"
    try:
        proc = subprocess.run(["jq", "-r", "-c", jq_expr], input=doc, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise ProviderError("jq is required by the Forgejo provider") from None
    if proc.returncode != 0:
        raise ProviderError(f"jq filter failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _pr_number(pos: list[str]) -> int:
    if len(pos) != 1 or not pos[0].isdigit():
        raise ProviderError("expected exactly one numeric PR argument")
    return int(pos[0])


class Context:
    def __init__(self, origin: Origin, state_dir: str | None):
        self.origin = origin
        self.state_dir = state_dir
        self._api: Api | None = None

    @property
    def api(self) -> Api:
        if self._api is None:
            self._api = Api(self.origin, resolve_credential(self.origin))
        return self._api

    def pr(self, number: int) -> PullRequest:
        return PullRequest(self.api, number, self.state_dir)


def gh_pr(ctx: Context, action: str, rest: list[str]) -> str:
    if action == "view":
        pos, o = _take_opts(rest, {"--json", "-q", "--jq"}, set())
        if "--json" not in o:
            raise ProviderError("`gh pr view` without --json is unsupported on the Forgejo provider")
        data = pr_json(ctx.pr(_pr_number(pos)), [f for f in o["--json"].split(",") if f])
        return _emit(data, o.get("-q") or o.get("--jq"))
    if action == "diff":
        pos, o = _take_opts(rest, set(), {"--name-only"})
        return diff_pr(ctx.pr(_pr_number(pos)), bool(o.get("--name-only")))
    if action == "merge":
        pos, o = _take_opts(rest, set(), {"--squash", "--merge", "--rebase", "--admin"})
        methods = [m for m in _MERGE_DO if o.get(m)]
        if len(methods) != 1:
            raise ProviderError("`gh pr merge` needs exactly one of --squash/--merge/--rebase")
        sha = merge_pr(ctx.pr(_pr_number(pos)), methods[0], bool(o.get("--admin")))
        return f"[forgejo] merged PR #{pos[0]} (merge commit {sha[:12]})\n"
    if action == "comment":
        pos, o = _take_opts(rest, {"--body", "-b"}, set())
        body = o.get("--body", o.get("-b"))
        if body is None:
            raise ProviderError("`gh pr comment` needs --body")
        comment_pr(ctx.pr(_pr_number(pos)), body)
        return ""
    if action == "edit":
        pos, o = _take_opts(rest, {"--title", "-t", "--body", "-b"}, set())
        edit_pr(ctx.pr(_pr_number(pos)), o.get("--title", o.get("-t")), o.get("--body", o.get("-b")))
        return ""
    raise _unsupported(["pr", action])


def gh_api(ctx: Context, rest: list[str]) -> str:
    if not rest or rest[0] != "graphql":
        raise _unsupported(["api", *(rest[:1] or [""])])
    _, o = _take_opts(rest[1:], {"-f", "-F", "--raw-field", "--field", "--jq", "-q"}, {"--paginate"})
    fields = dict(f.partition("=")[::2] for f in o["fields"])
    query = fields.get("query")
    if not query:
        raise ProviderError("`gh api graphql` without a query is unsupported")
    pr_arg = fields.get("pr", "")
    if not pr_arg.isdigit():
        raise ProviderError("this `gh api graphql` call has no numeric pr variable — unsupported")
    payload = graphql_payload(ctx.pr(int(pr_arg)), query)
    return _emit(payload, o.get("--jq") or o.get("-q"))


def gh_repo(ctx: Context, action: str, rest: list[str]) -> str:
    if action != "view":
        raise _unsupported(["repo", action])
    pos, o = _take_opts(rest, {"--json", "-q", "--jq"}, set())
    if pos and pos[0] and pos[0] != ctx.origin.slug:
        raise ProviderError(f"`gh repo view {pos[0]}` names a repo other than origin {ctx.origin.slug} — unsupported")
    known = {"nameWithOwner": ctx.origin.slug, "url": f"{ctx.origin.api_base}/{ctx.origin.slug}"}
    fields = [f for f in (o.get("--json") or "").split(",") if f]
    if not fields or any(f not in known for f in fields):
        raise ProviderError("`gh repo view` supports only --json nameWithOwner,url on the Forgejo provider")
    return _emit({f: known[f] for f in fields}, o.get("-q") or o.get("--jq"))


def run_gh(ctx: Context, argv: list[str]) -> str:
    if len(argv) >= 2 and argv[0] == "pr":
        return gh_pr(ctx, argv[1], argv[2:])
    if argv and argv[0] == "api":
        return gh_api(ctx, argv[1:])
    if len(argv) >= 2 and argv[0] == "repo":
        return gh_repo(ctx, argv[1], argv[2:])
    raise _unsupported(argv or [""])


# --- detection and entry point --------------------------------------------------------------


def detect(origin: Origin) -> str:
    """Positively identify a Forgejo instance (Forgejo-only /api/forgejo/v1/version)."""
    api = Api(origin, None)  # probe anonymously: never send a token before the host is confirmed
    info = api.call("GET", "/api/forgejo/v1/version")
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise ProviderError(f"{origin.host_key} did not answer the Forgejo version probe")
    return info["version"]


def _origin_from_env_or_git() -> Origin:
    url = os.environ.get("SHIP_FORGEJO_ORIGIN_URL")
    if url is None:
        proc = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
        url = proc.stdout.strip() if proc.returncode == 0 else ""
    return parse_origin(url)


RECEIPT_NAME = "fatal.log"


def record_fatal(state_dir: str | None, argv: list[str], reason: str) -> None:
    """Append to the per-run receipt ship.sh checks right before merging.

    Several ship.sh call sites tolerate a failed `gh` (`|| true`, `2>/dev/null`, a fallback
    value), so a non-zero exit alone could be swallowed. The receipt makes every provider
    failure in this run visible to the final pre-merge check, which refuses on any entry.
    """
    if not state_dir:
        return
    call = " ".join(a if len(a) <= 60 else a[:57] + "..." for a in argv[:4]).replace("\n", " ")
    try:
        with (Path(state_dir) / RECEIPT_NAME).open("a", encoding="utf-8") as fh:
            fh.write(f"gh {call}: {reason}\n".replace("\r", " "))
    except OSError as exc:
        print(f"[ship] forgejo provider: could not write the failure receipt: {exc}", file=sys.stderr)


def main(argv: list[str]) -> int:
    state_dir = os.environ.get("SHIP_FORGEJO_STATE_DIR")
    try:
        origin = _origin_from_env_or_git()
        if argv[:1] == ["detect"]:
            version = detect(origin)
            print(f"forgejo {version} {origin.host_key} {origin.slug}")
            return 0
        if argv[:1] == ["gh"]:
            sys.stdout.write(run_gh(Context(origin, state_dir), argv[1:]))
            return 0
        print("usage: forgejo_provider.py detect | gh <pr|api|repo> ...", file=sys.stderr)
        return 2
    except ProviderError as exc:
        reason = str(exc)
    except Exception as exc:  # noqa: BLE001 - any crash must be a recorded refusal, not a swallowed exit
        reason = f"internal error {exc.__class__.__name__}: {exc}"
    print(f"[ship] forgejo provider: {reason}", file=sys.stderr)
    if argv[:1] == ["gh"]:
        record_fatal(state_dir, argv[1:], reason)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

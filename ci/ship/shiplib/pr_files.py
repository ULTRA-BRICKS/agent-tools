"""The PR's changed files and its version-file diff, for a PR GitHub refuses to serve as a diff.

`gh pr diff` (with or without `--name-only`) fetches the whole diff, and GitHub answers HTTP 406
"the diff exceeded the maximum number of lines (20000)" for a big PR, so the version-bump and
screenshot gates could not evaluate it (agent-tools#1023). On GitHub the gates fall back here:

* the file LIST comes from the paginated pull-request files API. A partial list must never become
  a pass, so it is accepted only when its length equals `changedFiles` (read from the PR itself),
  the PR is within the API's 3000-file cap, and the PR head did not move while it was read;
* the version VALUE comes from the version file's contents at the PR head and at the merge-base.
  The files API cannot be used for it: on a huge PR GitHub drops the `patch` of most files.

Every failure returns a reason instead of a partial answer, and the caller refuses. Forgejo never
gets here: its provider already serves `--name-only` from `/pulls/N/files` and the full diff from
`.diff`, neither with a line limit, and it has no `gh api` REST passthrough.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

from shiplib.ctx import Result

# GET /repos/{owner}/{repo}/pulls/{n}/files serves at most this many files, however many pages.
FILES_API_CAP = 3000
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HEAD_MOVED = "the PR head moved while its files were being read"
_HEAD_UNCONFIRMED = "could not re-read the PR head to confirm it did not move"


@dataclass(frozen=True)
class PrMeta:
    head: str
    changed_files: int
    base: str


def _github_gh(ctx, *args: str, **kw) -> Result:
    """`gh ...` for a call that exists only on GitHub (REST paths, `changedFiles`). Deliberately not
    `ctx.gh`: that one routes through the Forgejo provider, whose fail-closed call-site map
    (tests/test_ship_forgejo.py) must not grow GitHub-only shapes. Anything else fails here."""
    if ctx.provider_resolved != "github":
        return Result(1, "", "GitHub-only call on provider %s" % ctx.provider_resolved)
    return ctx.cmd(["gh", *args], **kw)


def _tail(res: Result) -> str:
    """` (rc N: last stderr line)` of a failed call, so a refusal says what GitHub answered."""
    lines = [line for line in res.err.strip().split("\n") if line]
    return " (rc %d%s)" % (res.rc, ": " + lines[-1] if lines else "")


def _pr_meta(ctx) -> PrMeta | None:
    res = _github_gh(ctx, "pr", "view", ctx.pr, "--json", "headRefOid,changedFiles,baseRefName", stderr="devnull")
    if res.rc != 0:
        return None
    try:
        data = json.loads(res.out)
        head, count, base = data["headRefOid"], data["changedFiles"], data["baseRefName"]
    except (ValueError, KeyError, TypeError):
        return None
    if not (isinstance(head, str) and _SHA.fullmatch(head) and isinstance(base, str) and base):
        return None
    if not isinstance(count, int) or isinstance(count, bool):
        return None
    return PrMeta(head, count, base)


def _head_moved(ctx, head: str) -> str:
    """'' when the PR head is still `head`, else the reason it cannot be trusted to be."""
    after = _pr_meta(ctx)
    if after is None:
        return _HEAD_UNCONFIRMED
    return "" if after.head == head else _HEAD_MOVED


def _parse_file_names(text: str) -> list[str] | None:
    """One JSON string per line (`.filename | tojson`), so a name holding a newline cannot split."""
    names: list[str] = []
    for line in text.split("\n"):
        if not line:
            continue
        try:
            name = json.loads(line)
        except ValueError:
            return None
        if not isinstance(name, str) or not name:
            return None
        names.append(name)
    return names


def _list_via_files_api(ctx) -> tuple[list[str] | None, str]:
    before = _pr_meta(ctx)
    if before is None:
        return None, "could not read the PR's head and changed-file count"
    if before.changed_files < 1:
        return None, "the PR reports no changed files"
    if before.changed_files > FILES_API_CAP:
        return None, "the PR changes %d files and the files API serves at most %d" % (before.changed_files, FILES_API_CAP)
    res = _github_gh(
        ctx, "api", "--paginate", "repos/{owner}/{repo}/pulls/%s/files?per_page=100" % ctx.pr,
        "--jq", ".[].filename | tojson", stderr="capture",
    )
    if res.rc != 0:
        return None, "the files API request failed" + _tail(res)
    names = _parse_file_names(res.out)
    if names is None:
        return None, "the files API answer was not a list of file names"
    if len(set(names)) != len(names):
        return None, "the files API listed the same file more than once"
    if len(names) != before.changed_files:
        return None, "the files API returned %d files but the PR has %d" % (len(names), before.changed_files)
    why = _head_moved(ctx, before.head)
    if why:
        return None, why
    ctx.pr_files_head = before.head
    return names, ""


def pr_changed_files(ctx) -> tuple[list[str] | None, str]:
    """(paths, ''), or (None, why) when the complete list cannot be read. Read once per run."""
    if ctx.pr_files is not None:
        return ctx.pr_files, ""
    res = ctx.gh("pr", "diff", ctx.pr, "--name-only", stderr="devnull")
    if res.rc == 0:
        ctx.pr_files = [line for line in res.out.split("\n") if line]
        return ctx.pr_files, ""
    if ctx.provider_resolved != "github":
        return None, "gh pr diff --name-only failed (rc %d)" % res.rc
    names, why = _list_via_files_api(ctx)
    if names is None:
        return None, "gh pr diff --name-only failed (rc %d) and the files-API fallback could not list them: %s" % (res.rc, why)
    ctx.pr_files = names
    ctx.pr_files_from_api = True
    return names, ""


def _merge_base(ctx, meta: PrMeta) -> tuple[str | None, str]:
    res = _github_gh(
        ctx, "api", "repos/{owner}/{repo}/compare/%s...%s?per_page=1" % (quote(meta.base, safe="/"), meta.head),
        "--jq", ".merge_base_commit.sha", stderr="capture",
    )
    sha = res.out.strip()
    if res.rc == 0 and _SHA.fullmatch(sha):
        return sha, ""
    return None, "could not resolve the PR's merge-base" + _tail(res)


def _contents(ctx, path: str, sha: str) -> Result:
    return _github_gh(
        ctx, "api", "-H", "Accept: application/vnd.github.raw+json",
        "repos/{owner}/{repo}/contents/%s?ref=%s" % (quote(path), sha), stderr="capture",
    )


def _path_before_pr(ctx, path: str) -> tuple[str | None, str]:
    """Where `path` lived at the merge-base, for a file that does not exist there: '' for a file the
    PR adds, the previous path for a rename or copy; (None, why) for anything else. Without this a
    moved version file would read as a brand-new one and its unchanged version as a bump."""
    entry = ".[] | select(.filename == %s) | [.status, .previous_filename // \"\"] | tojson" % json.dumps(path)
    res = _github_gh(
        ctx, "api", "--paginate", "repos/{owner}/{repo}/pulls/%s/files?per_page=100" % ctx.pr, "--jq", entry,
        stderr="capture",
    )
    if res.rc != 0:
        return None, "the files API request failed" + _tail(res)
    try:
        rows = [json.loads(line) for line in res.out.split("\n") if line]
        (status, previous), = rows
    except (ValueError, TypeError):
        return None, "the files API has no single entry for %s" % path
    if status == "added":
        return "", ""
    if status in ("renamed", "copied") and isinstance(previous, str) and previous:
        return previous, ""
    return None, "%s is %s in the PR yet missing at its merge-base" % (path, status)


def _version_file_old_side(ctx, path: str, base: str) -> tuple[str | None, str]:
    res = _contents(ctx, path, base)
    if res.rc == 0:
        return res.out, ""
    if "HTTP 404" not in res.err:
        return None, "could not read %s at the PR merge-base%s" % (path, _tail(res))
    previous, why = _path_before_pr(ctx, path)
    if previous is None:
        return None, why
    if not previous:
        return "", ""
    res = _contents(ctx, previous, base)
    if res.rc != 0:
        return None, "could not read %s (renamed to %s) at the PR merge-base%s" % (previous, path, _tail(res))
    return res.out, ""


def version_file_diff(ctx, path: str) -> tuple[str | None, str]:
    """(unified diff of `path`, ''), or (None, why). Merge-base -> PR head, as GitHub shows a PR."""
    if ctx.provider_resolved != "github":
        return None, "the full diff is unavailable and the files-API fallback is GitHub-only"
    if not ctx.pr_files_head:
        # The list came from `gh pr diff --name-only`, which pins nothing: list it again through the
        # files API so the head the version is read at is the head the files were judged at. The
        # re-read list is deliberately not stored: it is proven equal to the one already judged.
        names, why = _list_via_files_api(ctx)
        if names is None:
            return None, why
        if set(names) != set(ctx.pr_files or ()):
            return None, "the PR's files changed since they were listed"
    meta = _pr_meta(ctx)
    if meta is None:
        return None, "could not read the PR's head commit"
    if meta.head != ctx.pr_files_head:
        return None, _HEAD_MOVED
    base, why = _merge_base(ctx, meta)
    if base is None:
        return None, why
    # The version file is located in the PR's own checkout, so it must exist at the PR head: a 404
    # there (deleted by the PR, or a fork commit the base repository does not serve) is no answer.
    new = _contents(ctx, path, meta.head)
    if new.rc != 0:
        return None, "could not read %s at the PR head%s" % (path, _tail(new))
    old, why = _version_file_old_side(ctx, path, base)
    if old is None:
        return None, why
    why = _head_moved(ctx, meta.head)
    if why:
        return None, why
    body = difflib.unified_diff(old.split("\n"), new.out.split("\n"), "a/" + path, "b/" + path, n=0, lineterm="")
    return "\n".join(["diff --git a/%s b/%s" % (path, path), *body]), ""

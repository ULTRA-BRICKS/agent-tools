#!/usr/bin/env bash
# Merge gate: FAIL if a PR has unresolved review threads.
#
# GitHub's "require conversation resolution" branch-protection toggle is the native way to
# do this, but it's admin-only and invisible from inside the repo. This script gives you
# the same gate as a portable CI check (and a pre-merge preflight in a ship script): it
# counts unresolved review threads and exits non-zero if any remain.
#
# Providers (REVIEW_THREADS_PROVIDER, default auto):
#   github   GraphQL `reviewThreads` via gh (authenticated). The default off-CI and whenever
#            $GITHUB_SERVER_URL is unset or https://github.com.
#   forgejo  Forgejo REST API v1 via stdlib python3 — picked automatically in a CI job whose
#            $GITHUB_SERVER_URL is NOT github.com (Forgejo / Gitea Actions). No gh, no jq.
#            API base = $GITHUB_API_URL (else $GITHUB_SERVER_URL/api/v1); token = $GH_TOKEN
#            (else $GITHUB_TOKEN) — the job token the workflow passes. Same conversation rule
#            as ci/ship's Forgejo provider: every review's comments are read; a conversation is
#            keyed by review id + original_commit_id + path + line (position/original_position);
#            a line-less comment is its own conversation; it is resolved only when its FIRST
#            comment has `resolver` (Forgejo resolves through the first comment).
#
# Reads the PR number from $1 or $PR_NUMBER.
#
# Knobs (env):
#   PR_NUMBER                PR number (or pass as $1).
#   GH_REPO                  owner/repo (github: default gh's current repo, honored by gh
#                            automatically; forgejo: default $GITHUB_REPOSITORY).
#   REVIEW_THREADS_PROVIDER  github | forgejo | auto (default). Set `github` on GitHub
#                            Enterprise Server, whose $GITHUB_SERVER_URL is not github.com.
#   IGNORE_OUTDATED          1 = skip threads GitHub marked outdated (github only; Forgejo has
#                            no outdated flag, so there every unresolved conversation counts).
#
# Usage:
#   sh ci/review-threads/review-threads.sh 123
#   PR_NUMBER=123 sh ci/review-threads/review-threads.sh
set -euo pipefail

PR="${1:-${PR_NUMBER:-}}"
[ -n "$PR" ] || { echo "Usage: $0 <pr-number>  (or set PR_NUMBER)" >&2; exit 2; }

IGNORE_OUTDATED="${IGNORE_OUTDATED:-0}"
PROVIDER="${REVIEW_THREADS_PROVIDER:-auto}"
if [ "$PROVIDER" = "auto" ]; then
  SERVER="${GITHUB_SERVER_URL:-}"
  SERVER="${SERVER%/}"
  if [ -n "$SERVER" ] && [ "$SERVER" != "https://github.com" ]; then
    PROVIDER=forgejo
  else
    PROVIDER=github
  fi
fi

# Count unresolved Forgejo conversations (prints one integer; non-zero exit on any API error).
forgejo_unresolved() {
  command -v python3 >/dev/null 2>&1 || { echo "python3 not found (the Forgejo provider needs it)" >&2; return 2; }
  local api="${GITHUB_API_URL:-}"
  if [ -z "$api" ]; then
    [ -n "${GITHUB_SERVER_URL:-}" ] || { echo "neither GITHUB_API_URL nor GITHUB_SERVER_URL is set" >&2; return 2; }
    api="${GITHUB_SERVER_URL%/}/api/v1"
  fi
  local repo="${GH_REPO:-${GITHUB_REPOSITORY:-}}"
  [ -n "$repo" ] || { echo "no repository: set GH_REPO or GITHUB_REPOSITORY (owner/repo)" >&2; return 2; }
  python3 - "$api" "$repo" "$PR" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

api, repo, pr = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
if not pr.isdigit():
    sys.exit(f"PR number must be numeric, got {pr!r}")
owner, _, name = repo.partition("/")
if not owner or not name or "/" in name:
    sys.exit(f"repository must be owner/repo, got {repo!r}")
token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
base = f"{api}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}/pulls/{pr}"
LIMIT, MAX_PAGES = 50, 200


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never follow a redirect: the Authorization header must not reach another host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused", headers, fp)


OPENER = urllib.request.build_opener(NoRedirect)


def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "agent-tools-review-threads/1"})
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with OPENER.open(req, timeout=30) as resp:
            body, total = resp.read(), resp.headers.get("X-Total-Count")
    except urllib.error.HTTPError as exc:
        sys.exit(f"Forgejo API GET {url} -> HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"Forgejo API GET {url} failed: {exc}")
    try:
        return json.loads(body), total
    except ValueError:
        sys.exit(f"Forgejo API GET {url} returned non-JSON")


def get_list(url):
    data, _ = get(url)
    if not isinstance(data, list):
        sys.exit(f"Forgejo API GET {url} did not return a list")
    return data


def paged(url):
    # Stop on X-Total-Count, else on an empty page — never on a short page, so a server whose
    # page cap is below LIMIT cannot truncate the read (a truncated read could hide a thread).
    items = []
    for page in range(1, MAX_PAGES + 1):
        chunk, total = get(f"{url}?page={page}&limit={LIMIT}")
        if not isinstance(chunk, list):
            sys.exit(f"Forgejo API GET {url} did not return a list")
        if not chunk:
            return items
        items.extend(chunk)
        if total is not None and total.strip().isdigit() and len(items) >= int(total):
            return items
    sys.exit(f"Forgejo API GET {url} exceeded {MAX_PAGES} pages — refusing a truncated read")


def key(c):
    line = (c.get("position") or 0, c.get("original_position") or 0)
    if line == (0, 0):
        return ("single", c.get("id"))
    return (c.get("pull_request_review_id"), c.get("original_commit_id") or c.get("commit_id") or "",
            c.get("path") or "", line)


conversations = {}
for review in paged(f"{base}/reviews"):
    rid = review.get("id") if isinstance(review, dict) else None
    if not isinstance(rid, int):
        sys.exit("Forgejo review without an id — cannot read its comments")
    # Always fetched: comments_count on the list payload is not trusted to skip a review.
    # (This endpoint is not paginated — it returns every comment of the review.)
    for c in get_list(f"{base}/reviews/{rid}/comments"):
        if not isinstance(c, dict) or not isinstance(c.get("id"), int):
            sys.exit("Forgejo review comment without an id — cannot place it in a conversation")
        conversations.setdefault(key(c), []).append(c)

unresolved = 0
for comments in conversations.values():
    comments.sort(key=lambda c: c["id"])
    if not comments[0].get("resolver"):
        unresolved += 1
print(unresolved)
PY
}

case "$PROVIDER" in
  forgejo)
    if [ "$IGNORE_OUTDATED" = "1" ]; then
      echo "note: IGNORE_OUTDATED=1 has no Forgejo equivalent (the API has no outdated flag) — counting every unresolved conversation." >&2
    fi
    # Fail-CLOSED on an API error: a silent 0 would let an unresolved PR merge.
    if UNRESOLVED=$(forgejo_unresolved) && [[ "$UNRESOLVED" =~ ^[0-9]+$ ]]; then
      :
    else
      echo "::error::could not query review conversations for PR #$PR (Forgejo API failed) — failing closed." >&2
      echo "FAIL: review-thread query failed for PR #$PR." >&2
      exit 1
    fi
    ;;
  github)
    command -v gh >/dev/null 2>&1 || { echo "gh CLI not found" >&2; exit 2; }

    # Paginate reviewThreads and count the unresolved ones. Using {owner}/{repo} placeholders
    # lets gh fill the current repo; override with GH_REPO / --repo if needed.
    QUERY='query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$pr){
      reviewThreads(first:100,after:$endCursor){
        pageInfo{hasNextPage endCursor}
        nodes{isResolved isOutdated}
      }
    }
  }
}'

    # Count unresolved threads (sum across pages). Outdated-but-unresolved still count by
    # default — set IGNORE_OUTDATED=1 to skip threads GitHub marked outdated.
    if [ "$IGNORE_OUTDATED" = "1" ]; then
      JQ='[.data.repository.pullRequest.reviewThreads.nodes[] | select((.isResolved|not) and (.isOutdated|not))] | length'
    else
      JQ='[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved|not)] | length'
    fi

    # Fail-CLOSED on an API error: a silent 0 would let an unresolved PR merge. Check gh's
    # exit status, not just the parsed count.
    if RAW=$(gh api graphql --paginate \
         -F owner='{owner}' -F name='{repo}' -F pr="$PR" \
         -f query="$QUERY" --jq "$JQ" 2>/dev/null); then
      UNRESOLVED=$(printf '%s' "$RAW" | awk '{s+=$1} END{print s+0}')
    else
      echo "::error::could not query review threads for PR #$PR (gh api failed) — failing closed." >&2
      echo "FAIL: review-thread query failed for PR #$PR." >&2
      exit 1
    fi
    ;;
  *)
    echo "unknown REVIEW_THREADS_PROVIDER '$PROVIDER' (github | forgejo | auto)" >&2
    exit 2
    ;;
esac

if [ "${UNRESOLVED:-0}" != "0" ]; then
  echo "::error::PR #$PR has $UNRESOLVED unresolved review thread(s) — resolve every thread before merging." >&2
  echo "FAIL: $UNRESOLVED unresolved review thread(s) on PR #$PR." >&2
  exit 1
fi
echo "PASS: PR #$PR has no unresolved review threads."

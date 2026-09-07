#!/usr/bin/env python3
"""agents-hooks/v1 pre-write hook — block writing secrets to files.

Inspects the content an agent is about to write/edit into a file and BLOCKS if it
contains a likely live secret: an API key / token assignment, a bearer credential,
or a private-key PEM block. The point is to catch the leak before it touches disk —
earlier than the no-secrets git-hook, which only fires at commit time.

Contract (agents-hooks/v1):
  stdin  : JSON event; the proposed content is in args.content (fall backs below),
           the target path in args.path / args.file_path.
  stdout : protocol JSON only
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": if the scan can't run, deny rather than risk writing a secret.

This is a heuristic pre-filter, NOT a replacement for a dedicated scanner (gitleaks).
It deliberately ignores obvious placeholders so it doesn't block templates/examples.
"""

from __future__ import annotations

import json
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Paths where example/placeholder secrets are expected — don't gate these.
ALLOW_PATH = re.compile(r"(?:\.example$|\.sample$|/fixtures?/|/__tests__/|\.lock$)")

# A value that is clearly a placeholder, not a real secret.
PLACEHOLDER = re.compile(
    r"(?:your[_-]?|example|placeholder|dummy|changeme|xxx+|<[^>]+>|\.\.\.|\bredacted\b)",
    re.IGNORECASE,
)

# Heuristic secret patterns. Each is (label, regex). Kept conservative to limit
# false positives on a public-repo-safety hook.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic api/secret token assignment",
     re.compile(r"(?i)\b(?:api[_-]?key|secret|token|access[_-]?key|client[_-]?secret|"
                r"password|passwd)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-+/=]{16,})['\"]?")),
    ("bearer credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{20,}")),
    ("slack token", re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}\b")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
]


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-secrets-write: {msg}\n")


# The ONE documented carve-out from the generic assignment pattern: a URL QUERY PARAMETER
# (`…?token=ultra-invalid-proof`, the key preceded by `?`/`&`) whose value is a SLUG — lowercase
# words joined by hyphens, nothing else. That is a human-readable marker quoted in prose (an
# audit report describing a request), never a credential: a real token carries digits, mixed
# case or symbols. Anything else in query position — `?token=AbCdEfGhIjKlMnOpQrStUvWx` written
# into source — is still an assignment and still blocked.
URL_QUERY_SLUG = re.compile(r"^[a-z]+(?:-[a-z]+)+$")


def looks_real(matched_value: str) -> bool:
    """A captured value is a likely-real secret if it isn't an obvious placeholder."""
    return not PLACEHOLDER.search(matched_value)


def is_url_query_slug(content: str, m: re.Match[str]) -> bool:
    """True when the generic-assignment match is a `?key=slug` / `&key=slug` query parameter
    whose value is a hyphenated lowercase slug (see URL_QUERY_SLUG)."""
    if m.start() == 0 or content[m.start() - 1] not in "?&" or not m.lastindex:
        return False
    return bool(URL_QUERY_SLUG.match(m.group(m.lastindex)))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-secrets-write: could not inspect the write (fail-closed)")
        return BLOCK_EXIT_CODE

    args = event.get("args") or {}
    path = args.get("path") or args.get("file_path") or event.get("command") or ""
    content = args.get("content") or args.get("new_string") or args.get("text") or ""
    if not isinstance(content, str):
        content = str(content)

    if isinstance(path, str) and ALLOW_PATH.search(path):
        emit("allow")  # example/fixture file — placeholders expected
        return 0

    label = first_real_secret(content)
    if label is not None:
        emit(
            "block",
            f"Refusing to write what looks like a {label} into {path or 'a file'}. "
            "Secrets belong in env / a secret manager referenced via config, never "
            "in source. If this is a placeholder, make it obviously fake (e.g. "
            "'YOUR_KEY_HERE') or use a .example file.",
        )
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


def first_real_secret(content: str) -> str | None:
    """The label of the first likely-real secret in `content`, or None.

    EVERY match of every pattern is judged (`finditer`), so a placeholder or a slug query
    parameter earlier in the file never shadows a live credential later in it — a single
    `search` + `continue` skipped the whole pattern on its first benign hit (review finding)."""
    for label, pattern in PATTERNS:
        for m in pattern.finditer(content):
            if label.startswith("generic") and is_url_query_slug(content, m):
                continue
            captured = m.group(m.lastindex) if m.lastindex else m.group(0)
            if looks_real(captured):
                return label
    return None


if __name__ == "__main__":
    sys.exit(main())

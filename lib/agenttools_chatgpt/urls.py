"""Permanent conversation identities shared by CLI and local control API."""

import re
from urllib.parse import urlsplit


def canonical_chat(raw: str) -> str:
    if (
        not isinstance(raw, str)
        or re.fullmatch(
            r"https://chatgpt\.com/(?:g/g-p-[a-f0-9]{32}(?:-[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*)?/)?c/[A-Za-z0-9_-]+/?",
            raw,
        )
        is None
    ):
        raise ValueError("Use an exact canonical permanent conversation URL")
    parsed = urlsplit(raw)

    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
        raise ValueError(
            "Use an exact permanent https://chatgpt.com/c/<id> conversation URL"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("Conversation URL must have no query or fragment")
    chat_id = parsed.path.rstrip("/").rsplit("/", 1)[1]
    if parsed.path.startswith("/g/") and not re.fullmatch(
        r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", chat_id, re.IGNORECASE
    ):
        raise ValueError("Project conversation ID must be an exact UUID")
    return "https://chatgpt.com/c/" + chat_id

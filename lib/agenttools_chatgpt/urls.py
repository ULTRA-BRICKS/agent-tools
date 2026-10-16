"""Permanent conversation identities shared by CLI and local control API."""

import re
from urllib.parse import urlsplit


def canonical_chat(raw: str) -> str:
    if (
        not isinstance(raw, str)
        or re.fullmatch(r"https://chatgpt\.com/c/[A-Za-z0-9_-]+/?", raw) is None
    ):
        raise ValueError("Use an exact canonical permanent conversation URL")
    parsed = urlsplit(raw)

    if (
        parsed.scheme != "https"
        or parsed.netloc != "chatgpt.com"
        or not re.fullmatch(r"/c/[A-Za-z0-9_-]+/?", parsed.path)
    ):
        raise ValueError(
            "Use an exact permanent https://chatgpt.com/c/<id> conversation URL"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("Conversation URL must have no query or fragment")
    return "https://chatgpt.com" + parsed.path.rstrip("/")

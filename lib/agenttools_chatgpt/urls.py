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


def conversation_identity(url: object) -> str | None:
    """The one identity of an open page URL, or None when it is not a conversation.

    Trailing slash, query, fragment and project (`/g/g-p-<id>-<slug>/c/<uuid>`) variants of
    one conversation all map to `https://chatgpt.com/c/<id>`. Everything else
    (`/space?tab=all`, `about:blank`, other chatgpt.com pages, a project URL whose id is
    not a UUID, any external site) is an auxiliary page. This is `canonical_chat` applied
    to what a browser tab shows: query and fragment are dropped first, and a
    non-conversation URL yields None instead of raising.
    """
    if not isinstance(url, str):
        return None
    try:
        return canonical_chat(urlsplit(url)._replace(query="", fragment="").geturl())
    except ValueError:
        return None

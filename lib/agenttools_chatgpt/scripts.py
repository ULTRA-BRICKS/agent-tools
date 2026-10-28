"""Browser-injected scripts that share one conversation-root contract."""

from pathlib import Path

_DIRECTORY = Path(__file__).parent
_MARKER = "/*@conversation-root*/"


def shared() -> str:
    """The shared conversation-root helper text (it also defines `approvalFacts`)."""
    return (_DIRECTORY / "conversation_root.js").read_text(encoding="utf-8")


def load(name: str) -> str:
    """Read an injected script and splice in the shared conversation-root helper."""
    script = (_DIRECTORY / name).read_text(encoding="utf-8")
    if script.count(_MARKER) != 1:
        raise ValueError(f"{name} must contain exactly one {_MARKER} marker")
    return script.replace(_MARKER, shared())

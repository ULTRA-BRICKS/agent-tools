"""agenttools_tg_inbox — the tg-ctl Stop-hook inbox reader (agent-tools#526, tg-cli#306).

Accessed via: ``omp_hook_bridge`` on yield→stop calls :func:`stop_inbox_text` (which wraps
:func:`consume_pending`) and, when it returns text, blocks the yield with the messages as
the reason — omp then feeds that text to the agent as its next instruction. The only
writer left is tg-ctl's grant-notice fallback for an omp whose omp-live endpoint is
unreachable. Claude Code and Codex are tmux-only (tg-cli#456): tg-ctl no longer queues for
them, archives their stranded backlog once as ``stranded-<stamp>.jsonl``, and their bridges
no longer read the inbox. pi has no session-end block contract.

THE INBOX KEY CONTRACT — shared byte-for-byte with tg-cli ``features/tg-ctl/unreachable.ts``
(both READMEs document it; the test vectors in ``tests/test_tg_inbox.py`` and tg-cli's
``tests/ctl-unreachable.test.ts`` are identical on purpose):

    key = sanitized(--name value)          when the agent's argv carries ``--name X``
        = "cwd-" + sha256(cwd)[:16] (hex)   otherwise
    sanitized(x) = every char outside [A-Za-z0-9._-] -> "_", truncated to 64 chars;
                   an empty result counts as "no name".
    cwd = the agent process's working directory with trailing "/" stripped ("/" stays).
    inbox dir = <tg-cli config dir>/inbox/<key>/
                (config dir = $TG_CTL_CONFIG_DIR, else ~/.config/tg-cli)
    pending.jsonl                     -- appended by the tg-ctl daemon, one object per line
    delivered-<pid>-<ns>-<rnd>.jsonl  -- ONE complete file per consumption by this reader
                                         (written as tmp-…, then renamed — never appended)
    acked.jsonl                       -- appended by the daemon after reacting on Telegram

Assumptions: the inbox is a local file written by the user's own tg-ctl daemon; its
content is DATA for the agent (rendered as the block reason), never executed here. A
missing, empty or malformed inbox never blocks (fail-open, logged to the hooks debug log). Delivery is
at-most-once: entries are archived to a delivered-* batch BEFORE the block reason is
returned, and ``omp_hook_bridge`` consumes the inbox as the LAST step of a yield (after
the v1 stop hooks ran), so a crashing hook cannot eat a message that was never shown.
"""

from __future__ import annotations

from .core import (
    agent_key,
    combine_stop_parts,
    consume_pending,
    format_block_reason,
    inbox_dir,
    inbox_root,
    sanitize_agent_name,
    stop_inbox_text,
)

__all__ = [
    "agent_key",
    "combine_stop_parts",
    "consume_pending",
    "format_block_reason",
    "inbox_dir",
    "inbox_root",
    "sanitize_agent_name",
    "stop_inbox_text",
]

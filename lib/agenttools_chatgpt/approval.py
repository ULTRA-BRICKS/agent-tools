"""A native platform approval card is the platform waiting for a human, not a response in flight.

Accessed via: LiveAdapter.observe (live_browser.py) merges the page's card facts (the
`approvalFacts` helper of conversation_root.js) into the
structural observation; the single-tab patrol (patrol.py) classifies and leaves the tab on the
merged result. Nothing here reads page text, clicks, sends or answers anything.

Assumptions: the card is the ChatGPT/Codex tool-permission UI, a `data-codex-approval-surface` div
holding one form with permission buttons inside the LATEST turn (`platformApprovalCard` finds it).
The latest turn still reads `in_progress` and shows no Stop, so the structural observation calls
it running and pins the tab. `ApprovalObservation` is the only carrier of the effective state; a
plain `Observation` (every other adapter, a missing field, an unreadable page) is never approval
and keeps today's behaviour. It lives here, not in core.py, so the dispatch core stays untouched.

Past bugs it guards against: one pending approval card (Stop absent, no draft) made the whole
account's single tab a protected generator and starved every other queued chat. The veto is
structural: an approval observation never passes `safe_failure` or `operator_candidate`, even
with `running` false, and always reads `turn_state == "in_progress"`, so the continuation
refusal (`observation_refusal`) rejects it as it does any generating turn.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .core import Observation


@dataclass(frozen=True)
class ApprovalObservation(Observation):
    """An observation whose latest turn waits on a native approval card (default: it does not)."""

    approval_wait: bool = False

    def __post_init__(self) -> None:
        # The raw page state stays recorded as in_progress; an inconsistent caller cannot launder
        # a card into a continuable (complete/cancelled/unknown) turn.
        if self.approval_wait and self.turn_state != "in_progress":
            object.__setattr__(self, "turn_state", "in_progress")

    def safe_failure(self) -> bool:
        return not self.approval_wait and super().safe_failure()

    def operator_candidate(self) -> bool:
        return not self.approval_wait and super().operator_candidate()

    @property
    def awaiting_approval(self) -> bool:
        """The effective `permission-blocked` state: a pending card and nothing else claims the page.

        Stop, a draft, a challenge, quota, an ambiguous page or missing identity each win and keep
        their own state; the card never releases or overrides them.
        """
        return (
            self.approval_wait
            and bool(self.chat and self.turn and self.generation)
            and not (self.status or self.active)
            and not any(
                (self.running, self.draft, self.challenge, self.ambiguous, self.quota)
            )
        )


def flagged(observed: Observation) -> bool:
    """The observation carries a card flag, whatever else is true of it (a veto signal)."""
    return isinstance(observed, ApprovalObservation) and observed.approval_wait


def pending(observed: Observation) -> bool:
    """The effective permission-blocked state; a plain Observation is never pending."""
    return isinstance(observed, ApprovalObservation) and observed.awaiting_approval


def _wrapped(script: str) -> str:
    """A script file holding one arrow function, as a parenthesised expression."""
    return "(" + script.strip().removesuffix(";") + ")"


def observe_script(live: str, shared: str) -> str:
    """One page.evaluate function returning {structural, approval} from a single instant.

    `live` is the live_dom.js arrow function; `shared` is conversation_root.js, whose helpers
    and `approvalFacts` run here in their own scope so they cannot clash with `live`'s copy.
    """
    return (
        "(args) => ({structural: "
        + _wrapped(live)
        + "(args), approval: (() => {"
        + shared
        + "\nreturn approvalFacts();})()})"
    )


def _facts(raw: object) -> tuple[bool, bool, str] | None:
    """(card, stop, turn) from the page's answer; any other shape or type is no evidence."""
    if not isinstance(raw, dict) or set(raw) != {"card", "stop", "turn"}:
        return None
    card, stop, turn = raw["card"], raw["stop"], raw["turn"]
    if type(card) is not bool or type(stop) is not bool or type(turn) is not str:
        return None
    return card, stop, turn


def merge(structural: Observation, raw: object) -> Observation:
    """The structural observation, or the effective approval observation when every fact agrees.

    The card must be the unique one of the latest turn the structural read also names, the turn
    must read `in_progress` with no Stop (neither read saw one), and nothing else may claim the
    page: no draft, challenge, quota, failure status or uncertain state. Anything missing,
    malformed or disagreeing returns the structural observation unchanged (still protected).
    """
    facts = _facts(raw)
    if facts is None:
        return structural
    card, stop, turn = facts
    if not (
        card
        and not stop
        and structural.chat
        and structural.turn
        and turn == structural.turn
        and structural.turn_state == "in_progress"
        and structural.running
        and not structural.status
        and not structural.active
        and not any(
            (
                structural.draft,
                structural.challenge,
                structural.quota,
                structural.ambiguous,
            )
        )
    ):
        return structural
    return ApprovalObservation(
        **{**asdict(structural), "running": False, "approval_wait": True}
    )

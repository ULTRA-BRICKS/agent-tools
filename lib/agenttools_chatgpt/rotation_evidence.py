"""The 429 transport evidence ledger of the single-tab rotation (a mixin of `Rotation`).

Accessed via: rotation.py (`Rotation` inherits `TransportEvidence`; the ledger lives in the
rotation file's `transport` field) and patrol.py (which feeds it the browser's own response
events through the rotation).

Assumptions: the ledger records what the browser's own responses showed, per EXACT endpoint path
(a query string is never part of it). It is the only basis for releasing a machine 429 hold: no
clock, no DOM, no probe of our own. A repeat of an already-recorded state is persisted at most
every `EVIDENCE_SAVE_GAP` seconds; every transition is persisted at once.

Past bugs it guards against: a fresh conversation-list 200 hiding a newer per-chat 429, and a
write per response during a burst.
"""

from __future__ import annotations

import math
import re

TRANSPORT_LIMIT = 128
ENDPOINT_PATTERN = re.compile(r"/[A-Za-z0-9._~%/-]{1,200}")
# A repeat of an already-recorded transport state (a stream of 401s, a polling endpoint that
# keeps succeeding) is persisted at most this often; every transition is persisted at once.
EVIDENCE_SAVE_GAP = 1.0


def endpoint_ok(path: object) -> bool:
    return isinstance(path, str) and ENDPOINT_PATTERN.fullmatch(path) is not None


class TransportEvidence:
    """Ledger methods; the host provides `transport`, `clock`, `cooling`, `save` and
    `_evidence_saved`."""

    def note_restricted(self, path: object, at: float) -> None:
        """A 429 for `path` at `at`. An endpoint that cannot be tracked exactly blocks release."""
        t = self.transport
        restricted = t["restricted"]
        if not endpoint_ok(path):
            self.note_blocked(at)
            return
        full = path not in restricted and len(restricted) >= TRANSPORT_LIMIT
        if full and not self._evict_verified():
            self.note_blocked(at)
            return
        if at > restricted.get(path, -math.inf):
            fresh = path not in restricted
            voids = t["succeeded"].get(path, -math.inf) > restricted.get(
                path, -math.inf
            )
            restricted[path] = at
            self._persist_evidence(fresh or voids)

    def _evict_verified(self) -> bool:
        """Make room by forgetting the oldest endpoint that already has a later success."""
        t = self.transport
        done = [
            p
            for p, at in t["restricted"].items()
            if t["succeeded"].get(p, -math.inf) > at
        ]
        if not done:
            return False
        oldest = min(done, key=lambda p: t["succeeded"][p])
        del t["restricted"][oldest]
        t["succeeded"].pop(oldest, None)
        return True

    def note_success(self, path: object, at: float) -> None:
        """A 2xx for `path`: counted only for an endpoint that was restricted, and only if later."""
        t = self.transport
        if not endpoint_ok(path) or path not in t["restricted"]:
            return
        done = t["succeeded"].get(path, -math.inf)
        if at > t["restricted"][path] and at > done:
            first = done <= t["restricted"][path]
            t["succeeded"][path] = at
            self._persist_evidence(first)

    def note_blocked(self, at: float) -> None:
        """A newer 401/403/quota (or an untrackable 429) voids every older success."""
        t = self.transport
        blocked = t["blocked_at"]
        if blocked is None or at > blocked:
            voids = blocked is None or any(
                ok > blocked for ok in t["succeeded"].values()
            )
            t["blocked_at"] = at
            self._persist_evidence(voids)

    def _persist_evidence(self, transition: bool) -> None:
        """Save now for a transition; a repeat of a recorded state rides on the next save."""
        now = self.clock()
        if transition or now - self._evidence_saved >= EVIDENCE_SAVE_GAP:
            self._evidence_saved = now
            self.save()

    def hold_unverified(self) -> str:
        """Why the machine 429 hold may not be released now; empty only when it is verified.

        Verified means: the cooldown (the real Retry-After) is over, every endpoint that was
        restricted has a strictly later success of the SAME exact path, and no auth/quota signal
        is newer than any of those successes. An elapsed clock alone is never enough.
        """
        t = self.transport
        if not t["restricted"]:
            return "no-restriction-evidence"
        if self.cooling():
            return "cooldown"
        if self.unverified_paths():
            return "unverified"
        blocked = t["blocked_at"]
        if blocked is not None and any(
            t["succeeded"][path] <= blocked for path in t["restricted"]
        ):
            return "blocked-newer"
        return ""

    def unverified_paths(self) -> list[str]:
        t = self.transport
        return sorted(
            p
            for p, at in t["restricted"].items()
            if t["succeeded"].get(p, -math.inf) <= at
        )

"""Adaptive pressure of the single-tab patrol: capped exponential backoff, strikes, recovery.

Accessed via: rotation.py (owns the persisted `Pressure` and the `BackoffConfig`) and patrol.py
(feeds it transport observations through the rotation). Pure: no clock, no generator, no file, no
network; every function takes the time or the random draw `u` in [0, 1) as an argument, so the
tests replay exact sequences. The formula and constants are documented in
docs/superpowers/plans/2026-10-28-guardian-adaptive-backoff.md.

Assumptions: pressure is per account (one rotation file per account); a 429 EPISODE costs one
level however many responses it holds, because one page load fires dozens of API calls at once;
only visit outcomes lower the level, never an unrelated 200 (an avatar or list success proves
nothing about the endpoint that was limited); a larger server delay (Retry-After) is never
shortened and never capped.

Past bugs it guards against: a flat 900 s default that never learned from repeated 429s, a
server delay clamped to 86400 s, a burst of 429s that would exponentiate once per subresource,
jitter that vanished at the cap (every long-running retry landed on the same second: the window
at the cap is [cap/(1+jitter), cap]), and DOM-only outcomes (complete, draft, unknown, failed:*)
counted as clean although they carry no transport evidence ("unproven" until a fresh 2xx of the
chat's own read endpoint is seen in the visit).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime

RECENT_LIMIT = 20
OUTCOMES = ("ok", "429", "error")
MAX_EXPONENT = 30
_CONFIG_KEYS = frozenset(
    {
        "base",
        "cap",
        "jitter",
        "clean_visits",
        "episode_gap",
        "transient_base",
        "transient_cap",
    }
)
_PRESSURE_KEYS = frozenset(
    {
        "level",
        "clean_streak",
        "strikes",
        "responses_429",
        "last_429_at",
        "strike_issue",
        "issued",
        "probes",
        "transient_streak",
        "transient_issue",
        "last_confirmed_at",
        "retry_turn",
        "recent",
    }
)
_NEUTRAL = frozenset({"challenge", "rate-limited", "redirected"})


class BackoffError(ValueError):
    """The backoff configuration or the stored pressure is unsafe."""


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _count(value: object, maximum: int | None = None) -> bool:
    return type(value) is int and value >= 0 and (maximum is None or value <= maximum)


def _optional_number(value: object) -> bool:
    return value is None or _number(value)


@dataclass(frozen=True)
class BackoffConfig:
    """Conservative defaults; every value is validated before it is used or stored."""

    base: float = 600.0  # the first 429 pause (strike 1)
    cap: float = 3600.0  # no adaptive pause or spacing ever exceeds this
    jitter: float = 0.25  # positive jitter fraction: delay * (1 + jitter * u)
    clean_visits: int = 3  # clean target visits that lower the level by one
    episode_gap: float = 120.0  # quiet seconds that separate two failure episodes
    transient_base: float = 300.0  # first retry after a timeout or a 5xx
    transient_cap: float = 1800.0  # bound of every transient retry

    def validated(self) -> BackoffConfig:
        numbers = (
            self.base,
            self.cap,
            self.jitter,
            self.episode_gap,
            self.transient_base,
            self.transient_cap,
        )
        if (
            not all(_number(value) for value in numbers)
            or type(self.clean_visits) is not int
        ):
            raise BackoffError("backoff settings must be finite numbers")
        ok = (
            300 <= self.base <= 3600
            and self.base <= self.cap <= 86400
            and 0 <= self.jitter <= 0.5
            and 1 <= self.clean_visits <= 10
            and 30 <= self.episode_gap <= 900
            and 60 <= self.transient_base <= self.transient_cap <= 3600
        )
        if not ok:
            raise BackoffError(
                "backoff needs 300<=base<=3600, base<=cap<=86400, 0<=jitter<=0.5, "
                "1<=clean_visits<=10, 30<=episode_gap<=900, "
                "60<=transient_base<=transient_cap<=3600"
            )
        return BackoffConfig(
            float(self.base),
            float(self.cap),
            float(self.jitter),
            self.clean_visits,
            float(self.episode_gap),
            float(self.transient_base),
            float(self.transient_cap),
        )

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object) -> BackoffConfig:
        if not isinstance(raw, dict) or set(raw) != _CONFIG_KEYS:
            raise BackoffError("backoff settings have unexpected fields")
        return cls(**raw).validated()


def ladder(level: int, cfg: BackoffConfig) -> float:
    """The pause of a pressure level before jitter: 0, base, 2*base, 4*base ... up to the cap."""
    if level <= 0:
        return 0.0
    return min(cfg.cap, cfg.base * 2.0 ** min(level - 1, MAX_EXPONENT))


def max_level(cfg: BackoffConfig) -> int:
    level = 1
    while ladder(level, cfg) < cfg.cap:
        level += 1
    return level


def delay_range(level: int, cfg: BackoffConfig) -> tuple[float, float]:
    """The exact [low, high] window of the pause at `level`, jitter kept at the cap.

    Below the cap it is [ladder, ladder*(1+jitter)]. A window whose top would pass the cap is
    shifted down instead of clamped to one point: [cap/(1+jitter), cap], so retries stay spread
    (2880..3600 s with the defaults) rather than synchronising at the cap. Level <= 0 is (0, 0).
    """
    if level <= 0:
        return 0.0, 0.0
    base = ladder(level, cfg)
    top = base * (1.0 + cfg.jitter)
    if top <= cfg.cap:
        return base, top
    return min(base, cfg.cap / (1.0 + cfg.jitter)), cfg.cap


def adaptive_delay(level: int, cfg: BackoffConfig, u: float) -> float:
    """The pause after a 429 episode at `level`: `low + (high-low)*u` of `delay_range`."""
    low, high = delay_range(level, cfg)
    return low + (high - low) * u


def transient_delay(streak: int, cfg: BackoffConfig, u: float) -> float:
    """The bounded retry delay after the `streak`-th consecutive transient failure."""
    if streak <= 0:
        return 0.0
    grown = cfg.transient_base * 2.0 ** min(streak - 1, MAX_EXPONENT)
    return min(cfg.transient_cap, grown * (1.0 + cfg.jitter * u))


def effective_interval(
    level: int, interval: tuple[float, float], cfg: BackoffConfig
) -> list[float]:
    """The range a visit draw falls in at this level: the base interval or the delay window."""
    low, high = interval
    if level <= 0:
        return [low, high]
    floor, ceiling = delay_range(level, cfg)
    return [max(low, floor), max(high, ceiling)]


def retry_after_seconds(value: object, now: float) -> float | None:
    """Seconds a Retry-After header asks for (delta-seconds or HTTP-date), else None.

    A past HTTP date comes back negative; the caller treats that as "no extra delay". The value
    is never clamped here: a larger server delay must not be shortened.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isascii() and text.isdigit() and len(text) <= 12:
        return float(int(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return when.timestamp() - now


def outcome_kind(outcome: str) -> str:
    """How a finished visit counts: 429, transient, neutral (no signal) or unproven.

    "unproven" is a clean CANDIDATE, never success: a DOM outcome (complete, draft, unknown,
    failed:*, working, approval:*) carries no transport evidence by itself. The caller upgrades it
    to "ok" only with a fresh 2xx of the chat's own read endpoint in the same visit.
    """
    if outcome == "cooldown":
        return "429"
    if outcome.startswith(("error:", "nav-error:")):
        return "transient"
    if outcome in _NEUTRAL or outcome.startswith("unreadable:"):
        return "neutral"
    return "unproven"


@dataclass
class Pressure:
    """Per-account pressure statistics; persisted in the rotation file."""

    level: int = 0
    clean_streak: int = 0
    strikes: int = 0  # 429 episodes counted
    responses_429: int = 0  # observed 429 responses (not requests)
    last_429_at: float | None = None
    strike_issue: int | None = None  # `issued` of the visit that last struck
    issued: int = 0  # visits/probes issued so far
    probes: int = 0  # controlled read-only probes among them
    transient_streak: int = 0
    transient_issue: int | None = None  # `issued` of the visit that saw a 5xx
    last_confirmed_at: float | None = (
        None  # last 2xx of a target chat's own read endpoint
    )
    retry_turn: bool = False  # the previous slot belonged to a failing/retried chat
    recent: list[str] = field(default_factory=list)  # bounded visit observations

    def begin(self, probe: bool) -> None:
        self.issued += 1
        if probe:
            self.probes += 1

    def strike_429(self, now: float, cooling: bool, cfg: BackoffConfig) -> bool:
        """Count one 429 response; True when it opens a new failure episode (a level up).

        New episode: no strike yet for this issued visit/probe, or a 429 outside our own cooldown
        after `episode_gap` quiet seconds. A burst inside one visit, or the page's own polling
        while the account is already cooling, is the same episode.
        """
        self.responses_429 += 1
        previous = self.last_429_at
        self.last_429_at = now
        self.clean_streak = 0
        fresh = (
            previous is None
            or self.strike_issue != self.issued
            or (not cooling and now - previous >= cfg.episode_gap)
        )
        if fresh:
            self.strikes += 1
            self.level = min(max_level(cfg), self.level + 1)
            self.strike_issue = self.issued
        return fresh

    def record(self, kind: str, cfg: BackoffConfig) -> None:
        """One finished visit: ok (clean), 429 or transient; neutral outcomes carry no signal."""
        if kind == "neutral":
            return
        shown = {"ok": "ok", "429": "429", "transient": "error"}[kind]
        self.recent = (self.recent + [shown])[-RECENT_LIMIT:]
        if kind == "ok":
            self.transient_streak = 0
            self.clean_streak = 0 if self.level == 0 else self.clean_streak + 1
            if self.level > 0 and self.clean_streak >= cfg.clean_visits:
                self.level -= 1
                self.clean_streak = 0
            return
        self.clean_streak = 0
        if kind == "transient":
            self.transient_streak += 1

    def success_rate(self) -> float | None:
        """Share of clean visits among the bounded actual observations; None without any."""
        return self.recent.count("ok") / len(self.recent) if self.recent else None

    def recent_counts(self) -> dict:
        return {
            "ok": self.recent.count("ok"),
            "429": self.recent.count("429"),
            "error": self.recent.count("error"),
            "window": len(self.recent),
        }

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object, cfg: BackoffConfig) -> Pressure:
        if not isinstance(raw, dict) or set(raw) != _PRESSURE_KEYS:
            raise BackoffError("rotation pressure record has unexpected fields")
        ok = _valid_counters(raw, cfg) and _valid_marks(raw)
        if not ok:
            raise BackoffError("rotation pressure record is invalid")
        return cls(**{**raw, "recent": list(raw["recent"])})


def _valid_counters(raw: dict, cfg: BackoffConfig) -> bool:
    return (
        _count(raw["level"], max_level(cfg))
        and _count(raw["clean_streak"])
        and _count(raw["strikes"])
        and _count(raw["responses_429"])
        and _count(raw["issued"])
        and _count(raw["probes"])
        and _count(raw["transient_streak"])
    )


def _valid_marks(raw: dict) -> bool:
    return (
        _optional_number(raw["last_429_at"])
        and (raw["strike_issue"] is None or _count(raw["strike_issue"]))
        and (raw["transient_issue"] is None or _count(raw["transient_issue"]))
        and _optional_number(raw["last_confirmed_at"])
        and type(raw["retry_turn"]) is bool
        and isinstance(raw["recent"], list)
        and len(raw["recent"]) <= RECENT_LIMIT
        and all(item in OUTCOMES for item in raw["recent"])
    )

"""Trend engine (doc s21). Direction over time, not a snapshot.

A single reading of "initiative 0.35" says almost nothing. The same number
arrived at from 0.10 means something entirely different from the same number
arrived at from 0.60, and only the second is worth telling the user about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum


class Direction(str, Enum):
    RISING = "rising"
    FLAT = "flat"
    FALLING = "falling"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Trend:
    axis: str
    direction: Direction
    delta: float
    first: float
    last: float
    n_points: int
    span_days: float

    @property
    def is_reportable(self) -> bool:
        """Enough points, enough time, enough movement to be worth saying."""
        return (
            self.n_points >= 3
            and self.span_days >= 5
            and abs(self.delta) >= 0.12
            and self.direction is not Direction.UNKNOWN
        )

    def describe(self) -> str:
        arrow = {"rising": "up", "falling": "down", "flat": "flat"}[self.direction.value]
        return (
            f"{self.axis}: {arrow} {self.first:.2f}->{self.last:.2f} "
            f"over {self.span_days:.0f}d ({self.n_points} readings)"
        )


def _axis_series(states: list, axis: str) -> list[tuple[datetime, float]]:
    group, name = axis.split(".", 1)
    out: list[tuple[datetime, float]] = []
    for st in states:
        payload = getattr(st, group, None)
        if payload is None:
            continue
        raw = payload.get(name) if isinstance(payload, dict) else None
        if not raw:
            continue
        # Only readings the agent was willing to act on belong in a trend;
        # otherwise noise from low-confidence periods creates phantom slopes.
        if raw.get("confidence", 0) < 0.6:
            continue
        out.append((st.as_of, float(raw["value"])))
    return sorted(out)


def compute_trend(states: list, axis: str, window_days: int = 45) -> Trend:
    """states: RelationshipStateRow list, oldest first."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    series = [(t, v) for t, v in _axis_series(states, axis) if t >= cutoff]
    if len(series) < 2:
        return Trend(axis, Direction.UNKNOWN, 0.0, 0.0, 0.0, len(series), 0.0)

    first, last = series[0][1], series[-1][1]
    delta = last - first
    span = (series[-1][0] - series[0][0]).total_seconds() / 86400
    if abs(delta) < 0.08:
        direction = Direction.FLAT
    else:
        direction = Direction.RISING if delta > 0 else Direction.FALLING
    return Trend(axis, direction, round(delta, 3), first, last, len(series), span)


TRACKED_AXES = (
    "attraction.romantic",
    "intimacy.emotional",
    "intimacy.conversational",
    "dynamics.initiative_balance",
    "dynamics.momentum",
    "bond.trust",
    "health.emotional_safety",
)


def summarise(states: list, window_days: int = 45) -> list[Trend]:
    trends = [compute_trend(states, a, window_days) for a in TRACKED_AXES]
    return [t for t in trends if t.is_reportable]

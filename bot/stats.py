"""Where a number sits inside its own recent history.

The digest already refuses to assert thresholds it cannot defend — there is no "PE above 30 is
expensive" rule anywhere in it, because that number belongs to a different decade and a different
index. What it can honestly say is where today's reading falls within the range the instrument
itself has traded in, computed from the data in hand.

This started as a private helper in gsr.py for the gold:silver ratio. It is a leaf module now
because the same question is worth asking of an index PE and of a price against its 52-week
range, and three copies of a percentile calculation would drift.

Deliberately a *percentile of observed closes*, not a z-score or a standard-deviation band: those
assume a distribution that index valuations do not have, and would put a confident-looking number
on an assumption nobody checked.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# Below this many observations a "percentile" is arithmetic theatre. A quarter of a trading year
# is enough to have seen more than one regime; less is not.
MIN_POINTS = 30

# A window is only called "1Y" if it holds something close to a year of sessions. A series that
# has just started, or one with a long hole in it, gets no label rather than a misleading one.
MIN_POINTS_FOR_YEAR = 180


@dataclass(frozen=True)
class RangePosition:
    """Today's value against the low, high and distribution of a trailing window."""

    current: float
    low: float
    high: float
    percentile: float          # share of observations at or below `current`, 0-100
    count: int
    first: Optional[dt.date] = None
    last: Optional[dt.date] = None

    @property
    def off_high_pct(self) -> float:
        """Distance below the window high, as a negative percentage (0.0 at the high)."""
        if self.high <= 0:
            return 0.0
        return (self.current - self.high) / self.high * 100.0

    @property
    def above_low_pct(self) -> float:
        if self.low <= 0:
            return 0.0
        return (self.current - self.low) / self.low * 100.0

    @property
    def spans_a_year(self) -> bool:
        return self.count >= MIN_POINTS_FOR_YEAR


def range_position(
    current: Optional[float],
    history: Sequence[float],
    *,
    min_points: int = MIN_POINTS,
    first: Optional[dt.date] = None,
    last: Optional[dt.date] = None,
) -> Optional[RangePosition]:
    """Locate `current` within `history`, or None if the question cannot be answered honestly.

    `history` must already be trimmed to the window being described, so that whatever label the
    caller prints and the arithmetic here agree. Returns None when there is too little data, or
    when the window is flat (every observation identical), because a percentile of a constant is
    not information.
    """
    if current is None:
        return None
    values = [v for v in history if v is not None]
    if len(values) < min_points:
        return None
    low, high = min(values), max(values)
    if high <= low:
        return None
    percentile = sum(1 for v in values if v <= current) / len(values) * 100.0
    return RangePosition(
        current=current, low=low, high=high, percentile=percentile,
        count=len(values), first=first, last=last,
    )


def trailing_window(
    points: Iterable[tuple[dt.date, float]],
    anchor: dt.date,
    *,
    days: int = 365,
) -> tuple[list[float], Optional[dt.date], Optional[dt.date]]:
    """The values from `points` falling in the `days` before `anchor`, plus the real date span.

    The span is returned rather than assumed: a series that only goes back four months still
    produces a valid range, and the caller needs to know not to call it a year.
    """
    cutoff = anchor - dt.timedelta(days=days)
    kept = sorted((when, value) for when, value in points if cutoff <= when <= anchor)
    if not kept:
        return [], None, None
    return [value for _when, value in kept], kept[0][0], kept[-1][0]

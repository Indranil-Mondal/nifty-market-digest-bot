"""The contract between fetchers and the message formatter.

Every source, however messy, is normalised into a `Snapshot`. The formatter only ever reads
Snapshots, so adding or replacing a data source cannot change how the digest looks.

Optionality is load-bearing here. `None` means "we do not know", and the formatter renders it
as an em dash. Nothing in this module ever substitutes a plausible-looking default, because a
fabricated PE in a financial digest is worse than a visible gap.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

# How a headline number relates to the moment the digest is sent. The digest prints this so a
# figure is never silently passed off as something it is not -- at 11:11 IST a TRI value is
# necessarily yesterday's close, while a Nifty 50 spot level is live.
FRESHNESS_LIVE = "live"            # intraday, captured during this run
FRESHNESS_PREV_CLOSE = "prev close"
FRESHNESS_T1 = "T-1"               # NAV published for the previous business day
FRESHNESS_STALE = "stale"          # older than expected; the digest flags it


@dataclass
class Reading:
    """One number plus provenance."""

    value: Optional[float] = None
    as_of: Optional[dt.date] = None
    freshness: Optional[str] = None
    source: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.value is not None


@dataclass
class Change:
    """Percentage move over one lookback window."""

    label: str
    pct: Optional[float] = None
    base_value: Optional[float] = None
    base_date: Optional[dt.date] = None
    target_date: Optional[dt.date] = None

    @property
    def known(self) -> bool:
        return self.pct is not None

    @property
    def drift_days(self) -> Optional[int]:
        """How far the date actually used sits from the calendar target.

        A couple of days is a weekend or holiday and is unremarkable. A large drift means the
        series is patchy and the digest marks the figure with a tilde.
        """
        if self.base_date is None or self.target_date is None:
            return None
        return (self.target_date - self.base_date).days


@dataclass
class Snapshot:
    """Everything the digest knows about one instrument this morning."""

    key: str
    display: str
    kind: str = "index"          # index | etf | fund
    # Headline numbers.
    level: Reading = field(default_factory=Reading)      # index level, or ETF last traded price
    tri: Reading = field(default_factory=Reading)        # total return index value, EOD only
    nav: Reading = field(default_factory=Reading)
    inav: Reading = field(default_factory=Reading)
    pe: Reading = field(default_factory=Reading)
    pb: Reading = field(default_factory=Reading)
    div_yield: Reading = field(default_factory=Reading)
    # Which series the percentage moves were computed on, so the digest can say so.
    change_basis: str = "level"
    changes: dict[str, Change] = field(default_factory=dict)
    pe_then: dict[str, Reading] = field(default_factory=dict)
    # Human-facing caveats ("TRI publishes after close") and machine failures, kept apart so
    # expected limitations are not presented to the user as errors.
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """Did we get anything worth printing?"""
        return self.level.known or self.tri.known or self.nav.known

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def fail(self, text: str) -> None:
        if text not in self.errors:
            self.errors.append(text)


@dataclass
class NewsItem:
    title: str
    url: Optional[str] = None
    source: Optional[str] = None
    published: Optional[dt.datetime] = None
    score: float = 0.0
    tags: list[str] = field(default_factory=list)

    def __hash__(self) -> int:  # de-duplication across overlapping feeds
        return hash(self.title.strip().lower())


@dataclass
class Digest:
    """The whole morning report, ready to render."""

    generated_at: dt.datetime
    snapshots: list[Snapshot] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return any(not s.healthy for s in self.snapshots) or bool(self.warnings)

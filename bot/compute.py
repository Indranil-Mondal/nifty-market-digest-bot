"""Turning a stored history plus today's fetch into a Snapshot.

This is the only place percentage changes are calculated, so every instrument's numbers are
derived the same way regardless of how messy its source was.

Two decisions worth stating explicitly, because they determine whether the digest is honest:

1. *The lookback table is anchored on the newest value of the basis series, not on "now".*
   TRI values publish only after the close, so at 11:11 IST the newest TRI is the previous
   session's. Measuring "1 week" from that anchor gives a true one-week move ending at a real
   close. Measuring it from today instead would silently compare a 6-day span and call it a
   week. The anchor date is printed in the digest.

2. *Price change and PE change resolve their base dates independently.* Exchanges publish
   closing levels more reliably than valuation ratios. Forcing both onto one base date would
   mean dropping a good percentage because PE was missing that day, or vice versa.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from .model import (
    FRESHNESS_LIVE,
    FRESHNESS_PREV_CLOSE,
    FRESHNESS_STALE,
    Change,
    Reading,
    Snapshot,
)
from .state import Series
from .util import LOOKBACKS, lookback_targets, pct_change

log = logging.getLogger(__name__)

# A basis value older than this many days means the source has stopped updating.
STALE_AFTER_DAYS = 6


@dataclass(frozen=True)
class InstrumentSpec:
    """Static description of one thing we track."""

    key: str                       # stable id; also the history filename
    display: str                   # exactly how it appears in the digest
    kind: str = "index"            # index | etf | fund
    basis: str = "level"           # which stored field the lookback table is computed on
    basis_label: str = ""          # shown when basis is not the plain level
    has_pe: bool = True            # False for gold, where PE is meaningless
    notes: tuple[str, ...] = ()    # permanent caveats, e.g. NAV lag for an overseas FoF


@dataclass
class FetchResult:
    """What a provider returns for one instrument today.

    `readings` are today's live/latest values keyed by field name. `history` lets a provider
    backfill several dates at once when its source happens to return a series.
    """

    readings: dict[str, Reading] = field(default_factory=dict)
    history: dict[dt.date, dict[str, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, name: str, value: Optional[float], *, as_of: Optional[dt.date] = None,
            freshness: Optional[str] = None, source: Optional[str] = None) -> None:
        if value is None:
            return
        self.readings[name] = Reading(value=value, as_of=as_of, freshness=freshness, source=source)


def persist(result: FetchResult, series: Series, today: dt.date) -> None:
    """Write everything a provider learned into the history store.

    Intraday values are deliberately NOT stored against today's date: an 11:11 snapshot is not
    a close, and storing it would corrupt tomorrow's "previous day" comparison. Only values
    that are already final (a published close, a published NAV) are persisted.
    """
    for when, fields in result.history.items():
        series.upsert(when, fields)

    final: dict[str, float] = {}
    for name, reading in result.readings.items():
        if reading.value is None or reading.as_of is None:
            continue
        if reading.freshness == FRESHNESS_LIVE:
            continue
        if reading.as_of >= today and reading.freshness == FRESHNESS_LIVE:
            continue
        series.upsert(reading.as_of, {name: reading.value})
        final[name] = reading.value


def _basis_field(spec: InstrumentSpec, series: Series, result: FetchResult) -> str:
    """Fall back through basis candidates until one has usable history.

    A TRI-based instrument whose TRI feed breaks should still report price moves rather than a
    column of dashes -- but the digest must then say the basis changed, which it does via
    Snapshot.change_basis.
    """
    candidates = [spec.basis]
    for alternative in ("tri", "level", "nav"):
        if alternative not in candidates:
            candidates.append(alternative)
    for name in candidates:
        if len(series.dates_with(name)) >= 2 or (name in result.readings and series.dates_with(name)):
            return name
    return spec.basis


def build_snapshot(
    spec: InstrumentSpec,
    series: Series,
    result: FetchResult,
    today: dt.date,
) -> Snapshot:
    snapshot = Snapshot(key=spec.key, display=spec.display, kind=spec.kind)

    for name in ("level", "tri", "nav", "inav", "pe", "pb", "div_yield"):
        reading = result.readings.get(name)
        if reading is not None:
            setattr(snapshot, name, reading)

    for note in spec.notes:
        snapshot.note(note)
    for note in result.notes:
        snapshot.note(note)
    for err in result.errors:
        snapshot.fail(err)

    basis = _basis_field(spec, series, result)

    # The anchor: the newest final value of the basis series, or today's live value if the
    # provider gave us one that is more recent than anything stored.
    stored_value, stored_date = series.latest(basis)
    live = result.readings.get(basis)
    if live is not None and live.freshness == FRESHNESS_LIVE and live.value is not None:
        anchor_value: Optional[float] = live.value
        anchor_date = live.as_of or today
        anchor_is_live = True
    else:
        anchor_value, anchor_date, anchor_is_live = stored_value, stored_date, False

    if anchor_value is None or anchor_date is None:
        snapshot.fail(f"no usable {basis} history to compare against")
        snapshot.changes = {label: Change(label=label) for label, _u, _n in LOOKBACKS}
        snapshot.change_basis = basis
        return snapshot

    if not anchor_is_live and (today - anchor_date).days > STALE_AFTER_DAYS:
        snapshot.note(f"{basis} last updated {anchor_date:%d %b} — source may be stale")
        for name in ("level", "tri", "nav"):
            reading = getattr(snapshot, name)
            if reading.known and reading.as_of == anchor_date:
                reading.freshness = FRESHNESS_STALE

    targets = lookback_targets(anchor_date)
    changes: dict[str, Change] = {}
    for label, _unit, _n in LOOKBACKS:
        target = targets[label]
        base_value, base_date = series.as_of(target, basis)
        # Guard against comparing the anchor with itself when history is one point deep.
        if base_date is not None and base_date == anchor_date and label != "1D":
            base_value, base_date = None, None
        changes[label] = Change(
            label=label,
            pct=pct_change(anchor_value, base_value),
            base_value=base_value,
            base_date=base_date,
            target_date=target,
        )
    snapshot.changes = changes

    # PE as it stood at each lookback date, resolved against PE's own available dates.
    if spec.has_pe:
        pe_then: dict[str, Reading] = {}
        for label, _unit, _n in LOOKBACKS:
            value, when = series.as_of(targets[label], "pe")
            pe_then[label] = Reading(value=value, as_of=when)
        # The 1D column should show the most recent published PE, which is what "PE now" is.
        if snapshot.pe.known:
            pe_then["1D"] = Reading(value=snapshot.pe.value, as_of=snapshot.pe.as_of)
        snapshot.pe_then = pe_then
    else:
        snapshot.pe_then = {}

    label = spec.basis_label or basis
    if basis != spec.basis:
        snapshot.change_basis = f"{basis} (fallback — {spec.basis} unavailable), to {anchor_date:%d %b}"
    elif basis != "level":
        snapshot.change_basis = f"{label}, to {anchor_date:%d %b} close"
    elif anchor_is_live:
        snapshot.change_basis = "level"
    else:
        snapshot.change_basis = f"level, to {anchor_date:%d %b} close"

    # A live headline needs its own same-day comparison, since the table may be anchored on a
    # previous close when the basis is an EOD series.
    if snapshot.level.known and snapshot.level.freshness == FRESHNESS_LIVE and basis != "level":
        prev, prev_date = series.as_of(today - dt.timedelta(days=1), "level")
        day_pct = pct_change(snapshot.level.value, prev)
        if day_pct is not None:
            snapshot.note(f"live level {day_pct:+.2f}% vs {prev_date:%d %b} close")

    if snapshot.level.known and snapshot.level.freshness is None:
        snapshot.level.freshness = FRESHNESS_PREV_CLOSE

    return snapshot

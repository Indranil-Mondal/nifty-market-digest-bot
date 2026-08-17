"""Incremental history cache.

The digest needs values as they stood up to 12 months ago, including PE on those dates. Re-
fetching a year of exchange archives every morning would be slow, fragile and rude to the
source. Instead each run appends today's numbers to a small JSON series per instrument, and
the file is committed back to the repository by the GitHub Actions job. Cold start is handled
by a one-off bootstrap that walks the archives once (see `scripts/bootstrap_history.py`).

The store is deliberately schema-free: a date maps to whatever fields that instrument's
source could supply. A field that is absent is absent -- it is never defaulted to zero, since
0.0 PE would render as a real number in the digest.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from .util import from_iso, iso, nearest_on_or_before

log = logging.getLogger(__name__)

# 12-month lookback needs ~370 days. Keep a wide margin so a long gap in the series (a source
# outage over a holiday week) cannot push the 1Y anchor off the end of the file.
RETENTION_DAYS = 800


class Series:
    """One instrument's date-indexed history."""

    def __init__(self, key: str, path: Path, series: Optional[dict[str, dict[str, Any]]] = None):
        self.key = key
        self.path = path
        self._series: dict[str, dict[str, Any]] = series or {}
        self._sorted_dates: Optional[list[dt.date]] = None

    # ---- persistence -------------------------------------------------------------

    @classmethod
    def load(cls, key: str, root: Path) -> "Series":
        path = root / f"{key}.json"
        if not path.exists():
            return cls(key, path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt cache must not kill the digest. Rename it aside and start fresh;
            # today's live numbers still work, only the lookbacks degrade to em dashes.
            log.error("history cache %s unreadable (%s); starting a new one", path, exc)
            try:
                path.replace(path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return cls(key, path)
        series = raw.get("series") or {}
        if not isinstance(series, dict):
            series = {}
        return cls(key, path, series)

    def save(self) -> None:
        """Atomic write: a killed run leaves the previous file intact, never a half file."""
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": self.key,
            "points": len(self._series),
            "first": min(self._series, default=None),
            "last": max(self._series, default=None),
            "series": {d: self._series[d] for d in sorted(self._series)},
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, sort_keys=False, default=str)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- writing -----------------------------------------------------------------

    def upsert(self, when: dt.date, fields: dict[str, Any], *, overwrite: bool = False) -> None:
        """Merge `fields` into the record for `when`, dropping None values.

        By default existing non-null values win, so a later partial fetch cannot blank out a
        good number. Pass overwrite=True when the new data is authoritative (e.g. a final
        settlement value replacing an intraday one).
        """
        key = iso(when)
        if key is None:
            return
        record = self._series.setdefault(key, {})
        for name, value in fields.items():
            if value is None:
                continue
            if overwrite or record.get(name) is None:
                record[name] = value
        if not record:
            self._series.pop(key, None)
        self._sorted_dates = None

    def prune(self, retention_days: int = RETENTION_DAYS) -> None:
        if not self._series:
            return
        newest = max(from_iso(d) or dt.date.min for d in self._series)
        cutoff = newest - dt.timedelta(days=retention_days)
        stale = [d for d in self._series if (from_iso(d) or dt.date.min) < cutoff]
        for d in stale:
            self._series.pop(d, None)
        if stale:
            self._sorted_dates = None

    # ---- reading -----------------------------------------------------------------

    @property
    def dates(self) -> list[dt.date]:
        if self._sorted_dates is None:
            parsed = [from_iso(d) for d in self._series]
            self._sorted_dates = sorted(d for d in parsed if d is not None)
        return self._sorted_dates

    def __len__(self) -> int:
        return len(self._series)

    def record(self, when: dt.date) -> dict[str, Any]:
        return dict(self._series.get(iso(when) or "", {}))

    def dates_with(self, field: str) -> list[dt.date]:
        """Dates whose record actually carries a non-null `field`.

        PE is published less consistently than closing levels, so the anchor date for a PE
        comparison is often not the same as for a price comparison. Resolving each field
        against its own available dates is what keeps the two columns honest.
        """
        out = []
        for text, record in self._series.items():
            if record.get(field) is None:
                continue
            parsed = from_iso(text)
            if parsed is not None:
                out.append(parsed)
        return sorted(out)

    def as_of(self, target: dt.date, field: str, *, max_slack_days: int = 12) -> tuple[Optional[float], Optional[dt.date]]:
        """Value of `field` on the latest available date <= `target`.

        Returns (value, actual_date) so the digest can disclose which date it really used.
        """
        candidates = self.dates_with(field)
        found = nearest_on_or_before(target, candidates, max_slack_days=max_slack_days)
        if found is None:
            return None, None
        value = self._series.get(iso(found) or "", {}).get(field)
        return (float(value) if isinstance(value, (int, float)) else None), found

    def latest(self, field: str) -> tuple[Optional[float], Optional[dt.date]]:
        candidates = self.dates_with(field)
        if not candidates:
            return None, None
        found = candidates[-1]
        value = self._series[iso(found)].get(field)
        return (float(value) if isinstance(value, (int, float)) else None), found


class Store:
    """All instruments' series, rooted at a directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache: dict[str, Series] = {}

    def series(self, key: str) -> Series:
        if key not in self._cache:
            self._cache[key] = Series.load(key, self.root)
        return self._cache[key]

    def save_all(self) -> None:
        for series in self._cache.values():
            try:
                series.save()
            except OSError as exc:
                log.error("could not persist history for %s: %s", series.key, exc)

    def summary(self) -> Iterable[str]:
        for key, series in sorted(self._cache.items()):
            span = f"{series.dates[0]}..{series.dates[-1]}" if series.dates else "empty"
            yield f"{key}: {len(series)} points, {span}"

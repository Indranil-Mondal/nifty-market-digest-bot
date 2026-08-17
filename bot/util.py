"""Date, timezone and arithmetic helpers.

Everything in this bot works in Asia/Kolkata.

We prefer a real IANA tzinfo, but fall back to a fixed +05:30 offset when no tz database is
present. That fallback is not a compromise in accuracy: India has observed a single, unchanged
UTC+05:30 offset with no daylight saving since 1945, so the two are equivalent in practice.
It matters because `zoneinfo` reads the *system* tz database, which Windows does not ship and
slim Linux containers often omit -- without this, importing the bot raises
ZoneInfoNotFoundError and nothing is delivered.
"""

from __future__ import annotations

import bisect
import datetime as dt
import logging
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)


def _india_tz() -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Kolkata")
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ImportError, or a broken tzdb
        log.warning("no IANA tz database found; using a fixed UTC+05:30 offset for IST")
        return dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


IST = _india_tz()

# Lookback windows the digest reports, in the order they should appear in the message.
# Weeks are exact calendar weeks; months are calendar months (same day-of-month, clamped).
LOOKBACKS: tuple[tuple[str, str, int], ...] = (
    ("1D", "days", 1),
    ("1W", "days", 7),
    ("2W", "days", 14),
    ("3W", "days", 21),
    ("4W", "days", 28),
    ("3M", "months", 3),
    ("6M", "months", 6),
    ("1Y", "months", 12),
)


def ist_now() -> dt.datetime:
    """Current wall-clock time in India."""
    return dt.datetime.now(IST)


def ist_today() -> dt.date:
    return ist_now().date()


def shift_months(anchor: dt.date, months: int) -> dt.date:
    """Move `months` back from `anchor`, clamping the day to the target month's length.

    31 May minus 3 months is 28/29 Feb, not an exception.
    """
    total = (anchor.year * 12 + (anchor.month - 1)) - months
    year, month = divmod(total, 12)
    month += 1
    # Day 1 of the following month, minus one day, is the last day of `month`.
    if month == 12:
        last = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return dt.date(year, month, min(anchor.day, last.day))


def lookback_targets(anchor: dt.date) -> dict[str, dt.date]:
    """Calendar target date for each lookback label.

    These are *calendar* targets. They are deliberately not snapped to trading days here --
    snapping happens against the actual data we hold, via `nearest_on_or_before`, so that
    market holidays and exchange-specific closures are handled by evidence rather than by a
    hardcoded holiday list that would rot.
    """
    out: dict[str, dt.date] = {}
    for label, unit, n in LOOKBACKS:
        out[label] = anchor - dt.timedelta(days=n) if unit == "days" else shift_months(anchor, n)
    return out


def nearest_on_or_before(
    target: dt.date,
    available: Sequence[dt.date],
    *,
    max_slack_days: int = 12,
) -> Optional[dt.date]:
    """Latest date in `available` that is <= `target`, or None.

    `available` must be sorted ascending. `max_slack_days` guards against silently comparing
    against a date weeks off target when history is patchy -- a long Diwali/weekend cluster is
    at most ~5 days, so 12 is generous while still catching a genuinely broken series. Better
    to print an em dash than a wrong percentage.
    """
    if not available:
        return None
    idx = bisect.bisect_right(available, target) - 1
    if idx < 0:
        return None
    found = available[idx]
    if (target - found).days > max_slack_days:
        return None
    return found


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    """Percentage change, or None when either side is missing or the base is unusable."""
    if new is None or old is None:
        return None
    try:
        new_f, old_f = float(new), float(old)
    except (TypeError, ValueError):
        return None
    if old_f == 0:
        return None
    return (new_f - old_f) / old_f * 100.0


def parse_float(raw: object) -> Optional[float]:
    """Best-effort float from exchange CSV/JSON cells.

    Handles the shapes these feeds actually emit: '1,234.56', ' 12.3 ', '-', '', 'NA',
    '2,345.67 ' with a stray currency symbol, and already-numeric values.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw).strip().replace(",", "").replace(" ", "")
    for junk in ("₹", "Rs.", "Rs", "$", "%"):
        text = text.replace(junk, "")
    text = text.strip()
    if text in ("", "-", "--", "NA", "N.A.", "N/A", "nil", "Nil", "NIL", "null", "None"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    # Exchange files use 0 as a filler for "not published" in PE/PB columns.
    return value


def parse_date(raw: object, formats: Iterable[str]) -> Optional[dt.date]:
    """Try each format in turn; return the first that parses."""
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    for fmt in formats:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# Month abbreviations spelled out rather than relying on strftime/strptime's %b, because those
# follow the process locale and the runner's locale is not ours to assume. Several sources
# (niftyindices POST bodies, AMFI report ranges, AMFI NAV rows) use this exact form.
MONTHS_ABBR: tuple[str, ...] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_MONTH_INDEX = {name.lower(): i + 1 for i, name in enumerate(MONTHS_ABBR)}


def fmt_ddmmmyyyy(when: dt.date) -> str:
    """17 Aug 2026 -> '17-Aug-2026'."""
    return f"{when.day:02d}-{MONTHS_ABBR[when.month - 1]}-{when.year}"


def parse_ddmmmyyyy(raw: object) -> Optional[dt.date]:
    """Accept '17-Aug-2026' or '17 Aug 2026', case-insensitively."""
    text = str(raw or "").strip()
    if not text:
        return None
    parts = text.replace("-", " ").split()
    if len(parts) < 3:
        return None
    month = _MONTH_INDEX.get(parts[1].lower()[:3])
    if month is None:
        return None
    try:
        return dt.date(int(parts[2]), month, int(parts[0]))
    except ValueError:
        return None


def iso(d: Optional[dt.date]) -> Optional[str]:
    return d.isoformat() if d is not None else None


def from_iso(text: Optional[str]) -> Optional[dt.date]:
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None

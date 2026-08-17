"""AMFI NAV history — the shared NAV source for every mutual-fund instrument.

AMFI publishes a per-AMC NAV report that accepts a date range and returns one semicolon-
delimited row per scheme per day. A single request covers a full year, which is what makes the
"NAV as it stood 12 months ago" requirement cheap.

Two hazards, both measured rather than assumed:

  * An invalid `mf` code answers HTTP 200 with an HTML frameset, and a *valid but wrong* `mf`
    answers 200 with a real report for the wrong AMC. So a 200 proves nothing on its own.
  * Scheme codes are dense: 140088 is Gold BeES while 140089 is Nifty PSU Bank BeES, and
    148063 vs 148064 are the direct and regular plans of the same fund. An off-by-one guess
    returns a different fund's NAV at HTTP 200, which would render as perfectly plausible data.

Every row is therefore validated against the expected ISIN before it is believed.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from ..http import Http
from ..util import fmt_ddmmmyyyy, parse_ddmmmyyyy, parse_float

log = logging.getLogger(__name__)

HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"

# The report is ~15MB for a twelve-month window across a large AMC. Request it in slices so no
# single response is enormous, and stream each one with a prefix filter.
CHUNK_DAYS = 100
STREAM_TIMEOUT = 120

# Column positions in the report. Header row:
#   Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;
#   Net Asset Value;Repurchase Price;Sale Price;Date
_COL_ISIN = 2
_COL_NAV = 4
_COL_DATE = 7
_MIN_COLS = 8


@dataclass(frozen=True)
class AmfiScheme:
    """One scheme, identified redundantly so a wrong row can be detected."""

    scheme_code: str
    isin: str
    mf_code: str          # the AMC's id in AMFI's report, established empirically per AMC
    label: str

    @property
    def line_prefix(self) -> str:
        return f"{self.scheme_code};"


def nav_history(
    http: Http,
    scheme: AmfiScheme,
    start: dt.date,
    end: dt.date,
    *,
    field: str = "nav",
) -> tuple[dict[dt.date, dict[str, float]], list[str]]:
    """NAV by date for one scheme. Returns (history, errors); never raises."""
    out: dict[dt.date, dict[str, float]] = {}
    errors: list[str] = []

    window_start = start
    while window_start <= end:
        window_end = min(window_start + dt.timedelta(days=CHUNK_DAYS), end)
        lines = http.stream_lines(
            HISTORY_URL,
            params={
                "mf": scheme.mf_code,
                "tp": "1",
                "frmdt": fmt_ddmmmyyyy(window_start),
                "todt": fmt_ddmmmyyyy(window_end),
            },
            keep=scheme.line_prefix,
            timeout=STREAM_TIMEOUT,
        )
        window_label = f"{window_start:%d %b %Y}..{window_end:%d %b %Y}"

        if lines is None:
            errors.append(f"AMFI unreachable ({window_label})")
            window_start = window_end + dt.timedelta(days=1)
            continue

        head = "\n".join(lines[:3]).lower()
        if "<html" in head or "scheme code" not in head:
            errors.append(f"AMFI returned a page rather than a report ({window_label})")
            window_start = window_end + dt.timedelta(days=1)
            continue

        found = 0
        mismatched = 0
        for line in lines:
            if not line.startswith(scheme.line_prefix):
                continue
            parts = line.split(";")
            if len(parts) < _MIN_COLS:
                continue
            if parts[_COL_ISIN].strip().upper() != scheme.isin.upper():
                mismatched += 1
                continue
            nav = parse_float(parts[_COL_NAV])
            when = parse_ddmmmyyyy(parts[_COL_DATE])
            if nav is None or when is None:
                continue
            out[when] = {field: nav}
            found += 1

        if mismatched:
            # Right scheme code, wrong ISIN: either the report changed shape or the code has been
            # reassigned. Either way the data is not ours.
            errors.append(f"AMFI ISIN mismatch on {mismatched} row(s) for {scheme.scheme_code}")
        if found == 0 and not mismatched:
            # A window with genuinely no NAV rows is normal for a short holiday span, so only
            # complain when the whole requested range came back empty.
            log.debug("AMFI: no rows for %s in %s", scheme.scheme_code, window_label)

        window_start = window_end + dt.timedelta(days=1)

    if not out and not errors:
        errors.append(f"AMFI returned no NAV rows for scheme {scheme.scheme_code}")
    return out, errors


def newest_nav(
    http: Http,
    scheme: AmfiScheme,
    today: dt.date,
    *,
    lookback_days: int = 20,
) -> tuple[Optional[float], Optional[dt.date]]:
    """Most recent published NAV, searched over a short window.

    Overseas fund-of-funds are the slow publishers: they need both an Indian business day and a
    fresh underlying NAV, so their no-NAV dates are the union of Indian and US market holidays.
    A fixed T-1 assumption is wrong often enough that we always look back over a window and
    report whichever date we actually got.
    """
    history, _errors = nav_history(http, scheme, today - dt.timedelta(days=lookback_days), today)
    if not history:
        return None, None
    when = max(history)
    return history[when].get("nav"), when

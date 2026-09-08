"""AMFI NAV history — the shared NAV source for every mutual-fund instrument.

AMFI publishes a per-AMC NAV report that accepts a date range and returns one semicolon-
delimited row per scheme per day. A single request covers a full year, which is what makes the
"NAV as it stood 12 months ago" requirement cheap.

Three hazards, all measured rather than assumed:

  * An invalid `mf` code answers HTTP 200 with an HTML frameset, and a *valid but wrong* `mf`
    answers 200 with a real report for the wrong AMC. So a 200 proves nothing on its own.
  * Scheme codes are dense: 140088 is Gold BeES while 140089 is Nifty PSU Bank BeES, and
    148063 vs 148064 are the direct and regular plans of the same fund. An off-by-one guess
    returns a different fund's NAV at HTTP 200, which would render as perfectly plausible data.
  * The column ORDER is not stable. On 19 Aug 2026 AMFI silently changed the report from

        Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;
        Net Asset Value;Repurchase Price;Sale Price;Date

    to

        Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;
        ISIN Div Reinvestment;Net Asset Value;Date

    Both are eight fields wide, so a field-count check does not notice; but ISIN moved from
    index 2 to index 4 and NAV from 4 to 6. This module used to hard-code those positions, and
    the change froze every fund NAV in the digest for three weeks. Columns are therefore now
    located by HEADER NAME, and a report whose header cannot be understood is refused outright
    rather than parsed on a guess.

Every row is validated against the expected ISIN before it is believed.
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

# Header labels, lowercased and stripped, that identify each column we need. Several spellings
# are listed per column because AMFI has used more than one and may again; the point of this
# table is that a NEW spelling produces a loud error instead of silently wrong numbers.
_ISIN_GROWTH_LABELS = ("isin div payout/isin growth", "isin div payout / isin growth", "isin growth")
_ISIN_REINVEST_LABELS = ("isin div reinvestment", "isin div reinvest")
_NAV_LABELS = ("net asset value", "nav")
_DATE_LABELS = ("date",)
_CODE_LABELS = ("scheme code",)


@dataclass(frozen=True)
class Columns:
    """Where each field lives in this particular response."""

    code: int
    nav: int
    date: int
    isin_growth: Optional[int]
    isin_reinvest: Optional[int]

    @property
    def width(self) -> int:
        present = [self.code, self.nav, self.date, self.isin_growth, self.isin_reinvest]
        return max(i for i in present if i is not None) + 1

    @property
    def isin_at(self) -> tuple[int, ...]:
        """Every column that may carry the scheme's ISIN.

        A growth plan puts it in the payout/growth column and leaves reinvestment blank; a
        reinvestment plan does the reverse. Accepting either means one fewer thing to get wrong
        per scheme, and the ISIN is still checked -- just not against a fixed column.
        """
        return tuple(i for i in (self.isin_growth, self.isin_reinvest) if i is not None)


def resolve_columns(header: str) -> Optional[Columns]:
    """Locate the columns we need from the report's own header row.

    Returns None if the line is not a recognisable header, which the caller must treat as a
    failed fetch. Guessing is what broke this module once already.
    """
    fields = [f.strip().lower() for f in header.split(";")]

    def find(labels: tuple[str, ...]) -> Optional[int]:
        # Label-major, not field-major. The label tuples are in priority order, so the precise
        # "net asset value" must win over the looser "nav" wherever each happens to sit. Walking
        # the fields in the outer loop would instead return whichever matched earliest -- and
        # column 1 of the live header is "NAV Name", which is a scheme name, not a NAV.
        for label in labels:
            for index, name in enumerate(fields):
                if name == label:
                    return index
        return None

    code = find(_CODE_LABELS)
    nav = find(_NAV_LABELS)
    when = find(_DATE_LABELS)
    if code is None or nav is None or when is None:
        return None

    growth = find(_ISIN_GROWTH_LABELS)
    reinvest = find(_ISIN_REINVEST_LABELS)
    if growth is None and reinvest is None:
        # Without an ISIN anywhere we cannot prove a row belongs to the scheme we asked for,
        # and scheme codes are too dense to trust on their own.
        return None

    return Columns(code=code, nav=nav, date=when, isin_growth=growth, isin_reinvest=reinvest)


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

        # stream_lines always keeps the first three lines regardless of the prefix filter, so
        # the header survives even though we asked for one scheme's rows only.
        columns = None
        for candidate in lines[:3]:
            columns = resolve_columns(candidate)
            if columns is not None:
                break

        if columns is None:
            head = " | ".join(line[:90] for line in lines[:2]) or "(empty response)"
            errors.append(f"AMFI report layout not recognised ({window_label}); header was: {head}")
            window_start = window_end + dt.timedelta(days=1)
            continue

        found = 0
        mismatched = 0
        for line in lines:
            if not line.startswith(scheme.line_prefix):
                continue
            parts = line.split(";")
            if len(parts) < columns.width:
                continue
            if not any(parts[i].strip().upper() == scheme.isin.upper() for i in columns.isin_at):
                mismatched += 1
                continue
            nav = parse_float(parts[columns.nav])
            when = parse_ddmmmyyyy(parts[columns.date])
            if nav is None or when is None:
                continue
            out[when] = {field: nav}
            found += 1

        if mismatched and not found:
            # Right scheme code, wrong ISIN on every row: either the code has been reassigned to
            # another fund or the report changed shape in a way resolve_columns did not catch.
            # Either way the data is not ours and must not be believed.
            errors.append(
                f"AMFI: scheme {scheme.scheme_code} returned {mismatched} row(s), none carrying "
                f"ISIN {scheme.isin} ({window_label})"
            )
        elif mismatched:
            log.warning(
                "AMFI: %s row(s) for %s did not carry ISIN %s in %s",
                mismatched, scheme.scheme_code, scheme.isin, window_label,
            )
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

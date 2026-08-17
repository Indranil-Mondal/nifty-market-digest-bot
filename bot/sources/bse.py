"""BSE 250 SmallCap — price, total return and valuation.

Every endpoint and quirk here was confirmed by live request before being written down.

Two sources, both on plain web servers with no cookie, key or header requirement, and both
confirmed reachable from a US datacenter IP (which matters because the bot runs on a GitHub
Actions runner, not from India):

  1. bseindices.com GetHistoricalPRTRData_Asia -- price AND total return in one call. Fifteen
     months of daily rows arrive in a single request, so the TRI series needs no incremental
     caching to be cheap.
  2. bseindia.com/Downloads/AllIndices/AllIndices_DDMMYYYY.csv -- one static file per trading
     date carrying P/E, P/B and dividend yield. This is what makes "PE as it stood 12 months
     ago" possible at all. We fetch only the dates we do not already have cached.

Naming, since three different codes refer to the same index depending on the host:
    microsite (bseindices.com)   code=103
    AllIndices CSV "Index Code"  SML250   (Index ID 56)
    legacy api.bseindia.com      SPB25SIP
Do NOT resolve this index through the site's FillddlIndex dropdown map, where the string
"SML250" points at a different index ("BSE Smallcap 500") -- match on Index Code in the CSV and
use code=103 on the microsite.

The index is now officially "BSE 250 SmallCap Index"; the "S&P BSE" co-brand was retired after
S&P Dow Jones sold its stake in Asia Index Pvt Ltd to BSE. Yahoo still shows the old name.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from dataclasses import dataclass
from typing import Optional

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..model import FRESHNESS_LIVE, FRESHNESS_PREV_CLOSE
from ..state import Series
from ..util import lookback_targets, parse_date, parse_float
from . import yahoo

log = logging.getLogger(__name__)

PRTR_URL = "https://www.bseindices.com/AsiaIndexAPI/api/GetHistoricalPRTRData_Asia/w"
ALLINDICES_URL = "https://www.bseindia.com/Downloads/AllIndices/AllIndices_{stamp}.csv"

# The per-date CSV archive begins on the first trading day of 2025; every 2024 date 404s.
ALLINDICES_FIRST_DATE = dt.date(2025, 1, 2)

# Cold start: pull enough history that the 12-month lookback has room to walk back over a
# holiday cluster. 460 days was verified to return the full span in one request.
COLD_START_DAYS = 460
# Warm run: re-fetch a short overlap so a revised close is picked up.
WARM_OVERLAP_DAYS = 12
# How far back to walk when a date lands on a weekend or exchange holiday.
TRADING_DAY_WALKBACK = 8


@dataclass(frozen=True)
class BseIndex:
    """Per-index identifiers across the three BSE hosts."""

    microsite_code: int
    csv_index_code: str
    display: str
    yahoo_symbol: Optional[str] = None


BSE_250_SMALLCAP = BseIndex(
    microsite_code=103,
    csv_index_code="SML250",
    display="BSE 250 SmallCap",
    yahoo_symbol="SML250.BO",
)


def _prtr_history(
    http: Http,
    index: BseIndex,
    start: dt.date,
    end: dt.date,
) -> tuple[dict[dt.date, dict[str, float]], Optional[str]]:
    """Daily price-return and total-return closes.

    Dates must be YYYYMMDD. Any other format returns HTTP 200 with an empty array -- a silent
    failure -- so the row count is asserted rather than trusted.
    """
    params = {
        "code": index.microsite_code,
        "Fromdate": start.strftime("%Y%m%d"),
        "Todate": end.strftime("%Y%m%d"),
        "flag": 1,
        "type": 1,
    }
    payload = http.get(PRTR_URL, params=params, expect="json")
    if payload is None:
        return {}, "BSE total-return endpoint unreachable"
    if not isinstance(payload, list):
        return {}, "BSE total-return endpoint returned an unexpected shape"
    if not payload:
        # Empty array with a 200 means the request was malformed, not that no data exists.
        return {}, "BSE total-return endpoint returned no rows (check date format)"

    out: dict[dt.date, dict[str, float]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        when = parse_date(
            str(row.get("TransDate", "")).split(" ")[0],
            ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"),
        )
        if when is None:
            continue
        fields: dict[str, float] = {}
        price = parse_float(row.get("PRValue"))
        total = parse_float(row.get("TRValue"))   # "" for inverse/volatility indices
        if price is not None:
            fields["level"] = price
        if total is not None:
            fields["tri"] = total
        if fields:
            out[when] = fields
    if not out:
        return {}, "BSE total-return rows could not be parsed"
    return out, None


def _allindices_for_date(
    http: Http,
    index: BseIndex,
    when: dt.date,
) -> tuple[Optional[dt.date], dict[str, float]]:
    """Valuation row for `when`, walking back to the previous trading day if needed.

    Non-trading dates return 404 with an HTML body, so we check that the payload really starts
    with the CSV header before parsing. Without that check an error page would parse to zero
    rows and look like "this index has no PE today".
    """
    for offset in range(TRADING_DAY_WALKBACK + 1):
        probe = when - dt.timedelta(days=offset)
        if probe < ALLINDICES_FIRST_DATE:
            return None, {}
        text = http.get(ALLINDICES_URL.format(stamp=probe.strftime("%d%m%Y")), allow_status=(200,))
        if not text or not text.lstrip().lower().startswith("date"):
            continue
        row = _find_index_row(text, index.csv_index_code)
        if row is None:
            continue
        fields: dict[str, float] = {}
        for column, name in (("P/E", "pe"), ("P/B", "pb"), ("Div Yield", "div_yield"), ("Close", "level")):
            value = parse_float(row.get(column))
            # BSE writes 0 into P/E and P/B when a ratio is not published. Zero is not a
            # valuation, so it must not be stored as one.
            if value is not None and not (name in ("pe", "pb") and value == 0):
                fields[name] = value
        if fields:
            return probe, fields
    return None, {}


def _find_index_row(csv_text: str, index_code: str) -> Optional[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("Index Code", "").upper() == index_code.upper():
            return row
    return None


def fetch(
    http: Http,
    series: Series,
    spec: InstrumentSpec,
    today: dt.date,
    *,
    index: BseIndex = BSE_250_SMALLCAP,
) -> FetchResult:
    result = FetchResult()

    # --- price and total return series -------------------------------------------------
    _, newest_cached = series.latest("tri")
    if newest_cached is None:
        start = today - dt.timedelta(days=COLD_START_DAYS)
    else:
        start = min(newest_cached - dt.timedelta(days=WARM_OVERLAP_DAYS), today)
        # Still make sure we hold a full year even if the cache was truncated.
        if len(series.dates_with("tri")) < 200:
            start = today - dt.timedelta(days=COLD_START_DAYS)

    history, error = _prtr_history(http, index, start, today)
    if error:
        result.errors.append(error)
    result.history.update(history)

    # Fold in what we just fetched so the anchor below reflects it.
    for when, fields in history.items():
        series.upsert(when, fields)

    tri_value, tri_date = series.latest("tri")
    if tri_value is not None and tri_date is not None:
        result.add("tri", tri_value, as_of=tri_date, freshness=FRESHNESS_PREV_CLOSE, source="bseindices PRTR")

    close_value, close_date = series.latest("level")

    # --- valuation on the dates we actually need ---------------------------------------
    anchor = tri_date or close_date or today
    needed = [anchor, *lookback_targets(anchor).values()]
    fetched_pe = 0
    for target in needed:
        existing, _ = series.as_of(target, "pe")
        if existing is not None:
            continue
        found, fields = _allindices_for_date(http, index, target)
        if found is not None and fields:
            result.history.setdefault(found, {}).update(fields)
            series.upsert(found, fields)
            fetched_pe += 1
    if fetched_pe:
        log.info("%s: fetched valuation for %s new date(s)", spec.key, fetched_pe)

    pe_value, pe_date = series.latest("pe")
    if pe_value is not None:
        result.add("pe", pe_value, as_of=pe_date, source="BSE AllIndices CSV")
    pb_value, pb_date = series.latest("pb")
    if pb_value is not None:
        result.add("pb", pb_value, as_of=pb_date, source="BSE AllIndices CSV")
    dy_value, dy_date = series.latest("div_yield")
    if dy_value is not None:
        result.add("div_yield", dy_value, as_of=dy_date, source="BSE AllIndices CSV")

    # --- live level -------------------------------------------------------------------
    # BSE's own files for the day are not written until ~19:30 IST, so at 11:11 Yahoo is the only
    # free route to an intraday level. It is the PRICE index, never total return, and carries no
    # PE -- labelled accordingly below.
    live, tick = (None, None)
    if index.yahoo_symbol:
        live, tick = yahoo.last(http, index.yahoo_symbol)
    if live is not None and tick == today:
        result.add("level", live, as_of=today, freshness=FRESHNESS_LIVE, source="Yahoo (price index)")
        result.notes.append("live level is the price index; TRI and PE are previous close")
    elif close_value is not None:
        # Either Yahoo was unreachable or it is serving a stale close; the exchange's own
        # published close is the better number and comes with an honest date.
        result.add("level", close_value, as_of=close_date, freshness=FRESHNESS_PREV_CLOSE, source="bseindices PRTR")

    if not result.readings:
        result.errors.append("no BSE data could be retrieved")
    return result

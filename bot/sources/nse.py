"""NSE / NIFTY indices — Smallcap 250, Midcap 150, Nifty 50, Nifty Next 50.

Verified by live request before being written down. Two decisions here run against the common
instinct, and both matter for a bot that must survive years of unattended runs:

*We do not use nseindia.com.* That host sits behind Akamai Bot Manager, which is
IP-reputation-driven and is the documented reason NSE blocks cloud IPs — exactly where GitHub
Actions runs. We use instead:
  * www.niftyindices.com          plain ASP.NET, no bot manager
  * liveindexsa.niftyindices.com  Azure Blob Storage, no WAF, no User-Agent required — the
                                  same cloud the runner itself lives in

*No cookie bootstrap is needed anywhere.* The "GET the homepage first for cookies" recipe found
in every tutorial does not apply: the /BackPage/ POST APIs answer a stone-cold request with no
cookie jar and no Referer. What actually gates access is the User-Agent.

And the failure mode to remember: a disallowed User-Agent produces a HANG, not a 403. Requests
with a library UA, or no UA, time out. So every call here sends a browser UA and allows a
generous timeout.

Three endpoints, all POST, all on /BackPage/ WITHOUT the legacy `.aspx` (the `Backpage.aspx/`
path is dead and returns a 302 error envelope):
    getTotalReturnIndexString          TRI history
    getpepbHistoricaldataDBtoString    PE / PB / dividend yield history
    getHistoricaldatatabletoString     price OHLC history
One range request returns the whole series, so holidays need no calendar — we just snap each
lookback to the nearest prior row present in the response.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..model import FRESHNESS_LIVE, FRESHNESS_PREV_CLOSE
from ..state import Series
from ..util import parse_float

log = logging.getLogger(__name__)

BACKPAGE = "https://www.niftyindices.com/BackPage/{method}"
LIVE_WATCH_URL = "https://liveindexsa.niftyindices.com/jsonfiles/LiveIndicesWatch.json"

# The UA filter hangs rather than rejecting, and a 13-month range is a real query, so allow
# plenty of time. Short timeouts here look like an outage when they are actually impatience.
POST_TIMEOUT = 90

COLD_START_DAYS = 460
WARM_OVERLAP_DAYS = 12
MIN_HEALTHY_POINTS = 200

# Month names spelled out rather than relying on %b, because strptime/strftime month
# abbreviations follow the process locale and the runner's locale is not ours to assume.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_INDEX = {name.lower(): i + 1 for i, name in enumerate(_MONTHS)}


@dataclass(frozen=True)
class NiftyIndex:
    """The same index is spelled differently on every NSE surface.

    `post_name` is IndexMapping.json's Trading_Index_Name and `post_index_name` is its
    Index_long_name. The PE endpoint is strict about post_name: pass the spelled-out
    'NIFTY SMALLCAP 250' instead of 'NIFTY SMLCAP 250' and it returns an empty array with
    HTTP 200 — a silent failure, which is why row counts are asserted below.
    """

    post_name: str
    post_index_name: str
    live_name: str


NIFTY_50 = NiftyIndex("Nifty 50", "Nifty 50", "NIFTY 50")
NIFTY_NEXT_50 = NiftyIndex("Nifty Next 50", "Nifty Next 50", "NIFTY NEXT 50")
NIFTY_MIDCAP_150 = NiftyIndex("NIFTY MIDCAP 150", "Nifty Midcap 150", "NIFTY MIDCAP 150")
NIFTY_SMALLCAP_250 = NiftyIndex("NIFTY SMLCAP 250", "NIFTY Smallcap 250", "NIFTY SMLCAP 250")


def _fmt_request_date(when: dt.date) -> str:
    """dd-MMM-yyyy, e.g. 01-Jul-2025. Any other format is rejected or silently empty."""
    return f"{when.day:02d}-{_MONTHS[when.month - 1]}-{when.year}"


def _parse_response_date(raw: object) -> Optional[dt.date]:
    """'17 Aug 2026' -> date. Locale-independent."""
    parts = str(raw or "").strip().split()
    if len(parts) != 3:
        return None
    month = _MONTH_INDEX.get(parts[1].lower()[:3])
    if month is None:
        return None
    try:
        return dt.date(int(parts[2]), month, int(parts[0]))
    except ValueError:
        return None


def _backpage(
    http: Http,
    method: str,
    index: NiftyIndex,
    start: dt.date,
    end: dt.date,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Call one /BackPage/ range API.

    `cinfo` is a JSON *string* containing single-quoted pseudo-JSON — not nested JSON. Sending
    real nested JSON, or double quotes inside, does not work.
    """
    cinfo = (
        "{'name':'%s','startDate':'%s','endDate':'%s','indexName':'%s'}"
        % (index.post_name, _fmt_request_date(start), _fmt_request_date(end), index.post_index_name)
    )
    payload = http.post_json(
        BACKPAGE.format(method=method),
        {"cinfo": cinfo},
        timeout=POST_TIMEOUT,
    )
    if payload is None:
        return [], f"{method} unreachable (a timeout here usually means a rejected User-Agent)"
    if not isinstance(payload, list):
        return [], f"{method} returned an unexpected shape"
    if not payload:
        # HTTP 200 with [] means the index code was wrong, not that no data exists.
        return [], f"{method} returned no rows for '{index.post_name}' (index code rejected)"
    return [row for row in payload if isinstance(row, dict)], None


def _series_from_rows(
    rows: list[dict[str, Any]],
    date_key: str,
    mapping: dict[str, str],
) -> dict[dt.date, dict[str, float]]:
    """Fold API rows into {date: {field: value}}.

    The date key differs per endpoint — `Date` for TRI, `DATE` for PE, `HistoricalDate` for
    OHLC — so it is passed in rather than guessed. Values arrive as strings and may be
    leading-dot decimals like '.57' or '-.12'.
    """
    out: dict[dt.date, dict[str, float]] = {}
    for row in rows:
        when = _parse_response_date(row.get(date_key))
        if when is None:
            continue
        fields: dict[str, float] = {}
        for source_key, field in mapping.items():
            value = parse_float(row.get(source_key))
            if value is None:
                continue
            # Zero is BSE/NSE filler for "not published" in a valuation ratio, never a real PE.
            if field in ("pe", "pb") and value == 0:
                continue
            fields[field] = value
        if fields:
            out[when] = fields
    return out


class LiveWatch:
    """Shared snapshot of every NIFTY index level.

    One fetch serves all four instruments, so it is cached per run. Served from Azure Blob with
    no WAF and no User-Agent requirement, which makes it the safest live source available.
    """

    def __init__(self) -> None:
        self._rows: Optional[dict[str, dict[str, Any]]] = None

    def load(self, http: Http) -> dict[str, dict[str, Any]]:
        if self._rows is not None:
            return self._rows
        payload = http.get(LIVE_WATCH_URL, expect="json", timeout=45)
        rows: dict[str, dict[str, Any]] = {}
        if isinstance(payload, dict):
            for row in payload.get("data") or []:
                if isinstance(row, dict) and row.get("indexName"):
                    rows[str(row["indexName"]).strip().upper()] = row
        if not rows:
            log.warning("LiveIndicesWatch returned no usable rows")
        self._rows = rows
        return rows

    def quote(self, http: Http, index: NiftyIndex) -> Optional[dict[str, Any]]:
        # Equality, not substring: 'NIFTY 50' is a prefix of 'NIFTY 50 ARBITRAGE' and four other
        # unrelated indices.
        return self.load(http).get(index.live_name.strip().upper())


def fetch(
    http: Http,
    series: Series,
    spec: InstrumentSpec,
    today: dt.date,
    *,
    index: NiftyIndex,
    live: LiveWatch,
) -> FetchResult:
    result = FetchResult()

    # --- how far back to ask ------------------------------------------------------------
    _, newest = series.latest("tri" if spec.basis == "tri" else "level")
    if newest is None or len(series.dates_with(spec.basis)) < MIN_HEALTHY_POINTS:
        start = today - dt.timedelta(days=COLD_START_DAYS)
    else:
        start = newest - dt.timedelta(days=WARM_OVERLAP_DAYS)

    # --- total return -------------------------------------------------------------------
    rows, error = _backpage(http, "getTotalReturnIndexString", index, start, today)
    if error:
        result.errors.append(error)
    else:
        # NTR_Value is a real number only for Nifty 50; it is the literal '-' elsewhere, which
        # parse_float correctly reads as None.
        for when, fields in _series_from_rows(rows, "Date", {"TotalReturnsIndex": "tri", "NTR_Value": "ntr"}).items():
            result.history.setdefault(when, {}).update(fields)

    # --- valuation ----------------------------------------------------------------------
    rows, error = _backpage(http, "getpepbHistoricaldataDBtoString", index, start, today)
    if error:
        result.errors.append(error)
    else:
        for when, fields in _series_from_rows(
            rows, "DATE", {"pe": "pe", "pb": "pb", "divYield": "div_yield"}
        ).items():
            result.history.setdefault(when, {}).update(fields)

    # --- price history, only when the lookback table is computed on the price index -------
    if spec.basis == "level":
        rows, error = _backpage(http, "getHistoricaldatatabletoString", index, start, today)
        if error:
            result.errors.append(error)
        else:
            for when, fields in _series_from_rows(rows, "HistoricalDate", {"CLOSE": "level"}).items():
                result.history.setdefault(when, {}).update(fields)

    for when, fields in result.history.items():
        series.upsert(when, fields)

    # --- headline readings from the stored series ----------------------------------------
    tri_value, tri_date = series.latest("tri")
    if tri_value is not None:
        result.add("tri", tri_value, as_of=tri_date, freshness=FRESHNESS_PREV_CLOSE, source="niftyindices TRI")

    for field, source in (("pe", "niftyindices PE/PB"), ("pb", "niftyindices PE/PB"), ("div_yield", "niftyindices PE/PB")):
        value, when = series.latest(field)
        if value is not None:
            result.add(field, value, as_of=when, source=source)

    # --- live level ---------------------------------------------------------------------
    # `timeVal` carries the actual last-tick time, e.g. '17-Aug-2026 15:30'. We trust that over
    # the wall clock: outside market hours this feed still serves the previous close, and
    # labelling that as "live" would both mislead the reader and make the day change compute as
    # 0.00% (the last close compared against itself).
    quote = live.quote(http, index)
    if quote:
        last = parse_float(quote.get("last"))
        tick = _parse_tick(quote.get("timeVal"))
        if last is not None:
            is_live = tick is None or tick == today
            result.add(
                "level",
                last,
                as_of=tick or today,
                freshness=FRESHNESS_LIVE if is_live else FRESHNESS_PREV_CLOSE,
                source="LiveIndicesWatch",
            )
            if not is_live and tick is not None:
                # Market shut: this is a real close, so it belongs in history.
                result.history.setdefault(tick, {}).setdefault("level", last)
    else:
        result.errors.append("live level unavailable (LiveIndicesWatch)")
        close, close_date = series.latest("level")
        if close is not None:
            result.add("level", close, as_of=close_date, freshness=FRESHNESS_PREV_CLOSE, source="niftyindices OHLC")

    if not result.readings:
        result.errors.append("no NSE data could be retrieved")
    return result


def _parse_tick(raw: object) -> Optional[dt.date]:
    """'17-Aug-2026 15:30' -> date(2026, 8, 17). Locale-independent."""
    head = str(raw or "").strip().split(" ")[0]
    parts = head.split("-")
    if len(parts) != 3:
        return None
    month = _MONTH_INDEX.get(parts[1].lower()[:3])
    if month is None:
        return None
    try:
        return dt.date(int(parts[2]), month, int(parts[0]))
    except ValueError:
        return None

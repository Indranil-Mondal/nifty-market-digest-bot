"""Small Yahoo Finance chart client.

Used only where no exchange or regulator publishes what we need: intraday index levels that
Indian exchanges do not release until after close, and the USD metals series behind the
gold:silver ratio.

Yahoo is treated as a convenience, never a dependency. It is unversioned, undocumented and free,
so any caller must degrade cleanly when it returns nothing. Two hazards worth knowing:

  * An unknown symbol returns {"chart":{"result":null,"error":{...}}} at HTTP 200.
  * A *delisted* symbol returns HTTP 200 with a real-looking price frozen years in the past.
    `series()` therefore returns dates alongside closes, and `last()` returns the tick date, so
    callers can reject stale data instead of printing it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from ..http import Http
from ..util import IST, parse_float

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _chart(http: Http, symbol: str, *, range_: str, interval: str) -> Optional[dict]:
    payload = http.get(
        CHART_URL.format(symbol=symbol),
        params={"range": range_, "interval": interval},
        expect="json",
        timeout=40,
    )
    if not isinstance(payload, dict):
        return None
    chart = payload.get("chart") or {}
    results = chart.get("result")
    if not results:
        log.warning("Yahoo has no data for %s (%s)", symbol, (chart.get("error") or {}))
        return None
    return results[0]


def last(http: Http, symbol: str) -> tuple[Optional[float], Optional[dt.date]]:
    """Latest price and the date it belongs to.

    The date matters: outside market hours Yahoo keeps serving the last close, and a delisted
    symbol serves a price years old. Callers compare the date against today before calling
    anything "live".
    """
    result = _chart(http, symbol, range_="5d", interval="1d")
    if not result:
        return None, None
    meta = result.get("meta") or {}
    price = parse_float(meta.get("regularMarketPrice"))
    when: Optional[dt.date] = None
    epoch = meta.get("regularMarketTime")
    if isinstance(epoch, (int, float)):
        when = dt.datetime.fromtimestamp(float(epoch), tz=IST).date()
    return price, when


def series(http: Http, symbol: str, *, range_: str = "1y") -> dict[dt.date, float]:
    """Daily closes keyed by date. Empty dict on any failure."""
    result = _chart(http, symbol, range_=range_, interval="1d")
    if not result:
        return {}
    stamps = result.get("timestamp") or []
    try:
        closes = result["indicators"]["quote"][0]["close"] or []
    except (KeyError, IndexError, TypeError):
        return {}

    out: dict[dt.date, float] = {}
    for epoch, close in zip(stamps, closes):
        value = parse_float(close)
        if value is None or not isinstance(epoch, (int, float)):
            continue
        out[dt.datetime.fromtimestamp(float(epoch), tz=IST).date()] = value
    return out

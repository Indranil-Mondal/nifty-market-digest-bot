"""India VIX — the market's own estimate of how much the Nifty will move.

Why it belongs beside the valuation blocks: PE says what the market costs, VIX says how nervous
it is about that price. The two disagree often, and the disagreement is the interesting part. On
9 Sep 2026 the Nifty 50 PE sat at the 2nd percentile of its own year while VIX sat at the 23rd —
cheap and calm at once, which is not the usual pairing.

Sourcing, and the honest limitation. NSE computes and publishes India VIX, but the only
programmatic route to it is nseindia.com, which sits behind Akamai Bot Manager and is the one
host this project refuses to depend on (see nse.py). It is NOT in the LiveIndicesWatch file that
serves the other NIFTY blocks — that file carries 131 indices and VIX is not among them, which
was checked rather than assumed. So this comes from Yahoo's `^INDIAVIX`, which mirrors the NSE
figure with a delay and is treated the way every other Yahoo dependency here is: a convenience
that must degrade to nothing rather than to a wrong number.

VIX is a percentage — an annualised standard deviation of expected 30-day returns — so it is
printed to two decimals with no thousands separator and no currency.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..model import FRESHNESS_LIVE, FRESHNESS_PREV_CLOSE
from ..state import Series
from . import yahoo

log = logging.getLogger(__name__)

SYMBOL = "^INDIAVIX"

# India VIX has closed as low as ~8.5 and, on the worst day of March 2020, above 80. A reading
# outside this band means Yahoo served something that is not India VIX -- a different instrument
# under a reused ticker, or a decimal shift -- and a volatility gauge printing 3 or 300 would be
# taken at face value by a reader. Refuse it instead.
PLAUSIBLE = (3.0, 150.0)

# Two years for the same reason gsr.py fetches two: a "1Y" lookback anchors *about* twelve months
# back, so a one-year pull can leave the 12-month row with no base on its first run.
RANGE = "2y"


def fetch(http: Http, series: Series, spec: InstrumentSpec, today: dt.date) -> FetchResult:
    result = FetchResult()

    closes = yahoo.series(http, SYMBOL, range_=RANGE)
    if not closes:
        result.errors.append(f"India VIX unavailable ({SYMBOL} returned no series)")
        return result

    rejected = 0
    for when, value in closes.items():
        if PLAUSIBLE[0] <= value <= PLAUSIBLE[1]:
            result.history[when] = {"level": value}
        else:
            rejected += 1

    if not result.history:
        result.errors.append(
            f"India VIX: all {rejected} value(s) fell outside {PLAUSIBLE[0]}-{PLAUSIBLE[1]}; "
            "the symbol may no longer be India VIX"
        )
        return result
    if rejected:
        log.warning("India VIX: dropped %s implausible value(s)", rejected)

    for when, fields in result.history.items():
        series.upsert(when, fields)

    newest = max(result.history)
    current = result.history[newest]["level"]
    result.add(
        "level",
        current,
        as_of=newest,
        freshness=FRESHNESS_LIVE if newest >= today else FRESHNESS_PREV_CLOSE,
        source="Yahoo ^INDIAVIX",
    )
    return result

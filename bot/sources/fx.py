"""USD/INR, for the blocks whose return is partly a currency move.

Three instruments in this digest are priced in rupees but driven by a dollar number: the Edelweiss
feeder fund holds a US index, and both metal ETFs track an internationally-quoted commodity
translated at the day's rate. Their headline percentages therefore blend two things a reader
cannot separate by eye.

It is not a small effect. Over the year to 9 Sep 2026 the rupee moved 7.5%, so a meaningful slice
of the silver block's +85% and the Russell sleeve's +26% is the currency rather than the asset.
The digest does not attempt to decompose the return — doing that properly needs the fund's own
FX-hedging policy, which is not published in a machine-readable form — it just shows the rate and
its own one-year move alongside, and leaves the arithmetic to the reader.

One request per run regardless of how many instruments use it: the result is cached on the
UsdInr instance the registry hands to each fetcher, the same pattern nse.LiveWatch uses.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from ..http import Http
from ..model import FxRate
from ..util import pct_change
from . import yahoo

log = logging.getLogger(__name__)

SYMBOL = "USDINR=X"

# The rupee has never been stronger than 39 or weaker than 120 to the dollar in the floating era.
# Outside this band Yahoo is serving INR/USD, a different pair, or a decimal error -- all of which
# would print as a plausible-looking rate.
PLAUSIBLE = (39.0, 120.0)


class UsdInr:
    """One rate per run, shared across every instrument that asks for it."""

    def __init__(self) -> None:
        self._loaded = False
        self._rate: Optional[FxRate] = None

    def rate(self, http: Http) -> Optional[FxRate]:
        if self._loaded:
            return self._rate
        self._loaded = True

        closes = yahoo.series(http, SYMBOL, range_="1y")
        if not closes:
            log.warning("USD/INR unavailable (%s returned no series)", SYMBOL)
            return None

        newest = max(closes)
        current = closes[newest]
        if not PLAUSIBLE[0] <= current <= PLAUSIBLE[1]:
            log.warning(
                "USD/INR %.3f outside the plausible band %s; ignoring", current, PLAUSIBLE
            )
            return None

        # The oldest close in a 1y pull is the natural base, but say nothing rather than
        # something wrong if the window came back short.
        oldest = min(closes)
        pct = None
        if (newest - oldest).days >= 300:
            pct = pct_change(current, closes[oldest])

        self._rate = FxRate(rate=current, as_of=newest, pct_1y=pct)
        return self._rate

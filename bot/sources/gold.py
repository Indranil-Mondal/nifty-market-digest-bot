"""Nippon India ETF Gold BeES.

Identity, each confirmed against a primary source:
    AMFI scheme code  140088
    ISIN              INF204KB17I5      (INF, not INE -- it is a mutual-fund ISIN)
    NSE symbol        GOLDBEES
    BSE scrip code    590095

EVERY fetch validates the ISIN or the fund name, never the numeric code alone. That is not
defensive habit, it is a measured hazard: AMFI scheme code 140089 returns "Nippon India ETF
Nifty PSU Bank BeES" and BSE scrip 590096 returns "Nippon India ETF Liquid BeES" -- both at
HTTP 200, both silently the wrong fund.

The shared plumbing -- bhavcopy, quote endpoint, iNAV, premium -- lives in bse_etf, because the
Zerodha Silver ETF is the same shape and two copies of that parsing would drift apart. What is
here is what is specific to gold.

Three things the user asked for do not exist for this instrument, and are reported as dashes
rather than approximated:

  * PE / PB / dividend yield -- gold is a physical commodity with no earnings. There is no
    numerator to find; no source is withholding it.
  * A true iNAV from the AMC -- Nippon's own realtime endpoint sits behind Cloudflare Bot
    Management and returns 403 to datacenter IPs. BSE's Header.NAVRate carries the AMC's
    realtime NAV instead, which is the same number by another route.
  * "Domestic Price of Gold Index" -- this is not a published index anywhere at any price. The
    scheme document defines it as a formula the AMC computes internally, licensing no index
    provider. The AMFI NAV series *is* the domestic gold price net of the expense ratio, and
    IBJA's 999 rate is the closest genuinely published domestic physical gold price, so that is
    carried as a cross-check.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..state import Series
from . import amfi, bse_etf
from .bse_etf import premium_str  # noqa: F401  (kept importable from here for callers/tests)

log = logging.getLogger(__name__)

ISIN = "INF204KB17I5"
BSE_SCRIP = "590095"
NAME_FRAGMENT = "GOLD BEES"
NSE_SYMBOL = "GOLDBEES"

# mf=21 is Nippon India Mutual Fund, established empirically rather than guessed: that response's
# AMC header reads "Nippon India Mutual Fund" and contains scheme 140088, while mf=53 returns
# Axis Mutual Fund data with zero occurrences of it.
GOLD_BEES = amfi.AmfiScheme(
    scheme_code="140088",
    isin=ISIN,
    mf_code="21",
    label="Nippon India ETF Gold BeES",
)

ETF = bse_etf.ListedEtf(
    amfi_scheme=GOLD_BEES,
    bse_scrip=BSE_SCRIP,
    nse_symbol=NSE_SYMBOL,
    name_fragment=NAME_FRAGMENT,
    cold_start_days=430,
    warm_overlap_days=15,
    min_healthy_points=180,
)

# IBJA publishes gold per 10 grams. Anything outside this band means the page layout moved and
# we scraped a different number.
IBJA_GOLD = bse_etf.IbjaMetal(
    element="lblGold999",
    label="IBJA 999 gold",
    grams_per_quote=10.0,
    unit_suffix="10g",
    plausible=(50_000.0, 500_000.0),
)


def fetch(http: Http, series: Series, spec: InstrumentSpec, today: dt.date) -> FetchResult:
    result = bse_etf.fetch_prices(http, series, ETF, today)

    rate = bse_etf.ibja_rate(http, IBJA_GOLD)
    if rate is not None:
        result.notes.append(f"{IBJA_GOLD.label} ₹{rate:,.0f}/{IBJA_GOLD.unit_suffix}")
        nav = result.readings.get("nav")
        if nav is not None and nav.value:
            # Grams of gold backing one unit. A stable ratio; a jump of more than about a
            # percent day-over-day means one of the two sources has broken.
            log.info(
                "gold backing per unit: %.6f g", nav.value / (rate / IBJA_GOLD.grams_per_quote)
            )

    if not result.readings:
        result.errors.append("no gold data could be retrieved")
    return result

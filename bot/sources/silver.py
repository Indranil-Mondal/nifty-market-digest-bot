"""Zerodha Silver ETF.

Identity, each confirmed against a primary source rather than inferred:
    AMFI scheme code  153413
    ISIN              INF0R8F01091      (INF, not INE -- it is a mutual-fund ISIN)
    AMFI AMC code     77                (Zerodha Fund House)
    NSE / BSE ticker  SILVERCASE
    BSE scrip code    544384

The request was for "the Zerodha Silver index". No such index exists: Zerodha Fund House
publishes no silver index, and none of the domestic silver benchmarks is theirs. What they do
run is a physically-backed silver ETF and a fund-of-fund that feeds it, so the ETF is what is
tracked here. It is the closer analogue of the gold block -- exchange-listed, so it has a traded
price and an iNAV, not just a once-a-day NAV:

    153413  Zerodha Silver ETF        listed, price + NAV + iNAV     <- tracked
    153656  Zerodha Silver ETF FoF    unlisted, NAV only

The FoF's NAV is published about a day earlier than the ETF's, which is the only argument in its
favour; against that it has no market price, no iNAV, and an extra layer of expense between the
holder and the metal.

Identity is proven at every source and never assumed from the ticker: the scrip code is matched
by ISIN in the bhavcopy, and the quote endpoint's own company name is checked before its numbers
are believed. Adjacent codes belong to entirely different funds -- scrip 544132 is the Zerodha
*Gold* ETF, one row away in the same bhavcopy.

PE does not exist here for the same reason it does not exist for gold: silver is a physical
commodity with no earnings, so there is no numerator to look for.

Sanity of scale: at an official NAV around 23.3 and IBJA 999 silver around 233,800 per kilogram,
one unit is backed by roughly a tenth of a gram of silver. That ratio is printed to the run log
each morning, because a sudden move in it means one of the two sources has broken rather than
that silver has.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..state import Series
from . import amfi, bse_etf

log = logging.getLogger(__name__)

ISIN = "INF0R8F01091"
BSE_SCRIP = "544384"
NAME_FRAGMENT = "SILVER ETF"
NSE_SYMBOL = "SILVERCASE"

# mf=77 is Zerodha Mutual Fund, established empirically: that response carries schemes 153413
# (Silver ETF) and 152476 (Gold ETF) and its rows all name Zerodha, while the neighbouring codes
# 76 and 78 return Helios and Old Bridge respectively.
ZERODHA_SILVER = amfi.AmfiScheme(
    scheme_code="153413",
    isin=ISIN,
    mf_code="77",
    label="Zerodha Silver ETF",
)

# Inception was 26 Mar 2025 (first AMFI NAV 10.1689), so the cold start is set to reach past
# it rather than to the usual 430 days, which would stop in July 2025 and quietly leave the
# fund's first four months out of the cache. AMFI simply returns nothing before the first NAV,
# so asking for more than exists costs one extra window and no correctness.
ETF = bse_etf.ListedEtf(
    amfi_scheme=ZERODHA_SILVER,
    bse_scrip=BSE_SCRIP,
    nse_symbol=NSE_SYMBOL,
    name_fragment=NAME_FRAGMENT,
    cold_start_days=560,
    warm_overlap_days=15,
    min_healthy_points=180,
)

# IBJA publishes silver per KILOGRAM, where gold is per 10 grams. Carrying the unit explicitly
# is the point: getting it wrong would not look wrong, it would just put the grams-per-unit
# cross-check out by a factor of a hundred.
IBJA_SILVER = bse_etf.IbjaMetal(
    element="lblSilver999",
    label="IBJA 999 silver",
    grams_per_quote=1000.0,
    unit_suffix="kg",
    plausible=(20_000.0, 2_000_000.0),
)


def fetch(http: Http, series: Series, spec: InstrumentSpec, today: dt.date) -> FetchResult:
    result = bse_etf.fetch_prices(http, series, ETF, today)

    rate = bse_etf.ibja_rate(http, IBJA_SILVER)
    if rate is not None:
        result.notes.append(f"{IBJA_SILVER.label} ₹{rate:,.0f}/{IBJA_SILVER.unit_suffix}")
        nav = result.readings.get("nav")
        if nav is not None and nav.value:
            log.info(
                "silver backing per unit: %.6f g",
                nav.value / (rate / IBJA_SILVER.grams_per_quote),
            )

    if not result.readings:
        result.errors.append("no silver data could be retrieved")
    return result

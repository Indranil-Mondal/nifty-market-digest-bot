"""Russell 1000 Equal Weight Technology — tracked via the Edelweiss US Technology Equity FoF.

Identity, proved from primary sources rather than inferred:

  * The index is "Russell 1000(R) Equal Weight Technology Index" -- Equal *Weight*, not "Equal
    Weighted". The latter is J.P. Morgan's house wording, which Indian distributor literature
    copies. FTSE Russell's own factsheet (issueName D4143001) uses "Equal Weight".
  * The Indian holding benchmarked to it is the EDELWEISS US TECHNOLOGY EQUITY FUND OF FUND,
    which feeds JPMorgan Funds - US Technology Fund (LU0082616367). J.P. Morgan's factsheet
    names the benchmark verbatim as "Russell 1000 Equal Weight Technology Index (Total Return
    Net of 30% withholding tax)".
  * Grepping AMFI's full NAV file for "russell" returns nothing, so the benchmark never appears
    in a scheme name -- the link can only be made through fund documents.

Four things do not exist for this instrument. They are reported as unavailable, and no proxy is
silently substituted:

  * The index level and its daily history. FTSE distributes this index end-of-day "via FTP and
    email" to licensees only. Yahoo's ^R1EWTEC has been frozen since October 2025.
  * PE, on any date. FTSE's factsheet prints no P/E for this index at all -- only constituent
    count, dividend yield and weight statistics. There is no ETF whose issuer might publish a
    portfolio P/E either. Substituting XLK's or the parent Russell 1000's PE would be a
    different instrument's valuation, so the digest shows a dash.
  * A tracking ETF. The only one ever built on this index (Questrade QRT, TSX) was delisted in
    2017. RSPT is S&P-500-based and EQAL covers all sectors, so neither is this index.
  * iNAV. This is an open-ended fund of fund, not an exchange-listed ETF: one NAV per day, no
    intraday indicative value.

NAV timing matters here and is easy to get wrong. The fund is India-domiciled but holds US
assets, so its NAV reflects the PREVIOUS US session -- established by weekday-isolated
regression and by an event study over the year's largest tech sessions, not by assumption. A
Friday US close occurs at 01:30 IST on Saturday and therefore cannot be inside Friday's Indian
NAV. Publication is also slower than most: the NAV for a Monday may not appear until well into
Tuesday. So we never assume T-1; we search a window and print the date we actually got.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..model import FRESHNESS_T1
from ..state import Series
from . import amfi

log = logging.getLogger(__name__)

# Direct Plan Growth. The regular plan is scheme 148064 / ISIN INF754K01LC5 -- swap the two
# fields below if that is the plan actually held. Percentage moves differ only by the expense
# gap, but the NAV level differs noticeably, so it is worth setting correctly.
EDELWEISS_US_TECH = amfi.AmfiScheme(
    scheme_code="148063",
    isin="INF754K01LB7",
    mf_code="47",                      # Edelweiss Mutual Fund
    label="Edelweiss US Technology Equity FoF - Direct Growth",
)

COLD_START_DAYS = 430
WARM_OVERLAP_DAYS = 20                 # wide, because this fund skips both Indian and US holidays
MIN_HEALTHY_POINTS = 170               # ~233 NAV points a year, not ~250: it loses both calendars


def fetch(http: Http, series: Series, spec: InstrumentSpec, today: dt.date) -> FetchResult:
    result = FetchResult()

    _, newest = series.latest("nav")
    if newest is None or len(series.dates_with("nav")) < MIN_HEALTHY_POINTS:
        start = today - dt.timedelta(days=COLD_START_DAYS)
    else:
        start = newest - dt.timedelta(days=WARM_OVERLAP_DAYS)

    history, errors = amfi.nav_history(http, EDELWEISS_US_TECH, start, today)
    result.history.update(history)
    result.errors.extend(errors)
    for when, fields in history.items():
        series.upsert(when, fields)

    nav_value, nav_date = series.latest("nav")
    if nav_value is not None:
        result.add("nav", nav_value, as_of=nav_date, freshness=FRESHNESS_T1, source="AMFI")
        if nav_date is not None:
            behind = (today - nav_date).days
            # Say which session the number actually reflects. Without this the figure looks a day
            # or two stale every single morning, and a reader cannot tell a lag from a fault.
            result.notes.append(
                f"NAV of {nav_date:%d %b} reflects the prior US session"
                + (f" · {behind}d behind today" if behind > 1 else "")
            )
    else:
        result.errors.append("no NAV available for the Edelweiss US Technology FoF")

    return result

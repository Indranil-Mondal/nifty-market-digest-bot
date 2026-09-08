"""The tracked instruments and how each is fetched.

This is the only file to edit when adding or removing something from the digest. Order here is
the order in the message.

`basis` is the series the lookback table is computed on. The four indices the user specified as
TRI use the total-return series, which is the correct basis for a return comparison because it
includes dividends. Nifty 50 and Nifty Next 50 were specified as plain indices, so they use the
price level — their TRI is fetched anyway (it arrives in the same call) and is displayed, just
not used as the comparison basis.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import partial
from typing import Callable

from .compute import FetchResult, InstrumentSpec
from .http import Http
from .sources import bse, fx, gold, gsr, nse, russell_tech, silver, vix
from .state import Series

Fetcher = Callable[[Http, Series, InstrumentSpec, dt.date], FetchResult]


@dataclass(frozen=True)
class Registration:
    spec: InstrumentSpec
    fetch: Fetcher


# Caveats that are permanent properties of the instrument, not run-time failures. They are
# rendered as italic notes, never as errors.
TRI_EOD_NOTE = "TRI and PE publish after close, so both are as of the last completed session"


def build_registry() -> list[Registration]:
    # One shared LiveIndicesWatch fetch serves every NIFTY instrument.
    live = nse.LiveWatch()
    # One shared USD/INR fetch serves every currency-exposed instrument.
    rupee = fx.UsdInr()

    def nifty(index: nse.NiftyIndex) -> Fetcher:
        return partial(nse.fetch, index=index, live=live)

    def with_fx(fetcher: Fetcher) -> Fetcher:
        """Attach the day's USD/INR to an instrument priced in rupees but driven in dollars.

        Wrapped here rather than plumbed through each source, so the registry stays the one place
        that answers "which blocks show the rupee, and why" -- and so a source module keeps
        knowing only about its own upstream.
        """
        def wrapped(http: Http, series: Series, spec: InstrumentSpec, today: dt.date) -> FetchResult:
            result = fetcher(http, series, spec, today)
            result.fx = rupee.rate(http)
            return result
        return wrapped

    return [
        Registration(
            InstrumentSpec(
                key="nifty_smallcap_250",
                display="NIFTY SMALLCAP 250 TRI",
                kind="index",
                basis="tri",
                basis_label="TRI (total return)",
                has_pe=True,
                notes=(TRI_EOD_NOTE,),
            ),
            nifty(nse.NIFTY_SMALLCAP_250),
        ),
        Registration(
            InstrumentSpec(
                key="bse_250_smallcap",
                display="BSE 250 SMALLCAP TRI",
                kind="index",
                basis="tri",
                basis_label="TRI (total return)",
                has_pe=True,
                notes=(
                    TRI_EOD_NOTE,
                    'formerly "S&P BSE 250 SmallCap" — co-brand retired',
                ),
            ),
            bse.fetch,
        ),
        Registration(
            InstrumentSpec(
                key="nifty_midcap_150",
                display="NIFTY MIDCAP 150 TRI",
                kind="index",
                basis="tri",
                basis_label="TRI (total return)",
                has_pe=True,
                notes=(TRI_EOD_NOTE,),
            ),
            nifty(nse.NIFTY_MIDCAP_150),
        ),
        Registration(
            InstrumentSpec(
                key="russell_1000_ew_tech",
                display="RUSSELL 1000 EW TECH (Edelweiss FoF)",
                kind="fund",
                basis="nav",
                basis_label="fund NAV",
                # FTSE's own factsheet for this index prints no P/E, and no ETF tracks it, so
                # there is no free source for PE on any date. Substituting the parent Russell
                # 1000's or XLK's PE would be a different instrument's valuation.
                has_pe=False,
                notes=(
                    "index level is licensee-only; tracked via the feeder fund's NAV",
                ),
                # It needs an Indian business day AND a fresh US NAV, so its no-NAV dates are
                # the union of two market calendars. Six calendar days behind is normal here;
                # it hit six on Mon 29 Dec 2025 with nothing wrong.
                stale_after_days=9,
            ),
            with_fx(russell_tech.fetch),
        ),
        Registration(
            InstrumentSpec(
                key="gold_goldbees",
                display="GOLD — NIPPON GOLD BeES",
                kind="etf",
                basis="nav",
                basis_label="NAV",
                # Gold has no earnings, so there is no PE to fetch or display -- not a gap in
                # coverage, an absence in the instrument.
                has_pe=False,
                notes=(
                    'benchmark "domestic price of gold" is an internal AMC formula, not a published index',
                ),
            ),
            with_fx(gold.fetch),
        ),
        Registration(
            InstrumentSpec(
                key="silver_zerodha",
                display="SILVER — ZERODHA SILVER ETF",
                kind="etf",
                basis="nav",
                basis_label="NAV",
                # Silver has no earnings either. Same absence as gold, same dash.
                has_pe=False,
                notes=(
                    "physically backed; one unit is about a tenth of a gram of silver",
                ),
            ),
            with_fx(silver.fetch),
        ),
        Registration(
            InstrumentSpec(
                key="gold_silver_ratio",
                display="GOLD : SILVER RATIO",
                kind="index",
                basis="level",
                basis_label="ratio",
                has_pe=False,
                # This block computes and prints its own percentile inside gsr.py, including the
                # long-run reference band and the domestic IBJA cross-check. A second, plainer
                # position line underneath would say the same thing worse.
                lead_range="none",
                notes=("COMEX futures, USD/oz both legs — a relative-value gauge, not advice",),
            ),
            gsr.fetch,
        ),
        Registration(
            InstrumentSpec(
                key="nifty_50",
                display="NIFTY 50",
                kind="index",
                basis="level",
                has_pe=True,
            ),
            nifty(nse.NIFTY_50),
        ),
        Registration(
            InstrumentSpec(
                key="nifty_next_50",
                display="NIFTY NEXT 50",
                kind="index",
                basis="level",
                has_pe=True,
            ),
            nifty(nse.NIFTY_NEXT_50),
        ),
        Registration(
            InstrumentSpec(
                key="india_vix",
                display="INDIA VIX",
                kind="index",
                basis="level",
                basis_label="index",
                # A volatility index has no earnings and therefore no PE, in the same way gold
                # does not -- an absence in the instrument, not a gap in coverage.
                has_pe=False,
                # VIX mean-reverts, so a percentile is the reading that means something. The
                # default "60% off the 1Y high" phrasing would be arithmetically true and read
                # as a loss, when what it actually describes is an unusually calm market.
                lead_range="percentile",
                notes=("expected 30-day Nifty volatility, annualised — a gauge of "
                       "nervousness, not direction",),
            ),
            vix.fetch,
        ),
    ]

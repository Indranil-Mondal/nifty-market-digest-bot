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
from .sources import bse, gold, gsr, nse, russell_tech
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

    def nifty(index: nse.NiftyIndex) -> Fetcher:
        return partial(nse.fetch, index=index, live=live)

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
            ),
            russell_tech.fetch,
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
            gold.fetch,
        ),
        Registration(
            InstrumentSpec(
                key="gold_silver_ratio",
                display="GOLD : SILVER RATIO",
                kind="index",
                basis="level",
                basis_label="ratio",
                has_pe=False,
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
    ]

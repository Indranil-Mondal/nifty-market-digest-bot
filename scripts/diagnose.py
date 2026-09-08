"""Reachability check for every source, runnable anywhere.

    python -m scripts.diagnose

Its reason for existing: several of these hosts behave differently from a datacenter IP than from
a home connection, and that cannot be tested from a laptop. Running this as a GitHub Actions step
answers the question directly instead of inferring it.

Sources are marked CRITICAL or OPTIONAL. Exit code is non-zero only if a CRITICAL source fails,
because the optional ones all have a documented fallback and their loss degrades the digest
rather than breaking it.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.http import Http                                            # noqa: E402
from bot.sources import amfi, bse, bse_etf, gold, gsr, nse           # noqa: E402
from bot.sources import russell_tech, silver                         # noqa: E402
from bot.sources.amfi import HISTORY_URL                             # noqa: E402
from bot.util import fmt_ddmmmyyyy, ist_today                        # noqa: E402

# How far behind a published NAV may fall before the source counts as broken rather than slow.
# An India-domiciled ETF publishes daily, so a week's silence is a fault. The overseas feeder
# fund legitimately lags both calendars, so it gets longer before we call it.
NAV_STALE_DAYS = 7
NAV_STALE_DAYS_OVERSEAS = 12

CRITICAL, OPTIONAL = "CRITICAL", "optional"


def main() -> int:
    # Windows consoles default to cp1252 and mangle the em dashes in these labels.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    http = Http(retries=2)
    today = ist_today()
    recent = today - dt.timedelta(days=1)
    rows: list[tuple[str, str, str, bool, str]] = []

    def check(name: str, tier: str, host: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:                             # noqa: BLE001
            ok, detail = False, f"{exc.__class__.__name__}: {exc}"
        rows.append((name, tier, host, ok, str(detail)[:60]))

    # --- NSE / NIFTY ------------------------------------------------------------------
    def nifty_tri():
        got, err = nse._backpage(
            http, "getTotalReturnIndexString", nse.NIFTY_50, today - dt.timedelta(days=10), today
        )
        return bool(got), err or f"{len(got)} rows"

    def nifty_pe():
        got, err = nse._backpage(
            http, "getpepbHistoricaldataDBtoString", nse.NIFTY_50, today - dt.timedelta(days=10), today
        )
        return bool(got), err or f"{len(got)} rows"

    def live_watch():
        rows_ = nse.LiveWatch().load(http)
        return bool(rows_), f"{len(rows_)} indices"

    check("niftyindices TRI (POST)", CRITICAL, "www.niftyindices.com", nifty_tri)
    check("niftyindices PE (POST)", CRITICAL, "www.niftyindices.com", nifty_pe)
    check("LiveIndicesWatch", OPTIONAL, "liveindexsa.niftyindices.com", live_watch)

    # --- BSE --------------------------------------------------------------------------
    def bse_prtr():
        got, err = bse._prtr_history(http, bse.BSE_250_SMALLCAP, today - dt.timedelta(days=15), today)
        return bool(got), err or f"{len(got)} rows"

    def bse_valuation():
        when, fields = bse._allindices_for_date(http, bse.BSE_250_SMALLCAP, recent)
        return bool(fields), f"{when} pe={fields.get('pe')}" if fields else "no row"

    check("bseindices PR/TR", CRITICAL, "www.bseindices.com", bse_prtr)
    check("BSE AllIndices CSV", CRITICAL, "www.bseindia.com", bse_valuation)

    # --- AMFI report shape --------------------------------------------------------------
    # The canary for the failure that actually happened. In Aug 2026 AMFI reordered this
    # report's columns; both layouts are eight fields wide, so nothing but the header names
    # distinguishes them, and every fund NAV in the digest froze for three weeks. Asserting the
    # header still resolves turns the next such change into a red line here instead of numbers
    # that quietly stop moving.
    def amfi_layout():
        lines = http.stream_lines(
            HISTORY_URL,
            params={"mf": "21", "tp": "1", "frmdt": fmt_ddmmmyyyy(recent), "todt": fmt_ddmmmyyyy(today)},
            keep="140088;",
            timeout=120,
        )
        if not lines:
            return False, "no response"
        for candidate in lines[:3]:
            columns = amfi.resolve_columns(candidate)
            if columns is not None:
                return True, f"isin@{columns.isin_growth} nav@{columns.nav} date@{columns.date}"
        return False, f"header not recognised: {lines[0][:80]!r}"

    check("AMFI report column layout", CRITICAL, "portal.amfiindia.com", amfi_layout)

    # --- listed commodity ETFs (gold, silver) --------------------------------------------
    def bhavcopy_for(etf):
        def run():
            when, fields = bse_etf.bhavcopy_close(http, etf.isin, recent)
            return bool(fields), f"{when} close={fields.get('level')}" if fields else "no row"
        return run

    def quote_for(etf):
        def run():
            quote = bse_etf.quote(http, etf)
            if quote is None:
                return False, "blocked or wrong instrument"
            return quote.price is not None, (
                f"ltp={quote.price} iNAV={quote.inav} ason={quote.price_date}"
            )
        return run

    def nse_etf_for(etf):
        def run():
            price = bse_etf.nse_price(http, etf.nse_symbol)
            return price is not None, f"ltP={price}"
        return run

    def nav_for(scheme, limit):
        def run():
            value, when = amfi.newest_nav(http, scheme, today)
            if value is None or when is None:
                return False, "no NAV rows"
            behind = (today - when).days
            # Reachable but frozen is the failure mode this bot has actually suffered, so
            # "we got a number" is not the test -- "we got a recent number" is.
            return behind <= limit, f"{when} nav={value} ({behind}d behind)"
        return run

    check("BSE bhavcopy CSV", CRITICAL, "www.bseindia.com", bhavcopy_for(gold.ETF))
    # This is THE open question: it is the only iNAV route that exists, and it is Akamai-fronted
    # behind a UA+Referer gate, so its behaviour from a datacenter IP cannot be predicted.
    check("BSE quote — GOLDBEES (iNAV)", OPTIONAL, "api.bseindia.com", quote_for(gold.ETF))
    check("BSE quote — SILVERCASE (iNAV)", OPTIONAL, "api.bseindia.com", quote_for(silver.ETF))
    check("NSE ETF list (price only)", OPTIONAL, "www.nseindia.com", nse_etf_for(gold.ETF))
    check("AMFI NAV — Gold BeES", CRITICAL, "portal.amfiindia.com",
          nav_for(gold.GOLD_BEES, NAV_STALE_DAYS))
    check("AMFI NAV — Zerodha Silver ETF", CRITICAL, "portal.amfiindia.com",
          nav_for(silver.ZERODHA_SILVER, NAV_STALE_DAYS))
    check("AMFI NAV — Edelweiss FoF", CRITICAL, "portal.amfiindia.com",
          nav_for(russell_tech.EDELWEISS_US_TECH, NAV_STALE_DAYS_OVERSEAS))

    # --- gold:silver ratio ------------------------------------------------------------
    def metals():
        g = gsr.yahoo.series(http, gsr.GOLD_SYMBOL, range_="1mo")
        s = gsr.yahoo.series(http, gsr.SILVER_SYMBOL, range_="1mo")
        return bool(g and s), f"gold {len(g)}pts, silver {len(s)}pts"

    def ibja():
        ratio = gsr._domestic_ratio(http)
        return ratio is not None, f"domestic ratio {ratio:.1f}" if ratio else "not parsed"

    check("Yahoo metals GC=F/SI=F", CRITICAL, "query1.finance.yahoo.com", metals)
    check("IBJA 999 rates", OPTIONAL, "www.ibjarates.com", ibja)

    # --- misc ---------------------------------------------------------------------------
    def amfi_raw():
        lines = http.stream_lines(
            HISTORY_URL,
            params={"mf": "21", "tp": "1", "frmdt": fmt_ddmmmyyyy(recent), "todt": fmt_ddmmmyyyy(today)},
            keep="140088;",
            timeout=120,
        )
        return bool(lines), f"{len(lines or [])} lines kept"

    def yahoo_bse():
        price, when = gsr.yahoo.last(http, "SML250.BO")
        return price is not None, f"{when} {price}"

    check("AMFI report (raw stream)", OPTIONAL, "portal.amfiindia.com", amfi_raw)
    check("Yahoo SML250.BO", OPTIONAL, "query1.finance.yahoo.com", yahoo_bse)

    # --- report -------------------------------------------------------------------------
    width = max(len(r[0]) for r in rows)
    print(f"\n{'SOURCE':<{width}}  {'TIER':<9} {'':<3} {'HOST':<30} DETAIL")
    print("-" * (width + 78))
    failed_critical: list[str] = []
    for name, tier, host, ok, detail in rows:
        mark = "OK " if ok else "DEAD"
        print(f"{name:<{width}}  {tier:<9} {mark:<3} {host:<30} {detail}")
        if not ok and tier == CRITICAL:
            failed_critical.append(name)

    print()
    ok_count = sum(1 for r in rows if r[3])
    print(f"{ok_count}/{len(rows)} sources reachable")
    if failed_critical:
        print(f"CRITICAL FAILURES: {', '.join(failed_critical)}")
        return 1
    optional_dead = [r[0] for r in rows if not r[3]]
    if optional_dead:
        print(f"optional unavailable (digest degrades, does not break): {', '.join(optional_dead)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

from bot.http import Http                                   # noqa: E402
from bot.sources import bse, gold, gsr, nse, russell_tech    # noqa: E402
from bot.sources.amfi import HISTORY_URL                     # noqa: E402
from bot.util import fmt_ddmmmyyyy, ist_today                # noqa: E402

CRITICAL, OPTIONAL = "CRITICAL", "optional"


def main() -> int:
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

    # --- gold -------------------------------------------------------------------------
    def bhavcopy():
        when, fields = gold._bhavcopy_close(http, recent)
        return bool(fields), f"{when} close={fields.get('level')}" if fields else "no row"

    def bse_quote():
        quote = gold._quote(http)
        if quote is None:
            return False, "blocked or wrong instrument"
        return quote.price is not None, f"ltp={quote.price} iNAV={quote.inav} ason={quote.price_date}"

    def nse_etf():
        price = gold._nse_price(http)
        return price is not None, f"ltP={price}"

    def amfi_gold():
        value, when = __import__("bot.sources.amfi", fromlist=["newest_nav"]).newest_nav(
            http, gold.GOLD_BEES, today
        )
        return value is not None, f"{when} nav={value}"

    def amfi_russell():
        value, when = __import__("bot.sources.amfi", fromlist=["newest_nav"]).newest_nav(
            http, russell_tech.EDELWEISS_US_TECH, today
        )
        return value is not None, f"{when} nav={value}"

    check("BSE bhavcopy CSV", CRITICAL, "www.bseindia.com", bhavcopy)
    # This is THE open question: it is the only iNAV route that exists, and it is Akamai-fronted
    # behind a UA+Referer gate, so its behaviour from a datacenter IP cannot be predicted.
    check("BSE quote (iNAV source)", OPTIONAL, "api.bseindia.com", bse_quote)
    check("NSE ETF list (price only)", OPTIONAL, "www.nseindia.com", nse_etf)
    check("AMFI NAV — Gold BeES", CRITICAL, "portal.amfiindia.com", amfi_gold)
    check("AMFI NAV — Edelweiss FoF", CRITICAL, "portal.amfiindia.com", amfi_russell)

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

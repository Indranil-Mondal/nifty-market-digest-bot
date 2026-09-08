"""Shared machinery for a BSE-listed commodity ETF.

Gold BeES and the Zerodha Silver ETF are the same instrument shape wearing different tickers:
an India-domiciled, exchange-listed fund whose official NAV comes from AMFI once a day, whose
traded price comes from the BSE bhavcopy and quote endpoint, and whose iNAV is only reachable
through one specific header combination. Everything in this module is that shared shape.

It exists because the alternative was copying about a hundred and fifty lines of subtle
parsing -- a two-digit-year timestamp, an ISIN-not-ticker row match, a 200-that-is-really-HTML
guard, a live-versus-close decision taken from the feed's own clock -- into a second file. Two
copies of that drift apart, and the copy nobody is looking at is the one that rots. What stays
in the per-metal modules is only what genuinely differs: identity, and the physical cross-check.

The hazards guarded here were all measured, not imagined:
  * BSE scrip codes are adjacent and unvalidated: 590095 is Gold BeES, 590096 is Liquid BeES.
    Every response is checked against a name fragment before it is believed.
  * api.bseindia.com answers 403 to a default requests User-Agent, and answers 200 with an HTML
    marketing page if you send a UA but no Referer. Only UA+Referer yields JSON.
  * Outside market hours the quote endpoint keeps serving the last close. Calling that "live"
    would be a lie repeated every morning, so the decision is taken from Header.Ason.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
from dataclasses import dataclass
from typing import Optional

from ..compute import FetchResult
from ..http import Http
from ..model import FRESHNESS_LIVE, FRESHNESS_PREV_CLOSE, FRESHNESS_T1
from ..state import Series
from ..util import MONTHS_ABBR, parse_float
from . import amfi

log = logging.getLogger(__name__)

BHAVCOPY_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{stamp}_F_0000.CSV"
)
QUOTE_URL = "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"
IBJA_URL = "https://www.ibjarates.com/"

# NSE's ETF list is a secondary price source. It genuinely carries these symbols with a
# last-traded price, which is worth having as a second opinion -- but note two things:
#   * There is NO iNAV field here. NSE's only iNAV-bearing endpoint is /api/quote-equity, which
#     returns 403 even after a cookie warm-up (the homepage itself 403s), so iNAV cannot come
#     from NSE at all.
#   * Its `nav` field is STALE -- it reported 124.8671 (the 14 Aug NAV) for GOLDBEES while the
#     official 17 Aug NAV was 126.3681. It is deliberately never read; using it for
#     premium/discount would silently understate the discount by whole sessions.
# The host is Akamai-fronted, so this is a fallback and never the primary.
NSE_ETF_URL = "https://www.nseindia.com/api/etf"

TRADING_DAY_WALKBACK = 8


@dataclass(frozen=True)
class ListedEtf:
    """One exchange-listed fund, identified redundantly at every source."""

    amfi_scheme: amfi.AmfiScheme
    bse_scrip: str
    nse_symbol: str
    name_fragment: str            # must appear in the BSE quote's company name, upper-cased
    cold_start_days: int = 430
    warm_overlap_days: int = 15
    min_healthy_points: int = 180

    @property
    def isin(self) -> str:
        return self.amfi_scheme.isin


@dataclass(frozen=True)
class IbjaMetal:
    """The IBJA physical-rate cross-check for one metal.

    IBJA quotes gold per 10 grams and silver per kilogram. Getting that unit wrong would not
    look wrong -- it would produce a grams-per-unit figure off by a factor of a hundred -- so
    the unit is carried explicitly alongside the element ids rather than assumed.
    """

    element: str                  # the ASP.NET label id, minus the _AM / _PM suffix
    label: str                    # how it reads in the digest, e.g. "IBJA 999 silver"
    grams_per_quote: float        # 10.0 for a per-10g quote, 1000.0 for a per-kg quote
    unit_suffix: str              # "10g" or "kg"
    plausible: tuple[float, float]


def _ibja_pattern(element: str, session: str) -> re.Pattern:
    return re.compile(rf'id="{element}_{session}"[^>]*>\s*([\d,.]+)', re.IGNORECASE)


def ibja_rate(http: Http, metal: IbjaMetal) -> Optional[float]:
    """IBJA 999 rate for one metal, in rupees per its own quote unit.

    A cross-check, never a headline number. A value outside the plausible band means the page
    layout moved and we scraped something else, so it is discarded rather than printed.
    """
    html = http.get(IBJA_URL, timeout=30)
    if not html:
        return None
    for session in ("PM", "AM"):        # the PM fixing is the later of the two
        match = _ibja_pattern(metal.element, session).search(html)
        if not match:
            continue
        value = parse_float(match.group(1))
        if value is not None and metal.plausible[0] <= value <= metal.plausible[1]:
            return value
        log.warning(
            "IBJA %s value %r outside the plausible band; layout may have changed",
            metal.element, match.group(1),
        )
    return None


def find_isin_row(csv_text: str, isin: str) -> Optional[dict[str, str]]:
    """Locate our row by ISIN. Matching on the ticker would be looser and less safe.

    Matching on the NAME would be worse than loose, it would be wrong: the bhavcopy's
    FinInstrmNm for both Zerodha ETFs is the literal string "Zerodha Mutual Fund", so GOLDCASE
    and SILVERCASE are indistinguishable by name in that file. Only the ISIN separates them.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("ISIN", "").upper() == isin.upper():
            return row
    return None


def bhavcopy_close(http: Http, isin: str, when: dt.date) -> tuple[Optional[dt.date], dict[str, float]]:
    """Closing price for `when`, walking back over weekends and holidays."""
    for offset in range(TRADING_DAY_WALKBACK + 1):
        probe = when - dt.timedelta(days=offset)
        text = http.get(BHAVCOPY_URL.format(stamp=probe.strftime("%Y%m%d")), timeout=60)
        # A short body or a missing header column means a non-trading day or an error page.
        if not text or len(text) < 10_000 or "TckrSymb" not in text[:2000]:
            continue
        row = find_isin_row(text, isin)
        if row is None:
            continue
        fields: dict[str, float] = {}
        close = parse_float(row.get("ClsPric"))
        previous = parse_float(row.get("PrvsClsgPric"))
        if close is not None:
            fields["level"] = close
        if previous is not None:
            fields["prev_close"] = previous
        if fields:
            return probe, fields
    return None, {}


def parse_bse_stamp(raw: object) -> Optional[dt.date]:
    """'17 Aug 26 | 16:00' -> date(2026, 8, 17).

    Two-digit year, so it is pinned to the 2000s -- fine for a bot reporting on recent sessions.
    """
    head = str(raw or "").split("|")[0].strip()
    parts = head.replace("-", " ").split()
    if len(parts) < 3:
        return None
    month = None
    for index, name in enumerate(MONTHS_ABBR):
        if parts[1].lower()[:3] == name.lower():
            month = index + 1
            break
    if month is None:
        return None
    try:
        year = int(parts[2])
        return dt.date(year + 2000 if year < 100 else year, month, int(parts[0]))
    except ValueError:
        return None


class Quote:
    """What the BSE scrip-header endpoint tells us, with provenance attached."""

    __slots__ = ("price", "price_date", "inav", "inav_date", "prev_close")

    def __init__(self) -> None:
        self.price: Optional[float] = None
        self.price_date: Optional[dt.date] = None
        self.inav: Optional[float] = None
        self.inav_date: Optional[dt.date] = None
        self.prev_close: Optional[float] = None


def quote(http: Http, etf: ListedEtf) -> Optional[Quote]:
    """Last-traded price, and the closest thing to an iNAV that is reachable unattended.

    HEADERS: a non-default User-Agent AND a Referer are BOTH required. Established by
    isolation, because this is easy to get wrong:

        (no headers)   -> 403
        UA only        -> 200, but an HTML marketing page
        Referer only   -> 403          <- with requests' default UA
        Origin only    -> 403
        UA + Origin    -> 200, HTML
        UA + Referer   -> 200, JSON with NAVRate    <- the only combination that works

    Do not "simplify" this by dropping the User-Agent: bseindia.com rejects the literal
    `python-requests/x.y` default, so a Referer-only request fails even though curl (which sends
    its own UA) makes Referer look sufficient. The UA arrives from Http's BASE_HEADERS.

    Three silent failures are guarded: an HTML body at 200, a valid JSON envelope carrying
    LTP "-" for an unknown scrip, and a stale close being mistaken for an intraday tick
    (`Header.Ason` carries the real timestamp, so we do not have to guess from the clock).

    `Header.NAVRate` is the interesting field. It tracks the AMC's own realtime NAV rather than
    the T-1 official NAV, so it is effectively the iNAV, obtained from a host that does not
    block datacenter IPs the way the AMCs' own endpoints do.
    """
    payload = http.get(
        QUOTE_URL,
        params={"Debtflag": "", "scripcode": etf.bse_scrip, "seriesid": ""},
        headers={"Referer": "https://www.bseindia.com/"},
        expect="json",
        timeout=30,
    )
    if not isinstance(payload, dict):
        return None

    company = payload.get("Cmpname") if isinstance(payload.get("Cmpname"), dict) else {}
    name = str((company or {}).get("FullN") or "")
    if etf.name_fragment.upper() not in name.upper():
        log.warning(
            "BSE quote for scrip %s returned an unexpected instrument: %r", etf.bse_scrip, name
        )
        return None

    result = Quote()
    header = payload.get("Header") if isinstance(payload.get("Header"), dict) else {}
    rate = payload.get("CurrRate") if isinstance(payload.get("CurrRate"), dict) else {}

    result.price = parse_float((rate or {}).get("LTP")) or parse_float((header or {}).get("LTP"))
    result.price_date = parse_bse_stamp((header or {}).get("Ason"))
    result.prev_close = parse_float((header or {}).get("PrevClose"))
    result.inav = parse_float((header or {}).get("NAVRate"))
    result.inav_date = parse_bse_stamp((header or {}).get("NAVdttm"))
    return result


def nse_price(http: Http, symbol: str) -> Optional[float]:
    """Last traded price from NSE's ETF list. Price only -- see NSE_ETF_URL notes."""
    payload = http.get(NSE_ETF_URL, expect="json", timeout=45)
    if not isinstance(payload, dict):
        return None
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")).strip().upper() != symbol.upper():
            continue
        return parse_float(row.get("ltP"))
    return None


def premium_str(value: float) -> str:
    return f"{value:+.2f}%"


def fetch_prices(http: Http, series: Series, etf: ListedEtf, today: dt.date) -> FetchResult:
    """NAV, traded price, iNAV and the premium note -- everything that is metal-agnostic.

    The caller adds only the physical cross-check for its own metal.
    """
    result = FetchResult()

    # --- NAV, and with it every lookback -------------------------------------------------
    _, newest = series.latest("nav")
    if newest is None or len(series.dates_with("nav")) < etf.min_healthy_points:
        start = today - dt.timedelta(days=etf.cold_start_days)
    else:
        start = newest - dt.timedelta(days=etf.warm_overlap_days)

    history, errors = amfi.nav_history(http, etf.amfi_scheme, start, today)
    result.history.update(history)
    result.errors.extend(errors)
    for when, fields in history.items():
        series.upsert(when, fields)

    nav_value, nav_date = series.latest("nav")
    if nav_value is not None:
        # An India-domiciled commodity ETF strikes NAV after the domestic close, so at 11:11 the
        # newest official NAV is the previous trading day's.
        result.add("nav", nav_value, as_of=nav_date, freshness=FRESHNESS_T1, source="AMFI")

    # --- price ---------------------------------------------------------------------------
    close_date, close_fields = bhavcopy_close(http, etf.isin, today - dt.timedelta(days=1))
    if close_date is not None and "level" in close_fields:
        # Keep the exchange's own previous close alongside the close. It costs nothing -- the
        # bhavcopy row already carries it -- and it is what lets the digest state the price's
        # OWN day move on day one, before this instrument has built up any price history of its
        # own. Without it a newly added ETF shows its NAV's move next to its price.
        stored = {"level": close_fields["level"]}
        if "prev_close" in close_fields:
            stored["prev_close"] = close_fields["prev_close"]
        series.upsert(close_date, stored)
        result.history.setdefault(close_date, {}).update(stored)
    else:
        result.errors.append("BSE bhavcopy close unavailable")

    live = quote(http, etf)
    price_for_premium: Optional[float] = None
    inav_value: Optional[float] = None

    if live is not None and live.price is not None:
        # Trust the endpoint's own timestamp over the wall clock: outside market hours it keeps
        # serving the last close, and calling that "live" would be a lie repeated every morning.
        is_live = live.price_date is None or live.price_date >= today
        result.add(
            "level",
            live.price,
            as_of=live.price_date or today,
            freshness=FRESHNESS_LIVE if is_live else FRESHNESS_PREV_CLOSE,
            source="BSE quote",
        )
        price_for_premium = live.price
        if not is_live and live.price_date is not None:
            result.history.setdefault(live.price_date, {}).setdefault("level", live.price)
            if live.prev_close is not None:
                result.history[live.price_date].setdefault("prev_close", live.prev_close)
                series.upsert(live.price_date, {"prev_close": live.prev_close})
    else:
        # BSE's quote endpoint is Referer-gated. NSE's ETF list is an independent second opinion
        # on price (though never on NAV or iNAV).
        fallback = nse_price(http, etf.nse_symbol)
        if fallback is not None:
            result.add("level", fallback, as_of=today, freshness=FRESHNESS_LIVE, source="NSE ETF list")
            price_for_premium = fallback
        else:
            stored, stored_date = series.latest("level")
            if stored is not None:
                result.add(
                    "level", stored, as_of=stored_date,
                    freshness=FRESHNESS_PREV_CLOSE, source="BSE bhavcopy",
                )
            price_for_premium = stored

    # --- iNAV -----------------------------------------------------------------------------
    if live is not None and live.inav is not None:
        inav_value = live.inav
        inav_live = live.inav_date is None or live.inav_date >= today
        result.add(
            "inav",
            inav_value,
            as_of=live.inav_date or today,
            freshness=FRESHNESS_LIVE if inav_live else FRESHNESS_PREV_CLOSE,
            source="BSE (AMC realtime NAV)",
        )

    # --- premium / discount ----------------------------------------------------------------
    # Against iNAV where we have it, since that is the comparison that actually matters for an
    # ETF. Against the official T-1 NAV otherwise, labelled so the two are never confused.
    if price_for_premium is not None:
        if inav_value:
            result.notes.append(
                f"price {premium_str((price_for_premium - inav_value) / inav_value * 100.0)} vs iNAV"
            )
        elif nav_value:
            nav_label = f"{nav_date:%d %b}" if nav_date else "latest"
            result.notes.append(
                f"price {premium_str((price_for_premium - nav_value) / nav_value * 100.0)} "
                f"vs NAV ({nav_label}); live iNAV unavailable"
            )

    return result

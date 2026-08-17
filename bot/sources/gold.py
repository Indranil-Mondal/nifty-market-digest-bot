"""Nippon India ETF Gold BeES.

Identity, each confirmed against a primary source:
    AMFI scheme code  140088
    ISIN              INF204KB17I5      (INF, not INE -- it is a mutual-fund ISIN)
    NSE symbol        GOLDBEES
    BSE scrip code    590095

EVERY fetch here validates the ISIN or the fund name, never the numeric code alone. That is not
defensive habit, it is a measured hazard: AMFI scheme code 140089 returns "Nippon India ETF
Nifty PSU Bank BeES" and BSE scrip 590096 returns "Nippon India ETF Liquid BeES" -- both at
HTTP 200, both silently the wrong fund.

Three things the user asked for do not exist for this instrument, and are reported as dashes
rather than approximated:

  * PE / PB / dividend yield -- gold is a physical commodity with no earnings. There is no
    numerator to find; no source is withholding it.
  * iNAV -- the only true source is Nippon's own realtime endpoint, which sits behind
    Cloudflare Bot Management and returns 403 to US datacenter IPs. Since the bot runs on a
    GitHub Actions runner, iNAV is treated as unavailable and the digest instead shows the
    premium/discount of market price to the official NAV, explicitly labelled as such.
  * "Domestic Price of Gold Index" -- this is not a published index anywhere at any price. The
    scheme document defines it as a formula the AMC computes internally, licensing no index
    provider. The AMFI NAV series *is* the domestic gold price net of the expense ratio, and
    IBJA's 999 rate is the closest genuinely published domestic physical gold price, so that is
    carried as a cross-check.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
from typing import Optional

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..model import FRESHNESS_LIVE, FRESHNESS_PREV_CLOSE, FRESHNESS_T1
from ..state import Series
from ..util import MONTHS_ABBR, parse_float
from . import amfi


def premium_str(value: float) -> str:
    return f"{value:+.2f}%"

log = logging.getLogger(__name__)

ISIN = "INF204KB17I5"
BSE_SCRIP = "590095"
NAME_FRAGMENT = "GOLD BEES"

# mf=21 is Nippon India Mutual Fund, established empirically rather than guessed: that response's
# AMC header reads "Nippon India Mutual Fund" and contains scheme 140088, while mf=53 returns
# Axis Mutual Fund data with zero occurrences of it.
GOLD_BEES = amfi.AmfiScheme(
    scheme_code="140088",
    isin=ISIN,
    mf_code="21",
    label="Nippon India ETF Gold BeES",
)

BSE_BHAVCOPY_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{stamp}_F_0000.CSV"
)
BSE_QUOTE_URL = "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"
IBJA_URL = "https://www.ibjarates.com/"

# NSE's ETF list is a secondary price source. It genuinely carries GOLDBEES with a last-traded
# price, which is worth having as a second opinion -- but note two things:
#   * There is NO iNAV field here. NSE's only iNAV-bearing endpoint is /api/quote-equity, which
#     returns 403 even after a cookie warm-up (the homepage itself 403s), so iNAV cannot come
#     from NSE at all.
#   * Its `nav` field is STALE -- it reported 124.8671 (the 14 Aug NAV) while the official 17 Aug
#     NAV was 126.3681. It is deliberately not read here; using it for premium/discount would
#     silently understate the discount by whole sessions.
# The host is Akamai-fronted, so this is a fallback and never the primary.
NSE_ETF_URL = "https://www.nseindia.com/api/etf"
NSE_SYMBOL = "GOLDBEES"

COLD_START_DAYS = 430
WARM_OVERLAP_DAYS = 15
MIN_HEALTHY_POINTS = 180
# The AMFI report is ~15MB for a year. Request it in windows so no single response is huge.
CHUNK_DAYS = 100
TRADING_DAY_WALKBACK = 8

# IBJA publishes INR per 10 grams. Anything outside this band means the page layout moved and we
# scraped a different number.
IBJA_PLAUSIBLE_RANGE = (50_000.0, 500_000.0)
_IBJA_AM = re.compile(r'id="lblGold999_AM"[^>]*>\s*([\d,.]+)', re.IGNORECASE)
_IBJA_PM = re.compile(r'id="lblGold999_PM"[^>]*>\s*([\d,.]+)', re.IGNORECASE)


def _bhavcopy_close(http: Http, when: dt.date) -> tuple[Optional[dt.date], dict[str, float]]:
    """Closing price for `when`, walking back over weekends and holidays."""
    for offset in range(TRADING_DAY_WALKBACK + 1):
        probe = when - dt.timedelta(days=offset)
        text = http.get(BSE_BHAVCOPY_URL.format(stamp=probe.strftime("%Y%m%d")), timeout=60)
        # A short body or a missing header column means a non-trading day or an error page.
        if not text or len(text) < 10_000 or "TckrSymb" not in text[:2000]:
            continue
        row = _find_isin_row(text)
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


def _find_isin_row(csv_text: str) -> Optional[dict[str, str]]:
    """Locate our row by ISIN. Matching on the ticker would be looser and less safe."""
    reader = csv.DictReader(io.StringIO(csv_text))
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("ISIN", "").upper() == ISIN:
            return row
    return None


def _parse_bse_stamp(raw: object) -> Optional[dt.date]:
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


def _quote(http: Http) -> Optional[Quote]:
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
    the T-1 official NAV -- 126.64 here against an official 126.3681 -- so it is effectively the
    iNAV, obtained from a host that does not block datacenter IPs the way the AMC's endpoint
    does. It is still best-effort: this endpoint is Referer-gated and therefore unproven from a
    US runner, and the digest degrades to a premium-versus-NAV figure without it.
    """
    payload = http.get(
        BSE_QUOTE_URL,
        params={"Debtflag": "", "scripcode": BSE_SCRIP, "seriesid": ""},
        headers={"Referer": "https://www.bseindia.com/"},
        expect="json",
        timeout=30,
    )
    if not isinstance(payload, dict):
        return None

    company = payload.get("Cmpname") if isinstance(payload.get("Cmpname"), dict) else {}
    name = str((company or {}).get("FullN") or "")
    if NAME_FRAGMENT not in name.upper():
        log.warning("BSE quote returned an unexpected instrument: %r", name)
        return None

    quote = Quote()
    header = payload.get("Header") if isinstance(payload.get("Header"), dict) else {}
    rate = payload.get("CurrRate") if isinstance(payload.get("CurrRate"), dict) else {}

    quote.price = parse_float((rate or {}).get("LTP")) or parse_float((header or {}).get("LTP"))
    quote.price_date = _parse_bse_stamp((header or {}).get("Ason"))
    quote.prev_close = parse_float((header or {}).get("PrevClose"))
    quote.inav = parse_float((header or {}).get("NAVRate"))
    quote.inav_date = _parse_bse_stamp((header or {}).get("NAVdttm"))
    return quote


def _nse_price(http: Http) -> Optional[float]:
    """Last traded price from NSE's ETF list. Price only -- see NSE_ETF_URL notes."""
    payload = http.get(NSE_ETF_URL, expect="json", timeout=45)
    if not isinstance(payload, dict):
        return None
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")).strip().upper() != NSE_SYMBOL:
            continue
        return parse_float(row.get("ltP"))
    return None


def _ibja_gold_rate(http: Http) -> Optional[float]:
    """IBJA 999 gold, INR per 10 grams. A cross-check, never a headline number."""
    html = http.get(IBJA_URL, timeout=30)
    if not html:
        return None
    for pattern in (_IBJA_PM, _IBJA_AM):        # PM fixing is the later of the two
        match = pattern.search(html)
        if not match:
            continue
        value = parse_float(match.group(1))
        if value is not None and IBJA_PLAUSIBLE_RANGE[0] <= value <= IBJA_PLAUSIBLE_RANGE[1]:
            return value
        log.warning("IBJA value %r outside the plausible band; layout may have changed", match.group(1))
    return None


def fetch(http: Http, series: Series, spec: InstrumentSpec, today: dt.date) -> FetchResult:
    result = FetchResult()

    # --- NAV, and with it every lookback -------------------------------------------------
    _, newest = series.latest("nav")
    if newest is None or len(series.dates_with("nav")) < MIN_HEALTHY_POINTS:
        start = today - dt.timedelta(days=COLD_START_DAYS)
    else:
        start = newest - dt.timedelta(days=WARM_OVERLAP_DAYS)

    history, errors = amfi.nav_history(http, GOLD_BEES, start, today)
    result.history.update(history)
    result.errors.extend(errors)
    for when, fields in history.items():
        series.upsert(when, fields)

    nav_value, nav_date = series.latest("nav")
    if nav_value is not None:
        # An India-domiciled gold ETF strikes NAV after the domestic close, so at 11:11 the
        # newest official NAV is the previous trading day's.
        result.add("nav", nav_value, as_of=nav_date, freshness=FRESHNESS_T1, source="AMFI")

    # --- price ---------------------------------------------------------------------------
    close_date, close_fields = _bhavcopy_close(http, today - dt.timedelta(days=1))
    if close_date is not None and "level" in close_fields:
        series.upsert(close_date, {"level": close_fields["level"]})
        result.history.setdefault(close_date, {})["level"] = close_fields["level"]
    else:
        result.errors.append("BSE bhavcopy close unavailable")

    quote = _quote(http)
    price_for_premium: Optional[float] = None
    inav_value: Optional[float] = None

    if quote is not None and quote.price is not None:
        # Trust the endpoint's own timestamp over the wall clock: outside market hours it keeps
        # serving the last close, and calling that "live" would be a lie repeated every morning.
        is_live = quote.price_date is None or quote.price_date >= today
        result.add(
            "level",
            quote.price,
            as_of=quote.price_date or today,
            freshness=FRESHNESS_LIVE if is_live else FRESHNESS_PREV_CLOSE,
            source="BSE quote",
        )
        price_for_premium = quote.price
        if not is_live and quote.price_date is not None:
            result.history.setdefault(quote.price_date, {}).setdefault("level", quote.price)
    else:
        # BSE's quote endpoint is Referer-gated and therefore unproven from a CI runner. NSE's
        # ETF list is an independent second opinion on price (though never on NAV or iNAV).
        nse_price = _nse_price(http)
        if nse_price is not None:
            result.add("level", nse_price, as_of=today, freshness=FRESHNESS_LIVE, source="NSE ETF list")
            price_for_premium = nse_price
        else:
            stored, stored_date = series.latest("level")
            if stored is not None:
                result.add("level", stored, as_of=stored_date, freshness=FRESHNESS_PREV_CLOSE, source="BSE bhavcopy")
            price_for_premium = stored

    # --- iNAV -----------------------------------------------------------------------------
    if quote is not None and quote.inav is not None:
        inav_value = quote.inav
        inav_live = quote.inav_date is None or quote.inav_date >= today
        result.add(
            "inav",
            inav_value,
            as_of=quote.inav_date or today,
            freshness=FRESHNESS_LIVE if inav_live else FRESHNESS_PREV_CLOSE,
            source="BSE (AMC realtime NAV)",
        )

    # --- premium / discount ----------------------------------------------------------------
    # Against iNAV where we have it, since that is the comparison that actually matters for an
    # ETF. Against the official T-1 NAV otherwise, labelled so the two are never confused.
    if price_for_premium is not None:
        if inav_value:
            premium = (price_for_premium - inav_value) / inav_value * 100.0
            result.notes.append(f"price {premium:+.2f}% vs iNAV")
        elif nav_value:
            nav_label = f"{nav_date:%d %b}" if nav_date else "latest"
            result.notes.append(
                f"price {premium_str((price_for_premium - nav_value) / nav_value * 100.0)} "
                f"vs NAV ({nav_label}); live iNAV unavailable"
            )

    # --- domestic physical gold cross-check ----------------------------------------------
    ibja = _ibja_gold_rate(http)
    if ibja is not None:
        result.notes.append(f"IBJA 999 gold ₹{ibja:,.0f}/10g")
        if nav_value:
            # Grams of gold backing one unit. A stable ratio; a jump of more than about a
            # percent day-over-day means one of the two sources has broken.
            grams = nav_value / (ibja / 10.0)
            log.info("gold backing per unit: %.6f g", grams)

    if not result.readings:
        result.errors.append("no gold data could be retrieved")
    return result

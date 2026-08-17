"""The gold:silver ratio — how many ounces of silver one ounce of gold buys.

Why it earns a place in this digest: it is the oldest relative-value measure in metals, and it
mean-reverts far more reliably than either metal's price. A high reading says silver is cheap
*relative to gold*, a low reading the reverse. It says nothing about whether either metal is
cheap in absolute terms.

Sourcing. Both legs come from COMEX futures via Yahoo — `GC=F` and `SI=F` — because they are
quoted in the same unit (USD per troy ounce), so the units cancel exactly and no conversion can
introduce error. They also return a full year of daily closes, which means the 1/2/3/4-week and
3/6/12-month lookbacks work from the very first run instead of being blank until the cache fills.

IBJA's domestic 999 gold and silver rates give a second, independent reading. The domestic and
international ratios differ — 65.4 against 67.7 when this was written — because Indian import
duty and GST are not identical on the two metals. Both are shown; neither is presented as the
"true" figure.

On interpretation: rather than assert fixed thresholds, this reports where the ratio sits within
its own trailing one-year range, which is derived from the data actually in hand. The long-run
reference bands are included as context only. This is a relative-value indicator, not advice.

Two caveats stated plainly:
  * These are futures, not spot, and the two contracts have different expiries, so the ratio
    carries a small basis difference against a spot-derived one. Immaterial for a mean-reversion
    signal, and it affects the level far more than the trend.
  * `GC=F` and `SI=F` are continuous front-month proxies, so the series contains roll steps.
    Both legs roll on a similar cadence, which largely cancels in a ratio.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

from ..compute import FetchResult, InstrumentSpec
from ..http import Http
from ..model import FRESHNESS_LIVE, FRESHNESS_PREV_CLOSE
from ..state import Series
from ..util import parse_float
from . import yahoo

log = logging.getLogger(__name__)

GOLD_SYMBOL = "GC=F"
SILVER_SYMBOL = "SI=F"

IBJA_URL = "https://www.ibjarates.com/"
# IBJA quotes 999 gold per 10 grams and 999 silver per kilogram. Getting these two divisors the
# wrong way round would produce a ratio out by a factor of 100, so they are named explicitly.
GOLD_GRAMS_PER_QUOTE = 10.0
SILVER_GRAMS_PER_QUOTE = 1000.0
_IBJA_PATTERN = r'id="lbl{metal}999_{session}"[^>]*>\s*([\d,.]+)'

# A ratio outside this band means one leg was misparsed or the units moved; the modern range has
# been roughly 30 to 125 across extremes.
PLAUSIBLE_RATIO = (10.0, 200.0)

# Long-run reference bands, context only.
REFERENCE_LOW = 55.0
REFERENCE_HIGH = 80.0


def _ibja_rate(http: Http, metal: str) -> Optional[float]:
    html = http.get(IBJA_URL, timeout=30)
    if not html:
        return None
    for session in ("PM", "AM"):        # PM fixing is the later of the two
        match = re.search(_IBJA_PATTERN.format(metal=metal, session=session), html, re.IGNORECASE)
        if match:
            value = parse_float(match.group(1))
            if value:
                return value
    return None


def ratio_from_ibja_quotes(gold_per_10g: float, silver_per_kg: float) -> Optional[float]:
    """Convert IBJA's two differently-scaled quotes into a ratio by weight.

    Kept as a pure function because the divisors are the one place an error would be silent and
    enormous: swapping them yields a ratio out by a factor of 10,000, and forgetting them
    entirely by 100. Both would still print as a number.
    """
    if gold_per_10g <= 0 or silver_per_kg <= 0:
        return None
    gold_per_gram = gold_per_10g / GOLD_GRAMS_PER_QUOTE
    silver_per_gram = silver_per_kg / SILVER_GRAMS_PER_QUOTE
    if silver_per_gram <= 0:
        return None
    ratio = gold_per_gram / silver_per_gram
    if not PLAUSIBLE_RATIO[0] <= ratio <= PLAUSIBLE_RATIO[1]:
        log.warning("domestic gold:silver ratio %.1f outside the plausible band; ignoring", ratio)
        return None
    return ratio


def _domestic_ratio(http: Http) -> Optional[float]:
    gold = _ibja_rate(http, "Gold")
    silver = _ibja_rate(http, "Silver")
    if not gold or not silver:
        return None
    return ratio_from_ibja_quotes(gold, silver)


def _position_in_range(current: float, history: list[float]) -> Optional[str]:
    """Where the current ratio sits within its own trailing one-year range.

    Preferred over fixed thresholds because it is computed from the data in hand rather than a
    number someone remembers from a different decade. `history` must already be trimmed to the
    trailing year, so the label and the arithmetic agree.
    """
    if len(history) < 30:
        return None
    low, high = min(history), max(history)
    if high <= low:
        return None
    percentile = sum(1 for value in history if value <= current) / len(history) * 100.0
    # Worded without an ordinal suffix on purpose: appending a literal "th" produces "1th",
    # "21th", "23th". Not worth an ordinal helper for one label.
    return f"percentile {percentile:.0f} of 1Y range {low:.1f}–{high:.1f}"


def _reading(ratio: float) -> str:
    """A factual descriptor of the relative-value implication. Not a recommendation."""
    if ratio >= REFERENCE_HIGH:
        return f"above {REFERENCE_HIGH:.0f} — silver historically cheap vs gold"
    if ratio <= REFERENCE_LOW:
        return f"below {REFERENCE_LOW:.0f} — gold historically cheap vs silver"
    return f"within the long-run {REFERENCE_LOW:.0f}–{REFERENCE_HIGH:.0f} band"


def fetch(http: Http, series_store: Series, spec: InstrumentSpec, today: dt.date) -> FetchResult:
    result = FetchResult()

    # Two years, not one: a "1y" range starts *about* twelve months back, so the 12-month
    # lookback anchor can fall a few days before the earliest close and resolve to nothing.
    # The extra year costs one small request and guarantees every lookback has a base.
    gold = yahoo.series(http, GOLD_SYMBOL, range_="2y")
    silver = yahoo.series(http, SILVER_SYMBOL, range_="2y")

    if not gold or not silver:
        missing = ", ".join(name for name, data in ((GOLD_SYMBOL, gold), (SILVER_SYMBOL, silver)) if not data)
        result.errors.append(f"metals series unavailable ({missing})")
        return result

    # Only dates where both legs traded; a one-legged ratio would be nonsense.
    for when in sorted(set(gold) & set(silver)):
        silver_close = silver[when]
        if silver_close <= 0:
            continue
        ratio = gold[when] / silver_close
        if PLAUSIBLE_RATIO[0] <= ratio <= PLAUSIBLE_RATIO[1]:
            result.history[when] = {"level": ratio}

    if not result.history:
        result.errors.append("no overlapping gold and silver closes")
        return result

    for when, fields in result.history.items():
        series_store.upsert(when, fields)

    newest = max(result.history)
    current = result.history[newest]["level"]
    is_live = newest >= today
    result.add(
        "level",
        current,
        as_of=newest,
        freshness=FRESHNESS_LIVE if is_live else FRESHNESS_PREV_CLOSE,
        source="COMEX futures via Yahoo",
    )

    # Percentile over the trailing YEAR only, even though two years were fetched, so that the
    # "1Y range" label describes exactly what was measured.
    year_ago = newest - dt.timedelta(days=365)
    trailing = [
        fields["level"] for when, fields in sorted(result.history.items()) if when >= year_ago
    ]
    # Position and interpretation are one note: they are two halves of the same statement, and
    # the renderer shows only the first few notes per instrument.
    parts = [p for p in (_position_in_range(current, trailing), _reading(current)) if p]
    if parts:
        result.notes.append(" · ".join(parts))

    domestic = _domestic_ratio(http)
    if domestic is not None:
        # The domestic gap reflects differing import duty and GST on the two metals, so it is
        # expected to persist rather than converge.
        result.notes.append(f"domestic (IBJA 999) {domestic:.1f}")

    return result

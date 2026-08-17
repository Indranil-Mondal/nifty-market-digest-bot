"""News and policy selection.

Feeds are RSS/Atom only, on purpose: no API key to expire, no free tier to be withdrawn, no
card on file. Roughly a hundred headlines are pulled each morning and scored by rules, not by
an LLM, so the bot has no per-run inference cost and no dependency on a vendor's free tier.

Scoring is the sum of:
  theme match   -- does the headline mention something that actually moves these holdings
  source weight -- an RBI or SEBI press release outranks a generic markets roundup
  recency       -- today beats yesterday, and anything older than the window is dropped
  penalties     -- listicles, "top 5 stocks to buy", horoscope-grade content

Only headlines clearing MIN_SCORE are shown, and at most one per near-duplicate title. A quiet
news day should produce a short section, not eight filler links.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional, Sequence

from .http import Http
from .model import NewsItem
from .util import IST

log = logging.getLogger(__name__)

MAX_AGE_HOURS = 30          # a morning digest should not resurface the day before yesterday
MIN_SCORE = 3.0
MAX_ITEMS = 8
MAX_PER_SOURCE = 2          # stop one prolific feed from crowding out the rest


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    weight: float = 1.0
    tag_hint: Optional[str] = None      # the subject this feed is about, if it has just one
    # True only when the WHOLE feed is about `tag_hint`, so membership alone establishes
    # relevance -- a dedicated gold section may run "Prices ease as dollar firms" with no
    # keyword at all. It must stay False for broad official feeds like RBI's and SEBI's, which
    # are mostly housekeeping: there, the hint may label an item but must not qualify it.
    topical: bool = False
    # How stale this feed's newest item may be before we call the feed itself broken. This has to
    # be per-feed: a market wire that goes four days silent is dead, while the US Fed's monetary
    # feed legitimately goes six to eight weeks between FOMC meetings, and a uniform rule would
    # either miss the former or cry wolf forever about the latter.
    max_age_days: int = 4


# Theme keyword sets. Weights are additive, so a headline touching two themes outranks one.
# Written as word-boundary regexes to avoid "gold" matching "Goldman" or "golden".
#
# Note on structure: each theme is ONE combined regex and its weight is added ONCE per theme,
# never once per matching alternative. Summing per-pattern would double-count redundant aliases
# -- "small ?cap fund" and "small[- ]cap fund" both match "Small Cap Fund" -- and inflate pure
# filler above the threshold.
#
# Note on policy: the regulator's NAME is a much weaker signal than the LEVER it is pulling.
# "Sebi chairman says cyber defence must move to boardroom priority" and an actual stress-testing
# circular both contain "sebi", but only one moves a smallcap holding. So `policy_actor` (a bare
# mention) is scored well below `policy_lever` (a rate, duty, tax or disclosure rule).
THEMES: dict[str, tuple[float, tuple[str, ...]]] = {
    "smallcap": (
        3.0,
        (
            r"small[\s-]?cap", r"smallcap", r"nifty smallcap", r"bse smallcap",
            r"broader market", r"small.{0,12}mid[\s-]?cap",
        ),
    ),
    "midcap": (
        3.0,
        (r"mid[\s-]?cap", r"midcap", r"nifty midcap"),
    ),
    "largecap": (
        2.0,
        (r"nifty 50", r"nifty50", r"sensex", r"large[\s-]?cap", r"nifty next 50", r"benchmark index"),
    ),
    "gold": (
        3.0,
        (
            r"\bgold\b", r"bullion", r"gold etf", r"sovereign gold bond", r"\bsgb\b",
            r"import duty.{0,20}gold", r"gold price", r"\bibja\b", r"\blbma\b",
        ),
    ),
    "ustech": (
        3.0,
        (
            r"nasdaq", r"\bus tech\b", r"technology stocks", r"semiconductor", r"\bai\s+(?:capex|spend)",
            r"\bfed\b", r"federal reserve", r"\bfomc\b", r"us rate", r"treasury yield",
            r"\bnvidia\b", r"\bmicrosoft\b", r"magnificent seven",
        ),
    ),
    # The actual lever: a rate, a duty, a tax, a disclosure rule. This is what moves holdings.
    "policy_lever": (
        4.5,
        (
            r"repo rate", r"monetary policy", r"\bmpc\b", r"\bcrr\b", r"\bslr\b",
            r"policy (?:rate|stance)", r"rate (?:cut|hike|decision)",
            r"union budget", r"customs duty", r"import duty", r"\bgst\b", r"income tax",
            r"capital gains", r"\bstt\b", r"securities transaction tax",
            r"expense ratio", r"stress test", r"disclosure norm", r"\bcircular\b",
            r"categoris|categoriz", r"riskometer", r"total expense",
        ),
    ),
    # A bare regulator mention. Deliberately weak: it must combine with something else to clear
    # the threshold on its own.
    "policy_actor": (
        1.5,
        (r"\brbi\b", r"reserve bank", r"\bsebi\b", r"\bamfi\b", r"\bfinance ministry\b", r"\bcbdt\b"),
    ),
    # Genuine fund-flow signal.
    "flows": (
        3.0,
        (
            r"\bfii\b", r"\bfpi\b", r"\bdii\b", r"foreign (?:institutional|portfolio)",
            r"\bsip\b", r"mutual fund (?:inflow|outflow|flows)", r"redemption",
            r"net (?:inflow|outflow)", r"\binflows?\b", r"\boutflows?\b",
        ),
    ),
    # Weak flow-adjacent vocabulary that alone means little -- a bare "AUM" match once promoted a
    # REIT story into the digest.
    "flows_weak": (
        0.5,
        (r"\baum\b", r"assets under management", r"\bfolio", r"\bnfo\b"),
    ),
    "valuation": (
        2.0,
        (
            r"\bvaluation", r"\bpe ratio\b", r"price[\s-]to[\s-]earnings", r"overvalu", r"frothy",
            r"bubble", r"correction", r"earnings season", r"\bq[1-4]\s+(?:result|earning)",
        ),
    ),
}

# Content that is technically on-topic but has no informational value for this digest.
# Several of these were added after running the ruleset over real feed output: the
# city-by-city "gold rate today" pages and the "N stocks hit 52-week high" screeners are
# high-volume, keyword-rich and completely uninformative, so without these penalties they
# dominate the gold and smallcap themes respectively.
PENALTIES: tuple[tuple[float, str], ...] = (
    (-9.0, r"\brate today\b|\bgold rate\b|\bsilver rate\b|\b(?:22|24)\s*(?:ct|carat)\b"),
    (-9.0, r"\bamong \d+\b|\b\d+ (?:stocks?|shares?) (?:to|that|hit|which)\b"),
    (-8.0, r"52[\s-]?week (?:high|low)"),
    (-6.0, r"stocks? to (?:buy|watch|pick|add|accumulate)"),
    (-6.0, r"\btop \d+\b|\bbest \d+\b"),
    (-6.0, r"\b(?:multibagger|jackpot|rocket|skyrocket|zoom(?:s|ed)?)\b"),
    (-5.0, r"technical (?:view|pick|call)|\bbuy or sell\b|\btarget price\b"),
    (-5.0, r"\bhoroscope\b|\bastro\b|\bnumerolog"),
    (-5.0, r"petrol|diesel price|\bfuel price\b"),
    (-4.0, r"\bwebinar\b|\bsponsored\b|\bpartnered content\b|\badvertorial\b"),
    (-3.0, r"^\s*(?:live|market live|closing bell|opening bell)\b.{0,25}$"),
    (-3.0, r"\bmuhurat\b|\bipo allotment status\b|\bgmp\b|\bsubscription status\b"),
    # A single company joining or leaving an index is not news about the index. Without this,
    # one such story arrives via six syndicated feeds and crowds out everything else -- and it
    # matches the 'gold' theme purely because the company is named "Sky Gold".
    (-7.0, r"\b(?:added to|added in|included in|inducted into|enters|joins|part of|removed from|excluded from|dropped from|exits)\b.{0,40}\bindex\b"),
    (-4.0, r"\bhits?\b.{0,14}\b(?:new high|new low|record high|record low|upper circuit|lower circuit)\b"),
)

# Routine regulator housekeeping. RBI and SEBI publish several of these EVERY day -- liquidity
# auctions, money-market statistics, recovery orders, single-bank enforcement -- and none of it
# moves a smallcap, midcap or gold holding. They are penalised hard rather than filtered by
# source, so that a genuinely important release from the same feed (a repo-rate decision, a
# smallcap disclosure norm) still ranks at the top.
ROUTINE_NOISE: tuple[tuple[float, str], ...] = (
    (-12.0, r"\bvrrr?\b|variable rate (?:reverse )?repo"),
    (-12.0, r"money market operations as on"),
    (-12.0, r"auction of (?:government of india|state government)|conversion/?switch auction"),
    (-12.0, r"treasury bills?|\bt-bills?\b|\bsdl\b auction|dated securities"),
    (-12.0, r"^result(?:s)?(?::| of) .{0,50}auction"),
    (-12.0, r"scheduled banks.{0,8}statement|reserve money|monetary aggregates|weekly statistical"),
    (-12.0, r"general remittance order|recovery certificate|attachment order"),
    (-10.0, r"imposes monetary penalty|monetary penalty on|penalty on .{0,40}(?:bank|nbfc)\b"),
    (-10.0, r"directions under section 35a|cancellation of (?:licence|license|certificate of registration)"),
    (-10.0, r"unclaimed deposit|depositor education|handbook of statistics|annual report \d{4}"),
    (-8.0, r"\bco-?operative bank\b"),
    # Crypto rides on the same Fed/rate vocabulary as US tech and scored top of the list once,
    # despite being irrelevant to every holding here.
    (-10.0, r"\bbitcoin\b|\bcrypto|\bethereum\b|\bbtc\b|\bstablecoin\b|\bweb3\b"),
    # Personal-finance explainers match the tax and policy vocabulary without carrying any market
    # information: "Freelancers earning income from US IT companies: Key FAQs answered on tax".
    (-9.0, r"\bfaqs?\b|how to (?:file|claim|save|check|apply)|income tax return|\bitr\b|"
           r"\bpan card\b|\baadhaar\b|\bkyc\b|step-by-step|here'?s how"),
)

PENALTIES = PENALTIES + ROUTINE_NOISE

# Several internal themes are tiers of one subject and must not surface as separate labels, nor
# be counted separately when capping how much of the digest one subject may occupy.
TAG_DISPLAY = {
    "policy_lever": "policy",
    "policy_actor": "policy",
    "flows_weak": "flows",
    "largecap": "largecap",
}

# At most this many items per subject, so a busy US-tech day cannot crowd out policy entirely.
MAX_PER_THEME = 3

# Themes too weak to qualify an item on their own. They add their (small) weight when a strong
# theme is also present, but a headline whose ONLY match is one of these is off-topic: a bare
# regulator name, or a stray "AUM", tells us nothing about whether the item moves a holding.
WEAK_THEMES = frozenset({"policy_actor", "flows_weak"})

_THEME_RE = {
    name: re.compile("|".join(patterns), re.IGNORECASE)
    for name, (_w, patterns) in THEMES.items()
}
_PENALTY_RE = tuple((weight, re.compile(pattern, re.IGNORECASE)) for weight, pattern in PENALTIES)
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", text or "")).strip()


# Formats seen in the wild that strict RFC-822 parsing rejects. SEBI publishes
# '17 Aug, 2026 +0530' -- note the comma after the month -- which parsedate_to_datetime cannot
# read, and a regulator feed is too important to lose to a punctuation quirk.
_LENIENT_DATE_FORMATS = (
    "%d %b, %Y %z",
    "%d %b, %Y",
    "%d %b %Y %z",
    "%d %b %Y",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


def _parse_when(raw: Optional[str]) -> Optional[dt.datetime]:
    if not raw:
        return None
    raw = raw.strip()
    parsed: Optional[dt.datetime] = None

    try:
        parsed = parsedate_to_datetime(raw)            # RSS: RFC 822
    except (TypeError, ValueError, IndexError):
        parsed = None

    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))   # Atom: ISO 8601
        except ValueError:
            parsed = None

    if parsed is None:
        for fmt in _LENIENT_DATE_FORMATS:
            try:
                parsed = dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # An undated-but-parseable local timestamp is far more likely to be IST than UTC given
        # every feed here is Indian; assuming UTC would make items look 5.5 hours older.
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def parse_feed(payload: bytes | str, feed: Feed) -> list[NewsItem]:
    """Parse RSS 2.0 or Atom with the standard library.

    feedparser would be friendlier, but it is an extra dependency on a bot whose whole point is
    that nothing needs maintaining. RSS and Atom are simple enough to handle directly, and a
    malformed feed must degrade to zero items rather than raise.

    Parse from BYTES, never from str. RBI and PIB emit a UTF-8 BOM ahead of the XML declaration;
    handing that to ExpatParser as a decoded string raises "not well-formed, line 1, column 1"
    even after stripping the BOM character, because the declaration then contradicts the input
    type. Passing raw bytes lets the parser honour the declaration and both feeds parse fine.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="replace")
    payload = payload.strip().lstrip(b"\xef\xbb\xbf")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        log.warning("feed %s is not parseable XML: %s", feed.name, exc)
        return []

    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(".//item") or root.findall(f".//{atom}entry")
    items: list[NewsItem] = []
    for entry in entries:
        title = _clean(_text(entry, "title", atom))
        if not title:
            continue
        link = _text(entry, "link", atom)
        if not link:
            anchor = entry.find(f"{atom}link")
            if anchor is not None:
                link = anchor.attrib.get("href", "")
        when = _parse_when(
            _text(entry, "pubDate", atom)
            or _text(entry, "published", atom)
            or _text(entry, "updated", atom)
            or _text(entry, "{http://purl.org/dc/elements/1.1/}date", atom)
        )
        items.append(
            NewsItem(title=title, url=(link or "").strip() or None, source=feed.name, published=when)
        )
    return items


def _text(entry: ET.Element, tag: str, atom: str) -> str:
    for candidate in (tag, f"{atom}{tag}"):
        found = entry.find(candidate)
        if found is not None and (found.text or "").strip():
            return found.text or ""
    return ""


def score(item: NewsItem, feed: Feed, now: dt.datetime) -> float:
    """Rule-based relevance. Also populates item.tags as a side effect.

    The governing rule: **a feed's trust weight amplifies relevance, it never creates it.** An
    item that matches no theme scores below the threshold no matter which feed carried it.
    Without that, every item from a weighted regulator feed cleared the bar on feed weight plus
    a recency bonus alone, which is exactly how routine liquidity notices flooded the digest.
    """
    text = item.title
    total = 0.0
    tags: list[str] = []

    for name, regex in _THEME_RE.items():
        if regex.search(text):
            total += THEMES[name][0]
            tags.append(name)

    # A single-subject feed establishes its own theme; a broad official feed does not.
    if feed.tag_hint and feed.topical and feed.tag_hint not in tags:
        tags.append(feed.tag_hint)
        total += THEMES.get(feed.tag_hint, (2.0, ()))[0]

    if not any(tag not in WEAK_THEMES for tag in tags):
        # Nothing but weak signals, or nothing at all. Return early so neither feed weight nor a
        # recency bonus can lift an off-topic item over the threshold.
        item.tags = []
        item.score = -1.0
        return item.score

    # A broad feed's hint labels the item for grouping, without adding score.
    if feed.tag_hint and feed.tag_hint not in tags:
        tags.append(feed.tag_hint)

    total += feed.weight

    for weight, regex in _PENALTY_RE:
        if regex.search(text):
            total += weight

    if item.published:
        age_h = (now - item.published).total_seconds() / 3600.0
        if age_h <= 12:
            total += 2.0
        elif age_h <= 24:
            total += 1.0
        elif age_h > MAX_AGE_HOURS:
            total -= 10.0            # effectively excluded
    else:
        total -= 0.5                 # undated items are usually evergreen filler

    # Collapse internal tiers to display subjects, preserving order and dropping duplicates.
    seen: set[str] = set()
    display: list[str] = []
    for tag in tags:
        label = TAG_DISPLAY.get(tag, tag)
        if label not in seen:
            seen.add(label)
            display.append(label)
    item.tags = display
    item.score = total
    return total


# Words that carry no distinguishing signal when comparing two headlines.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in into is it its of on or that the to
    with was were will would after over amid says say said new news update""".split()
)

# Google News appends " - Publisher" to every title; two copies of one story differ only there.
_PUBLISHER_SUFFIX = re.compile(r"\s+[-–|]\s+[^-–|]{2,40}$")


def _signature(title: str) -> frozenset[str]:
    """Distinctive words in a headline, for near-duplicate detection."""
    stripped = _PUBLISHER_SUFFIX.sub("", title)
    words = re.findall(r"[a-z0-9]{3,}", stripped.lower())
    return frozenset(w for w in words if w not in _STOPWORDS)


def _is_near_duplicate(
    candidate: frozenset[str],
    seen: list[frozenset[str]],
    threshold: float = 0.6,
) -> bool:
    """Jaccard overlap against already-selected headlines.

    A fixed prefix key is not enough: the same wire story appears as "X added to index",
    "X enters index" and "X included in index", which share no common prefix but overlap almost
    entirely in their significant words.
    """
    if not candidate:
        return False
    for previous in seen:
        if not previous:
            continue
        union = len(candidate | previous)
        if union and len(candidate & previous) / union >= threshold:
            return True
    return False


def feed_is_stale(items: Sequence[NewsItem], feed: Feed, now: dt.datetime) -> Optional[float]:
    """Age in days of the feed's newest dated item, if that exceeds its allowance.

    This is the failure mode no structural check catches: a feed can return HTTP 200, perfectly
    well-formed RSS, with plausible market headlines that are two to ten years old. Without an
    explicit freshness assertion such a feed contributes nothing and reports no error, which is
    indistinguishable from a quiet news day.
    """
    dated = [item.published for item in items if item.published]
    if not dated:
        return None                     # undated feeds are judged per item instead
    age_days = (now - max(dated)).total_seconds() / 86400.0
    return age_days if age_days > feed.max_age_days else None


def collect(
    feeds: Sequence[Feed],
    http: Http,
    *,
    now: Optional[dt.datetime] = None,
    limit: int = MAX_ITEMS,
    min_score: float = MIN_SCORE,
    already_reported: Optional[set[str]] = None,
) -> tuple[list[NewsItem], list[str]]:
    """Fetch, score, dedupe and rank.

    `already_reported` holds signature keys from previous days. Recency alone cannot dedupe
    across runs: a SEBI circular stays legitimately relevant for a fortnight, so without this the
    digest re-reports the same item every morning for two weeks.

    Returns (items, feed_errors) where an error names a feed that was unreachable, unparseable or
    stale beyond its own allowance.
    """
    now = now or dt.datetime.now(IST)
    scored: list[tuple[float, NewsItem, Feed]] = []
    errors: list[str] = []
    suppressed = already_reported or set()

    for feed in feeds:
        raw = http.get(
            feed.url,
            headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"},
            expect="bytes",
        )
        if not raw:
            errors.append(f"{feed.name} (unreachable)")
            continue
        parsed = parse_feed(raw, feed)
        if not parsed:
            errors.append(f"{feed.name} (unparseable)")
            continue

        stale_by = feed_is_stale(parsed, feed, now)
        if stale_by is not None:
            # Valid RSS, wrong decade. Report it rather than letting it quietly contribute zero.
            errors.append(f"{feed.name} (stale {stale_by:.0f}d)")
            continue

        for item in parsed:
            value = score(item, feed, now)
            if value >= min_score:
                scored.append((value, item, feed))

    scored.sort(key=lambda row: (-row[0], row[1].published or dt.datetime.min.replace(tzinfo=IST)))

    chosen: list[NewsItem] = []
    signatures: list[frozenset[str]] = []
    per_source: dict[str, int] = {}
    per_theme: dict[str, int] = {}
    for value, item, feed in scored:
        # Strip the " - Publisher" tail before any keying, so the same story from two feeds
        # produces the same signature.
        item.title = _PUBLISHER_SUFFIX.sub("", item.title).strip()
        signature = _signature(item.title)

        if any(key in suppressed for key in item_keys(item)):
            continue
        if _is_near_duplicate(signature, signatures):
            continue
        if per_source.get(feed.name, 0) >= MAX_PER_SOURCE:
            continue
        subject = item.tags[0] if item.tags else "other"
        if per_theme.get(subject, 0) >= MAX_PER_THEME:
            continue

        signatures.append(signature)
        per_source[feed.name] = per_source.get(feed.name, 0) + 1
        per_theme[subject] = per_theme.get(subject, 0) + 1
        chosen.append(item)
        if len(chosen) >= limit:
            break

    return chosen, errors


def signature_key(title: str) -> str:
    """Word-set key for cross-run repeat suppression.

    Order-independent but not stemmed, so "cuts" and "cut" differ. That is why the URL is the
    primary key and this is the secondary one -- see `item_keys`.
    """
    return " ".join(sorted(_signature(title)))


def item_keys(item: NewsItem) -> list[str]:
    """Identity keys for suppressing an item seen on a previous day.

    The URL comes first because it is the real identity: a SEBI circular republished in tomorrow's
    feed window is the same link, and link equality is exact where word matching is fuzzy. The
    title signature is kept as a fallback for feeds that rewrite or omit links.
    """
    keys: list[str] = []
    if item.url:
        keys.append(f"url:{item.url.strip().rstrip('/')}")
    keys.append(f"sig:{signature_key(item.title)}")
    return keys


def themes_covered(items: Iterable[NewsItem]) -> set[str]:
    covered: set[str] = set()
    for item in items:
        covered.update(item.tags)
    return covered

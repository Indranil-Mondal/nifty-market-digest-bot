"""The news feed list.

Every URL here was probed live and returned parseable XML with recent items. Feeds that are
commonly recommended but are actually dead, blocked, stale or wrong-country are recorded at the
bottom so nobody re-adds them.

`max_age_days` is set per feed rather than globally. A market wire silent for four days is broken;
the US Fed's monetary feed legitimately goes six to eight weeks between FOMC meetings. A single
uniform rule would either miss a dead wire or report the Fed as down forever.
"""

from __future__ import annotations

from .news import Feed

# Weights are added to every item from that feed, so they express "how much do I trust this
# source to be telling me something that matters" rather than topical relevance.
FEEDS: tuple[Feed, ...] = (
    # --- Regulators: primary sources, weighted up but NOT blanket-relevant --------------------
    # `always_relevant` is deliberately off. These feeds are mostly daily housekeeping (liquidity
    # auctions, money-market statistics, single-bank enforcement), so a flat bonus for "a
    # regulator said it" floods the digest with items that move nothing. Substantive releases
    # earn their place through the policy_lever keyword set, and ROUTINE_NOISE suppresses the rest.
    Feed(
        name="RBI",
        url="https://www.rbi.org.in/pressreleases_rss.xml",
        weight=2.0,
        tag_hint="policy",
        max_age_days=5,
    ),
    Feed(
        name="RBI Notifications",
        url="https://www.rbi.org.in/notifications_rss.xml",
        weight=1.5,
        tag_hint="policy",
        # Only 10 items and RBI publishes several a weekday, so the window turns over fast; but
        # notifications themselves can be sparse, hence a longer allowance than press releases.
        max_age_days=14,
    ),
    Feed(
        name="SEBI",
        url="https://www.sebi.gov.in/sebirss.xml",
        weight=2.0,
        tag_hint="policy",
        max_age_days=7,
    ),
    # --- Indian market press ----------------------------------------------------------------
    Feed(name="ET Markets", url="https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", weight=1.5),
    Feed(name="ET Stocks", url="https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms", weight=1.0),
    Feed(name="Mint Markets", url="https://www.livemint.com/rss/markets", weight=1.5),
    Feed(name="Mint Money", url="https://www.livemint.com/rss/money", weight=1.0),
    Feed(name="Business Standard", url="https://www.business-standard.com/rss/markets-106.rss", weight=1.5),
    # Carries mutual-fund and AMC stories that the markets feed does not.
    Feed(name="BS Finance", url="https://www.business-standard.com/rss/finance-103.rss", weight=1.0),
    Feed(name="BusinessLine", url="https://www.thehindubusinessline.com/markets/feeder/default.rss", weight=1.5),
    Feed(name="BL Money & Banking", url="https://www.thehindubusinessline.com/money-and-banking/feeder/default.rss", weight=1.0),
    Feed(name="CNBC-TV18", url="https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml", weight=1.0),
    # --- Theme-specific, replacing the Google News queries that geo-drift in CI --------------
    # A dedicated gold section beats any general markets feed for this holding.
    # `topical=True` on these three: the whole feed is about one subject, so an item qualifies by
    # membership even when the headline omits the keyword ("Prices ease as dollar firms").
    Feed(
        name="BL Gold",
        url="https://www.thehindubusinessline.com/markets/gold/feeder/default.rss",
        weight=1.5,
        tag_hint="gold",
        topical=True,
        max_age_days=6,
    ),
    Feed(
        name="CNBC US Tech",
        url="https://www.cnbc.com/id/19854910/device/rss/rss.html",
        weight=1.0,
        tag_hint="ustech",
        topical=True,
        max_age_days=5,
    ),
    Feed(
        name="US Fed Monetary",
        url="https://www.federalreserve.gov/feeds/press_monetary.xml",
        weight=2.0,
        tag_hint="ustech",
        topical=True,
        # FOMC meets eight times a year, so six to eight silent weeks is normal, not a fault.
        max_age_days=70,
    ),
)


# --- Probed and rejected. Recorded so these are not tried again. --------------------------
#
# Google News RSS      THE IMPORTANT ONE. It GEO-DRIFTS, and `gl=IN&ceid=IN:en` does NOT pin it.
# (search queries)     The identical query returned India-heavy results from an Indian IP and
#                      US-heavy results from US infrastructure -- top hit "SLYG vs IJT: Which
#                      Small-Cap Growth ETF Wins?". Since this bot runs on a US-region GitHub
#                      runner, these feeds would silently deliver wrong-country news that looks
#                      perfectly plausible. They also need a `when:` operator or they are ordered
#                      by relevance rather than date and surface months-old items. Replaced by
#                      the dedicated publisher feeds above.
# Moneycontrol         /rss/*.xml -- 403 to every header combination tried from here, AND the
#                      feeds are years stale where reachable (one newest item from 2016). Both a
#                      block and a trap.
# Financial Express    /market/feed/ -- 302s then serves ~935KB of HTML, not RSS. Its WordPress
#                      REST endpoint (/wp-json/wp/v2/posts) does work but returns JSON, which
#                      would mean a second parser for one source.
# Reuters India        feeds.reuters.com -- ConnectionError; Reuters retired public RSS.
# Zee Business         /latest.xml/feed -- 403.
# BS gold-rate-today   /rss/gold-rate-today-232.rss -- listed in BS's own catalogue but serves
#                      271KB of site shell, not RSS.
# PIB                  RssMain.aspx -- 20 items but titles come back in Hindi for every Lang
#                      value tried (1, 2, 3) AND items carry no pubDate, so recency scoring
#                      cannot work. RBI and SEBI cover the same ground with dated English items.
# AMFI                 /rss.xml -- no feed. (/api/press-release is JSON and has ~7 rows spanning
#                      years, so it is not worth a parser.) The host is fine and is used for NAV.
# IBJA                 /rss -- no feed; it is a rates page, used for gold/silver rates instead.
# GDELT 2.0 doc API    429s aggressively and stickily, and a shared CI egress IP fares worse than
#                      a residential one. Not viable unattended.
# Bing News RSS        Interleaves 18-month-old items into a live result set, so a feed-level
#                      freshness check passes while individual rows are junk.
#
# Note: sebi.gov.in and amfiindia.com present a valid Sectigo chain that an outdated certifi
# bundle rejects. That is a dependency-pin issue, handled in requirements.txt -- it is NOT a
# reason to disable certificate verification.

"""Probe every configured feed, then show what the scoring ruleset selects and rejects.

    python -m scripts.news_check

Run this after touching bot/feeds.py or the scoring rules. A dead feed in an unattended bot is
a silent failure, and a ruleset that lets "Gold Rate Today in <city>" through is worse than no
news section at all -- this makes both visible.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import news as news_mod          # noqa: E402
from bot.feeds import FEEDS               # noqa: E402
from bot.http import Http                 # noqa: E402
from bot.util import ist_now              # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

ACCEPT = "application/rss+xml, application/xml, text/xml, */*"


def main() -> int:
    http = Http()
    now = ist_now()
    pulled: list[tuple[news_mod.NewsItem, news_mod.Feed]] = []
    dead: list[str] = []

    print(f"{'ITEMS':>6}  FEED")
    print("-" * 60)
    for feed in FEEDS:
        raw = http.get(feed.url, expect="bytes", headers={"Accept": ACCEPT})
        items = news_mod.parse_feed(raw, feed) if raw else []
        if not items:
            dead.append(feed.name)
            print(f"{'DEAD':>6}  {feed.name}")
            continue
        print(f"{len(items):>6}  {feed.name}")
        pulled.extend((item, feed) for item in items)

    print()
    print(f"feeds alive: {len(FEEDS) - len(dead)}/{len(FEEDS)}    headlines pulled: {len(pulled)}")
    if dead:
        print(f"DEAD: {', '.join(dead)}")

    scored = [(news_mod.score(item, feed, now), item) for item, feed in pulled]
    scored.sort(key=lambda row: -row[0])

    print()
    print("=== TOP 14 BY SCORE ===")
    for value, item in scored[:14]:
        tags = ",".join(item.tags[:3])
        print(f"  {value:6.1f}  [{tags:<20}] {item.title[:72]}")

    negative = [(v, i) for v, i in scored if v < 0]
    print()
    print(f"=== REJECTED WITH A NEGATIVE SCORE ({len(negative)} items) ===")
    for value, item in negative[:10]:
        print(f"  {value:6.1f}  {item.title[:78]}")

    print()
    print("=== WHAT THE DIGEST WOULD ACTUALLY SHOW ===")
    chosen, errors = news_mod.collect(FEEDS, http, now=now)
    for item in chosen:
        tags = "/".join(item.tags[:2])
        stamp = item.published.strftime("%d %b %H:%M") if item.published else "undated"
        print(f"  [{tags:<18}] {stamp}  {item.title[:64]}")
        print(f"  {'':<20}  {item.source}")
    if not chosen:
        print("  (nothing cleared the threshold)")
    print()
    print(f"themes covered: {sorted(news_mod.themes_covered(chosen)) or 'none'}")
    return 0 if len(dead) <= len(FEEDS) // 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())

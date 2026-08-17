"""Entry point for the morning digest.

    python -m bot.main                # fetch, render, send
    python -m bot.main --dry-run      # fetch and render, send nothing
    python -m bot.main --only nifty_50 --only bse_250_smallcap

The controlling principle is that silence must never look like good news. Every failure path
ends in *something* being delivered:

  one source fails          -> that instrument shows an em dash, the rest of the digest sends
  one fetcher raises        -> caught per instrument, digest still sends
  every instrument fails    -> a failure notice is sent instead of the digest
  rendering or sending dies -> a failure notice is attempted, and the digest is printed to
                               stdout so it survives in the Actions log

Exit codes: 0 healthy, 1 delivered but degraded, 2 nothing delivered.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path
from typing import Optional, Sequence

from . import format as fmt
from . import news as news_mod
from .compute import FetchResult, build_snapshot, persist
from .feeds import FEEDS
from .http import Http
from .instruments import build_registry
from .ledger import NewsLedger, SendLedger
from .model import Digest
from .notify import active_notifiers, broadcast
from .state import Store
from .util import ist_now

log = logging.getLogger("digest")

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "history"
STATE_DIR = ROOT / "data" / "state"

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FAILED = 2


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send the Indian market morning digest.")
    parser.add_argument("--dry-run", action="store_true", help="render but do not send")
    parser.add_argument("--only", action="append", default=[], metavar="KEY",
                        help="restrict to these instrument keys (repeatable)")
    parser.add_argument("--no-news", action="store_true", help="skip the news section")
    parser.add_argument("--quiet", action="store_true", help="send without a notification sound")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--once-per-day",
        action="store_true",
        help="exit without sending if a digest already went out today "
             "(makes several scheduled attempts safe)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="send even if one already went out today; overrides --once-per-day",
    )
    return parser.parse_args(argv)


def build_digest(args: argparse.Namespace) -> tuple[Digest, list[str], Optional[NewsLedger]]:
    now = ist_now()
    today = now.date()
    http = Http()
    store = Store(HISTORY_DIR)
    digest = Digest(generated_at=now)
    unhealthy: list[str] = []

    registry = build_registry()
    if args.only:
        wanted = set(args.only)
        registry = [r for r in registry if r.spec.key in wanted]
        missing = wanted - {r.spec.key for r in registry}
        if missing:
            log.warning("unknown instrument key(s): %s", ", ".join(sorted(missing)))

    for registration in registry:
        spec = registration.spec
        series = store.series(spec.key)
        try:
            result = registration.fetch(http, series, spec, today)
        except Exception as exc:  # noqa: BLE001 - a broken source must not stop the digest
            log.exception("%s fetcher raised", spec.key)
            result = FetchResult()
            result.errors.append(f"fetcher crashed: {exc.__class__.__name__}")

        try:
            persist(result, series, today)
            snapshot = build_snapshot(spec, series, result, today)
        except Exception as exc:  # noqa: BLE001 - as above, but for the maths
            log.exception("%s snapshot build raised", spec.key)
            snapshot = build_snapshot(spec, series, FetchResult(), today)
            snapshot.fail(f"compute failed: {exc.__class__.__name__}")

        digest.snapshots.append(snapshot)
        if not snapshot.healthy:
            unhealthy.append(spec.key)
            log.error("%s unhealthy: %s", spec.key, "; ".join(snapshot.errors) or "no data")

    # History is saved even on a partly failed run: whatever we did learn is worth keeping, and
    # a saved point today is one fewer archive fetch tomorrow.
    store.save_all()
    for line in store.summary():
        log.info("cache %s", line)

    news_ledger: Optional[NewsLedger] = None
    if not args.no_news:
        try:
            news_ledger = NewsLedger(STATE_DIR / "reported_news.json")
            items, feed_errors = news_mod.collect(
                FEEDS, http, now=now, already_reported=news_ledger.recent(today)
            )
            digest.news = items
            if feed_errors:
                log.warning("feeds unavailable: %s", ", ".join(feed_errors))
            # Only complain to the reader if news is broadly broken, not for one dead feed.
            if len(feed_errors) > len(FEEDS) // 2:
                digest.warnings.append("news feeds mostly unavailable")
        except Exception:  # noqa: BLE001
            log.exception("news collection raised")
            digest.warnings.append("news section unavailable")

    if unhealthy:
        digest.warnings.append(f"{len(unhealthy)} instrument(s) unavailable")

    return digest, unhealthy, news_ledger


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    now = ist_now()
    today = now.date()

    # Cron-drift defence. GitHub delays scheduled runs by hours, so the workflow fires several
    # attempts and the first one that actually runs does the work. That is only safe because a
    # later attempt can see the digest already went out.
    send_ledger = SendLedger(STATE_DIR / "sent.json")
    if args.once_per_day and not args.force and not args.dry_run and send_ledger.already_sent(today):
        log.info("digest for %s already delivered; nothing to do", today)
        return EXIT_OK

    try:
        digest, unhealthy, news_ledger = build_digest(args)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        log.exception("digest build failed outright")
        text = fmt.render_failure(
            f"Could not build the digest: {exc.__class__.__name__}: {exc}",
            now,
            traceback.format_exc(limit=6),
        )
        if not args.dry_run:
            broadcast(text)
        else:
            print(text)
        return EXIT_FAILED

    everything_failed = bool(digest.snapshots) and len(unhealthy) == len(digest.snapshots)

    if everything_failed:
        text = fmt.render_failure(
            "Every data source failed. No figures could be retrieved this morning.\n"
            "This is usually an upstream block or an endpoint change, not a transient error.",
            now,
            "\n".join(f"{s.display}: {'; '.join(s.errors) or 'no data'}" for s in digest.snapshots),
        )
    else:
        text = fmt.render(digest)

    if args.dry_run:
        print(text)
        log.info("dry run: nothing sent (%s chars)", len(text))
        if digest.news:
            log.info("news selected: %s item(s)", len(digest.news))
        return EXIT_DEGRADED if unhealthy else EXIT_OK

    if not active_notifiers():
        log.error("no channel configured; set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        print(text)
        return EXIT_FAILED

    delivered = broadcast(text, silent=args.quiet)
    if not delivered:
        log.error("delivery failed")
        return EXIT_FAILED

    # Only record after a confirmed send. If delivery failed, the next attempt must try again
    # rather than assume the morning is handled.
    send_ledger.mark(today)
    send_ledger.save()
    if news_ledger is not None:
        news_ledger.record(
            (key for item in digest.news for key in news_mod.item_keys(item)), today
        )
        news_ledger.save(today)

    if everything_failed:
        return EXIT_FAILED
    log.info("digest delivered (%s chars)", len(text))
    return EXIT_DEGRADED if unhealthy else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

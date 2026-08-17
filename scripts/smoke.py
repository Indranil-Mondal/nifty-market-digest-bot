"""Live smoke test: fetch every registered instrument and print the rendered digest.

Run it directly -- it needs no Telegram credentials and sends nothing:
    python -m scripts.smoke

It exits non-zero if any instrument came back unhealthy, so it doubles as a CI check that the
exchanges still answer the way the fetchers expect.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import format as fmt                              # noqa: E402
from bot.compute import FetchResult, build_snapshot, persist  # noqa: E402
from bot.http import Http                           # noqa: E402
from bot.instruments import build_registry          # noqa: E402
from bot.model import Digest                        # noqa: E402
from bot.state import Store                         # noqa: E402
from bot.util import ist_now                        # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("smoke")

HISTORY_DIR = Path(__file__).resolve().parents[1] / "data" / "history"


def main() -> int:
    now = ist_now()
    today = now.date()
    http = Http()
    store = Store(HISTORY_DIR)
    digest = Digest(generated_at=now)

    failures: list[str] = []
    for registration in build_registry():
        spec = registration.spec
        series = store.series(spec.key)
        log.info("--- %s (cache: %s points) ---", spec.key, len(series))
        try:
            result = registration.fetch(http, series, spec, today)
        except Exception as exc:                     # noqa: BLE001 - one bad source must never
            log.exception("%s raised", spec.key)      # take down the whole digest
            result = FetchResult()
            result.errors.append(f"fetcher crashed: {exc.__class__.__name__}: {exc}")
            failures.append(spec.key)

        persist(result, series, today)
        snapshot = build_snapshot(spec, series, result, today)
        digest.snapshots.append(snapshot)

        if not snapshot.healthy:
            failures.append(spec.key)
        for err in snapshot.errors:
            log.warning("%s: %s", spec.key, err)
        log.info(
            "%s: level=%s tri=%s pe=%s | history %s points",
            spec.key,
            snapshot.level.value,
            snapshot.tri.value,
            snapshot.pe.value,
            len(series),
        )

    store.save_all()
    for line in store.summary():
        log.info("cache %s", line)

    print("\n" + "=" * 60)
    print(fmt.render(digest))
    print("=" * 60)

    if failures:
        log.error("unhealthy: %s", ", ".join(sorted(set(failures))))
        return 1
    log.info("all instruments healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Small persistent ledgers: what news has been reported, and whether today's digest went out.

Both live as JSON under data/state/ and are committed back by the Actions job, which is what
makes them survive between runs on ephemeral runners.

They exist for two failures that only show up in production:

  * A daily bot with no memory re-reports the same SEBI circular every morning for a fortnight,
    because such a circular stays genuinely relevant that long and recency alone cannot dedupe
    it across runs.
  * GitHub Actions scheduled runs drift by hours, so the practical defence is to schedule several
    attempts and let the first one that actually fires do the work. That is only safe if a second
    attempt can tell the digest already went out.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# A circular or policy item stays relevant about a fortnight, so suppress repeats for slightly
# longer than that and then let it go.
NEWS_MEMORY_DAYS = 21


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        # A corrupt ledger must never stop delivery. The worst case from starting fresh is one
        # repeated news item, or one extra digest.
        log.warning("ledger %s unreadable (%s); starting fresh", path, exc)
        return {}


class NewsLedger:
    """Signature -> date last reported."""

    def __init__(self, path: Path):
        self.path = path
        raw = _read(path)
        self._seen: dict[str, str] = {
            key: value for key, value in (raw.get("seen") or {}).items() if isinstance(value, str)
        }

    def recent(self, today: dt.date, days: int = NEWS_MEMORY_DAYS) -> set[str]:
        cutoff = today - dt.timedelta(days=days)
        out: set[str] = set()
        for key, when in self._seen.items():
            try:
                if dt.date.fromisoformat(when) >= cutoff:
                    out.add(key)
            except ValueError:
                continue
        return out

    def record(self, keys: Iterable[str], today: dt.date) -> None:
        stamp = today.isoformat()
        for key in keys:
            if key:
                self._seen[key] = stamp

    def save(self, today: dt.date, days: int = NEWS_MEMORY_DAYS) -> None:
        cutoff = today - dt.timedelta(days=days * 2)
        kept: dict[str, str] = {}
        for key, when in self._seen.items():
            try:
                if dt.date.fromisoformat(when) >= cutoff:
                    kept[key] = when
            except ValueError:
                continue
        try:
            _atomic_write(self.path, {"seen": kept, "count": len(kept)})
        except OSError as exc:
            log.error("could not persist news ledger: %s", exc)


class SendLedger:
    """Which dates a digest has already been delivered for."""

    def __init__(self, path: Path):
        self.path = path
        raw = _read(path)
        self._sent: list[str] = [d for d in (raw.get("sent") or []) if isinstance(d, str)]

    def already_sent(self, day: dt.date) -> bool:
        return day.isoformat() in self._sent

    def last_sent(self) -> Optional[dt.date]:
        dates = []
        for text in self._sent:
            try:
                dates.append(dt.date.fromisoformat(text))
            except ValueError:
                continue
        return max(dates) if dates else None

    def mark(self, day: dt.date) -> None:
        stamp = day.isoformat()
        if stamp not in self._sent:
            self._sent.append(stamp)

    def save(self, keep: int = 40) -> None:
        try:
            _atomic_write(self.path, {"sent": sorted(self._sent)[-keep:]})
        except OSError as exc:
            log.error("could not persist send ledger: %s", exc)

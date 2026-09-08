"""Delivery channels.

Telegram is the only channel wired up today. `Notifier` exists so that adding WhatsApp later
is a new subclass plus two environment variables -- no change to the digest code, which only
ever calls `send()`.

Design notes:
  * We use parse_mode=HTML, not MarkdownV2. Telegram's MarkdownV2 requires escaping 18
    characters, several of which (`.`, `-`, `(`, `)`, `!`) appear constantly in numbers,
    percentages and news headlines. A single missed escape returns 400 and the whole digest
    is lost. HTML needs exactly three escapes and is therefore the safe choice for a bot
    nobody is watching.
  * Messages are split at 4096 characters on paragraph, then line, then hard boundaries.
"""

from __future__ import annotations

import html
import logging
import os
import time
from typing import Optional, Sequence

import requests

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
# Leave room for the "(1/3)" continuation marker we append when splitting.
CHUNK_TARGET = 3900

# Telegram's own guidance is roughly one message per second to a single chat. Two seconds
# between chunks is cheap insurance: the job is already awake, and a flood-wait costs the whole
# morning's digest.
INTER_CHUNK_PAUSE = 2.0

# A 429 is not a fault -- it is Telegram telling us when to come back -- so it gets a budget of
# its own, separate from the attempt count for genuine errors.
#
# This is not hypothetical. On 21 Aug 2026 Telegram answered every send with 429 retry_after=8.
# Because a 429 consumed one of the four ordinary attempts and skipped the exponential backoff,
# the bot gave up after 32 seconds per chunk, and three consecutive scheduled runs lost the
# digest. Waiting several minutes instead costs nothing and is exactly what the API asked for.
RATE_LIMIT_PATIENCE = 480.0     # cumulative seconds we are willing to spend waiting out a 429
RATE_LIMIT_MAX_SLEEP = 60.0     # never sleep longer than this in a single wait


def esc(text: object) -> str:
    """Escape a value for Telegram HTML parse mode."""
    return html.escape(str(text), quote=False)


def split_message(text: str, limit: int = CHUNK_TARGET) -> list[str]:
    """Split into Telegram-sized chunks, preferring paragraph then line boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 3:
            cut = window.rfind("\n")
        if cut < limit // 3:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip():
        chunks.append(remaining)
    return chunks


class Notifier:
    """Base channel."""

    name = "base"

    def send(self, text: str, *, silent: bool = False) -> bool:  # pragma: no cover
        raise NotImplementedError


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, *, timeout: int = 30, retries: int = 4):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()

    @classmethod
    def from_env(cls) -> Optional["TelegramNotifier"]:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat_id:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; Telegram disabled")
            return None
        return cls(token, chat_id)

    @staticmethod
    def _retry_after(resp: requests.Response, fallback: float) -> float:
        """How long Telegram wants us to wait, in seconds.

        Read from the JSON envelope first, then the standard Retry-After header, then a
        fallback. Parsing is defensive on purpose: a 429 raised by an edge proxy rather than by
        Telegram itself carries an HTML body, and calling .json() on that raises ValueError --
        which used to escape this method, abort the whole run and turn a transient throttle
        into a traceback.
        """
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            params = body.get("parameters")
            if isinstance(params, dict):
                try:
                    return max(1.0, float(params["retry_after"]))
                except (KeyError, TypeError, ValueError):
                    pass
        try:
            return max(1.0, float(resp.headers.get("Retry-After", "")))
        except (TypeError, ValueError):
            return fallback

    def _post(self, payload: dict) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        delay = 2.0
        rate_limited_for = 0.0
        failures = 0

        while failures < self.retries:
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                failures += 1
                log.warning("telegram network error (%s/%s): %s", failures, self.retries, exc)
                if failures < self.retries:
                    time.sleep(delay)
                    delay *= 2
                continue

            if resp.ok:
                return True

            if resp.status_code == 429:
                wait = min(self._retry_after(resp, delay), RATE_LIMIT_MAX_SLEEP)
                if rate_limited_for + wait > RATE_LIMIT_PATIENCE:
                    log.error(
                        "telegram has been rate limiting for %.0fs; stopping so a later run can "
                        "retry rather than deepening the flood wait",
                        rate_limited_for,
                    )
                    return False
                rate_limited_for += wait
                log.warning(
                    "telegram rate limited, waiting %.0fs (%.0fs of %.0fs patience used)",
                    wait, rate_limited_for, RATE_LIMIT_PATIENCE,
                )
                time.sleep(wait)
                continue        # deliberately does NOT consume an error attempt

            failures += 1
            log.warning(
                "telegram HTTP %s (%s/%s): %s",
                resp.status_code, failures, self.retries, resp.text[:400],
            )
            # A 400 is our bug (bad HTML), not a transient fault. Retry once as plain text so
            # the content still reaches the user instead of vanishing.
            if resp.status_code == 400 and payload.get("parse_mode"):
                log.warning("retrying without parse_mode")
                payload = {**payload, "parse_mode": None}
                continue
            if failures < self.retries:
                time.sleep(delay)
                delay *= 2

        return False

    def send(self, text: str, *, silent: bool = False) -> bool:
        chunks = split_message(text)
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            body = chunk if total == 1 else f"{chunk}\n\n<i>({i}/{total})</i>"
            payload = {
                "chat_id": self.chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            }
            if not self._post(payload):
                # Stop at the first failure instead of pushing the remaining chunks at a chat
                # that is already refusing us -- continuing only deepens a flood wait, which is
                # what turned one bad chunk into a lost digest on 21 Aug 2026.
                #
                # Returning False means the caller will not mark the day delivered, so a later
                # attempt re-sends from chunk 1. That can repeat a chunk the reader already has.
                # It is the deliberate trade: a duplicate is an annoyance, a missing digest is a
                # silent failure, and silence is the one outcome this bot must never produce.
                log.error(
                    "telegram: chunk %s of %s failed; %s chunk(s) already delivered", i, total, i - 1
                )
                return False
            if i < total:
                time.sleep(INTER_CHUNK_PAUSE)
        return True


def active_notifiers() -> list[Notifier]:
    """Every channel that is configured. Empty list means nothing is deliverable."""
    channels: list[Notifier] = []
    telegram = TelegramNotifier.from_env()
    if telegram:
        channels.append(telegram)
    return channels


def broadcast(text: str, *, channels: Optional[Sequence[Notifier]] = None, silent: bool = False) -> bool:
    """Send to every configured channel. True only if every channel accepted."""
    targets = list(channels) if channels is not None else active_notifiers()
    if not targets:
        log.error("no notification channel configured -- digest not delivered")
        print(text)  # last resort: leave it in the Actions log so the run is not a total loss
        return False
    results = [t.send(text, silent=silent) for t in targets]
    return all(results)

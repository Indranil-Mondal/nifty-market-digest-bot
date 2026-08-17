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

    def _post(self, payload: dict) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("telegram network error (attempt %s/%s): %s", attempt, self.retries, exc)
            else:
                if resp.ok:
                    return True
                # 429 carries retry_after; honour it rather than guessing.
                if resp.status_code == 429:
                    wait = float(resp.json().get("parameters", {}).get("retry_after", delay))
                    log.warning("telegram rate limited, sleeping %ss", wait)
                    time.sleep(min(wait, 60))
                    continue
                body = resp.text[:400]
                log.warning(
                    "telegram HTTP %s (attempt %s/%s): %s",
                    resp.status_code, attempt, self.retries, body,
                )
                # A 400 is our bug (bad HTML), not a transient fault. Retry once as plain
                # text so the content still reaches the user instead of vanishing.
                if resp.status_code == 400 and payload.get("parse_mode"):
                    log.warning("retrying without parse_mode")
                    payload = {**payload, "parse_mode": None}
                    continue
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        return False

    def send(self, text: str, *, silent: bool = False) -> bool:
        chunks = split_message(text)
        total = len(chunks)
        all_ok = True
        for i, chunk in enumerate(chunks, start=1):
            body = chunk if total == 1 else f"{chunk}\n\n<i>({i}/{total})</i>"
            payload = {
                "chat_id": self.chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            }
            ok = self._post(payload)
            all_ok = all_ok and ok
            if total > 1 and i < total:
                time.sleep(1.2)  # stay clear of Telegram's ~1 msg/sec per-chat guidance
        return all_ok


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

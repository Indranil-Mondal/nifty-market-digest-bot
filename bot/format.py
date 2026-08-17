"""Render a Digest as Telegram HTML.

Layout constraints that shaped this:
  * Telegram's mobile monospace block fits roughly 34 characters before it wraps or shrinks,
    so every <pre> table is built to stay under that.
  * A missing value prints as an em dash. It never prints as 0, 0.00 or "n/a" dressed up as
    data.
  * A percentage computed against a base date that drifted well off the calendar target is
    prefixed with a tilde, so an unusually long market closure is visible rather than hidden.
  * The PE column is dropped entirely for instruments where PE is not a meaningful concept
    (gold), rather than printing a column of dashes.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

from .model import (
    FRESHNESS_LIVE,
    FRESHNESS_PREV_CLOSE,
    FRESHNESS_STALE,
    FRESHNESS_T1,
    Change,
    Digest,
    NewsItem,
    Reading,
    Snapshot,
)
from .notify import esc
from .util import LOOKBACKS

DASH = "—"          # em dash: the universal "we do not know" marker
UP = "▲"
DOWN = "▼"
FLAT = "▬"
RULE = "━" * 17

# Drift beyond this many days from the calendar target earns a tilde on the percentage.
DRIFT_TILDE_DAYS = 5


def _num(value: Optional[float], places: int = 2, *, thousands: bool = True) -> str:
    if value is None:
        return DASH
    spec = f",.{places}f" if thousands else f".{places}f"
    return format(value, spec)


def _signed(value: Optional[float], places: int = 2) -> str:
    if value is None:
        return DASH
    return f"{value:+.{places}f}"


def _arrow(value: Optional[float]) -> str:
    if value is None:
        return ""
    if value > 0.005:
        return UP
    if value < -0.005:
        return DOWN
    return FLAT


def _reading(reading: Reading, places: int = 2) -> str:
    return _num(reading.value, places)


def _freshness_tag(reading: Reading, generated_at: dt.datetime) -> str:
    """Short parenthetical describing how current a headline number is."""
    if not reading.known:
        return ""
    bits: list[str] = []
    if reading.freshness == FRESHNESS_LIVE:
        bits.append(f"live {generated_at:%H:%M}")
    elif reading.freshness == FRESHNESS_PREV_CLOSE:
        bits.append("prev close")
    elif reading.freshness == FRESHNESS_T1:
        bits.append("T-1 NAV")
    elif reading.freshness == FRESHNESS_STALE:
        bits.append("stale")
    if reading.as_of and reading.freshness != FRESHNESS_LIVE:
        bits.append(f"{reading.as_of:%d %b}")
    return " · ".join(bits)


def _change_cell(change: Optional[Change]) -> str:
    if change is None or not change.known:
        return DASH
    drift = change.drift_days
    prefix = "~" if drift is not None and drift > DRIFT_TILDE_DAYS else ""
    return f"{prefix}{change.pct:+.2f}"


def _table(snapshot: Snapshot) -> str:
    """The lookback table: one row per window, Δ% and (optionally) PE-as-of-then."""
    show_pe = any(r.known for r in snapshot.pe_then.values()) or snapshot.pe.known
    header = f"{'':<4}{'chg %':>8}" + (f"{'PE':>7}" if show_pe else "")
    lines = [header]
    for label, _unit, _n in LOOKBACKS:
        change = snapshot.changes.get(label)
        row = f"{label:<4}{_change_cell(change):>8}"
        if show_pe:
            pe = snapshot.pe_then.get(label)
            row += f"{_num(pe.value if pe else None, 1, thousands=False):>7}"
        lines.append(row)
    return "\n".join(lines)


def _headline(snapshot: Snapshot, generated_at: dt.datetime) -> list[str]:
    """The two or three lines above the table."""
    out: list[str] = []

    # Pick the number that leads. For a tradeable ETF the market price leads; for an index the
    # level leads; for a fund with no listed price the NAV leads.
    if snapshot.level.known:
        lead, lead_name = snapshot.level, "Price" if snapshot.kind == "etf" else "Level"
    elif snapshot.tri.known:
        lead, lead_name = snapshot.tri, "TRI"
    else:
        lead, lead_name = snapshot.nav, "NAV"

    day = snapshot.changes.get("1D")
    arrow = _arrow(day.pct if day else None)
    tag = _freshness_tag(lead, generated_at)
    line = f"<code>{esc(_reading(lead))}</code>"
    if day and day.known:
        line += f"  <b>{arrow}{esc(_signed(day.pct))}%</b>"
    if tag:
        line += f"  <i>{esc(tag)}</i>"
    out.append(f"{esc(lead_name)}  {line}" if lead_name != "Level" else line)

    # Second line: whichever of TRI / NAV / iNAV exist and are not already the lead.
    extras: list[str] = []
    if snapshot.tri.known and lead is not snapshot.tri:
        extras.append(f"TRI {esc(_reading(snapshot.tri))}")
    if snapshot.nav.known and lead is not snapshot.nav:
        extras.append(f"NAV {esc(_reading(snapshot.nav))}")
    if snapshot.inav.known:
        extras.append(f"iNAV {esc(_reading(snapshot.inav))}")
    elif snapshot.kind == "etf":
        extras.append(f"iNAV {DASH}")
    if extras:
        out.append(" · ".join(extras))

    # Third line: valuation, only where it exists.
    valuation: list[str] = []
    if snapshot.pe.known:
        valuation.append(f"PE {esc(_num(snapshot.pe.value, 2, thousands=False))}")
    if snapshot.pb.known:
        valuation.append(f"PB {esc(_num(snapshot.pb.value, 2, thousands=False))}")
    if snapshot.div_yield.known:
        valuation.append(f"DY {esc(_num(snapshot.div_yield.value, 2, thousands=False))}%")
    if valuation:
        out.append(" · ".join(valuation))

    return out


def render_snapshot(snapshot: Snapshot, generated_at: dt.datetime) -> str:
    parts = [RULE, f"<b>{esc(snapshot.display)}</b>"]

    if not snapshot.healthy:
        reason = snapshot.errors[0] if snapshot.errors else "no data returned"
        parts.append(f"<i>unavailable {DASH} {esc(reason)}</i>")
        return "\n".join(parts)

    parts.extend(_headline(snapshot, generated_at))
    parts.append(f"<pre>{esc(_table(snapshot))}</pre>")

    if snapshot.change_basis != "level":
        parts.append(f"<i>moves on {esc(snapshot.change_basis)}</i>")
    # Three notes, not two: the instruments with real caveats (a licensee-only index, a
    # non-existent benchmark, a ratio needing its range for context) each carry a permanent
    # spec note plus one or two computed ones, and truncating to two silently dropped the
    # computed half -- which is the part that changes daily.
    for note in snapshot.notes[:3]:
        parts.append(f"<i>{esc(note)}</i>")
    if snapshot.errors:
        parts.append(f"<i>partial: {esc(snapshot.errors[0])}</i>")

    return "\n".join(parts)


def render_news(items: Sequence[NewsItem], limit: int = 8) -> str:
    if not items:
        return f"{RULE}\n<b>News &amp; policy</b>\n<i>nothing above the relevance threshold today</i>"
    lines = [RULE, "<b>News &amp; policy</b>"]
    for item in items[:limit]:
        tags = f" <i>[{esc('/'.join(item.tags[:2]))}]</i>" if item.tags else ""
        title = esc(item.title.strip())
        if item.url:
            lines.append(f"• <a href=\"{esc(item.url)}\">{title}</a>{tags}")
        else:
            lines.append(f"• {title}{tags}")
        if item.source:
            lines.append(f"  <i>{esc(item.source)}</i>")
    return "\n".join(lines)


def render(digest: Digest) -> str:
    when = digest.generated_at
    head = [
        "\U0001f1ee\U0001f1f3 <b>Morning Market Digest</b>",
        f"<i>{when:%a %d %b %Y} · {when:%H:%M} IST</i>",
    ]
    if digest.warnings:
        head.append(f"<i>⚠ {esc(digest.warnings[0])}</i>")

    body = [render_snapshot(s, when) for s in digest.snapshots]
    tail = [render_news(digest.news)]

    legend = (
        f"<i>{DASH} = not published / not applicable. "
        "~ = base date drifted past the calendar target. "
        "PE column is PE as it stood on that past date.</i>"
    )
    tail.append(legend)

    return "\n\n".join(["\n".join(head), *body, *tail])


def render_failure(reason: str, when: dt.datetime, detail: str = "") -> str:
    """Sent when the digest itself could not be built, so silence never looks like good news."""
    lines = [
        "⚠ <b>Morning digest failed</b>",
        f"<i>{when:%a %d %b %Y} · {when:%H:%M} IST</i>",
        "",
        esc(reason),
    ]
    if detail:
        lines.append(f"<pre>{esc(detail[:1200])}</pre>")
    return "\n".join(lines)

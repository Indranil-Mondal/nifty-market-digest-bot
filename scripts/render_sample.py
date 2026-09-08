"""Render real digest output to docs/sample-digest.png.

The README image has to be trustworthy: if it is drawn by hand, or touched up, it stops being
evidence and becomes marketing. So this renders the bot's ACTUAL Telegram HTML -- the same
string `bot.main` hands to the Bot API -- and the only liberties it takes are the two it
declares out loud (see SUBSTITUTIONS).

    python -m scripts.render_sample                      # build a digest live, then render
    python -m scripts.render_sample --html out.txt       # render a digest captured earlier
    python -m scripts.render_sample --blocks "NIFTY 50" "GOLD : SILVER RATIO"

Telegram's own renderer is not available to us, so this is a reimplementation of the small HTML
subset bot/format.py emits: <b>, <i>, <code>, <pre>, <a>, and the five entities esc() produces.
It is deliberately not a general HTML renderer -- anything unexpected in the input raises rather
than being quietly dropped, because a silently missing line in the sample image would misdescribe
the product.
"""

from __future__ import annotations

import argparse
import html as html_mod
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "sample-digest.png"

# Sampled from the previous image so a regenerated sample stays visually continuous with the
# one people have already seen in the README.
BG_PAGE = (24, 26, 31)
BG_CARD = (33, 37, 45)
EDGE = (48, 54, 64)
RULE = (58, 66, 78)
BODY = (226, 232, 240)
MUTED = (138, 150, 168)
TITLE = (110, 170, 242)
UP = (86, 200, 133)
DOWN = (232, 106, 94)

FONT_DIR = Path("C:/Windows/Fonts")
FONTS = {"r": "consola.ttf", "b": "consolab.ttf", "i": "consolai.ttf"}

# Supersample, then downscale. Consolas hinted at 19px has visibly uneven stems; rendering at 2x
# and resampling gives the smooth greyscale the original image has.
SCALE = 2
SIZE = 19
LEADING = 1.42
PAD_X, PAD_Y = 22, 20        # inside the card
MARGIN = 14                  # page around the card
RADIUS = 14

# The two honest liberties, both disclosed in the README caption. Consolas has no glyph for
# either, and a .notdef box in a screenshot looks like a bug in the bot rather than a gap in a
# font. Everything else in the digest -- the arrows, the box rule, the middot, the em dash --
# Consolas does have, and is drawn as-is.
SUBSTITUTIONS = {
    "\U0001f1ee\U0001f1f3": "",     # the flag pair that opens the digest
    "\u20b9": "Rs ",                # rupee sign
}

TAG = re.compile(r"</?(b|i|code|pre|a)(?:\s[^>]*)?>")
BLOCK_SPLIT = "\u2501" * 17          # format.RULE


def _font(style: str, scale: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / FONTS[style]), SIZE * scale)


def missing_glyphs(text: str, font: ImageFont.FreeTypeFont) -> set[str]:
    """Characters the font will draw as .notdef.

    fontTools would answer this from the cmap, but it is not a dependency of this project and
    adding one for a docs script is not worth it. Instead compare each glyph's bitmap against a
    private-use codepoint that is certainly absent: if they are identical, so is the glyph.
    """
    def bitmap(ch: str) -> tuple:
        # ImagingCore grew .tobytes() only in Pillow 10; bytes() works on both.
        mask = font.getmask(ch)
        return mask.size, bytes(mask)

    notdef = bitmap("\ue000")
    out = set()
    for ch in set(text):
        if ch.isspace():
            continue
        if bitmap(ch) == notdef:
            out.add(ch)
    return out


class Run:
    """A stretch of text sharing one style."""

    __slots__ = ("text", "style", "color")

    def __init__(self, text: str, style: str, color: tuple[int, int, int]):
        self.text, self.style, self.color = text, style, color


def _colour_for(text: str, bold: bool, italic: bool, whole_line_italic: bool) -> tuple:
    if bold and ("%" in text):
        if text.lstrip().startswith(("\u25b2", "+")):
            return UP, "b"
        if text.lstrip().startswith(("\u25bc", "-")):
            return DOWN, "b"
        return BODY, "b"
    if italic:
        # An italic run on a line of its own is a caveat and is muted; an italic run sitting
        # beside a number is the freshness tag and reads as part of the headline.
        return (MUTED if whole_line_italic else BODY), "i"
    if bold:
        return BODY, "b"
    return BODY, "r"


def parse_line(line: str) -> list[Run]:
    """Turn one line of the formatter's HTML into styled runs."""
    stripped = TAG.sub("", line)
    whole_line_italic = line.strip().startswith("<i>") and line.strip().endswith("</i>")

    runs: list[Run] = []
    bold = italic = 0
    pos = 0
    for match in TAG.finditer(line):
        chunk = line[pos:match.start()]
        if chunk:
            colour, style = _colour_for(html_mod.unescape(chunk), bool(bold), bool(italic),
                                        whole_line_italic)
            runs.append(Run(html_mod.unescape(chunk), style, colour))
        tag, closing = match.group(1), match.group(0).startswith("</")
        if tag == "b":
            bold += -1 if closing else 1
        elif tag == "i":
            italic += -1 if closing else 1
        pos = match.end()
    tail = line[pos:]
    if tail:
        colour, style = _colour_for(html_mod.unescape(tail), bool(bold), bool(italic),
                                    whole_line_italic)
        runs.append(Run(html_mod.unescape(tail), style, colour))

    if not runs and stripped.strip():
        runs.append(Run(html_mod.unescape(stripped), "r", BODY))
    return runs


def substitute(text: str) -> str:
    for bad, good in SUBSTITUTIONS.items():
        text = text.replace(bad, good)
    return text


def select_blocks(digest_html: str, wanted: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split the digest into blocks and pick the requested ones.

    Returns (chosen, names_of_the_rest) so the image can say honestly what it is not showing.
    """
    parts = digest_html.split(BLOCK_SPLIT)
    header = parts[0].rstrip()
    blocks = [p.strip("\n") for p in parts[1:]]

    def name_of(block: str) -> str:
        first = block.strip().splitlines()[0] if block.strip() else ""
        return TAG.sub("", first).strip()

    chosen, rest = [], []
    wanted = list(wanted)
    for block in blocks:
        label = name_of(block)
        if label in ("News &amp; policy", "News & policy"):
            continue
        (chosen if label in wanted else rest).append(block if label in wanted else label)

    missing = [w for w in wanted if w not in [name_of(b) for b in chosen]]
    if missing:
        available = [name_of(b) for b in blocks]
        raise SystemExit(f"block(s) not found: {missing}\navailable: {available}")
    # Preserve the order the caller asked for.
    chosen.sort(key=lambda b: wanted.index(name_of(b)))
    return [header, *chosen], rest


def render(digest_html: str, wanted: list[str], out: Path) -> None:
    chosen, rest = select_blocks(digest_html, wanted)
    header, blocks = chosen[0], chosen[1:]

    scale = SCALE
    fonts = {k: _font(k, scale) for k in FONTS}
    advance = fonts["r"].getlength("M")
    line_h = int(SIZE * scale * LEADING)

    # ---- build the display list ----------------------------------------------------------
    # (kind, payload). kind is "line" | "rule" | "gap".
    items: list[tuple[str, object]] = []
    for raw in header.splitlines():
        if raw.strip():
            items.append(("line", parse_line(substitute(raw))))
    for block in blocks:
        items.append(("gap", None))
        items.append(("rule", None))
        for raw in block.splitlines():
            if raw.strip():
                items.append(("line", parse_line(substitute(raw))))
    if rest:
        items.append(("gap", None))
        items.append(("rule", None))
        wrapped = _wrap_tail(rest, 62)
        for text in wrapped:
            items.append(("line", [Run(text, "i", MUTED)]))

    # ---- measure --------------------------------------------------------------------------
    widest = 0
    for kind, payload in items:
        if kind != "line":
            continue
        width = sum(fonts[r.style].getlength(r.text) for r in payload)
        widest = max(widest, width)

    card_w = int(widest + 2 * PAD_X * scale)
    card_h = int(sum(line_h if k == "line" else (line_h // 2 if k == "gap" else line_h // 2)
                     for k, _ in items) + 2 * PAD_Y * scale)
    page_w = card_w + 2 * MARGIN * scale
    page_h = card_h + 2 * MARGIN * scale

    image = Image.new("RGB", (page_w, page_h), BG_PAGE)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [MARGIN * scale, MARGIN * scale, page_w - MARGIN * scale, page_h - MARGIN * scale],
        radius=RADIUS * scale, fill=BG_CARD, outline=EDGE, width=scale,
    )

    # ---- draw -----------------------------------------------------------------------------
    x0 = MARGIN * scale + PAD_X * scale
    y = MARGIN * scale + PAD_Y * scale
    all_text = []
    for kind, payload in items:
        if kind == "gap":
            y += line_h // 2
            continue
        if kind == "rule":
            mid = y + line_h // 4
            draw.line([x0, mid, page_w - x0, mid], fill=RULE, width=scale)
            y += line_h // 2
            continue
        x = x0
        for run in payload:
            font = fonts[run.style]
            colour = run.color
            # The digest's own title line is the one place a colour is not derivable from the
            # markup: it is a <b> like any other, but it is the message heading.
            if run.text.strip() == "Morning Market Digest":
                colour = TITLE
            draw.text((x, y), run.text, font=font, fill=colour)
            x += font.getlength(run.text)
            all_text.append(run.text)
        y += line_h

    # ---- verify before writing --------------------------------------------------------------
    absent = missing_glyphs("".join(all_text), fonts["r"])
    if absent:
        raise SystemExit(
            "Consolas has no glyph for: "
            + ", ".join(f"U+{ord(c):04X} {c!r}" for c in sorted(absent))
            + "\nAdd it to SUBSTITUTIONS (and to the README caption) rather than shipping tofu."
        )

    image = image.resize((page_w // scale, page_h // scale), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, optimize=True)
    print(f"wrote {out} ({image.width}x{image.height})")
    print("blocks shown:", ", ".join(wanted))
    print("blocks named in the footer:", ", ".join(rest))


def _wrap_tail(names: list[str], width: int) -> list[str]:
    """'... plus A, B, and the news & policy section', wrapped by hand."""
    # Wrapped on commas, not on spaces: an instrument name broken across two lines ("NIFTY /
    # 50") reads as two instruments, which is the one thing this footer exists to get right.
    chunks = [f"{n}," for n in names] + ["and the news & policy section"]
    lines, current = [], "... plus"
    for chunk in chunks:
        if len(current) + 1 + len(chunk) > width:
            lines.append(current)
            current = "  " + chunk
        else:
            current = f"{current} {chunk}"
    if current.strip():
        lines.append(current)
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html", type=Path, help="a file holding digest HTML (else build live)")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--blocks", nargs="+",
        default=["NIFTY SMALLCAP 250 TRI", "SILVER \u2014 ZERODHA SILVER ETF",
                 "GOLD : SILVER RATIO"],
        help="display names of the blocks to show, in order",
    )
    args = parser.parse_args(argv)

    if args.html:
        digest_html = args.html.read_text(encoding="utf-8")
    else:
        sys.path.insert(0, str(ROOT))
        from bot import format as fmt
        from bot.main import build_digest, parse_args as bot_args
        digest, _unhealthy, _ledger = build_digest(bot_args(["--dry-run", "--no-news"]))
        digest_html = fmt.render(digest)

    render(digest_html, args.blocks, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

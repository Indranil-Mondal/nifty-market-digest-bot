"""Tests for the logic that has no network in it.

Run with:
    python -m unittest discover -s tests -v

These target the places where a silent wrong answer is possible: calendar arithmetic, snapping
a lookback onto a real trading day, deciding what counts as final data, and rendering a missing
value. A wrong percentage in a financial digest looks exactly like a right one, so this is where
the confidence has to come from.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import format as fmt
from bot import news as news_mod
from bot import notify
from bot.compute import FetchResult, InstrumentSpec, _locate_in_range, build_snapshot, persist
from bot.instruments import build_registry, with_fx
from bot.ledger import NewsLedger, SendLedger
from bot.model import (
    FRESHNESS_LIVE,
    FRESHNESS_PREV_CLOSE,
    FRESHNESS_STALE,
    FRESHNESS_T1,
    Change,
    Digest,
    FxRate,
    Reading,
    Snapshot,
)
from bot.sources import amfi, bse, fx as fx_src, gsr, nse, vix
from bot.state import Series, Store
from bot.stats import range_position, trailing_window
from bot.util import (
    from_iso,
    lookback_targets,
    nearest_on_or_before,
    parse_date,
    parse_ddmmmyyyy,
    parse_float,
    pct_change,
    shift_months,
)

D = dt.date


class TestCalendarMath(unittest.TestCase):
    def test_shift_months_plain(self):
        self.assertEqual(shift_months(D(2026, 8, 17), 3), D(2026, 5, 17))
        self.assertEqual(shift_months(D(2026, 8, 17), 6), D(2026, 2, 17))
        self.assertEqual(shift_months(D(2026, 8, 17), 12), D(2025, 8, 17))

    def test_shift_months_clamps_short_months(self):
        # 31 May minus three months is the end of February, not an exception.
        self.assertEqual(shift_months(D(2024, 5, 31), 3), D(2024, 2, 29))  # leap
        self.assertEqual(shift_months(D(2023, 5, 31), 3), D(2023, 2, 28))  # non-leap
        self.assertEqual(shift_months(D(2026, 3, 31), 1), D(2026, 2, 28))

    def test_shift_months_crosses_year(self):
        self.assertEqual(shift_months(D(2026, 1, 15), 1), D(2025, 12, 15))
        self.assertEqual(shift_months(D(2026, 1, 31), 2), D(2025, 11, 30))

    def test_lookback_targets_are_complete_and_ordered(self):
        targets = lookback_targets(D(2026, 8, 17))
        self.assertEqual(
            set(targets),
            {"1D", "1W", "2W", "3W", "4W", "3M", "6M", "1Y"},
        )
        self.assertEqual(targets["1D"], D(2026, 8, 16))
        self.assertEqual(targets["1W"], D(2026, 8, 10))
        self.assertEqual(targets["4W"], D(2026, 7, 20))
        self.assertEqual(targets["1Y"], D(2025, 8, 17))
        # Strictly decreasing as the horizon lengthens.
        ordered = [targets[k] for k in ("1D", "1W", "2W", "3W", "4W", "3M", "6M", "1Y")]
        self.assertEqual(ordered, sorted(ordered, reverse=True))


class TestNearestOnOrBefore(unittest.TestCase):
    def setUp(self):
        # A realistic week with a weekend gap and one holiday (Friday 14th present, 15th/16th out)
        self.available = [D(2026, 8, 10), D(2026, 8, 11), D(2026, 8, 12), D(2026, 8, 13), D(2026, 8, 14), D(2026, 8, 17)]

    def test_exact_hit(self):
        self.assertEqual(nearest_on_or_before(D(2026, 8, 12), self.available), D(2026, 8, 12))

    def test_walks_backwards_over_a_weekend(self):
        # Sunday 16th resolves to Friday 14th, never forward to Monday 17th.
        self.assertEqual(nearest_on_or_before(D(2026, 8, 16), self.available), D(2026, 8, 14))

    def test_never_returns_a_later_date(self):
        found = nearest_on_or_before(D(2026, 8, 9), self.available)
        self.assertIsNone(found, "must not jump forward to 10 Aug")

    def test_refuses_an_absurd_gap(self):
        # Guard against comparing against a date weeks off target when history is patchy.
        self.assertIsNone(nearest_on_or_before(D(2026, 9, 30), self.available, max_slack_days=12))
        self.assertEqual(
            nearest_on_or_before(D(2026, 9, 30), self.available, max_slack_days=60), D(2026, 8, 17)
        )

    def test_empty_series(self):
        self.assertIsNone(nearest_on_or_before(D(2026, 8, 12), []))


class TestNumberParsing(unittest.TestCase):
    def test_real_exchange_cell_formats(self):
        self.assertEqual(parse_float("1,234.56"), 1234.56)
        self.assertEqual(parse_float(" 12.3 "), 12.3)
        self.assertEqual(parse_float(".57"), 0.57)        # leading-dot decimals are common
        self.assertEqual(parse_float("-.12"), -0.12)
        self.assertEqual(parse_float("₹4,740"), 4740.0)
        self.assertEqual(parse_float(23378.56), 23378.56)

    def test_missing_markers_become_none(self):
        for blank in ("", " ", "-", "--", "NA", "N/A", "null", "None", "nil", None):
            self.assertIsNone(parse_float(blank), f"{blank!r} should be None")

    def test_ntr_dash_from_niftyindices(self):
        # NTR_Value is the literal '-' for every index except Nifty 50.
        self.assertIsNone(parse_float("-"))

    def test_bool_is_not_a_number(self):
        self.assertIsNone(parse_float(True))

    def test_pct_change(self):
        self.assertAlmostEqual(pct_change(110, 100), 10.0)
        self.assertAlmostEqual(pct_change(23378.56, 23333.72), 0.19217, places=4)
        self.assertIsNone(pct_change(100, None))
        self.assertIsNone(pct_change(None, 100))
        self.assertIsNone(pct_change(100, 0), "a zero base must not raise or return inf")

    def test_parse_date_formats(self):
        self.assertEqual(parse_date("17/08/2026", ("%d/%m/%Y",)), D(2026, 8, 17))
        self.assertEqual(parse_date("8/17/2026", ("%m/%d/%Y",)), D(2026, 8, 17))
        self.assertIsNone(parse_date("garbage", ("%d/%m/%Y",)))


class TestSeries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        s = Series.load("demo", self.root)
        s.upsert(D(2026, 8, 17), {"tri": 100.0, "pe": 30.0})
        s.upsert(D(2026, 8, 14), {"tri": 99.0})
        s.save()

        again = Series.load("demo", self.root)
        self.assertEqual(len(again), 2)
        self.assertEqual(again.record(D(2026, 8, 17))["pe"], 30.0)
        self.assertEqual(again.dates, [D(2026, 8, 14), D(2026, 8, 17)])

    def test_none_values_are_not_stored(self):
        s = Series.load("demo", self.root)
        s.upsert(D(2026, 8, 17), {"tri": 100.0, "pe": None})
        self.assertNotIn("pe", s.record(D(2026, 8, 17)))

    def test_existing_value_is_not_blanked_by_a_later_partial_fetch(self):
        s = Series.load("demo", self.root)
        s.upsert(D(2026, 8, 17), {"tri": 100.0, "pe": 30.0})
        s.upsert(D(2026, 8, 17), {"tri": 101.0})            # no overwrite by default
        self.assertEqual(s.record(D(2026, 8, 17))["tri"], 100.0)
        self.assertEqual(s.record(D(2026, 8, 17))["pe"], 30.0)
        s.upsert(D(2026, 8, 17), {"tri": 102.0}, overwrite=True)
        self.assertEqual(s.record(D(2026, 8, 17))["tri"], 102.0)

    def test_as_of_resolves_each_field_independently(self):
        # PE is published less reliably than closes. The PE comparison must not be dropped just
        # because the price series has an extra day, nor vice versa.
        s = Series.load("demo", self.root)
        s.upsert(D(2026, 8, 17), {"tri": 100.0})            # no PE on the newest day
        s.upsert(D(2026, 8, 14), {"tri": 99.0, "pe": 30.0})
        tri, tri_date = s.as_of(D(2026, 8, 17), "tri")
        pe, pe_date = s.as_of(D(2026, 8, 17), "pe")
        self.assertEqual((tri, tri_date), (100.0, D(2026, 8, 17)))
        self.assertEqual((pe, pe_date), (30.0, D(2026, 8, 14)))

    def test_corrupt_cache_is_quarantined_not_fatal(self):
        path = self.root / "demo.json"
        path.write_text("{ this is not json", encoding="utf-8")
        s = Series.load("demo", self.root)
        self.assertEqual(len(s), 0, "a corrupt cache must degrade to empty, not raise")
        self.assertTrue((self.root / "demo.json.corrupt").exists())

    def test_prune_keeps_a_year_plus_margin(self):
        s = Series.load("demo", self.root)
        s.upsert(D(2026, 8, 17), {"tri": 1.0})
        s.upsert(D(2023, 1, 1), {"tri": 2.0})               # far outside retention
        s.prune()
        self.assertEqual(s.dates, [D(2026, 8, 17)])

    def test_save_is_atomic_and_sorted(self):
        s = Series.load("demo", self.root)
        for day in (17, 14, 13):
            s.upsert(D(2026, 8, day), {"tri": float(day)})
        s.save()
        payload = json.loads((self.root / "demo.json").read_text(encoding="utf-8"))
        self.assertEqual(list(payload["series"]), ["2026-08-13", "2026-08-14", "2026-08-17"])
        self.assertEqual(payload["points"], 3)


class TestPersistAndSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.today = D(2026, 8, 18)
        self.spec = InstrumentSpec(
            key="demo", display="DEMO TRI", basis="tri", basis_label="TRI (total return)", has_pe=True
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _series_with_history(self) -> Series:
        s = Series.load("demo", self.root)
        # A year of weekday closes, TRI rising 0.05% a day, PE flat at 30.
        day = D(2025, 7, 1)
        value = 20000.0
        while day <= D(2026, 8, 17):
            if day.weekday() < 5:
                s.upsert(day, {"tri": round(value, 2), "level": round(value * 0.8, 2), "pe": 30.0})
                value *= 1.0005
            day += dt.timedelta(days=1)
        return s

    def test_intraday_values_are_never_persisted(self):
        # An 11:11 reading is not a close. Storing it would corrupt tomorrow's previous-day
        # comparison, which is the single most likely way to ship a wrong number every day.
        s = Series.load("demo", self.root)
        result = FetchResult()
        result.add("level", 123.0, as_of=self.today, freshness=FRESHNESS_LIVE)
        persist(result, s)
        self.assertEqual(len(s), 0)

    def test_final_values_are_persisted(self):
        s = Series.load("demo", self.root)
        result = FetchResult()
        result.add("tri", 456.0, as_of=D(2026, 8, 17), freshness=FRESHNESS_PREV_CLOSE)
        persist(result, s)
        self.assertEqual(s.record(D(2026, 8, 17))["tri"], 456.0)

    def test_anchor_is_the_last_close_when_the_level_is_stale(self):
        s = self._series_with_history()
        result = FetchResult()
        # A "live" feed serving yesterday's close, correctly labelled as such.
        result.add("level", 16000.0, as_of=D(2026, 8, 17), freshness=FRESHNESS_PREV_CLOSE)
        snap = build_snapshot(self.spec, s, result, self.today)

        self.assertTrue(snap.healthy)
        self.assertIn("17 Aug", snap.change_basis)
        day = snap.changes["1D"]
        self.assertIsNotNone(day.pct)
        self.assertNotAlmostEqual(day.pct, 0.0, places=6, msg="must not compare a close with itself")
        # Anchored on 17 Aug, one day back is 14 Aug (Friday), not 17 Aug.
        self.assertEqual(day.base_date, D(2026, 8, 14))

    def test_all_eight_lookbacks_resolve_with_pe(self):
        s = self._series_with_history()
        result = FetchResult()
        result.add("pe", 30.0, as_of=D(2026, 8, 17))
        snap = build_snapshot(self.spec, s, result, self.today)
        for label in ("1D", "1W", "2W", "3W", "4W", "3M", "6M", "1Y"):
            self.assertIsNotNone(snap.changes[label].pct, f"{label} change missing")
            self.assertIsNotNone(snap.pe_then[label].value, f"{label} PE missing")

    def test_no_history_degrades_rather_than_raising(self):
        s = Series.load("demo", self.root)
        snap = build_snapshot(self.spec, s, FetchResult(), self.today)
        self.assertFalse(snap.healthy)
        self.assertTrue(snap.errors)
        self.assertEqual(set(snap.changes), {"1D", "1W", "2W", "3W", "4W", "3M", "6M", "1Y"})
        self.assertTrue(all(c.pct is None for c in snap.changes.values()))

    def test_basis_falls_back_and_says_so(self):
        # TRI feed broken, price history intact: report price moves but disclose the swap.
        s = Series.load("demo", self.root)
        day = D(2026, 6, 1)
        while day <= D(2026, 8, 17):
            if day.weekday() < 5:
                s.upsert(day, {"level": 100.0 + day.toordinal() % 7})
            day += dt.timedelta(days=1)
        snap = build_snapshot(self.spec, s, FetchResult(), self.today)
        self.assertIn("fallback", snap.change_basis)

    def test_pe_omitted_entirely_when_instrument_has_none(self):
        gold = InstrumentSpec(key="gold", display="GOLD", kind="etf", basis="nav", has_pe=False)
        s = Series.load("demo", self.root)
        s.upsert(D(2026, 8, 17), {"nav": 90.0})
        s.upsert(D(2026, 8, 14), {"nav": 89.0})
        snap = build_snapshot(gold, s, FetchResult(), self.today)
        self.assertEqual(snap.pe_then, {}, "gold must not carry a PE column at all")


class TestRendering(unittest.TestCase):
    def _snap(self, **kw):
        spec = InstrumentSpec(key="k", display="THING", basis="level", has_pe=True)
        s = Series("k", Path("unused.json"))
        s.upsert(D(2026, 8, 17), {"level": 100.0, "pe": 20.0})
        s.upsert(D(2026, 8, 14), {"level": 99.0, "pe": 21.0})
        result = FetchResult()
        result.add("level", 100.0, as_of=D(2026, 8, 17), freshness=FRESHNESS_PREV_CLOSE)
        result.add("pe", 20.0, as_of=D(2026, 8, 17))
        return build_snapshot(spec, s, result, D(2026, 8, 18))

    def test_missing_values_render_as_em_dash_never_zero(self):
        snap = self._snap()
        text = fmt.render_snapshot(snap, dt.datetime(2026, 8, 18, 11, 11))
        self.assertIn(fmt.DASH, text)
        # A missing 1Y must not appear as +0.00.
        self.assertNotIn("1Y     +0.00", text)

    def test_drifted_base_date_is_flagged_with_a_tilde(self):
        far = Change(label="6M", pct=5.0, base_date=D(2026, 1, 1), target_date=D(2026, 2, 17))
        self.assertTrue(fmt._change_cell(far).startswith("~"))
        near = Change(label="1W", pct=5.0, base_date=D(2026, 8, 10), target_date=D(2026, 8, 11))
        self.assertFalse(fmt._change_cell(near).startswith("~"))

    def test_html_is_escaped(self):
        self.assertEqual(notify.esc("S&P <b>"), "S&amp;P &lt;b&gt;")

    def test_unhealthy_snapshot_says_unavailable(self):
        spec = InstrumentSpec(key="k", display="BROKEN", basis="tri")
        snap = build_snapshot(spec, Series("k", Path("x.json")), FetchResult(), D(2026, 8, 18))
        text = fmt.render_snapshot(snap, dt.datetime(2026, 8, 18, 11, 11))
        self.assertIn("unavailable", text)

    def test_full_digest_renders_and_has_a_legend(self):
        digest = Digest(generated_at=dt.datetime(2026, 8, 18, 11, 11), snapshots=[self._snap()])
        text = fmt.render(digest)
        self.assertIn("Morning Market Digest", text)
        self.assertIn("18 Aug 2026", text)
        self.assertIn("News", text)
        self.assertIn("not published", text)

    def test_failure_notice_renders(self):
        text = fmt.render_failure("everything broke", dt.datetime(2026, 8, 18, 11, 11), "trace")
        self.assertIn("failed", text)
        self.assertIn("everything broke", text)


class TestMessageSplitting(unittest.TestCase):
    def test_short_message_is_one_chunk(self):
        self.assertEqual(notify.split_message("hello"), ["hello"])

    def test_long_message_splits_and_stays_under_the_limit(self):
        text = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(60))
        chunks = notify.split_message(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), notify.TELEGRAM_LIMIT)

    def test_no_content_is_lost(self):
        text = "\n".join(f"line {i}" for i in range(900))
        joined = "".join(notify.split_message(text)).replace("\n", "")
        self.assertEqual(joined, text.replace("\n", ""))


class TestNewsParsing(unittest.TestCase):
    FEED = news_mod.Feed(name="T", url="http://x", weight=1.0)

    def test_rss_with_utf8_bom_parses(self):
        # RBI and PIB both emit a BOM ahead of the declaration; this is the exact shape that
        # broke the first implementation.
        raw = (
            b"\xef\xbb\xbf<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>"
            b"<item><title>Repo rate held at 5.5%</title>"
            b"<link>http://e.x/1</link>"
            b"<pubDate>Mon, 17 Aug 2026 19:00:00 +0530</pubDate></item>"
            b"</channel></rss>"
        )
        items = news_mod.parse_feed(raw, self.FEED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Repo rate held at 5.5%")
        self.assertIsNotNone(items[0].published)

    def test_atom_parses(self):
        raw = (
            b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>"
            b"<entry><title>SEBI tightens smallcap disclosure</title>"
            b"<link href='http://e.x/2'/><updated>2026-08-17T10:00:00Z</updated></entry></feed>"
        )
        items = news_mod.parse_feed(raw, self.FEED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "http://e.x/2")

    def test_malformed_feed_returns_empty_not_raise(self):
        self.assertEqual(news_mod.parse_feed(b"<html>nope", self.FEED), [])
        self.assertEqual(news_mod.parse_feed(b"", self.FEED), [])

    def test_sebi_non_standard_pubdate(self):
        # '17 Aug, 2026 +0530' -- the comma defeats strict RFC-822 parsing.
        when = news_mod._parse_when("17 Aug, 2026 +0530")
        self.assertIsNotNone(when)
        self.assertEqual((when.year, when.month, when.day), (2026, 8, 17))

    def test_standard_pubdate_still_works(self):
        when = news_mod._parse_when("Mon, 17 Aug 2026 19:00:00 +0530")
        self.assertIsNotNone(when)
        self.assertEqual(when.hour, 19)


class TestNewsScoring(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 18, 11, 11, tzinfo=news_mod.IST)
    FEED = news_mod.Feed(name="T", url="http://x", weight=1.0)

    def _score(self, title: str, feed=None) -> float:
        item = news_mod.NewsItem(title=title, published=self.NOW - dt.timedelta(hours=4))
        return news_mod.score(item, feed or self.FEED, self.NOW)

    def test_relevant_policy_beats_threshold(self):
        self.assertGreater(self._score("RBI cuts repo rate by 25 bps in surprise move"), news_mod.MIN_SCORE)

    def test_smallcap_valuation_story_scores(self):
        self.assertGreater(self._score("Smallcap valuations look frothy as PE ratio nears record"), news_mod.MIN_SCORE)

    def test_listicle_junk_is_rejected(self):
        for junk in (
            "Top 5 smallcap stocks to buy this week",
            "Gold Rate Today in Visakhapatnam 17th August 2026 : 22 & 24 carat",
            "KEI Industries among 4 midcap stocks to hit 52-week highs",
        ):
            self.assertLess(self._score(junk), news_mod.MIN_SCORE, junk)

    def test_routine_regulator_housekeeping_is_rejected(self):
        for routine in (
            "RBI to conduct Overnight Variable Rate Reverse Repo (VRRR) auction",
            "Money Market Operations as on August 16, 2026",
            "Auction of Government of India Dated Securities",
            "General Remittance Order dated 14.08.2026 in Recovery Certificate",
        ):
            feed = news_mod.Feed(name="RBI", url="http://x", weight=2.0, tag_hint="policy")
            item = news_mod.NewsItem(title=routine, published=self.NOW - dt.timedelta(hours=4))
            self.assertLess(news_mod.score(item, feed, self.NOW), news_mod.MIN_SCORE, routine)

    def test_single_stock_index_inclusion_is_rejected(self):
        # Matches the 'gold' theme only because the company is called "Sky Gold".
        for variant in ("added to", "enters", "included in", "joins"):
            title = f"Sky Gold & Diamonds {variant} MSCI India domestic small cap index"
            self.assertLess(self._score(title), news_mod.MIN_SCORE, title)

    def test_stale_item_is_excluded(self):
        old = news_mod.NewsItem(title="RBI cuts repo rate", published=self.NOW - dt.timedelta(days=6))
        self.assertLess(news_mod.score(old, self.FEED, self.NOW), news_mod.MIN_SCORE)

    def test_near_duplicates_collapse(self):
        a = news_mod._signature("Sky Gold & Diamonds added to MSCI India domestic small cap index")
        b = news_mod._signature("Sky Gold enters MSCI India Domestic Small Cap Index - Business Standard")
        self.assertTrue(news_mod._is_near_duplicate(b, [a]))

    def test_distinct_stories_do_not_collapse(self):
        a = news_mod._signature("RBI cuts repo rate by 25 basis points")
        b = news_mod._signature("Gold nears $4,400 as traders weigh Fed rate path")
        self.assertFalse(news_mod._is_near_duplicate(b, [a]))

    def test_publisher_suffix_stripped_from_signature(self):
        with_suffix = news_mod._signature("Gold rises on weaker dollar - Economic Times")
        without = news_mod._signature("Gold rises on weaker dollar")
        self.assertTrue(news_mod._is_near_duplicate(with_suffix, [without]))


class TestExchangeIdentifiers(unittest.TestCase):
    """The date formats and index codes differ per endpoint; mixing them up is the likeliest bug."""

    def test_nifty_request_date_format(self):
        self.assertEqual(nse._fmt_request_date(D(2025, 7, 1)), "01-Jul-2025")
        self.assertEqual(nse._fmt_request_date(D(2026, 8, 17)), "17-Aug-2026")

    def test_nifty_response_date_format(self):
        self.assertEqual(nse._parse_response_date("17 Aug 2026"), D(2026, 8, 17))
        self.assertIsNone(nse._parse_response_date("garbage"))
        self.assertIsNone(nse._parse_response_date(""))

    def test_nifty_live_tick_timestamp(self):
        self.assertEqual(nse._parse_tick("17-Aug-2026 15:30"), D(2026, 8, 17))
        self.assertIsNone(nse._parse_tick(None))

    def test_smallcap_uses_the_abbreviated_trading_code(self):
        # The PE endpoint silently returns [] for the spelled-out name.
        self.assertEqual(nse.NIFTY_SMALLCAP_250.post_name, "NIFTY SMLCAP 250")
        self.assertEqual(nse.NIFTY_SMALLCAP_250.post_index_name, "NIFTY Smallcap 250")
        self.assertEqual(nse.NIFTY_SMALLCAP_250.live_name, "NIFTY SMLCAP 250")

    def test_bse_csv_row_matched_on_index_code(self):
        csv_text = (
            "Date,Index Code,Index ID,Index Name,Open,High,Low,Close,Points Change,Change(%),"
            "Volume(Cr.),Turnover (Rs.Cr.),P/E,P/B,Div Yield\n"
            "08/17/2026,SENSEX,1,BSE SENSEX,1,1,1,77728.16,1,1,1,1,22.5,3.5,1.10\n"
            "08/17/2026,SML250,56,BSE 250 SmallCap Index,1,1,1,7220.83,1,1,1,1,35.22,4.56,0.58\n"
        )
        row = bse._find_index_row(csv_text, "SML250")
        self.assertIsNotNone(row)
        self.assertEqual(row["Index Name"], "BSE 250 SmallCap Index")
        self.assertEqual(parse_float(row["P/E"]), 35.22)
        self.assertEqual(parse_float(row["Close"]), 7220.83)

    def test_bse_missing_index_returns_none(self):
        self.assertIsNone(bse._find_index_row("Date,Index Code\n08/17/2026,OTHER\n", "SML250"))


class TestFeedFreshness(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 18, 11, 11, tzinfo=news_mod.IST)

    def _items(self, *ages_days):
        return [
            news_mod.NewsItem(title=f"item {i}", published=self.NOW - dt.timedelta(days=age))
            for i, age in enumerate(ages_days)
        ]

    def test_fresh_feed_is_not_stale(self):
        feed = news_mod.Feed(name="X", url="u", max_age_days=4)
        self.assertIsNone(news_mod.feed_is_stale(self._items(0.2, 1, 3), feed, self.NOW))

    def test_valid_but_ancient_feed_is_caught(self):
        # The Moneycontrol failure mode: HTTP 200, well-formed RSS, years out of date.
        feed = news_mod.Feed(name="X", url="u", max_age_days=4)
        age = news_mod.feed_is_stale(self._items(800, 900), feed, self.NOW)
        self.assertIsNotNone(age)
        self.assertGreater(age, 700)

    def test_low_volume_official_feed_gets_a_longer_allowance(self):
        # The US Fed goes 6-8 weeks between FOMC meetings; that is normal, not a fault.
        fed = news_mod.Feed(name="Fed", url="u", max_age_days=70)
        wire = news_mod.Feed(name="Wire", url="u", max_age_days=4)
        self.assertIsNone(news_mod.feed_is_stale(self._items(40), fed, self.NOW))
        self.assertIsNotNone(news_mod.feed_is_stale(self._items(40), wire, self.NOW))

    def test_undated_feed_is_judged_per_item_not_rejected(self):
        feed = news_mod.Feed(name="X", url="u")
        undated = [news_mod.NewsItem(title="no date")]
        self.assertIsNone(news_mod.feed_is_stale(undated, feed, self.NOW))


class TestPolicyLeverVsActor(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 18, 11, 11, tzinfo=news_mod.IST)
    FEED = news_mod.Feed(name="SEBI", url="u", weight=2.0, tag_hint="policy")

    def _score(self, title):
        item = news_mod.NewsItem(title=title, published=self.NOW - dt.timedelta(hours=3))
        return news_mod.score(item, self.FEED, self.NOW)

    def test_a_real_lever_outranks_a_bare_regulator_mention(self):
        lever = self._score("Sebi tightens stress test disclosure norms for smallcap funds")
        soundbite = self._score("Sebi chairman says cyber defence must move to boardroom priority")
        self.assertGreater(lever, soundbite)
        self.assertGreater(lever, news_mod.MIN_SCORE)

    def test_tags_collapse_internal_tiers_to_one_label(self):
        item = news_mod.NewsItem(title="RBI repo rate decision", published=self.NOW)
        news_mod.score(item, self.FEED, self.NOW)
        self.assertIn("policy", item.tags)
        self.assertNotIn("policy_lever", item.tags)
        self.assertNotIn("policy_actor", item.tags)
        self.assertEqual(len(item.tags), len(set(item.tags)), "tags must not repeat")

    def test_weak_flow_vocabulary_alone_does_not_qualify(self):
        # A bare AUM mention once promoted an unrelated REIT story into the digest.
        self.assertLess(self._score("Company X reports AUM growth in its REIT portfolio"), news_mod.MIN_SCORE)

    def test_feed_weight_cannot_qualify_an_off_topic_item(self):
        # The governing rule: trust weight amplifies relevance, it never creates it. A weighted
        # regulator feed must not push a wholly off-topic notice over the bar.
        heavy = news_mod.Feed(name="SEBI", url="u", weight=9.0, tag_hint="policy")
        item = news_mod.NewsItem(title="Office premises tender for regional branch", published=self.NOW)
        self.assertLess(news_mod.score(item, heavy, self.NOW), news_mod.MIN_SCORE)
        self.assertEqual(item.tags, [])

    def test_topical_feed_qualifies_a_keywordless_headline(self):
        # A dedicated gold section may legitimately run a headline with no "gold" in it.
        gold_feed = news_mod.Feed(name="BL Gold", url="u", weight=1.5, tag_hint="gold", topical=True)
        item = news_mod.NewsItem(title="Prices ease as the dollar firms", published=self.NOW)
        self.assertGreater(news_mod.score(item, gold_feed, self.NOW), news_mod.MIN_SCORE)
        self.assertIn("gold", item.tags)

    def test_broad_feed_hint_labels_but_does_not_qualify(self):
        broad = news_mod.Feed(name="SEBI", url="u", weight=2.0, tag_hint="policy", topical=False)
        item = news_mod.NewsItem(title="Prices ease as the dollar firms", published=self.NOW)
        self.assertLess(news_mod.score(item, broad, self.NOW), news_mod.MIN_SCORE)


class TestLedgers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.today = D(2026, 8, 18)

    def tearDown(self):
        self.tmp.cleanup()

    def test_send_ledger_round_trip(self):
        path = self.root / "sent.json"
        a = SendLedger(path)
        self.assertFalse(a.already_sent(self.today))
        a.mark(self.today)
        a.save()

        b = SendLedger(path)
        self.assertTrue(b.already_sent(self.today), "a second attempt must see the first send")
        self.assertFalse(b.already_sent(self.today + dt.timedelta(days=1)))
        self.assertEqual(b.last_sent(), self.today)

    def test_send_ledger_survives_corruption(self):
        path = self.root / "sent.json"
        path.write_text("not json at all", encoding="utf-8")
        ledger = SendLedger(path)
        # Degrading to "not sent" risks one duplicate message; degrading the other way would lose
        # the digest entirely, which is worse.
        self.assertFalse(ledger.already_sent(self.today))

    def test_news_ledger_suppresses_then_forgets(self):
        path = self.root / "news.json"
        a = NewsLedger(path)
        a.record(["alpha beta gamma"], self.today - dt.timedelta(days=3))
        a.record(["stale item"], self.today - dt.timedelta(days=60))
        a.save(self.today)

        b = NewsLedger(path)
        recent = b.recent(self.today)
        self.assertIn("alpha beta gamma", recent, "a 3-day-old item must still be suppressed")
        self.assertNotIn("stale item", recent, "a 60-day-old item must be forgotten")

    def test_signature_key_is_order_independent(self):
        a = news_mod.signature_key("RBI cut repo rate by 25 bps")
        b = news_mod.signature_key("Repo rate cut by 25 bps, says RBI")
        # Same significant words in a different order collapse to the same key. Note it is not
        # stemmed, so "cut" and "cuts" differ -- which is exactly why the URL is the primary key.
        self.assertEqual(a, b)

    def test_url_is_the_primary_suppression_key(self):
        item = news_mod.NewsItem(title="Sebi tightens norms", url="https://x.example/a/1/")
        keys = news_mod.item_keys(item)
        self.assertEqual(keys[0], "url:https://x.example/a/1", "trailing slash must be normalised")
        self.assertTrue(keys[1].startswith("sig:"))

    def test_same_article_reworded_still_suppressed_by_url(self):
        # The real case: a SEBI circular still in tomorrow's feed window, headline tweaked.
        day_one = news_mod.NewsItem(title="Sebi tightens stress test norms", url="https://s.example/c/9")
        day_two = news_mod.NewsItem(title="Sebi tightens stress-testing norms for funds", url="https://s.example/c/9")
        seen = set(news_mod.item_keys(day_one))
        self.assertTrue(any(k in seen for k in news_mod.item_keys(day_two)))

    def test_item_without_url_still_has_a_key(self):
        item = news_mod.NewsItem(title="Gold rises on weaker dollar")
        keys = news_mod.item_keys(item)
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("sig:"))


class TestGoldSilverRatio(unittest.TestCase):
    def test_ibja_unit_conversion(self):
        # Real IBJA values: 999 gold 154,167 per 10g; 999 silver 235,642 per kg.
        ratio = gsr.ratio_from_ibja_quotes(154167.0, 235642.0)
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 65.42, places=1)

    def test_forgetting_the_divisors_is_caught(self):
        # Treating silver's per-kg quote as per-gram gives ~0.065, outside the plausible band.
        self.assertIsNone(gsr.ratio_from_ibja_quotes(15416.7, 235642.0 * 1000))

    def test_swapped_divisors_are_caught(self):
        # Gold read as per-kg and silver as per-gram: ~0.65, rejected rather than printed.
        self.assertIsNone(gsr.ratio_from_ibja_quotes(154167.0 / 100, 235642.0 * 1000))

    def test_zero_and_negative_rejected(self):
        self.assertIsNone(gsr.ratio_from_ibja_quotes(0.0, 235642.0))
        self.assertIsNone(gsr.ratio_from_ibja_quotes(154167.0, 0.0))

    def test_percentile_position(self):
        history = [float(v) for v in range(50, 150)]      # 50..149
        text = gsr._position_in_range(50.0, history)
        self.assertIsNotNone(text)
        self.assertIn("percentile 1", text)
        self.assertIn("50.0–149.0", text)
        top = gsr._position_in_range(149.0, history)
        self.assertIn("percentile 100", top)

    def test_percentile_label_has_no_broken_ordinal(self):
        # Guards the "1th"/"21th" class of bug that a literal "th" suffix produces.
        history = [float(v) for v in range(50, 150)]
        for probe in (50.0, 70.0, 71.0, 149.0):
            text = gsr._position_in_range(probe, history)
            for wrong in ("1th", "2th", "3th"):
                self.assertNotIn(wrong, text or "")

    def test_percentile_needs_enough_history(self):
        self.assertIsNone(gsr._position_in_range(67.0, [66.0, 67.0, 68.0]))

    def test_reading_bands_are_descriptive_not_advisory(self):
        high = gsr._reading(95.0)
        low = gsr._reading(40.0)
        mid = gsr._reading(67.0)
        self.assertIn("silver historically cheap", high)
        self.assertIn("gold historically cheap", low)
        self.assertIn("long-run", mid)
        # No instruction to act appears in any band.
        for text in (high, low, mid):
            for word in ("buy", "sell", "should"):
                self.assertNotIn(word, text.lower())


class TestStore(unittest.TestCase):
    def test_store_caches_series_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            a = store.series("x")
            b = store.series("x")
            self.assertIs(a, b)
            a.upsert(D(2026, 8, 17), {"tri": 1.0})
            store.save_all()
            self.assertTrue((Path(tmp) / "x.json").exists())
            self.assertIn("x: 1 points", "\n".join(store.summary()))


class TestStalenessGuard(unittest.TestCase):
    """The net that should have caught the three-week NAV freeze on about day two.

    The old check looked only at the basis series and only when the anchor was not live, so a
    live price could mask a frozen NAV indefinitely -- which is exactly what happened to gold.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.today = D(2026, 9, 9)

    def tearDown(self):
        self.tmp.cleanup()

    def _etf(self, nav_last: dt.date, level_last: dt.date, allowance: int = 6):
        spec = InstrumentSpec(
            key="metal", display="METAL ETF", kind="etf", basis="nav", basis_label="NAV",
            has_pe=False, stale_after_days=allowance,
        )
        s = Series.load("metal", self.root)
        day = D(2025, 9, 1)
        while day <= max(nav_last, level_last):
            if day.weekday() < 5:
                if day <= nav_last:
                    s.upsert(day, {"nav": 100.0})
                if day <= level_last:
                    s.upsert(day, {"level": 101.0})
            day += dt.timedelta(days=1)
        result = FetchResult()
        result.add("nav", 100.0, as_of=nav_last, freshness=FRESHNESS_T1)
        result.add("level", 101.0, as_of=level_last, freshness=FRESHNESS_PREV_CLOSE)
        return build_snapshot(spec, s, result, self.today)

    def test_a_normal_lag_is_not_flagged(self):
        # NAV published for the previous session, price for yesterday's close. Business as usual.
        snap = self._etf(nav_last=D(2026, 9, 8), level_last=D(2026, 9, 8))
        self.assertNotEqual(snap.nav.freshness, FRESHNESS_STALE)
        self.assertFalse([n for n in snap.notes if "stopped" in n])

    def test_a_long_weekend_is_not_flagged(self):
        # Four calendar days behind is the worst legitimate lag measured over 262 real runs.
        snap = self._etf(nav_last=D(2026, 9, 5), level_last=D(2026, 9, 8))
        self.assertNotEqual(snap.nav.freshness, FRESHNESS_STALE)

    def test_a_frozen_nav_is_flagged_even_though_the_price_still_moves(self):
        # Gold's exact failure: the price kept updating from BSE while AMFI gave nothing.
        snap = self._etf(nav_last=D(2026, 8, 19), level_last=D(2026, 9, 8))
        self.assertEqual(snap.nav.freshness, FRESHNESS_STALE)
        self.assertNotEqual(snap.level.freshness, FRESHNESS_STALE)
        self.assertTrue(
            any("nav" in n and "stopped" in n for n in snap.notes),
            f"expected a staleness note, got {snap.notes}",
        )

    def test_the_note_is_first_so_the_renderer_cannot_drop_it(self):
        snap = self._etf(nav_last=D(2026, 8, 19), level_last=D(2026, 9, 8))
        self.assertIn("stopped", snap.notes[0])

    def test_a_longer_allowance_tolerates_the_overseas_fund(self):
        # The feeder fund loses two market calendars and hit six days behind legitimately.
        snap = self._etf(nav_last=D(2026, 9, 3), level_last=D(2026, 9, 3), allowance=9)
        self.assertNotEqual(snap.nav.freshness, FRESHNESS_STALE)


class TestUpstreamSchemaGuards(unittest.TestCase):
    """A provider that renames a key must produce an ERROR, not an empty result.

    This is the exact shape of the AMFI failure of Aug 2026: rows kept arriving, nothing
    matched, the fetch looked empty rather than broken, and the digest reprinted cached numbers
    for three weeks. AMFI was caught only because a redundant ISIN check objected; the NSE
    endpoints have no such check, so the mismatch has to be detected directly.
    """

    ROWS = [
        {"Date": "08 Sep 2026", "TotalReturnsIndex": "35913.37", "NTR_Value": "-"},
        {"Date": "07 Sep 2026", "TotalReturnsIndex": "36134.02", "NTR_Value": "-"},
    ]

    def test_known_keys_parse_with_no_error(self):
        out, error = nse._series_from_rows(self.ROWS, "Date", {"TotalReturnsIndex": "tri"})
        self.assertIsNone(error)
        self.assertEqual(out[D(2026, 9, 8)]["tri"], 35913.37)

    def test_renamed_value_key_is_an_error_not_an_empty_result(self):
        out, error = nse._series_from_rows(self.ROWS, "Date", {"TotalReturnIndexValue": "tri"})
        self.assertEqual(out, {})
        self.assertIsNotNone(error)
        self.assertIn("TotalReturnIndexValue", error)

    def test_renamed_date_key_is_an_error(self):
        out, error = nse._series_from_rows(self.ROWS, "TradeDate", {"TotalReturnsIndex": "tri"})
        self.assertEqual(out, {})
        self.assertIsNotNone(error)
        self.assertIn("TradeDate", error)

    def test_a_partially_empty_column_is_not_an_error(self):
        # NTR_Value is the literal '-' for every index except Nifty 50. That is normal data,
        # not a schema change, and must not raise the alarm.
        out, error = nse._series_from_rows(
            self.ROWS, "Date", {"TotalReturnsIndex": "tri", "NTR_Value": "ntr"}
        )
        self.assertIsNone(error)
        self.assertNotIn("ntr", out[D(2026, 9, 8)])

    def test_no_rows_at_all_is_not_a_schema_error(self):
        # An empty response is the caller's problem to report; it is not evidence of a rename.
        out, error = nse._series_from_rows([], "Date", {"TotalReturnsIndex": "tri"})
        self.assertEqual(out, {})
        self.assertIsNone(error)


class TestRateLimitBudget(unittest.TestCase):
    """On 21 Aug 2026 a sustained Telegram 429 cost a whole morning's digest, so the shape of
    this budget is load-bearing: it must span the whole digest, not reset per message."""

    def test_budget_spans_every_chunk_of_one_digest(self):
        # A per-message budget would silently be worth double, because the digest always splits
        # into two chunks -- and could then outlast the job holding it.
        budget = notify.RateLimitBudget(30.0)
        self.assertTrue(budget.take(20.0))      # chunk 1 waits
        self.assertFalse(budget.take(20.0))     # chunk 2 must not get a fresh 20s
        self.assertEqual(budget.spent, 20.0)

    def test_budget_allows_waits_up_to_the_limit(self):
        budget = notify.RateLimitBudget(30.0)
        self.assertTrue(budget.take(30.0))
        self.assertEqual(budget.remaining, 0.0)
        self.assertFalse(budget.take(0.1))

    def test_default_budget_fits_inside_the_job(self):
        # The job allows a 180-minute wait plus the work; the 429 budget must be a rounding
        # error against that, not a second timeout.
        self.assertLessEqual(notify.RATE_LIMIT_PATIENCE, 600.0)
        self.assertGreaterEqual(notify.RATE_LIMIT_PATIENCE, 120.0)


class TestAmfiLayout(unittest.TestCase):
    """AMFI reordered this report's columns on 19 Aug 2026, which froze every fund NAV in the
    digest for three weeks. Both layouts are eight fields wide and both start with the scheme
    code, so nothing but the header names distinguishes them -- which is why the parser reads
    the header instead of counting fields."""

    OLD = (
        "Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;"
        "Net Asset Value;Repurchase Price;Sale Price;Date"
    )
    NEW = (
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
        "ISIN Div Reinvestment;Net Asset Value;Date"
    )

    def test_layout_before_the_change(self):
        columns = amfi.resolve_columns(self.OLD)
        self.assertIsNotNone(columns)
        self.assertEqual((columns.isin_growth, columns.nav, columns.date), (2, 4, 7))

    def test_layout_after_the_change(self):
        columns = amfi.resolve_columns(self.NEW)
        self.assertIsNotNone(columns)
        self.assertEqual((columns.isin_growth, columns.nav, columns.date), (4, 6, 7))

    def test_a_field_count_check_could_not_have_caught_it(self):
        self.assertEqual(len(self.OLD.split(";")), len(self.NEW.split(";")))

    def test_unrecognised_header_is_refused_rather_than_guessed(self):
        self.assertIsNone(amfi.resolve_columns("<html><head><title>Error</title>"))
        self.assertIsNone(amfi.resolve_columns(""))
        self.assertIsNone(amfi.resolve_columns("148063;Edelweiss;39.7487;04-Sep-2026"))

    def test_header_without_an_isin_column_is_refused(self):
        # Scheme codes are too dense to trust unaided: 140088 is Gold BeES and 140089 is Nifty
        # PSU Bank BeES, so a row must prove its identity with an ISIN or not be believed.
        self.assertIsNone(amfi.resolve_columns("Scheme Code;NAV Name;Net Asset Value;Date"))

    def test_real_row_parses_under_the_current_layout(self):
        row = (
            "148063;Edelweiss US Technology Equity Fund of Fund- Direct Plan- Growth;"
            "Direct Plan;Growth;INF754K01LB7;;39.7487;04-Sep-2026"
        )
        columns = amfi.resolve_columns(self.NEW)
        parts = row.split(";")
        self.assertEqual(parts[columns.isin_growth], "INF754K01LB7")
        self.assertEqual(parse_float(parts[columns.nav]), 39.7487)
        self.assertEqual(parse_ddmmmyyyy(parts[columns.date]), D(2026, 9, 4))

    def test_isin_is_accepted_from_either_isin_column(self):
        columns = amfi.resolve_columns(self.NEW)
        self.assertEqual(columns.isin_at, (4, 5))
        self.assertEqual(columns.width, 8)

    def test_precise_label_wins_over_the_looser_one(self):
        # Column lookup must be label-major. Searching fields first would match the loose "nav"
        # against a column called "NAV" at index 1 and return a scheme name as the price.
        header = (
            "Scheme Code;NAV;Plan;Option;ISIN Div Payout/ISIN Growth;"
            "ISIN Div Reinvestment;Net Asset Value;Date"
        )
        columns = amfi.resolve_columns(header)
        self.assertIsNotNone(columns)
        self.assertEqual(columns.nav, 6)

    def test_empty_option_column_does_not_shift_anything(self):
        # An adjacent double semicolon looks malformed but is just an empty Option field.
        row = "140088;Nippon India ETF Gold BeES;Direct Plan;;INF204KB17I5;;125.3986;08-Sep-2026"
        columns = amfi.resolve_columns(self.NEW)
        parts = row.split(";")
        self.assertEqual(len(parts), 8)
        self.assertEqual(parts[columns.isin_growth], "INF204KB17I5")
        self.assertEqual(parse_float(parts[columns.nav]), 125.3986)

    def test_spacing_variants_still_resolve(self):
        spaced = (
            "Scheme Code ; NAV Name ; Plan ; Option ; ISIN Div Payout/ISIN Growth ; "
            "ISIN Div Reinvestment ; Net Asset Value ; Date"
        )
        columns = amfi.resolve_columns(spaced)
        self.assertIsNotNone(columns)
        self.assertEqual((columns.isin_growth, columns.nav, columns.date), (4, 6, 7))


class TestHeadlineDayMove(unittest.TestCase):
    """The arrow beside the headline price must belong to that price, on that day.

    Two separate ways this has gone wrong, both shipped, both caught by eye rather than by a
    test:

      * Borrowing the table's 1D. The table may be computed on a fund NAV from an earlier
        session, so on 8 Sep 2026 the silver block printed "23.46 -1.41%" on a day the price
        had risen 0.47%.
      * Fixing that, then resolving "the previous close" through as_of's twelve-day default
        slack. The two NSE TRI indices stored only three price points, so Midcap 150 compared
        8 Sep against 28 Aug and printed -1.77% where the real day move was +0.12%.

    A wrong percentage in the headline is the worst defect this project can ship: it is the
    number a reader takes away, and it looks perfectly ordinary.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.today = D(2026, 9, 9)
        self.spec = InstrumentSpec(
            key="tri_index", display="AN INDEX TRI", basis="tri",
            basis_label="TRI (total return)", has_pe=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot(self, level_dates):
        """A full daily TRI series, but levels only on the dates given."""
        s = Series.load("tri_index", self.root)
        day = D(2025, 9, 1)
        while day <= D(2026, 9, 8):
            if day.weekday() < 5:
                s.upsert(day, {"tri": 29000.0})
            day += dt.timedelta(days=1)
        for when, value in level_dates.items():
            s.upsert(when, {"level": value})
        s.upsert(D(2026, 9, 8), {"tri": 29511.78})
        result = FetchResult()
        result.add("tri", 29511.78, as_of=D(2026, 9, 8), freshness=FRESHNESS_PREV_CLOSE)
        result.add("level", 23089.95, as_of=D(2026, 9, 8), freshness=FRESHNESS_PREV_CLOSE)
        return build_snapshot(self.spec, s, result, self.today)

    def test_the_day_move_uses_the_previous_session(self):
        snap = self._snapshot({D(2026, 9, 7): 23062.25, D(2026, 9, 8): 23089.95})
        self.assertIsNotNone(snap.level_day)
        self.assertAlmostEqual(snap.level_day.pct, 0.120, places=2)

    def test_a_gap_in_the_price_series_yields_no_day_move_at_all(self):
        # 28 Aug is the only prior level held -- eleven days back. That is not a day move, and
        # the honest output is no percentage rather than a plausible-looking wrong one.
        snap = self._snapshot({D(2026, 8, 28): 23507.15, D(2026, 9, 8): 23089.95})
        self.assertIsNone(snap.level_day)

    def test_a_long_weekend_is_still_a_day_move(self):
        # Thursday close to the following Tuesday: five days, the worst normal case.
        snap = self._snapshot({D(2026, 9, 3): 23220.5, D(2026, 9, 8): 23089.95})
        self.assertIsNotNone(snap.level_day)

    def test_the_renderer_never_borrows_the_tables_1D_for_the_price(self):
        snap = self._snapshot({D(2026, 8, 28): 23507.15, D(2026, 9, 8): 23089.95})
        snap.changes["1D"] = Change(label="1D", pct=0.12)   # the TRI's move, not the price's
        head = fmt._headline(snap, dt.datetime(2026, 9, 9, 11, 11))[0]
        self.assertIn("23,089.95", head)
        self.assertNotIn("0.12", head)
        self.assertNotIn("%", head)

    def test_an_index_whose_table_runs_on_price_still_shows_its_1D(self):
        # basis == "level": the table's 1D IS the price's own move, so it must survive.
        spec = InstrumentSpec(key="plain", display="PLAIN", basis="level", has_pe=False)
        s = Series.load("plain", self.root)
        s.upsert(D(2026, 9, 7), {"level": 23000.0})
        s.upsert(D(2026, 9, 8), {"level": 23100.0})
        result = FetchResult()
        result.add("level", 23100.0, as_of=D(2026, 9, 8), freshness=FRESHNESS_PREV_CLOSE)
        snap = build_snapshot(spec, s, result, self.today)
        head = fmt._headline(snap, dt.datetime(2026, 9, 9, 11, 11))[0]
        self.assertIn("%", head)


class TestNoteBudget(unittest.TestCase):
    """The renderer's note cap must fit the worst block, not the average one.

    Notes are the only channel the digest has for "this figure is not what it looks like", and
    the renderer shows a fixed number of them. Every time a block has reached that number, the
    line that fell off was a real one -- and it fell off silently, on the exact morning something
    had gone wrong, because a staleness warning is what pushes a block over the edge.

    So the cap is pinned here rather than left as a magic number in format.py: if a future note
    takes any block past it, this fails instead of the digest quietly getting shorter.
    """

    WORST_CASE = [
        "nav last updated 19 Aug — source may have stopped",   # machine: a source stopped
        "price +0.47% on 08 Sep; table is nav to 07 Sep",           # machine: session divergence
        "physically backed; one unit is about a tenth of a gram of silver",   # spec caveat
        "price -0.47% vs iNAV",                                     # machine: premium
        "IBJA 999 silver ₹233,826/kg",                          # machine: cross-check
    ]

    def _render(self, notes):
        snap = Snapshot(key="silver_zerodha", display="SILVER", kind="etf")
        snap.level = Reading(value=23.46, as_of=D(2026, 9, 8), freshness=FRESHNESS_PREV_CLOSE)
        snap.nav = Reading(value=23.32, as_of=D(2026, 9, 7))
        snap.changes = {"1D": Change(label="1D", pct=-1.41)}
        snap.basis_field, snap.change_basis = "nav", "NAV, to 07 Sep close"
        snap.notes = list(notes)
        return fmt.render_snapshot(snap, dt.datetime(2026, 9, 9, 11, 11))

    def test_the_worst_realistic_block_loses_nothing(self):
        rendered = self._render(self.WORST_CASE)
        for note in self.WORST_CASE:
            self.assertIn(note, rendered, f"dropped: {note!r}")

    def test_a_source_has_stopped_warning_is_never_the_line_that_falls_off(self):
        # Even past the cap, the warning must survive: compute.py inserts machine-discovered
        # notes at the front precisely so the casualty is the least urgent line.
        rendered = self._render(self.WORST_CASE + ["a sixth note from some future change"])
        self.assertIn("source may have stopped", rendered)

    def test_an_ordinary_morning_leaves_a_slot_spare(self):
        # Four notes is silver on a normal day. Counting <i> tags would be the wrong measure --
        # the basis line and the headline freshness tag are italic too -- so assert on the notes
        # themselves: all four render, and the cap still has room for the warning that only
        # appears once something has broken.
        ordinary = self.WORST_CASE[1:]
        self.assertEqual(len(ordinary), 4)
        rendered = self._render(ordinary)
        for note in ordinary:
            self.assertIn(note, rendered)


class TestRangePosition(unittest.TestCase):
    """The arithmetic behind "expensive for this instrument" and "off its high".

    These numbers get read as judgements, so the failure to avoid is answering at all when the
    data cannot support an answer -- a percentile over eleven observations, or over a window
    where nothing moved, looks exactly as authoritative as a real one.
    """

    def test_too_few_observations_answers_nothing(self):
        self.assertIsNone(range_position(50.0, [float(v) for v in range(29)]))
        self.assertIsNotNone(range_position(50.0, [float(v) for v in range(30)]))

    def test_a_flat_window_answers_nothing(self):
        # Every observation identical: a percentile here is meaningless, not 100.
        self.assertIsNone(range_position(20.0, [20.0] * 200))

    def test_percentile_counts_observations_at_or_below(self):
        position = range_position(75.0, [float(v) for v in range(1, 101)])
        self.assertEqual(position.percentile, 75.0)
        self.assertEqual((position.low, position.high), (1.0, 100.0))

    def test_distance_from_the_extremes(self):
        position = range_position(90.0, [float(v) for v in range(50, 151)])
        self.assertAlmostEqual(position.off_high_pct, (90 - 150) / 150 * 100, places=6)
        self.assertAlmostEqual(position.above_low_pct, (90 - 50) / 50 * 100, places=6)

    def test_a_value_outside_the_window_is_still_located(self):
        # Today's live price can be above every stored close -- that is what a new high IS.
        position = range_position(160.0, [float(v) for v in range(50, 151)])
        self.assertEqual(position.percentile, 100.0)
        self.assertGreater(position.off_high_pct, 0)

    def test_the_window_reports_its_real_span_not_the_one_requested(self):
        # A series that only goes back four months must not be labelled 1Y.
        points = [(D(2026, 5, 1) + dt.timedelta(days=i), 100.0 + i) for i in range(120)]
        values, first, last = trailing_window(points, D(2026, 9, 9))
        self.assertEqual((first, last), (D(2026, 5, 1), D(2026, 8, 28)))
        self.assertFalse(range_position(150.0, values).spans_a_year)

    def test_the_window_excludes_anything_after_the_anchor(self):
        points = [(D(2026, 9, 8), 1.0), (D(2026, 9, 30), 2.0)]
        values, _first, last = trailing_window(points, D(2026, 9, 9))
        self.assertEqual(values, [1.0])
        self.assertEqual(last, D(2026, 9, 8))


class TestContextLines(unittest.TestCase):
    """The three ways a block can express where its headline sits, and when it says nothing."""

    def _year(self, values):
        """A full year of dated observations, so the window is honestly labelled 1Y.

        Built through trailing_window rather than handed to range_position bare: an undated
        window reports its own point count instead ("101d"), which is correct behaviour and not
        what these tests are about.
        """
        points = [(D(2025, 9, 10) + dt.timedelta(days=i), v) for i, v in enumerate(values)]
        window, first, last = trailing_window(points, D(2026, 9, 9))
        return window, first, last

    def _snapshot(self, lead_range, *, values=None, fx=None):
        snap = Snapshot(key="k", display="K", lead_range=lead_range)
        snap.level = Reading(value=90.0, as_of=D(2026, 9, 8), freshness=FRESHNESS_PREV_CLOSE)
        snap.changes = {"1D": Change(label="1D", pct=0.1)}
        values = values if values is not None else [float(v % 101 + 50) for v in range(300)]
        window, first, last = self._year(values)
        snap.lead_position = range_position(90.0, window, first=first, last=last)
        snap.fx = fx
        return snap

    def test_distance_is_the_default_framing(self):
        line = " ".join(fmt._context(self._snapshot("distance")))
        self.assertIn("40.0% off the 1Y high", line)
        self.assertIn("+80.0% above the low", line)

    def test_a_new_high_is_named_rather_than_shown_as_zero(self):
        # "0.0% off the high" reads as a rounding artefact; at the high, say so.
        snap = self._snapshot("distance", values=[float(v % 90 + 1) for v in range(300)])
        self.assertIn("at its 1Y high", " ".join(fmt._context(snap)))

    def test_percentile_framing_for_a_mean_reverting_series(self):
        line = " ".join(fmt._context(self._snapshot("percentile")))
        self.assertIn("percentile", line)
        self.assertNotIn("off the", line)

    def test_none_framing_stays_silent(self):
        self.assertEqual(fmt._context(self._snapshot("none")), [])

    def test_a_short_window_is_not_called_a_year(self):
        snap = Snapshot(key="k", display="K")
        snap.level = Reading(value=90.0, as_of=D(2026, 9, 8))
        points = [(D(2026, 5, 1) + dt.timedelta(days=i), 50.0 + i) for i in range(120)]
        values, first, last = trailing_window(points, D(2026, 9, 9))
        snap.lead_position = range_position(90.0, values, first=first, last=last)
        line = " ".join(fmt._context(snap))
        self.assertIn("4M", line)
        self.assertNotIn("1Y", line)

    def test_the_rupee_line_appears_only_where_it_was_attached(self):
        rate = FxRate(rate=94.81, as_of=D(2026, 9, 8), pct_1y=7.51)
        self.assertIn("USD/INR 94.81", " ".join(fmt._context(self._snapshot("distance", fx=rate))))
        self.assertNotIn("USD/INR", " ".join(fmt._context(self._snapshot("distance"))))

    def test_the_rupee_line_omits_a_move_it_could_not_measure(self):
        rate = FxRate(rate=94.81, as_of=D(2026, 9, 8), pct_1y=None)
        line = " ".join(fmt._context(self._snapshot("distance", fx=rate)))
        self.assertIn("USD/INR 94.81", line)
        self.assertNotIn("1Y", line.split("USD/INR")[1])


class TestPlausibilityBands(unittest.TestCase):
    """Both new sources are Yahoo symbols, so both must refuse a number that is not theirs.

    A delisted or reused ticker answers HTTP 200 with a real-looking figure -- the documented
    hazard in yahoo.py -- and a volatility gauge printing 300, or a rupee rate printing 9.4,
    would be read at face value.
    """

    def test_india_vix_band_brackets_every_real_reading(self):
        low, high = vix.PLAUSIBLE
        self.assertLess(low, 8.5)        # the calmest close on record
        self.assertGreater(high, 87.0)   # the March 2020 spike
        self.assertTrue(low <= 11.16 <= high)

    def test_a_decimal_shift_in_vix_falls_outside_the_band(self):
        low, high = vix.PLAUSIBLE
        self.assertFalse(low <= 1.116 <= high)
        self.assertFalse(low <= 1116.0 <= high)

    def test_the_rupee_band_rejects_the_inverted_pair(self):
        low, high = fx_src.PLAUSIBLE
        self.assertTrue(low <= 94.81 <= high)
        # INR/USD instead of USD/INR is the mistake that would still look like a rate.
        self.assertFalse(low <= 1 / 94.81 <= high)


class TestLeadIsSingleSourced(unittest.TestCase):
    """compute measures the 52-week range on whatever format prints. One rule, one place."""

    def test_an_etf_leads_on_price(self):
        snap = Snapshot(key="k", display="K", kind="etf")
        snap.level = Reading(value=23.46)
        snap.nav = Reading(value=23.32)
        self.assertEqual(snap.lead[1], "level")

    def test_a_fund_with_no_price_leads_on_nav(self):
        snap = Snapshot(key="k", display="K", kind="fund")
        snap.nav = Reading(value=39.75)
        self.assertEqual(snap.lead[1], "nav")

    def test_tri_leads_when_there_is_no_level(self):
        snap = Snapshot(key="k", display="K")
        snap.tri = Reading(value=23661.28)
        self.assertEqual(snap.lead[1], "tri")

    def test_the_renderer_agrees_with_the_model(self):
        for kind, field, value in (("etf", "level", 23.46), ("fund", "nav", 39.75)):
            snap = Snapshot(key="k", display="K", kind=kind)
            setattr(snap, field, Reading(value=value, as_of=D(2026, 9, 8)))
            snap.changes = {"1D": Change(label="1D", pct=0.5)}
            head = fmt._headline(snap, dt.datetime(2026, 9, 9, 11, 11))[0]
            self.assertIn(f"{value:,.2f}", head)


class TestRangeAnchors(unittest.TestCase):
    """Each range window ends at its own series' newest close, and an instrument can decline."""

    def _series(self) -> Series:
        s = Series("k", Path("unused.json"))
        day = D(2025, 8, 1)
        while day <= D(2026, 9, 7):
            if day.weekday() < 5:
                n = day.toordinal()
                s.upsert(day, {"tri": 100.0 + n % 50, "level": 80.0 + n % 40, "pe": 20.0 + n % 10})
            day += dt.timedelta(days=1)
        # The price has closed one more session than the TRI has published.
        s.upsert(D(2026, 9, 8), {"level": 999.0})
        return s

    def test_the_price_window_ends_at_the_price_series_not_the_basis_anchor(self):
        snap = Snapshot(key="k", display="K")
        snap.level = Reading(value=999.0, as_of=D(2026, 9, 8), freshness=FRESHNESS_PREV_CLOSE)
        _locate_in_range(snap, self._series(), D(2026, 9, 7))   # the TRI anchor, a day behind
        self.assertEqual(snap.lead_position.last, D(2026, 9, 8))
        self.assertEqual(snap.lead_position.high, 999.0)
        self.assertIn("at its 1Y high", " ".join(fmt._context(snap)))

    def test_a_series_with_nothing_stored_falls_back_to_the_anchor(self):
        snap = Snapshot(key="k", display="K")
        snap.level = Reading(value=1.0, as_of=D(2026, 9, 8))
        _locate_in_range(snap, Series("k", Path("unused.json")), D(2026, 9, 7))
        self.assertIsNone(snap.lead_position)

    def test_an_instrument_can_decline_the_pe_percentile(self):
        s = self._series()
        for pe_range in (True, False):
            snap = Snapshot(key="k", display="K", pe_range=pe_range)
            snap.level = Reading(value=90.0, as_of=D(2026, 9, 8))
            snap.pe = Reading(value=25.0, as_of=D(2026, 9, 7))
            _locate_in_range(snap, s, D(2026, 9, 7))
            self.assertEqual(snap.pe_position is not None, pe_range)
            self.assertIsNotNone(snap.lead_position)   # declining PE never silences the price

    def test_bse_is_the_one_instrument_that_declines(self):
        specs = {r.spec.key: r.spec for r in build_registry()}
        self.assertFalse(specs["bse_250_smallcap"].pe_range)
        for key, spec in specs.items():
            if key != "bse_250_smallcap":
                self.assertTrue(spec.pe_range, key)


class TestFxDecorationIsAdditive(unittest.TestCase):
    """A rupee problem may cost the reader the rupee line, never the block it decorates."""

    def test_a_failing_rate_fetch_leaves_the_block_intact(self):
        base = FetchResult()
        base.add("nav", 23.4, as_of=D(2026, 9, 8), source="test")

        class Exploding:
            def rate(self, http):
                raise RuntimeError("yahoo changed shape")

        spec = InstrumentSpec(key="k", display="K")
        wrapped = with_fx(lambda http, series, spec, today: base, Exploding())
        with self.assertLogs("bot.instruments", level="WARNING"):
            result = wrapped(None, None, spec, D(2026, 9, 8))
        self.assertIs(result, base)
        self.assertIsNone(result.fx)
        self.assertTrue(result.readings["nav"].known)

    def test_a_working_rate_is_attached(self):
        base = FetchResult()

        class Steady:
            def rate(self, http):
                return FxRate(rate=94.81, as_of=D(2026, 9, 8), pct_1y=7.5)

        wrapped = with_fx(lambda http, series, spec, today: base, Steady())
        result = wrapped(None, None, InstrumentSpec(key="k", display="K"), D(2026, 9, 8))
        self.assertEqual(result.fx.rate, 94.81)


if __name__ == "__main__":
    unittest.main(verbosity=2)


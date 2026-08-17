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
from bot.compute import FetchResult, InstrumentSpec, build_snapshot, persist
from bot.ledger import NewsLedger, SendLedger
from bot.model import FRESHNESS_LIVE, FRESHNESS_PREV_CLOSE, Change, Digest, Reading
from bot.sources import bse, gsr, nse
from bot.state import Series, Store
from bot.util import (
    from_iso,
    lookback_targets,
    nearest_on_or_before,
    parse_date,
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
        persist(result, s, self.today)
        self.assertEqual(len(s), 0)

    def test_final_values_are_persisted(self):
        s = Series.load("demo", self.root)
        result = FetchResult()
        result.add("tri", 456.0, as_of=D(2026, 8, 17), freshness=FRESHNESS_PREV_CLOSE)
        persist(result, s, self.today)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)


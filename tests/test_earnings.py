"""Tests for opptions.earnings. No network: everything runs on hand-built fixtures.

Fixtures (synthetic company ACME, shapes as returned by each source):
  earnings_nasdaq_date.json      expected* to report 10/22/2026 (a Thursday) after market close
  earnings_nasdaq_nodate.json    Zacks "hasn't provided us with the upcoming earnings report date"
  earnings_nasdaq_surprise.json  5 quarters, newest first; one row uses "$" strings
  earnings_chain.json            Cboe chain as of 2026-10-01 10:15, spot 152.40, expiries
                                 10/16 (IV ~40%), 10/23 (earnings week, IV ~58%), 10/30 (IV ~50%),
                                 11/20 (IV ~45%); 10/23 has a call-only 152.5 strike
  earnings_sec_submissions.json  EDGAR submissions; newest 8-K with item 2.02 filed 2026-07-23

fetched_at is 2026-10-01T14:30:00+00:00 (10:30 US Eastern), so the report is 21 days away.
"""

import copy
import json
import os
import random
import unittest
from datetime import date
from unittest import mock

from opptions import earnings
from opptions.http import FetchError

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FETCHED_AT = "2026-10-01T14:30:00+00:00"


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def make_raw(**overrides):
    raw = {
        "symbol": "ACME",
        "fetched_at": FETCHED_AT,
        "nasdaq_date": load("earnings_nasdaq_date.json"),
        "nasdaq_surprise": load("earnings_nasdaq_surprise.json"),
        "chain": load("earnings_chain.json"),
        "sec_submissions": load("earnings_sec_submissions.json"),
        "errors": {},
    }
    raw.update(overrides)
    return raw


def with_report_text(text):
    payload = load("earnings_nasdaq_date.json")
    payload["data"]["reportText"] = text
    payload["data"]["announcement"] = "Earnings announcement* for ACME: "
    return payload


class ParsingTest(unittest.TestCase):
    def test_parse_date_formats(self):
        self.assertEqual(earnings.parse_date("on  10/22/2026 after"), date(2026, 10, 22))
        self.assertEqual(earnings.parse_date("7/3/2026"), date(2026, 7, 3))
        self.assertEqual(earnings.parse_date("2026-10-22"), date(2026, 10, 22))
        self.assertEqual(earnings.parse_date("Earnings announcement* for NVDA: Nov 19, 2025"),
                         date(2025, 11, 19))
        self.assertEqual(earnings.parse_date("Sept 3, 2026"), date(2026, 9, 3))
        self.assertEqual(earnings.parse_date("January 28 2027"), date(2027, 1, 28))
        self.assertIsNone(earnings.parse_date("Earnings announcement* for SPY: "))
        self.assertIsNone(earnings.parse_date("13/45/2026"))
        self.assertIsNone(earnings.parse_date(None))

    def test_parse_timing(self):
        self.assertEqual(earnings.parse_timing("on 10/22/2026 after market close."), "after close")
        self.assertEqual(earnings.parse_timing("after the close"), "after close")
        self.assertEqual(earnings.parse_timing("on 11/12/2019 before market open."), "before open")
        self.assertEqual(earnings.parse_timing("prior to market open"), "before open")
        self.assertEqual(earnings.parse_timing("pre-market"), "before open")
        self.assertEqual(earnings.parse_timing("is estimated to report earnings on 10/20/2026. The"),
                         "unknown")

    def test_to_number(self):
        self.assertEqual(earnings.to_number(1.38), 1.38)
        self.assertEqual(earnings.to_number("1.31"), 1.31)
        self.assertEqual(earnings.to_number("$1.45"), 1.45)
        self.assertEqual(earnings.to_number("-$0.05"), -0.05)
        self.assertEqual(earnings.to_number("$-0.05"), -0.05)
        self.assertEqual(earnings.to_number("($0.05)"), -0.05)
        self.assertEqual(earnings.to_number("5.3%"), 5.3)
        for empty in ("N/A", "--", "", None, float("nan")):
            self.assertIsNone(earnings.to_number(empty))

    def test_next_date_from_fixture(self):
        nxt = earnings.parse_next_date(load("earnings_nasdaq_date.json"))
        self.assertEqual(nxt["date"], "2026-10-22")
        self.assertEqual(nxt["timing"], "after close")
        self.assertEqual(nxt["wording"], "expected")
        self.assertEqual(nxt["fiscal_quarter"], "Sep 2026")
        self.assertEqual(nxt["consensus_eps"], 1.42)
        self.assertEqual(nxt["analysts"], 8)
        self.assertEqual(nxt["year_ago_eps"], 1.10)
        self.assertFalse(nxt["sources_disagree"])

    def test_announcement_only_and_negative_consensus(self):
        payload = {"data": {"reportText": "", "announcement": "Earnings announcement* for NVDA: Nov 19, 2025"}}
        nxt = earnings.parse_next_date(payload)
        self.assertEqual(nxt["date"], "2025-11-19")
        self.assertEqual(nxt["timing"], "unknown")
        payload = {"data": {"reportText": "X is estimated to report earnings on 11/03/2026 before market "
                                          "open. ... the consensus EPS forecast for the quarter is -$0.12."}}
        nxt = earnings.parse_next_date(payload)
        self.assertEqual((nxt["date"], nxt["timing"], nxt["wording"]), ("2026-11-03", "before open", "estimated"))
        self.assertEqual(nxt["consensus_eps"], -0.12)

    def test_no_date(self):
        nxt = earnings.parse_next_date(load("earnings_nasdaq_nodate.json"))
        self.assertIsNone(nxt["date"])
        self.assertEqual(nxt["timing"], "unknown")
        self.assertIsNone(nxt["wording"])

    def test_eastern_date(self):
        # EDT (UTC-4) in October; a late-evening ET fetch is still the same ET date.
        self.assertEqual(str(earnings.eastern_now("2026-10-01T14:30:00+00:00")), "2026-10-01 10:30:00")
        self.assertEqual(earnings.eastern_now("2026-10-02T03:00:00Z").date(), date(2026, 10, 1))
        # EST (UTC-5) in December; naive timestamps are read as UTC.
        self.assertEqual(str(earnings.eastern_now("2026-12-01T04:30:00")), "2026-11-30 23:30:00")
        # DST boundaries 2026: starts Sun Mar 8 07:00 UTC, ends Sun Nov 1 06:00 UTC.
        self.assertEqual(earnings.eastern_now("2026-03-08T06:59:00Z").hour, 1)
        self.assertEqual(earnings.eastern_now("2026-03-08T07:00:00Z").hour, 3)
        self.assertEqual(str(earnings.eastern_now("2026-11-01T05:59:00Z")), "2026-11-01 01:59:00")
        self.assertEqual(str(earnings.eastern_now("2026-11-01T06:00:00Z")), "2026-11-01 01:00:00")
        self.assertIsNone(earnings.eastern_now("not a time"))


class ExpiryChoiceTest(unittest.TestCase):
    EXP = [date(2026, 10, 16), date(2026, 10, 23), date(2026, 10, 30)]

    def test_after_close_on_expiry_day_uses_next_expiry(self):
        self.assertEqual(earnings.pick_earnings_expiry(self.EXP, date(2026, 10, 23), "after close"),
                         date(2026, 10, 30))

    def test_before_open_on_expiry_day_uses_same_day(self):
        self.assertEqual(earnings.pick_earnings_expiry(self.EXP, date(2026, 10, 23), "before open"),
                         date(2026, 10, 23))

    def test_unknown_timing_is_strict(self):
        self.assertEqual(earnings.pick_earnings_expiry(self.EXP, date(2026, 10, 23), "unknown"),
                         date(2026, 10, 30))

    def test_mid_week_report(self):
        self.assertEqual(earnings.pick_earnings_expiry(self.EXP, date(2026, 10, 22), "after close"),
                         date(2026, 10, 23))
        self.assertEqual(earnings.pick_earnings_expiry(self.EXP, date(2026, 10, 22), "before open"),
                         date(2026, 10, 23))

    def test_no_expiry_after_date(self):
        self.assertIsNone(earnings.pick_earnings_expiry(self.EXP, date(2026, 10, 30), "after close"))


class FullAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = earnings.analyze(make_raw())

    def test_next_earnings(self):
        nxt = self.res["next_earnings"]
        self.assertEqual(self.res["date_status"], "found")
        self.assertEqual(nxt["date"], "2026-10-22")
        self.assertEqual(nxt["timing"], "after close")
        self.assertEqual(nxt["days_until"], 21)
        self.assertEqual(self.res["as_of_date_et"], "2026-10-01")

    def test_straddle_math(self):
        mv = self.res["implied_move"]
        self.assertEqual(mv["basis"], "earnings")
        self.assertEqual(mv["expiry"], "2026-10-23")          # first expiry after a 10/22 after-close report
        self.assertEqual(mv["dte"], 22)
        # 152.5 is nearest spot but has no put in 10/23, so the ATM strike is 150 (2.4 away vs 2.6).
        self.assertEqual(mv["atm_strike"], 150.0)
        self.assertAlmostEqual(mv["call_mid"], 10.00)
        self.assertAlmostEqual(mv["put_mid"], 7.50)
        self.assertAlmostEqual(mv["straddle"], 17.50)
        self.assertAlmostEqual(mv["move_pct"], round(17.50 / 152.40 * 100, 2))   # 11.48
        self.assertAlmostEqual(mv["range_low"], 134.90)
        self.assertAlmostEqual(mv["range_high"], 169.90)
        self.assertEqual(mv["spot"], 152.40)

    def test_iv_difference(self):
        mv = self.res["implied_move"]
        self.assertAlmostEqual(mv["atm_iv"], (0.585 + 0.575) / 2)       # 0.58
        self.assertEqual(mv["next_expiry"], "2026-10-30")
        self.assertAlmostEqual(mv["next_atm_iv"], (0.505 + 0.495) / 2)  # 0.50
        self.assertAlmostEqual(mv["iv_diff"], 0.08, places=6)

    def test_surprise_history(self):
        hist = self.res["surprise_history"]
        self.assertEqual(len(hist), 4)
        self.assertEqual([h["reported"] for h in hist],
                         ["2026-07-23", "2026-04-23", "2026-01-28", "2025-10-23"])
        self.assertEqual([h["result"] for h in hist], ["beat", "miss", "beat", "beat"])
        self.assertEqual(hist[2]["actual_eps"], 1.52)        # "$1.52" string parsed
        self.assertEqual(hist[2]["consensus_eps"], 1.45)
        self.assertEqual(hist[1]["surprise_pct"], -3.85)
        self.assertEqual((self.res["beats"], self.res["quarters_compared"]), (3, 4))

    def test_surprise_order_and_computed_pct(self):
        payload = load("earnings_nasdaq_surprise.json")
        rows = payload["data"]["earningsSurpriseTable"]["rows"]
        rows[1]["percentageSurprise"] = "N/A"          # Mar 2026: 1.25 vs 1.30
        rows[1]["consensusForecast"] = "$1.30"
        random.Random(7).shuffle(rows)
        hist = earnings.parse_surprises(payload)
        self.assertEqual([h["reported"] for h in hist],
                         ["2026-07-23", "2026-04-23", "2026-01-28", "2025-10-23"])
        self.assertAlmostEqual(hist[1]["surprise_pct"], round((1.25 - 1.30) / 1.30 * 100, 2))   # -3.85

    def test_latest_release(self):
        rel = self.res["latest_release"]
        self.assertEqual(rel["filed"], "2026-07-23")
        self.assertEqual(rel["items"], ["2.02", "9.01"])
        self.assertEqual(rel["url"], "https://www.sec.gov/Archives/edgar/data/1234567/"
                                     "000123456726000045/acme-20260723.htm")
        self.assertEqual(rel["days_since"], 70)
        self.assertFalse(self.res["just_reported"])
        # Two days after the filing it counts as just reported.
        later = earnings.latest_release(load("earnings_sec_submissions.json"), date(2026, 7, 25))
        self.assertEqual(later["days_since"], 2)

    def test_json_serializable_and_pure(self):
        text = json.dumps(self.res)
        with mock.patch("opptions.http.urllib.request.urlopen", side_effect=AssertionError("network")):
            again = earnings.analyze(make_raw())
        self.assertEqual(json.dumps(again), text)

    def test_markdown(self):
        md = earnings.to_markdown(self.res)
        self.assertTrue(md.startswith("## Earnings: ACME"))
        self.assertIn("Thu Oct 22, 2026, after close (in 21 days)", md)
        self.assertIn("Options are pricing about an 11.5% move either way", md)
        self.assertIn("+/-11.48% (+/-$17.50)", md)
        self.assertIn("$134.90 to $169.90", md)
        self.assertIn("+8.0 pts", md)
        self.assertIn("IV crush", md)
        self.assertIn("| Jun 2026 | 2026-07-23 | $1.31 | $1.38 | +5.34% | beat |", md)
        self.assertIn("Beat the consensus in 3 of the last 4 quarters", md)
        self.assertIn("(https://www.sec.gov/Archives/edgar/data/1234567/000123456726000045/acme-20260723.htm)", md)
        self.assertIn("Motley Fool", md)
        self.assertIn("not gathered by this script", md)
        lowered = md.lower()
        for word in (" buy ", " sell ", "you should"):
            self.assertNotIn(word, lowered)


class TimingVariantsTest(unittest.TestCase):
    def test_before_open_on_expiry_day(self):
        raw = make_raw(nasdaq_date=with_report_text(
            "Acme is expected* to report earnings on 10/23/2026 before market open."))
        mv = earnings.analyze(raw)["implied_move"]
        self.assertEqual(mv["expiry"], "2026-10-23")
        self.assertEqual(mv["next_expiry"], "2026-10-30")

    def test_after_close_on_expiry_day(self):
        raw = make_raw(nasdaq_date=with_report_text(
            "Acme is expected* to report earnings on 10/23/2026 after market close."))
        mv = earnings.analyze(raw)["implied_move"]
        self.assertEqual(mv["expiry"], "2026-10-30")
        self.assertAlmostEqual(mv["straddle"], 9.90 + 7.30)
        self.assertEqual(mv["next_expiry"], "2026-11-20")
        self.assertAlmostEqual(mv["iv_diff"], 0.50 - 0.45, places=6)

    def test_unknown_timing_note(self):
        raw = make_raw(nasdaq_date=with_report_text("Acme is estimated to report earnings on 10/23/2026."))
        res = earnings.analyze(raw)
        self.assertEqual(res["implied_move"]["expiry"], "2026-10-30")
        self.assertTrue(any("timing is unknown" in n for n in res["notes"]))

    def test_sources_disagree(self):
        payload = load("earnings_nasdaq_date.json")
        payload["data"]["announcement"] = "Earnings announcement* for ACME: Oct 29, 2026"
        res = earnings.analyze(make_raw(nasdaq_date=payload))
        self.assertEqual(res["next_earnings"]["date"], "2026-10-22")
        self.assertTrue(any("investor-relations" in n for n in res["notes"]))

    def test_past_date_is_not_upcoming(self):
        res = earnings.analyze(make_raw(fetched_at="2026-10-26T15:00:00Z"))
        self.assertEqual(res["date_status"], "passed")
        self.assertIsNone(res["next_earnings"])
        self.assertEqual(res["stale_listed_date"], "2026-10-22")
        self.assertEqual(res["implied_move"]["basis"], "front_expiry")
        self.assertIn("not announced yet", earnings.to_markdown(res))


class FrontExpiryTest(unittest.TestCase):
    def test_no_date_uses_front_expiry(self):
        res = earnings.analyze(make_raw(nasdaq_date=load("earnings_nasdaq_nodate.json")))
        self.assertEqual(res["date_status"], "none_listed")
        self.assertIsNone(res["next_earnings"])
        mv = res["implied_move"]
        self.assertEqual(mv["basis"], "front_expiry")
        self.assertEqual(mv["expiry"], "2026-10-16")
        self.assertAlmostEqual(mv["straddle"], 6.10 + 3.80)
        self.assertAlmostEqual(mv["move_pct"], round(9.90 / 152.40 * 100, 2))   # 6.50
        self.assertEqual(mv["next_expiry"], "2026-10-23")
        self.assertAlmostEqual(mv["iv_diff"], 0.40 - 0.58, places=6)
        md = earnings.to_markdown(res)
        self.assertIn("Front-expiry implied move (not an earnings move)", md)
        self.assertIn("No upcoming earnings date", md)
        self.assertIn("no upcoming date listed", md)
        self.assertIn("18.0 points higher, which often means the market expects an event", md)


class PartialFailureTest(unittest.TestCase):
    def test_nasdaq_down(self):
        raw = make_raw(nasdaq_date=None, nasdaq_surprise=None,
                       errors={"nasdaq_date": "could not reach api.nasdaq.com (Forbidden)",
                               "nasdaq_surprise": "could not reach api.nasdaq.com (Forbidden)"})
        res = earnings.analyze(raw)
        self.assertEqual(res["date_status"], "unavailable")
        self.assertEqual(res["implied_move"]["basis"], "front_expiry")
        self.assertEqual(res["surprise_history"], [])
        self.assertEqual(res["latest_release"]["filed"], "2026-07-23")
        md = earnings.to_markdown(res)
        self.assertIn("Next report: unknown", md)
        self.assertIn("The earnings date is unknown", md)
        self.assertIn("Nasdaq earnings date not available: could not reach api.nasdaq.com", md)
        json.dumps(res)

    def test_chain_and_sec_down(self):
        raw = make_raw(chain=None, sec_submissions=None,
                       errors={"chain": "cdn-api.cboe.com answered HTTP 403",
                               "sec_submissions": "could not reach data.sec.gov"})
        res = earnings.analyze(raw)
        self.assertEqual(res["next_earnings"]["date"], "2026-10-22")
        self.assertIsNone(res["implied_move"])
        self.assertIsNone(res["latest_release"])
        md = earnings.to_markdown(res)
        self.assertIn("### Implied move", md)
        self.assertIn("Cboe option chain not available", md)
        self.assertIn("SEC filings not available", md)

    def test_empty_inputs(self):
        res = earnings.analyze({})
        self.assertEqual(res["date_status"], "unavailable")
        self.assertIsNone(res["implied_move"])
        self.assertTrue(earnings.to_markdown(res).startswith("## Earnings: ?"))
        json.dumps(res)

    def test_chain_without_spot_or_timestamp(self):
        chain = load("earnings_chain.json")
        del chain["timestamp"]
        for key in ("current_price", "close", "prev_day_close"):
            chain["data"].pop(key, None)
        for o in chain["data"]["options"]:
            o["delta"] = 0.0          # no delta, so cboe cannot estimate spot either

        class NoToday(date):
            @classmethod
            def today(cls):
                raise AssertionError("analyze read the clock")

        with mock.patch("opptions.cboe.date", NoToday):
            res = earnings.analyze(make_raw(chain=chain))
        self.assertIsNone(res["implied_move"])
        self.assertTrue(any("No usable spot" in n for n in res["notes"]))
        self.assertTrue(any("no timestamp" in n for n in res["notes"]))

    def test_no_priced_pair(self):
        chain = load("earnings_chain.json")
        chain["data"]["options"] = [o for o in chain["data"]["options"]
                                    if not (o["option"].startswith("ACME261023P"))]
        res = earnings.analyze(make_raw(chain=chain))
        self.assertIsNone(res["implied_move"])
        self.assertTrue(any("2026-10-23" in n and "both a priced call and put" in n for n in res["notes"]))

    def test_no_2_02_filing(self):
        subs = load("earnings_sec_submissions.json")
        subs["filings"]["recent"]["items"] = ["" if "2.02" in i else i
                                              for i in subs["filings"]["recent"]["items"]]
        res = earnings.analyze(make_raw(sec_submissions=subs))
        self.assertIsNone(res["latest_release"])
        self.assertTrue(any("item 2.02" in n for n in res["notes"]))


class ReviewRegressionTest(unittest.TestCase):
    """Cases found in review; each failed (or read the clock) before the fix."""

    def test_pre_report_baseline_iv(self):
        mv = earnings.analyze(make_raw())["implied_move"]
        # 10/16 ends before a 10/22 report; its 150 strike: call IV 0.402, put IV 0.398.
        self.assertEqual((mv["pre_expiry"], mv["pre_dte"]), ("2026-10-16", 15))
        self.assertAlmostEqual(mv["pre_atm_iv"], 0.40, places=6)

    def test_pre_report_expiry_follows_timing(self):
        cases = {"after market close.": ("2026-10-30", "2026-10-23", 0.58),   # 10/23 ends before the report
                 "before market open.": ("2026-10-23", "2026-10-16", 0.40),   # 10/23 already includes it
                 "": ("2026-10-30", "2026-10-16", 0.40)}                      # unknown: 10/23 is ambiguous
        for tail, (exp, pre, pre_iv) in cases.items():
            raw = make_raw(nasdaq_date=with_report_text(
                f"Acme is expected* to report earnings on 10/23/2026 {tail}"))
            mv = earnings.analyze(raw)["implied_move"]
            self.assertEqual((mv["expiry"], mv["pre_expiry"]), (exp, pre), tail)
            self.assertAlmostEqual(mv["pre_atm_iv"], pre_iv, places=6)

    def test_crush_wording_is_not_the_iv_gap(self):
        md = earnings.to_markdown(earnings.analyze(make_raw()))
        self.assertNotIn("That extra is the event premium", md)
        self.assertIn("Both expiries include the report", md)
        self.assertIn("| ATM IV, 2026-10-16 (ends before the report) | 40.0% |", md)
        self.assertIn("sits at 40.0% (vs 58.0% for the earnings expiry)", md)
        self.assertLess(md.index("ATM IV, 2026-10-16"), md.index("ATM IV, 2026-10-23"))

    def test_far_expiry_mixes_ordinary_movement(self):
        res = earnings.analyze(make_raw())
        self.assertTrue(res["implied_move"]["mixes_ordinary_movement"])           # 22 DTE
        self.assertIn("22 days of ordinary movement", earnings.to_markdown(res))
        chain = load("earnings_chain.json")
        chain["timestamp"] = "2026-10-20 10:15:00"                                # 10/23 is 3 DTE
        res = earnings.analyze(make_raw(chain=chain, fetched_at="2026-10-20T14:30:00Z"))
        self.assertEqual((res["implied_move"]["dte"], res["next_earnings"]["days_until"]), (3, 2))
        self.assertFalse(res["implied_move"]["mixes_ordinary_movement"])
        self.assertNotIn("ordinary movement", earnings.to_markdown(res))

    def test_date_beyond_listed_expiries_is_not_called_missing(self):
        raw = make_raw(nasdaq_date=with_report_text(
            "Acme is expected* to report earnings on 01/27/2027 after market close."))
        res = earnings.analyze(raw)
        self.assertEqual((res["date_status"], res["implied_move"]["basis"]), ("found", "front_expiry"))
        md = earnings.to_markdown(res)
        self.assertIn("No listed expiry ends after the 2027-01-27 report yet", md)
        self.assertNotIn("No upcoming earnings date", md)

    def test_non_finite_numbers(self):
        for bad in ("NaN", "nan", "inf", "-Infinity", float("inf")):
            self.assertIsNone(earnings.to_number(bad), bad)
        payload = load("earnings_nasdaq_surprise.json")
        payload["data"]["earningsSurpriseTable"]["rows"][0]["eps"] = "NaN"
        row = earnings.parse_surprises(payload)[0]
        self.assertEqual((row["actual_eps"], row["result"]), (None, "n/a"))   # was nan and "in line"
        json.dumps(row, allow_nan=False)

    def test_no_clock_read_anywhere(self):
        from datetime import datetime as real_datetime

        class NoNow(real_datetime):
            @classmethod
            def now(cls, tz=None):
                raise AssertionError("analyze read the clock")

        class NoToday(date):
            @classmethod
            def today(cls):
                raise AssertionError("analyze read the clock")

        chain = load("earnings_chain.json")
        del chain["timestamp"]
        with mock.patch("opptions.earnings.datetime", NoNow), mock.patch("opptions.earnings.date", NoToday), \
                mock.patch("opptions.cboe.date", NoToday):
            res = earnings.analyze(make_raw())
            self.assertEqual(res["implied_move"]["straddle"], 17.5)
            # No chain timestamp and no fetch time: skip the chain rather than fall back to date.today().
            res = earnings.analyze(make_raw(fetched_at=None, chain=chain))
        self.assertIsNone(res["implied_move"])
        self.assertTrue(any("cannot be dated" in n for n in res["notes"]))
        json.dumps(res, allow_nan=False)

    def test_atm_strike_far_from_spot_is_flagged(self):
        chain = load("earnings_chain.json")
        chain["data"]["current_price"] = 165.0          # nearest both-priced 10/23 strike is 155: 6.1% away
        res = earnings.analyze(make_raw(chain=chain))
        self.assertEqual(res["implied_move"]["atm_strike"], 155.0)
        self.assertTrue(any("6.1% from spot" in n for n in res["notes"]))
        self.assertFalse(any("from spot" in n for n in earnings.analyze(make_raw())["notes"]))

    def test_after_close_today_fetched_in_evening(self):
        late = earnings.analyze(make_raw(fetched_at="2026-10-22T21:00:00Z"))      # 17:00 ET on report day
        self.assertEqual(late["next_earnings"]["days_until"], 0)
        self.assertTrue(any("may already be out" in n for n in late["notes"]))
        early = earnings.analyze(make_raw(fetched_at="2026-10-22T18:00:00Z"))     # 14:00 ET
        self.assertFalse(any("may already be out" in n for n in early["notes"]))

    def test_no_comparable_quarters(self):
        payload = load("earnings_nasdaq_surprise.json")
        for row in payload["data"]["earningsSurpriseTable"]["rows"]:
            row["consensusForecast"] = "N/A"
        res = earnings.analyze(make_raw(nasdaq_surprise=payload))
        self.assertEqual((res["beats"], res["quarters_compared"]), (0, 0))
        md = earnings.to_markdown(res)
        self.assertNotIn("0 of the last 0", md)
        self.assertIn("beats cannot be counted", md)


class FetchTest(unittest.TestCase):
    def patches(self, date_=None, surprise=None, chain=None, subs=None):
        def side(value):
            return {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
        return (mock.patch("opptions.earnings.fetch_earnings_date", **side(date_)),
                mock.patch("opptions.earnings.fetch_surprise", **side(surprise)),
                mock.patch("opptions.cboe.fetch_chain", **side(chain)),
                mock.patch("opptions.earnings.fetch_sec_submissions", **side(subs)))

    def test_partial_failure_keeps_other_sources(self):
        p1, p2, p3, p4 = self.patches(date_=FetchError("could not reach api.nasdaq.com (Forbidden)"),
                                      surprise=load("earnings_nasdaq_surprise.json"),
                                      chain=load("earnings_chain.json"),
                                      subs=ValueError("bad json"))
        with p1, p2, p3, p4:
            raw = earnings.fetch("acme")
        self.assertEqual(raw["symbol"], "ACME")
        self.assertIsNone(raw["nasdaq_date"])
        self.assertIsNone(raw["sec_submissions"])
        self.assertIsNotNone(raw["chain"])
        self.assertIsNotNone(raw["nasdaq_surprise"])
        self.assertIn("api.nasdaq.com", raw["errors"]["nasdaq_date"])
        self.assertIn("ValueError", raw["errors"]["sec_submissions"])
        self.assertTrue(raw["fetched_at"].endswith("+00:00"))
        res = earnings.analyze(raw)
        self.assertEqual(res["implied_move"]["basis"], "front_expiry")

    def test_unexpected_error_in_one_source_keeps_the_rest(self):
        import http.client
        p1, p2, p3, p4 = self.patches(date_=http.client.IncompleteRead(b"{"),
                                      surprise=load("earnings_nasdaq_surprise.json"),
                                      chain=load("earnings_chain.json"),
                                      subs=load("earnings_sec_submissions.json"))
        with p1, p2, p3, p4:
            raw = earnings.fetch("ACME")
        self.assertIsNone(raw["nasdaq_date"])
        self.assertIn("IncompleteRead", raw["errors"]["nasdaq_date"])
        self.assertEqual(set(raw["errors"]), {"nasdaq_date"})

    def test_symbol_is_fully_quoted(self):
        with mock.patch("opptions.earnings.fetch_json", return_value=load("earnings_nasdaq_date.json")) as m:
            earnings.fetch_earnings_date("brk/b")
        self.assertEqual(m.call_args[0][0], "https://api.nasdaq.com/api/analyst/BRK%2FB/earnings-date")

    def test_all_failed_raises(self):
        err = FetchError("could not reach host")
        p1, p2, p3, p4 = self.patches(err, err, err, err)
        with p1, p2, p3, p4:
            with self.assertRaises(FetchError):
                earnings.fetch("ACME")

    def test_nasdaq_without_data_is_an_error(self):
        bad = {"data": None, "message": None,
               "status": {"rCode": 400, "bCodeMessage": [{"code": 1001, "errorMessage": "Symbol not exists."}]}}
        with mock.patch("opptions.earnings.fetch_json", return_value=bad) as m:
            with self.assertRaises(FetchError) as ctx:
                earnings.fetch_earnings_date("zzzz")
        self.assertIn("Symbol not exists.", str(ctx.exception))
        url, headers = m.call_args[0]
        self.assertEqual(url, "https://api.nasdaq.com/api/analyst/ZZZZ/earnings-date")
        self.assertEqual(headers["Origin"], "https://www.nasdaq.com")
        self.assertEqual(headers["Referer"], "https://www.nasdaq.com/")
        self.assertIn("Mozilla", headers["User-Agent"])

    def test_surprise_url(self):
        with mock.patch("opptions.earnings.fetch_json", return_value=load("earnings_nasdaq_surprise.json")) as m:
            earnings.fetch_surprise("acme")
        self.assertEqual(m.call_args[0][0], "https://api.nasdaq.com/api/company/ACME/earnings-surprise")

    def test_sec_unknown_ticker(self):
        with mock.patch("opptions.sec.ticker_to_cik", return_value=None):
            with self.assertRaises(FetchError):
                earnings.fetch_sec_submissions("SPY")

    def test_sec_known_ticker(self):
        subs = load("earnings_sec_submissions.json")
        with mock.patch("opptions.sec.ticker_to_cik", return_value=1234567), \
                mock.patch("opptions.sec.fetch_submissions", return_value=subs) as m:
            self.assertIs(earnings.fetch_sec_submissions("ACME"), subs)
        m.assert_called_once_with(1234567)


if __name__ == "__main__":
    unittest.main()

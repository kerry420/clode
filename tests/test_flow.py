"""Tests for the options activity (flow) tracker. No network.

Fixture flow_chain.json (ACME, spot 100, as of 2026-09-30 16:15) hand-checked:

  #  contract              vol   OI    mid   premium   side     unusual
  1  10/02 105C            2000  500   1.15  230,000   ask      yes
  2  10/02  95P            1000  3000  0.85   85,000   bid      no (vol < OI)
  3  10/16 100C             400  300   3.10  124,000   bid      yes
  4  10/16 100P             300  250   3.00   90,000   ask      yes
  5  10/02 100C             600  700   1.55   93,000   mid      no (vol < OI)
  6  10/02 100P             200  900   1.45   29,000   mid      no
  7  11/20 120C               0  4000  0.55        0   stale    no
  8  11/20 110C              50  8000  2.05   10,250   mid      no
  9  11/20  90P             100  6000  1.85   18,500   ask      no
 10  10/02 115C            3000  100   0.06   18,000   ask      no (premium < 50k)
 11  10/02  85P             100  2000  0 -> last 0.02  200  unknown  no
 12  10/16 110C             800  0     1.05   84,000   ask      yes (OI 0)
 13  10/16 105P             249  10    6.10  151,890   mid      no (volume < 250)

  calls: vol 6,850  premium 559,250  OI 13,600
  puts:  vol 1,949  premium 374,590  OI 12,160
  bullish = 230,000 + 18,000 + 84,000 (calls at ask) + 85,000 (put at bid) = 417,000
  bearish = 90,000 + 18,500 (puts at ask) + 124,000 (call at bid)        = 232,500
  417,000 > 1.5 * 232,500 = 348,750 -> bullish
  <= 7 DTE premium = 230,000 + 85,000 + 93,000 + 29,000 + 18,000 + 200 = 455,200
"""

import copy
import json
import math
import os
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from opptions import flow

FIXTURES = Path(__file__).parent / "fixtures"
_ENV = mock.patch.dict(os.environ)


def setUpModule():
    # Keep a user's own OPPTIONS_MIN_* settings from changing the hand-checked results.
    _ENV.start()
    os.environ.pop("OPPTIONS_MIN_VOLUME", None)
    os.environ.pop("OPPTIONS_MIN_PREMIUM", None)


def tearDownModule():
    _ENV.stop()


def load(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


def quote(bid, ask, last, ltt="2026-09-30T15:00:00"):
    return {"bid": bid, "ask": ask, "last": last, "last_trade_time": ltt}


class TestHelpers(unittest.TestCase):
    def test_premium_uses_mid_then_last(self):
        self.assertAlmostEqual(flow.premium({"volume": 2000, "mid": 1.15, "last": 1.2}), 230_000)
        self.assertAlmostEqual(flow.premium({"volume": 100, "mid": 0.0, "last": 0.02}), 200)

    def test_side_thresholds(self):
        s = "2026-09-30"
        # spread 0.10, band 0.01: ask zone >= 1.19, bid zone <= 1.11
        self.assertEqual(flow.estimate_side(quote(1.10, 1.20, 1.20), s), "ask")
        self.assertEqual(flow.estimate_side(quote(1.10, 1.20, 1.195), s), "ask")
        self.assertEqual(flow.estimate_side(quote(1.10, 1.20, 1.25), s), "ask")
        self.assertEqual(flow.estimate_side(quote(1.10, 1.20, 1.105), s), "bid")
        self.assertEqual(flow.estimate_side(quote(1.10, 1.20, 1.05), s), "bid")
        self.assertEqual(flow.estimate_side(quote(1.10, 1.20, 1.15), s), "mid")
        self.assertEqual(flow.estimate_side(quote(1.10, 1.20, 1.18), s), "mid")

    def test_side_exact_band_edges_with_cent_prices(self):
        # The band edge is inclusive. In floating point 5.10 + 0.1 * 0.10 is
        # 5.10999..., which used to push a 5.11 print to "mid".
        s = "2026-09-30"
        for bid, ask in ((5.10, 5.20), (1.10, 1.20), (0.05, 0.15), (12.30, 12.40), (0.30, 0.40)):
            self.assertEqual(flow.estimate_side(quote(bid, ask, round(bid + 0.01, 2)), s), "bid", (bid, ask))
            self.assertEqual(flow.estimate_side(quote(bid, ask, round(ask - 0.01, 2)), s), "ask", (bid, ask))
            self.assertEqual(flow.estimate_side(quote(bid, ask, round(bid + 0.02, 2)), s), "mid", (bid, ask))

    def test_side_stale_and_unknown(self):
        s = "2026-09-30"
        self.assertEqual(flow.estimate_side(quote(1.1, 1.2, 1.2, "2026-09-29T15:59:00"), s), "stale")
        self.assertEqual(flow.estimate_side(quote(1.1, 1.2, 1.2, None), s), "stale")
        self.assertEqual(flow.estimate_side(quote(1.1, 1.2, 1.2), None), "stale")
        self.assertEqual(flow.estimate_side(quote(0.0, 0.0, 0.02), s), "unknown")
        self.assertEqual(flow.estimate_side(quote(1.3, 1.2, 1.25), s), "unknown")  # crossed
        self.assertEqual(flow.estimate_side(quote(1.1, 1.2, 0.0), s), "unknown")

    def test_trade_date_formats(self):
        self.assertEqual(flow.trade_date("2026-09-30T15:59:12"), "2026-09-30")
        self.assertEqual(flow.trade_date("2026-09-30 15:59:12"), "2026-09-30")
        self.assertEqual(flow.trade_date("09/30/2026 15:59:12"), "2026-09-30")
        self.assertIsNone(flow.trade_date(""))
        self.assertIsNone(flow.trade_date("garbage"))

    def test_lean_label(self):
        self.assertEqual(flow.lean_label(151, 100), "bullish")
        self.assertEqual(flow.lean_label(150, 100), "mixed")   # exactly 1.5x is not "exceeds"
        self.assertEqual(flow.lean_label(100, 151), "bearish")
        self.assertEqual(flow.lean_label(100, 0), "bullish")
        self.assertEqual(flow.lean_label(0, 0), "mixed")

    def test_is_unusual(self):
        th = {"min_volume": 250, "min_premium": 50_000}
        self.assertTrue(flow.is_unusual(250, 249, 50_000, th))
        self.assertFalse(flow.is_unusual(249, 0, 1e6, th))       # volume floor
        self.assertFalse(flow.is_unusual(500, 500, 1e6, th))     # volume must exceed OI
        self.assertFalse(flow.is_unusual(500, 0, 49_999, th))    # premium floor

    def test_iv30_units(self):
        self.assertEqual(flow._iv30_decimal(48.5), 0.485)
        self.assertEqual(flow._iv30_decimal(0.485), 0.485)
        self.assertIsNone(flow._iv30_decimal(None))

    def test_fmt_money(self):
        self.assertEqual(flow.fmt_money(230_000), "$230K")
        self.assertEqual(flow.fmt_money(10_250), "$10.2K")
        self.assertEqual(flow.fmt_money(1_250_000), "$1.25M")
        self.assertEqual(flow.fmt_money(200), "$200")
        self.assertEqual(flow.fmt_money(84_000), "$84K")
        self.assertEqual(flow.fmt_money(50_000), "$50K")
        self.assertEqual(flow.fmt_money(None), "n/a")
        self.assertEqual(flow.fmt_money(999_499), "$999K")
        self.assertEqual(flow.fmt_money(999_999), "$1.00M")      # not "$1000K"
        self.assertEqual(flow.fmt_money(2_500_000_000), "$2.50B")

    def test_env_thresholds_reject_bad_values(self):
        for bad in ("nan", "inf", "-5", "", "lots"):
            with mock.patch.dict(os.environ, {"OPPTIONS_MIN_VOLUME": bad, "OPPTIONS_MIN_PREMIUM": bad}):
                self.assertEqual(flow.thresholds(), {"min_volume": 250.0, "min_premium": 50_000.0}, bad)


class TestAnalyzeChain(unittest.TestCase):
    def setUp(self):
        self.raw = load("flow_chain.json")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPPTIONS_MIN_VOLUME", None)
            os.environ.pop("OPPTIONS_MIN_PREMIUM", None)
            self.r = flow.analyze(self.raw)

    def test_header_fields(self):
        r = self.r
        self.assertEqual(r["symbol"], "ACME")
        self.assertEqual(r["spot"], 100.0)
        self.assertEqual(r["as_of"], "2026-09-30T16:15:00")
        self.assertEqual(r["session_date"], "2026-09-30")
        self.assertEqual(r["session_date_source"], "as_of")
        self.assertEqual(r["contracts_in_chain"], 13)
        self.assertEqual(r["contracts_traded"], 12)
        self.assertEqual(r["thresholds"], {"min_volume": 250.0, "min_premium": 50_000.0})

    def test_totals_and_ratios(self):
        t = self.r["totals"]
        self.assertEqual(t["call_volume"], 6850)
        self.assertEqual(t["put_volume"], 1949)
        self.assertAlmostEqual(t["call_premium"], 559_250, places=2)
        self.assertAlmostEqual(t["put_premium"], 374_590, places=2)
        self.assertAlmostEqual(t["total_premium"], 933_840, places=2)
        self.assertEqual(t["call_open_interest"], 13_600)
        self.assertEqual(t["put_open_interest"], 12_160)
        self.assertEqual(t["put_call_volume_ratio"], 0.285)    # 1949 / 6850 = 0.2845
        self.assertEqual(t["put_call_premium_ratio"], 0.67)    # 374590 / 559250 = 0.6698
        self.assertEqual(t["put_call_oi_ratio"], 0.894)        # 12160 / 13600 = 0.8941

    def test_sentiment_lean(self):
        s = self.r["sentiment"]
        self.assertAlmostEqual(s["bullish_premium"], 417_000, places=2)
        self.assertAlmostEqual(s["bearish_premium"], 232_500, places=2)
        self.assertEqual(s["lean"], "bullish")
        self.assertEqual(s["classified_share"], 0.696)          # 649500 / 933840 = 0.6955

    def test_premium_by_side(self):
        p = self.r["premium_by_side"]
        self.assertAlmostEqual(p["ask"], 230_000 + 90_000 + 18_500 + 18_000 + 84_000, places=2)
        self.assertAlmostEqual(p["bid"], 85_000 + 124_000, places=2)
        self.assertAlmostEqual(p["mid"], 93_000 + 29_000 + 10_250 + 151_890, places=2)
        self.assertAlmostEqual(p["unknown"], 200, places=2)
        self.assertEqual(p["stale"], 0)

    def test_short_dated_share(self):
        sd = self.r["short_dated"]
        self.assertAlmostEqual(sd["premium"], 455_200, places=2)
        self.assertEqual(sd["share"], 0.487)                    # 455200 / 933840 = 0.4874

    def test_unusual_ranking_and_fields(self):
        u = self.r["unusual"]
        self.assertEqual(self.r["unusual_count"], 4)
        self.assertEqual([x["contract"] for x in u],
                         ["ACME261002C00105000", "ACME261016C00100000",
                          "ACME261016P00100000", "ACME261016C00110000"])
        top = u[0]
        self.assertEqual(top["type"], "call")
        self.assertEqual(top["strike"], 105.0)
        self.assertEqual(top["expiry"], "2026-10-02")
        self.assertEqual(top["dte"], 2)
        self.assertEqual(top["volume"], 2000)
        self.assertEqual(top["open_interest"], 500)
        self.assertEqual(top["vol_oi"], 4.0)
        self.assertAlmostEqual(top["premium"], 230_000, places=2)
        self.assertEqual(top["side"], "ask")
        self.assertEqual(top["iv"], 0.55)
        self.assertEqual(top["delta"], 0.25)
        self.assertEqual(top["moneyness_pct"], 5.0)
        self.assertTrue(top["otm"])
        self.assertEqual(u[1]["side"], "bid")
        self.assertEqual(u[1]["vol_oi"], 1.33)
        self.assertEqual(u[2]["side"], "ask")
        self.assertEqual(u[2]["type"], "put")
        new = u[3]
        self.assertIsNone(new["vol_oi"])                         # OI 0
        self.assertEqual(new["moneyness_pct"], 10.0)

    def test_threshold_exclusions(self):
        names = {x["contract"] for x in self.r["unusual"]}
        self.assertNotIn("ACME261002C00115000", names)   # premium 18k < 50k
        self.assertNotIn("ACME261016P00105000", names)   # volume 249 < 250
        self.assertNotIn("ACME261002C00100000", names)   # volume 600 < OI 700

    def test_positioning(self):
        calls = self.r["positioning"]["calls"]
        puts = self.r["positioning"]["puts"]
        self.assertEqual([c["strike"] for c in calls], [110.0, 120.0, 100.0, 105.0, 115.0])
        self.assertEqual(calls[0]["open_interest"], 8000)
        self.assertEqual(calls[0]["volume"], 850)              # 50 (11/20) + 800 (10/16)
        self.assertEqual(calls[0]["top_expiry"], "2026-11-20")
        self.assertEqual(calls[2]["open_interest"], 1000)      # 700 + 300 across expiries
        self.assertEqual(calls[2]["top_expiry"], "2026-10-02")
        self.assertEqual(calls[2]["top_expiry_share"], 0.7)
        self.assertEqual([p["strike"] for p in puts], [90.0, 95.0, 85.0, 100.0, 105.0])
        self.assertEqual(puts[0]["moneyness_pct"], -10.0)

    def test_volatility(self):
        v = self.r["volatility"]
        front = v["front"]
        self.assertEqual(front["expiry"], "2026-10-02")
        self.assertEqual(front["dte"], 2)
        self.assertEqual(front["strike"], 100.0)
        self.assertEqual(front["iv"], 0.51)                    # (0.50 + 0.52) / 2
        self.assertAlmostEqual(front["expected_move"], 100 * 0.51 * math.sqrt(2 / 365), places=2)
        self.assertEqual(v["iv30"], 0.485)

    def test_json_serializable_and_pure(self):
        before = copy.deepcopy(self.raw)
        result = flow.analyze(self.raw)
        json.dumps(result, allow_nan=False)
        self.assertEqual(self.raw, before)

    def test_analyze_reads_no_network_or_clock(self):
        class NoClock(date):
            @classmethod
            def today(cls):
                raise AssertionError("analyze() read the clock")

        def no_network(*a, **k):
            raise AssertionError("analyze() touched the network")

        with mock.patch("opptions.cboe.date", NoClock), \
                mock.patch("urllib.request.urlopen", no_network):
            again = flow.analyze(copy.deepcopy(self.raw))
        self.assertEqual(again, self.r)

    def test_env_thresholds(self):
        with mock.patch.dict(os.environ, {"OPPTIONS_MIN_VOLUME": "1000", "OPPTIONS_MIN_PREMIUM": "100000"}):
            r = flow.analyze(self.raw)
        self.assertEqual([x["contract"] for x in r["unusual"]], ["ACME261002C00105000"])
        self.assertEqual(r["thresholds"]["min_volume"], 1000.0)
        r = flow.analyze(self.raw, min_volume=100, min_premium=10_000)
        # adds 115C (18k, vol 3000 > OI 100) and 105P (vol 249 > OI 10, 151,890)
        self.assertEqual(r["unusual_count"], 6)
        self.assertEqual(r["unusual"][0]["contract"], "ACME261002C00105000")
        self.assertEqual(r["unusual"][1]["contract"], "ACME261016P00105000")

    def test_markdown(self):
        md = flow.to_markdown(self.r)
        self.assertTrue(md.startswith("## Options Activity"))
        self.assertIn("ACME", md.splitlines()[0])
        self.assertIn("not trade-by-trade", md)
        self.assertIn("Unusual Whales", md)
        self.assertIn("| Contract | Expiry (DTE) |", md)
        self.assertIn("ACME261002C00105000", md)
        self.assertIn("ask (likely bought)", md)
        self.assertIn("new (OI 0)", md)
        self.assertIn("Estimated lean: bullish", md)
        self.assertIn("put/call 0.28", md)
        self.assertIn("### Positioning", md)
        self.assertIn("tends to", md)
        self.assertIn("Front IV is close to IV30", md)
        self.assertIn("could be called bullish or bearish", md)
        # 48.7% within 7 days is an even split, not "most of the money is longer-dated".
        self.assertIn("split roughly evenly", md)
        self.assertNotIn("Most of the money", md)
        self.assertIn("More money went into calls.", md)
        self.assertIn("2026-10-02 (2)", md)
        lowered = md.lower()
        for phrase in ("you should buy", "you should sell", "buy now", "sell now"):
            self.assertNotIn(phrase, lowered)


class TestStaleAndSession(unittest.TestCase):
    def test_stale_trade_is_not_classified(self):
        r = flow.analyze(load("flow_stale.json"))
        self.assertEqual(r["session_date"], "2026-09-30")
        sides = {x["contract"]: x["side"] for x in r["unusual"]}
        self.assertEqual(sides, {"ACME261016C00100000": "ask", "ACME261016P00095000": "stale"})
        s = r["sentiment"]
        self.assertAlmostEqual(s["bullish_premium"], 155_000, places=2)
        self.assertEqual(s["bearish_premium"], 0)
        self.assertEqual(s["lean"], "bullish")
        self.assertAlmostEqual(r["premium_by_side"]["stale"], 58_000, places=2)
        self.assertEqual(s["classified_share"], 0.728)          # 155000 / 213000
        self.assertIn("| stale |", flow.to_markdown(r))
        self.assertTrue(any("1 contract shows volume but its last trade is not from the 2026-09-30"
                            in n for n in r["notes"]), r["notes"])

    def test_weekend_fetch_uses_latest_session(self):
        raw = load("flow_chain.json")
        raw["timestamp"] = "2026-10-03 10:00:00"   # a Saturday; the newest trades are from 9/30
        r = flow.analyze(raw)
        self.assertEqual(r["session_date"], "2026-09-30")
        self.assertEqual(r["session_date_source"], "latest_trade")
        self.assertEqual(r["unusual"][0]["side"], "ask")
        self.assertEqual(r["sentiment"]["lean"], "bullish")
        self.assertTrue(any("latest session" in n for n in r["notes"]))
        # 10/02 has expired relative to 10/03, so the front expiry moves out.
        self.assertEqual(r["volatility"]["front"]["expiry"], "2026-10-16")
        self.assertEqual(r["unusual"][0]["dte"], -1)
        md = flow.to_markdown(r)
        self.assertIn("2026-10-02 (expired)", md)
        self.assertNotIn("(-1)", md)


class TestEdgeCases(unittest.TestCase):
    def test_empty_chain(self):
        r = flow.analyze(load("flow_empty.json"))
        self.assertEqual(r["contracts_in_chain"], 0)
        self.assertEqual(r["unusual"], [])
        self.assertIsNone(r["totals"]["put_call_volume_ratio"])
        self.assertIsNone(r["volatility"]["front"])
        self.assertIsNone(r["volatility"]["iv30"])
        self.assertEqual(r["sentiment"]["lean"], "mixed")
        json.dumps(r, allow_nan=False)
        md = flow.to_markdown(r)
        self.assertTrue(md.startswith("## Options Activity"))
        self.assertIn("no option contracts", md)
        self.assertIn("Caveat", md)

    def test_no_data_at_all(self):
        r = flow.analyze({})
        self.assertEqual(r["contracts_in_chain"], 0)
        self.assertIsNone(r["symbol"])
        self.assertTrue(flow.to_markdown(r).startswith("## Options Activity"))

    def test_zero_volume_day(self):
        raw = load("flow_chain.json")
        for o in raw["data"]["options"]:
            o["volume"] = 0.0
        r = flow.analyze(raw)
        t = r["totals"]
        self.assertEqual(t["total_volume"], 0)
        self.assertEqual(r["contracts_traded"], 0)
        self.assertIsNone(t["put_call_volume_ratio"])
        self.assertIsNone(t["put_call_premium_ratio"])
        self.assertEqual(t["put_call_oi_ratio"], 0.894)
        self.assertEqual(r["unusual"], [])
        self.assertEqual(r["sentiment"]["lean"], "mixed")
        self.assertIsNone(r["short_dated"]["share"])
        self.assertEqual(len(r["positioning"]["calls"]), 5)
        md = flow.to_markdown(r)
        self.assertIn("no contracts have traded", md.lower())
        self.assertIn("None (no volume yet).", md)
        self.assertNotIn("Estimated lean", md)

    def test_ratio_meanings_when_equal(self):
        self.assertIn("About the same money", flow._pc_premium_meaning(1.0))
        self.assertIn("about even", flow._pc_oi_meaning(1.0))
        self.assertIn("puts", flow._pc_premium_meaning(1.2))
        self.assertTrue(flow._short_meaning(None))               # never a blank meaning line

    def test_unknown_spot(self):
        raw = load("flow_stale.json")
        raw["data"]["current_price"] = None
        raw["data"]["close"] = None
        for o in raw["data"]["options"]:
            o["delta"] = 0.0
        r = flow.analyze(raw)
        self.assertEqual(r["spot"], 0.0)
        self.assertIsNone(r["unusual"][0]["moneyness_pct"])
        self.assertIsNone(r["volatility"]["front"])
        self.assertTrue(any("Spot price is unknown" in n for n in r["notes"]))
        self.assertIn("n/a", flow.to_markdown(r))

    def test_more_than_top_n(self):
        raw = load("flow_stale.json")
        base = raw["data"]["options"][0]
        raw["data"]["options"] = []
        for i in range(20):
            o = dict(base)
            o["option"] = f"ACME261016C{(100 + i) * 1000:08d}"
            o["volume"] = 500.0 + i
            raw["data"]["options"].append(o)
        r = flow.analyze(raw)
        self.assertEqual(r["unusual_count"], 20)
        self.assertEqual(len(r["unusual"]), flow.TOP_UNUSUAL)
        self.assertEqual(r["unusual"][0]["volume"], 519)
        self.assertIn("top 15 by premium", flow.to_markdown(r))


class TestFetch(unittest.TestCase):
    def test_fetch_delegates_to_cboe(self):
        with mock.patch("opptions.cboe.fetch_chain", return_value={"data": {}}) as m:
            self.assertEqual(flow.fetch("acme"), {"data": {}})
        m.assert_called_once_with("acme")


if __name__ == "__main__":
    unittest.main()

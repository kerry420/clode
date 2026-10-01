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
        # adds 115C (18k, vol 3000 > OI 100) and 105P (vol 249 > OI 10, 151,890).
        # Ranked by time value: the 105P is $5 in the money, so only 249 * (6.10 - 5) * 100
        # = 27,390 of its 151,890 is extrinsic, and it ranks 5th, not 2nd as by premium.
        self.assertEqual(r["unusual_count"], 6)
        self.assertEqual([x["contract"] for x in r["unusual"]],
                         ["ACME261002C00105000", "ACME261016C00100000", "ACME261016P00100000",
                          "ACME261016C00110000", "ACME261016P00105000", "ACME261002C00115000"])
        self.assertAlmostEqual(r["unusual"][4]["extrinsic_premium"], 27_390, places=2)

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
        self.assertIn("top 15 by time value (extrinsic premium)", flow.to_markdown(r))


def contract(opt_type, strike, mid, volume, delta, last=None):
    """A normalized contract with just the fields flow.time_value reads."""
    return {"type": opt_type, "strike": strike, "mid": mid, "last": mid if last is None else last,
            "volume": volume, "delta": delta}


def option(sym, bid, ask, last, volume, oi, delta, ltt="2026-09-30T15:50:00"):
    return {"option": sym, "bid": bid, "ask": ask, "last_trade_price": last, "volume": volume,
            "open_interest": oi, "delta": delta, "gamma": 0.001, "iv": 0.3, "theo": (bid + ask) / 2,
            "last_trade_time": ltt}


def leaps_chain(deep_last=174.0):
    """LEAP (spot 331, like the live AAPL report) on 2026-09-30.

      #  contract          vol    OI    mid     premium     intrinsic  extrinsic  ext premium  delta
      1  2029-01-19 170C  1,000    50   173.0  17,300,000    161.0      12.0      1,200,000    0.97 deep
      2  2026-10-16 350C  5,000   100     3.0   1,500,000      0.0       3.0      1,500,000    0.30
      3  2026-10-16 335C    900  4,000    7.0     630,000      0.0       7.0        630,000    0.45 (vol < OI)
    By premium the deep ITM block is first; by time value the 350 call is.
    """
    return {"timestamp": "2026-09-30 16:15:00",
            "data": {"symbol": "LEAP", "current_price": 331.0, "iv30": 25.0, "options": [
                option("LEAP290119C00170000", 172.0, 174.0, deep_last, 1000, 50, 0.97),
                option("LEAP261016C00350000", 2.9, 3.1, 3.1, 5000, 100, 0.30),
                option("LEAP261016C00335000", 6.9, 7.1, 7.0, 900, 4000, 0.45),
            ]}}


class TestTimeValue(unittest.TestCase):
    """Hand-checked intrinsic, extrinsic and deep-ITM flags."""

    def test_itm_call(self):
        # spot 331, 170 call at 173: intrinsic 331 - 170 = 161, extrinsic 12,
        # 1,000 contracts * 12 * 100 = 1,200,000 of the 17,300,000 premium.
        c = contract("C", 170.0, 173.0, 1000, 0.97)
        self.assertEqual(flow.intrinsic_value("C", 170.0, 331.0), 161.0)
        self.assertEqual(flow.time_value(c, 331.0), (161.0, 12.0, 1_200_000.0, True))
        self.assertEqual(flow.premium(c), 17_300_000)

    def test_itm_put(self):
        # spot 100, 105 put at 6.10: intrinsic 5, extrinsic 1.10, 249 * 1.10 * 100 = 27,390.
        intrinsic, extrinsic, ext_prem, deep = flow.time_value(contract("P", 105.0, 6.10, 249, -0.70), 100.0)
        self.assertEqual(intrinsic, 5.0)
        self.assertAlmostEqual(extrinsic, 1.10, places=9)
        self.assertAlmostEqual(ext_prem, 27_390, places=6)
        self.assertFalse(deep)
        # A put below spot is out of the money: no intrinsic value.
        self.assertEqual(flow.intrinsic_value("P", 95.0, 100.0), 0.0)

    def test_otm_extrinsic_is_the_whole_price(self):
        c = contract("C", 105.0, 1.15, 2000, 0.25)
        intrinsic, extrinsic, ext_prem, deep = flow.time_value(c, 100.0)
        self.assertEqual((intrinsic, extrinsic, deep), (0.0, 1.15, False))
        self.assertAlmostEqual(ext_prem, flow.premium(c), places=6)       # 230,000
        self.assertAlmostEqual(ext_prem, 230_000, places=6)

    def test_uses_last_when_there_is_no_mid_and_never_goes_negative(self):
        # mid 0 -> last 20.5, as premium() does: intrinsic 20, extrinsic 0.50.
        self.assertEqual(flow.time_value(contract("C", 80.0, 0.0, 100, 0.95, last=20.5), 100.0)[:3],
                         (20.0, 0.5, 5_000.0))
        # A stale quote below intrinsic counts as no time value, not a negative one.
        self.assertEqual(flow.time_value(contract("C", 80.0, 19.0, 100, 0.95), 100.0)[1:3], (0.0, 0.0))

    def test_deep_itm_by_delta(self):
        self.assertTrue(flow.time_value(contract("C", 80.0, 21.0, 10, 0.90), 100.0)[3])    # edge counts
        self.assertTrue(flow.time_value(contract("P", 130.0, 30.5, 10, -0.92), 100.0)[3])
        self.assertFalse(flow.time_value(contract("C", 80.0, 21.0, 10, 0.89), 100.0)[3])
        # With a delta, the delta decides even when intrinsic is 95% of the price.
        self.assertFalse(flow.time_value(contract("C", 81.0, 20.0, 10, 0.85), 100.0)[3])

    def test_deep_itm_fallback_without_delta(self):
        # delta missing (0): 161 / 173 = 0.93 of the price is intrinsic -> deep.
        self.assertTrue(flow.time_value(contract("C", 170.0, 173.0, 10, 0.0), 331.0)[3])
        # 90 / 100 exactly -> deep; 5 / 6.10 = 0.82 -> not deep.
        self.assertTrue(flow.time_value(contract("P", 190.0, 100.0, 10, 0.0), 100.0)[3])
        self.assertFalse(flow.time_value(contract("P", 105.0, 6.10, 10, 0.0), 100.0)[3])
        self.assertFalse(flow.time_value(contract("C", 105.0, 1.15, 10, 0.0), 100.0)[3])   # OTM

    def test_unknown_spot(self):
        # No spot: intrinsic is blank, the whole price is time value, delta alone can flag deep.
        self.assertEqual(flow.time_value(contract("C", 170.0, 173.0, 10, 0.0), 0.0), (None, 173.0, 173_000.0, False))
        self.assertTrue(flow.time_value(contract("C", 170.0, 173.0, 10, 0.97), 0.0)[3])

    def test_fixture_rows_and_aggregates(self):
        r = flow.analyze(load("flow_chain.json"))
        top = r["unusual"][0]
        self.assertEqual((top["intrinsic"], top["extrinsic"], top["extrinsic_premium"], top["deep_itm"]),
                         (0.0, 1.15, 230_000.0, False))
        self.assertEqual(r["unusual_deep_itm_count"], 0)
        ex = r["extrinsic"]
        # Only the 105P (vol 249, $5 in the money) has built-in value: 249 * 5 * 100 = 124,500.
        self.assertAlmostEqual(ex["call_premium"], 559_250, places=2)
        self.assertAlmostEqual(ex["put_premium"], 374_590 - 124_500, places=2)
        self.assertAlmostEqual(ex["total_premium"], 809_340, places=2)
        self.assertEqual(ex["share_of_premium"], 0.867)                    # 809,340 / 933,840
        self.assertAlmostEqual(ex["premium_by_side"]["mid"], 93_000 + 29_000 + 10_250 + 27_390, places=2)
        self.assertAlmostEqual(ex["premium_by_side"]["ask"], 440_500, places=2)
        self.assertAlmostEqual(ex["premium_by_side"]["bid"], 209_000, places=2)
        self.assertEqual((ex["bullish_premium"], ex["bearish_premium"], ex["lean"]),
                         (417_000.0, 232_500.0, "bullish"))
        self.assertEqual((ex["deep_itm_contracts"], ex["deep_itm_premium"]), (0, 0.0))
        self.assertEqual(ex["classified_share"], 0.803)                    # 649,500 / 809,340 = 0.80251
        # The total-premium fields keep their meaning.
        self.assertAlmostEqual(r["sentiment"]["bullish_premium"], 417_000, places=2)
        self.assertAlmostEqual(r["premium_by_side"]["mid"], 93_000 + 29_000 + 10_250 + 151_890, places=2)

    def test_deep_itm_block_ranks_by_time_value(self):
        r = flow.analyze(leaps_chain())
        u = r["unusual"]
        self.assertEqual([x["contract"] for x in u], ["LEAP261016C00350000", "LEAP290119C00170000"])
        deep = u[1]
        self.assertEqual((deep["intrinsic"], deep["extrinsic"], deep["deep_itm"]), (161.0, 12.0, True))
        self.assertEqual((deep["premium"], deep["extrinsic_premium"]), (17_300_000.0, 1_200_000.0))
        self.assertEqual(r["unusual_deep_itm_count"], 1)
        ex = r["extrinsic"]
        self.assertEqual((ex["deep_itm_contracts"], ex["deep_itm_premium"]), (1, 17_300_000.0))
        self.assertEqual(ex["total_premium"], 1_200_000 + 1_500_000 + 630_000)
        json.dumps(r, allow_nan=False)

    def test_deep_itm_is_left_out_of_the_time_value_lean(self):
        # The deep block printed at the bid (a call at the bid reads bearish): by total premium
        # that swamps the lean, while the time-value lean leaves it out.
        r = flow.analyze(leaps_chain(deep_last=172.0))
        self.assertEqual(r["sentiment"]["lean"], "bearish")                # 17.3M vs 1.5M
        ex = r["extrinsic"]
        self.assertEqual((ex["bullish_premium"], ex["bearish_premium"], ex["lean"]),
                         (1_500_000.0, 0.0, "bullish"))
        self.assertEqual(ex["premium_by_side"]["bid"], 1_200_000.0)         # sides still count it

    def test_deep_itm_put_is_left_out_of_the_time_value_lean(self):
        # Puts carry a negative Cboe delta. A deep ITM put block at the ask (bearish-looking):
        # spot 331, 500 put at mid 170 (bid 169.5, ask 170.5, last 170.5), delta -0.96, 1,000
        # contracts: premium 17,000,000, intrinsic 169, extrinsic 1.00 -> 100,000.
        # The 170 call block prints at the mid (173), so it is in neither lean.
        raw = leaps_chain(deep_last=173.0)
        raw["data"]["options"].append(option("LEAP261016P00500000", 169.5, 170.5, 170.5, 1000, 50, -0.96))
        r = flow.analyze(raw)
        put = next(x for x in r["unusual"] if x["contract"] == "LEAP261016P00500000")
        self.assertEqual((put["intrinsic"], put["extrinsic"], put["deep_itm"]), (169.0, 1.0, True))
        self.assertEqual((put["premium"], put["extrinsic_premium"]), (17_000_000.0, 100_000.0))
        # By total premium the put block makes the day look bearish: 17.0M vs the 350 call's 1.5M.
        self.assertEqual((r["sentiment"]["bullish_premium"], r["sentiment"]["bearish_premium"],
                          r["sentiment"]["lean"]), (1_500_000.0, 17_000_000.0, "bearish"))
        ex = r["extrinsic"]
        self.assertEqual((ex["bullish_premium"], ex["bearish_premium"], ex["lean"]),
                         (1_500_000.0, 0.0, "bullish"))
        self.assertEqual(ex["premium_by_side"]["ask"], 1_600_000.0)        # 1.5M + the put's 100K
        self.assertEqual((ex["deep_itm_contracts"], ex["deep_itm_premium"]), (2, 34_300_000.0))
        self.assertEqual(r["unusual_deep_itm_count"], 2)

    def test_markdown_shows_time_value(self):
        md = flow.to_markdown(flow.analyze(leaps_chain()))
        self.assertIn("| ~Premium | ~Extrinsic |", md)
        self.assertIn("| $17.30M | $1.20M |", md)
        self.assertIn("-48.6% (deep ITM)", md)
        self.assertIn("ranked by time value (extrinsic premium)", md)
        self.assertIn("carry mostly built-in value, often from stock replacement or rolls", md)
        self.assertIn("desk weighs the time-value part", md)
        self.assertIn("**Time value:** ~$3.33M of the ~$19.43M premium (17%)", md)
        self.assertIn("1 deep in-the-money contract traded ~$17.30M of premium.", md)
        # A chain with no built-in value does not claim there is a rest.
        plain = flow.to_markdown(flow.analyze(load("flow_stale.json")))
        self.assertIn("is time value. Time-value lean", plain)


class TestFetch(unittest.TestCase):
    def test_fetch_delegates_to_cboe(self):
        with mock.patch("opptions.cboe.fetch_chain", return_value={"data": {}}) as m:
            self.assertEqual(flow.fetch("acme"), {"data": {}})
        m.assert_called_once_with("acme")


if __name__ == "__main__":
    unittest.main()

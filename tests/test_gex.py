"""Tests for opptions.gex. No network: everything runs on hand-built fixtures.

Fixture gex_chain.json: synthetic underlying XYZ, spot 100, as of 2026-10-01,
strikes 90/95/100/105/110, calls and puts, expiries 2026-10-09 (8 DTE) and
2026-10-30 (29 DTE): 20 contracts. Gammas are Black-Scholes values rounded to
4 decimals. With spot 100, GEX per contract = gamma * OI * 10,000.
"""

import copy
import json
import math
import os
import unittest
from unittest import mock

from opptions import gex

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def analyze(raw):
    return gex.analyze(raw, rate=0.04)


class ChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = load("gex_chain.json")
        cls.res = analyze(cls.raw)

    def test_one_contract_hand_checked(self):
        # XYZ261030C00105000: gamma 0.0522, OI 9000, spot 100
        # 0.0522 * 9000 * 100 * 100^2 * 0.01 = 4,698,000
        self.assertAlmostEqual(gex.contract_gex(0.0522, 9000, 100.0, "C"), 4_698_000, places=4)
        self.assertAlmostEqual(gex.contract_gex(0.0522, 9000, 100.0, "P"), -4_698_000, places=4)
        # Strike 105 calls: 0.0373*6000*1e4 (8 DTE) + 4,698,000 (29 DTE) = 6,936,000
        row = next(s for s in self.res["by_strike"] if s["strike"] == 105.0)
        self.assertAlmostEqual(row["call_gex"], 6_936_000, places=2)

    def test_totals_and_regime(self):
        self.assertAlmostEqual(self.res["total_call_gex"], 15_047_500, places=2)
        self.assertAlmostEqual(self.res["total_put_gex"], -11_573_450, places=2)
        self.assertAlmostEqual(self.res["total_net_gex"], 3_474_050, places=2)
        self.assertGreater(self.res["total_net_gex"], 0)
        self.assertEqual(self.res["regime"], "positive")
        self.assertEqual(self.res["contracts_used"], 20)
        self.assertEqual(self.res["multiplier"], 100)

    def test_walls(self):
        self.assertEqual(self.res["call_wall"],
                         {"strike": 105.0, "gex": 6_936_000.0, "distance_pct": 5.0})
        self.assertEqual(self.res["put_wall"],
                         {"strike": 95.0, "gex": -5_433_500.0, "distance_pct": -5.0})

    def test_top_strikes(self):
        top = self.res["top_strikes"]
        self.assertEqual([s["strike"] for s in top], [105.0, 95.0, 100.0, 110.0, 90.0])
        self.assertAlmostEqual(top[0]["net_gex"], 6_610_900, places=2)
        self.assertAlmostEqual(top[1]["net_gex"], -4_709_100, places=2)
        self.assertEqual(top[3]["distance_pct"], 10.0)

    def test_gamma_flip_exists(self):
        flip = self.res["gamma_flip"]
        self.assertIsNotNone(flip)
        # Independent bisection on the same model gives 98.792.
        self.assertAlmostEqual(flip["level"], 98.79, delta=0.02)
        self.assertEqual(flip["spot_side"], "above")
        self.assertEqual((flip["sign_below"], flip["sign_above"]), ("negative", "positive"))
        self.assertAlmostEqual(flip["distance_pct"], -1.21, delta=0.02)

        # Cross-check the sign change with bs_gamma directly, contract by contract.
        chain = gex.cboe.normalize_chain(self.raw)

        def net_at(x):
            total = 0.0
            for o in chain["options"]:
                g = gex.bs_gamma(x, o["strike"], o["iv"], max(o["dte"], 0.5) / 365, r=0.04)
                total += gex.contract_gex(g, o["open_interest"], x, o["type"])
            return total

        self.assertLess(net_at(flip["level"] - 0.5), 0)
        self.assertGreater(net_at(flip["level"] + 0.5), 0)
        sweep = self.res["flip_sweep"]
        self.assertEqual((sweep["low"], sweep["high"], sweep["steps"]), (80.0, 120.0, 120))
        self.assertEqual(sweep["skipped_no_iv"], 0)
        # Model GEX at spot should be close to the Cboe-gamma total.
        self.assertAlmostEqual(sweep["model_net_gex_at_spot"], net_at(100.0), delta=1.0)
        self.assertLess(abs(sweep["model_net_gex_at_spot"] / self.res["total_net_gex"] - 1), 0.01)

    def test_expiry_concentration(self):
        ne = self.res["nearest_expiry"]
        self.assertEqual((ne["expiry"], ne["dte"]), ("2026-10-09", 8))
        # Gross |GEX| of the 8 DTE lines = 11,759,400 of 26,620,950.
        self.assertAlmostEqual(ne["share_of_gross"], 0.4417, places=4)
        self.assertEqual(self.res["zero_dte_share"], 0.0)
        self.assertEqual([e["expiry"] for e in self.res["by_expiry"]], ["2026-10-09", "2026-10-30"])
        self.assertAlmostEqual(sum(e["share_of_gross"] for e in self.res["by_expiry"]), 1.0, places=3)

    def test_json_serializable_and_pure(self):
        before = copy.deepcopy(self.raw)
        result = analyze(self.raw)
        self.assertEqual(self.raw, before, "analyze must not mutate raw")
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertEqual(result["as_of"], "2026-10-01T15:45:00")

    def test_markdown(self):
        md = gex.to_markdown(self.res)
        self.assertTrue(md.startswith("## Gamma Exposure (GEX): XYZ"))
        for text in ("$100.00", "positive gamma regime", "+$3.47M", "### Key levels",
                     "| Call wall | 105.00 | +5.00% | +$6.94M", "| Put wall | 95.00 | -5.00% | -$5.43M",
                     "| Gamma flip | 98.79 | -1.21%", "### Top strikes", "### Expiry concentration",
                     "2026-10-09 (8 DTE): 44%", "How to read this", "15 minutes delayed",
                     "once a day", "long calls and short puts"):
            self.assertIn(text, md)
        for banned in ("you should buy", "you should sell", "buy signal", "sell signal"):
            self.assertNotIn(banned, md.lower())


class EdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.raw = load("gex_chain.json")

    def test_empty_chain(self):
        res = analyze(load("gex_empty.json"))
        self.assertEqual(res["regime"], "unknown")
        self.assertEqual(res["total_net_gex"], 0)
        self.assertIsNone(res["call_wall"])
        self.assertIsNone(res["put_wall"])
        self.assertIsNone(res["gamma_flip"])
        self.assertEqual(res["top_strikes"], [])
        self.assertIsNone(res["nearest_expiry"])
        self.assertIsNone(res["zero_dte_share"])
        self.assertTrue(any("empty" in n for n in res["notes"]))
        json.dumps(res)
        md = gex.to_markdown(res)
        self.assertTrue(md.startswith("## Gamma Exposure (GEX): XYZ"))
        self.assertIn("unknown", md)
        self.assertIn("Caveat", md)

    def test_analyze_handles_blank_raw(self):
        res = gex.analyze({}, rate=0.04)
        self.assertEqual(res["regime"], "unknown")
        self.assertTrue(gex.to_markdown(res).startswith("## Gamma Exposure (GEX)"))

    def test_missing_iv(self):
        # 6 lines of the 29 DTE expiry: C95 and P105 have iv 0 and gamma 0
        # (how Cboe shows deep in-the-money lines), P100 has no "iv" key at all.
        res = analyze(load("gex_missing_iv.json"))
        sweep = res["flip_sweep"]
        self.assertEqual(sweep["skipped_no_iv"], 3)
        self.assertEqual(sweep["contracts_used"], 3)
        self.assertEqual(res["contracts_used"], 6)
        # Cboe-gamma GEX still counts P100: 2,564,000 + 4,698,000 - 3,096,000 - 2,145,500
        self.assertAlmostEqual(res["total_net_gex"], 2_020_500, places=2)
        self.assertEqual(res["call_wall"]["strike"], 105.0)
        self.assertEqual(res["put_wall"]["strike"], 95.0)
        self.assertIsNotNone(res["gamma_flip"])
        self.assertTrue(any("3 contract(s) with no IV" in n for n in res["notes"]))
        self.assertIn("3 contract(s) with no IV", gex.to_markdown(res))

    def test_all_iv_missing_gives_no_flip(self):
        for o in self.raw["data"]["options"]:
            o["iv"] = None
        res = analyze(self.raw)
        self.assertIsNone(res["gamma_flip"])
        self.assertEqual(res["flip_sweep"]["skipped_no_iv"], 20)
        self.assertEqual(res["regime"], "positive")  # Cboe gammas are still there
        md = gex.to_markdown(res)
        # The sweep never ran, so it must not claim "no flip in range" or that the regime holds.
        self.assertIn("| Gamma flip | not computed (no IV) |", md)
        self.assertNotIn("none in ±20%", md)
        self.assertNotIn("tends to keep", md)
        self.assertNotIn("tends to hold", md)

    def test_no_flip_in_range_calls_only(self):
        # Calls only: every model term is positive, so there is no crossing anywhere.
        self.raw["data"]["options"] = [o for o in self.raw["data"]["options"] if o["option"][9] == "C"]
        res = analyze(self.raw)
        self.assertIsNone(res["gamma_flip"])
        self.assertEqual(res["flip_sweep"]["crossings"], [])
        self.assertEqual(res["flip_sweep"]["sign_at_spot"], "positive")
        md = gex.to_markdown(res)
        self.assertIn("| Gamma flip | none in ±20% |", md)
        self.assertIn("Model net gamma stays positive from 80% to 120% of spot", md)

    def test_model_sign_disagrees_with_cboe_regime(self):
        # Drop IV on XYZ261030C00105000 (Cboe GEX +4,698,000): Cboe net stays +3,474,050,
        # but the IV model loses that call. Model net at spot by hand = -1,225,578.
        for o in self.raw["data"]["options"]:
            if o["option"] == "XYZ261030C00105000":
                o["iv"] = 0.0
        res = analyze(self.raw)
        self.assertEqual(res["regime"], "positive")
        self.assertAlmostEqual(res["flip_sweep"]["model_net_gex_at_spot"], -1_225_578, delta=50)
        self.assertEqual(res["flip_sweep"]["sign_at_spot"], "negative")
        self.assertTrue(any("regime is uncertain" in n for n in res["notes"]))
        self.assertFalse(any("far from Cboe" in n for n in res["notes"]))
        md = gex.to_markdown(res)
        self.assertIn("model net gamma is negative, so hedging tends to amplify", md)

    def test_iv_in_percent_is_flagged(self):
        # If IV ever arrived as 30.0 instead of 0.30, model gamma collapses; the note must say so.
        for o in self.raw["data"]["options"]:
            o["iv"] = o["iv"] * 100
        res = analyze(self.raw)
        self.assertTrue(any("far from Cboe's own gammas" in n for n in res["notes"]))
        clean = analyze(load("gex_chain.json"))
        self.assertFalse(any("far from Cboe" in n for n in clean["notes"]))
        ratio = clean["flip_sweep"]["model_gross_gex_at_spot"] / clean["gross_gex"]
        self.assertAlmostEqual(ratio, 1.0, delta=0.01)

    def test_no_timestamp_is_noted(self):
        del self.raw["timestamp"]
        res = analyze(self.raw)
        self.assertIsNone(res["as_of"])
        self.assertTrue(any("no timestamp" in n for n in res["notes"]))
        self.assertFalse(any("no timestamp" in n for n in analyze(load("gex_chain.json"))["notes"]))

    def test_malformed_raw_does_not_crash(self):
        for bad in (None, [], [1, 2], "x", {"data": []}, {"data": {"options": "x"}},
                    {"timestamp": "2026-10-01 15:45:00", "data": {"options": [None, 5, "y"]}}):
            res = gex.analyze(bad, rate=0.04)
            self.assertEqual(res["regime"], "unknown", repr(bad))
            json.dumps(res, allow_nan=False)
            self.assertTrue(gex.to_markdown(res).startswith("## Gamma Exposure (GEX)"))
        # Good rows next to junk rows still count.
        self.raw["data"]["options"] = self.raw["data"]["options"] + [None, "junk"]
        self.assertAlmostEqual(analyze(self.raw)["total_net_gex"], 3_474_050, places=2)

    def test_non_finite_spot_gives_strict_json(self):
        self.raw["data"]["current_price"] = float("inf")
        res = analyze(self.raw)
        self.assertEqual(res["spot"], 0.0)
        self.assertEqual(res["regime"], "unknown")
        json.dumps(res, allow_nan=False)

    def test_spot_zero(self):
        data = self.raw["data"]
        for key in ("current_price", "close", "prev_day_close"):
            data[key] = 0
        for o in data["options"]:
            o["delta"] = 0.0  # no ATM delta to estimate spot from
        res = analyze(self.raw)
        self.assertEqual(res["spot"], 0.0)
        self.assertEqual(res["regime"], "unknown")
        self.assertEqual(res["total_net_gex"], 0)
        self.assertIsNone(res["gamma_flip"])
        self.assertIsNone(res["call_wall"])
        self.assertTrue(any("spot" in n for n in res["notes"]))
        json.dumps(res)
        self.assertIn("unknown", gex.to_markdown(res))

    def test_all_zero_gamma(self):
        for o in self.raw["data"]["options"]:
            o["gamma"] = 0.0
        res = analyze(self.raw)
        self.assertEqual(res["regime"], "unknown")
        self.assertEqual(res["gross_gex"], 0)
        self.assertIsNone(res["call_wall"])
        self.assertIsNone(res["put_wall"])
        self.assertEqual(res["top_strikes"], [])
        self.assertIsNone(res["nearest_expiry"]["share_of_gross"])
        self.assertTrue(any("zero gamma" in n for n in res["notes"]))
        # The sweep uses model gamma from IV, so a flip can still be found.
        self.assertAlmostEqual(res["gamma_flip"]["level"], 98.79, delta=0.02)
        self.assertIn("Model gamma flip from IV alone", gex.to_markdown(res))

    def test_zero_dte_when_as_of_is_expiry_day(self):
        self.raw["timestamp"] = "2026-10-09 10:00:00"
        res = analyze(self.raw)
        self.assertEqual(res["nearest_expiry"]["dte"], 0)
        self.assertAlmostEqual(res["zero_dte_share"], 0.4417, places=4)
        self.assertEqual(res["zero_dte_share"], res["nearest_expiry"]["share_of_gross"])
        self.assertIsNotNone(res["gamma_flip"])  # T floors at 0.5 day, no division by zero

    def test_expired_and_zero_oi_excluded(self):
        self.raw["timestamp"] = "2026-10-10 10:00:00"  # 2026-10-09 lines are now dte -1
        self.raw["data"]["options"][-1]["open_interest"] = 0
        res = analyze(self.raw)
        self.assertEqual(res["contracts_in_chain"], 20)
        self.assertEqual(res["contracts_used"], 9)
        self.assertEqual([e["expiry"] for e in res["by_expiry"]], ["2026-10-30"])

    def test_index_multiplier_is_100(self):
        raw = {"timestamp": "2026-10-01 15:45:00",
               "data": {"symbol": "_SPX", "current_price": 6000.0, "options": [
                   {"option": "SPXW261009C06100000", "iv": 0.14, "open_interest": 12000.0,
                    "gamma": 0.0011, "delta": 0.27},
                   {"option": "SPXW261009P05900000", "iv": 0.18, "open_interest": 15000.0,
                    "gamma": 0.0010, "delta": -0.28}]}}
        res = analyze(raw)
        self.assertEqual(res["symbol"], "SPX")
        # 0.0011 * 12000 * 100 * 6000^2 * 0.01 = 475,200,000
        self.assertAlmostEqual(res["call_wall"]["gex"], 475_200_000, places=2)
        # 0.0010 * 15000 * 100 * 6000^2 * 0.01 = 540,000,000
        self.assertAlmostEqual(res["put_wall"]["gex"], -540_000_000, places=2)
        self.assertEqual(res["regime"], "negative")
        self.assertIn("-$64.8M", gex.to_markdown(res))


def pocket_chain():
    """Narrow negative gamma pocket: two flip crossings well under 1% apart.

    Five 29-DTE calls (iv 0.20, OI 1000) at 90..110 and one 0DTE put at 100
    (iv 0.05, OI 1200). Gammas are hand-computed Black-Scholes values at spot 100
    (r 0.04, put T = 0.5/365), rounded to 4 decimals:
      calls 0.0105, 0.0432, 0.0705, 0.0522, 0.0195 -> +1,959,000
      put 2.1548 * 1200 * 10,000                    -> -25,857,600
      net                                           -> -23,898,600
    """
    calls = [("XYZ261030C00090000", 0.0105), ("XYZ261030C00095000", 0.0432),
             ("XYZ261030C00100000", 0.0705), ("XYZ261030C00105000", 0.0522),
             ("XYZ261030C00110000", 0.0195)]
    opts = [{"option": c, "iv": 0.20, "open_interest": 1000.0, "gamma": g, "delta": 0.5}
            for c, g in calls]
    opts.append({"option": "XYZ261001P00100000", "iv": 0.05, "open_interest": 1200.0,
                 "gamma": 2.1548, "delta": -0.5})
    return {"timestamp": "2026-10-01 10:00:00",
            "data": {"symbol": "XYZ", "current_price": 100.0, "options": opts}}


class NearbyCrossingsTest(unittest.TestCase):
    """Regression: signs around the flip used to be sampled at flip * 0.99 / 1.01,
    which jumps over a second crossing less than 1% away and reported 'positive'
    on both sides while spot sat in a negative pocket."""

    def setUp(self):
        self.res = analyze(pocket_chain())

    def test_totals_hand_checked(self):
        self.assertAlmostEqual(self.res["total_call_gex"], 1_959_000, places=2)
        self.assertAlmostEqual(self.res["total_put_gex"], -25_857_600, places=2)
        self.assertEqual(self.res["regime"], "negative")
        self.assertEqual(self.res["zero_dte_share"], round(25_857_600 / 27_816_600, 4))

    def test_signs_bracket_the_flip(self):
        sweep, flip = self.res["flip_sweep"], self.res["gamma_flip"]
        self.assertEqual(len(sweep["crossings"]), 2)
        lo, hi = sweep["crossings"]
        self.assertLess(hi - lo, 1.5)
        self.assertGreater(lo, 99.0)
        self.assertLess(hi, 101.0)
        self.assertEqual(sweep["sign_at_spot"], "negative")
        self.assertEqual(flip["sign_at_spot"], "negative")
        # Spot 100 is inside the pocket; the nearest crossing is one of the two edges.
        self.assertIn(flip["level"], (lo, hi))
        expected = ("negative", "positive") if flip["level"] == hi else ("positive", "negative")
        self.assertEqual((flip["sign_below"], flip["sign_above"]), expected)
        self.assertNotEqual(flip["sign_below"], flip["sign_above"])
        self.assertTrue(any("crosses zero 2 times" in n for n in self.res["notes"]))
        self.assertFalse(any("crosses zero" in n for n in analyze(load("gex_chain.json"))["notes"]))

    def test_markdown_says_amplify(self):
        md = gex.to_markdown(self.res)
        self.assertIn("model net gamma is negative, so hedging tends to amplify moves there", md)
        self.assertNotIn("tends to dampen moves there", md)


class HelperTest(unittest.TestCase):
    def test_norm_pdf_and_bs_gamma(self):
        self.assertAlmostEqual(gex.norm_pdf(0.0), 0.3989422804, places=9)
        # S = K = 100, iv 0.20, T = 1, r = 0: d1 = 0.1, gamma = pdf(0.1) / 20
        self.assertAlmostEqual(gex.bs_gamma(100, 100, 0.20, 1.0, r=0.0), 0.0198476, places=6)
        self.assertEqual(gex.bs_gamma(100, 100, 0.0, 1.0), 0.0)
        self.assertEqual(gex.bs_gamma(0, 100, 0.2, 1.0), 0.0)

    def test_find_zero_crossings(self):
        self.assertEqual(gex.find_zero_crossings([1, 2, 3], [-1.0, 1.0, 2.0]), [1.5])
        self.assertEqual(gex.find_zero_crossings([1, 2, 3, 4], [-2.0, 0.0, 0.0, 2.0]), [2.5])
        self.assertEqual(gex.find_zero_crossings([1, 2], [1.0, 2.0]), [])
        self.assertEqual(gex.find_zero_crossings([1, 2], [0.0, 0.0]), [])
        # Two crossings: the bracketing totals carry the sign on each side.
        self.assertEqual(gex._bracketed_crossings([1, 2, 3, 4], [1.0, -1.0, -1.0, 3.0]),
                         [(1.5, 1.0, -1.0), (3.25, -1.0, 3.0)])

    def test_fmt_money(self):
        self.assertEqual(gex.fmt_money(1.234e9), "$1.23B")
        self.assertEqual(gex.fmt_money(4.56e8), "$456M")
        self.assertEqual(gex.fmt_money(7.8e4), "$78K")
        self.assertEqual(gex.fmt_money(-2.5e6, signed=True), "-$2.5M")
        self.assertEqual(gex.fmt_money(3.0e6, signed=True), "+$3M")
        self.assertEqual(gex.fmt_money(999_999), "$1M")
        self.assertEqual(gex.fmt_money(512), "$512")
        self.assertEqual(gex.fmt_money(0), "$0")
        self.assertEqual(gex.fmt_money(None), "n/a")

    def test_rate_from_env(self):
        with mock.patch.dict(os.environ, {"OPPTIONS_RATE": "0.05"}):
            self.assertEqual(gex.rate_from_env(), 0.05)
            self.assertEqual(gex.analyze(load("gex_chain.json"))["flip_sweep"]["rate"], 0.05)
        with mock.patch.dict(os.environ, {"OPPTIONS_RATE": "abc"}):
            self.assertEqual(gex.rate_from_env(), 0.04)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gex.rate_from_env(), 0.04)

    def test_fetch_uses_cboe_chain(self):
        with mock.patch.object(gex.cboe, "fetch_chain", return_value={"data": {}}) as fc:
            self.assertEqual(gex.fetch("spy"), {"data": {}})
            fc.assert_called_once_with("spy")

    def test_sweep_speed_on_a_large_chain(self):
        # SPX-sized chain (~20k lines) should sweep in a few seconds at most.
        opts = [{"contract": "x", "type": "C" if i % 2 else "P", "strike": 4000 + (i % 400) * 5.0,
                 "dte": i % 60, "iv": 0.15 + (i % 7) * 0.01, "open_interest": 100.0}
                for i in range(20000)]
        info = gex.gamma_flip(opts, 5000.0, 0.04)
        self.assertEqual(info["contracts_used"], 20000)
        self.assertTrue(math.isfinite(info["model_net_gex_at_spot"]))


if __name__ == "__main__":
    unittest.main()

"""Tests for the desk scan (opptions/scan.py) and the universe loader. No network.

Synthetic symbols are built in code from the existing fixtures:

  flow_chain.json (ACME, 4 unusual contracts, $528K unusual premium, lean bullish)
    BIG    every volume x10   -> 9 unusual contracts, ~$9.05M
    MID    as is
    SMALL  every volume x0.5  -> 1 unusual contract, $115K
    QUIET  volume capped at 10 -> nothing unusual

  gex_chain.json (XYZ, spot 100, flip 98.79, call wall 105, put wall 95, nearest
  expiry 8 DTE with 44% of gross gamma, positive regime at +13% of gross)
    FLIP   as is: spot 1.21% above the flip
    WALL   spot 104.5: 0.48% under the call wall, flip 5.5% away
    NEG    put open interest x3: net -40% of gross
    EXP    10/09 open interest x4 and the clock moved to 10/05: 76% expires in 4 days
    QUIET  spot 102.5: flip 3.6%, walls 2.4% and 7.3% away, nothing near
"""

import contextlib
import copy
import io
import json
import math
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from opptions import __main__ as cli
from opptions import cboe, flow, gex, scan, universe
from opptions.http import FetchError

FIXTURES = Path(__file__).parent / "fixtures"
_ENV = mock.patch.dict(os.environ)
TRADE_WORDS = re.compile(r"\b(buy|buying|sell|selling|bought|sold)\b", re.I)
OCC = re.compile(r"^(?P<root>[A-Z.]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def setUpModule():
    # Keep a user's own OPPTIONS_* settings from changing the hand-checked results.
    _ENV.start()
    for name in ("OPPTIONS_MIN_VOLUME", "OPPTIONS_MIN_PREMIUM", "OPPTIONS_RATE", "OPPTIONS_UNIVERSE"):
        os.environ.pop(name, None)


def tearDownModule():
    _ENV.stop()


def load(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


FLOW = load("flow_chain.json")
GEX = load("gex_chain.json")


def flow_raw(volume_scale=1.0, cap=None, timestamp=None):
    raw = copy.deepcopy(FLOW)
    if timestamp:
        raw["timestamp"] = timestamp
    for o in raw["data"]["options"]:
        o["volume"] *= volume_scale
        if cap is not None:
            o["volume"] = min(o["volume"], cap)
    return raw


def gex_raw(spot=None, oi_scale=1.0, put_oi_scale=1.0, near_oi_scale=1.0,
            price_scale=1.0, timestamp=None):
    """The GEX fixture with spot moved, open interest scaled, or every price scaled.

    price_scale multiplies spot, strikes and option prices and divides gamma, so
    the chain keeps its shape (same distances, same IVs) while dollar GEX grows
    by price_scale.
    """
    raw = copy.deepcopy(GEX)
    data = raw["data"]
    if timestamp:
        raw["timestamp"] = timestamp
    if price_scale != 1.0:
        for k in ("current_price", "close", "prev_day_close", "open", "high", "low", "bid", "ask"):
            if k in data:
                data[k] *= price_scale
    if spot is not None:
        data["current_price"] = spot
    for o in data["options"]:
        m = OCC.match(o["option"])
        if price_scale != 1.0:
            strike = round(int(m["strike"]) * price_scale)
            o["option"] = f"{m['root']}{m['ymd']}{m['cp']}{strike:08d}"
            for k in ("bid", "ask", "theo", "last_trade_price", "prev_day_close"):
                if k in o:
                    o[k] *= price_scale
            o["gamma"] /= price_scale
        o["open_interest"] *= oi_scale
        if m["cp"] == "P":
            o["open_interest"] *= put_oi_scale
        if m["ymd"] == "261009":
            o["open_interest"] *= near_oi_scale
    return raw


FLOW_RAWS = {"BIG": flow_raw(10), "MID": flow_raw(), "SMALL": flow_raw(0.5), "QUIET": flow_raw(cap=10)}
GEX_RAWS = {
    "FLIP": gex_raw(),
    "WALL": gex_raw(spot=104.5),
    "NEG": gex_raw(put_oi_scale=3),
    "EXP": gex_raw(near_oi_scale=4, timestamp="2026-10-05 15:45:00"),
    "QUIET": gex_raw(spot=102.5),
}


def from_dict(raws):
    return lambda sym: raws[sym]


def gex_row(raw):
    return scan.score_gex(gex.analyze(raw, rate=0.04))


class FlowScoreTest(unittest.TestCase):
    def test_fixture_row_hand_checked(self):
        row = scan.score_flow(flow.analyze(FLOW))
        # Unusual: 105C 230,000 + 100C 124,000 + 100P 90,000 + 110C 84,000
        self.assertEqual(row["unusual_premium"], 528_000)
        self.assertEqual(row["unusual_count"], 4)
        self.assertFalse(row["unusual_premium_partial"])
        self.assertEqual(row["lean"], "bullish")
        self.assertEqual(row["put_call_premium_ratio"], 0.67)
        self.assertAlmostEqual(row["unusual_share"], round(528_000 / 933_840, 3))
        # Time value: all four unusual contracts are at or out of the money, so their extrinsic
        # premium is their whole premium. The day's extrinsic premium is 933,840 less the 105P's
        # $5 of built-in value (249 * 5 * 100 = 124,500) = 809,340.
        self.assertEqual(row["unusual_extrinsic_premium"], 528_000)
        self.assertEqual(row["unusual_extrinsic_share"], round(528_000 / 809_340, 3))
        self.assertEqual(row["unusual_deep_itm_count"], 0)
        # Lean strength is now in time value: |417,000 - 232,500| / 809,340 (was / 933,840 = 0.198)
        self.assertAlmostEqual(row["lean_strength"], 0.228, places=3)
        self.assertEqual((row["bullish_extrinsic"], row["bearish_extrinsic"]), (417_000, 232_500))
        self.assertEqual(row["short_dated_share"], 0.487)
        top = row["top_contract"]
        self.assertEqual((top["contract"], top["premium"], top["side"]),
                         ("ACME261002C00105000", 230_000.0, "ask"))
        self.assertGreater(row["score"], 0)
        self.assertLessEqual(row["score"], 100)
        self.assertAlmostEqual(row["score"], sum(row["score_parts"].values()), delta=0.2)
        self.assertIn("4 unusual contracts", row["why"])
        self.assertIn("leans bullish", row["why"])
        self.assertNotIn("\n", row["why"])
        self.assertIsNone(TRADE_WORDS.search(row["why"]))

    def test_more_unusual_premium_scores_higher(self):
        scores = [scan.score_flow(flow.analyze(FLOW_RAWS[s]))["score"] for s in ("BIG", "MID", "SMALL")]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(scores[0], scores[1])

    def test_zero_unusual_scores_zero(self):
        row = scan.score_flow(flow.analyze(FLOW, min_premium=1e12))
        self.assertEqual(row["unusual_count"], 0)
        self.assertEqual(row["score"], 0)
        self.assertIsNone(row["top_contract"])
        self.assertIn("routine", row["why"])

    def test_partial_premium_when_more_are_unusual_than_listed(self):
        res = flow.analyze(FLOW)
        res["unusual_count"] = 40  # flow lists only its top 15 by time value
        row = scan.score_flow(res)
        self.assertTrue(row["unusual_premium_partial"])
        self.assertIn("at least", row["why"])

    def test_empty_result_does_not_crash(self):
        row = scan.score_flow(flow.analyze({}))
        self.assertEqual(row["score"], 0)
        json.dumps(row, allow_nan=False)


class GexScoreTest(unittest.TestCase):
    def test_fixture_is_near_the_flip_only(self):
        row = gex_row(GEX)
        self.assertEqual(row["regime"], "positive")
        self.assertEqual((row["flip"], row["flip_pct"]), (98.79, -1.21))
        self.assertEqual((row["call_wall"], row["call_wall_pct"]), (105.0, 5.0))
        self.assertEqual((row["put_wall"], row["put_wall_pct"]), (95.0, -5.0))
        self.assertEqual(row["tags"], ["near_flip"])
        # 35 * (1 - 1.21 / 3)
        self.assertAlmostEqual(row["score"], 20.9, places=1)
        self.assertIn("1.21% above the gamma flip", row["why"])

    def test_setup_tags(self):
        expected = {"WALL": {"near_call_wall"}, "NEG": {"negative_gamma"},
                    "EXP": {"expiry_heavy"}, "QUIET": set()}
        for sym, tags in expected.items():
            with self.subTest(sym):
                row = gex_row(GEX_RAWS[sym])
                self.assertTrue(tags <= set(row["tags"]), row["tags"])
                if not tags:
                    self.assertEqual(row["tags"], [])
                    self.assertEqual(row["score"], 0)
                    self.assertIn("No setup flagged", row["why"])
                self.assertIsNone(TRADE_WORDS.search(row["why"]))
        neg = gex_row(GEX_RAWS["NEG"])
        self.assertEqual(neg["regime"], "negative")
        self.assertLessEqual(neg["net_to_gross_pct"], -25)
        self.assertIn("amplify", neg["why"])
        exp = gex_row(GEX_RAWS["EXP"])
        self.assertEqual(exp["nearest_expiry_dte"], 4)
        self.assertGreaterEqual(exp["nearest_expiry_share"], 0.4)

    def test_expiry_share_only_counts_within_a_week(self):
        row = gex_row(gex_raw(near_oi_scale=4))  # same share, but 8 DTE
        self.assertNotIn("expiry_heavy", row["tags"])
        self.assertEqual(row["score_parts"]["expiry"], 0)

    def test_scaled_up_chain_scores_the_same(self):
        small = gex_row(GEX)
        more_oi = gex_row(gex_raw(oi_scale=100))
        pricier = gex_row(gex_raw(price_scale=10))
        self.assertAlmostEqual(more_oi["net_gex"], small["net_gex"] * 100, delta=1)
        self.assertGreater(pricier["net_gex"], small["net_gex"] * 9)
        self.assertEqual(more_oi["score"], small["score"])
        self.assertEqual(more_oi["tags"], small["tags"])
        self.assertAlmostEqual(pricier["score"], small["score"], delta=0.2)
        self.assertEqual(pricier["tags"], small["tags"])

    def test_bigger_chain_does_not_outrank_a_better_setup(self):
        # BIGCAP has 1000x the open interest but spot a little further from the flip.
        raws = {"SMALLCAP": gex_raw(), "BIGCAP": gex_raw(oi_scale=1000, spot=100.5)}
        res = scan.scan("gex", list(raws), fetch=from_dict(raws))
        self.assertEqual([r["symbol"] for r in res["ranked"]], ["SMALLCAP", "BIGCAP"])
        big, small = res["ranked"][1], res["ranked"][0]
        self.assertGreater(abs(big["net_gex"]), abs(small["net_gex"]) * 900)

    def test_unknown_regime_scores_zero(self):
        row = scan.score_gex(gex.analyze(load("gex_empty.json")))
        self.assertEqual((row["regime"], row["score"], row["tags"]), ("unknown", 0, []))
        self.assertIn("No usable gamma", row["why"])


class ScanTest(unittest.TestCase):
    def test_flow_ranking_and_zero_unusual_exclusion(self):
        res = scan.scan("flow", ["mid", "QUIET", "big", "SMALL"], fetch=from_dict(FLOW_RAWS))
        self.assertEqual((res["kind"], res["scanned"], res["ok"], res["errors"]), ("flow", 4, 4, {}))
        self.assertEqual([r["symbol"] for r in res["ranked"]], ["BIG", "MID", "SMALL"])
        self.assertEqual(res["unranked"], ["QUIET"])
        self.assertEqual(res["as_of"], "2026-09-30T16:15:00")
        row = res["ranked"][0]
        for key in ("symbol", "spot", "unusual_premium", "unusual_count", "lean",
                    "put_call_premium_ratio", "top_contract", "why"):
            self.assertIn(key, row)

    def test_zero_unusual_ranks_last_when_included(self):
        raws = {"QUIET": flow_raw(cap=10), "SMALL": flow_raw(0.5)}
        res = scan.scan("flow", ["QUIET", "SMALL"], fetch=from_dict(raws), include_unranked=True)
        self.assertEqual([r["symbol"] for r in res["ranked"]], ["SMALL", "QUIET"])
        self.assertEqual(res["ranked"][-1]["score"], 0)

    def test_gex_ranking_order(self):
        res = scan.scan("gex", list(GEX_RAWS), fetch=from_dict(GEX_RAWS))
        order = [r["symbol"] for r in res["ranked"]]
        self.assertEqual(order[0], "EXP")      # flip, both walls and expiry all line up
        self.assertEqual(order[-1], "QUIET")   # nothing near
        self.assertEqual(order, ["EXP", "FLIP", "NEG", "WALL", "QUIET"])
        scores = [r["score"] for r in res["ranked"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(res["as_of"], "2026-10-05T15:45:00")  # the latest chain timestamp
        for key in ("symbol", "spot", "regime", "net_gex", "flip", "flip_pct", "call_wall",
                    "call_wall_pct", "put_wall", "put_wall_pct", "tags", "why"):
            self.assertIn(key, res["ranked"][0])

    def test_gex_unusable_chain_is_unranked(self):
        raws = {"FLIP": gex_raw(), "EMPTY": load("gex_empty.json")}
        res = scan.scan("gex", list(raws), fetch=from_dict(raws))
        self.assertEqual(([r["symbol"] for r in res["ranked"]], res["unranked"]), (["FLIP"], ["EMPTY"]))

    def test_one_failure_never_stops_the_scan(self):
        def fetch(sym):
            if sym == "BLOCKED":
                raise FetchError("could not reach cdn-api.cboe.com (Tunnel connection failed: 403 "
                                 "Forbidden). If this runs in a sandbox, add cdn-api.cboe.com to its "
                                 "allowed network domains.")
            if sym == "BROKEN":
                raise RuntimeError("bad payload")
            if sym == "NONE":
                return None
            return FLOW_RAWS["MID"]

        res = scan.scan("flow", ["BLOCKED", "MID", "BROKEN", "NONE"], fetch=fetch)
        self.assertEqual((res["scanned"], res["ok"]), (4, 1))
        self.assertEqual([r["symbol"] for r in res["ranked"]], ["MID"])
        self.assertEqual(set(res["errors"]), {"BLOCKED", "BROKEN", "NONE"})
        self.assertTrue(res["errors"]["BLOCKED"].startswith("could not reach cdn-api.cboe.com"))
        self.assertEqual(res["errors"]["BROKEN"], "RuntimeError: bad payload")
        self.assertTrue(res["errors"]["NONE"].startswith("AttributeError"))

    def test_all_fail(self):
        def fetch(sym):
            raise FetchError("could not reach cdn-api.cboe.com (blocked)")
        res = scan.scan("gex", ["A", "B"], fetch=fetch)
        self.assertEqual((res["ok"], res["ranked"], res["as_of"]), (0, [], None))
        self.assertEqual(len(res["errors"]), 2)

    def test_symbols_are_cleaned_and_fetched_once(self):
        calls = []
        lock = threading.Lock()

        def fetch(sym):
            with lock:
                calls.append(sym)
            return FLOW_RAWS["MID"]

        res = scan.scan("flow", ["mid", " MID ", "$mid", "", "small"], fetch=fetch)
        self.assertEqual(sorted(calls), ["MID", "SMALL"])
        self.assertEqual(res["scanned"], 2)

    def test_fetches_run_concurrently(self):
        barrier = threading.Barrier(2, timeout=5)

        def fetch(sym):
            barrier.wait()  # breaks (and errors) if the two fetches are not in flight together
            return FLOW_RAWS["MID"]

        res = scan.scan("flow", ["A", "B"], workers=2, fetch=fetch)
        self.assertEqual(res["errors"], {})

    def test_default_fetch_skips_the_chain_cache(self):
        with mock.patch.object(scan, "fetch_json", return_value=FLOW_RAWS["MID"]) as fj, \
                mock.patch.object(cboe, "fetch_chain", side_effect=AssertionError("cached path")):
            res = scan.scan("flow", ["SPX"])
        fj.assert_called_once_with(cboe.chain_url("SPX"))
        self.assertEqual(res["ok"], 1)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            scan.scan("news", ["SPY"])

    def test_output_is_strict_json(self):
        cases = {
            "flow": dict(FLOW_RAWS, EMPTY=load("flow_empty.json"), STALE=load("flow_stale.json")),
            "gex": dict(GEX_RAWS, EMPTY=load("gex_empty.json"), NOIV=load("gex_missing_iv.json")),
        }
        for kind, raws in cases.items():
            raws = dict(raws, NULL={})

            def fetch(sym, raws=raws):
                if sym == "DOWN":
                    raise FetchError("could not reach cdn-api.cboe.com")
                return raws[sym]

            for include in (False, True):
                with self.subTest(kind=kind, include_unranked=include):
                    res = scan.scan(kind, list(raws) + ["DOWN"], fetch=fetch, include_unranked=include)
                    text = json.dumps(res, allow_nan=False)
                    self.assertEqual(json.loads(text), res)


class MarkdownTest(unittest.TestCase):
    def errors_scan(self, kind, raws):
        def fetch(sym):
            if sym in ("DOWN1", "DOWN2"):
                raise FetchError("could not reach cdn-api.cboe.com (Tunnel connection failed: 403 "
                                 "Forbidden). If this runs in a sandbox, add cdn-api.cboe.com to its "
                                 "allowed network domains.")
            if sym == "ODD":
                raise RuntimeError("bad payload")
            return raws[sym]
        return scan.scan(kind, list(raws) + ["DOWN1", "DOWN2", "ODD"], fetch=fetch)

    def test_flow_markdown(self):
        md = scan.to_markdown(self.errors_scan("flow", FLOW_RAWS))
        self.assertTrue(md.startswith("## Desk scan: flow\n"))
        self.assertIn("Scanned 7 symbols: 4 ok, 3 errors. Data as of 2026-09-30 16:15", md)
        self.assertIn("| 1 | BIG |", md)
        self.assertLess(md.index("| BIG |"), md.index("| MID |"))
        self.assertNotIn("| QUIET |", md)
        self.assertIn("Not ranked (no unusual contracts): QUIET.", md)
        error_lines = [ln for ln in md.splitlines() if ln.startswith("Errors")]
        self.assertEqual(error_lines, [
            "Errors (3): DOWN1, DOWN2 (could not reach cdn-api.cboe.com (Tunnel connection failed: "
            "403 Forbidden)); ODD (RuntimeError: bad payload)."])
        self.assertIn("_Caveat:", md)
        self.assertIn("15 minutes delayed", md)
        self.assertIn("not sweeps", md)
        self.assertIsNone(TRADE_WORDS.search(md))

    def test_gex_markdown(self):
        md = scan.to_markdown(self.errors_scan("gex", GEX_RAWS), top=2)
        self.assertTrue(md.startswith("## Desk scan: gex\n"))
        self.assertIn("| 1 | EXP |", md)
        self.assertIn("| 2 | FLIP |", md)
        self.assertNotIn("| 3 |", md)
        self.assertIn("showing the top 2", md)
        self.assertIn("98.79 (-1.21%)", md)
        self.assertIn("naive sign model", md)
        self.assertIn("Older data than the rest", md)  # EXP's chain is dated 10/05, the rest 10/01
        self.assertIsNone(TRADE_WORDS.search(md))

    def test_nothing_ranked(self):
        res = scan.scan("flow", ["QUIET"], fetch=from_dict(FLOW_RAWS))
        md = scan.to_markdown(res)
        self.assertIn("No names to rank.", md)
        self.assertNotIn("Errors", md)


WATCHLIST = """# Watchlist

One ticker per line. Anything after a dash is a note for the agents.
Every tracker thread reads this file, so edit it here to change what they follow.

SPY - market backdrop for every other name
NVDA - AI bellwether
## Earnings season
A note: these lines are prose and must be skipped.
I think AMD is next.
- TSLA - bullet form
amd  # inline comment, lowercase alone
QQQ: tech
brk.b - class B shares
F
spy - duplicate
note that this lowercase sentence is prose
| SPY | table row |
"""


class UniverseTest(unittest.TestCase):
    def test_default_universe(self):
        u = universe.DEFAULT_UNIVERSE
        self.assertIsInstance(u, tuple)
        self.assertTrue(70 <= len(u) <= 90, len(u))
        self.assertEqual(len(set(u)), len(u))
        for sym in ("SPY", "QQQ", "IWM", "DIA", "TLT", "TSLA", "NVDA", "AAPL", "MSTR", "PFE"):
            self.assertIn(sym, u)
        for sym in u:
            self.assertEqual(universe.parse_line(sym), sym)

    def test_watchlist_format(self):
        self.assertEqual(universe.parse_universe(WATCHLIST),
                         ("SPY", "NVDA", "TSLA", "AMD", "QQQ", "BRK.B", "F"))

    def test_plain_file(self):
        text = "# my universe\n\nspy\nIWM some note\nIWM\n  xle  \nGLD # gold\nTOOLONG\n"
        self.assertEqual(universe.parse_universe(text), ("SPY", "IWM", "XLE", "GLD"))

    def test_load_from_path_env_and_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u.txt")
            with open(path, "w") as f:
                f.write(WATCHLIST)
            self.assertEqual(universe.load_universe(path)[:2], ("SPY", "NVDA"))
            with mock.patch.dict(os.environ, {"OPPTIONS_UNIVERSE": path}):
                self.assertEqual(universe.load_universe()[:2], ("SPY", "NVDA"))
            with mock.patch.dict(os.environ, {"OPPTIONS_UNIVERSE": os.path.join(d, "missing.txt")}):
                self.assertEqual(universe.load_universe(), universe.DEFAULT_UNIVERSE)
            self.assertEqual(universe.load_universe(), universe.DEFAULT_UNIVERSE)
            with self.assertRaises(OSError):
                universe.load_universe(os.path.join(d, "missing.txt"))
            empty = os.path.join(d, "empty.md")
            with open(empty, "w") as f:
                f.write("# Watchlist\n\nNothing here yet.\n")
            with self.assertRaises(ValueError):
                universe.load_universe(empty)


class CliTest(unittest.TestCase):
    def run_cli(self, argv, raws=None, fetch=None):
        fetch = fetch or from_dict(raws)
        with mock.patch.object(scan, "fetch_raw", side_effect=fetch) as m, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            status = cli.main(argv)
        return status, out.getvalue(), err.getvalue(), m

    def test_scan_flow_markdown(self):
        status, out, err, _ = self.run_cli(["scan", "flow", "mid", "big", "quiet"], FLOW_RAWS)
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("## Desk scan: flow"))
        self.assertLess(out.index("| BIG |"), out.index("| MID |"))
        self.assertEqual(err, "")

    def test_scan_gex_json_with_top(self):
        status, out, _, _ = self.run_cli(["scan", "gex", "--json", "--top", "2"] + list(GEX_RAWS), GEX_RAWS)
        self.assertEqual(status, 0)
        res = json.loads(out)
        json.dumps(res, allow_nan=False)
        self.assertEqual([r["symbol"] for r in res["ranked"]], ["EXP", "FLIP"])
        self.assertEqual(res["ok"], 5)

    def test_universe_file_when_no_symbols(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "watchlist.md")
            with open(path, "w") as f:
                f.write("# Watchlist\n\nOne ticker per line.\n\nMID - note\nBIG\n")
            status, out, _, m = self.run_cli(["scan", "flow", "--universe", path], FLOW_RAWS)
        self.assertEqual(status, 0)
        self.assertEqual(sorted(c.args[0] for c in m.call_args_list), ["BIG", "MID"])
        self.assertIn("Scanned 2 symbols: 2 ok", out)

    def test_default_universe_when_nothing_given(self):
        status, out, _, m = self.run_cli(["scan", "flow", "--workers", "8"],
                                         fetch=lambda sym: FLOW_RAWS["MID"])
        self.assertEqual(status, 0)
        self.assertEqual(sorted(c.args[0] for c in m.call_args_list), sorted(universe.DEFAULT_UNIVERSE))

    def test_exit_status(self):
        def some_fail(sym):
            if sym == "MID":
                return FLOW_RAWS["MID"]
            raise FetchError("could not reach cdn-api.cboe.com (blocked)")

        status, _, err, _ = self.run_cli(["scan", "flow", "MID", "BAD"], fetch=some_fail)
        self.assertEqual(status, 0)
        self.assertIn("1 of 2 symbols failed", err)
        status, out, err, _ = self.run_cli(["scan", "gex", "BAD", "WORSE"], fetch=some_fail)
        self.assertEqual(status, 1)
        self.assertIn("could not reach cdn-api.cboe.com", err)
        self.assertIn("Errors (2)", out)

    def test_missing_universe_file(self):
        status, _, err, m = self.run_cli(["scan", "gex", "--universe", "/nonexistent/u.txt"], {})
        self.assertEqual(status, 1)
        self.assertIn("could not read the universe file", err)
        m.assert_not_called()

    def test_bad_arguments_exit_2(self):
        for argv in (["scan"], ["scan", "news"], ["scan", "flow", "--top", "0"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.main(argv)
            self.assertEqual(cm.exception.code, 2)

    def test_tracker_commands_unchanged(self):
        with mock.patch.object(cboe, "fetch_chain", return_value=FLOW), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            status = cli.main(["flow", "acme"])
        self.assertEqual(status, 0)
        self.assertTrue(out.getvalue().startswith("## Options Activity"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["gex"])  # symbols are still required for tracker commands



def expired_raw():
    """flow_chain.json read the next morning: stamped 10/03, so its 10/02 contracts (6 of 13,
    including the $230K 105 call) have expired, while every trade is still from 09/30."""
    return flow_raw(timestamp="2026-10-03 12:05:00")


class LiveContractsTest(unittest.TestCase):
    def test_expired_contracts_are_left_out_of_flow(self):
        raw = expired_raw()
        before = copy.deepcopy(raw)
        res = scan.scan("flow", ["NEXT"], fetch=lambda sym: raw)
        row = res["ranked"][0]
        self.assertEqual(row["expired_left_out"], 6)
        # 528,000 minus the expired 105 call (230,000)
        self.assertEqual((row["unusual_count"], row["unusual_premium"]), (3, 298_000))
        self.assertFalse(row["top_contract"]["expiry"] < "2026-10-03")
        self.assertEqual(row["session_date"], "2026-09-30")
        self.assertEqual(raw, before)  # the fetched chain is not changed
        # The tracker on its own still counts the expired contracts.
        self.assertEqual(flow.analyze(raw)["unusual_count"], 4)

    def test_live_chain_keeps_everything(self):
        row = scan.scan("flow", ["MID"], fetch=from_dict(FLOW_RAWS))["ranked"][0]
        self.assertEqual((row["expired_left_out"], row["unusual_premium"]), (0, 528_000))

    def test_odd_shapes_pass_through(self):
        no_stamp = {"data": {"options": [{"option": "ACME200101C00100000"}]}}
        bad_symbol = {"timestamp": "2026-10-03 12:00:00",
                      "data": {"options": [{"option": "junk"}, "not a dict"]}}
        for raw in (None, {}, [], {"data": None}, {"data": {"options": "x"}}, no_stamp, bad_symbol):
            with self.subTest(raw=raw):
                out, dropped = scan.live_contracts(raw)
                self.assertIs(out, raw)
                self.assertEqual(dropped, 0)

    def test_gex_is_unchanged(self):
        # gex.analyze already leaves out dte < 0, so the GEX scan reads the chain as is.
        raw = gex_raw(timestamp="2026-10-12 15:45:00")  # the 10/09 expiry is now expired
        row = scan.scan("gex", ["XYZ"], fetch=lambda sym: raw)["ranked"][0]
        self.assertEqual(row, scan.scan("gex", ["XYZ"], fetch=lambda sym: copy.deepcopy(raw))["ranked"][0])
        self.assertNotIn("expired_left_out", row)


class ThreadSafetyTest(unittest.TestCase):
    def test_shared_chains_are_not_mutated_and_threads_agree(self):
        # One raw object per kind is handed to every symbol at once, as a cache would.
        for kind, raw in (("flow", expired_raw()), ("gex", gex_raw(put_oi_scale=3))):
            with self.subTest(kind=kind):
                before = copy.deepcopy(raw)
                syms = [f"S{chr(65 + i)}" for i in range(12)]
                parallel = scan.scan(kind, syms, workers=6, fetch=lambda sym: raw)
                serial = scan.scan(kind, syms, workers=1, fetch=lambda sym: copy.deepcopy(before))
                self.assertEqual(raw, before)
                self.assertEqual(parallel, serial)
                self.assertEqual(parallel["ok"], 12)

    def test_workers_are_capped(self):
        sizes = []
        real = scan.ThreadPoolExecutor

        def recording(max_workers):
            sizes.append(max_workers)
            return real(max_workers=max_workers)

        syms = [f"Z{chr(65 + i)}{chr(65 + j)}" for i in range(5) for j in range(8)]
        with mock.patch.object(scan, "ThreadPoolExecutor", side_effect=recording):
            res = scan.scan("flow", syms, workers=500, fetch=lambda sym: FLOW_RAWS["MID"])
        self.assertEqual(sizes, [scan.MAX_WORKERS])
        self.assertEqual(res["ok"], 40)

    def test_scorers_read_no_clock_or_env(self):
        self.assertFalse({"datetime", "date", "time"} & set(vars(scan)))
        flow_res, gex_res = flow.analyze(FLOW), gex.analyze(GEX, rate=0.04)
        plain = (scan.score_flow(flow_res), scan.score_gex(gex_res))
        with mock.patch.dict(os.environ, {"OPPTIONS_MIN_PREMIUM": "1", "OPPTIONS_MIN_VOLUME": "1",
                                          "OPPTIONS_RATE": "0.2"}):
            self.assertEqual((scan.score_flow(flow_res), scan.score_gex(gex_res)), plain)


class SymbolsTest(unittest.TestCase):
    def test_a_string_is_one_symbol_not_letters(self):
        calls = []
        res = scan.scan("flow", "mid", fetch=lambda sym: calls.append(sym) or FLOW_RAWS["MID"])
        self.assertEqual((calls, res["scanned"]), (["MID"], 1))

    def test_comma_lists_split_and_odd_items_are_reported_not_fetched(self):
        calls = []
        lock = threading.Lock()

        def fetch(sym):
            with lock:
                calls.append(sym)
            return FLOW_RAWS[sym]

        res = scan.scan("flow", ["MID,small", "big quiet", "AB/CD", "../X"], fetch=fetch)
        self.assertEqual(sorted(calls), ["BIG", "MID", "QUIET", "SMALL"])
        self.assertEqual(sorted(res["errors"]), ["../X", "AB/CD"])
        self.assertIn("not fetched", res["errors"]["AB/CD"])
        self.assertEqual((res["scanned"], res["ok"]), (6, 4))


class ScoreHardeningTest(unittest.TestCase):
    def test_gex_score_ignores_dollar_gex(self):
        res = gex.analyze(GEX_RAWS["EXP"], rate=0.04)
        big = copy.deepcopy(res)
        for k in ("total_call_gex", "total_put_gex", "total_net_gex", "gross_gex"):
            big[k] *= 1e6
        big["call_wall"]["gex"] *= 1e6
        big["put_wall"]["gex"] *= 1e6
        for k in ("model_net_gex_at_spot", "model_gross_gex_at_spot"):
            big["flip_sweep"][k] *= 1e6
        a, b = scan.score_gex(res), scan.score_gex(big)
        self.assertEqual((a["score"], a["score_parts"], a["tags"], a["why"]),
                         (b["score"], b["score_parts"], b["tags"], b["why"]))

    def test_odd_numbers_in_the_top_contract_stay_strict_json(self):
        res = flow.analyze(FLOW)
        res["unusual"] = [dict(res["unusual"][0], strike=float("nan"), volume=float("inf"))]
        row = scan.score_flow(res)
        self.assertIsNone(row["top_contract"]["strike"])
        self.assertIsNone(row["top_contract"]["volume"])
        json.dumps(row, allow_nan=False)


def option(sym, bid, ask, last, volume, oi, delta):
    return {"option": sym, "bid": bid, "ask": ask, "last_trade_price": last, "volume": volume,
            "open_interest": oi, "delta": delta, "gamma": 0.001, "iv": 0.3, "theo": (bid + ask) / 2,
            "last_trade_time": "2026-09-30T15:50:00"}


def deep_chain(extra=()):
    """DEEP, spot 331 on 2026-09-30, like the live AAPL report: its only unusual contract is a
    deep in-the-money LEAPS call block, 1,000 x 2029-01-19 170C at 173 (delta 0.97) printed at
    the ask: premium $17.3M, of which 161 is built-in value and 12 is time value ($1.2M).
    Two routine contracts (volume below open interest) trade $1.15M of time value."""
    return {"timestamp": "2026-09-30 16:15:00",
            "data": {"symbol": "DEEP", "current_price": 331.0, "iv30": 25.0, "options": [
                option("DEEP290119C00170000", 172.0, 174.0, 174.0, 1000, 50, 0.97),
                option("DEEP261016C00335000", 6.9, 7.1, 7.0, 900, 4000, 0.45),
                option("DEEP261016P00325000", 6.4, 6.6, 6.5, 800, 3000, -0.40),
                *extra]}}


def without_time_value(res):
    """A flow result as it looked before time value, so score_flow scores total premium."""
    res = copy.deepcopy(res)
    for k in ("extrinsic", "unusual_deep_itm_count"):
        res.pop(k)
    for r in res["unusual"]:
        for k in ("intrinsic", "extrinsic", "extrinsic_premium", "deep_itm"):
            r.pop(k)
    return res


class DeepInTheMoneyTest(unittest.TestCase):
    def test_deep_itm_leaps_block_ranks_below_smaller_otm_activity(self):
        raws = {"DEEP": deep_chain(), "MID": flow_raw()}
        res = scan.scan("flow", list(raws), fetch=from_dict(raws))
        self.assertEqual([r["symbol"] for r in res["ranked"]], ["MID", "DEEP"])
        mid, deep = res["ranked"]
        # MID is smaller both in premium ($528K vs $17.3M) and in time value ($528K vs $1.2M).
        self.assertEqual((deep["unusual_premium"], deep["unusual_extrinsic_premium"]), (17_300_000, 1_200_000))
        self.assertGreater(deep["unusual_premium"], 30 * mid["unusual_premium"])
        self.assertGreater(deep["unusual_extrinsic_premium"], mid["unusual_extrinsic_premium"])
        # Deep ITM counts for no breadth and no lean.
        self.assertEqual(deep["unusual_deep_itm_count"], 1)
        self.assertEqual((deep["score_parts"]["breadth"], deep["score_parts"]["lean"]), (0, 0))
        self.assertEqual(deep["lean"], "mixed")
        self.assertTrue(deep["top_contract"]["deep_itm"])
        self.assertIn("it is deep in the money", deep["why"])
        self.assertIn("stock replacement or rolls", deep["why"])
        self.assertIsNone(TRADE_WORDS.search(deep["why"]))

    def test_the_drop_against_total_premium_scoring(self):
        deep_res = flow.analyze(deep_chain())
        mid_res = flow.analyze(FLOW)
        old_deep, old_mid = (scan.score_flow(without_time_value(r))["score"] for r in (deep_res, mid_res))
        new_deep, new_mid = scan.score_flow(deep_res)["score"], scan.score_flow(mid_res)["score"]
        self.assertGreater(old_deep, old_mid)       # what the first live run showed: DEEP on top
        self.assertLess(new_deep, new_mid)
        self.assertGreaterEqual(old_deep - new_deep, 30)
        # Hand-checked. The day: 17.3M (block) + 630K (335C, 7.00) + 520K (325P, 6.50) premium;
        # time value 1.2M + 630K + 520K = 2.35M.
        # Old: size 40 * log10(17.3M / 50K) / 3 = 33.86; breadth 20 * ln 2 / ln 26 = 4.25;
        #      concentration 17.3 / 18.45 = 94% -> 20; lean 17.3 / 18.45 -> 20. Total 78.1.
        # New: size 40 * log10(1.2M / 50K) / 3 = 18.40; breadth 0 (deep); concentration
        #      1.2 / 2.35 = 51% -> 20 (the block still earns it in so small a chain); lean 0.
        self.assertEqual((old_deep, new_deep), (78.1, 38.4))
        self.assertEqual(scan.score_flow(deep_res)["score_parts"],
                         {"size": 18.4, "breadth": 0.0, "concentration": 20.0, "lean": 0.0})

    def test_deep_itm_does_not_count_toward_breadth(self):
        # Add two out-of-the-money unusual calls: 3 unusual contracts, 2 of them count.
        extra = (option("DEEP261016C00350000", 2.9, 3.1, 3.1, 5000, 100, 0.30),
                 option("DEEP261016C00360000", 0.9, 1.1, 1.1, 2000, 100, 0.15))
        row = scan.score_flow(flow.analyze(deep_chain(extra)))
        self.assertEqual((row["unusual_count"], row["unusual_deep_itm_count"]), (3, 1))
        two = scan.FLOW_W_BREADTH * math.log1p(2) / math.log1p(scan.FLOW_COUNT_FULL)
        self.assertAlmostEqual(row["score_parts"]["breadth"], round(two, 1))
        # Top contract by time value: the 350 call ($1.5M), not the $17.3M deep block ($1.2M).
        self.assertEqual(row["top_contract"]["contract"], "DEEP261016C00350000")
        self.assertEqual([c["contract"] for c in row["contracts"]],
                         ["DEEP261016C00350000", "DEEP290119C00170000", "DEEP261016C00360000"])
        self.assertIn("; 1 of them is deep in the money, mostly built-in value (often stock replacement "
                      "or rolls), so it is left out of the contract count and the lean;", row["why"])
        self.assertEqual(row["lean"], "bullish")    # from the two calls at the ask, block left out

    def test_contracts_are_kept_for_the_morning_confirmation(self):
        res = scan.scan("flow", ["MID", "BIG"], fetch=from_dict(FLOW_RAWS))
        rows = {r["symbol"]: r for r in res["ranked"]}
        mid = rows["MID"]["contracts"]
        self.assertEqual([c["contract"] for c in mid],
                         ["ACME261002C00105000", "ACME261016C00100000",
                          "ACME261016P00100000", "ACME261016C00110000"])
        self.assertEqual(set(mid[0]), set(scan.CONTRACT_FIELDS))
        self.assertEqual((mid[0]["volume"], mid[0]["open_interest"], mid[0]["side"], mid[0]["deep_itm"]),
                         (2000, 500, "ask", False))
        self.assertEqual(mid[0]["extrinsic_premium"], 230_000)
        big = rows["BIG"]["contracts"]
        self.assertEqual(rows["BIG"]["unusual_count"], 9)
        self.assertEqual(len(big), scan.FLOW_CONTRACTS)
        ext = [c["extrinsic_premium"] for c in big]
        self.assertEqual(ext, sorted(ext, reverse=True))
        json.dumps(res, allow_nan=False)

    def test_markdown_names_time_value_and_deep_itm(self):
        raws = {"DEEP": deep_chain(), "MID": flow_raw()}
        md = scan.to_markdown(scan.scan("flow", list(raws), fetch=from_dict(raws)))
        self.assertIn("Unusual time value (premium, n)", md)
        self.assertIn("$1.20M ($17.30M, 1, 1 deep ITM)", md)
        self.assertIn("170 call 2029-01-19, deep ITM (~$1.20M time value", md)
        self.assertIn("all in time value", md)
        self.assertIn("deep in-the-money options (|delta| 0.90 or more)", md)
        self.assertIn("opptions confirm", md)
        self.assertIsNone(TRADE_WORDS.search(md))


class ConfirmableContractsTest(unittest.TestCase):
    """The "contracts" kept for `opptions confirm` and the deep ITM wording of the why line."""

    def test_same_day_expiry_is_not_kept_for_the_morning_check(self):
        # A 4:20 PM scan on 2026-09-30 with a same-day (0DTE) 335 call: 400,000 x 0.05 (bid 0,
        # ask 0.05) = $2.0M, all time value, so it is the top contract. It expires before the
        # overnight open interest update, so "contracts" skips it.
        extra = (option("DEEP260930C00335000", 0.0, 0.05, 0.05, 400_000, 1000, 0.30),
                 option("DEEP261016C00350000", 2.9, 3.1, 3.1, 5000, 100, 0.30))
        row = scan.score_flow(flow.analyze(deep_chain(extra)))
        self.assertEqual(row["session_date"], "2026-09-30")
        self.assertEqual((row["top_contract"]["contract"], row["top_contract"]["dte"]),
                         ("DEEP260930C00335000", 0))
        self.assertEqual(row["top_contract"]["extrinsic_premium"], 2_000_000)
        self.assertEqual([c["contract"] for c in row["contracts"]],
                         ["DEEP261016C00350000", "DEEP290119C00170000"])

    def test_outlives_session(self):
        self.assertFalse(scan._outlives_session({"expiry": "2026-09-30", "dte": 0}, "2026-09-30"))
        self.assertTrue(scan._outlives_session({"expiry": "2026-10-01", "dte": 1}, "2026-09-30"))
        # The session date decides over dte (a late-evening UTC timestamp can make dte 0).
        self.assertTrue(scan._outlives_session({"expiry": "2026-10-01", "dte": 0}, "2026-09-30"))
        # No session date: dte decides; nothing to go on keeps the contract.
        self.assertFalse(scan._outlives_session({"dte": 0}, None))
        self.assertTrue(scan._outlives_session({"dte": 3}, None))
        self.assertTrue(scan._outlives_session({}, None))

    def test_why_line_with_two_of_three_deep(self):
        extra = (option("DEEP290119C00200000", 132.0, 134.0, 134.0, 1000, 50, 0.95),
                 option("DEEP261016C00350000", 2.9, 3.1, 3.1, 5000, 100, 0.30))
        row = scan.score_flow(flow.analyze(deep_chain(extra)))
        self.assertEqual((row["unusual_count"], row["unusual_deep_itm_count"]), (3, 2))
        self.assertIn("; 2 of them are deep in the money, mostly built-in value (often stock replacement "
                      "or rolls), so those are left out of the contract count and the lean;", row["why"])
        one = scan.FLOW_W_BREADTH * math.log1p(1) / math.log1p(scan.FLOW_COUNT_FULL)
        self.assertAlmostEqual(row["score_parts"]["breadth"], round(one, 1))


def older_session_raw():
    """flow_chain.json with every trade moved to 09/29: the chain still says 09/30, so the flow
    tracker reads the 09/29 session."""
    raw = flow_raw()
    for o in raw["data"]["options"]:
        o["last_trade_time"] = o["last_trade_time"].replace("2026-09-30", "2026-09-29")
    return raw


class MarkdownHonestyTest(unittest.TestCase):
    def test_http_errors_with_urls_group_on_one_short_line(self):
        syms = [f"Z{chr(65 + i)}{chr(65 + j)}" for i in range(5) for j in range(6)]

        def fetch(sym):
            url = cboe.chain_url(sym)
            raise FetchError(f"cdn-api.cboe.com answered HTTP 403 for {url}")

        res = scan.scan("gex", syms, fetch=fetch)
        self.assertIn(cboe.chain_url("ZAA"), res["errors"]["ZAA"])  # JSON keeps the full message
        line = next(ln for ln in scan.to_markdown(res).splitlines() if ln.startswith("Errors"))
        self.assertTrue(line.endswith("(cdn-api.cboe.com answered HTTP 403)."), line)
        self.assertNotIn("https://", line)
        self.assertLess(len(line), 30 * 5 + 80)

    def test_flow_names_the_session_and_flags_an_older_one(self):
        raws = {"MID": flow_raw(), "OLD": older_session_raw()}
        md = scan.to_markdown(scan.scan("flow", list(raws), fetch=from_dict(raws)))
        self.assertIn("Volume is from the 2026-09-30 session.", md)
        self.assertIn("Older session than the rest: OLD (2026-09-29).", md)
        self.assertIn("no time zone", md)
        self.assertIn("already expired", md)
        self.assertIsNone(TRADE_WORDS.search(md))

    def test_same_session_flags_nothing(self):
        md = scan.to_markdown(scan.scan("flow", ["MID", "BIG"], fetch=from_dict(FLOW_RAWS)))
        self.assertNotIn("Older", md)


REALISTIC_WATCHLIST = """# Watchlist

One ticker per line. Anything after a dash is a note for the agents.
NOTE: earnings season starts next week, so expect wider moves.
TODO: add two more semis
IV is elevated across tech.
AI names below are the core of the book.
FOMC week: keep size small
SWING TRADES:
ETF list:
PM session was weak for most names.

## Core
**NVDA** - AI bellwether
1. AMD - second source
2) TSLA
- [ ] MSFT - check the cloud numbers
- [x] AAPL - done
`QQQ` - tech
META, GOOGL, AMZN
SPY — market backdrop (em dash)
BRK-B - class B shares
AI - C3.ai, a real ticker when written with a note
PM - Philip Morris

| Ticker | Note |
|---|---|
| PLTR | defense software |
"""


class WatchlistFormatsTest(unittest.TestCase):
    def test_realistic_watchlist(self):
        self.assertEqual(universe.parse_universe(REALISTIC_WATCHLIST), (
            "NVDA", "AMD", "TSLA", "MSFT", "AAPL", "QQQ", "META", "GOOGL", "AMZN", "SPY",
            "BRK.B", "AI", "PM", "PLTR"))

    def test_project_watchlist_shape(self):
        text = ("# Watchlist\n\nOne ticker per line. Anything after a dash is a note for the agents.\n"
                "Every tracker thread reads this file, so edit it here to change what they follow.\n\n"
                "SPY - market backdrop for every other name\n")
        self.assertEqual(universe.parse_universe(text), ("SPY",))

    def test_parse_line_keeps_its_contract(self):
        self.assertEqual(universe.parse_line("META, GOOGL"), "META")
        self.assertIsNone(universe.parse_line("NOTE: not a ticker"))
        self.assertEqual(universe.line_tickers("AAPL, the iPhone maker"), ["AAPL"])


class CliHonestyTest(unittest.TestCase):
    run_cli = CliTest.run_cli

    def test_env_universe_that_is_not_a_file_warns(self):
        with mock.patch.dict(os.environ, {"OPPTIONS_UNIVERSE": "/nonexistent/universe.md"}):
            status, _, err, m = self.run_cli(["scan", "flow"], fetch=lambda sym: FLOW_RAWS["MID"])
        self.assertEqual(status, 0)
        self.assertIn("OPPTIONS_UNIVERSE=/nonexistent/universe.md is not a file", err)
        self.assertEqual(len(m.call_args_list), len(universe.DEFAULT_UNIVERSE))

    def test_no_env_no_warning(self):
        status, _, err, _ = self.run_cli(["scan", "flow", "MID"], FLOW_RAWS)
        self.assertEqual((status, err), (0, ""))

    def test_blank_symbols_are_a_usage_error(self):
        for argv in (["scan", "flow", ""], ["scan", "gex", ",", " "]):
            with self.subTest(argv=argv):
                status, out, err, m = self.run_cli(argv, {})
                self.assertEqual((status, out), (2, ""))
                self.assertIn("no symbols to scan", err)
                m.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class GexWhyWallSideTest(unittest.TestCase):
    """Wall and flip wording depends on which side of the level spot sits."""

    def _row(self, **kw):
        row = {"regime": "positive", "spot": 100.0, "tags": [], "flip": 101.32, "flip_pct": 1.32,
               "call_wall": 100.5, "call_wall_pct": 0.5, "put_wall": 99.5, "put_wall_pct": -0.5,
               "net_to_gross_pct": 20.0, "regime_uncertain": False,
               "nearest_expiry": "2026-10-02", "nearest_expiry_dte": 1, "nearest_expiry_share": 0.1}
        row.update(kw)
        return row

    def test_put_wall_below_spot_is_support(self):
        why = scan.gex_why(self._row(tags=["near_put_wall"]))
        self.assertIn("0.50% above the put wall", why)
        self.assertIn("tends to act as support", why)

    def test_put_wall_above_spot_is_already_broken(self):
        # Live case from 2026-10-01: SBUX put wall 95 at +1.17% from spot.
        why = scan.gex_why(self._row(tags=["near_put_wall"], put_wall=95.0, put_wall_pct=0.22))
        self.assertIn("0.22% below the put wall", why)
        self.assertIn("already under it", why)
        self.assertNotIn("act as support", why)

    def test_call_wall_below_spot_is_cleared(self):
        why = scan.gex_why(self._row(tags=["near_call_wall"], call_wall=99.6, call_wall_pct=-0.4))
        self.assertIn("0.40% above the call wall", why)
        self.assertIn("already through it", why)
        self.assertNotIn("stall or pin", why)

    def test_call_wall_above_spot_pins(self):
        why = scan.gex_why(self._row(tags=["near_call_wall"]))
        self.assertIn("stall or pin", why)

    def test_flip_states_the_distance(self):
        why = scan.gex_why(self._row(tags=["near_flip"]))
        self.assertIn("a move of about 1.32% would cross it", why)
        self.assertNotIn("small move", why)

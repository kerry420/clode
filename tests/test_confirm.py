"""Tests for the morning flow confirmation (opptions/confirm.py). No network.

The evening scan is scan.scan("flow") over flow_chain.json (ACME, 2026-09-30 16:15),
saved through JSON as the desk saves scan-flow.json. Its MID row keeps four contracts:

  contract               volume  OI at scan
  ACME261002C00105000     2,000     500
  ACME261016C00100000       400     300
  ACME261016P00100000       300     250
  ACME261016C00110000       800       0

The morning chain is the same fixture stamped 2026-10-01 12:05 with new open interest.
"""

import contextlib
import copy
import io
import json
import os
import re
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from opptions import __main__ as cli
from opptions import confirm, scan
from opptions.http import FetchError

FIXTURES = Path(__file__).parent / "fixtures"
_ENV = mock.patch.dict(os.environ)
TRADE_WORDS = re.compile(r"\b(buy|buying|sell|selling|bought|sold)\b", re.I)


def setUpModule():
    _ENV.start()
    for name in ("OPPTIONS_MIN_VOLUME", "OPPTIONS_MIN_PREMIUM", "OPPTIONS_UNIVERSE"):
        os.environ.pop(name, None)


def tearDownModule():
    _ENV.stop()


with open(FIXTURES / "flow_chain.json") as f:
    FLOW = json.load(f)


def evening_scan(symbols=("MID",), raws=None):
    """`scan flow --json` as saved to scan-flow.json (a JSON round trip)."""
    raws = raws or {sym: FLOW for sym in symbols}
    res = scan.scan("flow", list(symbols), fetch=lambda sym: raws[sym])
    return json.loads(json.dumps(res))


def morning(oi=None, timestamp="2026-10-01 12:05:00", drop=()):
    """The fixture read the next morning: new open interest, no volume yet, some contracts gone."""
    raw = copy.deepcopy(FLOW)
    raw["timestamp"] = timestamp
    raw["data"]["options"] = [o for o in raw["data"]["options"] if o["option"] not in drop]
    for o in raw["data"]["options"]:
        o["volume"] = 0.0
        if oi and o["option"] in oi:
            o["open_interest"] = float(oi[o["option"]])
    return raw


# 105C: +1,800 on 2,000 (90%) opened; 100C: +80 on 400 (20%) partial;
# 100P: -30 (fell) closed; 110C: +500 on 800 (62.5%) opened.
UPDATED = {"ACME261002C00105000": 2300, "ACME261016C00100000": 380,
           "ACME261016P00100000": 220, "ACME261016C00110000": 500}


def by_contract(entry):
    return {c["contract"]: c for c in entry["contracts"]}


class ClassifyTest(unittest.TestCase):
    def test_thresholds_hand_checked(self):
        # volume 1,000: opened from +500, partial from +100, closed below +100 or on a fall.
        self.assertEqual(confirm.classify(1000, 200, 700), "confirmed_opened")
        self.assertEqual(confirm.classify(1000, 200, 699), "partial")
        self.assertEqual(confirm.classify(1000, 200, 300), "partial")
        self.assertEqual(confirm.classify(1000, 200, 299), "closed_or_daytrade")
        self.assertEqual(confirm.classify(1000, 200, 200), "closed_or_daytrade")
        self.assertEqual(confirm.classify(1000, 200, 150), "closed_or_daytrade")

    def test_the_spy_example_from_the_desk(self):
        # SPY Dec 31 650P: volume 75,239, OI 4,578 before and 79,607 after: +75,029 is 99.7%.
        self.assertEqual(confirm.classify(75_239, 4_578, 79_607), "confirmed_opened")


class ConfirmRowsTest(unittest.TestCase):
    def test_classifications(self):
        res = confirm.confirm_rows(evening_scan(), {"MID": morning(UPDATED)})
        self.assertEqual(res["kind"], "flow_confirm")
        self.assertEqual((res["scan_as_of"], res["scan_session_date"], res["checked_as_of"]),
                         ("2026-09-30T16:15:00", "2026-09-30", "2026-10-01T12:05:00"))
        entry = res["symbols"][0]
        self.assertEqual((entry["symbol"], entry["rank"], entry["status"]), ("MID", 1, "checked"))
        c = by_contract(entry)
        self.assertEqual(c["ACME261002C00105000"]["status"], "confirmed_opened")
        self.assertEqual((c["ACME261002C00105000"]["oi_at_scan"], c["ACME261002C00105000"]["oi_now"],
                          c["ACME261002C00105000"]["oi_change"], c["ACME261002C00105000"]["oi_change_vs_volume"]),
                         (500, 2300, 1800, 0.9))
        self.assertEqual(c["ACME261016C00100000"]["status"], "partial")
        self.assertEqual(c["ACME261016C00100000"]["oi_change_vs_volume"], 0.2)
        self.assertEqual(c["ACME261016P00100000"]["status"], "closed_or_daytrade")
        self.assertEqual(c["ACME261016P00100000"]["oi_change"], -30)
        self.assertEqual(c["ACME261016C00110000"]["status"], "confirmed_opened")   # 500 / 800
        self.assertEqual(entry["counts"]["confirmed_opened"], 2)
        self.assertEqual(res["counts"], {"confirmed_opened": 2, "partial": 1, "closed_or_daytrade": 1,
                                         "expired": 0, "oi_not_updated": 0, "unavailable": 0})
        self.assertEqual((res["oi_not_updated"], res["errors"]), ([], {}))
        self.assertIn("MID: 2 of 4 flagged contracts look opened", entry["summary"])
        self.assertIn("1 partly opened", entry["summary"])
        self.assertIn("1 looks closed or day-traded", entry["summary"])
        json.dumps(res, allow_nan=False)

    def test_small_rise_is_closed_or_day_trade(self):
        # 105C: +199 on 2,000 is under 10% -> closed or day trade; +200 is partial.
        res = confirm.confirm_rows(evening_scan(), {"MID": morning(dict(UPDATED, ACME261002C00105000=699))})
        self.assertEqual(by_contract(res["symbols"][0])["ACME261002C00105000"]["status"], "closed_or_daytrade")
        res = confirm.confirm_rows(evening_scan(), {"MID": morning(dict(UPDATED, ACME261002C00105000=700))})
        self.assertEqual(by_contract(res["symbols"][0])["ACME261002C00105000"]["status"], "partial")

    def test_expired_and_gone(self):
        # Read on 10/03: the 10/02 105C is past expiry although still listed; the 110C is gone.
        raw = morning(UPDATED, timestamp="2026-10-03 12:05:00", drop=("ACME261016C00110000",))
        c = by_contract(confirm.confirm_rows(evening_scan(), {"MID": raw})["symbols"][0])
        self.assertEqual(c["ACME261002C00105000"]["status"], "expired")
        self.assertEqual(c["ACME261016C00110000"]["status"], "expired")
        self.assertIsNone(c["ACME261016C00110000"]["oi_now"])
        self.assertEqual(c["ACME261016C00100000"]["status"], "partial")

    def test_all_expired(self):
        raw = morning(timestamp="2026-10-20 12:05:00")
        entry = confirm.confirm_rows(evening_scan(), {"MID": raw})["symbols"][0]
        self.assertEqual(entry["counts"]["expired"], 4)
        self.assertIn("all 4 flagged contracts have expired", entry["summary"])

    def test_oi_not_updated(self):
        # Re-read the same evening (or before Cboe posts the update): no OI has moved.
        res = confirm.confirm_rows(evening_scan(), {"MID": morning()})
        entry = res["symbols"][0]
        self.assertEqual(entry["status"], "oi_not_updated")
        self.assertEqual({c["status"] for c in entry["contracts"]}, {"oi_not_updated"})
        self.assertEqual(res["oi_not_updated"], ["MID"])
        self.assertEqual(res["counts"]["oi_not_updated"], 4)
        self.assertEqual(res["counts"]["closed_or_daytrade"], 0)   # not classified
        self.assertIn("has most likely not posted the overnight update", entry["summary"])
        md = confirm.to_markdown(res)
        self.assertIn("Open interest has not changed on any checked name", md)
        self.assertIn("| OI not updated |", md)

    def test_oi_not_updated_ignores_expired_contracts(self):
        # 10/03: the 10/02 105C has expired; the three live ones are unchanged -> not updated.
        raw = morning(timestamp="2026-10-03 12:05:00")
        entry = confirm.confirm_rows(evening_scan(), {"MID": raw})["symbols"][0]
        self.assertEqual(entry["status"], "oi_not_updated")
        self.assertEqual((entry["counts"]["expired"], entry["counts"]["oi_not_updated"]), (1, 3))

    def test_one_symbol_updated_one_not(self):
        scan_res = evening_scan(("MID", "LAG"))
        res = confirm.confirm_rows(scan_res, {"MID": morning(UPDATED), "LAG": morning()})
        status = {e["symbol"]: e["status"] for e in res["symbols"]}
        self.assertEqual(status, {"MID": "checked", "LAG": "oi_not_updated"})
        self.assertEqual(res["oi_not_updated"], ["LAG"])
        md = confirm.to_markdown(res)
        self.assertIn("Open interest not updated yet: LAG.", md)
        self.assertNotIn("not changed on any checked name", md)

    def test_one_moved_contract_means_updated(self):
        # Only the 100P moved: OI was updated, and the unchanged ones are real "no change" readings.
        res = confirm.confirm_rows(evening_scan(), {"MID": morning({"ACME261016P00100000": 260})})
        entry = res["symbols"][0]
        self.assertEqual(entry["status"], "checked")
        self.assertEqual(entry["counts"]["closed_or_daytrade"], 4)

    def test_unavailable_chains(self):
        scan_res = evening_scan(("MID", "DOWN", "EMPTY"))
        res = confirm.confirm_rows(scan_res, {"MID": morning(UPDATED), "EMPTY": {"data": {"options": []}}},
                                   errors={"DOWN": "could not reach cdn-api.cboe.com (blocked). If this..."})
        e = {x["symbol"]: x for x in res["symbols"]}
        self.assertEqual(e["DOWN"]["status"], "unavailable")
        self.assertEqual({c["status"] for c in e["DOWN"]["contracts"]}, {"unavailable"})
        self.assertIn("could not reach cdn-api.cboe.com", e["DOWN"]["summary"])
        self.assertEqual(e["EMPTY"]["error"], "today's chain has no contracts")
        self.assertEqual(set(res["errors"]), {"DOWN", "EMPTY"})
        self.assertEqual(res["counts"]["unavailable"], 8)
        json.dumps(res, allow_nan=False)

    def test_older_scan_file_falls_back_to_the_top_contract(self):
        scan_res = evening_scan()
        del scan_res["ranked"][0]["contracts"]
        entry = confirm.confirm_rows(scan_res, {"MID": morning(UPDATED)})["symbols"][0]
        self.assertEqual([c["contract"] for c in entry["contracts"]], ["ACME261002C00105000"])
        self.assertEqual(entry["contracts"][0]["status"], "confirmed_opened")

    def test_contract_without_scan_volume_is_not_classified(self):
        scan_res = evening_scan()
        for c in scan_res["ranked"][0]["contracts"]:
            c["volume"] = None
        entry = confirm.confirm_rows(scan_res, {"MID": morning(UPDATED)})["symbols"][0]
        self.assertEqual(entry["counts"]["unavailable"], 4)
        self.assertIn("no usable volume or open interest", entry["summary"])

    def test_no_contracts_and_top(self):
        scan_res = evening_scan(("MID", "BIG"))
        scan_res["ranked"][1]["contracts"] = []
        res = confirm.confirm_rows(scan_res, {"MID": morning(UPDATED)})
        self.assertEqual(res["symbols"][1]["status"], "no_contracts")
        only = confirm.confirm_rows(scan_res, {"MID": morning(UPDATED)}, top=1)
        self.assertEqual(len(only["symbols"]), 1)

    def test_same_day_expiry_counts_as_expired(self):
        # A scan file whose session was 2026-10-02 still lists the 10/02 105C (an older file, or
        # one made before same-day expiries were left out). It stopped trading at that close, so
        # even a check stamped the same date, or with no timestamp at all, calls it expired.
        scan_res = evening_scan()
        scan_res["ranked"][0]["session_date"] = "2026-10-02"
        for raw in (morning(UPDATED, timestamp="2026-10-02 20:30:00"), morning(UPDATED)):
            if raw["timestamp"].startswith("2026-10-01"):
                raw.pop("timestamp")          # falls back to the scan's session date, 10/02
            c = by_contract(confirm.confirm_rows(scan_res, {"MID": raw})["symbols"][0])
            self.assertEqual(c["ACME261002C00105000"]["status"], "expired")
            self.assertEqual(c["ACME261016C00110000"]["status"], "confirmed_opened")

    def test_today_may_be_a_datetime(self):
        raw = morning(UPDATED)
        raw.pop("timestamp")
        res = confirm.confirm_rows(evening_scan(), {"MID": raw}, today=datetime(2026, 10, 3, 8, 5))
        c = by_contract(res["symbols"][0])
        self.assertEqual(c["ACME261002C00105000"]["status"], "expired")      # 10/02 < 10/03
        self.assertEqual(c["ACME261016C00100000"]["status"], "partial")

    def test_damaged_contracts_field_is_isolated(self):
        scan_res = evening_scan(("MID", "BIG"))
        next(r for r in scan_res["ranked"] if r["symbol"] == "MID")["contracts"] = 5
        res = confirm.confirm(scan_res, fetch=lambda sym: morning(UPDATED))
        e = {x["symbol"]: x for x in res["symbols"]}
        self.assertEqual(e["MID"]["status"], "no_contracts")
        self.assertEqual(e["BIG"]["status"], "checked")

    def test_non_finite_open_interest_is_not_checked(self):
        # A chain with "Infinity" open interest on one contract: that one is not checked, the
        # rest are, and the result stays strict JSON.
        raw = morning(UPDATED)
        for o in raw["data"]["options"]:
            if o["option"] == "ACME261002C00105000":
                o["open_interest"] = "Infinity"
        res = confirm.confirm_rows(evening_scan(), {"MID": raw})
        entry = res["symbols"][0]
        c = by_contract(entry)
        self.assertEqual((c["ACME261002C00105000"]["status"], c["ACME261002C00105000"]["oi_now"]),
                         ("unavailable", None))
        self.assertEqual(entry["counts"]["confirmed_opened"], 1)            # the 110C
        self.assertIn("1 not checked (no usable volume or open interest)", entry["summary"])
        json.dumps(res, allow_nan=False)

    def test_rejects_other_files(self):
        for bad in ({"kind": "gex", "ranked": []}, {"kind": "flow"}, [], "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                confirm.confirm_rows(bad, {})

    def test_pure_no_network_or_clock(self):
        class NoClock(date):
            @classmethod
            def today(cls):
                raise AssertionError("confirm_rows read the clock")

        def no_network(*a, **k):
            raise AssertionError("confirm_rows touched the network")

        scan_res = evening_scan()
        before = copy.deepcopy(scan_res)
        raw = morning(UPDATED)
        raw.pop("timestamp")   # no chain timestamp: falls back to the scan's session date
        with mock.patch("opptions.cboe.date", NoClock), mock.patch("opptions.confirm.date", NoClock), \
                mock.patch("urllib.request.urlopen", no_network):
            res = confirm.confirm_rows(scan_res, {"MID": raw})
        self.assertEqual(res["symbols"][0]["counts"]["confirmed_opened"], 2)
        self.assertEqual(scan_res, before)

    def test_markdown(self):
        md = confirm.to_markdown(confirm.confirm_rows(evening_scan(), {"MID": morning(UPDATED)}))
        self.assertTrue(md.startswith("## Flow confirmation (overnight open interest)\n"))
        self.assertIn("1 name, 4 flagged contracts: 2 opened, 1 partly opened, 1 closed or day-traded.", md)
        self.assertIn("### MID (scan #1, score 52.6, lean bullish)", md)
        self.assertIn("| Contract | Type | Strike | Expiry | Scan volume | OI at scan | OI now | OI change | Result |", md)
        self.assertIn("| ACME261002C00105000 | call | 105 | 2026-10-02 | 2,000 | 500 | 2,300 | "
                      "+1,800 (90% of volume) | opened |", md)
        self.assertIn("| -30 (-10% of volume) | closed or day trade |", md)
        self.assertEqual(sum(ln.startswith("MID: ") for ln in md.splitlines()), 1)
        self.assertIn("at least half of the scan's volume", md)
        self.assertIn("_Caveat:", md)
        self.assertIsNone(TRADE_WORDS.search(md))

    def test_markdown_with_nothing_ranked(self):
        md = confirm.to_markdown(confirm.confirm_rows({"kind": "flow", "ranked": [], "as_of": None}, {}))
        self.assertIn("nothing to confirm", md)


class ConfirmFetchTest(unittest.TestCase):
    def test_fetches_each_name_once_and_records_errors(self):
        calls = []

        def fetch(sym):
            calls.append(sym)
            if sym == "DOWN":
                raise FetchError("could not reach cdn-api.cboe.com (blocked)")
            return morning(UPDATED)

        scan_res = evening_scan(("MID", "DOWN"))
        res = confirm.confirm(scan_res, fetch=fetch)
        self.assertEqual(sorted(calls), ["DOWN", "MID"])
        self.assertEqual(res["errors"], {"DOWN": "could not reach cdn-api.cboe.com (blocked)"})
        e = {x["symbol"]: x for x in res["symbols"]}
        self.assertEqual(e["MID"]["counts"]["confirmed_opened"], 2)
        self.assertEqual(e["DOWN"]["status"], "unavailable")

    def test_default_fetch_is_the_uncached_scan_fetch(self):
        with mock.patch.object(scan, "fetch_json", return_value=morning(UPDATED)) as fj:
            res = confirm.confirm(evening_scan(("SPX",)))
        fj.assert_called_once_with(scan.cboe.chain_url("SPX"))
        self.assertEqual(res["symbols"][0]["status"], "checked")


class ConfirmCliTest(unittest.TestCase):
    def run_cli(self, argv, fetch):
        with mock.patch.object(confirm, "fetch_raw", side_effect=fetch), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            status = cli.main(argv)
        return status, out.getvalue(), err.getvalue()

    def saved(self, d, data):
        path = os.path.join(d, "scan-flow.json")
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_evening_scan_json_then_morning_confirm(self):
        with tempfile.TemporaryDirectory() as d:
            # 4:20 PM: `scan flow --json`, saved as scan-flow.json.
            with mock.patch.object(scan, "fetch_raw", side_effect=lambda sym: FLOW), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["scan", "flow", "MID", "--json"]), 0)
            path = self.saved(d, out.getvalue())
            # Next morning.
            status, md, err = self.run_cli(["confirm", path], lambda sym: morning(UPDATED))
            self.assertEqual((status, err), (0, ""))
            self.assertTrue(md.startswith("## Flow confirmation (overnight open interest)"))
            self.assertIn("| opened |", md)
            status, js, _ = self.run_cli(["confirm", path, "--json", "--top", "1"], lambda sym: morning(UPDATED))
        self.assertEqual(status, 0)
        res = json.loads(js)
        self.assertEqual(res["counts"]["confirmed_opened"], 2)

    def test_all_fetches_fail(self):
        def down(sym):
            raise FetchError("could not reach cdn-api.cboe.com (blocked)")

        with tempfile.TemporaryDirectory() as d:
            status, out, err = self.run_cli(["confirm", self.saved(d, evening_scan(("MID", "BIG")))], down)
        self.assertEqual(status, 1)
        self.assertIn("2 of 2 chains could not be fetched", err)
        self.assertIn("could not reach cdn-api.cboe.com", err)
        self.assertIn("nothing was confirmed", out)

    def test_bad_files(self):
        with tempfile.TemporaryDirectory() as d:
            cases = {"missing": os.path.join(d, "nope.json"),
                     "not json": self.saved(d, "not json"),
                     "gex scan": self.saved(d, {"kind": "gex", "ranked": []})}
            for label, path in cases.items():
                with self.subTest(label):
                    status, out, err = self.run_cli(["confirm", path], lambda sym: FLOW)
                    self.assertEqual((status, out), (1, ""))
                    self.assertIn("[confirm] could not use", err)

    def test_usage_errors_exit_2(self):
        for argv in (["confirm"], ["confirm", "f.json", "--top", "0"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.main(argv)
            self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

"""Integration checks across trackers and the shared modules. No network.

Covers the module contract every tracker follows, the shared HTTP/Cboe/SEC
helpers, the packet builder, the CLI's failure path, and that
examples/sample-packet.md matches what examples/build_sample.py produces.
"""

import contextlib
import http.client
import importlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timezone
from unittest import mock

from opptions import TRACKERS, cboe, packet, sec
from opptions import __main__ as cli
from opptions.http import FetchError, fetch_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_build_sample():
    path = os.path.join(ROOT, "examples", "build_sample.py")
    spec = importlib.util.spec_from_file_location("build_sample", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_sample = load_build_sample()


class ContractTest(unittest.TestCase):
    """fetch/analyze/to_markdown exist; analyze gives strict JSON; markdown starts with '## '."""

    def test_every_tracker_on_its_fixture_and_on_empty_input(self):
        raws = build_sample.raw_inputs()
        for name in TRACKERS:
            mod = importlib.import_module(f"opptions.{name}")
            for fn in ("fetch", "analyze", "to_markdown"):
                self.assertTrue(callable(getattr(mod, fn, None)), f"{name}.{fn}")
            for label, raw in (("fixture", raws[name]), ("empty", {})):
                with self.subTest(tracker=name, input=label):
                    result = mod.analyze(raw)
                    json.dumps(result, allow_nan=False)
                    self.assertTrue(mod.to_markdown(result).startswith("## "))


class HttpTest(unittest.TestCase):
    def test_broken_response_becomes_fetch_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaises(FetchError) as cm:
                fetch_text("https://cdn-api.cboe.com/x.json")
        self.assertIn("could not reach cdn-api.cboe.com", str(cm.exception))

    def test_http_status_names_host(self):
        err = urllib.error.HTTPError("https://api.nasdaq.com/x", 503, "busy", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(FetchError) as cm:
                fetch_text("https://api.nasdaq.com/x")
        self.assertIn("api.nasdaq.com answered HTTP 503", str(cm.exception))


class SharedHelpersTest(unittest.TestCase):
    def test_normalize_chain_today_avoids_the_clock(self):
        raw = {"data": {"symbol": "DEMO", "current_price": 100,
                        "options": [{"option": "DEMO261009C00100000", "bid": 1, "ask": 1.2}]}}
        class NoClockDate(date):
            @classmethod
            def today(cls):
                raise AssertionError("clock read")

        with mock.patch.object(cboe, "date", NoClockDate):
            chain = cboe.normalize_chain(raw, today=date(2026, 10, 1))
            with self.assertRaises(AssertionError):  # the patch does bite without `today`
                cboe.normalize_chain(raw)
        self.assertEqual(chain["options"][0]["dte"], 8)

    def test_chain_timestamp_wins_over_today(self):
        raw = {"timestamp": "2026-10-05 10:00:00",
               "data": {"options": [{"option": "DEMO261009C00100000"}]}}
        self.assertEqual(cboe.normalize_chain(raw, today=date(2026, 10, 1))["options"][0]["dte"], 4)

    def test_recent_filings_keeps_acceptance_time_and_new_13d_name(self):
        subs = {"cik": "9999999", "filings": {"recent": {
            "form": ["SCHEDULE 13D"], "filingDate": ["2026-09-30"],
            "acceptanceDateTime": ["2026-09-30T16:05:12.000Z"],
            "accessionNumber": ["0000950103-26-000001"], "primaryDocument": ["x.xml"], "items": [""]}}}
        row = sec.recent_filings(subs)[0]
        self.assertEqual(row["accepted"], "2026-09-30T16:05:12.000Z")
        self.assertEqual(row["description"], "Activist or >5% holder stake")


class PacketTest(unittest.TestCase):
    def test_eastern_date_follows_dst(self):
        cases = {
            "2026-10-02T03:30:00": "2026-10-01",   # 11:30 PM EDT
            "2026-10-02T04:00:00": "2026-10-02",
            "2026-01-15T04:59:00": "2026-01-14",   # 11:59 PM EST
            "2026-01-15T05:00:00": "2026-01-15",
        }
        for utc, expected in cases.items():
            now = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
            self.assertEqual(packet.eastern_date(now), expected, utc)

    def _patched(self, failing=()):
        """Patch every tracker's fetch to return its fixture, or raise for names in failing."""
        raws = build_sample.raw_inputs()
        stack = contextlib.ExitStack()
        for name in TRACKERS:
            mod = importlib.import_module(f"opptions.{name}")
            if name in failing:
                effect = FetchError(f"could not reach {name}.example.com (blocked)")
                stack.enter_context(mock.patch.object(mod, "fetch", side_effect=effect))
            else:
                stack.enter_context(mock.patch.object(mod, "fetch", return_value=raws[name]))
        return stack

    def test_build_keeps_going_when_one_tracker_fails(self):
        with self._patched(failing=("flow",)):
            md, results = packet.build("demo")
        self.assertTrue(md.startswith("# Research packet: DEMO"))
        self.assertIn("## Gamma Exposure (GEX): DEMO", md)
        self.assertIn("## FLOW\n\n_Not available: could not reach flow.example.com", md)
        self.assertIn("## News and Filings: DEMO", md)
        self.assertIn("## Earnings: DEMO", md)
        self.assertEqual(results["flow"], {"error": "could not reach flow.example.com (blocked)"})

    def test_main_writes_eastern_dated_file_and_flags_errors(self):
        with tempfile.TemporaryDirectory() as out, self._patched(failing=("gex",)), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            status = packet.main(["DEMO"], out_dir=out)
            path = printed.getvalue().strip()
            self.assertEqual(status, 1)
            self.assertRegex(os.path.basename(path), r"^DEMO-\d{4}-\d{2}-\d{2}\.md$")
            with open(path, encoding="utf-8") as f:
                self.assertIn("## Options Activity", f.read())


class CliTest(unittest.TestCase):
    def test_fetch_error_prints_host_and_exits_1(self):
        err = FetchError("could not reach cdn-api.cboe.com (Tunnel connection failed: 403 Forbidden)")
        with mock.patch.object(cboe, "fetch_chain", side_effect=err), \
                contextlib.redirect_stderr(io.StringIO()) as stderr, \
                contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(["gex", "spy"])
        self.assertEqual(status, 1)
        self.assertIn("[gex SPY] could not reach cdn-api.cboe.com", stderr.getvalue())


class SamplePacketTest(unittest.TestCase):
    def test_sample_packet_is_up_to_date(self):
        with open(build_sample.OUT, encoding="utf-8") as f:
            current = f.read()
        self.assertEqual(build_sample.build(), current,
                         "examples/sample-packet.md is stale: run python3 examples/build_sample.py")

    def test_sample_is_labeled_synthetic_and_relabeled(self):
        md = build_sample.build()
        self.assertIn("Synthetic sample, not market data", md.split("## ", 1)[0])
        for leftover in ("ACME", "Acme", "XYZ"):
            self.assertNotIn(leftover, md)


if __name__ == "__main__":
    unittest.main()

"""Tests for opptions.news. No network: everything runs on hand-built fixtures.

Fixtures (synthetic company ACME Robotics, fetched_at 2026-10-01 05:50 UTC, so the
default 7-day window starts 2026-09-24 05:50 UTC):

  news_yahoo.xml   7 items: 5 dated in window, 1 old (2026-09-20), 1 undated,
                   one with an ISO 8601 pubDate.
  news_google.xml  11 items with " - Publisher" titles and <source>: 1 old
                   (2026-09-22), 2 cross-feed duplicates of Yahoo stories and one
                   story carried twice (Barron's, then MSN).
  news_sec_submissions.json  10 filings in EDGAR's column layout; 7 filed on or
                   after 2026-09-24 (424B5, 8-K 2.02, 2x Form 4, 144, 8-K 5.02, SC 13D).
"""

import json
import os
import unittest
from unittest import mock

from opptions import news
from opptions.http import FetchError

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FETCHED_AT = "2026-10-01T05:50:00+00:00"


def read(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def make_raw(yahoo=True, google=True, sec=True, errors=None, fetched_at=FETCHED_AT):
    return {
        "symbol": "ACME",
        "fetched_at": fetched_at,
        "yahoo_rss": read("news_yahoo.xml") if yahoo else None,
        "google_rss": read("news_google.xml") if google else None,
        "sec_submissions": json.loads(read("news_sec_submissions.json")) if sec else None,
        "errors": dict(errors or {}),
    }


def rss(items):
    """Small RSS 2.0 document from (title, pubDate) pairs."""
    body = "".join(f"<item><title>{t}</title><link>https://example.com/{i}</link>"
                   f"<pubDate>{d}</pubDate></item>" for i, (t, d) in enumerate(items))
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>{body}</channel></rss>'


class FullAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPPTIONS_NEWS_DAYS", None)
            cls.res = news.analyze(make_raw())
        cls.by_title = {i["title"]: i for i in cls.res["items"]}

    def test_counts(self):
        c = self.res["counts"]
        self.assertEqual(c["parsed"], {"yahoo": 7, "google": 11})
        self.assertEqual(c["undated_dropped"], 1)
        self.assertEqual(c["duplicates_merged"], 3)
        self.assertEqual(c["outside_window"], 2)
        self.assertEqual(c["in_window"], 12)
        self.assertEqual(c["shown"], 12)
        self.assertEqual(self.res["impact_counts"], {"high": 5, "medium": 3, "low": 4})
        self.assertEqual(self.res["window_days"], 7)
        self.assertEqual(self.res["window_start"], "2026-09-24T05:50:00+00:00")
        self.assertEqual(self.res["sources_ok"], ["yahoo", "google", "sec"])

    def test_google_publisher_split(self):
        item = self.by_title["FTC opens antitrust probe into ACME Robotics warehouse deal"]
        self.assertEqual(item["source"], "The Wall Street Journal")
        self.assertEqual(item["published"], "2026-09-24T18:00:00+00:00")
        self.assertEqual(item["url"], "https://news.google.com/rss/articles/CBMiAAA8?oc=5")
        self.assertFalse(any(i["title"].endswith(" - MarketBeat") for i in self.res["items"]))

    def test_yahoo_dates_rfc822_and_iso(self):
        self.assertEqual(self.by_title["ACME Robotics CFO to step down at year end"]["published"],
                         "2026-09-25T16:45:00+00:00")
        iso = self.by_title["Why ACME Robotics stock is a long-term hold"]
        self.assertEqual(iso["published"], "2026-09-28T10:00:00+00:00")
        self.assertEqual(iso["source"], "Yahoo Finance")

    def test_dedupe_keeps_earliest_and_lists_others(self):
        robots = [i for i in self.res["items"] if "warehouse robot line" in i["title"].lower()]
        self.assertEqual(len(robots), 1)
        self.assertEqual(robots[0]["source"], "Business Wire")          # 13:05 beats Yahoo's 14:00
        self.assertEqual(robots[0]["also_reported_by"], ["Yahoo Finance"])

        earnings = [i for i in self.res["items"] if "q3 earnings" in i["title"].lower()]
        self.assertEqual(len(earnings), 1)                              # ';' vs ':' and case differ
        self.assertEqual(earnings[0]["source"], "Yahoo Finance")
        self.assertEqual(earnings[0]["also_reported_by"], ["Investing.com"])

        upgraded = [i for i in self.res["items"] if i["title"].startswith("ACME Robotics upgraded")]
        self.assertEqual(len(upgraded), 1)
        self.assertEqual((upgraded[0]["source"], upgraded[0]["also_reported_by"]), ("Barron's", ["MSN"]))

    def test_window_and_order(self):
        titles = [i["title"] for i in self.res["items"]]
        self.assertNotIn("ACME Robotics to present at investor conference", titles)
        self.assertNotIn("ACME Robotics stock hits 52-week high", titles)
        self.assertNotIn("ACME Robotics stock: what to watch", titles)   # undated
        published = [i["published"] for i in self.res["items"]]
        self.assertEqual(published, sorted(published, reverse=True))
        self.assertTrue(all(p >= self.res["window_start"] for p in published))
        self.assertTrue(titles[0].startswith("ACME Robotics prices $250 million offering"))
        self.assertTrue(titles[-1].startswith("FTC opens antitrust probe"))

    def test_tags_and_impact_on_fixture(self):
        expect = {
            "ACME Robotics prices $250 million offering of convertible senior notes": (["offering"], "high"),
            "ACME Robotics Q3 earnings beat estimates; company raises full-year outlook":
                (["earnings", "guidance"], "high"),
            "Morgan Stanley upgrades ACME Robotics to Overweight, lifts price target to $85": (["analyst"], "high"),
            "FTC opens antitrust probe into ACME Robotics warehouse deal": (["legal/regulatory"], "high"),
            "ACME Robotics CEO Jane Doe sells 50,000 shares under 10b5-1 plan": (["management", "insider"], "medium"),
            "ACME Robotics unveils new warehouse robot line": (["product"], "medium"),
            "ACME Robotics CFO to step down at year end": (["management"], "medium"),
            "Stocks slip as Fed signals fewer rate cuts; tariff worries weigh": (["macro"], "low"),
            "Why ACME Robotics stock is a long-term hold": ([], "low"),
            "Vanguard Group Inc. Acquires 12,345 Shares of ACME Robotics Inc. (NASDAQ:ACME)":
                (["fund filing"], "low"),
            "Shareholder Alert: Smith & Jones Law Firm investigates claims on behalf of ACME Robotics investors":
                (["legal/regulatory", "law-firm ad"], "low"),
        }
        for title, (tags, impact) in expect.items():
            with self.subTest(title=title):
                self.assertEqual(self.by_title[title]["tags"], tags)
                self.assertEqual(self.by_title[title]["impact"], impact)
        self.assertEqual(self.by_title["Morgan Stanley upgrades ACME Robotics to Overweight, lifts price "
                                       "target to $85"]["impact_reason"], "upgrade/downgrade")

    def test_json_serializable_and_deterministic(self):
        text = json.dumps(self.res)
        self.assertEqual(json.loads(text), self.res)
        self.assertEqual(news.analyze(make_raw(), days=7), self.res)


class SecTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sec = news.analyze(make_raw(), days=7)["sec"]

    def test_window_and_counts(self):
        s = self.sec
        self.assertTrue(s["available"])
        self.assertEqual((s["company"], s["cik"], s["since"]), ("ACME Robotics Inc.", 1999999, "2026-09-24"))
        self.assertEqual(s["filings_in_window"], 7)                # S-3ASR (09-15), Form 4 (09-20), 10-Q out
        self.assertEqual(s["form_counts"], {"4": 2, "8-K": 2, "144": 1, "424B5": 1, "SC 13D": 1})
        self.assertEqual((s["form4_count"], s["form144_count"]), (2, 1))
        self.assertEqual((s["eight_k_count"], s["dilution_count"], s["activist_count"]), (2, 1, 1))
        self.assertEqual(s["eight_k_items"], ["2.02", "5.02", "9.01"])

    def test_flags(self):
        self.assertEqual(self.sec["flags"], [
            "8-K filed 2026-09-30: Earnings results",
            "8-K filed 2026-09-25: Executive or director change",
            "Possible dilution: 424B5 x1 (filed 2026-09-30)",
            "Activist or >5% stake: SC 13D filed 2026-09-24",
            "2 Form 4 insider transaction filings",
            "1 Form 144 notice of planned insider sales",
        ])

    def test_table_rows_skip_insider_forms(self):
        rows = self.sec["filings"]
        self.assertEqual([r["form"] for r in rows], ["424B5", "8-K", "8-K", "SC 13D"])
        self.assertEqual(rows[0]["impact"], "high")
        self.assertEqual(rows[0]["url"], "https://www.sec.gov/Archives/edgar/data/1999999/"
                                         "000119312526210001/d123456d424b5.htm")
        self.assertEqual(rows[1]["flags"], ["item 2.02: Earnings results"])

    def test_classify_filing_forms(self):
        def row(form, items=()):
            items = list(items)
            return {"form": form, "filed": "2026-09-30", "items": items,
                    "item_meanings": [news.sec.EIGHT_K_ITEMS.get(i, i) for i in items],
                    "description": news.sec.NOTABLE_FORMS.get(form, ""), "url": ""}
        self.assertEqual(news.classify_filing(row("S-3"))["flags"], ["possible dilution"])
        self.assertEqual(news.classify_filing(row("424B2"))["impact"], "medium")
        self.assertEqual(news.classify_filing(row("424B2"))["flags"], [])
        self.assertEqual(news.classify_filing(row("424B3"))["flags"], ["possible dilution"])
        self.assertEqual(news.classify_filing(row("SCHEDULE 13D"))["impact"], "high")
        self.assertIn("possible dilution (unregistered share sale)",
                      news.classify_filing(row("8-K", ["3.02"]))["flags"])
        self.assertEqual(news.classify_filing(row("8-K", ["7.01", "9.01"]))["impact"], "medium")
        self.assertEqual(news.classify_filing(row("4"))["impact"], "low")
        self.assertEqual(news.classify_filing(row("S-8"))["flags"], [])


class ClassifyTest(unittest.TestCase):
    def check(self, title, tags, impact):
        got = news.classify_headline(title)
        with self.subTest(title=title):
            for t in tags:
                self.assertIn(t, got["tags"])
            self.assertEqual(got["impact"], impact)

    def test_high(self):
        self.check("Goldman downgrades Tesla to Sell", ["analyst"], "high")
        self.check("Microsoft to acquire gaming studio in $2B deal", ["m&a"], "high")
        self.check("Moderna announces Phase 3 trial results for flu vaccine", ["fda"], "high")
        self.check("FDA approves Vertex drug for sickle cell", ["fda"], "high")
        self.check("Tesla recalls 2 million vehicles over Autopilot", ["legal/regulatory"], "high")
        self.check("GameStop announces $1 billion at-the-market program", ["offering"], "high")
        self.check("Netflix sees Q4 revenue above estimates", ["earnings", "guidance"], "high")
        self.check("DOJ subpoenas chipmaker in antitrust case", ["legal/regulatory"], "high")

    def test_medium(self):
        self.check("Nvidia price target raised to $250 at Citi", ["analyst"], "medium")
        self.check("Jefferies initiates coverage of Rivian with Hold rating", ["analyst"], "medium")
        self.check("Intel CEO resigns effective immediately", ["management"], "medium")
        self.check("AMD launches new AI chip to rival Nvidia", ["product"], "medium")

    def test_low_and_noise(self):
        self.check("Fed holds rates steady; Powell signals patience", ["macro"], "low")
        self.check("NVIDIA Director Sells $5M in Stock", ["insider"], "low")
        self.check("Insider buys shares after Form 4 shows purchase", ["insider"], "low")
        self.check("XYZ Capital LLC Sells 2,345 Shares of NVIDIA Co.", ["fund filing"], "low")
        self.check("The Rosen Law Firm reminds investors of lead plaintiff deadline",
                   ["law-firm ad"], "low")

    def test_word_boundaries(self):
        # "upgrade cycle" is product talk, "secures"/"fedex"/"rates" substrings must not match.
        self.assertEqual(news.classify_headline("Apple's iPhone upgrade cycle could lift sales")["tags"], [])
        self.assertEqual(news.classify_headline("FedEx secures warehouse space")["tags"], [])
        self.assertNotIn("m&a", news.classify_headline("Vanguard Acquires 1,000 Shares of Apple")["tags"])
        self.assertNotIn("fund filing", news.classify_headline("CEO Sells 75,000 Shares of Stock")["tags"])


class ParsingTest(unittest.TestCase):
    def test_normalize_title(self):
        self.assertEqual(news.normalize_title("ACME’s Q3: Beat! - Reuters", "Reuters"), "acme s q3 beat")
        self.assertEqual(news.normalize_title("Acme's  q3 -- beat"), "acme s q3 beat")

    def test_split_publisher(self):
        self.assertEqual(news.split_publisher("A - B - Reuters", "Reuters"), ("A - B", "Reuters"))
        self.assertEqual(news.split_publisher("Headline - Yahoo Finance"), ("Headline", "Yahoo Finance"))
        self.assertEqual(news.split_publisher("No suffix here"), ("No suffix here", ""))

    def test_parse_date_variants(self):
        self.assertEqual(news.parse_date("Sun, 13 Jan 2019 21:57:48 GMT").isoformat(), "2019-01-13T21:57:48+00:00")
        self.assertEqual(news.parse_date("Tue, 30 Sep 2026 16:15:00 -0400").isoformat(), "2026-09-30T20:15:00+00:00")
        self.assertEqual(news.parse_date("2025-12-29T21:12:30Z").isoformat(), "2025-12-29T21:12:30+00:00")
        self.assertIsNone(news.parse_date("yesterday"))
        self.assertIsNone(news.parse_date(""))

    def test_bad_feed_becomes_error_not_crash(self):
        raw = make_raw()
        raw["yahoo_rss"] = "<html><body>Will be right back</body>"
        res = news.analyze(raw, days=7)
        self.assertIn("yahoo", res["errors"])
        self.assertEqual(res["sources_ok"], ["google", "sec"])
        self.assertEqual(res["counts"]["parsed"], {"google": 11})
        self.assertIn("Missing source: Yahoo Finance RSS", news.to_markdown(res))

    def test_window_days_param_and_env(self):
        self.assertEqual(news.analyze(make_raw(), days=2)["counts"]["in_window"], 6)
        with mock.patch.dict(os.environ, {"OPPTIONS_NEWS_DAYS": "2"}):
            res = news.analyze(make_raw())
        self.assertEqual((res["window_days"], res["counts"]["in_window"]), (2, 6))
        self.assertEqual(res["sec"]["since"], "2026-09-29")
        with mock.patch.dict(os.environ, {"OPPTIONS_NEWS_DAYS": "junk"}):
            self.assertEqual(news.analyze(make_raw())["window_days"], 7)

    def test_cap_at_40(self):
        items = [(f"Story number {n}", f"Tue, 30 Sep 2026 {n % 24:02d}:{n % 60:02d}:00 +0000") for n in range(55)]
        raw = {"symbol": "ACME", "fetched_at": FETCHED_AT, "yahoo_rss": rss(items),
               "google_rss": None, "sec_submissions": None, "errors": {}}
        res = news.analyze(raw, days=7)
        self.assertEqual(res["counts"]["in_window"], 55)
        self.assertEqual(len(res["items"]), news.MAX_ITEMS)
        self.assertIn("Showing 40 of 55:", news.to_markdown(res))
        # all low impact, so the 40 kept are simply the newest 40
        self.assertEqual(res["items"][-1]["published"], "2026-09-30T05:05:00+00:00")

    def test_missing_fetched_at_uses_newest_item(self):
        res = news.analyze(make_raw(fetched_at=None), days=7)
        self.assertEqual(res["window_start"], "2026-09-24T01:10:00+00:00")
        self.assertTrue(res["notes"])


class FetchTest(unittest.TestCase):
    """fetch() with every network call mocked."""

    def patches(self, yahoo=None, google=None, cik=1999999, subs=None, sec_error=None):
        def fake_text(url, headers=None):
            value = yahoo if "yahoo" in url else google
            if isinstance(value, Exception):
                raise value
            return value

        def fake_cik(symbol):
            if sec_error:
                raise sec_error
            return cik

        return [mock.patch("opptions.news.fetch_text", side_effect=fake_text),
                mock.patch("opptions.sec.ticker_to_cik", side_effect=fake_cik),
                mock.patch("opptions.sec.fetch_submissions", return_value=subs)]

    def run_fetch(self, **kw):
        ps = self.patches(**kw)
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])
        return news.fetch("acme")

    def test_all_sources_ok(self):
        subs = json.loads(read("news_sec_submissions.json"))
        raw = self.run_fetch(yahoo=read("news_yahoo.xml"), google=read("news_google.xml"), subs=subs)
        self.assertEqual(raw["symbol"], "ACME")
        self.assertEqual(raw["errors"], {})
        self.assertIs(raw["sec_submissions"], subs)
        self.assertRegex(raw["fetched_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")

    def test_partial_failure_is_recorded(self):
        raw = self.run_fetch(yahoo=FetchError("could not reach feeds.finance.yahoo.com"),
                             google=read("news_google.xml"),
                             sec_error=FetchError("www.sec.gov answered HTTP 403"))
        self.assertIsNone(raw["yahoo_rss"])
        self.assertIsNone(raw["sec_submissions"])
        self.assertEqual(set(raw["errors"]), {"yahoo", "sec"})
        res = news.analyze(dict(raw, fetched_at=FETCHED_AT), days=7)
        self.assertEqual(res["sources_ok"], ["google"])
        self.assertFalse(res["sec"]["available"])
        md = news.to_markdown(res)
        self.assertIn("Missing source: Yahoo Finance RSS (could not reach feeds.finance.yahoo.com)", md)
        self.assertIn("_Not available: www.sec.gov answered HTTP 403._", md)

    def test_unknown_ticker_at_sec_is_an_error_not_a_crash(self):
        raw = self.run_fetch(yahoo=read("news_yahoo.xml"), google=read("news_google.xml"), cik=None)
        self.assertIn("no SEC CIK", raw["errors"]["sec"])

    def test_all_sources_failed_raises(self):
        with self.assertRaises(FetchError) as ctx:
            self.run_fetch(yahoo=FetchError("yahoo down"), google=FetchError("google down"),
                           sec_error=FetchError("sec down"))
        self.assertIn("yahoo down", str(ctx.exception))
        self.assertIn("sec down", str(ctx.exception))

    def test_feeds_down_and_no_cik_raises(self):
        with self.assertRaises(FetchError):
            self.run_fetch(yahoo=FetchError("y"), google=FetchError("g"), cik=None)

    def test_urls(self):
        self.assertEqual(news.yahoo_url("brk.b"),
                         "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BRK.B&region=US&lang=en-US")
        self.assertEqual(news.google_url("nvda"),
                         "https://news.google.com/rss/search?q=NVDA+stock&hl=en-US&gl=US&ceid=US:en")

    def test_analyze_makes_no_network_calls(self):
        boom = mock.Mock(side_effect=AssertionError("network used in analyze"))
        with mock.patch("opptions.news.fetch_text", boom), \
                mock.patch("opptions.sec.fetch_json", boom):
            news.analyze(make_raw(), days=7)
        boom.assert_not_called()


class MarkdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = news.to_markdown(news.analyze(make_raw(), days=7))

    def test_structure_and_order(self):
        md = self.md
        self.assertTrue(md.startswith("## News and Filings: ACME\n"))
        self.assertIn("**12 headlines in the last 7 days**", md)
        self.assertIn("5 high, 3 medium, 4 low impact", md)
        order = [md.index(h) for h in ("### High impact (5)", "### Headlines (newest first)",
                                       "### SEC filings (EDGAR)", "### Caveats")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(md.rstrip().endswith("Next step: send this to the Explainer agent for a plain-English read."))

    def test_table_and_links(self):
        self.assertIn("| When (ET) | Source | Impact | Tags | Headline |", self.md)
        # 18:00 UTC on 24 Sep is 14:00 EDT (UTC-4), inside 9:30-16:00.
        self.assertIn("| 2026-09-24 14:00 (market hours) | The Wall Street Journal | high | legal/regulatory | "
                      "[FTC opens antitrust probe into ACME Robotics warehouse deal]"
                      "(https://news.google.com/rss/articles/CBMiAAA8?oc=5) |", self.md)
        self.assertIn("(also: Investing.com)", self.md)
        self.assertIn("| 2026-09-30 | 424B5 | Prospectus for an offering (possible dilution) |", self.md)

    def test_plain_english_lines(self):
        self.assertIn("**offering** (1): A share or convertible offering adds supply", self.md)
        self.assertIn("A 13D means a holder above 5%", self.md)
        self.assertIn("tends to", self.md)
        for word in ("you should buy", "you should sell", "will rise", "will fall"):
            self.assertNotIn(word, self.md.lower())

    def test_pipe_in_title_is_escaped(self):
        raw = {"symbol": "ACME", "fetched_at": FETCHED_AT, "google_rss": None, "sec_submissions": None,
               "yahoo_rss": rss([("ACME | [Update] earnings", "Tue, 30 Sep 2026 12:00:00 +0000")]),
               "errors": {}}
        md = news.to_markdown(news.analyze(raw, days=7))
        self.assertIn("[ACME \\| (Update) earnings](https://example.com/0)", md)

    def test_empty_result(self):
        raw = {"symbol": "ACME", "fetched_at": FETCHED_AT, "yahoo_rss": rss([]), "google_rss": None,
               "sec_submissions": None, "errors": {"google": "down", "sec": "down"}}
        md = news.to_markdown(news.analyze(raw, days=7))
        self.assertIn("**0 headlines in the last 7 days**", md)
        self.assertIn("No headlines in this window.", md)
        self.assertIn("### High impact (0)", md)
        self.assertTrue(md.rstrip().endswith(news.NEXT_STEP))


def submissions(rows, files=()):
    """EDGAR submissions JSON (column layout) from (form, filingDate, items) rows."""
    n = len(rows)
    return {"cik": "1999999", "name": "ACME Robotics Inc.",
            "filings": {"recent": {
                "accessionNumber": [f"0001999999-26-{i:06d}" for i in range(n)],
                "filingDate": [r[1] for r in rows],
                "form": [r[0] for r in rows],
                "items": [r[2] for r in rows],
                "primaryDocument": [f"doc{i}.htm" for i in range(n)]},
                "files": list(files)}}


class NoiseAndFalseMatchTest(unittest.TestCase):
    """Headline shapes seen on Google News that the first version scored wrongly."""

    def tags(self, title):
        return news.classify_headline(title)

    def test_marketbeat_13f_passive_and_cashtag_forms_are_low(self):
        # MarketBeat's current titles use a cashtag and the passive "Shares Acquired by";
        # "Acquired" used to trigger m&a and mark these high.
        for title in ("NVIDIA Corporation $NVDA Shares Acquired by Milestone Asset Management Group LLC",
                      "16,726 NVIDIA Corporation $NVDA Shares Acquired by Argent Trust Co",
                      "NVIDIA Co. (NASDAQ:NVDA) Shares Sold by XYZ Advisors LLC",
                      "Argent Trust Co Buys 16,726 Shares of NVIDIA Corporation $NVDA",
                      "XYZ LLC Makes New $1.2 Million Investment in NVIDIA Co. (NASDAQ:NVDA)"):
            with self.subTest(title=title):
                got = self.tags(title)
                self.assertEqual((got["tags"], got["impact"]), (["fund filing"], "low"))

    def test_real_stakes_are_not_fund_filings(self):
        # Same verb shape as a 13F report but no 13F markers: these are market-moving news.
        for title in ("Nvidia takes $5 billion stake in Intel",
                      "Elliott takes $2 billion stake in ACME",
                      "Berkshire Hathaway buys stake in ACME",
                      "Microsoft takes 9.9% stake in ACME"):
            with self.subTest(title=title):
                got = self.tags(title)
                self.assertNotIn("fund filing", got["tags"])
                self.assertIn("m&a", got["tags"])
                self.assertEqual(got["impact"], "high")

    def test_routine_sec_mentions_are_not_legal_news(self):
        self.assertEqual(self.tags("SEC Form 4: ACME director sells $1.2M in stock")["tags"], ["insider"])
        self.assertEqual(self.tags("ACME Robotics files 10-Q with SEC")["tags"], [])
        self.assertEqual(self.tags("ACME settles with the SEC over accounting")["impact"], "high")
        self.assertEqual(self.tags("SEC charges ACME Robotics with fraud")["impact"], "high")
        self.assertIn("legal/regulatory", self.tags("ACME files lawsuit with SEC backing")["tags"])

    def test_law_firm_reminders_are_low(self):
        for title in ("Robbins Geller Reminds ACME Investors of Class Action Deadline",
                      "FCEL Class Action Reminder: Robbins LLP Reminds Investors of the Lead Plaintiff Deadline",
                      "Hagens Berman Encourages ACME Robotics Investors with Losses to Contact Firm"):
            with self.subTest(title=title):
                got = self.tags(title)
                self.assertIn("law-firm ad", got["tags"])
                self.assertEqual(got["impact"], "low")
        # A real lawsuit, a poison pill and a proxy fight are not ads.
        self.assertEqual(self.tags("ACME hit with class action lawsuit over data breach")["impact"], "high")
        self.assertNotIn("law-firm ad", self.tags("ACME adopts shareholder rights plan")["tags"])
        self.assertNotIn("law-firm ad", self.tags("Elliott urges ACME shareholders to vote against board")["tags"])

    def test_option_contracts_are_not_product_news(self):
        self.assertEqual(self.tags("Unusual options activity: 50,000 ACME call contracts trade")["tags"], [])
        self.assertEqual(self.tags("ACME wins $300 million Army contract")["tags"], ["product"])

    def test_dollar_offering_and_analyst_view(self):
        got = self.tags("ACME launches $500 million offering")
        self.assertIn("offering", got["tags"])
        self.assertEqual(got["impact"], "high")
        self.assertNotIn("guidance", self.tags("Morgan Stanley maintains bullish view on ACME")["tags"])
        self.assertIn("guidance", self.tags("ACME reaffirms full-year outlook")["tags"])


class EasternTimeTest(unittest.TestCase):
    def et(self, iso):
        return news.to_eastern(news.parse_date(iso)).isoformat()

    def test_dst_boundaries_by_hand(self):
        # 2026: EDT from Sun 8 Mar 07:00 UTC to Sun 1 Nov 06:00 UTC.
        self.assertEqual(self.et("2026-03-08T06:59:00Z"), "2026-03-08T01:59:00-05:00")
        self.assertEqual(self.et("2026-03-08T07:00:00Z"), "2026-03-08T03:00:00-04:00")
        self.assertEqual(self.et("2026-11-01T05:59:00Z"), "2026-11-01T01:59:00-04:00")
        self.assertEqual(self.et("2026-11-01T06:00:00Z"), "2026-11-01T01:00:00-05:00")
        self.assertEqual(self.et("2026-12-15T14:30:00Z"), "2026-12-15T09:30:00-05:00")

    def test_matches_zoneinfo_when_available(self):
        try:
            from zoneinfo import ZoneInfo
            ny = ZoneInfo("America/New_York")
        except Exception:
            self.skipTest("no time-zone database on this system")
        from datetime import datetime, timedelta, timezone
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        while t.year == 2026:
            self.assertEqual(news.to_eastern(t).utcoffset(), t.astimezone(ny).utcoffset(), t)
            t += timedelta(hours=7)

    def test_sessions(self):
        def session(iso):
            return news.market_session(news.to_eastern(news.parse_date(iso)))
        self.assertEqual(session("2026-09-30T13:29:00Z"), "pre-market")     # 09:29 EDT
        self.assertEqual(session("2026-09-30T13:30:00Z"), "market hours")   # 09:30 EDT
        self.assertEqual(session("2026-09-30T19:59:00Z"), "market hours")   # 15:59 EDT
        self.assertEqual(session("2026-09-30T20:00:00Z"), "after hours")    # 16:00 EDT
        self.assertEqual(session("2026-09-27T15:00:00Z"), "weekend")        # Sunday

    def test_fixture_items_carry_et(self):
        res = news.analyze(make_raw(), days=7)
        item = next(i for i in res["items"] if i["title"].startswith("ACME Robotics Q3 earnings"))
        self.assertEqual(item["published"], "2026-09-30T20:15:00+00:00")
        self.assertEqual((item["published_et"], item["session"]), ("2026-09-30T16:15:00-04:00", "after hours"))
        self.assertEqual(res["window_start_et"], "2026-09-24T01:50:00-04:00")
        md = news.to_markdown(res)
        self.assertIn("(since 2026-09-24 01:50 ET)", md)
        self.assertIn("2026-09-30 16:15 ET (after hours) · Yahoo Finance · earnings, guidance", md)
        self.assertIn("tends to show up as a gap at the next open", md)


class CapPriorityTest(unittest.TestCase):
    def test_old_high_impact_item_survives_a_busy_week(self):
        # 50 low-impact stories on 30 Sep plus one earnings story six days earlier.
        items = [(f"Story number {n}", f"Tue, 30 Sep 2026 {n % 24:02d}:{n % 60:02d}:00 +0000") for n in range(50)]
        items.append(("ACME Robotics Q3 earnings beat estimates", "Thu, 25 Sep 2026 20:05:00 +0000"))
        raw = {"symbol": "ACME", "fetched_at": FETCHED_AT, "yahoo_rss": rss(items),
               "google_rss": None, "sec_submissions": None, "errors": {}}
        res = news.analyze(raw, days=7)
        self.assertEqual((res["counts"]["in_window"], res["counts"]["shown"]), (51, 40))
        self.assertEqual(res["impact_counts"], {"high": 1, "medium": 0, "low": 50})
        self.assertEqual(res["items"][-1]["title"], "ACME Robotics Q3 earnings beat estimates")
        published = [i["published"] for i in res["items"]]
        self.assertEqual(published, sorted(published, reverse=True))
        md = news.to_markdown(res)
        self.assertIn("**51 headlines in the last 7 days**", md)
        self.assertIn("### High impact (1)", md)


class SecHeavyFilerTest(unittest.TestCase):
    def test_bank_424b2s_are_counted_not_called_dilution(self):
        rows = ([("424B2", "2026-09-30", "")] * 50 + [("FWP", "2026-09-29", "")] * 5
                + [("8-K", "2026-09-26", "7.01,9.01")])
        s = news.analyze_sec(submissions(rows), "2026-09-24")
        self.assertEqual(s["dilution_count"], 0)
        self.assertEqual(s["bulk_counts"], {"424B2": 50, "FWP": 5})
        self.assertEqual([r["form"] for r in s["filings"]], ["8-K"])
        self.assertIn("424B2 prospectus supplements x50 (filed 2026-09-30): usually notes or debt, "
                      "not new shares; open one to check", s["flags"])
        self.assertEqual(s["coverage_note"], "")       # no older pages listed under filings.files
        md = "\n".join(news._sec_markdown(s, "ACME"))
        self.assertNotIn("Possible dilution", md)
        self.assertIn("Large banks file 424B2s", md)

    def test_scans_past_sec_default_limit_and_notes_truncation(self):
        # 450 in-window Form 4s, then a 424B5: sec.recent_filings' default of 40 rows would miss it.
        rows = [("4", "2026-09-30", "")] * 450 + [("424B5", "2026-09-25", "")]
        s = news.analyze_sec(submissions(rows, files=[{"name": "CIK0001999999-submissions-001.json"}]),
                             "2026-09-24")
        self.assertEqual((s["filings_in_window"], s["form4_count"], s["dilution_count"]), (451, 450, 1))
        self.assertIn("only reaches back to 2026-09-25", s["coverage_note"])
        self.assertIn("only reaches back to 2026-09-25", "\n".join(news._sec_markdown(s, "ACME")))


class PurityTest(unittest.TestCase):
    def test_analyze_reads_no_clock(self):
        from datetime import datetime as real_datetime

        class NoClock(real_datetime):
            @classmethod
            def now(cls, tz=None):
                raise AssertionError("analyze read the clock")

            @classmethod
            def utcnow(cls):
                raise AssertionError("analyze read the clock")

            @classmethod
            def today(cls):
                raise AssertionError("analyze read the clock")

        expected = news.analyze(make_raw(), days=7)
        with mock.patch("opptions.news.datetime", NoClock):
            got = news.analyze(make_raw(), days=7)
            news.to_markdown(got)
        self.assertEqual(got, expected)

    def test_no_reference_time_and_null_recent(self):
        raw = {"symbol": "ACME", "fetched_at": None, "yahoo_rss": None, "google_rss": None,
               "sec_submissions": {"cik": "1999999", "filings": {"recent": None}}, "errors": {}}
        res = news.analyze(raw, days=7)
        self.assertIsNone(res["window_start"])
        self.assertEqual(res["sec"]["filings_in_window"], 0)
        json.dumps(res)
        self.assertIn("no reference time", news.to_markdown(res))


if __name__ == "__main__":
    unittest.main()

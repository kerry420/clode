"""Build examples/sample-packet.md offline from the synthetic test fixtures.

    python3 examples/build_sample.py           # rewrite examples/sample-packet.md
    python3 examples/build_sample.py --check   # exit 1 if the file is out of date

Each tracker's analyze() and to_markdown() run over that tracker's own test
fixture, relabeled as the made-up ticker DEMO. No network and no clock: the
output is the same on every run. Tracker settings are pinned to their defaults
so OPPTIONS_* variables in your shell do not change the sample.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opptions import TRACKERS, earnings, flow, gex, news, packet  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
OUT = os.path.join(ROOT, "examples", "sample-packet.md")
SYMBOL = "DEMO"
FAKE_CIK = "9999999"  # unassigned, so EDGAR links cannot land on a real company

# Order matters: company names first, then bare tickers and URL slugs.
RENAMES = (
    ("ACME Robotics", "Demo Robotics"),
    ("Acme Widgets", "Demo Robotics"),
    ("ACME", SYMBOL),
    ("Acme", "Demo"),
    ("acme", "demo"),
    ("XYZ", SYMBOL),
)

BANNER = """> **Synthetic sample, not market data.** Built by `examples/build_sample.py`
> from the hand-made test fixtures in `tests/fixtures/`, relabeled as the
> made-up ticker DEMO. No network was used and no real company is described.
> Each section comes from its own fixture, so spot prices, times and stories do
> not line up across sections the way they do in a real packet (for example,
> the news fixture has an earnings release while the earnings fixture's next
> report is three weeks away). Links, CIK and filing numbers are placeholders.
> The "Generated" time below is fixed so the file is identical on every run.

"""

# Settings pinned to the module defaults (see the README's configuration table).
OPTIONS = {
    "gex": {"rate": gex.DEFAULT_RATE},
    "flow": {"min_volume": flow.DEFAULT_MIN_VOLUME, "min_premium": flow.DEFAULT_MIN_PREMIUM},
    "news": {"days": news.DEFAULT_DAYS},
    "earnings": {},
}
MODULES = {"gex": gex, "flow": flow, "news": news, "earnings": earnings}


def relabel(text):
    """Swap the fixtures' placeholder names for DEMO."""
    for old, new in RENAMES:
        text = text.replace(old, new)
    return text


def _text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return relabel(f.read())


def _json(name):
    return json.loads(_text(name))


def _sec(name):
    sub = _json(name)
    sub["cik"] = FAKE_CIK
    return sub


def raw_inputs():
    """The raw dict each tracker's fetch() would return, built from fixtures."""
    return {
        "gex": _json("gex_chain.json"),
        "flow": _json("flow_chain.json"),
        "news": {
            "symbol": SYMBOL,
            "fetched_at": "2026-10-01T05:50:00+00:00",
            "yahoo_rss": _text("news_yahoo.xml"),
            "google_rss": _text("news_google.xml"),
            "sec_submissions": _sec("news_sec_submissions.json"),
            "errors": {},
        },
        "earnings": {
            "symbol": SYMBOL,
            "fetched_at": "2026-10-01T14:30:00+00:00",
            "nasdaq_date": _json("earnings_nasdaq_date.json"),
            "nasdaq_surprise": _json("earnings_nasdaq_surprise.json"),
            "chain": _json("earnings_chain.json"),
            "sec_submissions": _sec("earnings_sec_submissions.json"),
            "errors": {},
        },
    }


def build():
    """Return the sample packet as Markdown, laid out like packet.build()."""
    header = packet.HEADER.format(symbol=SYMBOL, generated="2026-10-01 14:30", et_date="2026-10-01")
    title, rest = header.split("\n", 1)
    parts = [title + " (synthetic sample)\n\n", BANNER, rest.lstrip("\n")]
    raws = raw_inputs()
    for name in TRACKERS:
        mod = MODULES[name]
        result = mod.analyze(raws[name], **OPTIONS[name])
        parts.append(mod.to_markdown(result).rstrip() + "\n\n")
    return "".join(parts).rstrip() + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    md = build()
    if "--check" in argv:
        try:
            with open(OUT, encoding="utf-8") as f:
                current = f.read()
        except FileNotFoundError:
            current = None
        if current != md:
            print(f"{OUT} is out of date; run: python3 examples/build_sample.py", file=sys.stderr)
            return 1
        print(f"{OUT} is up to date")
        return 0
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(md)
    print(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())

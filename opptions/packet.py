"""Build one Markdown research packet per symbol from every tracker.

The packet is what goes to the Options Study chat: paste it, or upload the
.md file, together with your chart screenshots.
"""

import importlib
import json
import os
from datetime import datetime, timezone

from . import TRACKERS
from .http import FetchError
from .news import to_eastern

HEADER = """# Research packet: {symbol}

Generated {generated} UTC (US Eastern date {et_date}) by the opptions
trackers. Option data is Cboe's 15-minute-delayed chain; open interest updates
once per day. Treat every section as raw input for analysis, not a
recommendation.

"""


def eastern_date(now):
    """US Eastern trading date (YYYY-MM-DD) for an aware datetime; the agents name files by it."""
    return to_eastern(now).strftime("%Y-%m-%d")


def build(symbol):
    """Return (markdown, results) for one symbol. A failing tracker becomes a note, not a crash."""
    symbol = symbol.upper()
    now = datetime.now(timezone.utc)
    parts = [HEADER.format(symbol=symbol, generated=now.strftime("%Y-%m-%d %H:%M"),
                           et_date=eastern_date(now))]
    results = {}
    for name in TRACKERS:
        try:
            mod = importlib.import_module(f"opptions.{name}")
            result = mod.analyze(mod.fetch(symbol))
            results[name] = result
            parts.append(mod.to_markdown(result).rstrip() + "\n\n")
        except FetchError as e:
            results[name] = {"error": str(e)}
            parts.append(f"## {name.upper()}\n\n_Not available: {e}_\n\n")
        except Exception as e:  # keep the rest of the packet usable
            results[name] = {"error": f"{type(e).__name__}: {e}"}
            parts.append(f"## {name.upper()}\n\n_Tracker failed: {type(e).__name__}: {e}_\n\n")
    return "".join(parts), results


def main(symbols, out_dir=None, as_json=False):
    status = 0
    for symbol in symbols:
        md, results = build(symbol)
        if any("error" in r for r in results.values()):
            status = 1
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            stamp = eastern_date(datetime.now(timezone.utc))
            path = os.path.join(out_dir, f"{symbol.upper()}-{stamp}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(md)
            print(path)
        elif as_json:
            print(json.dumps(results, indent=2, default=str))
        else:
            print(md)
    return status

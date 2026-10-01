"""Command line entry point.

    python -m opptions gex SPY
    python -m opptions flow NVDA --json
    python -m opptions news TSLA
    python -m opptions earnings AAPL
    python -m opptions packet NVDA TSLA --out packets/
    python -m opptions scan flow|gex [SYMBOLS...] [--universe FILE] [--top N] [--json]
    python -m opptions confirm SCAN_FLOW_JSON [--json] [--top N]
"""

import argparse
import importlib
import json
import sys

from . import TRACKERS
from .http import FetchError


def run_tracker(name, symbol):
    mod = importlib.import_module(f"opptions.{name}")
    raw = mod.fetch(symbol)
    return mod, mod.analyze(raw)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["scan"]:  # desk scan has its own arguments; see opptions/scan.py
        from . import scan
        return scan.main(argv[1:])
    if argv[:1] == ["confirm"]:  # next-morning check of a saved `scan flow --json`; see opptions/confirm.py
        from . import confirm
        return confirm.main(argv[1:])
    p = argparse.ArgumentParser(prog="opptions", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=TRACKERS + ("packet",))
    p.add_argument("symbols", nargs="+", help="ticker symbols, e.g. NVDA SPY")
    p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    p.add_argument("--out", help="packet only: directory to write one .md per symbol")
    args = p.parse_args(argv)

    if args.command == "packet":
        from . import packet
        return packet.main(args.symbols, out_dir=args.out, as_json=args.json)

    status = 0
    for symbol in args.symbols:
        symbol = symbol.upper()
        try:
            mod, result = run_tracker(args.command, symbol)
        except FetchError as e:
            print(f"[{args.command} {symbol}] {e}", file=sys.stderr)
            status = 1
            continue
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(mod.to_markdown(result))
            print()
    return status


if __name__ == "__main__":
    sys.exit(main())

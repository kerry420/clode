"""Free 15-minute-delayed option chains from Cboe.

One request returns every listed contract for a symbol with greeks, IV,
volume and open interest, which is all the GEX and flow trackers need.

    https://cdn-api.cboe.com/api/global/delayed_quotes/options/SPY.json
    https://cdn-api.cboe.com/api/global/delayed_quotes/options/_SPX.json   (indexes)

Shape (abridged):
    {"timestamp": "2026-10-01 03:57:05",
     "data": {"symbol": "SPY", "current_price": 601.2, "close": ..., "iv30": ...,
              "options": [{"option": "SPY260930C00375000", "bid": ..., "ask": ...,
                           "iv": 0.21, "open_interest": 15475.0, "volume": 6.0,
                           "delta": ..., "gamma": ..., "theo": ...,
                           "last_trade_price": ..., "last_trade_time": "...",
                           "prev_day_close": ...}, ...]}}
"""

import re
from datetime import date, datetime
from functools import lru_cache

from .http import fetch_json

BASE = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/"
# Cboe prefixes cash-settled index underlyings with an underscore.
INDEXES = {"SPX", "XSP", "NDX", "RUT", "VIX", "DJX", "OEX", "XEO", "MRUT"}

OCC_RE = re.compile(r"^(?P<root>[A-Z.]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def chain_url(symbol):
    sym = symbol.upper().lstrip("_^$")
    return f"{BASE}{'_' if sym in INDEXES else ''}{sym}.json"


@lru_cache(maxsize=32)
def fetch_chain(symbol):
    """Raw Cboe JSON for one underlying (cached per process; treat as read-only)."""
    return fetch_json(chain_url(symbol))


def parse_option_symbol(contract):
    """'SPY260930C00375000' -> {'root': 'SPY', 'expiry': date(2026, 9, 30), 'type': 'C', 'strike': 375.0}"""
    m = OCC_RE.match(contract.replace(" ", ""))
    if not m:
        raise ValueError(f"not an OCC option symbol: {contract!r}")
    ymd = m["ymd"]
    return {
        "root": m["root"],
        "expiry": date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:])),
        "type": m["cp"],
        "strike": int(m["strike"]) / 1000.0,
    }


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0  # drop NaN


def _as_of(raw):
    ts = raw.get("timestamp") or (raw.get("data") or {}).get("timestamp")
    if ts:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(str(ts)[:19], fmt)
            except ValueError:
                pass
    return None


def _infer_spot(data, options):
    for key in ("current_price", "close", "last_trade_price", "prev_day_close"):
        v = _num(data.get(key))
        if v > 0:
            return v, key
    # Fall back to the strike whose call delta is closest to 0.5.
    calls = [o for o in options if o["type"] == "C" and 0 < o["delta"] < 1]
    if calls:
        best = min(calls, key=lambda o: abs(o["delta"] - 0.5))
        return best["strike"], "atm_delta_estimate"
    return 0.0, "unknown"


def normalize_chain(raw):
    """Turn Cboe's raw JSON into a flat, typed structure.

    Returns {"symbol", "spot", "spot_source", "as_of" (ISO str or None),
             "iv30", "options": [{"contract", "expiry" (ISO date), "dte", "type",
             "strike", "bid", "ask", "mid", "last", "volume", "open_interest",
             "iv", "delta", "gamma", "theo", "last_trade_time"}]}
    Contracts whose symbol cannot be parsed are skipped.
    """
    data = raw.get("data") or {}
    as_of = _as_of(raw)
    today = as_of.date() if as_of else date.today()
    options = []
    for o in data.get("options") or []:
        try:
            parsed = parse_option_symbol(str(o.get("option", "")))
        except ValueError:
            continue
        bid, ask = _num(o.get("bid")), _num(o.get("ask"))
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else max(bid, ask)
        options.append({
            "contract": o.get("option"),
            "expiry": parsed["expiry"].isoformat(),
            "dte": (parsed["expiry"] - today).days,
            "type": parsed["type"],
            "strike": parsed["strike"],
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": _num(o.get("last_trade_price")),
            "volume": _num(o.get("volume")),
            "open_interest": _num(o.get("open_interest")),
            "iv": _num(o.get("iv")),
            "delta": _num(o.get("delta")),
            "gamma": _num(o.get("gamma")),
            "theo": _num(o.get("theo")),
            "last_trade_time": o.get("last_trade_time"),
        })
    spot, spot_source = _infer_spot(data, options)
    return {
        "symbol": (data.get("symbol") or "").lstrip("_") or None,
        "spot": spot,
        "spot_source": spot_source,
        "as_of": as_of.isoformat() if as_of else None,
        "iv30": _num(data.get("iv30")) or None,
        "options": options,
    }

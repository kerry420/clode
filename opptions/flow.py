"""Options activity: unusual volume from the free delayed chain (NOT true flow).

Source: Cboe's free, 15-minute-delayed option chain (see cboe.py). Each
contract carries its cumulative volume for the session, open interest (OI,
updated once a day), bid/ask, the last trade and its time.

What this is, honestly: an "unusual options activity" screen built from the
day's cumulative volume per contract. It is NOT trade-by-trade flow. It cannot
see individual prints, sweeps versus blocks, multi-leg spreads, or whether a
trade opened or closed a position. True flow needs a paid tape feed (Unusual
Whales, Cheddar Flow, Polygon/Massive and similar), which can be wired in
later behind the same fetch/analyze/to_markdown contract.

Method:
  * premium = volume * (mid if mid > 0 else last) * 100   (rough dollars traded)
  * side estimate, from where the LAST trade printed against the current quote:
        last >= ask - 0.1 * spread  -> "ask"  (likely bought)
        last <= bid + 0.1 * spread  -> "bid"  (likely sold)
        otherwise                   -> "mid"
    Only when the last trade is from the chain's session; otherwise "stale".
    "unknown" when there is no usable quote or last price. The session is the
    chain's as_of date; if no contract traded on that date (a weekend or
    pre-market fetch), it falls back to the newest trade date in the chain.
  * unusual: volume >= OPPTIONS_MIN_VOLUME (default 250) AND volume > OI AND
    premium >= OPPTIONS_MIN_PREMIUM (default 50000). Ranked by extrinsic
    premium (below), then premium.
  * estimated bullish premium = calls at ask + puts at bid; bearish = puts at
    ask + calls at bid. The lean is "bullish" or "bearish" only when one side
    exceeds the other by 1.5x, otherwise "mixed". The whole day's volume of a
    contract is credited to its last trade's side, so this is weak evidence.
  * time value: intrinsic = max(0, spot - strike) for calls, max(0, strike -
    spot) for puts; extrinsic = max(0, price - intrinsic) with the same price
    premium() uses; extrinsic premium = volume * extrinsic * 100. A contract is
    deep in the money (deep_itm) when |delta| >= 0.90, or, with no delta, when
    intrinsic is at least 90% of its price. Deep ITM premium is mostly built-in
    value (stock replacement, financing, rolls), so the "extrinsic" block gives
    totals, sides and a lean in extrinsic premium, with deep ITM contracts left
    out of that lean. With spot unknown, intrinsic is blank and extrinsic is
    the whole price. "totals", "sentiment" and "premium_by_side" keep counting
    total premium.

analyze() is pure: no network and no clock. Days to expiry come from the
chain's own timestamp via cboe.normalize_chain. All IVs in the result are
decimals (0.42 = 42%); Cboe's iv30 arrives in percent points and is converted.
"""

import math
import os
import re
from datetime import datetime

from . import cboe

TITLE = "Options Activity (unusual volume)"
MULTIPLIER = 100
DEFAULT_MIN_VOLUME = 250
DEFAULT_MIN_PREMIUM = 50_000
SIDE_BAND = 0.10        # last within 10% of the spread from the ask or bid
LEAN_FACTOR = 1.5
SHORT_DTE = 7
TOP_UNUSUAL = 15
TOP_STRIKES = 5
ATM_BAND_PCT = 0.5      # within 0.5% of spot reads as at-the-money
DEEP_ITM_DELTA = 0.90   # |delta| at or above this reads as deep in the money
DEEP_ITM_INTRINSIC = 0.90  # with no delta: intrinsic at least this share of the price
_EPS = 1e-9             # float slack so a last exactly on a band edge counts as inside it

SIDES = ("ask", "bid", "mid", "stale", "unknown")
SIDE_LABEL = {
    "ask": "ask (likely bought)",
    "bid": "bid (likely sold)",
    "mid": "mid",
    "stale": "stale",
    "unknown": "unknown",
}
TYPE_NAME = {"C": "call", "P": "put"}

LEAN_MEANING = {
    "bullish": ("More premium looks bullish (calls paid at the ask, puts sold at the bid). "
                "That tends to lean toward upside bets, but a hedge or one leg of a spread "
                "can look the same."),
    "bearish": ("More premium looks bearish (puts paid at the ask, calls sold at the bid). "
                "That tends to lean toward downside bets or hedging, but a spread leg or "
                "covered-call selling can look the same."),
    "mixed": ("Neither side clearly dominated, so the day's activity does not show a "
              "directional tilt."),
}

PAID_FEEDS = "Unusual Whales, Cheddar Flow, Polygon/Massive or similar"


def fetch(symbol):
    """Raw Cboe chain JSON (one request). Raises FetchError if Cboe cannot be reached."""
    return cboe.fetch_chain(symbol)


def _env_number(name, default):
    """Env value as a float; blank, non-numeric, negative, NaN or infinite -> default."""
    try:
        v = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) and v >= 0 else float(default)


def thresholds(min_volume=None, min_premium=None):
    """Unusual-activity thresholds: explicit arguments win, then env, then defaults."""
    return {
        "min_volume": float(min_volume) if min_volume is not None
        else _env_number("OPPTIONS_MIN_VOLUME", DEFAULT_MIN_VOLUME),
        "min_premium": float(min_premium) if min_premium is not None
        else _env_number("OPPTIONS_MIN_PREMIUM", DEFAULT_MIN_PREMIUM),
    }


def contract_price(o):
    """Per-share price used for premium: mid if mid > 0 else last."""
    return o["mid"] if o["mid"] > 0 else o["last"]


def premium(o):
    """Rough dollars traded: volume * (mid if mid > 0 else last) * 100."""
    return o["volume"] * contract_price(o) * MULTIPLIER


def intrinsic_value(opt_type, strike, spot):
    """Built-in value per share: max(0, spot - strike) for calls, max(0, strike - spot)
    for puts. None when spot is unknown."""
    if spot <= 0:
        return None
    return max(0.0, spot - strike) if opt_type == "C" else max(0.0, strike - spot)


def time_value(o, spot):
    """(intrinsic, extrinsic, extrinsic_premium, deep_itm) for one normalized contract.

    extrinsic = max(0, price - intrinsic) per share, with premium()'s price; extrinsic
    premium = volume * extrinsic * 100. With spot unknown, intrinsic is None and the
    whole price counts as extrinsic. deep_itm: |delta| >= 0.90, or, when delta is
    missing (zero), intrinsic >= 90% of the price.
    """
    price = contract_price(o)
    intrinsic = intrinsic_value(o["type"], o["strike"], spot)
    extrinsic = max(0.0, price - (intrinsic or 0.0))
    if o["delta"]:
        deep = abs(o["delta"]) >= DEEP_ITM_DELTA
    else:
        deep = bool(intrinsic) and price > 0 and intrinsic / price >= DEEP_ITM_INTRINSIC
    return intrinsic, extrinsic, o["volume"] * extrinsic * MULTIPLIER, deep


_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def trade_date(ts):
    """ISO date string of a Cboe last_trade_time ('2026-09-30T15:59:12'), or None."""
    if not ts:
        return None
    s = str(ts).strip()
    m = _ISO_DATE.match(s)
    if m:
        return m.group(1)
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def session_date(as_of, options):
    """(session ISO date, source) the volume belongs to.

    The chain's as_of date when any contract traded that day; otherwise the
    newest trade date in the chain (a weekend or pre-market fetch still carries
    the last session), else as_of's date.
    """
    as_of_date = as_of[:10] if as_of else None
    dates = {d for d in (trade_date(o.get("last_trade_time")) for o in options) if d}
    if as_of_date and as_of_date in dates:
        return as_of_date, "as_of"
    if dates:
        return max(dates), "latest_trade"
    return as_of_date, "as_of"


def estimate_side(o, session):
    """'ask', 'bid', 'mid', 'stale' or 'unknown' from the last trade vs the quote."""
    if not session or trade_date(o.get("last_trade_time")) != session:
        return "stale"
    bid, ask, last = o["bid"], o["ask"], o["last"]
    if last <= 0 or ask <= 0 or bid > ask:
        return "unknown"
    band = SIDE_BAND * (ask - bid)
    # _EPS keeps cent prices on the edge inside the band: 5.10 + 0.1 * 0.10
    # is 5.10999... in floating point, so a 5.11 print would miss "bid".
    if last >= ask - band - _EPS:
        return "ask"
    if last <= bid + band + _EPS:
        return "bid"
    return "mid"


def is_unusual(volume, open_interest, prem, th):
    """Volume clears the floor, exceeds open interest, and premium clears the floor."""
    return (volume >= th["min_volume"] and volume > open_interest
            and prem >= th["min_premium"])


def lean_label(bullish, bearish, factor=LEAN_FACTOR):
    """'bullish' or 'bearish' when one side beats the other by `factor`, else 'mixed'."""
    if bullish > 0 and bullish > factor * bearish:
        return "bullish"
    if bearish > 0 and bearish > factor * bullish:
        return "bearish"
    return "mixed"


def _ratio(num, den, digits=3):
    return round(num / den, digits) if den > 0 else None


def _moneyness(strike, spot):
    return round((strike - spot) / spot * 100, 2) if spot > 0 else None


def _is_otm(opt_type, strike, spot):
    if spot <= 0:
        return None
    return strike > spot if opt_type == "C" else strike < spot


def _iv30_decimal(v):
    """Cboe's iv30 is in percent points (48.5); option iv is a decimal (0.485)."""
    if not v or v <= 0:
        return None
    return round(v / 100.0 if v > 3 else v, 4)


def front_atm_iv(options, spot):
    """ATM IV of the nearest expiry (skipping same-day expiry when a later one exists).

    Averages call and put IV at the strike closest to spot, and gives the
    one-standard-deviation expected move to that expiry: spot * iv * sqrt(dte/365).
    """
    if spot <= 0:
        return None
    expiries = sorted({(o["dte"], o["expiry"]) for o in options if o["dte"] >= 0 and o["iv"] > 0})
    if not expiries:
        return None
    dte, expiry = next((e for e in expiries if e[0] >= 1), expiries[0])
    rows = [o for o in options if o["expiry"] == expiry and o["iv"] > 0]
    strike = min({o["strike"] for o in rows}, key=lambda k: (abs(k - spot), k))
    ivs = [o["iv"] for o in rows if o["strike"] == strike]
    iv = sum(ivs) / len(ivs)
    frac = iv * math.sqrt(max(dte, 1) / 365.0)
    return {
        "expiry": expiry,
        "dte": dte,
        "strike": strike,
        "iv": round(iv, 4),
        "expected_move": round(spot * frac, 2),
        "expected_move_pct": round(frac * 100, 2),
    }


def top_oi_strikes(options, opt_type, spot, n=TOP_STRIKES):
    """Strikes with the most open interest for one type, summed over live expiries."""
    by_strike = {}
    for o in options:
        if o["type"] != opt_type or o["dte"] < 0:
            continue
        s = by_strike.setdefault(o["strike"], {"oi": 0.0, "vol": 0.0, "exp": {}})
        s["oi"] += o["open_interest"]
        s["vol"] += o["volume"]
        s["exp"][o["expiry"]] = s["exp"].get(o["expiry"], 0.0) + o["open_interest"]
    held = [kv for kv in by_strike.items() if kv[1]["oi"] > 0]
    ranked = sorted(held, key=lambda kv: (-kv[1]["oi"], kv[0]))[:n]
    out = []
    for strike, s in ranked:
        top_exp, top_oi = max(s["exp"].items(), key=lambda kv: (kv[1], kv[0]))
        out.append({
            "strike": strike,
            "open_interest": int(s["oi"]),
            "volume": int(s["vol"]),
            "moneyness_pct": _moneyness(strike, spot),
            "top_expiry": top_exp,
            "top_expiry_share": round(top_oi / s["oi"], 3),
        })
    return out


def _unusual_row(o, prem, side, spot, tv):
    oi = o["open_interest"]
    intrinsic, extrinsic, ext_prem, deep = tv
    return {
        "contract": o["contract"],
        "type": TYPE_NAME[o["type"]],
        "strike": o["strike"],
        "expiry": o["expiry"],
        "dte": o["dte"],
        "volume": int(o["volume"]),
        "open_interest": int(oi),
        "vol_oi": round(o["volume"] / oi, 2) if oi > 0 else None,
        "premium": round(prem, 2),
        "intrinsic": None if intrinsic is None else round(intrinsic, 4),
        "extrinsic": round(extrinsic, 4),
        "extrinsic_premium": round(ext_prem, 2),
        "deep_itm": deep,
        "side": side,
        "bid": o["bid"],
        "ask": o["ask"],
        "last": o["last"],
        "iv": round(o["iv"], 4) if o["iv"] > 0 else None,
        "delta": round(o["delta"], 3),
        "moneyness_pct": _moneyness(o["strike"], spot),
        "otm": _is_otm(o["type"], o["strike"], spot),
    }


def analyze(raw, min_volume=None, min_premium=None):
    """Turn a raw Cboe chain into the activity summary (pure, JSON-serializable).

    Returns {"symbol", "spot", "spot_source", "as_of", "session_date",
    "session_date_source", "thresholds", "contracts_in_chain",
    "contracts_traded", "totals", "sentiment", "short_dated",
    "premium_by_side", "extrinsic", "unusual_count", "unusual_deep_itm_count",
    "unusual" (top 15 by extrinsic premium), "positioning": {"calls", "puts"},
    "volatility": {"front", "iv30"}, "notes"}.

    "totals", "sentiment" and "premium_by_side" count total premium, as before.
    "extrinsic" counts only time value: {"call_premium", "put_premium",
    "total_premium", "share_of_premium", "deep_itm_contracts" (traded),
    "deep_itm_premium", "premium_by_side", "bullish_premium", "bearish_premium",
    "classified_share", "lean", "lean_factor"}; its lean leaves out deep ITM
    contracts. Each unusual row adds "intrinsic" (per share), "extrinsic" (per
    share), "extrinsic_premium" and "deep_itm".
    """
    chain = cboe.normalize_chain(raw)
    th = thresholds(min_volume, min_premium)
    options = chain["options"]
    spot = chain["spot"]
    session, session_src = session_date(chain["as_of"], options)

    vol = {"C": 0.0, "P": 0.0}
    prem_by_type = {"C": 0.0, "P": 0.0}
    oi = {"C": 0.0, "P": 0.0}
    by_side = dict.fromkeys(SIDES, 0.0)
    ext_by_type = {"C": 0.0, "P": 0.0}
    ext_by_side = dict.fromkeys(SIDES, 0.0)
    bullish = bearish = short_prem = 0.0
    ext_bullish = ext_bearish = deep_prem = 0.0
    traded = stale_traded = deep_traded = 0
    unusual = []
    for o in options:
        t = o["type"]
        prem = premium(o)
        side = estimate_side(o, session)
        tv = time_value(o, spot)
        ext_prem, deep = tv[2], tv[3]
        vol[t] += o["volume"]
        prem_by_type[t] += prem
        ext_by_type[t] += ext_prem
        oi[t] += o["open_interest"]
        if o["volume"] > 0:
            traded += 1
            by_side[side] += prem
            ext_by_side[side] += ext_prem
            stale_traded += side == "stale"
            if deep:
                deep_traded += 1
                deep_prem += prem
        if (t, side) in (("C", "ask"), ("P", "bid")):
            bullish += prem
            ext_bullish += 0.0 if deep else ext_prem
        elif (t, side) in (("P", "ask"), ("C", "bid")):
            bearish += prem
            ext_bearish += 0.0 if deep else ext_prem
        if o["dte"] <= SHORT_DTE:
            short_prem += prem
        if is_unusual(o["volume"], o["open_interest"], prem, th):
            unusual.append(_unusual_row(o, prem, side, spot, tv))
    # Time value first: a deep ITM block is mostly built-in value and says little.
    unusual.sort(key=lambda r: (-r["extrinsic_premium"], -r["premium"], r["contract"]))
    total_ext = ext_by_type["C"] + ext_by_type["P"]

    total_prem = prem_by_type["C"] + prem_by_type["P"]
    total_vol = vol["C"] + vol["P"]
    notes = []
    if not options:
        notes.append("Cboe returned no option contracts. The symbol may have no listed "
                     "options, or it may be wrong.")
    elif total_vol == 0:
        notes.append("No contracts have traded in this session yet (pre-market, a market "
                     "holiday, or the feed has reset), so activity numbers are empty. Open "
                     "interest positioning is still meaningful.")
    if session_src == "latest_trade" and chain["as_of"]:
        notes.append(f"No trades are stamped on the chain's own date ({chain['as_of'][:10]}), "
                     f"so side estimates use the latest session in the chain ({session}).")
    if stale_traded:
        many = stale_traded != 1
        when = f"is not from the {session} session" if session else "has no usable time"
        notes.append(f"{stale_traded} contract{'s' if many else ''} show{'' if many else 's'} volume "
                     f"but {'their' if many else 'its'} last trade {when}. That volume may be left "
                     "over from an earlier session, so those numbers are less reliable and are kept "
                     "out of the lean.")
    if options and spot <= 0:
        notes.append("Spot price is unknown, so moneyness, intrinsic value and expected move are "
                     "blank, and all premium counts as time value.")
    if len(unusual) > TOP_UNUSUAL:
        notes.append(f"{len(unusual)} contracts met the unusual rule; showing the top "
                     f"{TOP_UNUSUAL} by time value (extrinsic premium).")

    return {
        "symbol": chain["symbol"],
        "spot": spot,
        "spot_source": chain["spot_source"],
        "as_of": chain["as_of"],
        "session_date": session,
        "session_date_source": session_src,
        "thresholds": th,
        "contracts_in_chain": len(options),
        "contracts_traded": traded,
        "totals": {
            "call_volume": int(vol["C"]),
            "put_volume": int(vol["P"]),
            "total_volume": int(total_vol),
            "call_premium": round(prem_by_type["C"], 2),
            "put_premium": round(prem_by_type["P"], 2),
            "total_premium": round(total_prem, 2),
            "call_open_interest": int(oi["C"]),
            "put_open_interest": int(oi["P"]),
            "put_call_volume_ratio": _ratio(vol["P"], vol["C"]),
            "put_call_premium_ratio": _ratio(prem_by_type["P"], prem_by_type["C"]),
            "put_call_oi_ratio": _ratio(oi["P"], oi["C"]),
        },
        "sentiment": {
            "bullish_premium": round(bullish, 2),
            "bearish_premium": round(bearish, 2),
            "classified_share": _ratio(bullish + bearish, total_prem),
            "lean": lean_label(bullish, bearish),
            "lean_factor": LEAN_FACTOR,
        },
        "short_dated": {
            "max_dte": SHORT_DTE,
            "premium": round(short_prem, 2),
            "share": _ratio(short_prem, total_prem),
        },
        "premium_by_side": {k: round(v, 2) for k, v in by_side.items()},
        "extrinsic": {
            "call_premium": round(ext_by_type["C"], 2),
            "put_premium": round(ext_by_type["P"], 2),
            "total_premium": round(total_ext, 2),
            "share_of_premium": _ratio(total_ext, total_prem),
            "deep_itm_contracts": deep_traded,
            "deep_itm_premium": round(deep_prem, 2),
            "premium_by_side": {k: round(v, 2) for k, v in ext_by_side.items()},
            "bullish_premium": round(ext_bullish, 2),
            "bearish_premium": round(ext_bearish, 2),
            "classified_share": _ratio(ext_bullish + ext_bearish, total_ext),
            "lean": lean_label(ext_bullish, ext_bearish),
            "lean_factor": LEAN_FACTOR,
        },
        "unusual_count": len(unusual),
        "unusual_deep_itm_count": sum(1 for r in unusual if r["deep_itm"]),
        "unusual": unusual[:TOP_UNUSUAL],
        "positioning": {
            "calls": top_oi_strikes(options, "C", spot),
            "puts": top_oi_strikes(options, "P", spot),
        },
        "volatility": {
            "front": front_atm_iv(options, spot),
            "iv30": _iv30_decimal(chain["iv30"]),
        },
        "notes": notes,
    }


# ---------------------------------------------------------------- Markdown

def fmt_money(x):
    """$230K, $1.25M, $900; 'n/a' for None."""
    if x is None:
        return "n/a"
    a = abs(x)
    # Unit cut-offs sit where rounding would otherwise print "$1000K" or "$1000.00M".
    if a >= 999_995_000:
        s = f"${a / 1e9:.2f}B"
    elif a >= 999_500:
        s = f"${a / 1e6:.2f}M"
    elif a >= 1e5:
        s = f"${a / 1e3:.0f}K"
    elif a >= 1e3:
        s = f"${a / 1e3:.1f}".rstrip("0").rstrip(".") + "K"
    else:
        s = f"${a:,.0f}"
    return ("-" if x < 0 else "") + s


def _fmt_int(x):
    return f"{x:,.0f}" if x is not None else "n/a"


def _fmt_ratio(r):
    return f"{r:.2f}" if r is not None else "n/a"


def _fmt_share(s):
    return f"{s * 100:.0f}%" if s is not None else "n/a"


def _fmt_iv(iv):
    return f"{iv * 100:.0f}%" if iv else "n/a"


def _fmt_price(p):
    return f"{p:,.2f}".rstrip("0").rstrip(".") if p else "n/a"


def _fmt_pct(pct):
    if pct is None:
        return "n/a"
    return "at spot" if abs(pct) < 0.05 else f"{pct:+.1f}%"


def _fmt_moneyness(pct, otm, deep=False):
    if pct is None:
        return "deep ITM" if deep else "n/a"
    if abs(pct) < ATM_BAND_PCT:
        return f"{_fmt_pct(pct)} (ATM)"
    return f"{_fmt_pct(pct)} ({'deep ITM' if deep else 'OTM' if otm else 'ITM'})"


def _pc_volume_meaning(r):
    if r is None:
        return "No call volume, so the ratio cannot be computed."
    if r >= 1.5:
        return ("Put-heavy trading. This often reflects hedging or bearish bets; extreme readings "
                "sometimes show up near short-term lows when fear gets crowded.")
    if r >= 1.0:
        return ("More puts than calls traded, which tends to reflect caution, hedging or bearish "
                "bets (for index ETFs, readings above 1 are normal hedging).")
    if r >= 0.5:
        return "More calls than puts, which is common for single stocks and leans mildly optimistic."
    return ("Call-heavy trading, which tends to reflect bullish speculation; very lopsided call "
            "activity sometimes shows up near short-term tops when optimism gets crowded.")


def _pc_premium_meaning(r):
    if r is None:
        return "No call premium, so the ratio cannot be computed."
    lead = ("More money went into puts." if r > 1 else "More money went into calls." if r < 1
            else "About the same money went into calls and puts.")
    return (f"{lead} Premium counts dollars, so it tends to show where the bigger bets went better "
            "than raw contract counts do.")


def _pc_oi_meaning(r):
    if r is None:
        return "No call open interest, so the ratio cannot be computed."
    lead = ("More open puts than calls, often hedges protecting shares." if r > 1
            else "More open calls than puts." if r < 1 else "Open calls and puts are about even.")
    return (f"{lead} Open interest is positioning built over days or weeks (updated overnight), "
            "so this ratio moves slowly.")


def _short_meaning(share):
    if share is None:
        return "No premium was traded, so there is no split to read."
    if share > 0.6:
        return ("Most of the money is in options expiring within a week. Short-dated activity tends "
                "to be speculative or event-driven, can add to swings around big strikes, and fades fast.")
    if share >= 0.4:
        return ("The money is split roughly evenly between options expiring within a week and "
                "longer-dated ones, so neither quick bets nor longer positions clearly dominate.")
    return ("Most of the money is in longer-dated options, which tends to reflect positions meant to "
            "last more than a few days.")


def _iv_lines(vol):
    front, iv30 = vol.get("front"), vol.get("iv30")
    lines = []
    if front:
        lines.append(f"- **Implied volatility:** front expiry {front['expiry']} ({front['dte']} DTE) "
                     f"ATM IV {_fmt_iv(front['iv'])} at the {_fmt_price(front['strike'])} strike"
                     + (f"; IV30 {_fmt_iv(iv30)}." if iv30 else "."))
        lines.append(f"  Options price a typical move of about ±${_fmt_price(front['expected_move'])} "
                     f"(±{front['expected_move_pct']:.1f}%) by {front['expiry']}; price tends to stay "
                     "inside that range about two times out of three when nothing unexpected happens.")
        if iv30:
            r = front["iv"] / iv30
            if r > 1.15:
                lines.append("  Front IV is well above IV30, which usually means the market is pricing "
                             "a near-term event such as earnings.")
            elif r < 0.85:
                lines.append("  Front IV is below IV30, which usually means traders expect a quieter "
                             "near term than the month ahead.")
            else:
                lines.append("  Front IV is close to IV30, so no special near-term event appears priced in.")
    elif iv30:
        lines.append(f"- **Implied volatility:** IV30 {_fmt_iv(iv30)}. Higher IV tends to mean bigger "
                     "expected moves and pricier options.")
    return lines


DEEP_ITM_LINE = ("Deep in-the-money options (|delta| 0.90 or more, marked \"deep ITM\") carry mostly "
                 "built-in value, often from stock replacement or rolls rather than a fresh bet, so the "
                 "desk weighs the time-value part (~Extrinsic) more than the total premium.")


def _time_value_lines(ex, total_premium):
    """Summary bullet for the time-value (extrinsic) totals and lean; [] for older results."""
    if not ex:
        return []
    lean = ex.get("lean", "mixed")
    deep_n = ex.get("deep_itm_contracts") or 0
    deep = (f" {deep_n} deep in-the-money contract{'s' if deep_n != 1 else ''} traded "
            f"~{fmt_money(ex.get('deep_itm_premium'))} of premium." if deep_n else "")
    built_in = (total_premium or 0) - (ex.get("total_premium") or 0)
    rest = "; the rest is built-in value of in-the-money options" if built_in >= 0.5 else ""
    return [f"- **Time value:** ~{fmt_money(ex.get('total_premium'))} of the ~{fmt_money(total_premium)} "
            f"premium ({_fmt_share(ex.get('share_of_premium'))}) is time value{rest}.{deep} "
            f"Time-value lean: {lean} "
            f"(~{fmt_money(ex.get('bullish_premium'))} bullish-looking vs "
            f"~{fmt_money(ex.get('bearish_premium'))} bearish-looking, deep in-the-money contracts left out).",
            "  Time value is the part of the price that can be lost by expiry, so it tends to show the size "
            "of a bet better than total premium, which deep in-the-money options inflate."]


def _caveat(result):
    th = result.get("thresholds") or {}
    return ("_Caveat: free Cboe data, about 15 minutes delayed. This is unusual activity from each "
            "contract's cumulative volume for the session, not trade-by-trade flow: it cannot see "
            "sweeps, blocks, multi-leg spreads, or whether trades opened or closed positions. \"Side\" "
            "credits the whole day's volume to where the last trade printed against the current "
            "bid/ask, which is weak evidence. Open interest updates once a day. Unusual rule: volume "
            f"≥ {_fmt_int(th.get('min_volume'))}, volume above open interest, premium ≥ "
            f"{fmt_money(th.get('min_premium'))}. True flow needs a paid feed ({PAID_FEEDS}), which "
            "can be wired in later. Nothing here is a trade recommendation._")


def to_markdown(result):
    """Readable activity section for a person learning, and for an LLM agent."""
    sym = result.get("symbol") or "?"
    lines = [f"## {TITLE}: {sym}", ""]
    spot = result.get("spot") or 0
    as_of = (result.get("as_of") or "unknown time").replace("T", " ")[:16]
    lines.append(f"Spot **{'$' + _fmt_price(spot) if spot else 'n/a'}** as of {as_of} (Cboe timestamp, "
                 f"about 15 minutes delayed). Session {result.get('session_date') or 'unknown'}: "
                 f"{result.get('contracts_traded', 0):,} of {result.get('contracts_in_chain', 0):,} "
                 "contracts traded.")
    lines.append("_Built from each contract's total volume for the day, not trade-by-trade prints, "
                 "so it shows where activity was unusual, not individual sweeps or blocks._")
    lines.append("")

    if not result.get("contracts_in_chain"):
        lines += [f"- {n}" for n in result.get("notes") or []] + ["", _caveat(result)]
        return "\n".join(lines) + "\n"

    t = result.get("totals") or {}
    s = result.get("sentiment") or {}
    sd = result.get("short_dated") or {}
    lines += ["### Summary", ""]
    if t.get("total_volume"):
        lines.append(f"- **Volume:** calls {_fmt_int(t['call_volume'])} vs puts {_fmt_int(t['put_volume'])} "
                     f"(put/call {_fmt_ratio(t['put_call_volume_ratio'])}).")
        lines.append(f"  {_pc_volume_meaning(t['put_call_volume_ratio'])}")
        lines.append(f"- **Premium:** calls ~{fmt_money(t['call_premium'])} vs puts ~{fmt_money(t['put_premium'])} "
                     f"(put/call {_fmt_ratio(t['put_call_premium_ratio'])}).")
        lines.append(f"  {_pc_premium_meaning(t['put_call_premium_ratio'])}")
    else:
        lines.append("- **Volume:** no contracts have traded in this session yet, so there is no "
                     "activity to read.")
    lines.append(f"- **Open interest:** calls {_fmt_int(t.get('call_open_interest'))} vs puts "
                 f"{_fmt_int(t.get('put_open_interest'))} (put/call {_fmt_ratio(t.get('put_call_oi_ratio'))}).")
    lines.append(f"  {_pc_oi_meaning(t.get('put_call_oi_ratio'))}")
    if t.get("total_volume"):
        lean = s.get("lean", "mixed")
        lines.append(f"- **Estimated lean: {lean}.** ~{fmt_money(s.get('bullish_premium'))} bullish-looking "
                     f"(calls at ask, puts at bid) vs ~{fmt_money(s.get('bearish_premium'))} bearish-looking "
                     f"(puts at ask, calls at bid); {_fmt_share(s.get('classified_share'))} of premium "
                     "could be called bullish or bearish.")
        lines.append(f"  {LEAN_MEANING.get(lean, '')}")
        if (s.get("classified_share") or 0) < 0.5:
            lines.append("  Less than half the premium could be called bullish or bearish, so treat "
                         "this lean as weak.")
        lines += _time_value_lines(result.get("extrinsic"), t.get("total_premium"))
        lines.append(f"- **Short-dated:** {_fmt_share(sd.get('share'))} of premium is in options expiring "
                     f"within {sd.get('max_dte', SHORT_DTE)} days.")
        lines.append(f"  {_short_meaning(sd.get('share'))}")
    lines += _iv_lines(result.get("volatility") or {})
    lines.append("")

    unusual = result.get("unusual") or []
    lines += ["### Unusual contracts", ""]
    if unusual:
        count = result.get("unusual_count", len(unusual))
        lines.append(f"{count} contract{'s' if count != 1 else ''} met the unusual rule"
                     + (f"; top {len(unusual)} by time value (extrinsic premium)." if count > len(unusual)
                        else ", ranked by time value (extrinsic premium)."))
        lines += ["",
                  "| Contract | Expiry (DTE) | Strike | Type | Volume | OI | Vol/OI | ~Premium | ~Extrinsic | "
                  "Side | IV | Delta | vs spot |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in unusual:
            vol_oi = f"{r['vol_oi']:.1f}x" if r["vol_oi"] is not None else "new (OI 0)"
            dte = "expired" if r["dte"] is not None and r["dte"] < 0 else r["dte"]
            lines.append(f"| {r['contract']} | {r['expiry']} ({dte}) | {_fmt_price(r['strike'])} | "
                         f"{r['type']} | {_fmt_int(r['volume'])} | {_fmt_int(r['open_interest'])} | {vol_oi} | "
                         f"{fmt_money(r['premium'])} | {fmt_money(r.get('extrinsic_premium'))} | "
                         f"{SIDE_LABEL.get(r['side'], r['side'])} | {_fmt_iv(r['iv'])} | {r['delta']:+.2f} | "
                         f"{_fmt_moneyness(r['moneyness_pct'], r['otm'], r.get('deep_itm'))} |")
        lines += ["",
                  DEEP_ITM_LINE,
                  "Side: \"ask\" means the last trade printed near the ask (likely bought), \"bid\" near the "
                  "bid (likely sold), \"mid\" in between, \"stale\" means the last trade is not from this "
                  "session, \"unknown\" means there was no usable quote.",
                  "Volume above open interest tends to mean many of today's trades opened new positions, which "
                  "is why traders watch it. Check tomorrow's open interest: if it rises by about the volume, "
                  "positions were likely opened. Any one contract can also be a hedge or one leg of a spread, "
                  "so treat these as clues, not conclusions."]
    elif t.get("total_volume"):
        lines.append("None today. No contract cleared the volume, volume-above-OI and premium thresholds, "
                     "which tends to mean activity was routine.")
    else:
        lines.append("None (no volume yet).")
    lines.append("")

    pos = result.get("positioning") or {}
    rows = [("call", r) for r in pos.get("calls") or []] + [("put", r) for r in pos.get("puts") or []]
    lines += ["### Positioning (open interest by strike, all live expiries)", ""]
    if rows:
        lines += ["| Type | Strike | Open interest | Today's volume | vs spot | Largest expiry (share) |",
                  "|---|---|---|---|---|---|"]
        for typ, r in rows:
            lines.append(f"| {typ} | {_fmt_price(r['strike'])} | {_fmt_int(r['open_interest'])} | "
                         f"{_fmt_int(r['volume'])} | {_fmt_pct(r['moneyness_pct'])} | "
                         f"{r['top_expiry']} ({_fmt_share(r['top_expiry_share'])}) |")
        lines += ["",
                  "Strikes with large open interest can act like magnets or walls, especially when price "
                  "is near them and their main expiry is close; their pull tends to fade after that expiry."]
    else:
        lines.append("No open interest in live expiries.")
    lines.append("")

    notes = result.get("notes") or []
    if notes:
        lines += ["### Notes", ""] + [f"- {n}" for n in notes] + [""]
    lines.append(_caveat(result))
    return "\n".join(lines) + "\n"

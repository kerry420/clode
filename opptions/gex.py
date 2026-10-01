"""Gamma exposure (GEX): where dealer hedging may dampen or amplify moves.

Source: Cboe's free, 15-minute-delayed option chain (see cboe.py). One
request gives every listed contract with its gamma, IV and open interest.

Method (the common "naive" public GEX estimate):

    GEX per contract = gamma * open_interest * 100 * spot^2 * 0.01

That is the dollar amount of the underlying dealers would need to trade to
stay hedged after a 1% move. Only contracts with dte >= 0 and open
interest > 0 count. The multiplier is 100 for stocks, ETFs and cash-settled
indexes alike; adjusted contracts with digit roots (e.g. "XYZ1") are not
parsed by cboe.py, so they are left out.

Naive dealer-positioning assumption: calls count positive and puts count
negative, which assumes dealers are LONG calls (customers sell calls, as in
covered-call writing) and SHORT puts (customers buy puts as protection).
Real dealer positioning is not public. When customers buy calls heavily the
true call sign flips, so every number here is a rough estimate.

Readings:
  * net GEX > 0: dealers are (estimated) long gamma; their hedging sells
    rallies and buys dips, which tends to dampen moves and pin price.
  * net GEX < 0: dealers are short gamma; hedging buys rallies and sells
    dips, which tends to amplify moves.
  * call wall: strike with the largest call GEX (often acts as resistance).
  * put wall: strike with the most negative put GEX (often acts as support).
  * gamma flip: the hypothetical spot where net GEX crosses zero. It is found
    by sweeping spot from 0.80x to 1.20x and recomputing every contract's
    Black-Scholes gamma from its own IV, with T = max(dte, 0.5) / 365,
    r from env OPPTIONS_RATE (default 0.04) and q = 0. Contracts with no IV
    are skipped in the sweep and counted.

analyze() is pure: it reads no clock and makes no network calls. Days to
expiry come from the chain's own timestamp via cboe.normalize_chain.
"""

import math
import os

from . import cboe

TITLE = "Gamma Exposure (GEX)"
MULTIPLIER = 100
DEFAULT_RATE = 0.04
SWEEP_LOW, SWEEP_HIGH, SWEEP_STEPS = 0.80, 1.20, 120
TOP_N = 5
EXPIRY_ROWS = 6
_SQRT_2PI = math.sqrt(2.0 * math.pi)

REGIME_MEANING = {
    "positive": ("Dealers are estimated to be net long gamma: their hedging tends to "
                 "sell rallies and buy dips, which usually dampens moves and can pin "
                 "price near large strikes."),
    "negative": ("Dealers are estimated to be net short gamma: their hedging tends to "
                 "buy rallies and sell dips, which usually amplifies moves and can make "
                 "swings faster and larger."),
    "neutral": ("Call and put gamma roughly cancel: dealer hedging is not expected to "
                "lean strongly either way."),
    "unknown": ("No usable gamma (empty chain, no open interest, zero gamma or no spot price), "
                "so there is no regime to report."),
}


# ---------------------------------------------------------------- fetch

def fetch(symbol):
    """Raw Cboe chain JSON for one symbol (raises FetchError if Cboe is unreachable)."""
    return cboe.fetch_chain(symbol)


# ---------------------------------------------------------------- math helpers

def rate_from_env():
    """Risk-free rate for the flip sweep: env OPPTIONS_RATE, else 0.04."""
    try:
        r = float(os.environ.get("OPPTIONS_RATE", DEFAULT_RATE))
    except ValueError:
        return DEFAULT_RATE
    return r if math.isfinite(r) else DEFAULT_RATE


def norm_pdf(x):
    """Standard normal density."""
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def bs_gamma(spot, strike, iv, t, r=DEFAULT_RATE, q=0.0):
    """Black-Scholes gamma per share (same for calls and puts). 0.0 if inputs are unusable."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t <= 0:
        return 0.0
    vol_t = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * t) / vol_t
    return math.exp(-q * t) * norm_pdf(d1) / (spot * vol_t)


def contract_gex(gamma, open_interest, spot, opt_type):
    """Signed dollar GEX per 1% move for one contract line: calls +, puts -."""
    sign = 1.0 if opt_type == "C" else -1.0
    return sign * gamma * open_interest * MULTIPLIER * spot * spot * 0.01


def _pct(level, spot):
    if level is None or not spot or spot <= 0:
        return None
    return round((level - spot) / spot * 100.0, 2)


def _usable(o):
    """Contracts that count toward GEX: not expired, with open interest and a real strike."""
    return (o["dte"] >= 0 and o["open_interest"] > 0 and o["strike"] > 0
            and math.isfinite(o["gamma"]) and math.isfinite(o["open_interest"]))


# ---------------------------------------------------------------- gamma flip

def _prepare_sweep(contracts, rate):
    """Precompute per-contract constants for the sweep; returns (prepared, skipped_no_iv)."""
    prepared, skipped = [], 0
    for o in contracts:
        iv = o["iv"]
        if not (iv > 0 and math.isfinite(iv)):
            skipped += 1
            continue
        t = max(o["dte"], 0.5) / 365.0
        vol_t = iv * math.sqrt(t)
        sign = 1.0 if o["type"] == "C" else -1.0
        # gex(x) = sign*OI*100*0.01 * gamma(x) * x^2, gamma(x) = pdf(d1)/(x*vol_t)
        #        = [sign*OI*100*0.01/vol_t] * pdf(d1) * x
        weight = sign * o["open_interest"] * MULTIPLIER * 0.01 / vol_t
        prepared.append((math.log(o["strike"]), (rate + 0.5 * iv * iv) * t, vol_t, weight))
    return prepared, skipped


def _model_net_gex(prepared, level, gross=False):
    """Model net GEX (Black-Scholes gammas) if spot were at `level`; gross=True sums |GEX|."""
    lx = math.log(level)
    acc = 0.0
    for log_k, drift, vol_t, weight in prepared:
        d1 = (lx - log_k + drift) / vol_t
        acc += (abs(weight) if gross else weight) * math.exp(-0.5 * d1 * d1)
    return acc * level / _SQRT_2PI


def _sign(x):
    return None if not x else ("positive" if x > 0 else "negative")


def _bracketed_crossings(levels, totals):
    """[(level, y_low, y_high)]: interpolated zero crossings with the totals on either side."""
    points = [(x, y) for x, y in zip(levels, totals) if y != 0.0]
    out = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if (y0 < 0) != (y1 < 0):
            out.append((x0 - y0 * (x1 - x0) / (y1 - y0), y0, y1))
    return out


def find_zero_crossings(levels, totals):
    """Linearly interpolated levels where totals change sign (exact zeros are stepped over)."""
    return [c for c, _, _ in _bracketed_crossings(levels, totals)]


def gamma_flip(contracts, spot, rate, low=SWEEP_LOW, high=SWEEP_HIGH, steps=SWEEP_STEPS):
    """Sweep hypothetical spot and return the zero-gamma level nearest spot, with sweep details.

    sign_below / sign_above come from the sweep points that bracket the flip, so they
    are right even when another crossing sits close by; sign_at_spot is the model's
    own sign at the current spot.
    """
    prepared, skipped = _prepare_sweep(contracts, rate)
    info = {"low": None, "high": None, "steps": steps, "rate": rate,
            "contracts_used": len(prepared), "skipped_no_iv": skipped,
            "model_net_gex_at_spot": None, "model_gross_gex_at_spot": None,
            "sign_at_spot": None, "crossings": [], "flip": None,
            "sign_below": None, "sign_above": None}
    if not (spot > 0 and math.isfinite(spot)) or not prepared:
        return info
    levels = [spot * (low + (high - low) * i / steps) for i in range(steps + 1)]
    totals = [_model_net_gex(prepared, x) for x in levels]
    crossings = _bracketed_crossings(levels, totals)
    at_spot = _model_net_gex(prepared, spot)
    info.update({
        "low": round(levels[0], 2),
        "high": round(levels[-1], 2),
        "model_net_gex_at_spot": round(at_spot, 2),
        "model_gross_gex_at_spot": round(_model_net_gex(prepared, spot, gross=True), 2),
        "sign_at_spot": _sign(at_spot),
        "crossings": [round(c, 2) for c, _, _ in crossings],
    })
    if crossings:
        flip, y_low, y_high = min(crossings, key=lambda c: abs(c[0] - spot))
        info["flip"] = round(flip, 2)
        info["sign_below"] = _sign(y_low)
        info["sign_above"] = _sign(y_high)
    return info


# ---------------------------------------------------------------- analyze

def _clean_raw(raw):
    """Shallow copy of raw that cboe.normalize_chain can read: non-dict parts become empty."""
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    if not isinstance(data, dict):
        return {**raw, "data": {}}
    options = data.get("options")
    options = [o for o in options if isinstance(o, dict)] if isinstance(options, list) else []
    return {**raw, "data": {**data, "options": options}}


def analyze(raw, rate=None):
    """Pure GEX analysis of a raw Cboe chain. JSON-serializable output; see module docstring."""
    chain = cboe.normalize_chain(_clean_raw(raw))
    spot = chain["spot"] if math.isfinite(chain["spot"]) else 0.0
    rate = rate_from_env() if rate is None else rate
    contracts = [o for o in chain["options"] if _usable(o)]
    notes = []

    by_strike, by_expiry = {}, {}
    total_call = total_put = gross = gross_with_iv = 0.0
    for o in contracts:
        gex = contract_gex(o["gamma"], o["open_interest"], spot, o["type"])
        if o["iv"] > 0 and math.isfinite(o["iv"]):
            gross_with_iv += abs(gex)
        s = by_strike.setdefault(o["strike"], {"call_gex": 0.0, "put_gex": 0.0})
        e = by_expiry.setdefault(o["expiry"], {"dte": o["dte"], "call_gex": 0.0,
                                               "put_gex": 0.0, "gross_gex": 0.0})
        side = "call_gex" if o["type"] == "C" else "put_gex"
        s[side] += gex
        e[side] += gex
        e["gross_gex"] += abs(gex)
        if o["type"] == "C":
            total_call += gex
        else:
            total_put += gex
        gross += abs(gex)
    net = total_call + total_put

    strikes = [{"strike": k, "call_gex": round(v["call_gex"], 2), "put_gex": round(v["put_gex"], 2),
                "net_gex": round(v["call_gex"] + v["put_gex"], 2), "distance_pct": _pct(k, spot)}
               for k, v in sorted(by_strike.items())]
    expiries = [{"expiry": k, "dte": v["dte"], "call_gex": round(v["call_gex"], 2),
                 "put_gex": round(v["put_gex"], 2),
                 "net_gex": round(v["call_gex"] + v["put_gex"], 2),
                 "gross_gex": round(v["gross_gex"], 2),
                 "share_of_gross": round(v["gross_gex"] / gross, 4) if gross > 0 else None}
                for k, v in sorted(by_expiry.items())]

    call_wall = max((s for s in strikes if s["call_gex"] > 0), key=lambda s: s["call_gex"], default=None)
    put_wall = min((s for s in strikes if s["put_gex"] < 0), key=lambda s: s["put_gex"], default=None)
    top = sorted((s for s in strikes if s["net_gex"] != 0), key=lambda s: -abs(s["net_gex"]))[:TOP_N]

    if gross <= 0:
        regime = "unknown"
    elif net > 0:
        regime = "positive"
    elif net < 0:
        regime = "negative"
    else:
        regime = "neutral"

    nearest = expiries[0] if expiries else None
    zero_dte = next((e for e in expiries if e["dte"] == 0), None)

    sweep = gamma_flip(contracts, spot, rate)
    flip = sweep["flip"]

    if not chain["options"]:
        notes.append("Chain is empty: Cboe returned no option contracts for this symbol.")
    elif not contracts:
        notes.append("No contracts with open interest and dte >= 0.")
    if chain["options"] and chain["as_of"] is None:
        notes.append("The chain has no timestamp, so days to expiry were counted from the date "
                     "this ran, not from the data's own date.")
    if spot <= 0:
        notes.append("No usable spot price, so dollar GEX and the flip cannot be computed.")
    elif contracts and gross <= 0:
        notes.append("Every contract reports zero gamma, so Cboe-gamma GEX is zero; "
                     "the model flip below recomputes gamma from IV instead.")
    if chain["spot_source"] not in ("current_price", "unknown"):
        notes.append(f"Spot taken from '{chain['spot_source']}', not a live quote.")
    if sweep["skipped_no_iv"]:
        notes.append(f"{sweep['skipped_no_iv']} contract(s) with no IV were skipped in the flip sweep.")
    if spot > 0 and sweep["contracts_used"] and flip is None:
        notes.append("Model net GEX does not change sign between "
                     f"{SWEEP_LOW:.0%} and {SWEEP_HIGH:.0%} of spot, so there is no flip in range.")
    if len(sweep["crossings"]) > 1:
        levels_txt = ", ".join(f"{c:,.2f}" for c in sweep["crossings"])
        notes.append(f"Model net gamma crosses zero {len(sweep['crossings'])} times in range "
                     f"({levels_txt}); the flip shown is the one nearest spot.")
    model_gross = sweep["model_gross_gex_at_spot"]
    if model_gross and gross_with_iv > 0 and not 0.5 <= model_gross / gross_with_iv <= 2.0:
        notes.append(f"The IV model's gamma at spot ({fmt_money(model_gross)} gross) is far from "
                     f"Cboe's own gammas ({fmt_money(gross_with_iv)} gross), for example from very "
                     "short-dated contracts or odd IV values, so treat the flip with extra caution.")
    if regime in ("positive", "negative") and sweep["sign_at_spot"] not in (None, regime):
        notes.append(f"Cboe's gammas give a {regime} total, but the IV model puts spot on the "
                     f"{sweep['sign_at_spot']} side; the lean is borderline, so the regime is uncertain.")

    def level(row, key):
        return None if row is None else {"strike": row["strike"], "gex": row[key],
                                         "distance_pct": row["distance_pct"]}

    return {
        "tracker": "gex",
        "symbol": chain["symbol"],
        "spot": spot,
        "spot_source": chain["spot_source"],
        "as_of": chain["as_of"],
        "multiplier": MULTIPLIER,
        "contracts_in_chain": len(chain["options"]),
        "contracts_used": len(contracts),
        "total_call_gex": round(total_call, 2),
        "total_put_gex": round(total_put, 2),
        "total_net_gex": round(net, 2),
        "gross_gex": round(gross, 2),
        "net_to_gross_pct": round(net / gross * 100.0, 1) if gross > 0 else None,
        "regime": regime,
        "regime_meaning": REGIME_MEANING[regime],
        "call_wall": level(call_wall, "call_gex"),
        "put_wall": level(put_wall, "put_gex"),
        "gamma_flip": None if flip is None else {
            "level": flip,
            "distance_pct": _pct(flip, spot),
            "spot_side": "at" if abs(spot - flip) < 0.005 else ("above" if spot > flip else "below"),
            "sign_below": sweep["sign_below"],
            "sign_above": sweep["sign_above"],
            "sign_at_spot": sweep["sign_at_spot"],
        },
        "flip_sweep": {k: sweep[k] for k in ("low", "high", "steps", "rate", "contracts_used",
                                             "skipped_no_iv", "model_net_gex_at_spot",
                                             "model_gross_gex_at_spot", "sign_at_spot", "crossings")},
        "top_strikes": top,
        "nearest_expiry": None if nearest is None else {
            "expiry": nearest["expiry"], "dte": nearest["dte"],
            "share_of_gross": nearest["share_of_gross"]},
        "zero_dte_share": (zero_dte["share_of_gross"] if zero_dte else 0.0) if gross > 0 else None,
        "by_expiry": expiries,
        "by_strike": strikes,
        "notes": notes,
    }


# ---------------------------------------------------------------- markdown

def fmt_money(x, signed=False):
    """Dollar shorthand: 1.23e9 -> '$1.23B', 4.56e8 -> '$456M', 7.8e4 -> '$78K'."""
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ("+" if signed and x > 0 else "")
    v = abs(x)
    units = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"), (1.0, ""))
    for i, (scale, unit) in enumerate(units):
        if v >= scale or scale == 1.0:
            n = v / scale
            text = f"{n:.{2 if n < 10 else 1 if n < 100 else 0}f}"
            if float(text) >= 1000 and i > 0:  # 999.96K rounds to 1000K -> 1M
                scale, unit = units[i - 1]
                text = f"{v / scale:.2f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{sign}${text}{unit}"
    return f"{sign}$0"


SPOT_SOURCE_LABEL = {
    "current_price": "last price",
    "close": "today's close",
    "last_trade_price": "last trade",
    "prev_day_close": "previous close",
    "atm_delta_estimate": "estimated from at-the-money options",
    "unknown": "no price found",
}


def _fmt_pct(p):
    return "n/a" if p is None else f"{p:+.2f}%"


def _fmt_price(p):
    return "n/a" if p is None else f"{p:,.2f}"


def _fmt_share(s):
    return "n/a" if s is None else f"{s * 100:.0f}%"


def to_markdown(result):
    """Readable GEX section for a person learning, and for an LLM agent."""
    sym = result.get("symbol") or "?"
    lines = [f"## {TITLE}: {sym}", ""]
    spot = result.get("spot") or 0
    as_of = (result.get("as_of") or "unknown time").replace("T", " ")[:16]
    source = SPOT_SOURCE_LABEL.get(result.get("spot_source"), result.get("spot_source") or "unknown")
    lines.append(f"Spot **{'$' + _fmt_price(spot) if spot else 'n/a'}** ({source}) "
                 f"as of {as_of} (Cboe timestamp, about 15 minutes delayed). "
                 f"{result.get('contracts_used', 0):,} of {result.get('contracts_in_chain', 0):,} "
                 "contracts have open interest and count.")
    lines.append("")

    regime = result.get("regime", "unknown")
    if regime == "unknown":
        lines.append("**Regime: unknown.** " + REGIME_MEANING["unknown"])
        for n in result.get("notes") or []:
            lines.append(f"- {n}")
        gf = result.get("gamma_flip")
        if gf:
            lines.append(f"- Model gamma flip from IV alone: {_fmt_price(gf['level'])} "
                         f"({_fmt_pct(gf['distance_pct'])} from spot); treat it as a rough zone.")
        lines += ["", _caveat(result)]
        return "\n".join(lines) + "\n"

    lines.append(f"**Net GEX: {fmt_money(result['total_net_gex'], signed=True)} per 1% move "
                 f"({regime} gamma regime).** Calls {fmt_money(result['total_call_gex'], signed=True)}, "
                 f"puts {fmt_money(result['total_put_gex'], signed=True)}.")
    lines.append(result.get("regime_meaning", ""))
    ratio = result.get("net_to_gross_pct")
    if ratio is not None and abs(ratio) < 10:
        lines.append(f"Net is only {abs(ratio):.0f}% of gross gamma, so this lean is weak and can "
                     "change with a modest move or the next open-interest update.")
    lines.append("")

    lines += ["### Key levels", "",
              "| Level | Price | vs spot | GEX per 1% | Tends to |",
              "|---|---|---|---|---|"]
    cw, pw, gf = result.get("call_wall"), result.get("put_wall"), result.get("gamma_flip")
    if cw:
        lines.append(f"| Call wall | {_fmt_price(cw['strike'])} | {_fmt_pct(cw['distance_pct'])} | "
                     f"{fmt_money(cw['gex'], signed=True)} | act as resistance or a magnet |")
    else:
        lines.append("| Call wall | n/a | | | |")
    if pw:
        lines.append(f"| Put wall | {_fmt_price(pw['strike'])} | {_fmt_pct(pw['distance_pct'])} | "
                     f"{fmt_money(pw['gex'], signed=True)} | act as support; moves can speed up below it |")
    else:
        lines.append("| Put wall | n/a | | | |")
    sweep = result.get("flip_sweep") or {}
    model_sign = sweep.get("sign_at_spot")
    if gf:
        calm = "above" if gf["sign_above"] == "positive" else "below"
        fast = "below" if calm == "above" else "above"
        lines.append(f"| Gamma flip | {_fmt_price(gf['level'])} | {_fmt_pct(gf['distance_pct'])} | 0 | "
                     f"separate calmer trading ({calm}) from faster moves ({fast}) |")
    elif not sweep.get("contracts_used"):
        lines.append("| Gamma flip | not computed (no IV) | | | |")
    else:
        lines.append("| Gamma flip | none in ±20% | | | |")
    lines.append("")
    if gf and gf["spot_side"] == "at":
        lines.append("Spot is sitting right at the flip, so a small move either way can switch "
                     "hedging from dampening to amplifying.")
    elif gf and model_sign:
        tendency = "dampen" if model_sign == "positive" else "amplify"
        lines.append(f"Spot is {abs(gf['distance_pct'] or 0):.2f}% {gf['spot_side']} the flip, on the side "
                     f"where model net gamma is {model_sign}, so hedging tends to {tendency} moves there.")
    elif not sweep.get("contracts_used"):
        lines.append("No contract had an implied volatility, so the flip could not be modelled; "
                     "there is no estimate of where the regime might change.")
    elif model_sign:
        tendency = "dampening" if model_sign == "positive" else "amplifying"
        lines.append(f"Model net gamma stays {model_sign} from {SWEEP_LOW:.0%} to {SWEEP_HIGH:.0%} of "
                     f"spot, so hedging tends to keep {tendency} moves even after a sizable move.")
    lines.append("")

    top = result.get("top_strikes") or []
    if top:
        lines += ["### Top strikes by |net GEX|", "",
                  "| Strike | vs spot | Call GEX | Put GEX | Net GEX |",
                  "|---|---|---|---|---|"]
        for s in top:
            lines.append(f"| {_fmt_price(s['strike'])} | {_fmt_pct(s['distance_pct'])} | "
                         f"{fmt_money(s['call_gex'], signed=True)} | {fmt_money(s['put_gex'], signed=True)} | "
                         f"{fmt_money(s['net_gex'], signed=True)} |")
        lines += ["", "Large positive strikes tend to slow price down or pin it nearby; large negative "
                      "strikes are where moves tend to accelerate.", ""]

    lines += ["### Expiry concentration", ""]
    ne = result.get("nearest_expiry")
    if ne:
        lines.append(f"- Nearest expiry {ne['expiry']} ({ne['dte']} DTE): "
                     f"{_fmt_share(ne['share_of_gross'])} of gross gamma.")
    lines.append(f"- 0DTE: {_fmt_share(result.get('zero_dte_share'))} of gross gamma.")
    expiries = result.get("by_expiry") or []
    if expiries:
        lines += ["", "| Expiry | DTE | Net GEX | Share of gross |", "|---|---|---|---|"]
        for e in expiries[:EXPIRY_ROWS]:
            lines.append(f"| {e['expiry']} | {e['dte']} | {fmt_money(e['net_gex'], signed=True)} | "
                         f"{_fmt_share(e['share_of_gross'])} |")
        rest = expiries[EXPIRY_ROWS:]
        if rest:
            share = sum(e["share_of_gross"] or 0 for e in rest)
            lines.append(f"| {len(rest)} later expiries | | | {_fmt_share(share)} |")
    lines += ["", "When a big share of gamma expires soon, the levels above tend to lose their pull "
                  "after that expiry and price can move more freely.", ""]

    notes = result.get("notes") or []
    if notes:
        lines += ["### Notes", ""] + [f"- {n}" for n in notes] + [""]

    lines.append("**How to read this:** positive net GEX tends to mean calmer, range-bound trading "
                 "with price drawn toward big strikes; negative net GEX tends to mean larger, faster "
                 "moves. The call wall often behaves like resistance, the put wall like support, and "
                 "the flip marks the rough line between the two regimes. These are tendencies from "
                 "hedging flows, not predictions or trade signals.")
    lines += ["", _caveat(result)]
    return "\n".join(lines) + "\n"


def _caveat(result):
    rate = (result.get("flip_sweep") or {}).get("rate")
    rate_txt = f"{rate * 100:.2f}%" if isinstance(rate, (int, float)) else "4.00%"
    return ("_Caveat: free Cboe data, about 15 minutes delayed; open interest updates once a day, "
            "so today's new positions are not in it. Signs assume dealers are long calls and short "
            "puts (customers sell calls and buy puts); real dealer positioning is not public and can "
            "be the opposite. GEX is dollars of hedging per 1% move. The flip uses a Black-Scholes "
            f"model on each contract's IV (r = {rate_txt}, q = 0), so treat all levels as rough zones._")

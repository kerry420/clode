"""Desk scan: rank a universe of optionable names by unusual flow or GEX setups.

    python3 -m opptions scan flow                       # DEFAULT_UNIVERSE or $OPPTIONS_UNIVERSE
    python3 -m opptions scan gex SPY QQQ NVDA --top 5
    python3 -m opptions scan flow --universe watchlist.md --json

Each symbol's Cboe delayed chain is fetched once in a small thread pool, run
through the tracker's own analyze(), and scored by a pure function. A symbol
that fails (blocked host, bad data, a bug) is recorded under "errors" and the
scan carries on. Only the score rows are kept, not the full chains.

Symbols are upper-cased and de-duplicated; "SPY,QQQ" or "SPY QQQ" is split, and
an item that is not shaped like a ticker is reported as an error, not fetched.

Flow score, 0 to 100 (score_flow). Everything scored is time value (extrinsic
premium = premium minus built-in value; see flow.time_value), because a deep
in-the-money block (|delta| >= 0.90, often a LEAPS stock replacement or a roll)
is mostly built-in value and once ranked first on its total premium alone:
    40  size: unusual extrinsic premium on a log scale, from the unusual-premium
        floor (default $50K) to $50M, capped there
    20  breadth: number of unusual contracts that are not deep in the money,
        log scale, full at 25
    20  concentration: unusual extrinsic premium as a share of the day's total
        extrinsic premium, full at 50%, so a mega cap's routine volume does not
        win on its own
    20  lean: |bullish - bearish| extrinsic premium (deep ITM left out) / the
        day's total extrinsic premium, full at 50%
    The short-dated (7 DTE or less) share is shown as context, not scored.
    Names with no unusual contracts score 0, sort last and are left out of
    "ranked" unless include_unranked=True. When more contracts are unusual than
    the flow tracker lists (top 15 by extrinsic premium), the sums are floors.
    Each row keeps its top few unusual contracts under "contracts", with the
    volume and open interest at scan time, for `opptions confirm` the next
    morning. Contracts expiring on or before the session date (same-day
    expiries in a 4:20 PM scan) are not kept there: they are gone before the
    overnight open interest update, so they could never be confirmed.
    Contracts that had already expired by the chain's own timestamp are dropped
    before flow.analyze (live_contracts): Cboe keeps an expired expiry in the
    chain until the next session, so a pre-market scan would otherwise rank
    names on yesterday's same-day trading in contracts that no longer exist.

GEX score, 0 to 100 (score_gex). Only distances (% from spot) and ratios are
used, never dollar GEX, so a bigger chain does not outrank a smaller one with
the same shape:
    35  flip: from 35 with spot at the gamma flip to 0 at 3% away
        (tag near_flip within 1.5%: the regime could change soon)
    30  negative gamma: |net| / gross in a negative regime, full at 75%
        (tag negative_gamma from 25%: moves tend to be amplified); halved when
        Cboe's gammas and the IV model disagree on the sign at spot
    20  walls: from 20 with spot at the call or put wall to 0 at 2% away
        (tags near_call_wall / near_put_wall within 1%: a pin or test of a level)
    15  expiry: share of gross gamma in the nearest expiry when it is 7 days
        or less away, from 0 at 20% to full at 80% (tag expiry_heavy from 40%:
        levels tend to lose pull after it)
    Names with no usable gamma (regime unknown) are left unranked.
"""

import argparse
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from . import cboe, flow, gex
from .http import FetchError, fetch_json
from .universe import ENV_VAR, load_universe

KINDS = ("flow", "gex")
TRACKERS = {"flow": flow, "gex": gex}
DEFAULT_WORKERS = 6
MAX_WORKERS = 16  # politeness cap on parallel requests to Cboe
DEFAULT_TOP = 10

# Flow weights and full-credit points.
FLOW_W_SIZE, FLOW_W_BREADTH, FLOW_W_CONC, FLOW_W_LEAN = 40, 20, 20, 20
FLOW_SIZE_FULL = 50_000_000
FLOW_COUNT_FULL = 25
FLOW_SHARE_FULL = 0.50
FLOW_LEAN_FULL = 0.50
FLOW_SHORT_HEAVY = 0.60
FLOW_CONTRACTS = 5  # unusual contracts kept per row for the morning confirmation
CONTRACT_FIELDS = ("contract", "type", "strike", "expiry", "dte", "volume", "open_interest",
                   "premium", "extrinsic_premium", "side", "deep_itm")

# GEX weights, distances (in % of spot) and ratios.
GEX_W_FLIP, GEX_W_NEG, GEX_W_WALL, GEX_W_EXPIRY = 35, 30, 20, 15
FLIP_TAG_PCT, FLIP_ZERO_PCT = 1.5, 3.0
WALL_TAG_PCT, WALL_ZERO_PCT = 1.0, 2.0
NEG_TAG_RATIO, NEG_FULL_RATIO = 0.25, 0.75
EXPIRY_MAX_DTE = 7
EXPIRY_TAG_SHARE, EXPIRY_FLOOR_SHARE, EXPIRY_FULL_SHARE = 0.40, 0.20, 0.80

UNRANKED_LABEL = {"flow": "no unusual contracts", "gex": "no usable gamma"}


def fetch_raw(symbol):
    """One Cboe delayed chain for `symbol` (raises FetchError).

    gex.fetch and flow.fetch both return this same chain, but through
    cboe.fetch_chain's per-process cache (32 chains). A scan of ~80 names
    cycles through more than that, so the cache would never hit and would only
    hold large chains in memory; the scan fetches directly instead.
    """
    return fetch_json(cboe.chain_url(symbol))


def live_contracts(raw):
    """(chain, dropped): a shallow copy of a raw Cboe chain without the contracts that had
    already expired by the chain's own timestamp, and how many were dropped.

    Cboe keeps an expired expiry in the chain until the next session (a live pre-market
    SPY run still carried the Sep 30 quarterly, 66% of the session's volume), and the flow
    tracker counts it. Expiry dates use the same timestamp rule as cboe.normalize_chain,
    so "expired" here means dte < 0 there. With no timestamp, or an odd shape, raw is
    returned as is for analyze() to judge. raw itself is never changed.
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    options = data.get("options") if isinstance(data, dict) else None
    as_of = cboe._as_of(raw) if isinstance(options, list) else None
    if as_of is None:
        return raw, 0
    today = as_of.date()
    live = []
    for o in options:
        try:
            expired = cboe.parse_option_symbol(str(o.get("option", "")))["expiry"] < today
        except (AttributeError, ValueError):
            expired = False
        if not expired:
            live.append(o)
    dropped = len(options) - len(live)
    if not dropped:
        return raw, 0
    return {**raw, "data": {**data, "options": live}}, dropped


# ---------------------------------------------------------------- small helpers

def _num(x):
    """A finite float, or None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _closeness(distance_pct, zero_at):
    """1.0 at the level, falling linearly to 0.0 at `zero_at` percent away."""
    if distance_pct is None:
        return 0.0
    return _clip(1.0 - abs(distance_pct) / zero_at)


def _round(x, digits):
    return None if x is None else round(x, digits)


def _plain(v):
    """v, or None for a NaN or infinite float, so rows stay strict JSON."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _money(x):
    return flow.fmt_money(x)


def _share(s):
    return "n/a" if s is None else f"{s * 100:.0f}%"


def _pct(p):
    return "n/a" if p is None else f"{p:+.2f}%"


def _price(p):
    return "n/a" if p is None else f"{p:,.2f}"


def _strike(k):
    return "n/a" if k is None else f"{k:,.2f}".rstrip("0").rstrip(".")


def _sentence(parts):
    text = "; ".join(p for p in parts if p)
    return (text[:1].upper() + text[1:] + ".") if text else ""


# ---------------------------------------------------------------- flow score

def _ext(r):
    """A flow unusual row's extrinsic premium; its premium for rows saved before time value."""
    ext = _num(r.get("extrinsic_premium"))
    if ext is None:
        ext = _num(r.get("premium")) or 0.0
    return ext


def _by_time_value(r):
    return (-_ext(r), -(_num(r.get("premium")) or 0.0), str(r.get("contract")))


def _outlives_session(r, session):
    """Whether an unusual contract still exists after its session, so the next morning's open
    interest can show whether its volume became new positions. A contract expiring on or before
    the session date (same-day 0DTE flow in a 4:20 PM scan) never can, so it is not kept for
    `opptions confirm`. Without a session date, days to expiry (from the chain's timestamp)
    decide: 0 or less is left out."""
    expiry = str(r.get("expiry") or "")[:10]
    if session and expiry:
        return expiry > str(session)[:10]
    dte = _num(r.get("dte"))
    return dte is None or dte >= 1


def score_flow(result):
    """Ranked row for one flow.analyze() result: score 0-100 plus the fields the desk reads.

    Scored on time value (see the module docstring). "unusual_premium", "unusual_share",
    "bullish_premium" and "bearish_premium" still count total premium, as before; "lean"
    and "lean_strength" are now the time-value lean with deep ITM contracts left out.
    """
    listed = sorted((r for r in result.get("unusual") or [] if isinstance(r, dict)), key=_by_time_value)
    count = int(_num(result.get("unusual_count")) or len(listed))
    prem = sum(_num(r.get("premium")) or 0.0 for r in listed)
    ext_prem = sum(_ext(r) for r in listed)
    deep_listed = sum(1 for r in listed if r.get("deep_itm") is True)
    deep_count = _num(result.get("unusual_deep_itm_count"))
    deep_count = deep_listed if deep_count is None else int(deep_count)
    breadth = max(0, count - deep_count)
    partial = count > len(listed)
    totals = result.get("totals") or {}
    total_prem = _num(totals.get("total_premium")) or 0.0
    sent = result.get("sentiment") or {}
    bull = _num(sent.get("bullish_premium")) or 0.0
    bear = _num(sent.get("bearish_premium")) or 0.0
    ex = result.get("extrinsic")
    if isinstance(ex, dict):
        total_ext = _num(ex.get("total_premium")) or 0.0
        ext_bull = _num(ex.get("bullish_premium")) or 0.0
        ext_bear = _num(ex.get("bearish_premium")) or 0.0
        lean = ex.get("lean") or "mixed"
    else:  # a result from before time value: fall back to total premium
        total_ext, ext_bull, ext_bear, lean = total_prem, bull, bear, sent.get("lean") or "mixed"
    share = prem / total_prem if total_prem > 0 else None
    ext_share = ext_prem / total_ext if total_ext > 0 else None
    lean_strength = abs(ext_bull - ext_bear) / total_ext if total_ext > 0 else 0.0
    floor = _num((result.get("thresholds") or {}).get("min_premium")) or flow.DEFAULT_MIN_PREMIUM
    floor = max(floor, 1.0)

    parts = {"size": 0.0, "breadth": 0.0, "concentration": 0.0, "lean": 0.0}
    if count > 0:
        span = math.log10(FLOW_SIZE_FULL / floor) if FLOW_SIZE_FULL > floor else 0.0
        size = _clip(math.log10(max(ext_prem, floor) / floor) / span) if span > 0 else 1.0
        parts = {
            "size": FLOW_W_SIZE * size if ext_prem > 0 else 0.0,
            "breadth": FLOW_W_BREADTH * _clip(math.log1p(breadth) / math.log1p(FLOW_COUNT_FULL)),
            "concentration": FLOW_W_CONC * _clip((ext_share or 0.0) / FLOW_SHARE_FULL),
            "lean": FLOW_W_LEAN * _clip(lean_strength / FLOW_LEAN_FULL),
        }
    top = listed[0] if listed else None
    keep = [r for r in listed if _outlives_session(r, result.get("session_date"))][:FLOW_CONTRACTS]
    row = {
        "symbol": result.get("symbol"),
        "score": round(sum(parts.values()), 1),
        "score_parts": {k: round(v, 1) for k, v in parts.items()},
        "spot": _num(result.get("spot")) or None,
        "as_of": result.get("as_of"),
        "session_date": result.get("session_date"),
        "unusual_premium": round(prem, 2),
        "unusual_extrinsic_premium": round(ext_prem, 2),
        "unusual_premium_partial": partial,
        "unusual_count": count,
        "unusual_deep_itm_count": deep_count,
        "unusual_share": _round(share, 3),
        "unusual_extrinsic_share": _round(ext_share, 3),
        "lean": lean,
        "lean_strength": round(lean_strength, 3),
        "bullish_premium": round(bull, 2),
        "bearish_premium": round(bear, 2),
        "bullish_extrinsic": round(ext_bull, 2),
        "bearish_extrinsic": round(ext_bear, 2),
        "put_call_premium_ratio": _num(totals.get("put_call_premium_ratio")),
        "short_dated_share": _num((result.get("short_dated") or {}).get("share")),
        "top_contract": None if top is None else {
            k: _plain(top.get(k)) for k in ("contract", "type", "strike", "expiry", "dte", "premium",
                                            "extrinsic_premium", "deep_itm", "side", "volume",
                                            "open_interest")},
        "contracts": [{k: _plain(r.get(k)) for k in CONTRACT_FIELDS} for r in keep],
        "total_volume": int(_num(totals.get("total_volume")) or 0),
    }
    row["why"] = flow_why(row)
    return row


def _contract_label(c):
    return f"{_strike(_num(c.get('strike')))} {c.get('type') or '?'} {c.get('expiry') or '?'}"


def _side_text(side):
    return {"ask": "last trade at the ask", "bid": "last trade at the bid",
            "mid": "last trade near the mid"}.get(side, "side unclear")


def flow_why(row):
    """One plain-English line for a flow row."""
    if not row["unusual_count"]:
        if not row.get("total_volume"):
            return "No volume yet in this session, so there is no activity to read."
        return "No contract cleared the unusual rule, so activity looks routine."
    n = row["unusual_count"]
    floor = "at least " if row["unusual_premium_partial"] else ""
    ext_share = row.get("unusual_extrinsic_share")
    parts = [f"{n} unusual contract{'s' if n != 1 else ''} worth {floor}~{_money(row['unusual_premium'])}, "
             f"{floor}~{_money(row.get('unusual_extrinsic_premium'))} of it time value"
             + (f" ({_share(ext_share)} of the day's time value)" if ext_share is not None else "")]
    deep = row.get("unusual_deep_itm_count") or 0
    if deep:
        one = deep == 1
        who = ("it is" if n == 1 else "all are" if deep >= n else "1 of them is" if one
               else f"{deep} of them are")
        parts.append(f"{who} deep in the money, mostly built-in value (often stock replacement or rolls), "
                     f"so {'it is' if one else 'those are'} left out of the contract count and the lean")
    bull, bear = row.get("bullish_extrinsic", 0.0), row.get("bearish_extrinsic", 0.0)
    if row["lean"] in ("bullish", "bearish"):
        parts.append(f"time value leans {row['lean']} (~{_money(bull)} bullish-looking vs "
                     f"~{_money(bear)} bearish-looking)")
    else:
        parts.append(f"no clear lean in time value (~{_money(bull)} bullish-looking vs "
                     f"~{_money(bear)} bearish-looking)")
    short = row["short_dated_share"]
    if short is not None:
        parts.append(f"{_share(short)} of premium expires within {flow.SHORT_DTE} days"
                     + (", which tends to fade fast" if short > FLOW_SHORT_HEAVY else ""))
    top = row["top_contract"]
    if top:
        parts.append(f"largest by time value is the {_contract_label(top)}{_deep_tag(top)} "
                     f"(~{_money(_num(top.get('extrinsic_premium')))} time value of "
                     f"~{_money(_num(top.get('premium')))}, {_side_text(top.get('side'))})")
    return _sentence(parts)


def _deep_tag(c):
    return ", deep ITM" if c.get("deep_itm") is True else ""


# ---------------------------------------------------------------- GEX score

def score_gex(result):
    """Ranked row for one gex.analyze() result: score 0-100, setup tags and key levels."""
    regime = result.get("regime") or "unknown"
    spot = _num(result.get("spot")) or 0.0
    gf = result.get("gamma_flip") or {}
    cw = result.get("call_wall") or {}
    pw = result.get("put_wall") or {}
    ne = result.get("nearest_expiry") or {}
    flip_pct = _num(gf.get("distance_pct"))
    cw_pct, pw_pct = _num(cw.get("distance_pct")), _num(pw.get("distance_pct"))
    ratio_pct = _num(result.get("net_to_gross_pct"))
    ratio = None if ratio_pct is None else ratio_pct / 100.0
    ne_share, ne_dte = _num(ne.get("share_of_gross")), _num(ne.get("dte"))
    sign_at_spot = (result.get("flip_sweep") or {}).get("sign_at_spot")
    uncertain = regime in ("positive", "negative") and sign_at_spot not in (None, regime)
    usable = regime != "unknown" and spot > 0
    expiry_soon = ne_share is not None and ne_dte is not None and 0 <= ne_dte <= EXPIRY_MAX_DTE
    negative = regime == "negative" and ratio is not None

    parts = {"flip": 0.0, "negative_gamma": 0.0, "wall": 0.0, "expiry": 0.0}
    tags = []
    if usable:
        parts = {
            "flip": GEX_W_FLIP * _closeness(flip_pct, FLIP_ZERO_PCT),
            "negative_gamma": (GEX_W_NEG * _clip(abs(ratio) / NEG_FULL_RATIO)
                               * (0.5 if uncertain else 1.0)) if negative else 0.0,
            "wall": GEX_W_WALL * max(_closeness(cw_pct, WALL_ZERO_PCT), _closeness(pw_pct, WALL_ZERO_PCT)),
            "expiry": GEX_W_EXPIRY * _clip((ne_share - EXPIRY_FLOOR_SHARE)
                                           / (EXPIRY_FULL_SHARE - EXPIRY_FLOOR_SHARE))
            if expiry_soon else 0.0,
        }
        if flip_pct is not None and abs(flip_pct) <= FLIP_TAG_PCT:
            tags.append("near_flip")
        if negative and abs(ratio) >= NEG_TAG_RATIO:
            tags.append("negative_gamma")
        if cw_pct is not None and abs(cw_pct) <= WALL_TAG_PCT:
            tags.append("near_call_wall")
        if pw_pct is not None and abs(pw_pct) <= WALL_TAG_PCT:
            tags.append("near_put_wall")
        if expiry_soon and ne_share >= EXPIRY_TAG_SHARE:
            tags.append("expiry_heavy")
    row = {
        "symbol": result.get("symbol"),
        "score": round(sum(parts.values()), 1),
        "score_parts": {k: round(v, 1) for k, v in parts.items()},
        "spot": spot or None,
        "as_of": result.get("as_of"),
        "regime": regime,
        "regime_uncertain": uncertain,
        "net_gex": _num(result.get("total_net_gex")),
        "net_to_gross_pct": ratio_pct,
        "flip": _num(gf.get("level")),
        "flip_pct": flip_pct,
        "call_wall": _num(cw.get("strike")),
        "call_wall_pct": cw_pct,
        "put_wall": _num(pw.get("strike")),
        "put_wall_pct": pw_pct,
        "nearest_expiry": ne.get("expiry"),
        "nearest_expiry_dte": None if ne_dte is None else int(ne_dte),
        "nearest_expiry_share": ne_share,
        "tags": tags,
    }
    row["why"] = gex_why(row)
    return row


def _spot_vs(pct):
    """Where spot sits against a level `pct` percent away: '0.40% above', '0.40% below' or 'at'."""
    if abs(pct) < 0.005:
        return "at"
    return f"{abs(pct):.2f}% {'above' if pct < 0 else 'below'}"


def gex_why(row):
    """One plain-English line for a GEX row."""
    if row["regime"] == "unknown" or not row["spot"]:
        return ("No usable gamma (empty chain, no open interest, zero gamma or no spot price), "
                "so there is no setup to read.")
    tags = row["tags"]
    parts = []
    if "near_flip" in tags:
        move = ("so the regime could change right here" if abs(row["flip_pct"]) < 0.005
                else f"so a move of about {abs(row['flip_pct']):.2f}% would cross it and change the regime")
        parts.append(f"spot is {_spot_vs(row['flip_pct'])} the gamma flip ({_strike(row['flip'])}), {move}")
    if "negative_gamma" in tags:
        parts.append(f"negative gamma with net at {abs(row['net_to_gross_pct']):.0f}% of gross, "
                     "so hedging tends to amplify moves"
                     + (" (borderline: the IV model disagrees on the sign)" if row["regime_uncertain"] else ""))
    if "near_call_wall" in tags:
        cleared = row["call_wall_pct"] <= -0.005
        parts.append(f"spot is {_spot_vs(row['call_wall_pct'])} the call wall ({_strike(row['call_wall'])}), "
                     + ("already through it; a cleared call wall can act as a floor or a magnet"
                        if cleared else "where price tends to stall or pin"))
    if "near_put_wall" in tags:
        broken = row["put_wall_pct"] >= 0.005
        parts.append(f"spot is {_spot_vs(row['put_wall_pct'])} the put wall ({_strike(row['put_wall'])}), "
                     + ("already under it, where moves tend to speed up rather than find support"
                        if broken else "which tends to act as support, with moves speeding up if it breaks"))
    if "expiry_heavy" in tags:
        parts.append(f"{_share(row['nearest_expiry_share'])} of gamma expires {row['nearest_expiry']} "
                     f"({row['nearest_expiry_dte']} DTE), after which these levels tend to lose pull")
    if parts:
        return _sentence(parts)
    tendency = {"positive": "tends to dampen moves", "negative": "tends to amplify moves",
                "neutral": "has no strong lean"}.get(row["regime"], "")
    flip = (f"flip {_pct(row['flip_pct'])} from spot" if row["flip_pct"] is not None
            else "no flip within ±20%")
    return _sentence([f"no setup flagged: {row['regime']} gamma {tendency}, {flip}, "
                      f"call wall {_pct(row['call_wall_pct'])}, put wall {_pct(row['put_wall_pct'])}"])


SCORERS = {"flow": score_flow, "gex": score_gex}


def _rankable(kind, row):
    if kind == "flow":
        return row["unusual_count"] > 0
    return row["regime"] != "unknown" and bool(row["spot"])


def _sort_key(kind, row):
    tiebreak = -row["unusual_extrinsic_premium"] if kind == "flow" else -len(row["tags"])
    return (not _rankable(kind, row), -row["score"], tiebreak, row["symbol"])


# ---------------------------------------------------------------- scan

_SYMBOL = re.compile(r"^[\^_]?[A-Z0-9][A-Z0-9.\-]{0,9}$")


def _clean_symbols(symbols):
    """(symbols, rejected): unique upper-case tickers in order, and the items that are not
    shaped like a ticker. A string, or an item such as "SPY,QQQ", is split on commas and
    whitespace, so scan("flow", "SPY") scans SPY, not S, P and Y."""
    if isinstance(symbols, str):
        symbols = [symbols]
    seen = {}
    for item in symbols:
        for part in re.split(r"[\s,]+", str(item)):
            sym = part.lstrip("$").upper()
            if sym:
                seen.setdefault(sym, None)
    good = [s for s in seen if _SYMBOL.match(s)]
    return good, [s for s in seen if not _SYMBOL.match(s)]


def _error_text(exc):
    if isinstance(exc, FetchError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def scan(kind, symbols, workers=DEFAULT_WORKERS, fetch=None, include_unranked=False):
    """Fetch, analyze and score every symbol concurrently, then rank.

    Returns {"kind", "scanned", "ok", "errors": {symbol: message},
    "ranked": [row, ...] best first, "unranked": [symbols], "as_of" (latest
    chain timestamp seen, ISO), "include_unranked"}. Names that cannot be ranked
    (flow: no unusual contracts; gex: no usable gamma) sort after every other
    name and appear in "ranked" only when include_unranked is true. Flow rows
    also carry "expired_left_out" (see live_contracts). At most MAX_WORKERS
    fetches run at once. The raw chains are only read, never changed.
    """
    if kind not in TRACKERS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    mod, scorer = TRACKERS[kind], SCORERS[kind]
    fetch = fetch or fetch_raw
    symbols, rejected = _clean_symbols(symbols)

    def one(sym):
        raw, dropped = fetch(sym), 0
        if kind == "flow":
            raw, dropped = live_contracts(raw)
        row = scorer(mod.analyze(raw))
        row["symbol"] = sym  # the requested ticker, so rows and errors use the same keys
        if kind == "flow":
            row["expired_left_out"] = dropped
        return row

    rows = []
    errors = {sym: "not shaped like a ticker symbol, so it was not fetched" for sym in rejected}
    if symbols:
        size = max(1, min(int(workers), MAX_WORKERS, len(symbols)))
        with ThreadPoolExecutor(max_workers=size) as pool:
            futures = [(sym, pool.submit(one, sym)) for sym in symbols]
            for sym, fut in futures:
                try:
                    rows.append(fut.result())
                except Exception as e:  # one bad symbol never stops the scan
                    errors[sym] = _error_text(e)
    rows.sort(key=lambda r: _sort_key(kind, r))
    unranked = sorted(r["symbol"] for r in rows if not _rankable(kind, r))
    ranked = rows if include_unranked else [r for r in rows if _rankable(kind, r)]
    return {
        "kind": kind,
        "scanned": len(symbols) + len(rejected),
        "ok": len(rows),
        "errors": dict(sorted(errors.items())),
        "ranked": ranked,
        "unranked": unranked,
        "include_unranked": include_unranked,
        "as_of": max((r["as_of"] for r in rows if r.get("as_of")), default=None),
    }


# ---------------------------------------------------------------- markdown

CAVEAT_COMMON = ("Cboe's free chain is about 15 minutes delayed and open interest updates once a day, "
                 "so today's new positions are not in it.")
CAVEAT_KIND = {
    "flow": ("Flow here is volume-based (each contract's total for the day against open interest), "
             "not sweeps or blocks, and the side comes only from where the last trade printed. "
             "Scores use time value (premium minus built-in value): deep in-the-money options "
             "(|delta| 0.90 or more) are mostly built-in value, often stock replacement or rolls, so "
             "they are left out of the contract count and the lean. "
             "Contracts that had already expired by the chain's timestamp are left out, so totals "
             "can be lower than in the flow tracker's own report. Run `opptions confirm` on the saved "
             "JSON the next morning to see which flagged contracts became new positions."),
    "gex": ("GEX uses a naive sign model (dealers assumed long calls and short puts); real "
            "positioning is not public and can be the opposite, so treat levels as rough zones."),
}
SCORE_LINE = {
    "flow": ("Score 0-100, all in time value: unusual extrinsic premium (log scale, 40), number of "
             "unusual contracts that are not deep in the money (20), unusual share of the day's time "
             "value (20) and how one-sided the time-value lean is (20). Short-dated share is context "
             "only."),
    "gex": ("Score 0-100 from distances and ratios only, not dollar size: spot near the flip (35), "
            "negative gamma strength (30), spot near a wall (20), gamma expiring within "
            f"{EXPIRY_MAX_DTE} days (15)."),
}


def _when(as_of):
    return (as_of or "unknown time").replace("T", " ")[:16]


# FetchError messages end with " for <url>"; the URL names the symbol, which would keep
# identical failures (HTTP 403 for 80 names) from being grouped on one short line.
_URL_TAIL = re.compile(r"\s+for\s+https?://\S+")


def _first_sentence(msg, limit=140):
    text = _URL_TAIL.sub("", " ".join(str(msg).split()))
    head = text.split(". ", 1)[0]
    return head if len(head) <= limit else head[:limit - 3] + "..."


def _errors_line(errors):
    groups = {}
    for sym, msg in errors.items():
        groups.setdefault(_first_sentence(msg), []).append(sym)
    parts = [f"{', '.join(syms)} ({msg})" for msg, syms in groups.items()]
    return f"Errors ({len(errors)}): " + "; ".join(parts) + "."


def _cell(text):
    return str(text).replace("|", "/").replace("\n", " ")


def _flow_table(rows, start):
    lines = ["| # | Symbol | Score | Spot | Unusual time value (premium, n) | Lean | P/C premium | ≤7 DTE | "
             "Top contract | Why |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, start):
        top = r["top_contract"]
        top_txt = (f"{_contract_label(top)}{_deep_tag(top)} (~{_money(_num(top.get('extrinsic_premium')))} "
                   f"time value, {_side_text(top.get('side'))})" if top else "none")
        ge = "≥" if r["unusual_premium_partial"] else ""
        deep = r.get("unusual_deep_itm_count") or 0
        n = f"{r['unusual_count']}" + (f", {deep} deep ITM" if deep else "")
        prem = f"{ge}{_money(r['unusual_extrinsic_premium'])} ({ge}{_money(r['unusual_premium'])}, {n})"
        pc = "n/a" if r["put_call_premium_ratio"] is None else f"{r['put_call_premium_ratio']:.2f}"
        lines.append(f"| {i} | {r['symbol']} | {r['score']:.1f} | {_price(r['spot'])} | "
                     f"{prem} | {r['lean']} | {pc} | "
                     f"{_share(r['short_dated_share'])} | {_cell(top_txt)} | {_cell(r['why'])} |")
    return lines


def _level(price, pct):
    return "n/a" if price is None else f"{_strike(price)} ({_pct(pct)})"


def _gex_table(rows, start):
    lines = ["| # | Symbol | Score | Spot | Regime (net/gross) | Flip (vs spot) | Call wall | Put wall | "
             "Setups | Why |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, start):
        ratio = "" if r["net_to_gross_pct"] is None else f" ({r['net_to_gross_pct']:+.0f}%)"
        regime = r["regime"] + (" borderline" if r["regime_uncertain"] else "") + ratio
        tags = ", ".join(t.replace("_", " ") for t in r["tags"]) or "none"
        lines.append(f"| {i} | {r['symbol']} | {r['score']:.1f} | {_price(r['spot'])} | {regime} | "
                     f"{_level(r['flip'], r['flip_pct'])} | {_level(r['call_wall'], r['call_wall_pct'])} | "
                     f"{_level(r['put_wall'], r['put_wall_pct'])} | {tags} | {_cell(r['why'])} |")
    return lines


def to_markdown(scan_result, top=DEFAULT_TOP):
    """Markdown for a scan: counts, the top rows, unranked names, errors and caveats."""
    kind = scan_result.get("kind", "?")
    ranked = scan_result.get("ranked") or []
    errors = scan_result.get("errors") or {}
    scanned, ok = scan_result.get("scanned", 0), scan_result.get("ok", 0)
    as_of = scan_result.get("as_of")
    lines = [f"## Desk scan: {kind}", ""]
    stamp = (f"Data as of {_when(as_of)} (latest Cboe timestamp; Cboe gives no time zone, and live "
             "runs have matched UTC)." if as_of else "No Cboe timestamp.")
    if kind == "flow":
        dates = {r["symbol"]: r["session_date"][:10] for r in ranked if r.get("session_date")}
        latest, older_label = max(dates.values(), default=None), "Older session than the rest"
        if latest:
            stamp += f" Volume is from the {latest} session."
    else:
        dates = {r["symbol"]: r["as_of"][:10] for r in ranked if r.get("as_of")}
        latest, older_label = (as_of or "")[:10] or None, "Older data than the rest"
    lines.append(f"Scanned {scanned} symbol{'s' if scanned != 1 else ''}: {ok} ok, "
                 f"{len(errors)} error{'s' if len(errors) != 1 else ''}. {stamp}")
    shown = ranked[:top] if top else ranked
    if ranked:
        what = "names" if scan_result.get("include_unranked") else (
            "names with unusual activity" if kind == "flow" else "names with usable gamma")
        lines.append(f"Ranked {len(ranked)} {what}; showing the top {len(shown)}.")
        lines.append("")
        table = _flow_table if kind == "flow" else _gex_table
        lines += table(shown, 1)
        lines += ["", SCORE_LINE.get(kind, "")]
    else:
        lines.append("No names to rank.")
    extra = []
    unranked = scan_result.get("unranked") or []
    if unranked:
        extra.append(f"Not ranked ({UNRANKED_LABEL.get(kind, 'nothing to score')}): {', '.join(unranked)}.")
    older = [f"{sym} ({d})" for sym, d in dates.items() if latest and d < latest]
    if older:
        extra.append(f"{older_label}: {', '.join(older)}.")
    if errors:
        extra.append(_errors_line(errors))
    if extra:
        lines += [""] + extra
    lines += ["", f"_Caveat: {CAVEAT_COMMON} {CAVEAT_KIND.get(kind, '')} Scores rank what stands out "
                  "for a closer look; they describe tendencies, not trade signals._"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def _positive_int(text):
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def main(argv=None):
    """python3 -m opptions scan flow|gex [SYMBOLS...] [--universe FILE] [--top N] [--json]."""
    p = argparse.ArgumentParser(
        prog="opptions scan",
        description="Rank a universe of optionable names by unusual flow or GEX setups.")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("symbols", nargs="*",
                   help="tickers to scan (default: --universe, else $OPPTIONS_UNIVERSE, "
                        "else the built-in list)")
    p.add_argument("--universe", metavar="FILE",
                   help="file with one ticker per line (watchlist.md format works); "
                        "used when no symbols are given")
    p.add_argument("--top", type=_positive_int, default=None, metavar="N",
                   help=f"rows to show (Markdown default {DEFAULT_TOP}; JSON default all)")
    p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    p.add_argument("--workers", type=_positive_int, default=DEFAULT_WORKERS, metavar="N",
                   help=f"parallel fetches (default {DEFAULT_WORKERS}, at most {MAX_WORKERS})")
    args = p.parse_intermixed_args(argv)  # options may come before or after the symbols

    symbols = args.symbols
    if not symbols:
        env = os.environ.get(ENV_VAR, "").strip()
        if args.universe is None and env and not os.path.isfile(os.path.expanduser(env)):
            print(f"[scan {args.kind}] {ENV_VAR}={env} is not a file, so the built-in list is used",
                  file=sys.stderr)
        try:
            symbols = load_universe(args.universe)
        except (OSError, ValueError) as e:
            print(f"[scan {args.kind}] could not read the universe file: {e}", file=sys.stderr)
            return 1
    result = scan(args.kind, symbols, workers=args.workers)
    if not result["scanned"]:
        print(f"[scan {args.kind}] no symbols to scan", file=sys.stderr)
        return 2
    if args.json:
        out = result if args.top is None else {**result, "ranked": result["ranked"][:args.top]}
        print(json.dumps(out, indent=2, default=str))
    else:
        print(to_markdown(result, top=args.top or DEFAULT_TOP))
    errors = result["errors"]
    if errors:
        first = _first_sentence(next(iter(errors.values())))
        scanned = result["scanned"]
        print(f"[scan {args.kind}] {len(errors)} of {scanned} symbol{'s' if scanned != 1 else ''} failed"
              + (f"; first error: {first}" if not result["ok"] else ""), file=sys.stderr)
    return 0 if result["ok"] else 1

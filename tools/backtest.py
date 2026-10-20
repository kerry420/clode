"""Backtest the desk's signals and the Trader's rules, and print what they show.

Engine: backtesting.py (kernc/backtesting.py 0.6), installed for every thread
in /mnt/project-files/opptions/tools/pylib. It fills orders at the next bar's
open, handles stops and targets inside a bar (stop first when both are hit,
gaps fill at the open), and charges commission. Trades run on daily bars of
the underlying; each trade is then priced as a long option with
Black-Scholes so the result reads like what the Trader actually buys.

Data, all free:
- Cboe daily bars (cdn-api.cboe.com charts/historical), stocks and ETFs since
  2004, VIX since 1990, VIX9D since 2011, VIX1D since 2022, VXN and RVX.
- The desk's own board since 2026-10-02: seat Nominations tables, the nightly
  flow scan (scan-flow.json) and the GEX scan (scan-gex.md / scan-gex.json).
- The Trader's log.

    python3 /mnt/project-files/opptions/tools/backtest.py digest   # refresh if stale, print the lessons (Trader runs this)
    python3 /mnt/project-files/opptions/tools/backtest.py run      # force a full refresh
    python3 /mnt/project-files/opptions/tools/backtest.py report   # print the full report

Outputs in /mnt/project-files/opptions/backtest/: lessons.md (short, what the
Trader reads), report.md (every table), results.json, cache/ (daily bars).
"""

import concurrent.futures as cf
import datetime as dt
import json
import math
import pathlib
import re
import sys
import urllib.request
import warnings
import zoneinfo

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "pylib"))
warnings.filterwarnings("ignore")

try:
    import numpy as np
    import pandas as pd
except ImportError:   # a container without them: install once, then go on
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "numpy", "pandas"], check=False)
    import numpy as np
    import pandas as pd

ROOT = pathlib.Path("/mnt/project-files/opptions")
BOARD = ROOT / "desk"
OUT = ROOT / "backtest"
CACHE = OUT / "cache"
LOG = ROOT / "trader" / "log.md"
NY = zoneinfo.ZoneInfo("America/New_York")
SEATS = ("news", "earnings", "flow", "gex")
VOL_INDEX = {"SPY": "_VIX", "QQQ": "_VXN", "IWM": "_RVX"}
RATE = 0.045          # flat risk-free rate for Black-Scholes
OPT_DTE = 30          # calendar days to expiry for the option a swing trade buys
OPT_COST = 0.03       # round-trip spread and slippage, as a share of premium
MIN_N = 15            # fewer trades than this and a verdict says "too few"
HOLD, SL_ATR, TP_ATR = 5, 1.5, 3.0   # swing sim: max days held, stop and target in ATR(14)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------- data

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def bars(sym, fresh_after=None):
    """Daily OHLC for sym from Cboe, cached; refetched when the cache is older than fresh_after."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{sym}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        if fresh_after is None or cached.get("fetched", "") >= fresh_after:
            return _frame(cached["rows"])
    try:
        rows = _get(f"https://cdn-api.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json")["data"]
    except Exception:
        if path.exists():
            return _frame(json.loads(path.read_text())["rows"])
        return None
    rows = [[r["date"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])] for r in rows]
    if not sym.startswith("_") and sym not in VOL_INDEX:
        rows = rows[-800:]          # single names: about three years is plenty
    path.write_text(json.dumps({"fetched": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": rows}))
    return _frame(rows)


def _frame(rows):
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    return df[~df.index.duplicated(keep="last")]


def load_bars(symbols, fresh_after):
    with cf.ThreadPoolExecutor(8) as ex:
        got = dict(zip(symbols, ex.map(lambda s: bars(s, fresh_after), symbols)))
    return {s: d for s, d in got.items() if d is not None and len(d) > 30}


def entry_day(df, after_date, same_day_ok):
    """First trading day in df on/after (same_day_ok) or strictly after after_date."""
    ts = pd.Timestamp(after_date)
    idx = df.index[df.index >= ts] if same_day_ok else df.index[df.index > ts]
    return idx[0] if len(idx) else None


# ---------------------------------------------------------------- board parsing

def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def nominations():
    """Every seat nomination since Oct 2: (date, seat, ticker, lean). Latest section of a file wins."""
    out = {}
    for day in sorted(p for p in BOARD.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)):
        for seat in SEATS:
            f = day / f"{seat}.md"
            if not f.exists():
                continue
            lines = f.read_text(errors="replace").splitlines()
            in_nom, header = False, None
            for line in lines:
                if line.startswith("#"):
                    in_nom = "nomination" in line.lower()
                    header = None
                    continue
                if not in_nom or not line.startswith("|"):
                    continue
                cells = _cells(line)
                if header is None:
                    header = [c.lower() for c in cells]
                    continue
                if set("".join(cells)) <= set("-: "):
                    continue
                if "ticker" not in header or "lean" not in header:
                    continue
                row = dict(zip(header, cells))
                tick = re.sub(r"[^A-Z.]", "", row.get("ticker", "").split()[0] if row.get("ticker") else "")
                lean = _lean(row.get("lean", ""))
                text = line.lower()
                if not tick or not lean or "withdrawn" in text or "dropped" in text:
                    continue
                out[(day.name, seat, tick)] = lean
    return [(d, s, t, l) for (d, s, t), l in out.items()]


def _lean(text):
    t = text.lower()
    if "either" in t or "big move" in t or "straddle" in t:
        return "move"
    if "bull" in t:
        return "bullish"
    if "bear" in t:
        return "bearish"
    return None


def flow_scans():
    """Nightly flow scan leans: (session_date, ticker, lean, strength, score)."""
    rows = []
    for f in sorted(BOARD.glob("*/scan-flow.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        for r in d.get("ranked", []):
            if r.get("lean") in ("bullish", "bearish") and r.get("session_date"):
                rows.append((r["session_date"], r["symbol"], r["lean"],
                             float(r.get("lean_strength") or 0), float(r.get("score") or 0)))
    return rows


def _num(text):
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group()) if m else None


def gex_scans():
    """GEX scan rows: (as_of datetime str, ticker, spot, net%, flip, call_wall, put_wall)."""
    rows = []
    for day in sorted(p for p in BOARD.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)):
        js, md = day / "scan-gex.json", day / "scan-gex.md"
        if js.exists():
            try:
                d = json.loads(js.read_text())
                for r in d.get("ranked", []):
                    rows.append((r.get("as_of") or d.get("as_of"), r["symbol"], r.get("spot"),
                                 r.get("net_to_gross_pct"), r.get("flip"),
                                 r.get("call_wall"), r.get("put_wall")))
                continue
            except (ValueError, KeyError):
                pass
        if not md.exists():
            continue
        text = md.read_text(errors="replace")
        m = re.search(r"Data as of (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})", text)
        if not m:
            continue
        as_of = f"{m.group(1)}T{m.group(2)}:{m.group(3)}"
        header = None
        for line in text.splitlines():
            if not line.startswith("|"):
                header = None
                continue
            cells = _cells(line)
            if header is None:
                header = [c.lower() for c in cells]
                continue
            if set("".join(cells)) <= set("-: ") or "symbol" not in header:
                continue
            row = dict(zip(header, cells))
            reg = row.get("regime (net/gross)", "")
            net = _num(reg.split("(")[-1]) if "(" in reg else None
            rows.append((as_of, row["symbol"], _num(row.get("spot")), net,
                         _num(row.get("flip (vs spot)")), _num(row.get("call wall")),
                         _num(row.get("put wall"))))
    return rows


def trader_log():
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        if not re.match(r"\|\s*\d{4}-\d{2}-\d{2}", line):
            continue
        c = _cells(line)
        if len(c) >= 9 and c[1].lower() != "none":
            out.append({"date": c[0], "symbol": c[1], "structure": c[2], "status": c[7].lower(), "result": _num(c[8])})
    return out


# ---------------------------------------------------------------- pricing

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S, K, T, iv, call):
    if T <= 1e-6 or iv <= 1e-6:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (RATE + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if call:
        return S * _ncdf(d1) - K * math.exp(-RATE * T) * _ncdf(d2)
    return K * math.exp(-RATE * T) * _ncdf(-d2) - S * _ncdf(-d1)


def iv_series(sym, df, vols):
    """Implied-vol proxy per day: the Cboe vol index for SPY/QQQ/IWM, else 20-day realized x 1.15."""
    vi = VOL_INDEX.get(sym)
    if vi and vi in vols:
        return (vols[vi]["Close"] / 100).reindex(df.index).ffill()
    rv = np.log(df["Close"]).diff().rolling(20).std() * math.sqrt(252)
    return (rv * 1.15).clip(lower=0.12).bfill()


def option_return(lean, S0, S1, days_held, iv0, iv1):
    """Return of a long ATM option (call, put, or straddle for 'move') bought at S0, sold at S1."""
    T0, T1 = OPT_DTE / 365, max(OPT_DTE - days_held * 7 / 5, 0) / 365
    legs = {"bullish": [True], "bearish": [False], "move": [True, False]}[lean]
    p0 = sum(bs(S0, S0, T0, iv0, c) for c in legs)
    p1 = sum(bs(S1, S0, T1, iv1, c) for c in legs)
    return p1 / p0 - 1 - OPT_COST if p0 > 0 else None


# ---------------------------------------------------------------- engine

def simulate(signals, data, vols, hold=HOLD, sl_atr=SL_ATR, tp_atr=TP_ATR):
    """Run each symbol's signals through backtesting.py; return one row per trade.

    signals: list of (entry_ts, symbol, lean). The order is placed on the bar
    before entry_ts so backtesting.py fills it at entry_ts's open.
    """
    from backtesting import Backtest, Strategy

    by_sym = {}
    for ts, sym, lean in signals:
        by_sym.setdefault(sym, []).append((ts, lean))
    trades = []
    for sym, sigs in by_sym.items():
        df = data.get(sym)
        if df is None:
            continue
        start = min(t for t, _ in sigs) - pd.Timedelta(days=60)
        df = df[df.index >= start].copy()
        if len(df) < 25:
            continue
        sig = pd.Series(0, index=df.index, dtype=float)
        moves = {}
        for ts, lean in sigs:
            if ts not in df.index:
                continue
            i = df.index.get_loc(ts)
            if i == 0:
                continue
            sig.iloc[i - 1] = 1 if lean == "bullish" else -1 if lean == "bearish" else 2
            moves[df.index[i - 1]] = lean
        df["Signal"] = sig
        tr = (df["High"] - df["Low"]).combine(abs(df["High"] - df["Close"].shift()), max)
        tr = tr.combine(abs(df["Low"] - df["Close"].shift()), max)
        df["ATR"] = tr.rolling(14, min_periods=5).mean().bfill()

        class Sig(Strategy):
            def init(self):
                pass

            def next(self):
                for t in self.trades:
                    if len(self.data) - 1 - t.entry_bar >= hold:
                        t.close()
                s, a, c = self.data.Signal[-1], self.data.ATR[-1], self.data.Close[-1]
                if self.position or s == 0 or not a:
                    return
                if s == 1:
                    self.buy(size=0.5, sl=c - sl_atr * a, tp=c + tp_atr * a)
                elif s == -1:
                    self.sell(size=0.5, sl=c + sl_atr * a, tp=c - tp_atr * a)
                else:   # big move either way: priced as a straddle, held to the time stop
                    self.buy(size=0.5)

        bt = Backtest(df, Sig, cash=1_000_000, commission=0.0005, finalize_trades=True)
        st = bt.run()
        iv = iv_series(sym, data[sym], vols)
        for _, t in st["_trades"].iterrows():
            et, xt = t["EntryTime"], t["ExitTime"]
            lean = {1: "bullish", -1: "bearish"}.get(int(np.sign(t["Size"])), "bullish")
            sig_day = df.index[df.index.get_loc(et) - 1]
            if moves.get(sig_day) == "move":
                lean = "move"
            days = int(df.index.get_loc(xt) - df.index.get_loc(et))
            S0, S1 = float(t["EntryPrice"]), float(t["ExitPrice"])
            ret = (S1 / S0 - 1) * (1 if lean != "bearish" else -1)
            if lean == "move":
                ret = abs(S1 / S0 - 1)
            trades.append({
                "symbol": sym, "lean": lean, "entry": et.date().isoformat(), "exit": xt.date().isoformat(),
                "days": days, "underlying_ret": ret,
                "option_ret": option_return(lean, S0, S1, days, float(iv.get(et, 0.3)), float(iv.get(xt, 0.3))),
                "open": xt >= df.index[-1] and days < hold,
            })
    return trades


def summarize(trades):
    closed = [t for t in trades if not t["open"]]
    if not closed:
        return {"n": 0}
    u = [t["underlying_ret"] for t in closed if t["lean"] != "move"]
    o = [t["option_ret"] for t in closed if t["option_ret"] is not None]
    return {
        "n": len(closed),
        "open": len(trades) - len(closed),
        "win_rate": float(np.mean([r > 0 for r in o])) if o else None,
        "avg_option_ret": float(np.mean(o)) if o else None,
        "median_option_ret": float(np.median(o)) if o else None,
        "avg_underlying_ret": float(np.mean(u)) if u else None,
        "avg_days": float(np.mean([t["days"] for t in closed])),
    }


def verdict(s):
    if not s.get("n") or s["n"] < MIN_N or s.get("avg_option_ret") is None:
        return "too few"
    if s["avg_option_ret"] > 0.05 and s["win_rate"] >= 0.45:
        return "works"
    if s["avg_option_ret"] < -0.05 and s["win_rate"] < 0.45:
        return "fails"
    return "no edge"


# ---------------------------------------------------------------- studies

def study_seats(data, vols):
    noms = nominations()
    groups = {}
    for d, seat, tick, lean in noms:
        df = data.get(tick)
        if df is None:
            continue
        ts = entry_day(df, d, same_day_ok=True)
        if ts is None:
            continue
        groups.setdefault(seat, []).append((ts, tick, lean))
        groups.setdefault("all seats", []).append((ts, tick, lean))
        groups.setdefault(f"lean: {lean}", []).append((ts, tick, lean))
    # names two or more seats nominated on the same day
    seen = {}
    for d, seat, tick, lean in noms:
        seen.setdefault((d, tick, lean), set()).add(seat)
    for (d, tick, lean), seats in seen.items():
        df = data.get(tick)
        if len(seats) >= 2 and df is not None:
            ts = entry_day(df, d, True)
            if ts is not None:
                groups.setdefault("2+ seats agree", []).append((ts, tick, lean))
    out = {}
    for g, sigs in groups.items():
        tr = simulate(sigs, data, vols)
        out[g] = {**summarize(tr), "trades": tr}
    return out, len(noms)


def study_flow(data, vols):
    rows = flow_scans()
    groups = {}
    for sd, sym, lean, strength, score in rows:
        df = data.get(sym)
        if df is None:
            continue
        ts = entry_day(df, sd, same_day_ok=False)
        if ts is None:
            continue
        b = "strong (lean >= 0.5)" if strength >= 0.5 else "medium (0.3-0.5)" if strength >= 0.3 else "weak (< 0.3)"
        groups.setdefault(f"flow lean {b}", []).append((ts, sym, lean))
        if score >= 60 and strength >= 0.3:
            groups.setdefault("flow top score >= 60, lean >= 0.3", []).append((ts, sym, lean))
    out = {}
    for g, sigs in groups.items():
        tr = simulate(sigs, data, vols)
        out[g] = {**summarize(tr), "trades": tr}
    return out, len(rows)


def study_gex(data):
    """Next-day behaviour by gamma regime and wall position, from the recorded GEX scans."""
    rows = gex_scans()
    obs = []
    for as_of, sym, spot, net, flip, cw, pw in rows:
        df = data.get(sym)
        if df is None or net is None or not spot:
            continue
        d, hh = as_of[:10], int(as_of[11:13] or 0)
        ts = entry_day(df, d, same_day_ok=hh < 13)   # Cboe stamps look like UTC; before 13:00 is pre-open
        if ts is None:
            continue
        i = df.index.get_loc(ts)
        if i == 0:
            continue
        prev = df["Close"].iloc[i - 1]
        bar = df.iloc[i]
        o = {"sym": sym, "date": ts.date().isoformat(), "neg": net < 0,
             "range": (bar["High"] - bar["Low"]) / prev, "absret": abs(bar["Close"] / prev - 1)}
        # wall tests: did price that reached a wall close beyond it?
        if cw and bar["High"] >= cw > prev:
            o["call_wall_touch"] = True
            o["call_wall_broke"] = bar["Close"] > cw
        if pw and bar["Low"] <= pw < prev:
            o["put_wall_touch"] = True
            o["put_wall_broke"] = bar["Close"] < pw
        obs.append(o)
    out = {}
    for name, neg in (("negative gamma", True), ("positive gamma", False)):
        g = [o for o in obs if o["neg"] == neg]
        if not g:
            continue
        cw = [o for o in g if o.get("call_wall_touch")]
        pw = [o for o in g if o.get("put_wall_touch")]
        out[name] = {
            "n": len(g),
            "avg_range": float(np.mean([o["range"] for o in g])),
            "avg_absret": float(np.mean([o["absret"] for o in g])),
            "call_wall_touches": len(cw),
            "call_wall_break_rate": float(np.mean([o["call_wall_broke"] for o in cw])) if cw else None,
            "put_wall_touches": len(pw),
            "put_wall_break_rate": float(np.mean([o["put_wall_broke"] for o in pw])) if pw else None,
        }
    return out, len(rows)


def study_gaps(data, vols):
    """Do opening gaps keep going or fade by the close? SPY, QQQ, IWM since 2004/2009."""
    out = {}
    vix = vols.get("_VIX")
    for sym in ("SPY", "QQQ", "IWM"):
        df = data.get(sym)
        if df is None:
            continue
        gap = df["Open"] / df["Close"].shift() - 1
        oc = df["Close"] / df["Open"] - 1
        v = vix["Close"].shift().reindex(df.index) if vix is not None else pd.Series(np.nan, index=df.index)
        for label, mask in (("up >= 0.5%", gap >= 0.005), ("up >= 1%", gap >= 0.01),
                            ("down <= -0.5%", gap <= -0.005), ("down <= -1%", gap <= -0.01)):
            for vl, vm in (("all", v.notna() | v.isna()), ("VIX >= 20", v >= 20), ("VIX < 20", v < 20)):
                m = mask & vm
                if m.sum() < 10:
                    continue
                direction = 1 if label.startswith("up") else -1
                cont = (oc[m] * direction) > 0
                recent = m & (df.index >= df.index[-1] - pd.Timedelta(days=365 * 2))
                out[f"{sym} gap {label}, {vl}"] = {
                    "n": int(m.sum()),
                    "continue_rate": float(cont.mean()),
                    "avg_open_to_close_with_gap": float((oc[m] * direction).mean()),
                    "n_last_2y": int(recent.sum()),
                    "continue_rate_last_2y": float(((oc[recent] * direction) > 0).mean()) if recent.sum() else None,
                }
    return out


def study_straddles(data, vols):
    """Long SPY straddles priced off Cboe's own implied vol: 1-day (VIX1D) and 7-day (VIX9D)."""
    out = {}
    spy = data.get("SPY")
    if spy is None:
        return out
    if "_VIX1D" in vols:
        v1 = vols["_VIX1D"]["Open"].reindex(spy.index) / 100
        oc = (spy["Close"] / spy["Open"] - 1).abs()
        implied = v1 * math.sqrt(1 / 252) * math.sqrt(2 / math.pi) * 1.0   # expected |move| ~ 0.8 sigma
        priced = v1 * math.sqrt(1 / 252) * 0.8
        m = v1.notna() & (v1 > 0.03)
        ret = (oc[m] / priced[m]) - 1 - 0.05
        out["SPY 0DTE straddle bought at the open (VIX1D)"] = {
            "n": int(m.sum()), "win_rate": float((ret > 0).mean()), "avg_ret": float(ret.mean()),
            "realized_over_implied": float((oc[m] / implied[m]).median())}
    if "_VIX9D" in vols and "_VIX" in vols:
        v9 = vols["_VIX9D"]["Close"].reindex(spy.index) / 100
        v30 = vols["_VIX"]["Close"].reindex(spy.index) / 100
        S0, S1 = spy["Close"], spy["Close"].shift(-6)
        rows = []
        for ts in spy.index[spy.index >= pd.Timestamp("2011-01-10")][:-6]:
            iv0, s0, s1 = v9.get(ts), S0[ts], S1[ts]
            if not iv0 or np.isnan(iv0) or np.isnan(s1):
                continue
            p0 = bs(s0, s0, 9 / 365, iv0, True) + bs(s0, s0, 9 / 365, iv0, False)
            p1 = abs(s1 - s0) + 0.0
            rows.append((ts, p1 / p0 - 1 - 0.03, iv0 / v30.get(ts, iv0)))
        r = pd.DataFrame(rows, columns=["ts", "ret", "ratio"]).set_index("ts")
        for lbl, m in (("all", r["ratio"] > 0), ("front IV high (VIX9D/VIX >= 1)", r["ratio"] >= 1),
                       ("front IV low (VIX9D/VIX < 0.9)", r["ratio"] < 0.9)):
            g = r[m]
            out[f"SPY 1-week straddle held to expiry, {lbl}"] = {
                "n": len(g), "win_rate": float((g["ret"] > 0).mean()), "avg_ret": float(g["ret"].mean())}
    return out


def study_spreads(data, vols):
    """Long ATM option vs ATM/+3% debit spread on SPY, 21 DTE held 10 days, by IV level."""
    spy, vix = data.get("SPY"), vols.get("_VIX")
    if spy is None or vix is None:
        return {}
    iv = (vix["Close"] / 100).reindex(spy.index).ffill()
    rank = iv.rolling(252, min_periods=200).rank(pct=True)
    res = {}
    idx = spy.index[spy.index >= pd.Timestamp("2008-01-01")]
    for k, ts in enumerate(idx[:-10]):
        if k % 2:
            continue                     # every other day keeps the run quick
        s0, s1 = spy["Close"][ts], spy["Close"][idx[k + 10]]
        i0, i1, rk = iv[ts], iv[idx[k + 10]], rank.get(ts)
        if np.isnan(i0) or rk is None or np.isnan(rk):
            continue
        bucket = "IV rank >= 70%" if rk >= 0.7 else "IV rank <= 30%" if rk <= 0.3 else "IV rank 30-70%"
        T0, T1 = 21 / 365, 7 / 365
        for side, call in (("call", True), ("put", False)):
            k2 = s0 * (1.03 if call else 0.97)
            long0, long1 = bs(s0, s0, T0, i0, call), bs(s1, s0, T1, i1, call)
            sp0 = long0 - bs(s0, k2, T0, i0, call)
            sp1 = long1 - bs(s1, k2, T1, i1, call)
            right = (s1 > s0) == call
            for struct, a, b, cost in (("long", long0, long1, 0.02), ("spread", sp0, sp1, 0.04)):
                key = (side, struct, bucket, right)
                res.setdefault(key, []).append(b / a - 1 - cost)
    out = {}
    for (side, struct, bucket, right), v in sorted(res.items()):
        out[f"SPY {side} {struct}, {bucket}, {'direction right' if right else 'direction wrong'}"] = {
            "n": len(v), "avg_ret": float(np.mean(v)), "win_rate": float(np.mean([x > 0 for x in v]))}
    return out


# ---------------------------------------------------------------- run, lessons

def board_stamp():
    days = sorted(p.name for p in BOARD.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))
    newest = max((f.stat().st_mtime for d in days[-2:] for f in (BOARD / d).iterdir()), default=0)
    return f"{days[-1] if days else ''}@{int(newest)}|log@{int(LOG.stat().st_mtime) if LOG.exists() else 0}"


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(NY)
    # bars are fresh if fetched since 6 AM ET today (Cboe posts the prior session by then)
    fresh_after = now.replace(hour=6, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).isoformat()
    names = {t for _, _, t, _ in nominations()} | {s for _, s, *_ in flow_scans()} | {r[1] for r in gex_scans()}
    names |= {"SPY", "QQQ", "IWM"}
    vols = load_bars(["_VIX", "_VIX9D", "_VIX1D", "_VXN", "_RVX"], fresh_after)
    data = load_bars(sorted(names), fresh_after)
    seats, n_noms = study_seats(data, vols)
    flow, n_flow = study_flow(data, vols)
    gex, n_gex = study_gex(data)
    long_hist = {"gaps": study_gaps(data, vols), "straddles": study_straddles(data, vols),
                 "spreads": study_spreads(data, vols)}
    log = trader_log()
    res = {"generated": now.isoformat(timespec="minutes"), "stamp": board_stamp(),
           "inputs": {"nominations": n_noms, "flow_scan_rows": n_flow, "gex_scan_rows": n_gex,
                      "symbols": len(data), "trader_calls": len(log)},
           "seats": seats, "flow": flow, "gex": gex, **long_hist, "trader": log}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / "report.md").write_text(report_md(res))
    (OUT / "lessons.md").write_text(lessons_md(res))
    return res


def _p(x, signed=True):
    return "n/a" if x is None else (f"{x * 100:+.1f}%" if signed else f"{x * 100:.0f}%")


def _need(right, wrong):
    """Win rate a structure needs to break even, from its average win and loss."""
    if not right or not wrong or right["avg_ret"] <= 0:
        return None
    return -wrong["avg_ret"] / (right["avg_ret"] - wrong["avg_ret"])


def needed_rates(sp):
    out = {}
    for side in ("call", "put"):
        for struct in ("long", "spread"):
            for b in ("IV rank >= 70%", "IV rank 30-70%", "IV rank <= 30%"):
                k = f"SPY {side} {struct}, {b}, direction "
                out[(side, struct, b)] = _need(sp.get(k + "right"), sp.get(k + "wrong"))
    return out


def lessons_md(r):
    """Short read for the Trader: the scoreboard, then rules that follow from it (they update with the data)."""
    i = r["inputs"]
    L = [f"# Backtest lessons ({r['generated'][:16].replace('T', ' ')} ET)", "",
         f"Desk data since 2026-10-02 ({i['nominations']} seat nominations, {i['flow_scan_rows']} flow-scan leans, "
         f"{i['gex_scan_rows']} GEX rows), swing sim: buy at the open, stop {SL_ATR} ATR, target {TP_ATR} ATR, out after "
         f"{HOLD} days, priced as a long ATM {OPT_DTE}-day option (modelled). Verdicts need {MIN_N}+ closed trades.", "",
         "| Signal | Trades | Win | Avg option | Verdict |", "|---|---|---|---|---|"]
    rows = [(g, s) for sect in ("seats", "flow") for g, s in r[sect].items() if s.get("n")]
    for g, s in sorted(rows, key=lambda kv: -kv[1]["n"]):
        L.append(f"| {g} | {s['n']} | {_p(s['win_rate'], False)} | {_p(s['avg_option_ret'])} | {verdict(s)} |")
    L += ["", "Rules from the backtest (apply them; they change as results come in):"]
    seats = r["seats"]
    allv = verdict(seats.get("all seats", {}))
    winning = [g for g in SEATS if verdict(seats.get(g, {})) == "works"]
    failing = [g for g in SEATS if verdict(seats.get(g, {})) == "fails"]
    if winning:
        L.append(f"- Seat nominations that work: {', '.join(winning)}. A nomination from them can be the main reason for a trade.")
    if allv == "fails" or failing:
        a = seats.get("all seats", {})
        L.append(f"- Seat nominations lose as swing trades ({a.get('n')} trades, {_p(a.get('win_rate'), False)} won, "
                 f"avg {_p(a.get('avg_option_ret'))}{'; worst: ' + ', '.join(failing) if failing else ''}). "
                 "Never trade a nomination on its own: it needs a GEX or price trigger on the day.")
    fl = {g: s for g, s in r["flow"].items() if s.get("n")}
    bad = [g.replace("flow lean ", "") for g, s in fl.items() if verdict(s) == "fails"]
    good = [g.replace("flow lean ", "") for g, s in fl.items() if verdict(s) == "works"]
    if good:
        L.append(f"- Flow scan leans that work: {', '.join(good)}. Count them as support.")
    if bad:
        L.append(f"- Flow scan leans that lose: {', '.join(bad)}. Give them no weight.")
    g = r["gex"]
    neg, pos = g.get("negative gamma"), g.get("positive gamma")
    if neg and pos and neg.get("call_wall_break_rate") is not None and pos.get("call_wall_break_rate") is not None:
        nb = (neg["call_wall_break_rate"] * neg["call_wall_touches"] + (neg["put_wall_break_rate"] or 0) * neg["put_wall_touches"]) / max(1, neg["call_wall_touches"] + neg["put_wall_touches"])
        pb = (pos["call_wall_break_rate"] * pos["call_wall_touches"] + (pos["put_wall_break_rate"] or 0) * pos["put_wall_touches"]) / max(1, pos["call_wall_touches"] + pos["put_wall_touches"])
        nt, pt = neg["call_wall_touches"] + neg["put_wall_touches"], pos["call_wall_touches"] + pos["put_wall_touches"]
        L.append(f"- GEX walls: in negative gamma a wall that is reached closes broken {_p(nb, False)} of the time ({nt} touches), "
                 f"in positive gamma {_p(pb, False)} ({pt}). Next-day range {neg['avg_range'] * 100:.2f}% vs {pos['avg_range'] * 100:.2f}%. "
                 + ("Trade breaks in negative gamma; in positive gamma a wall is a coin flip, not a fade." if nb >= 0.6 and pb <= 0.55
                    else "The regime rule is not confirmed yet; weigh it lightly."))
    gp = r["gaps"]
    for sym in ("SPY", "QQQ"):
        up, dn = gp.get(f"{sym} gap up >= 0.5%, all"), gp.get(f"{sym} gap down <= -0.5%, all")
        if up and dn:
            u2, d2 = up["continue_rate_last_2y"], dn["continue_rate_last_2y"]
            L.append(f"- {sym} 0.5%+ gaps kept going to the close {_p(u2, False)} (up) and {_p(d2, False)} (down) over the last 2 years "
                     f"({_p(up['continue_rate'], False)} / {_p(dn['continue_rate'], False)} since 2004). "
                     + ("Never chase a gap at the open; enter only on a trigger after 10:00 ET." if max(u2 or 0, d2 or 0) < 0.55 else "Gaps in this direction tend to run."))
    st = r["straddles"]
    z = st.get("SPY 0DTE straddle bought at the open (VIX1D)")
    if z:
        L.append(f"- SPY 0DTE straddles bought at the open won {_p(z['win_rate'], False)} (avg {_p(z['avg_ret'])}, n={z['n']}): "
                 + ("don't buy same-day straddles." if z["avg_ret"] < 0 else "they have paid."))
    hi = st.get("SPY 1-week straddle held to expiry, front IV high (VIX9D/VIX >= 1)")
    if hi:
        L.append(f"- SPY 1-week straddles held to expiry averaged {_p(hi['avg_ret'])} even when front IV was high; a 'big move' trade needs "
                 "a dated catalyst the options are not pricing, not just negative gamma.")
    need = needed_rates(r["spreads"])
    parts = []
    for side in ("call", "put"):
        lo, sp = need.get((side, "long", "IV rank >= 70%")), need.get((side, "spread", "IV rank >= 70%"))
        lo2, sp2 = need.get((side, "long", "IV rank <= 30%")), need.get((side, "spread", "IV rank <= 30%"))
        if None not in (lo, sp, lo2, sp2):
            parts.append(f"{side}s: long needs {_p(lo, False)} wins, spread {_p(sp, False)} in high IV ({_p(lo2, False)} / {_p(sp2, False)} in low IV)")
    if parts:
        L.append("- Break-even win rate on SPY, 10-day hold: " + "; ".join(parts) + ". Use the structure that needs the lower rate. "
                 "The model has no put skew, so real put spreads do a little better than shown.")
    t = r["trader"]
    done = [x for x in t if x["result"] is not None]
    if done:
        w = sum(1 for x in done if x["result"] > 0)
        tags = {}
        for x in done:
            tags.setdefault(x["structure"].split(":")[0].strip().lower() if ":" in x["structure"] else "untagged", []).append(x["result"])
        by = "; ".join(f"{k} {len(v)} ({sum(1 for y in v if y > 0)} won, avg {sum(v) / len(v):+.0f}%)" for k, v in sorted(tags.items()))
        L.append(f"- Your own calls: {len(done)} closed, {w} won, avg {sum(x['result'] for x in done) / len(done):+.1f}%. By setup: {by}.")
    else:
        L.append(f"- Your own calls: {len(t)} logged, none closed yet.")
    L += ["", "Full tables: /mnt/project-files/opptions/backtest/report.md"]
    return "\n".join(L) + "\n"


def report_md(r):
    L = [f"# Backtest report ({r['generated']})", "",
         "Engine: backtesting.py 0.6 on Cboe daily bars. Option results are modelled with Black-Scholes "
         "(IV = VIX/VXN/RVX for SPY/QQQ/IWM, 20-day realized x 1.15 for single names, no skew, "
         f"{OPT_COST * 100:.0f}% round-trip cost). See the accuracy notes at the end.", ""]
    for sect, title in (("seats", "Seat nominations"), ("flow", "Flow scan leans")):
        L += [f"## {title}", "", "| Group | Closed | Open | Win | Avg option | Median option | Avg stock move (lean's way) | Avg days | Verdict |",
              "|---|---|---|---|---|---|---|---|---|"]
        for g, s in sorted(r[sect].items(), key=lambda kv: -kv[1].get("n", 0)):
            if s.get("n"):
                L.append(f"| {g} | {s['n']} | {s['open']} | {_p(s['win_rate'], False)} | {_p(s['avg_option_ret'])} | "
                         f"{_p(s['median_option_ret'])} | {_p(s['avg_underlying_ret'])} | {s['avg_days']:.1f} | {verdict(s)} |")
        L.append("")
    L += ["## GEX regime, next session", "", "| Regime | Days x names | Avg range | Avg abs move | Call wall touched / broke | Put wall touched / broke |",
          "|---|---|---|---|---|---|"]
    for name, s in r["gex"].items():
        L.append(f"| {name} | {s['n']} | {_p(s['avg_range'], False)} | {_p(s['avg_absret'])} | {s['call_wall_touches']} / "
                 f"{_p(s['call_wall_break_rate'], False)} | {s['put_wall_touches']} / {_p(s['put_wall_break_rate'], False)} |")
    L += ["", "## Opening gaps (open to close, in the gap's direction)", "", "| Case | n | Kept going | Avg | n last 2y | Kept going last 2y |", "|---|---|---|---|---|---|"]
    for k, s in r["gaps"].items():
        L.append(f"| {k} | {s['n']} | {_p(s['continue_rate'], False)} | {_p(s['avg_open_to_close_with_gap'])} | {s['n_last_2y']} | {_p(s['continue_rate_last_2y'], False)} |")
    L += ["", "## SPY straddles priced off Cboe implied vol", "", "| Case | n | Win | Avg |", "|---|---|---|---|"]
    for k, s in r["straddles"].items():
        L.append(f"| {k} | {s['n']} | {_p(s['win_rate'], False)} | {_p(s['avg_ret'])} |")
    L += ["", "## SPY long option vs debit spread (21 DTE, held 10 days)", "", "| Case | n | Win | Avg |", "|---|---|---|---|"]
    for k, s in r["spreads"].items():
        L.append(f"| {k} | {s['n']} | {_p(s['win_rate'], False)} | {_p(s['avg_ret'])} |")
    L += ["", "## Accuracy notes", "",
          "- Stock entries, stops and exits are real Cboe daily bars; backtesting.py cannot see inside a day, so a day that hits both stop and target counts as stopped.",
          "- Option prices are modelled, not recorded quotes: no free source here keeps historical option prices. Single-name IV is a realized-vol proxy, which misses earnings IV crush and skew; SPY/QQQ/IWM use their Cboe vol index, which sits a little above at-the-money IV, so long options look slightly dearer than they were.",
          "- The desk record is short (since 2026-10-02), so most desk verdicts say \"too few\" until about 15 closed trades per group. It grows every trading day.",
          "- GEX levels come from the scan's top 10 names until the nightly scan also saves scan-gex.json with every name.",
          ""]
    return "\n".join(L)


def digest():
    res_path = OUT / "results.json"
    stale = True
    if res_path.exists():
        r = json.loads(res_path.read_text())
        today = dt.datetime.now(NY).date().isoformat()
        stale = r.get("stamp") != board_stamp() or r.get("generated", "")[:10] != today
    if stale:
        run()
    print((OUT / "lessons.md").read_text(), end="")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "digest"
    if cmd == "run":
        run()
        print((OUT / "lessons.md").read_text(), end="")
    elif cmd == "report":
        print((OUT / "report.md").read_text(), end="")
    else:
        digest()

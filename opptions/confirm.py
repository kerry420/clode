"""Morning confirmation: did last night's unusual flow become new positions?

    python3 -m opptions scan flow --json > scan-flow.json   # 4:20 PM ET, after the close
    python3 -m opptions confirm scan-flow.json              # next morning, after the OI update
    python3 -m opptions confirm scan-flow.json --json --top 10

Why: open interest (OI) updates once a day, overnight. Until then, "volume
above OI" catches every contract with big new volume. After the update, the
contracts whose volume became new positions no longer look unusual (SPY Dec 31
650P: volume 75,239 on OI 4,578, then OI 79,607 the next morning), so a
morning scan misses them. The desk therefore scans after the close, and each
ranked row of `scan flow --json` keeps its top unusual contracts with the
volume and OI at scan time ("contracts"). This command refetches today's
chain for each of those names and compares.

For each saved contract, with V = the scan's volume and change = OI now - OI
at scan:
    confirmed_opened     change >= 0.5 * V
    partial              0.1 * V <= change < 0.5 * V
    closed_or_daytrade   change < 0.1 * V, including any fall
    expired              gone from today's chain, or past its expiry date; a
                         contract expiring on the scan's session date counts
                         as past, since it stopped trading before the update
When OI is unchanged on every live saved contract of a symbol, Cboe has most
likely not posted the overnight update yet, so those contracts are marked
oi_not_updated instead of being classified.

OI is net: other traders' openings and closings in the same contract are mixed
in, so each reading is a strong hint, not proof. confirm_rows() is pure (no
network, no clock); confirm() fetches the chains and calls it.
"""

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from . import cboe, flow, scan

TITLE = "Flow confirmation (overnight open interest)"
OPENED_SHARE = 0.5     # OI rose by at least half of the scan's volume
CLOSED_SHARE = 0.10    # OI fell, or rose by under a tenth of the scan's volume
STATUSES = ("confirmed_opened", "partial", "closed_or_daytrade", "expired", "oi_not_updated",
            "unavailable")
LABEL = {
    "confirmed_opened": "opened",
    "partial": "partly opened",
    "closed_or_daytrade": "closed or day trade",
    "expired": "expired",
    "oi_not_updated": "OI not updated",
    "unavailable": "not checked",
}


def fetch_raw(symbol):
    """Today's raw Cboe chain for `symbol`, uncached (raises FetchError)."""
    return scan.fetch_raw(symbol)


def classify(volume, oi_before, oi_now):
    """'confirmed_opened', 'partial' or 'closed_or_daytrade' for one live contract."""
    change = oi_now - oi_before
    if change >= OPENED_SHARE * volume:
        return "confirmed_opened"
    if change < CLOSED_SHARE * volume:
        return "closed_or_daytrade"
    return "partial"


# ---------------------------------------------------------------- helpers

def _num(x):
    return scan._num(x)


def _date(value):
    """A date from a date, a datetime or an ISO string ('2026-09-30' or '2026-09-30T16:15:00'),
    else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _expiry(c):
    """A saved contract's expiry date, from its OCC symbol or its "expiry" field."""
    try:
        return cboe.parse_option_symbol(str(c.get("contract", "")))["expiry"]
    except ValueError:
        return _date(c.get("expiry"))


def _index_chain(raw):
    """(contracts, as_of): {OCC symbol: (expiry, open interest)} for one raw chain, and the
    chain's timestamp (datetime or None). None for a chain with no contract list. A
    non-finite open interest is kept as None, so that contract is reported as not checked."""
    data = raw.get("data") if isinstance(raw, dict) else None
    options = data.get("options") if isinstance(data, dict) else None
    if not isinstance(options, list):
        return None, None
    out = {}
    for o in options:
        if not isinstance(o, dict):
            continue
        sym = str(o.get("option", "")).replace(" ", "")
        try:
            expiry = cboe.parse_option_symbol(sym)["expiry"]
        except ValueError:
            continue
        oi = cboe._num(o.get("open_interest"))
        out[sym] = (expiry, oi if math.isfinite(oi) else None)
    return out, cboe._as_of(raw)


def _saved_contracts(row):
    """The contracts a scan row kept; older scan files only have "top_contract"."""
    contracts = row.get("contracts")
    if contracts is None:
        top = row.get("top_contract")
        contracts = [top] if isinstance(top, dict) else []
    elif not isinstance(contracts, list):  # a hand-edited or damaged file
        contracts = []
    return [c for c in contracts if isinstance(c, dict) and c.get("contract")]


def _rows(scan_result, top=None):
    if not isinstance(scan_result, dict):
        raise ValueError("not a scan result (expected the JSON object from `scan flow --json`)")
    kind = scan_result.get("kind")
    if kind not in (None, "flow"):
        raise ValueError(f"this is a `scan {kind}` file; confirm needs `scan flow --json`")
    ranked = scan_result.get("ranked")
    if not isinstance(ranked, list):
        raise ValueError("no \"ranked\" list (expected the JSON object from `scan flow --json`)")
    rows = [r for r in ranked if isinstance(r, dict) and r.get("symbol")]
    return rows[:top] if top else rows


# ---------------------------------------------------------------- pure core

def _base(c):
    """A saved contract as confirm reports it, before today's chain is read."""
    volume, oi_before = _num(c.get("volume")), _num(c.get("open_interest"))
    return {
        "contract": c.get("contract"),
        "type": c.get("type"),
        "strike": _num(c.get("strike")),
        "expiry": c.get("expiry"),
        "deep_itm": c.get("deep_itm") is True,
        "side": c.get("side"),
        "extrinsic_premium": _num(c.get("extrinsic_premium")),
        "volume": None if volume is None else int(volume),
        "oi_at_scan": None if oi_before is None else int(oi_before),
        "oi_now": None,
        "oi_change": None,
        "oi_change_vs_volume": None,
        "status": "unavailable",
    }


def _check_contract(c, index, ref_date, session=None):
    """One saved contract against today's chain. ref_date: today's date (the chain's own);
    session: the scan's session date, on which a same-day expiry already stopped trading."""
    out = _base(c)
    volume, oi_before = _num(c.get("volume")), _num(c.get("open_interest"))
    expiry = _expiry(c)
    found = index.get(str(c.get("contract")).replace(" ", ""))
    past = expiry is not None and ((ref_date is not None and expiry < ref_date)
                                   or (session is not None and expiry <= session))
    if found is None or past:
        out["status"] = "expired"
        return out
    oi_now = found[1]
    if oi_now is None:
        return out  # today's chain has no usable open interest for it
    out["oi_now"] = int(oi_now)
    if volume is None or volume <= 0 or oi_before is None:
        return out  # the scan file has no usable volume or OI for it
    change = oi_now - oi_before
    out["oi_change"] = int(round(change))
    out["oi_change_vs_volume"] = round(change / volume, 3)
    out["status"] = classify(volume, oi_before, oi_now)
    return out


def _counts(contracts):
    counts = dict.fromkeys(STATUSES, 0)
    for c in contracts:
        counts[c["status"]] += 1
    return counts


def confirm_rows(scan_result, chains_by_symbol, errors=None, today=None, top=None):
    """Compare each ranked row's saved contracts with today's chains (pure).

    scan_result: the dict saved from `scan flow --json`. chains_by_symbol: {symbol: raw
    Cboe chain fetched today}. errors: {symbol: message} for chains that could not be
    fetched. today: the date (or ISO string) used as "today" for a chain without a
    timestamp; by default the scan's session date. top: only the first N ranked rows.

    Returns {"kind": "flow_confirm", "scan_as_of", "scan_session_date", "checked_as_of",
    "rules", "symbols": [{"symbol", "rank", "score", "lean", "status" ("checked",
    "oi_not_updated", "unavailable" or "no_contracts"), "chain_as_of", "error",
    "contracts": [{..., "volume", "oi_at_scan", "oi_now", "oi_change",
    "oi_change_vs_volume", "status"}], "counts", "summary"}], "counts",
    "oi_not_updated": [symbols], "errors"}.
    """
    rows = _rows(scan_result, top)
    errors = errors or {}
    sessions = [str(r.get("session_date"))[:10] for r in rows if r.get("session_date")]
    scan_session = max(sessions, default=None)
    fallback = _date(today) or _date(scan_session) or _date(scan_result.get("as_of"))
    symbols, stamps = [], []
    for rank, row in enumerate(rows, 1):
        sym = str(row["symbol"])
        saved = _saved_contracts(row)
        entry = {"symbol": sym, "rank": rank, "score": _num(row.get("score")), "lean": row.get("lean"),
                 "status": "checked", "chain_as_of": None, "error": None, "contracts": []}
        index, as_of = _index_chain(chains_by_symbol.get(sym))
        if not saved:
            entry["status"] = "no_contracts"
        elif not index:
            entry["status"] = "unavailable"
            if sym not in chains_by_symbol:
                entry["error"] = errors.get(sym) or "today's chain was not fetched"
            else:
                entry["error"] = ("today's chain has no contracts" if index is not None
                                  else "today's chain is not a Cboe option chain")
            entry["contracts"] = [_base(c) for c in saved]
        else:
            if as_of:
                entry["chain_as_of"] = as_of.isoformat()
                stamps.append(entry["chain_as_of"])
            ref_date = as_of.date() if as_of else fallback
            session = _date(row.get("session_date"))
            checked = [_check_contract(c, index, ref_date, session) for c in saved]
            live = [c for c in checked if c["status"] not in ("expired", "unavailable")]
            if live and all(c["oi_change"] == 0 for c in live):
                entry["status"] = "oi_not_updated"
                for c in live:
                    c["status"] = "oi_not_updated"
            entry["contracts"] = checked
        entry["counts"] = _counts(entry["contracts"])
        entry["summary"] = symbol_summary(entry)
        symbols.append(entry)
    total = dict.fromkeys(STATUSES, 0)
    for entry in symbols:
        for k, v in entry["counts"].items():
            total[k] += v
    return {
        "kind": "flow_confirm",
        "scan_as_of": scan_result.get("as_of"),
        "scan_session_date": scan_session,
        "checked_as_of": max(stamps, default=None),
        "rules": {"opened_share": OPENED_SHARE, "closed_share": CLOSED_SHARE},
        "symbols": symbols,
        "counts": total,
        "oi_not_updated": [e["symbol"] for e in symbols if e["status"] == "oi_not_updated"],
        "errors": {e["symbol"]: e["error"] for e in symbols if e["status"] == "unavailable"},
    }


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def symbol_summary(entry):
    """One plain-English line for one symbol."""
    sym, counts, status = entry["symbol"], entry.get("counts") or {}, entry["status"]
    if status == "no_contracts":
        return f"{sym}: the scan file lists no contracts for this name, so nothing was checked."
    if status == "unavailable":
        return (f"{sym}: today's chain could not be checked ({scan._first_sentence(entry.get('error') or '')}), "
                "so nothing was confirmed.")
    if status == "oi_not_updated":
        n = counts.get("oi_not_updated", 0)
        tail = (" With one contract, opening and closing trades that exactly offset would look the same."
                if n == 1 else "")
        return (f"{sym}: open interest is unchanged on {'its' if n == 1 else 'all ' + str(n)} live flagged "
                f"contract{'s' if n != 1 else ''}, so Cboe has most likely not posted the overnight update "
                f"yet; run confirm again later.{tail}")
    total = sum(counts.values())
    opened, part = counts.get("confirmed_opened", 0), counts.get("partial", 0)
    closed, expired = counts.get("closed_or_daytrade", 0), counts.get("expired", 0)
    unchecked = counts.get("unavailable", 0)
    if expired == total:
        return f"{sym}: all {_plural(total, 'flagged contract')} have expired, so there is nothing to check."
    if not opened + part + closed:
        return (f"{sym}: the scan file has no usable volume or open interest for its live flagged "
                "contracts, so nothing was confirmed.")
    parts = [f"{opened} of {_plural(total, 'flagged contract')} look{'s' if opened == 1 else ''} opened "
             "(open interest rose by at least half the volume)"]
    if part:
        parts.append(f"{part} partly opened")
    if closed:
        parts.append(f"{closed} look{'s' if closed == 1 else ''} closed or day-traded "
                     "(open interest fell or barely moved)")
    if expired:
        parts.append(f"{expired} expired")
    if unchecked:
        parts.append(f"{unchecked} not checked (no usable volume or open interest)")
    if opened:
        tend = ("at least some of last night's activity became new positions, which tends to make "
                "it more meaningful")
    elif part:
        tend = "only part of the volume looks like new positions"
    else:
        tend = ("the volume looks like closing or same-day trading rather than new positions, which "
                "tends to make it less meaningful")
    return f"{sym}: {', '.join(parts)}; {tend}."


# ---------------------------------------------------------------- fetch + confirm

def confirm(scan_result, fetch=None, workers=scan.DEFAULT_WORKERS, top=None, today=None):
    """Fetch today's chain for every ranked name with saved contracts, then confirm_rows()."""
    fetch = fetch or fetch_raw
    rows = _rows(scan_result, top)
    symbols = [str(r["symbol"]) for r in rows if _saved_contracts(r)]
    chains, errors = {}, {}
    if symbols:
        size = max(1, min(int(workers), scan.MAX_WORKERS, len(symbols)))
        with ThreadPoolExecutor(max_workers=size) as pool:
            futures = [(sym, pool.submit(fetch, sym)) for sym in symbols]
            for sym, fut in futures:
                try:
                    chains[sym] = fut.result()
                except Exception as e:  # one bad symbol never stops the check
                    errors[sym] = scan._error_text(e)
    return confirm_rows(scan_result, chains, errors=errors, today=today, top=top)


# ---------------------------------------------------------------- markdown

RULE_LINE = ("A flagged contract counts as opened when its open interest rose by at least half of the "
             "scan's volume, partly opened from a tenth to half, and closed or day-traded when it fell or "
             "rose by less than a tenth. Contracts that became new positions tend to matter more than "
             "volume that was closed again the same day.")
CAVEAT = ("_Caveat: open interest is net, so other traders opening and closing the same contract are "
          "mixed in, and a spread leg or a roll can look the same as a fresh position. Cboe's chain is "
          "free and about 15 minutes delayed, and open interest updates once a day. These are clues to "
          "weigh, not trade signals._")


def _when(ts):
    return (ts or "unknown time").replace("T", " ")[:16]


def _fmt_int(x):
    return "n/a" if x is None else f"{x:,.0f}"


def _change(c):
    if c["oi_change"] is None:
        return "n/a"
    share = c["oi_change_vs_volume"]
    return f"{c['oi_change']:+,} ({share * 100:.0f}% of volume)"


def _tally(counts):
    order = (("confirmed_opened", "opened"), ("partial", "partly opened"),
             ("closed_or_daytrade", "closed or day-traded"), ("expired", "expired"),
             ("oi_not_updated", "waiting for the OI update"), ("unavailable", "not checked"))
    parts = [f"{counts[k]} {label}" for k, label in order if counts.get(k)]
    return ", ".join(parts) if parts else "none"


def to_markdown(result):
    """Markdown: one short table and one summary line per symbol."""
    symbols = result.get("symbols") or []
    lines = [f"## {TITLE}", ""]
    n_contracts = sum(len(e["contracts"]) for e in symbols)
    session = result.get("scan_session_date")
    head = (f"Scan data as of {_when(result.get('scan_as_of'))}"
            + (f" (volume from the {session} session)" if session else "")
            + (f", checked against chains as of {_when(result.get('checked_as_of'))}"
               if result.get("checked_as_of") else "") + ".")
    if not symbols:
        lines += [head, "", "No ranked names in the scan file, so there is nothing to confirm.", "", CAVEAT]
        return "\n".join(lines) + "\n"
    lines.append(f"{head} {_plural(len(symbols), 'name')}, {_plural(n_contracts, 'flagged contract')}: "
                 f"{_tally(result.get('counts') or {})}.")
    waiting = result.get("oi_not_updated") or []
    if waiting and len(waiting) == sum(e["status"] in ("checked", "oi_not_updated") for e in symbols):
        lines.append("Open interest has not changed on any checked name, so Cboe has most likely not "
                     "posted the overnight update yet. Run confirm again later in the morning.")
    elif waiting:
        lines.append(f"Open interest not updated yet: {', '.join(waiting)}.")
    for e in symbols:
        score = "" if e["score"] is None else f", score {e['score']:.1f}"
        lean = f", lean {e['lean']}" if e.get("lean") else ""
        lines += ["", f"### {e['symbol']} (scan #{e['rank']}{score}{lean})", ""]
        if e["contracts"]:
            lines += ["| Contract | Type | Strike | Expiry | Scan volume | OI at scan | OI now | "
                      "OI change | Result |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for c in e["contracts"]:
                typ = (c.get("type") or "?") + (" (deep ITM)" if c.get("deep_itm") else "")
                lines.append(f"| {c['contract']} | {typ} | {flow._fmt_price(c['strike'])} | "
                             f"{c.get('expiry') or 'n/a'} | "
                             f"{_fmt_int(c['volume'])} | {_fmt_int(c['oi_at_scan'])} | "
                             f"{_fmt_int(c['oi_now'])} | {_change(c)} | {LABEL[c['status']]} |")
            lines.append("")
        lines.append(e["summary"])
    lines += ["", RULE_LINE, "", CAVEAT]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def main(argv=None):
    """python3 -m opptions confirm SCAN_FLOW_JSON [--json] [--top N]."""
    p = argparse.ArgumentParser(
        prog="opptions confirm",
        description="Check which contracts flagged by last night's `scan flow --json` became new "
                    "positions, from today's open interest.")
    p.add_argument("scan_file", metavar="SCAN_FLOW_JSON",
                   help="file saved from `python3 -m opptions scan flow --json` after the close")
    p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    p.add_argument("--top", type=scan._positive_int, default=None, metavar="N",
                   help="only the first N ranked names (default: all in the file)")
    args = p.parse_args(argv)

    try:
        with open(args.scan_file, encoding="utf-8") as f:
            saved = json.load(f)
        result = confirm(saved, top=args.top)
    except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
        print(f"[confirm] could not use {args.scan_file}: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(to_markdown(result))
    errors = result["errors"]
    fetched = [e for e in result["symbols"] if e["status"] not in ("unavailable", "no_contracts")]
    if errors:
        first = scan._first_sentence(next(iter(errors.values())))
        print(f"[confirm] {len(errors)} of {len(errors) + len(fetched)} chains could not be fetched"
              + (f"; first error: {first}" if not fetched else ""), file=sys.stderr)
    return 1 if errors and not fetched else 0

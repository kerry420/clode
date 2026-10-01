"""Earnings: next report date, what options price for it, and the beat history.

Sources (all free, all independent; one failing does not stop the others):

  * Nasdaq next earnings date (data from Zacks Investment Research):
        https://api.nasdaq.com/api/analyst/{SYM}/earnings-date
    {"data": {"reportText": "Acme, Inc. Common Stock is expected* to report
              earnings on 10/22/2026 after market close. The report will be for
              the fiscal Quarter ending Sep 2026. According to Zacks Investment
              Research, based on 8 analysts' forecasts, the consensus EPS
              forecast for the quarter is $1.42. The reported EPS for the same
              quarter last year was $1.10.",
              "announcement": "Earnings announcement* for ACME: Oct 22, 2026",
              "heading": "ACME Earnings Date", ...},
     "message": null, "status": {"rCode": 200, ...}}
    With no date, reportText says Zacks "hasn't provided us with the upcoming
    earnings report date" and the announcement ends after the colon.
  * Nasdaq EPS surprise history:
        https://api.nasdaq.com/api/company/{SYM}/earnings-surprise
    {"data": {"earningsSurpriseTable": {"rows": [
        {"fiscalQtrEnd": "Jun 2026", "dateReported": "7/23/2026", "eps": 1.38,
         "consensusForecast": "1.31", "percentageSurprise": "5.34"}, ...]}}}
    Values arrive as numbers or strings (sometimes with "$" or "N/A").
  * Cboe's 15-minute-delayed chain (cboe.py) for the implied move.
  * SEC EDGAR submissions (sec.py) for the newest 8-K with item 2.02
    (results of operations: the earnings press release).

Method:
  * Timing comes from reportText: "after market close" -> after close,
    "before market open" -> before open, otherwise unknown.
  * Earnings expiry: the first expiry on or after the report date, strictly
    after it when the report is after the close. When the timing is unknown
    it is also taken strictly after, so the expiry surely includes the
    reaction (an expiry on report day would miss an after-close report).
  * ATM strike: the strike nearest spot that has both a call and a put with a
    price; straddle = call mid + put mid; implied move = straddle / spot.
  * The straddle covers everything until expiry, so when the earnings expiry
    is more than a few days out it also prices ordinary movement and the
    report-only move is smaller; the output says so.
  * ATM IV = average of the ATM call and put IV. The earnings expiry's ATM IV
    minus the next later expiry's ATM IV shows how the report's premium is
    concentrated in the nearer expiry. Both include the report, so the gap
    understates the "IV crush": the ATM IV of the last expiry that ends
    before the report (pre_atm_iv) is shown as a rough baseline that IV
    tends to fall back toward after the report.
  * With no upcoming date, the same numbers are computed for the front expiry
    (nearest one at least a day out) and labeled as not an earnings move.

analyze() is pure: the only clock read is fetched_at, stored by fetch().
Days until the report count from fetched_at converted to US Eastern time.
All IVs in the result are decimals (0.42 = 42%); moves are in percent.
"""

import math
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from . import cboe, sec
from .http import FetchError, fetch_json

TITLE = "Earnings"
DATE_URL = "https://api.nasdaq.com/api/analyst/{sym}/earnings-date"
SURPRISE_URL = "https://api.nasdaq.com/api/company/{sym}/earnings-surprise"
NASDAQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
SOURCES = ("nasdaq_date", "nasdaq_surprise", "chain", "sec_submissions")
SOURCE_NAMES = {"nasdaq_date": "Nasdaq earnings date", "nasdaq_surprise": "Nasdaq EPS history",
                "chain": "Cboe option chain", "sec_submissions": "SEC filings"}
HISTORY_QUARTERS = 4
SEC_SCAN = 400          # filings to scan for the last 8-K 2.02 (Form 4s crowd the list)
JUST_REPORTED_DAYS = 7
CLEAN_READ_DTE = 5      # beyond this, the straddle holds a lot of ordinary (non-report) movement
ATM_GAP_WARN_PCT = 5.0  # nearest priced strike this far from spot inflates the straddle

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}

AFTER_RE = re.compile(r"after[\s-]+(?:the\s+)?(?:market[\s-]+)?clos|after[\s-]+hours|post[\s-]?market", re.I)
BEFORE_RE = re.compile(r"(?:before|prior\s+to)\s+(?:the\s+)?(?:market\s+)?open|pre[\s-]?market", re.I)
MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MON_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
ON_DATE_RE = re.compile(r"earnings\s+on\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
WORDING_RE = re.compile(r"\b(estimated|expected|scheduled|confirmed)\b", re.I)
QUARTER_RE = re.compile(r"fiscal\s+quarter\s+ending\s+([A-Za-z]{3,9}\s+\d{4})", re.I)
ANALYSTS_RE = re.compile(r"based\s+on\s+(\d+)\s+analyst", re.I)
CONSENSUS_RE = re.compile(r"consensus\s+EPS\s+forecast\s+for\s+the\s+quarter\s+is\s+(-?\$?\s?-?[\d,]*\.?\d+)", re.I)
YEAR_AGO_RE = re.compile(r"same\s+quarter\s+last\s+year\s+was\s+(-?\$?\s?-?[\d,]*\.?\d+)", re.I)

TRANSCRIPT_NOTE = ("The earnings-call transcript is not gathered by this script. The Earnings agent "
                   "reads it from the web (for example Motley Fool's transcript pages or the company's "
                   "investor-relations site) and summarizes it for the Explainer.")


# ---------------------------------------------------------------- fetch

def _nasdaq(url):
    """Nasdaq JSON payload; FetchError when it answers without data (unknown symbol, no coverage)."""
    payload = fetch_json(url, NASDAQ_HEADERS)
    if not isinstance(payload, dict) or not payload.get("data"):
        status = payload.get("status") if isinstance(payload, dict) else None
        detail = ""
        if isinstance(status, dict):
            msgs = status.get("bCodeMessage") or []
            if isinstance(msgs, list):
                detail = "; ".join(str(m.get("errorMessage", m)) if isinstance(m, dict) else str(m)
                                   for m in msgs)
        raise FetchError(f"api.nasdaq.com returned no data for {url}" + (f" ({detail})" if detail else ""))
    return payload


def fetch_earnings_date(symbol):
    """Raw Nasdaq earnings-date JSON."""
    return _nasdaq(DATE_URL.format(sym=quote(symbol.upper(), safe="")))


def fetch_surprise(symbol):
    """Raw Nasdaq earnings-surprise JSON."""
    return _nasdaq(SURPRISE_URL.format(sym=quote(symbol.upper(), safe="")))


def fetch_sec_submissions(symbol):
    """SEC EDGAR submissions JSON for the ticker's CIK."""
    cik = sec.ticker_to_cik(symbol)
    if cik is None:
        raise FetchError(f"www.sec.gov has no CIK for ticker {symbol} (ETFs and many funds have none)")
    return sec.fetch_submissions(cik)


def fetch(symbol):
    """Gather every source independently. Raises FetchError only if all of them failed.

    Returns {"symbol", "fetched_at" (UTC ISO), "nasdaq_date", "nasdaq_surprise",
             "chain", "sec_submissions", "errors": {source: message}}.
    """
    sym = symbol.upper()
    raw = {"symbol": sym,
           "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "errors": {}}
    getters = {
        "nasdaq_date": fetch_earnings_date,
        "nasdaq_surprise": fetch_surprise,
        "chain": cboe.fetch_chain,
        "sec_submissions": fetch_sec_submissions,
    }
    for key in SOURCES:
        raw[key] = None
        try:
            raw[key] = getters[key](sym)
        except Exception as e:  # noqa: BLE001 - each source is independent; record and go on
            raw[key] = None
            raw["errors"][key] = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {e}"
    if all(raw[key] is None for key in SOURCES):
        raise FetchError("no earnings source could be reached: " + " | ".join(
            f"{SOURCE_NAMES[k]}: {v}" for k, v in raw["errors"].items()))
    return raw


# ---------------------------------------------------------------- parsing helpers

def to_number(v):
    """Tolerant float: 1.2, "1.20", "$1.20", "-$0.05", "($0.05)", "5.3%" -> float; "N/A", "", "NaN", None -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    if s.count("-") == 1 and not s.startswith("-"):
        s = "-" + s.replace("-", "")
    try:
        f = float(s)
    except ValueError:
        return None
    if not math.isfinite(f):          # "NaN", "inf" would break JSON and comparisons
        return None
    return -f if neg else f


def parse_date(text):
    """First date in text as a date: '10/22/2026', '2026-10-22', 'Oct 22, 2026', 'Sept 3, 2026'. None if absent."""
    if not text:
        return None
    text = str(text)
    for rx, order in ((MDY_RE, "mdy"), (ISO_RE, "ymd")):
        m = rx.search(text)
        if m:
            a, b, c = (int(g) for g in m.groups())
            try:
                return date(c, a, b) if order == "mdy" else date(a, b, c)
            except ValueError:
                pass
    for m in MON_RE.finditer(text):
        month = MONTHS.get(m.group(1)[:3].lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                continue
    return None


def parse_timing(text):
    """'after close', 'before open' or 'unknown' from Nasdaq's report text."""
    text = text or ""
    if AFTER_RE.search(text):
        return "after close"
    if BEFORE_RE.search(text):
        return "before open"
    return "unknown"


def eastern_now(fetched_at):
    """US Eastern wall-clock datetime for a stored UTC ISO timestamp (US DST rules since 2007)."""
    try:
        dt = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    start = datetime(dt.year, 3, 8, 7, tzinfo=timezone.utc)       # 2nd Sunday of March, 2:00 EST
    start += timedelta(days=(6 - start.weekday()) % 7)
    end = datetime(dt.year, 11, 1, 6, tzinfo=timezone.utc)        # 1st Sunday of November, 2:00 EDT
    end += timedelta(days=(6 - end.weekday()) % 7)
    offset = -4 if start <= dt < end else -5
    return (dt + timedelta(hours=offset)).replace(tzinfo=None)


def parse_next_date(payload):
    """Next report from the Nasdaq earnings-date payload (dates as ISO strings)."""
    data = (payload or {}).get("data") or {}
    text = str(data.get("reportText") or "")
    announcement = str(data.get("announcement") or "")
    m = ON_DATE_RE.search(text)
    from_text = parse_date(m.group(1)) if m else parse_date(text)
    from_ann = parse_date(announcement.split(":", 1)[-1]) if announcement else None
    when = from_text or from_ann
    wording = WORDING_RE.search(text)
    quarter = QUARTER_RE.search(text)
    analysts = ANALYSTS_RE.search(text)
    consensus = CONSENSUS_RE.search(text)
    year_ago = YEAR_AGO_RE.search(text)
    return {
        "date": when.isoformat() if when else None,
        "timing": parse_timing(text) if when else "unknown",
        "wording": wording.group(1).lower() if (wording and when) else None,
        "fiscal_quarter": quarter.group(1) if quarter else None,
        "consensus_eps": to_number(consensus.group(1)) if consensus else None,
        "analysts": int(analysts.group(1)) if analysts else None,
        "year_ago_eps": to_number(year_ago.group(1)) if year_ago else None,
        "sources_disagree": bool(from_text and from_ann and from_text != from_ann),
        "announcement_date": from_ann.isoformat() if from_ann else None,
        "source_text": text[:400] or announcement[:200] or None,
    }


def parse_surprises(payload, quarters=HISTORY_QUARTERS):
    """Newest-first EPS history rows from the Nasdaq earnings-surprise payload."""
    data = (payload or {}).get("data") or {}
    table = data.get("earningsSurpriseTable") or {}
    rows = table.get("rows") if isinstance(table, dict) else None
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        reported = parse_date(r.get("dateReported"))
        actual = to_number(r.get("eps"))
        consensus = to_number(r.get("consensusForecast"))
        surprise = to_number(r.get("percentageSurprise"))
        if surprise is None and actual is not None and consensus:
            surprise = (actual - consensus) / abs(consensus) * 100.0
        if actual is None or consensus is None:
            result = "n/a"
        elif actual > consensus:
            result = "beat"
        elif actual < consensus:
            result = "miss"
        else:
            result = "in line"
        out.append({
            "fiscal_quarter": r.get("fiscalQtrEnd") or None,
            "reported": reported.isoformat() if reported else None,
            "consensus_eps": consensus,
            "actual_eps": actual,
            "surprise_pct": None if surprise is None else round(surprise, 2),
            "result": result,
        })
    out.sort(key=lambda row: row["reported"] or "", reverse=True)
    return out[:quarters]


def latest_release(submissions, as_of=None):
    """Newest 8-K (or 8-K/A) whose items include 2.02, with filing date, URL and days since."""
    if not submissions:
        return None
    for f in sec.recent_filings(submissions, limit=SEC_SCAN):
        if str(f.get("form", "")).startswith("8-K") and "2.02" in f.get("items", []):
            filed = parse_date(f.get("filed"))
            return {
                "form": f["form"],
                "filed": filed.isoformat() if filed else f.get("filed"),
                "items": f["items"],
                "item_meanings": f["item_meanings"],
                "url": f["url"] or None,
                "days_since": (as_of - filed).days if (filed and as_of) else None,
            }
    return None


# ---------------------------------------------------------------- implied move

def pick_earnings_expiry(expiries, earnings_date, timing):
    """First expiry on or after earnings_date; strictly after unless the report is before the open."""
    strict = timing != "before open"
    for e in sorted(expiries):
        if e > earnings_date or (e == earnings_date and not strict):
            return e
    return None


def atm_pair(options, expiry, spot):
    """ATM call/put for one expiry: strike nearest spot with both a call and a put priced (mid > 0)."""
    calls, puts = {}, {}
    for o in options:
        if o["expiry"] != expiry or o["mid"] <= 0 or o["strike"] <= 0:
            continue
        (calls if o["type"] == "C" else puts)[o["strike"]] = o
    both = set(calls) & set(puts)
    if not both or spot <= 0:
        return None
    strike = min(both, key=lambda k: (abs(k - spot), k))
    return calls[strike], puts[strike]


def _atm_iv(call, put):
    ivs = [o["iv"] for o in (call, put) if o["iv"] > 0]
    return sum(ivs) / len(ivs) if ivs else None


def straddle_stats(options, expiry, spot):
    """Straddle math for one expiry; None when no strike has both sides priced."""
    pair = atm_pair(options, expiry, spot)
    if pair is None:
        return None
    call, put = pair
    straddle = call["mid"] + put["mid"]
    iv = _atm_iv(call, put)
    return {
        "expiry": expiry,
        "dte": call["dte"],
        "atm_strike": call["strike"],
        "call_mid": round(call["mid"], 4),
        "put_mid": round(put["mid"], 4),
        "straddle": round(straddle, 4),
        "move_pct": round(straddle / spot * 100.0, 2),
        "range_low": round(spot - straddle, 2),
        "range_high": round(spot + straddle, 2),
        "atm_iv": None if iv is None else round(iv, 4),
    }


def implied_move(chain, earnings_date=None, timing="unknown"):
    """Implied move for the earnings expiry, or the front expiry when there is no usable date."""
    spot = chain["spot"]
    dte = {o["expiry"]: o["dte"] for o in chain["options"]}
    expiries = sorted(x for x, d in dte.items() if d >= 0)
    notes = []
    if not expiries:
        return None, ["The option chain has no unexpired contracts, so there is no implied move."]
    if spot <= 0:
        return None, ["No usable spot price in the chain, so the implied move cannot be computed."]

    basis, expiry = "front_expiry", None
    if earnings_date:
        e = pick_earnings_expiry([date.fromisoformat(x) for x in expiries],
                                 date.fromisoformat(earnings_date), timing)
        if e is not None:
            basis, expiry = "earnings", e.isoformat()
        else:
            notes.append("No listed expiry falls after the earnings date yet; showing the front expiry instead.")
    if expiry is None:
        later = [x for x in expiries if dte[x] >= 1]
        expiry = (later or expiries)[0]

    stats = straddle_stats(chain["options"], expiry, spot)
    if stats is None:
        return None, notes + [f"No strike in the {expiry} expiry has both a priced call and put, "
                              "so the straddle cannot be computed."]
    gap_pct = abs(stats["atm_strike"] - spot) / spot * 100.0
    if gap_pct > ATM_GAP_WARN_PCT:
        notes.append(f"The nearest strike with both sides priced ({stats['atm_strike']:g}) is "
                     f"{gap_pct:.1f}% from spot, so the straddle carries in-the-money value and tends "
                     "to overstate the move.")
    nxt = next((x for x in expiries if x > expiry), None)
    nxt_stats = straddle_stats(chain["options"], nxt, spot) if nxt else None
    iv_diff = None
    if stats["atm_iv"] is not None and nxt_stats and nxt_stats["atm_iv"] is not None:
        iv_diff = round(stats["atm_iv"] - nxt_stats["atm_iv"], 4)
    if nxt is None:
        notes.append("There is no later expiry to compare IV with.")
    elif nxt_stats is None:
        notes.append(f"The next expiry {nxt} has no strike with both a priced call and put, "
                     "so there is no IV to compare with.")
    pre_stats = None
    if basis == "earnings":
        # Baseline: the latest expiry (at least a day out) that ends before the report.
        cut = date.fromisoformat(earnings_date)
        before = [x for x in expiries if dte[x] >= 1 and (
            date.fromisoformat(x) < cut or (date.fromisoformat(x) == cut and timing == "after close"))]
        pre_stats = straddle_stats(chain["options"], before[-1], spot) if before else None
    stats.update({
        "basis": basis,
        "spot": spot,
        "spot_source": chain["spot_source"],
        "chain_as_of": chain["as_of"],
        "next_expiry": nxt,
        "next_dte": nxt_stats["dte"] if nxt_stats else None,
        "next_atm_strike": nxt_stats["atm_strike"] if nxt_stats else None,
        "next_atm_iv": nxt_stats["atm_iv"] if nxt_stats else None,
        "iv_diff": iv_diff,
        "pre_expiry": pre_stats["expiry"] if pre_stats else None,
        "pre_dte": pre_stats["dte"] if pre_stats else None,
        "pre_atm_iv": pre_stats["atm_iv"] if pre_stats else None,
    })
    return stats, notes


# ---------------------------------------------------------------- analyze

def analyze(raw):
    """Pure earnings analysis of fetch() output. JSON-serializable; see module docstring."""
    raw = raw or {}
    errors = dict(raw.get("errors") or {})
    notes = []
    now_et = eastern_now(raw.get("fetched_at"))
    today = now_et.date() if now_et else None

    nxt = parse_next_date(raw.get("nasdaq_date")) if raw.get("nasdaq_date") else None
    upcoming = None
    date_status = "unavailable"
    if nxt and nxt["date"]:
        days = (date.fromisoformat(nxt["date"]) - today).days if today else None
        nxt["days_until"] = days
        if days is not None and days < 0:
            date_status = "passed"
            notes.append(f"Nasdaq still lists {nxt['date']}, which has passed; the next date is not out yet.")
        else:
            date_status = "found"
            upcoming = nxt
            if days == 0 and nxt["timing"] == "before open":
                notes.append("The report is today before the open, so it may already be out.")
            elif days == 0 and nxt["timing"] == "after close" and now_et.hour >= 16:
                notes.append("The report is today after the close and this was fetched after 4 PM ET, "
                             "so it may already be out.")
        if nxt["sources_disagree"]:
            notes.append(f"Nasdaq's report text says {nxt['date']} but its headline says "
                         f"{nxt['announcement_date']}; check the company's investor-relations page.")
    elif nxt:
        date_status = "none_listed"
        nxt["days_until"] = None
        notes.append("Nasdaq/Zacks has no upcoming earnings date for this symbol "
                     "(normal for ETFs and indexes, or when the company has not been tracked yet).")

    history = parse_surprises(raw.get("nasdaq_surprise")) if raw.get("nasdaq_surprise") else []
    compared = [h for h in history if h["result"] != "n/a"]
    beats = sum(1 for h in compared if h["result"] == "beat")

    move = None
    chain_raw = raw.get("chain")
    if chain_raw and not (chain_raw.get("timestamp") or (chain_raw.get("data") or {}).get("timestamp")):
        # keep normalize_chain off the clock (it falls back to date.today()): date it by our fetch time
        if raw.get("fetched_at"):
            chain_raw = dict(chain_raw, timestamp=str(raw["fetched_at"])[:19].replace("T", " "))
            notes.append("The Cboe chain carried no timestamp; days to expiry count from the fetch time (UTC).")
        else:
            chain_raw = None
            notes.append("The Cboe chain carried no timestamp and the fetch time is missing, so expiries "
                         "cannot be dated and the implied move is skipped.")
    if chain_raw:
        chain = cboe.normalize_chain(chain_raw)
        if upcoming:
            move, move_notes = implied_move(chain, upcoming["date"], upcoming["timing"])
        else:
            move, move_notes = implied_move(chain)
        notes += move_notes
        if move and move["basis"] == "earnings" and upcoming["timing"] == "unknown":
            notes.append("Report timing is unknown, so the expiry after the report date is used to be "
                         "sure it includes the reaction.")
        if move:
            # True when the earnings straddle also holds many days of ordinary movement (see to_markdown)
            move["mixes_ordinary_movement"] = move["basis"] == "earnings" and move["dte"] > CLEAN_READ_DTE
        if move and chain["spot_source"] not in ("current_price",):
            notes.append(f"Spot taken from '{chain['spot_source']}', not a live quote.")

    release = latest_release(raw.get("sec_submissions"), today) if raw.get("sec_submissions") else None
    if raw.get("sec_submissions") and release is None:
        notes.append("No 8-K with item 2.02 (earnings release) in the company's recent SEC filings.")
    just_reported = bool(release and release["days_since"] is not None
                         and 0 <= release["days_since"] <= JUST_REPORTED_DAYS)

    return {
        "tracker": "earnings",
        "symbol": raw.get("symbol"),
        "fetched_at": raw.get("fetched_at"),
        "as_of_date_et": today.isoformat() if today else None,
        "date_status": date_status,
        "next_earnings": upcoming,
        "stale_listed_date": nxt["date"] if (nxt and nxt["date"] and not upcoming) else None,
        "implied_move": move,
        "surprise_history": history,
        "beats": beats,
        "quarters_compared": len(compared),
        "latest_release": release,
        "just_reported": just_reported,
        "transcript_note": TRANSCRIPT_NOTE,
        "errors": errors,
        "notes": notes,
    }


# ---------------------------------------------------------------- markdown

def _money(x, digits=2):
    if x is None:
        return "n/a"
    return f"-${abs(x):,.{digits}f}" if x < 0 else f"${x:,.{digits}f}"


def _iv(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _long_date(iso):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "n/a"
    return f"{d:%a %b} {d.day}, {d.year}"


def _article(number_text):
    """'a' or 'an' before a number as spoken: an 8%, an 11%, an 18%, a 12%."""
    whole = number_text.split(".")[0].lstrip("+-")
    return "an" if whole.startswith("8") or whole in ("11", "18") else "a"


def _countdown(days):
    if days is None:
        return "countdown unknown"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def to_markdown(result):
    """Readable earnings section for a person learning, and for an LLM agent."""
    sym = result.get("symbol") or "?"
    lines = [f"## {TITLE}: {sym}", ""]
    fetched = (result.get("fetched_at") or "unknown time").replace("T", " ")[:16]
    lines += [f"Fetched {fetched} UTC (US Eastern date {result.get('as_of_date_et') or 'unknown'}).", ""]

    # Calendar
    nxt = result.get("next_earnings")
    if nxt:
        wording = f'Nasdaq/Zacks "{nxt["wording"]}" date' if nxt.get("wording") else "Nasdaq/Zacks date"
        lines.append(f"**Next report: {_long_date(nxt['date'])}, {nxt['timing']} "
                     f"({_countdown(nxt.get('days_until'))}).** Source: {wording}.")
        bits = []
        if nxt.get("fiscal_quarter"):
            bits.append(f"fiscal quarter ending {nxt['fiscal_quarter']}")
        if nxt.get("consensus_eps") is not None:
            who = f" from {nxt['analysts']} analysts" if nxt.get("analysts") else ""
            bits.append(f"consensus EPS {_money(nxt['consensus_eps'])}{who} (Zacks)")
        if nxt.get("year_ago_eps") is not None:
            bits.append(f"same quarter last year {_money(nxt['year_ago_eps'])}")
        if bits:
            lines.append(bits[0][0].upper() + "; ".join(bits)[1:] + ".")
        if nxt.get("consensus_eps") is not None:
            lines.append("The price reaction tends to hinge on results versus this consensus and, often "
                         "more, on guidance for the next quarter.")
        lines.append("Dates from Nasdaq/Zacks can be estimates until the company confirms them; the "
                     "company's investor-relations page is the final word.")
        if nxt.get("days_until") is not None and nxt["days_until"] <= 10:
            lines.append("The report is close: near-dated option prices already include the expected "
                         "jump, and that extra premium tends to drain out right after the report.")
    elif result.get("date_status") == "passed":
        lines.append(f"**Next report: not announced yet.** Nasdaq still shows the past date "
                     f"{result.get('stale_listed_date')}.")
    elif result.get("date_status") == "none_listed":
        lines.append("**Next report: no upcoming date listed** by Nasdaq/Zacks.")
    else:
        lines.append("**Next report: unknown**, because the Nasdaq earnings date could not be fetched. "
                     "Check the company's investor-relations page.")
    if result.get("just_reported"):
        rel = result.get("latest_release") or {}
        lines.append(f"The company filed an earnings release {rel.get('days_since')} day(s) ago "
                     f"({rel.get('filed')}): it has **just reported**.")
    lines.append("")

    # Implied move
    mv = result.get("implied_move")
    if mv and mv["basis"] == "earnings":
        lines += ["### Implied earnings move", "",
                  f"Expiry used: **{mv['expiry']}** ({mv['dte']} DTE), the first expiry that "
                  "includes the reaction to the report"
                  + (" (an after-close report first trades the next session)."
                     if (nxt or {}).get("timing") == "after close" else "."), ""]
    elif mv:
        status = result.get("date_status")
        if status == "unavailable":
            why, what = "The earnings date is unknown", ("the expected range to that date. If a report "
                                                          "falls before this expiry, the move includes it.")
        elif status == "found":
            why = f"No listed expiry ends after the {(nxt or {}).get('date')} report yet"
            what = "the normal expected range before the report, not an earnings jump."
        else:
            why, what = "No upcoming earnings date", "the normal expected range, not an earnings jump."
        lines += ["### Front-expiry implied move (not an earnings move)", "",
                  f"{why}, so this is the front expiry **{mv['expiry']}** ({mv['dte']} DTE): it shows "
                  f"{what}", ""]
    else:
        lines += ["### Implied move", "", "Not available (see notes below).", ""]
    if mv:
        sign = "+" if (mv["iv_diff"] or 0) >= 0 else "-"
        diff = "n/a" if mv["iv_diff"] is None else f"{sign}{abs(mv['iv_diff']) * 100:.1f} pts"
        nxt_label = f"next expiry {mv['next_expiry']}" if mv.get("next_expiry") else "next expiry"
        lines += ["| Item | Value |", "|---|---|",
                  f"| Spot | {_money(mv['spot'])} |",
                  f"| ATM strike | {mv['atm_strike']:g} |",
                  f"| Call mid + put mid | {_money(mv['call_mid'])} + {_money(mv['put_mid'])} |",
                  f"| Straddle | {_money(mv['straddle'])} |",
                  f"| Implied move | +/-{mv['move_pct']:.2f}% (+/-{_money(mv['straddle'])}) |",
                  f"| Implied range | {_money(mv['range_low'])} to {_money(mv['range_high'])} |"]
        if mv.get("pre_expiry"):
            lines.append(f"| ATM IV, {mv['pre_expiry']} (ends before the report) | {_iv(mv.get('pre_atm_iv'))} |")
        lines += [f"| ATM IV, {mv['expiry']} | {_iv(mv['atm_iv'])} |",
                  f"| ATM IV, {nxt_label} | {_iv(mv.get('next_atm_iv'))} |",
                  f"| IV difference | {diff} |", ""]
        pct = f"{mv['move_pct']:.1f}"
        lines.append(f"Options are pricing about {_article(pct)} {pct}% move either way "
                     f"(roughly {_money(mv['range_low'])} to {_money(mv['range_high'])}) by {mv['expiry']}. "
                     "That is the market's estimate of size, not direction, and the actual move "
                     "can be larger or smaller.")
        if mv["basis"] == "earnings":
            if mv.get("mixes_ordinary_movement"):
                lines.append(f"Because this expiry is {mv['dte']} days away, that price also covers "
                             f"{mv['dte']} days of ordinary movement, not only the report, so the report's "
                             "own share is smaller. It becomes a cleaner read on the report in the last "
                             "few days before it, which is when it compares fairly with past "
                             "earnings-day moves.")
            if mv["iv_diff"] is not None:
                gap = f"{abs(mv['iv_diff']) * 100:.1f} points"
                if mv["iv_diff"] > 0:
                    lines.append(f"The earnings expiry's IV is {gap} above the next expiry's. Both expiries "
                                 "include the report, so both carry event premium; the nearer one shows "
                                 "more because the report's jump is packed into fewer days.")
                else:
                    lines.append(f"The earnings expiry's IV is {'equal to' if not mv['iv_diff'] else gap + ' below'} "
                                 "the next expiry's, so this comparison shows no clear event premium; that "
                                 "is common while the report is still weeks away and its jump is spread "
                                 "over many days.")
            if mv.get("pre_atm_iv") is not None and mv.get("atm_iv") is not None:
                lines.append(f"The {mv['pre_expiry']} expiry ends before the report and sits at "
                             f"{_iv(mv['pre_atm_iv'])} (vs {_iv(mv['atm_iv'])} for the earnings expiry), a "
                             "rough guide to the usual level. After the report, IV tends to fall back "
                             "toward that level (the \"IV crush\"), so option prices often drop even when "
                             "the stock moves.")
            else:
                lines.append("After the report, IV tends to fall back toward its usual level (the \"IV "
                             "crush\"), often by more than the gap to the next expiry, so option prices "
                             "often drop even when the stock moves.")
        elif mv["iv_diff"] is not None:
            lines.append("Front IV vs the next expiry shows whether near-term options are priced "
                         "richer (front higher) or calmer (front lower) than later ones.")
            if mv["iv_diff"] <= -0.05:
                lines.append(f"Here the {mv['next_expiry']} expiry's IV is "
                             f"{abs(mv['iv_diff']) * 100:.1f} points higher, which often means the market "
                             "expects an event (such as an earnings report) before that expiry.")
        lines.append("")

    # Surprise history
    hist = result.get("surprise_history") or []
    lines += [f"### EPS surprise history (last {len(hist) or HISTORY_QUARTERS} quarters)", ""]
    if hist:
        lines += ["| Fiscal quarter | Reported | Consensus EPS | Actual EPS | Surprise | Result |",
                  "|---|---|---|---|---|---|"]
        for h in hist:
            sp = "n/a" if h["surprise_pct"] is None else f"{h['surprise_pct']:+.2f}%"
            lines.append(f"| {h['fiscal_quarter'] or 'n/a'} | {h['reported'] or 'n/a'} | "
                         f"{_money(h['consensus_eps'])} | {_money(h['actual_eps'])} | {sp} | {h['result']} |")
        if result.get("quarters_compared"):
            lines += ["", f"Beat the consensus in {result.get('beats', 0)} of the last "
                          f"{result['quarters_compared']} quarters. A steady beat record tends to be "
                          "expected already, so the reaction often depends more on guidance and the call "
                          "than on the beat itself."]
        else:
            lines += ["", "No quarter has both a consensus and an actual EPS, so beats cannot be counted."]
    else:
        lines.append("Not available.")
    lines.append("")

    # Latest release
    lines += ["### Latest earnings release (SEC 8-K, item 2.02)", ""]
    rel = result.get("latest_release")
    if rel:
        ago = f", {rel['days_since']} days ago" if rel.get("days_since") is not None else ""
        link = f"[{rel['form']} filed {rel['filed']}]({rel['url']})" if rel.get("url") else \
            f"{rel['form']} filed {rel['filed']}"
        lines.append(f"- {link}{ago}; items {', '.join(rel['items'])}.")
        lines.append("- The press release is usually exhibit 99.1 in that filing's folder.")
    else:
        lines.append("Not available.")
    lines += ["", "### Earnings call", "", result.get("transcript_note") or TRANSCRIPT_NOTE, ""]

    errors = result.get("errors") or {}
    notes = result.get("notes") or []
    if errors or notes:
        lines += ["### Notes", ""]
        for key, msg in errors.items():
            lines.append(f"- {SOURCE_NAMES.get(key, key)} not available: {msg}")
        lines += [f"- {n}" for n in notes]
        lines.append("")

    lines.append("_Caveat: free data. Dates and consensus come from Nasdaq/Zacks and can be estimates; "
                 "consensus differs by source. Option prices are Cboe's 15-minute-delayed quotes, and mids "
                 "of wide bid/ask spreads are rough. The implied move describes what options price in, "
                 "not what will happen, and nothing here is a trade recommendation._")
    return "\n".join(lines) + "\n"

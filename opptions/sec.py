"""SEC EDGAR helpers: ticker -> CIK and a company's recent filings.

EDGAR asks every client to identify itself. Set OPPTIONS_SEC_UA to
"Your Name your@email" before running; a generic value is used otherwise.
https://www.sec.gov/os/accessing-edgar-data
"""

import os

from .http import fetch_json

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# What the common 8-K item numbers mean, in plain words.
EIGHT_K_ITEMS = {
    "1.01": "Entered a material agreement",
    "1.02": "Ended a material agreement",
    "1.03": "Bankruptcy or receivership",
    "1.05": "Material cybersecurity incident",
    "2.01": "Completed an acquisition or sale of assets",
    "2.02": "Earnings results",
    "2.03": "Took on a material debt obligation",
    "2.05": "Exit or restructuring costs (layoffs, closures)",
    "2.06": "Material impairment",
    "3.01": "Delisting notice or listing-rule failure",
    "3.02": "Unregistered sale of shares",
    "4.01": "Changed auditor",
    "4.02": "Past financials can no longer be relied on",
    "5.01": "Change in control",
    "5.02": "Executive or director change",
    "5.03": "Charter or bylaw amendment",
    "5.07": "Shareholder vote results",
    "7.01": "Regulation FD disclosure (often a presentation)",
    "8.01": "Other material event",
    "9.01": "Financial statements and exhibits",
}

# Forms worth flagging to a trader, beyond 8-Ks.
NOTABLE_FORMS = {
    "4": "Insider transaction",
    "144": "Insider intends to sell restricted shares",
    "S-3": "Shelf registration (can enable share offerings)",
    "S-3ASR": "Automatic shelf registration (can enable share offerings)",
    "424B5": "Prospectus for an offering (possible dilution)",
    "424B3": "Prospectus (possible offering)",
    "SC 13D": "Activist or >5% holder stake",
    "SC 13G": ">5% passive holder stake",
    # EDGAR's names for the same schedules since the XML switch in December 2024.
    "SCHEDULE 13D": "Activist or >5% holder stake",
    "SCHEDULE 13G": ">5% passive holder stake",
    "10-Q": "Quarterly report",
    "10-K": "Annual report",
    "8-K": "Current report",
    "DEF 14A": "Proxy statement",
}


def _headers():
    return {"User-Agent": os.environ.get("OPPTIONS_SEC_UA", "opptions research tool admin@example.com")}


def ticker_to_cik(symbol, tickers_json=None):
    """CIK int for a ticker, or None. Pass tickers_json to avoid a fetch (tests)."""
    table = tickers_json if tickers_json is not None else fetch_json(TICKERS_URL, _headers())
    sym = symbol.upper().replace(".", "-")
    for row in table.values():
        if str(row.get("ticker", "")).upper() == sym:
            return int(row["cik_str"])
    return None


def fetch_submissions(cik):
    return fetch_json(SUBMISSIONS_URL.format(cik=cik), _headers())


def recent_filings(submissions, limit=40):
    """Flatten EDGAR's column-oriented 'filings.recent' into a list of dicts, newest first."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    cik = int(submissions.get("cik") or 0)
    out = []
    for i, form in enumerate(forms[:limit]):
        def col(name):
            vals = recent.get(name) or []
            return vals[i] if i < len(vals) else ""
        acc = col("accessionNumber")
        doc = col("primaryDocument")
        items = [s.strip() for s in str(col("items")).split(",") if s.strip()]
        url = ""
        if cik and acc:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
        out.append({
            "form": form,
            "filed": col("filingDate"),
            "accepted": col("acceptanceDateTime"),  # EDGAR acceptance time string, "" if absent
            "items": items,
            "item_meanings": [EIGHT_K_ITEMS.get(it, it) for it in items],
            "description": NOTABLE_FORMS.get(form, ""),
            "url": url,
        })
    return out

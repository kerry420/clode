"""The scan universe: liquid US optionable tickers the desk scouts each morning.

DEFAULT_UNIVERSE is a hand-picked list of names with deep, active option
chains. It is not live data: liquidity drifts, so edit the list or point
OPPTIONS_UNIVERSE (or `scan --universe FILE`) at your own file.

File format, which also reads the project's watchlist.md:

    # Comments and markdown headings are skipped
    SPY - market backdrop       first token is the ticker; the note is ignored
    NVDA  AI bellwether         anything after whitespace is ignored too
    - TSLA                      bullets, numbered lists (1. TSLA) and task boxes are fine
    **AMZN** - note             bold or `code` around the ticker is fine
    | MSFT | note |             a markdown table row: the first cell is read
    AAPL, GOOGL, META           a comma list counts only when every item is a ticker
    amd                         a lowercase ticker alone (or with " - note") is uppercased
    One ticker per line.        prose is skipped: its first word is not a ticker

A ticker is 1 to 5 capital letters, optionally with a class suffix (BRK.B;
BRK-B and BRK/B are read as BRK.B). Duplicates are dropped and file order is
kept. Capitalized words that often open a sentence or a note in a trading
file (NOTE, TODO, IV, FOMC, ETF, AI, PM, US, ...; see _PROSE_WORDS) count
only alone on a line or followed by " - note", so "NOTE: ..." or "AI names
below" are not read as tickers while "AI - C3.ai" still is. A line whose
note ends in ":" with no dash ("SWING TRADES:") is read as a label, not a
ticker. Headings should start with "#".
"""

import os
import re

DEFAULT_UNIVERSE = (
    # Index ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Sector, rates, credit, commodity and country ETFs
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "SMH", "KRE",
    "XBI", "ARKK", "TLT", "HYG", "GLD", "SLV", "USO", "GDX", "EEM", "FXI",
    # Mega caps
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "NFLX", "ORCL",
    # Semis and software
    "AMD", "MU", "INTC", "SMCI", "ARM", "QCOM", "TSM", "MRVL", "CRM", "ADBE", "PLTR", "SHOP",
    # Crypto-linked and fintech
    "COIN", "MSTR", "HOOD", "SOFI", "PYPL", "MARA",
    # Consumer, internet and China ADRs
    "UBER", "DIS", "NKE", "SBUX", "WMT", "COST", "TGT", "HD", "MCD", "BABA", "PDD", "NIO", "RIVN",
    # Banks
    "JPM", "BAC", "C", "WFC", "GS",
    # Energy
    "XOM", "CVX", "OXY",
    # Health care
    "LLY", "UNH", "PFE", "MRNA",
    # Industrials and autos
    "BA", "CAT", "F", "GM",
)

ENV_VAR = "OPPTIONS_UNIVERSE"

_TICKER = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z]{1,2})?$")
_CLASS_SEP = re.compile(r"^([A-Za-z]{1,5})[-/]([A-Za-z]{1,2})$")  # BRK-B, BRK/B -> BRK.B
_LIST_MARK = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+")              # "- ", "* ", "1. ", "2) "
_TASK_BOX = re.compile(r"^\[[ xX]\]\s+")                            # "[ ] ", "[x] "
_NOTE_START = ("-", "\u2013", "\u2014", "(")                     # " - note", en/em dash, "(note)"
# Capitalized words that also open sentences or notes ("A note ...", "I think ...",
# "NOTE: ...", "IV is high", "AI names below"). Some are real tickers (AI, PM, ET),
# so they count only alone on a line or followed by " - note".
_PROSE_WORDS = frozenset({
    "A", "I", "AI", "AM", "PM", "ET", "US", "USA", "OK", "NB", "PS", "FYI", "TBD", "TODO",
    "NOTE", "NOTES", "EDIT", "UPDATE", "SYM", "IV", "OI", "DTE", "GEX", "ETF", "ETFS", "ATM",
    "ITM", "OTM", "EPS", "IPO", "FOMC", "CPI", "PPI", "PCE", "GDP", "FED", "SEC", "EOD",
    "ATH", "YTD",
})


def _ticker(token):
    """(ticker, was_lowercase) for one token such as 'NVDA', '$NVDA', '**NVDA**', 'NVDA:',
    'BRK-B' or 'amd'; None when the token is not shaped like a ticker."""
    t = token.strip("*_`$:,;-")
    m = _CLASS_SEP.match(t)
    if m:
        t = f"{m[1]}.{m[2]}"
    if _TICKER.match(t):
        return t, False
    if t.islower() and _TICKER.match(t.upper()):
        return t.upper(), True
    return None


def line_tickers(line):
    """Tickers on one universe/watchlist line, in order: usually zero or one, several
    for a comma list. Comments, headings, labels and prose give []."""
    s = line.strip()
    if not s or s.startswith("#"):
        return []
    table_row = s.startswith("|")
    if table_row:  # markdown table: the first cell, the other cells are the note
        s = s.strip("|").split("|", 1)[0].strip()
    s = _LIST_MARK.sub("", s, count=1)
    s = _TASK_BOX.sub("", s, count=1)
    s = s.split("#", 1)[0].strip()  # inline comment
    if not s:
        return []
    items = [p.strip() for p in s.split(",") if p.strip()]
    if len(items) > 1 and all(len(p.split()) == 1 for p in items):
        found = [_ticker(p) for p in items]
        if all(found) and not any(sym in _PROSE_WORDS for sym, _ in found):
            return [sym for sym, _ in found]
    parts = s.split(None, 1)
    found = _ticker(parts[0])
    if not found:
        return []
    sym, lowercase = found
    rest = parts[1].strip() if len(parts) > 1 else ""
    noted = table_row or not rest or rest.startswith(_NOTE_START)
    # Lowercase words and sentence openers count only alone or with a " - note",
    # so "note that ..." and "NOTE: ..." are not read as tickers.
    if (lowercase or sym in _PROSE_WORDS) and not noted:
        return []
    if not noted and not parts[0].endswith(":") and rest.endswith(":"):
        return []  # "SWING TRADES:" is a label
    return [sym]


def parse_line(line):
    """The first ticker on one universe/watchlist line, or None (see line_tickers)."""
    found = line_tickers(line)
    return found[0] if found else None


def parse_universe(text):
    """Tuple of unique tickers from file text, in order (see the module docstring)."""
    seen = {}
    for line in text.splitlines():
        for sym in line_tickers(line):
            seen.setdefault(sym, None)
    return tuple(seen)


def load_universe(path=None):
    """Tickers to scan.

    `path` if given (a missing file raises OSError); else the file named by env
    OPPTIONS_UNIVERSE when it points to an existing file; else DEFAULT_UNIVERSE.
    A file with no tickers in it raises ValueError rather than scanning nothing.
    """
    if path is None:
        env = os.path.expanduser(os.environ.get(ENV_VAR, "").strip())
        if env and os.path.isfile(env):
            path = env
    if path is None:
        return DEFAULT_UNIVERSE
    path = os.path.expanduser(str(path))
    with open(path, encoding="utf-8") as f:
        tickers = parse_universe(f.read())
    if not tickers:
        raise ValueError(f"no tickers found in {path}")
    return tickers

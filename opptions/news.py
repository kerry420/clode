"""News and SEC filings: what is new on a ticker, tagged by type and impact.

Free sources, no API key, each tried on its own so one outage does not
blank the section:

  * Yahoo Finance per-ticker RSS 2.0
      https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA&region=US&lang=en-US
    <item><title/><link/><guid isPermaLink="false"/>
          <pubDate>Tue, 30 Sep 2026 20:15:00 +0000</pubDate><description/></item>
    (Yahoo's newer feeds use an ISO 8601 pubDate and a <source url=".."> element;
    both forms parse.)
  * Google News RSS search
      https://news.google.com/rss/search?q=NVDA+stock&hl=en-US&gl=US&ceid=US:en
    <item><title>Headline text - Reuters</title>
          <link>https://news.google.com/rss/articles/...</link>
          <pubDate>Tue, 30 Sep 2026 21:04:00 GMT</pubDate>
          <source url="https://www.reuters.com">Reuters</source></item>
    The " - Publisher" suffix is split off the title into the source.
  * SEC EDGAR submissions JSON via sec.py (column arrays under filings.recent).

Headlines are tagged by keyword (word-boundary regexes on the title only) and
given an impact level. Two kinds of common noise are recognised and pushed to
low impact: law-firm "shareholder alert" ads and 13F fund-holding reports
("Argent Trust Co Buys 16,726 Shares of NVIDIA Corporation $NVDA"). A 13F
report needs one of that template's markers (exchange tag or cashtag, exact
share count, fund-manager name), so "Nvidia takes $5 billion stake in Intel"
stays high-impact news.

Times are kept in UTC in the JSON ("published") and shown in US Eastern with a
market-session label ("published_et", "session"), computed without a time-zone
database. When more than 40 headlines are in the window, high-impact ones are
kept first so a busy week cannot push out an older earnings or offering story.

analyze() is pure: no network, no clock. The window is measured back from
raw["fetched_at"], the only clock read, made in fetch().
"""

import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, quote_plus

from . import sec
from .http import FetchError, fetch_text

TITLE = "News and Filings"
YAHOO_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
GOOGLE_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
EDGAR_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}"
             "&type=&dateb=&owner=include&count=40")
INSIDER_URL = "https://www.sec.gov/cgi-bin/own-disp?action=getissuer&CIK={cik:010d}"

DEFAULT_DAYS = 7
MAX_ITEMS = 40
SEC_TABLE_ROWS = 25    # filings listed in the table (insider and bulk forms are only counted)
SOURCE_NAMES = {"yahoo": "Yahoo Finance RSS", "google": "Google News RSS", "sec": "SEC EDGAR"}
NEXT_STEP = "Next step: send this to the Explainer agent for a plain-English read."


def _rx(*parts):
    """One case-insensitive regex; each part must stand alone as a word or phrase."""
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.I)


_AMOUNT = r"\$?\d[\d,.]*\s*(?:million|billion|mln|bln|[mbk])?"

TAG_PATTERNS = {
    "earnings": _rx(
        r"earnings", r"eps",
        r"(?:quarterly|annual)\s+(?:results|report|revenue|sales|profit|loss)",
        r"q[1-4]\s+(?:results|revenue|sales|profit|loss|report|numbers)",
        r"(?:first|second|third|fourth)[- ]quarter\s+(?:results|revenue|sales|profit|loss)",
        r"(?:beats?|misses|missed|miss|tops|topped)\s+(?:\w+\s+){0,2}"
        r"(?:estimates|expectations|forecasts|consensus)",
    ),
    "guidance": _rx(
        r"guidance", r"pre-?announce\w*",
        r"(?:raises|raised|lifts|lifted|hikes|hiked|ups|boosts|boosted|cuts|cut|lowers|lowered|"
        r"slashes|slashed|trims|trimmed|withdraws|withdrew|pulls|suspends)\s+(?:its\s+|[\w-]+\s+){0,2}"
        r"(?:outlook|forecasts?|guidance|view)",
        # "maintains a bullish view" is analyst talk, so affirmations need outlook/forecast/guidance.
        r"(?:reaffirms|reaffirmed|reiterates|reiterated|maintains)\s+(?:its\s+|[\w-]+\s+){0,2}"
        r"(?:outlook|forecasts?|guidance)",
        r"(?:full[- ]year|annual|fiscal[- ]year|fy\d{0,4})\s+(?:outlook|forecast|guidance)",
        r"sees\s+(?:q[1-4]|fy\d{0,4}|full[- ]year|fiscal|(?:first|second|third|fourth)[- ]quarter)",
    ),
    "analyst": _rx(
        r"(?:up|down)grade[sd]?(?!\s+cycle)", r"price\s+targets?",
        r"(?:initiates|initiated|initiating|resumes|resumed|assumes|assumed)\s+coverage",
        r"coverage\s+initiated", r"initiated\s+(?:at|with)",
        r"reiterate[sd]?", r"reiterating", r"overweight", r"underweight",
        r"(?:buy|sell|hold|neutral|outperform|underperform)\s+rating",
    ),
    "m&a": _rx(
        r"acquir(?:e|es|ed|ing)", r"acquisitions?", r"mergers?", r"merges?", r"merged", r"merging",
        r"buyouts?", r"takeovers?", r"take-private", r"takes?\s+private",
        r"deal\s+to\s+(?:buy|acquire)", r"tender\s+offer",
        r"(?:agrees?|agreed|offers?|offered|bids?)\s+to\s+(?:buy|acquire|purchase)",
        r"to\s+be\s+(?:acquired|bought)",
        # Strategic or activist stakes ("Nvidia takes $5 billion stake in Intel").
        r"(?:takes?|took|buys?|bought|builds?|built|discloses?|disclosed|reveals?|revealed)\s+"
        r"(?:an?\s+)?(?:\$?\d[\d,.]*\s*(?:%|percent|million|billion|mln|bln|[mbk])?\s+)?(?:new\s+)?stakes?",
    ),
    "offering": _rx(
        r"(?:public|secondary|follow-on|stock|share|equity|debt|notes|bond|convertible|upsized|"
        r"proposed|underwritten|registered\s+direct|private|direct|overnight|block)\s+offerings?",
        r"offerings?\s+of\s+(?:\$?\d[\d,.]*|common|shares|ordinary|american\s+depositary|"
        r"senior|convertible|notes|units)",
        r"(?:prices|priced|pricing)\s+(?:[\w$.,-]+\s+){0,5}offerings?",
        r"\$\d[\d,.]*\s*(?:million|billion|mln|bln|[mbk])?\s+(?:[\w-]+\s+){0,3}offerings?",
        r"dilution", r"dilutive",
        r"convertible\s+(?:senior\s+)?(?:notes|bonds|debentures|debt|preferred)",
        r"at-the-market", r"atm\s+(?:program|offering|facility|equity|sales?)",
        r"shelf\s+registration", r"mixed\s+shelf",
        r"priced\s+(?:[\d,.]+\s+(?:million\s+)?)?shares",
    ),
    "legal/regulatory": _rx(
        r"lawsuits?", r"sues", r"sued", r"suing", r"class[- ]action",
        # "SEC" itself, but not "SEC Form 4" / "SEC filing" (see also ROUTINE_SEC below).
        r"sec(?!\s+(?:forms?|filings?|filed)(?!\w))",
        r"doj", r"justice\s+department", r"department\s+of\s+justice", r"ftc",
        r"probes?", r"probed", r"probing", r"investigations?", r"investigat(?:es|ed|ing)",
        r"recalls?", r"recalled", r"antitrust", r"subpoena(?:s|ed)?",
        r"regulators?", r"fined", r"indicted", r"indictment",
        r"export\s+(?:ban|bans|controls?|curbs?|restrictions?)",
    ),
    "fda": _rx(
        r"fda", r"pdufa", r"approvals?",
        r"(?:clinical\s+)?trials?\s+(?:results|data|readout)", r"study\s+(?:results|data)",
        r"top-?line", r"phase\s+(?:[123]|i{1,3})(?:/(?:[123]|i{1,3}))?[ab]?",
        r"complete\s+response\s+letter", r"clinical\s+hold", r"breakthrough\s+therapy",
    ),
    "product": _rx(
        r"launch(?:es|ed|ing)?", r"unveil(?:s|ed|ing)?", r"debut(?:s|ed)?",
        r"introduc(?:es|ed|ing)", r"rolls?\s+out", r"rolled\s+out", r"rollout",
        r"new\s+(?:product|model|chip|phone|device|vehicle|platform|service|feature|drug|line)s?",
        r"partnership", r"partners\s+with", r"teams\s+up", r"collaborat(?:es|ion)",
        # A contract win, not "50,000 call contracts" in an options-activity headline.
        r"(?<!options\s)(?<!option\s)(?<!call\s)(?<!put\s)(?<!futures\s)(?<!\d\s)contracts?",
    ),
    "management": _rx(
        r"ceo", r"cfo", r"coo", r"chief\s+(?:executive|financial|operating)",
        r"resign(?:s|ed|ation)?", r"appoint(?:s|ed|ment)?",
        r"steps?\s+down", r"stepping\s+down", r"stepped\s+down",
        r"successor", r"succession", r"ousted", r"chairman", r"chairwoman",
    ),
    "insider": _rx(
        r"insiders?", r"form\s+4", r"10b5-1",
        r"(?:buys|bought|sells|sold|purchases|purchased)\s+(?:" + _AMOUNT +
        r"\s+(?:in\s+|of\s+|worth\s+of\s+)?)?(?:shares|stock)",
    ),
    "macro": _rx(
        r"fed", r"federal\s+reserve", r"fomc", r"powell",
        r"cpi", r"ppi", r"pce", r"inflation", r"tariffs?",
        r"(?:interest|mortgage)\s+rates?", r"rates?\s+(?:cuts?|hikes?|decision|path)",
        r"(?:bond|treasury)\s+yields?", r"jobs\s+report", r"payrolls", r"recession",
    ),
}

# In "files 10-Q with the SEC" the word SEC is routine, so it is dropped before the
# legal/regulatory tag is checked (other words in the phrase still count).
ROUTINE_SEC = _rx(r"(?:files?|filed|filing)\s+(?:[\w/.-]+\s+){0,3}with\s+(?:the\s+)?sec")
RATING_CHANGE = _rx(r"(?:up|down)grade[sd]?(?!\s+cycle)")
HIGH_TAGS = ("earnings", "guidance", "m&a", "offering", "legal/regulatory", "fda")
MEDIUM_TAGS = ("analyst", "management", "product")

LAW_FIRM_AD = _rx(
    r"law\s+firm", r"shareholder\s+alert", r"investor\s+alert",
    r"shareholder\s+rights\s+(?:law|attorneys?|firm|litigation)",   # not "shareholder rights plan"
    r"(?:shareholders?|investors?)\s+(?:notice|reminder)", r"lead\s+plaintiff",
    r"class\s+action\s+(?:reminder|alert|deadline)", r"(?:investor|shareholder)\s+deadline",
    r"deadline\s+(?:alert|reminder)", r"investors?\s+(?:who|with)\s+(?:lost|losses)",
    # "Robbins LLP Reminds ACME Investors ...": up to four words between verb and investors.
    # ("urges shareholders" is left out: activists use it in real proxy fights.)
    r"(?:reminds|encourages|alerts|notifies|invites)\s+(?:[\w.&'$-]+\s+){0,4}"
    r"(?:investors|shareholders|stockholders)",
    r"on\s+behalf\s+of\s+(?:[\w.&-]+\s+){0,3}(?:investors|shareholders)",
    r"rosen\s+law", r"pomerantz", r"bragar", r"levi\s*&\s*korsinsky", r"faruqi",
    r"bronstein", r"kessler\s+topaz", r"glancy", r"schall\s+law", r"robbins\s+(?:geller|llp)",
    r"hagens\s+berman", r"portnoy", r"gross\s+law", r"bleichmar", r"kirby\s+mcinerney",
    r"johnson\s+fistel", r"gainey\s+mckenna", r"holzer\s*&\s*holzer", r"kahn\s+swick",
)
# 13F holding reports (MarketBeat and similar): "Argent Trust Co Buys 16,726 Shares of
# NVIDIA Corporation $NVDA", "NVIDIA Corporation $NVDA Shares Acquired by XYZ LLC".
FUND_HOLDING = _rx(
    r"(?:acquires|buys|sells|purchases|trims|raises|lifts|boosts|cuts|reduces|increases|"
    r"decreases|grows|lowers|takes|has|holds|establishes|initiates|invests|makes)\s+(?:a\s+)?"
    r"(?:new\s+)?(?:" + _AMOUNT + r"\s+)?(?:shares|stake|investment|(?:stock\s+)?(?:position|holdings))"
    r"\s+(?:of|in)",
    r"(?:shares|stake|(?:stock\s+)?(?:position|holdings))\s+(?:acquired|bought|purchased|sold|"
    r"trimmed|raised|lifted|boosted|cut|reduced|increased|decreased|lowered|grown)\s+by",
)
# The verb pattern alone also fits real news ("Nvidia takes $5 billion stake in Intel"), so
# a 13F report must also show one of the template's markers: an exchange tag or cashtag,
# an exact share count, or a fund-manager name.
FUND_MARKER = _rx(
    r"\((?:nasdaq|nyse|nysearca|nyseamerican|nysemkt|amex|otcmkts|otc|cboe|bats)\s*:\s*[a-z0-9.-]+\)",
    r"\$[a-z]{1,5}(?:\.[a-z])?",
    r"(?<![$.,\d])\d[\d,]*\s+shares",
    r"llc", r"l\.l\.c\.?", r"l\.p\.", r"lp", r"n\.a\.", r"ltd\.?", r"advisors", r"advisers",
    r"wealth", r"asset\s+management", r"capital\s+management", r"investment\s+management",
    r"family\s+office", r"retirement\s+system", r"pension",
)
INSIDER_WORDS = _rx(
    r"insiders?", r"ceo", r"cfo", r"coo", r"directors?", r"officers?", r"chair\w*",
    r"president", r"founder", r"evp", r"svp", r"executive", r"activist", r"13d",
)

TAG_MEANING = {
    "earnings": ("Earnings news tends to move a stock most when results differ from what was "
                 "expected, and implied volatility usually drops right after the report (IV crush)."),
    "guidance": ("A guidance change often matters more than the quarter itself, because it resets "
                 "what analysts expect for the months ahead."),
    "analyst": ("Upgrades and downgrades tend to move a stock for a day or two, more when the firm "
                "is large or the call is a surprise; a price-target change alone usually moves it less."),
    "m&a": ("In a takeover the target tends to jump toward the offer price and then trade in a "
            "narrow range, while the buyer often dips; a large strategic or activist stake tends to "
            "lift the company receiving it."),
    "offering": ("A share or convertible offering adds supply and can dilute holders, so the stock "
                 "often slips toward the offer price for a few days."),
    "legal/regulatory": ("Legal and regulatory news can weigh on a stock and keep implied volatility "
                         "elevated while the outcome is unknown; most cases resolve slowly."),
    "fda": ("FDA decisions and trial readouts are binary events: smaller biotech stocks can gap "
            "sharply either way, and implied volatility is usually high before the date."),
    "product": ("Product and contract news usually moves price less than earnings, unless it "
                "changes the revenue outlook."),
    "management": ("A surprise CEO or CFO exit tends to add uncertainty and can pressure the stock; "
                   "a planned succession usually matters less."),
    "insider": ("Insider buying is often read as confidence; insider selling is common (pay, taxes, "
                "pre-planned 10b5-1 plans) and usually says less."),
    "macro": ("Macro news (Fed, inflation data, tariffs, rates) moves the whole market, so the stock "
              "may follow its sector and the index more than its own story."),
    "law-firm ad": ("Law-firm 'shareholder alert' releases are advertisements that usually follow a "
                    "drop rather than cause one; they rarely move price on their own."),
    "fund filing": ("Fund-holding reports describe 13F filings that are weeks old; they rarely move "
                    "price."),
}

# --- SEC filings -----------------------------------------------------------------

DILUTION_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "F-1", "F-1/A", "F-3", "F-3/A", "F-3ASR"}
ACTIVIST_FORMS = {"SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"}
INSIDER_FORMS = {"3", "4", "4/A", "5", "144", "144/A"}
# Banks (GS, MS, JPM, C...) file hundreds of these a week for structured notes. They are
# counted, not listed, and a 424B2 is not called dilution: it is usually notes or debt.
BULK_FORMS = {"424B2", "FWP"}
HIGH_8K_ITEMS = {"1.01", "1.03", "1.05", "2.01", "2.02", "2.06", "3.01", "3.02", "4.02", "5.01", "5.02"}
EXTRA_FORMS = {
    "3": "Initial insider ownership report",
    "4/A": "Amended insider transaction",
    "5": "Annual insider transaction report",
    "8-K/A": "Amended current report",
    "6-K": "Current report from a foreign issuer",
    "20-F": "Annual report (foreign issuer)",
    "10-Q/A": "Amended quarterly report",
    "10-K/A": "Amended annual report",
    "S-1": "Registration for a share sale (possible dilution)",
    "S-1/A": "Amended registration for a share sale (possible dilution)",
    "S-3/A": "Amended shelf registration (can enable share offerings)",
    "F-3": "Shelf registration, foreign issuer (can enable share offerings)",
    "S-8": "Registers shares for employee stock plans (routine, small dilution)",
    "424B2": "Prospectus for notes or an offering (banks file many for structured notes)",
    "SC 13D/A": "Amended activist or >5% holder stake",
    "SCHEDULE 13D": "Activist or >5% holder stake",
    "SCHEDULE 13D/A": "Amended activist or >5% holder stake",
    "SC 13G/A": "Amended >5% passive holder stake",
    "SCHEDULE 13G": ">5% passive holder stake",
    "SCHEDULE 13G/A": "Amended >5% passive holder stake",
    "DEFA14A": "Additional proxy materials",
    "PRE 14A": "Preliminary proxy statement",
    "FWP": "Free writing prospectus (offering marketing material)",
}


def yahoo_url(symbol):
    return YAHOO_URL.format(sym=quote(symbol.upper(), safe=""))


def google_url(symbol):
    return GOOGLE_URL.format(q=quote_plus(f"{symbol.upper()} stock"))


def fetch(symbol):
    """Raw RSS text and EDGAR JSON. Raises FetchError only if all three sources failed."""
    sym = symbol.upper().strip()
    raw = {
        "symbol": sym,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "yahoo_rss": None,
        "google_rss": None,
        "sec_submissions": None,
        "errors": {},
    }
    for key, url in (("yahoo", yahoo_url(sym)), ("google", google_url(sym))):
        try:
            raw[f"{key}_rss"] = fetch_text(url, {"Accept": "application/rss+xml, application/xml, text/xml"})
        except FetchError as e:
            raw["errors"][key] = str(e)
    try:
        cik = sec.ticker_to_cik(sym)
        if cik is None:
            raw["errors"]["sec"] = f"no SEC CIK for {sym} in EDGAR's ticker list (ETF, fund or non-US listing?)"
        else:
            raw["sec_submissions"] = sec.fetch_submissions(cik)
    except FetchError as e:
        raw["errors"]["sec"] = str(e)
    if raw["yahoo_rss"] is None and raw["google_rss"] is None and raw["sec_submissions"] is None:
        detail = "; ".join(f"{k}: {v}" for k, v in raw["errors"].items())
        raise FetchError(f"no news source answered for {sym} ({detail})")
    return raw


# --- parsing -----------------------------------------------------------------------

def parse_date(text):
    """RFC 822 or ISO 8601 string -> aware UTC datetime, or None."""
    s = (text or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def split_publisher(title, source=""):
    """'Headline - Reuters' -> ('Headline', 'Reuters'). Only splits when a source is known
    or the suffix is short enough to be a publisher name."""
    if source and title.lower().endswith(" - " + source.lower()):
        return title[: -len(source) - 3].rstrip(), source
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if head and 0 < len(tail.split()) <= 6:
            return head.rstrip(), source or tail.strip()
    return title, source


def parse_rss(text, feed):
    """RSS 2.0 text -> list of {title, source, published (datetime|None), url, feed}.
    Raises ValueError when the text is not RSS."""
    try:
        root = ET.fromstring((text or "").lstrip("﻿ \t\r\n"))
    except ET.ParseError as e:
        raise ValueError(f"not valid RSS/XML ({e})") from e
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        if not title:
            continue
        src_el = it.find("source")
        source = _clean(src_el.text) if src_el is not None else ""
        if feed == "google":
            title, source = split_publisher(title, source)
        elif source and title.lower().endswith(" - " + source.lower()):
            title = title[: -len(source) - 3].rstrip()
        out.append({
            "title": title,
            "source": source or ("Yahoo Finance" if feed == "yahoo" else "Google News"),
            "published": parse_date(it.findtext("pubDate")),
            "url": _clean(it.findtext("link")),
            "feed": feed,
        })
    return out


def normalize_title(title, source=""):
    """Dedupe key: lowercase, publisher suffix and punctuation removed, spaces collapsed."""
    t = html.unescape(title or "").lower()
    if source and t.endswith(" - " + source.lower()):
        t = t[: -len(source) - 3]
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify_headline(title):
    """Keyword tags and impact for one headline: {"tags", "impact", "impact_reason"}."""
    legal_text = ROUTINE_SEC.sub(lambda m: m.group(0)[:-3], title)   # drop only the word "SEC"
    tags = [name for name, rx in TAG_PATTERNS.items()
            if rx.search(legal_text if name == "legal/regulatory" else title)]
    if LAW_FIRM_AD.search(title):
        return {"tags": tags + ["law-firm ad"], "impact": "low",
                "impact_reason": "law-firm solicitation, not company news"}
    if FUND_HOLDING.search(title) and FUND_MARKER.search(title) and not INSIDER_WORDS.search(title):
        tags = [t for t in tags if t not in ("m&a", "insider")]
        return {"tags": tags + ["fund filing"], "impact": "low",
                "impact_reason": "routine fund-holding (13F) report"}
    high = [t for t in HIGH_TAGS if t in tags]
    if RATING_CHANGE.search(title):
        high.append("upgrade/downgrade")
    if high:
        return {"tags": tags, "impact": "high", "impact_reason": ", ".join(high)}
    medium = [t for t in MEDIUM_TAGS if t in tags]
    if medium:
        return {"tags": tags, "impact": "medium", "impact_reason": ", ".join(medium)}
    return {"tags": tags, "impact": "low", "impact_reason": "no high- or medium-impact keyword"}


def dedupe(items):
    """Merge items whose normalized titles match. Keeps the earliest report (ties: first seen)
    and lists the other outlets in also_reported_by. Returns (kept, removed_count)."""
    kept, by_key, removed = [], {}, 0
    for item in items:
        key = normalize_title(item["title"], item["source"])
        prev = by_key.get(key)
        if prev is None:
            item = dict(item, also_reported_by=[])
            by_key[key] = item
            kept.append(item)
            continue
        removed += 1
        if item["published"] < prev["published"]:
            others = [prev["source"]] + prev["also_reported_by"]
            prev.update({k: item[k] for k in ("title", "source", "published", "url", "feed")})
            prev["also_reported_by"] = others
        else:
            prev["also_reported_by"].append(item["source"])
    for item in kept:
        others = []
        for s in item["also_reported_by"]:
            if s != item["source"] and s not in others:
                others.append(s)
        item["also_reported_by"] = others
    return kept, removed


def days_from_env():
    try:
        days = int(os.environ.get("OPPTIONS_NEWS_DAYS", DEFAULT_DAYS))
    except ValueError:
        return DEFAULT_DAYS
    return days if days > 0 else DEFAULT_DAYS


# --- SEC ---------------------------------------------------------------------------

def describe_form(form, description=""):
    if description:
        return description
    if form in EXTRA_FORMS:
        return EXTRA_FORMS[form]
    if form.startswith("424B"):
        return "Prospectus for an offering (possible dilution)"
    return ""


def classify_filing(f):
    """Add plain-language flags and an impact level to one recent_filings() row."""
    form = f["form"].upper()
    flags, impact = [], "low"
    if form in ("8-K", "8-K/A"):
        impact = "high" if HIGH_8K_ITEMS.intersection(f["items"]) else "medium"
        flags += [f"item {it}: {meaning}" for it, meaning in zip(f["items"], f["item_meanings"])
                  if it != "9.01"]
        if "3.02" in f["items"]:
            flags.append("possible dilution (unregistered share sale)")
    elif form in BULK_FORMS:
        impact = "medium"
    elif form in DILUTION_FORMS or form.startswith("424B"):
        impact = "high"
        flags.append("possible dilution")
    elif form in ACTIVIST_FORMS:
        impact = "high"
        flags.append("activist or >5% holder stake")
    elif form in ("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "6-K"):
        impact = "medium"
    return dict(f, description=describe_form(f["form"], f.get("description", "")),
                flags=flags, impact=impact)


def _dates(rows, n=3):
    dates = sorted({r["filed"] for r in rows}, reverse=True)
    return ", ".join(dates[:n]) + (f" and {len(dates) - n} more dates" if len(dates) > n else "")


def analyze_sec(submissions, since_date, error=None):
    """In-window filings from EDGAR submissions JSON, with counts and flags."""
    if not submissions:
        return {"available": False, "error": error}
    cik = int(submissions.get("cik") or 0)
    filings = submissions.get("filings") or {}
    # Scan every row of filings.recent: sec.recent_filings defaults to 40, which can be
    # less than one week for a heavy filer.
    n_recent = len((filings.get("recent") or {}).get("form") or [])
    scanned = sec.recent_filings(submissions, limit=n_recent)
    rows = [classify_filing(f) for f in scanned if str(f.get("filed", "")) >= since_date]
    rows.sort(key=lambda r: r["filed"], reverse=True)
    coverage_note = ""
    oldest = min((str(f.get("filed") or "") for f in scanned), default="")
    if scanned and oldest >= since_date and filings.get("files"):
        coverage_note = (f"EDGAR's recent-filings list only reaches back to {oldest}; earlier filings "
                         "in this window are in EDGAR's older pages and are not counted here.")

    counts = {}
    for r in rows:
        counts[r["form"]] = counts.get(r["form"], 0) + 1
    upper = [(r, r["form"].upper()) for r in rows]
    eight_ks = [r for r, f in upper if f in ("8-K", "8-K/A")]
    dilution = [r for r, f in upper if f not in BULK_FORMS and (
                f in DILUTION_FORMS or f.startswith("424B")
                or (f in ("8-K", "8-K/A") and "3.02" in r["items"]))]
    activist = [r for r, f in upper if f in ACTIVIST_FORMS]
    form4 = sum(1 for _, f in upper if f in ("4", "4/A"))
    form144 = sum(1 for _, f in upper if f in ("144", "144/A"))
    bulk = {}
    for r, f in upper:
        if f in BULK_FORMS:
            bulk.setdefault(f, []).append(r)
    listed = [r for r, f in upper if f not in INSIDER_FORMS and f not in BULK_FORMS]

    flags = []
    for r in eight_ks:
        meanings = [m for it, m in zip(r["items"], r["item_meanings"]) if it != "9.01"] or ["exhibits only"]
        flags.append(f"{r['form']} filed {r['filed']}: " + "; ".join(meanings))
    by_form = {}
    for r in dilution:
        by_form.setdefault(r["form"], []).append(r)
    for form, rs in by_form.items():
        flags.append(f"Possible dilution: {form} x{len(rs)} (filed {_dates(rs)})")
    for r in activist:
        flags.append(f"Activist or >5% stake: {r['form']} filed {r['filed']}")
    if "424B2" in bulk:
        rs = bulk["424B2"]
        flags.append(f"424B2 prospectus supplements x{len(rs)} (filed {_dates(rs)}): usually notes or "
                     "debt, not new shares; open one to check")
    if "FWP" in bulk:
        rs = bulk["FWP"]
        flags.append(f"Free writing prospectus x{len(rs)} (filed {_dates(rs)}): offering marketing material")
    if form4:
        flags.append(f"{form4} Form 4 insider transaction filing{'s' if form4 != 1 else ''}")
    if form144:
        flags.append(f"{form144} Form 144 notice{'s' if form144 != 1 else ''} of planned insider sales")

    return {
        "available": True,
        "company": submissions.get("name") or "",
        "cik": cik or None,
        "since": since_date,
        "filings_in_window": len(rows),
        "form_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "form4_count": form4,
        "form144_count": form144,
        "bulk_counts": {f: len(rs) for f, rs in sorted(bulk.items())},
        "coverage_note": coverage_note,
        "eight_k_count": len(eight_ks),
        "dilution_count": len(dilution),
        "activist_count": len(activist),
        "eight_k_items": sorted({it for r in eight_ks for it in r["items"]}),
        "flags": flags,
        "filings": listed[:SEC_TABLE_ROWS],
        "filings_not_listed": max(0, len(listed) - SEC_TABLE_ROWS),
        "edgar_url": EDGAR_URL.format(cik=cik) if cik else "",
        "insider_url": INSIDER_URL.format(cik=cik) if cik else "",
    }


# --- analyze -------------------------------------------------------------------------

def _fallback_reference(items, submissions):
    """Newest headline or filing time, used only when fetched_at is missing."""
    times = [i["published"] for i in items]
    filed = (((submissions or {}).get("filings") or {}).get("recent") or {}).get("filingDate") or []
    times += [d for d in (parse_date(f"{f}T00:00:00Z") for f in filed if isinstance(f, str) and f) if d]
    return max(times) if times else None


def to_eastern(dt):
    """Aware datetime -> aware US Eastern datetime (EST/EDT by the US rules in force since 2007).

    Computed by hand so it works on systems without a time-zone database."""
    utc = dt.astimezone(timezone.utc)
    y = utc.year
    mar1, nov1 = datetime(y, 3, 1).weekday(), datetime(y, 11, 1).weekday()
    dst_start = datetime(y, 3, 8 + (6 - mar1) % 7, 7, tzinfo=timezone.utc)   # 2nd Sun Mar, 2:00 EST
    dst_end = datetime(y, 11, 1 + (6 - nov1) % 7, 6, tzinfo=timezone.utc)    # 1st Sun Nov, 2:00 EDT
    hours = -4 if dst_start <= utc < dst_end else -5
    return utc.astimezone(timezone(timedelta(hours=hours)))


def market_session(et):
    """'pre-market', 'market hours', 'after hours' or 'weekend' for a US Eastern datetime.
    Exchange holidays are not known here."""
    if et.weekday() >= 5:
        return "weekend"
    minutes = et.hour * 60 + et.minute
    if minutes < 9 * 60 + 30:
        return "pre-market"
    return "market hours" if minutes < 16 * 60 else "after hours"


IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}


def _pick(classified, cap):
    """Up to `cap` items, newest first. When there are more, high-impact items are kept first,
    then medium, then the newest low-impact ones, so a busy week cannot push out the earnings
    report or an offering from five days ago."""
    if len(classified) <= cap:
        return classified
    chosen = sorted(classified, key=lambda pc: (IMPACT_RANK[pc[1]["impact"]], -pc[0]["published"].timestamp()))
    chosen = chosen[:cap]
    return sorted(chosen, key=lambda pc: pc[0]["published"], reverse=True)


def analyze(raw, days=None):
    """Pure: tagged, deduped headlines in the window plus SEC filings. JSON-serializable.

    impact_counts cover every in-window headline; items holds at most MAX_ITEMS of them."""
    days = days or days_from_env()
    sym = (raw.get("symbol") or "").upper()
    errors = dict(raw.get("errors") or {})
    notes = []

    parsed, feed_counts = [], {}
    for feed in ("yahoo", "google"):
        text = raw.get(f"{feed}_rss")
        if text is None:
            continue
        try:
            rows = parse_rss(text, feed)
        except ValueError as e:
            errors.setdefault(feed, f"response was {e}")
            continue
        feed_counts[feed] = len(rows)
        parsed += rows

    dated = [p for p in parsed if p["published"] is not None]
    undated = len(parsed) - len(dated)
    items, duplicates = dedupe(dated)

    ref = parse_date(raw.get("fetched_at"))
    if ref is None:
        ref = _fallback_reference(items, raw.get("sec_submissions"))
        if ref is not None:
            notes.append("fetched_at was missing, so the window is measured back from the newest "
                         "headline or filing.")
    cutoff = ref - timedelta(days=days) if ref else None
    in_window = [i for i in items if cutoff is None or i["published"] >= cutoff]
    in_window.sort(key=lambda i: i["published"], reverse=True)
    outside = len(items) - len(in_window)

    classified = [(i, classify_headline(i["title"])) for i in in_window]
    impact_counts = {"high": 0, "medium": 0, "low": 0}
    for _, c in classified:
        impact_counts[c["impact"]] += 1

    out_items = []
    for i, c in _pick(classified, MAX_ITEMS):
        et = to_eastern(i["published"])
        out_items.append({
            "title": i["title"],
            "source": i["source"],
            "published": i["published"].isoformat(),
            "published_et": et.isoformat(),
            "session": market_session(et),
            "url": i["url"],
            "tags": c["tags"],
            "impact": c["impact"],
            "impact_reason": c["impact_reason"],
            "feed": i["feed"],
            "also_reported_by": i["also_reported_by"],
        })

    tag_counts = {}
    for i in out_items:
        for t in i["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    since_date = cutoff.date().isoformat() if cutoff else "0000-00-00"
    sec_result = analyze_sec(raw.get("sec_submissions"), since_date, errors.get("sec"))

    return {
        "symbol": sym,
        "fetched_at": raw.get("fetched_at"),
        "window_days": days,
        "window_start": cutoff.isoformat() if cutoff else None,
        "window_start_et": to_eastern(cutoff).isoformat() if cutoff else None,
        "sources": {"yahoo": yahoo_url(sym), "google": google_url(sym)} if sym else {},
        "sources_ok": [k for k in ("yahoo", "google") if k in feed_counts]
                      + (["sec"] if sec_result["available"] else []),
        "errors": errors,
        "notes": notes,
        "counts": {
            "parsed": feed_counts,
            "undated_dropped": undated,
            "duplicates_merged": duplicates,
            "outside_window": outside,
            "in_window": len(in_window),
            "shown": len(out_items),
        },
        "impact_counts": impact_counts,
        "tag_counts": dict(sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "items": out_items,
        "sec": sec_result,
    }


# --- markdown ------------------------------------------------------------------------

def _when(iso):
    return (iso or "")[:16].replace("T", " ")


def _when_et(item):
    """'2026-09-30 16:15 ET (after hours)'; falls back to UTC for results without ET fields."""
    if item.get("published_et"):
        session = f" ({item['session']})" if item.get("session") else ""
        return f"{_when(item['published_et'])} ET{session}"
    return f"{_when(item.get('published'))} UTC"


def _md(text):
    """Safe inside a table cell and a link label."""
    return (text or "").replace("|", "\\|").replace("[", "(").replace("]", ")").replace("\n", " ")


def _link(title, url):
    return f"[{_md(title)}]({url.replace(')', '%29').replace(' ', '%20')})" if url else _md(title)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _sec_markdown(s, symbol):
    lines = ["### SEC filings (EDGAR)", ""]
    if not s.get("available"):
        lines.append(f"_Not available: {s.get('error') or 'no EDGAR data'}._")
        return lines + [""]
    who = s.get("company") or symbol
    cik = f" (CIK {s['cik']})" if s.get("cik") else ""
    n = s.get("filings_in_window", 0)
    lines.append(f"**{_plural(n, 'filing')} since {s['since']}** for {who}{cik}.")
    if s.get("coverage_note"):
        lines.append(f"_{s['coverage_note']}_")
    if not n:
        lines += ["Filings are the primary source, so a quiet EDGAR feed usually means no hidden "
                  "corporate event in this window.", ""]
        return lines
    lines.append("")
    for flag in s.get("flags") or []:
        lines.append(f"- {flag}")
    lines.append("")
    if s.get("dilution_count"):
        lines.append("Offerings add new shares; stocks often drift lower toward the offer price for a few "
                     "days, though a well-received raise can be absorbed quickly.")
    if (s.get("bulk_counts") or {}).get("424B2"):
        lines.append("Large banks file 424B2s almost daily for structured notes, so a 424B2 count by itself "
                     "usually says little about the stock price.")
    if s.get("activist_count"):
        lines.append("A 13D means a holder above 5% may push for changes; stocks often rise on activist "
                     "news and volatility tends to stay elevated.")
    if "5.02" in (s.get("eight_k_items") or []):
        lines.append("An executive change adds uncertainty; a sudden CFO exit tends to worry the market "
                     "more than a planned retirement.")
    if "2.02" in (s.get("eight_k_items") or []):
        lines.append("Item 2.02 is the earnings release itself; the price reaction usually depends on "
                     "guidance and on how results compare with expectations.")
    if s.get("form4_count"):
        lines.append(f"Form 4s do not say buy or sell in this feed; check {s.get('insider_url')}. Several "
                     "insiders buying on the open market tends to be read as confidence; sales usually say less.")
    if s.get("form144_count"):
        lines.append("A Form 144 announces an intended insider sale; it is a notice, not proof the sale happened.")
    rows = s.get("filings") or []
    if rows:
        lines += ["", "| Filed | Form | What it means | Link |", "|---|---|---|---|"]
        for r in rows:
            meaning = r.get("description") or ""
            new = [f for f in r.get("flags") or [] if f.lower() not in meaning.lower()]
            if new:
                meaning = (meaning + ": " if meaning else "") + "; ".join(new)
            lines.append(f"| {r['filed']} | {_md(r['form'])} | {_md(meaning)} | "
                         f"{'[filing](' + r['url'] + ')' if r.get('url') else ''} |")
        if s.get("filings_not_listed"):
            lines.append(f"\n_{s['filings_not_listed']} more filings not listed; see {s.get('edgar_url')}._")
    lines.append("")
    return lines


def to_markdown(result):
    """Readable news section for a person learning, and for an LLM agent."""
    sym = result.get("symbol") or "?"
    lines = [f"## {TITLE}: {sym}", ""]
    counts = result.get("counts") or {}
    ic = result.get("impact_counts") or {}
    days = result.get("window_days", DEFAULT_DAYS)
    items = result.get("items") or []
    total = counts.get("in_window", len(items))
    if result.get("window_start_et"):
        since = f"since {_when(result['window_start_et'])} ET"
    elif result.get("window_start"):
        since = f"since {_when(result['window_start'])} UTC"
    else:
        since = "no reference time, so every dated item is kept"

    parsed = counts.get("parsed") or {}
    feeds = ", ".join(f"{SOURCE_NAMES.get(k, k)} ({_plural(v, 'item')})" for k, v in parsed.items()) or "none"
    extra = []
    if counts.get("duplicates_merged"):
        extra.append(f"{_plural(counts['duplicates_merged'], 'duplicate')} merged")
    if counts.get("outside_window"):
        extra.append(f"{_plural(counts['outside_window'], 'older item')} left out")
    if counts.get("undated_dropped"):
        extra.append(f"{_plural(counts['undated_dropped'], 'undated item')} left out")
    lines.append(f"**{_plural(total, 'headline')} in the last {days} days** ({since}): "
                 f"{ic.get('high', 0)} high, {ic.get('medium', 0)} medium, {ic.get('low', 0)} low impact. "
                 f"Feeds: {feeds}" + (f"; {', '.join(extra)}." if extra else "."))
    if total > len(items):
        lines.append(f"Showing {len(items)} of {total}: every high-impact headline is kept first, then "
                     "medium, then the newest low-impact ones.")
    if ic.get("high", 0) >= 3:
        lines.append("A week with several high-impact headlines tends to bring bigger moves and higher "
                     "implied volatility than usual.")
    elif ic.get("high", 0):
        lines.append("One or two high-impact headlines can move the stock and its implied volatility; "
                     "the rest of the list is usually background.")
    elif items:
        lines.append("No high-impact headline this window; with quieter news, the stock often trades with "
                     "its sector and the market.")
    else:
        lines.append("No headlines in this window. Quiet news flow usually means price follows the market, "
                     "the sector and chart levels.")
    for key, msg in (result.get("errors") or {}).items():
        lines.append(f"_Missing source: {SOURCE_NAMES.get(key, key)} ({msg}). Coverage is thinner than usual._")
    for note in result.get("notes") or []:
        lines.append(f"_{note}_")
    lines.append("")

    high = [i for i in items if i["impact"] == "high"]
    lines += [f"### High impact ({len(high)})", ""]
    if high:
        for i in high:
            also = f" (also: {', '.join(i['also_reported_by'])})" if i.get("also_reported_by") else ""
            lines.append(f"- {_when_et(i)} · {_md(i['source'])} · "
                         f"{i['impact_reason']} · {_link(i['title'], i['url'])}{also}")
        lines += ["", "These are the headlines most likely to move the stock and its implied volatility; "
                      "the size of the move tends to depend on how far the news is from what was expected.", ""]
    else:
        lines += ["None in this window by keyword. That usually means no scheduled catalyst hit the "
                  "headlines, not that nothing can happen.", ""]

    if items:
        lines += ["### Headlines (newest first)", "",
                  "| When (ET) | Source | Impact | Tags | Headline |", "|---|---|---|---|---|"]
        for i in items:
            lines.append(f"| {_when_et(i).replace(' ET', '')} | {_md(i['source'])} | {i['impact']} | "
                         f"{_md(', '.join(i['tags'])) or '-'} | {_link(i['title'], i['url'])} |")
        lines.append("")
        if any(i.get("session") and i["session"] != "market hours" for i in items):
            lines += ["News that lands after hours, before the open or on a weekend tends to show up as a "
                      "gap at the next open rather than as a move during the session. Exchange holidays are "
                      "not marked.", ""]
        present = [t for t in TAG_MEANING if t in (result.get("tag_counts") or {})]
        if present:
            lines += ["### What these tags tend to mean", ""]
            for t in present:
                lines.append(f"- **{t}** ({result['tag_counts'][t]}): {TAG_MEANING[t]}")
            lines.append("")

    lines += _sec_markdown(result.get("sec") or {}, sym)

    lines += ["### Caveats", "",
              "Free RSS headlines and EDGAR data, not a paid news feed: stories can arrive late, paywalled "
              "outlets are thin, and Google links are redirects. Tags and impact come from keywords in the "
              "headline only, so read the story before trusting a tag. Times are US Eastern; EDGAR filing "
              "dates are dates only. Nothing here is a trade signal.",
              "", NEXT_STEP]
    return "\n".join(lines) + "\n"

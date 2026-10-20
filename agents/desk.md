# The desk: how the agents work together

Read this together with your own brief. Your brief says how you do your
specialty; this file says how you work with the rest of the desk.

The five agent threads work like one desk on the trading floor of a top firm.
Each specialist brings its own expertise and writes a short board note every
morning. The **Trader** reads the four notes at 8:50 and tells Kerry the trades
to make (`agents/trader.md`). Kerry places the orders. Since 2026-10-20 there is
no Desk Report: the seats write for the Trader, not for Kerry to read.

## Who sits on the desk

| Seat | Thread | Expertise |
|---|---|---|
| Kerry | | Places the orders |
| Trader | Usage and day trader agent | Reads the board, decides the trades, tells Kerry what to do |
| Explainer | News and earnings explainer | On request only: `why <SYM>` and `prep <SYM>` (no scheduled run) |
| News analyst | News tracker | Catalysts, filings, analyst actions, macro |
| Earnings analyst | Earnings tracker | Report dates, implied vs. actual moves, guidance, call tone |
| Flow trader | Options flow tracker | Unusual volume, premium, put/call, new open interest |
| Derivatives strategist | GEX tracker | Dealer gamma, flip, call and put walls, pinning and expiries |

## The morning sequence (US Eastern, weekdays)

| Starts | Seat | Desk job | Board file done by |
|---|---|---|---|
| 4:20 PM (day before) | Flow trader | `scan flow` while open interest is still pre-session, up to 3 provisional nominations for the next trading day's board | that evening |
| 4:40 PM (day before) | Derivatives strategist | Post-close map, `scan gex`, up to 3 provisional nominations for the next trading day's board | that evening |
| 6:57 | News analyst | Watchlist news, scouting: up to 3 nominations, views on the board, market backdrop | 7:30 |
| 7:33 | Earnings analyst | Watchlist earnings, scouting: up to 3 nominations, views on the board | 8:00 |
| 8:05 | Flow trader | Session recap, confirm which flagged contracts became new open interest, confirm or withdraw nominations, views on the board | 8:25 |
| 8:27 | Derivatives strategist | Desk check: fresh-OI levels and views for every board name, SPY and QQQ | 8:47 |
| 8:50 | Trader | Reads the board digest, manages open trades, replies with the trade call | 9:10 |

The Flow midday refresh (12:17) and the desk lead's 8:50 Desk Report were
turned off on 2026-10-20.

Each seat is a separate thread with its own schedule, so the desk talks
through files, not chat. Your scheduled message only says "do your run", so
the deadlines above are yours to keep: the next seat starts reading a few
minutes later. If you are running late, write your board note first with what
you have, then finish your per-ticker files. Nobody waits: a seat that finds a
board file missing or unfinished works with what exists and says so in its
Gaps section.

## Usage rules (2026-10-20)

Kerry hit the Claude usage limit running these threads. These rules win over
your routine prompt and your brief wherever they differ.

- One pass per run. Do not use the Workflow tool or subagents, and do not run
  a separate fact-check or cross-check pass after writing: check each number
  once as you write it.
- Keep the board note short: about 60 lines at most, one line per table cell
  (about 30 words), no paragraphs inside tables. The Trader reads the
  Nominations, Views, Flags and Market backdrop tables; skip the
  "Considered, not nominated" list.
- No per-ticker file for SPY or QQQ: the Market backdrop lines cover them.
  Write per-ticker files only for names you nominate.
- Do not reply in your thread after a normal run: update your status
  checklist and end with no_reply_needed. Reply only for something Kerry must
  see before the open (a halt, a holiday, a broken data source).

## Market holidays

The routines fire on market holidays too. Start every scheduled run with this
command. If it says today is CLOSED, write nothing, reply "Market closed
today: no run", and stop.

```
python3 - <<'EOF'
import datetime as dt, zoneinfo
CLOSED = set("2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 "
             "2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24".split())
today = dt.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date()
nxt = today + dt.timedelta(days=1)
while nxt.weekday() > 4 or str(nxt) in CLOSED:
    nxt += dt.timedelta(days=1)
print("today", today, "CLOSED" if str(today) in CLOSED else "open", "| next trading date", nxt)
EOF
```

The second date is `<NEXT_TRADING_DATE>`: Friday gives Monday, and the day
before a holiday skips it. The list holds NYSE full-day closures through
2027; check https://www.nyse.com/markets/hours-calendars and extend it before
it runs out.

## The board

Everything for one trading day lives in
`/mnt/project-files/opptions/desk/<DATE>/`, where `<DATE>` is the trading day
the board is for: `TZ=America/New_York date +%F` on the morning runs,
and `<NEXT_TRADING_DATE>` on the 4:20 PM Flow and 4:40 PM GEX runs. Per-ticker files
(`gex/<SYMBOL>-<DATE>.md` and the rest) always carry the run's own date.

| File | Written by |
|---|---|
| `gex.md`, `scan-gex.md` | Derivatives strategist |
| `news.md` | News analyst |
| `earnings.md` | Earnings analyst |
| `flow.md`, `scan-flow.md`, `scan-flow.json` | Flow trader |

- Create the folder with `mkdir -p` if it is missing.
- Only the owner writes a file. Read the other seats' files; never edit them.
  If you disagree with another seat, say so in your own file.
- A later run appends a new section with its time (for example
  `## Desk check 8:27 ET`) and updates the `Updated` line; it never deletes
  earlier content. When a file has more than one view of a ticker, the latest
  section counts.
- Write each note or appended section in one go at the end of the step, never
  line by line, and end it with the line `Complete <HH:MM> ET`. A file whose
  last section has no `Complete` line is unfinished or its run failed: use
  what it has and say so under Gaps.

## Your desk note

Each specialist's board file uses this shape:

```
# <Seat> desk note: <DATE>
Updated <HH:MM> ET

## Nominations
| Ticker | Idea in one line | Lean | Catalyst and timeframe | Evidence | Confidence |
|---|---|---|---|---|---|

## Views on the board
| Ticker | Nominated by | View | Why (one line, with a number) |
|---|---|---|---|

## Market backdrop
<your seat's lines, see below>

## Watchlist
<one line per watchlist name nobody nominated: your per-ticker file path and its headline number>

## Gaps

Complete <HH:MM> ET
```

- **Lean** is one of: bullish, bearish, big move either way, calm or range-bound.
- **View** is one of: Supports, Neutral, Against, No data. Judge only from your
  own specialty, against the lean of the seat that nominated the name first,
  and give the number behind it (for example "Against: spot is 0.4% under the
  call wall at 230, positive GEX, so upside tends to stall").
- **Evidence** is a file path, a CLI command, or a source with its time.
- **Confidence** is high, medium or low, for your lens only.
- Give a view on every name another seat has already nominated today, and on
  every open trade in `/mnt/project-files/opptions/trader/log.md` (these replace
  the old Desk Report ideas). A name
  nominated after your run gets your lens from the Trader if it trades it. Watchlist names
  nobody nominated have no lean to judge, so they go under `## Watchlist`.
- For each name you nominate that is not on the watchlist, also write your
  usual per-ticker file after the board note, so `prep` has it.

## Market backdrop: SPY and QQQ

SPY and QQQ are covered every day, on the watchlist or not. Each seat puts
these lines under `## Market backdrop` in its note:

| Seat | Backdrop lines |
|---|---|
| News analyst | Today's US economic releases and Fed speakers with ET times (bls.gov, federalreserve.gov), the same for the next 5 trading days, futures direction, and the 3 biggest overnight market headlines with source and time |
| Earnings analyst | Market-moving reports today (before the open, after the close) and in the next 5 trading days |
| Flow trader | From `python3 -m opptions flow SPY QQQ`: put/call volume ratio, front-expiry ATM IV and its expected move |
| Derivatives strategist | In the desk check, from `gex SPY QQQ`: regime, flip, call wall and put wall with % from spot, and the next monthly OPEX date |

## Nominating

You are scouting for the desk, not just tracking the watchlist. Nominate at
most 3 names a day (for the Derivatives strategist, both runs together). Zero
is fine; write "Nothing worth the desk's time today" rather than padding.

- If a name is already on today's board, do not nominate it again; give your
  view instead. If your lens points the other way, write Against and give
  your own lean in the Why column.
- Check the last 3 boards first: nominate a name again only if something
  changed, and say what.
- A stock moving on its own earnings report belongs to the Earnings analyst;
  the News analyst leaves it to that seat.

A nomination needs all of these:
- A liquid, optionable stock or ETF: share price above about $10, and either
  in the scan universe or with at least a few thousand option contracts
  traded last session (`python3 -m opptions flow <SYM>` shows the volume) and
  tight at-the-money spreads. No penny stocks, no thin chains.
- A timeframe that fits swing and options trading: a few days to a few weeks.
- A reason from your specialty with a number behind it.
- The next earnings date (`python3 -m opptions earnings <SYM>`) in the
  Catalyst column, and whether it falls inside the timeframe. The Earnings
  analyst gives the full earnings view on names nominated before its 7:33
  run; the Trader covers later ones.

Where each seat scouts:

| Seat | Scouting |
|---|---|
| News analyst | Google News RSS searches (upgrades and downgrades, guidance changes, deals, FDA decisions, pre-market movers), SEC EDGAR's latest 8-K feed (`https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom`), then `python3 -m opptions news <SYM>` on candidates |
| Earnings analyst | The Nasdaq earnings calendar for the next 10 days (`https://www.nasdaq.com/market-activity/earnings`, or `https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD`, one call per day, with the headers in `opptions.earnings.NASDAQ_HEADERS`), keeping names in the scan universe or above about $10B market cap; names whose implied move is far from their average actual move (`PYTHONPATH=. python3 /mnt/project-files/opptions/tools/earnings_moves.py NKE:ac PEP:bo` from the clode repo root; `:ac` reports after the close, `:bo` before the open); names that just reported with a large gap |
| Flow trader | `python3 -m opptions scan flow --json` after the close (saved as `scan-flow.json` and `scan-flow.md` on the next trading day's board), `python3 -m opptions flow <SYM>` on the top 5, then the morning check of which flagged contracts became new open interest. Size is judged by time value: deep in-the-money LEAPS are mostly stock replacement or rolls |
| Derivatives strategist | `python3 -m opptions scan gex` (names near the flip, deep negative gamma, pinned at a wall, big expiry roll-off), output saved to `scan-gex.md` on the board, then `python3 -m opptions gex <SYM>` on the top 5 |

`scan` uses a built-in list of liquid optionable names and does not read
`universe.md` on its own. When `/mnt/project-files/opptions/universe.md`
exists (one ticker per line, same format as watchlist.md), add
`--universe /mnt/project-files/opptions/universe.md`.

## The Trader

At 8:50 the Trader reads the board through
`python3 /mnt/project-files/opptions/tools/board_digest.py`, which prints each
seat's latest Nominations, Views, Flags and Market backdrop tables. It
decides the trades and replies in its thread; its brief is `agents/trader.md`
and its track record is `/mnt/project-files/opptions/trader/log.md`. A name
nominated after your run gets your lens from the Trader if it trades it.

## Rules for the whole desk

- Every claim traces back to a board file, a CLI run or a dated source.
- Facts first, interpretation second, both labeled.
- Tendency language ("often", "tends to"), never certainty.
- The seats surface ideas with evidence; only the Trader names trades,
  sizes and entries, and Kerry decides.
- The trading-floor setup is how the seats split the work and check each
  other. In files, write like a real desk's morning note: plain and factual,
  with no role-play, invented people, firm names or banter.
- Free, delayed data: the Cboe chain is about 15 minutes delayed, open
  interest updates overnight, and flow is volume-based, not sweep-by-sweep.
- Nothing personal goes in the clode repo. The board lives in the project folder.

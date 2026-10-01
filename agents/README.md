# opptions agent team: the desk

Five Claude agents in one Claude Project, each in its own thread, working like
one desk on the trading floor of a top firm. Each specialist brings its own
expertise, they check each other's ideas, and every morning the desk lead
hands Kerry, the portfolio manager, one **Desk Report**: the stocks most worth
looking into for options and swing trades, with the desk's combined reasoning.
Kerry decides, then takes the chosen ideas to the **Options Study** chat.
Nothing here places trades. How the desk works together is in
[desk.md](desk.md); each seat's own craft is in its brief.

| Seat | Thread | Brief | Expertise | Writes to |
|---|---|---|---|---|
| Desk lead | Explainer | [explainer.md](explainer.md) | Runs the morning meeting, writes the Desk Report, explains in plain English | `desk/<DATE>/desk-report.md`, `explainer/` |
| News analyst | News tracker | [news-tracker.md](news-tracker.md) | Catalysts, filings, analyst actions, macro | `news/`, `desk/<DATE>/news.md` |
| Earnings analyst | Earnings tracker | [earnings-tracker.md](earnings-tracker.md) | Report dates, implied vs. actual moves, guidance, call tone | `earnings/`, `desk/<DATE>/earnings.md` |
| Flow trader | Options flow tracker | [flow-tracker.md](flow-tracker.md) | Unusual volume, premium, put/call, new open interest | `flow/`, `desk/<DATE>/flow.md` |
| Derivatives strategist | GEX tracker | [gex-tracker.md](gex-tracker.md) | Dealer gamma: flip, call and put walls, pinning | `gex/`, `desk/<DATE>/gex.md` |
| Portfolio manager | Kerry | [options-study-handoff.md](options-study-handoff.md) | Reads the Desk Report, picks, takes ideas to Options Study | - |

## How the desk works together

Every seat covers Kerry's watchlist and also scouts beyond it, nominating up
to 3 names a day from its own specialty. Each seat that runs later gives its
view (Supports, Neutral or Against) on every name already nominated, so ideas
get checked from every angle. The desk lead fills any missing view, scores
conviction by how many seats agree, and writes the report.

```
  4:20 PM day before   Flow trader: flow scan while OI is pre-session, first nominations
  4:40 PM day before   Derivatives strategist: GEX scan, first nominations
          |
  6:57 AM              News analyst: catalysts, nominations, views on the board
          |
  7:33 AM              Earnings analyst: calendar, nominations, views on the board
          |
  8:05 AM              Flow trader: confirms which flagged trades became new positions, views
          |
  8:27 AM              Derivatives strategist: fresh-OI levels for every board name
          |
          v
  8:50 AM              Desk lead: morning meeting, fills gaps, ranks by conviction
          |
          v
  about 9:15 AM        Kerry reads the Desk Report and picks
          |
          v
  Send "prep SYMBOL idea" to the Explainer: it bundles that ticker's GEX, flow,
  news and earnings into one packet and drafts the message for Options Study.
  Add chart and option-chain screenshots.
          |
          v
  Options Study chat (claude.ai) analyzes the trade
```

The seats share work through files on the board,
`/mnt/project-files/opptions/desk/<DATE>/`, not through chat.

## Folder layout (shared project folder, never the repo)

```
/mnt/project-files/opptions/
  watchlist.md                          Kerry's tickers: one per line, "NVDA - note"
  gex/<SYMBOL>-<YYYY-MM-DD>.md          GEX tracker
  flow/<SYMBOL>-<YYYY-MM-DD>.md         Flow tracker (midday refresh appended)
  news/<SYMBOL>-<YYYY-MM-DD>.md         News tracker, ends with "Hand-off to the Explainer"
  earnings/<SYMBOL>-<YYYY-MM-DD>.md     Earnings tracker, ends with "Hand-off to the Explainer"
  explainer/<SYMBOL>-<YYYY-MM-DD>.md    Explainer
  desk/<YYYY-MM-DD>/                    the board for one trading day:
    gex.md news.md earnings.md flow.md    each seat's desk note (nominations, views)
    scan-gex.md scan-flow.md              raw `scan` output behind the GEX and Flow nominations
    desk-report.md                        the Desk Report for Kerry
  universe.md                           optional: tickers for `scan --universe` (default list built in)
  tools/earnings_moves.py               Earnings tracker helper: past report-day moves
  packets/<SYMBOL>-<YYYY-MM-DD>.md      opptions packet output (the command prints the path)
  notes/                                optional: Kerry's own notes from Options Study
```

Dates are the US Eastern trading date: `TZ=America/New_York date +%F`.
One exception: the 4:20 PM Flow and 4:40 PM GEX runs write their desk notes to the next trading
day's board folder (see desk.md, which also handles market holidays).
This repo is public, so the watchlist, positions and picks live only in the
project folder. Agents never write them into the repo.

## Daily rhythm (US Eastern time)

| When | Seat | What |
|---|---|---|
| 4:20 PM, day before | Flow trader | `scan flow` while open interest is still pre-session, first nominations |
| 4:40 PM, day before | Derivatives strategist | Next day's levels from the close, `scan gex`, first nominations |
| 6:57 | News analyst | Overnight news and filings, scouting, views on the board |
| 7:33 | Earnings analyst | Dates and implied moves, scouting, views on the board |
| 8:05 | Flow trader | Yesterday's session, which flagged contracts became new OI, views on the board |
| 8:27 | Derivatives strategist | Desk check: levels for every board name with fresh OI |
| 8:50 | Desk lead | Morning meeting, Desk Report ready by about 9:10 |
| about 9:15 | Kerry | Reads the Desk Report, picks, sends `prep` |
| 12:17 | Flow trader | Midday refresh, including the Desk Report names |
| on request | Earnings, Explainer | Kerry sends `run <SYM>` when a watchlist name reports after the close; otherwise the 7:33 run catches it |

Kerry then follows [options-study-handoff.md](options-study-handoff.md).

## Starting each thread

1. In the Claude Project, start one thread per agent and name it (GEX tracker,
   Flow tracker, News tracker, Earnings tracker, Explainer).
2. As the first message, paste the full brief for that agent, or send:
   "Your instructions are /mnt/project-files/agents/gex-tracker.md (desk.md is
   beside it). Read it now and follow it for this whole thread."
   The threads read their briefs and desk.md from `/mnt/project-files/agents/`.
   After changing a brief in the repo, copy it there too:
   `cp agents/*.md /mnt/project-files/agents/` from the clode repo root.
3. Then send a command:
   - `run`: the seat's full run: watchlist files plus its Desk duties (for the
     Explainer, the morning meeting). The scheduled runs do this.
   - `run NVDA AMD`: only these tickers' per-ticker files; the board is not touched
   - `refresh`: Flow tracker's midday update
   - `flags`: just today's flags, no new fetch
   - Explainer only: `why NVDA` explains a Desk Report name in more depth, and
     `prep NVDA <trade idea>` drafts the Options Study message
4. Edit the watchlist file in the project folder (or ask any thread to).
   Every tracker re-reads it on each run.

## Data access (one-time setup)

The toolkit CLI (`python3 -m opptions gex|flow|news|earnings SYM [--json]`,
`python3 -m opptions packet SYM ... --out DIR` and
`python3 -m opptions scan flow|gex [SYMBOLS...] [--universe FILE] [--top N] [--json]`)
needs these hosts allowed in the Project's network settings:

`cdn-api.cboe.com, api.nasdaq.com, feeds.finance.yahoo.com, news.google.com, www.sec.gov, data.sec.gov`

Until they are allowed, the CLI prints `could not reach <host>`, and each agent
falls back to web search, then says in its file what it could not get.
SEC EDGAR asks for a contact in the User-Agent. Set `OPPTIONS_SEC_UA` (for
example `opptions research you@example.com`) in the project environment's
secrets or variables, not in the repo.

## Rules every agent follows

- Free, delayed data (Cboe chain is 15 minutes delayed; open interest is as of
  the prior close). It is not a paid flow feed. Say so where it matters.
- No trade execution, and no buy, sell, enter or exit instructions. Explain
  what the data tends to mean, then leave the decision to Kerry.
- Keep facts (with source and time) apart from interpretation.
- After a number, add one plain-English line on what it usually means for
  price, phrased as a tendency ("often", "tends to"), never a certainty.
- Every file ends with a Gaps section that lists what was missing.

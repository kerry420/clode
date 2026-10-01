# opptions agent team

A small team of Claude agents in one Claude Project, each in its own thread.
They gather free, delayed market information on Kerry's watchlist, explain it
in plain English, and help Kerry bring a well-prepared question to the
**Options Study** chat. The agents research and explain; Kerry decides.
Nothing here places trades or tells anyone to buy or sell.

| Thread | Brief | Job | Writes to |
|---|---|---|---|
| GEX tracker | [gex-tracker.md](gex-tracker.md) | Dealer gamma levels: gamma flip, call wall, put wall | `gex/` |
| Flow tracker | [flow-tracker.md](flow-tracker.md) | Unusual volume vs open interest, put/call, big premium | `flow/` |
| News tracker | [news-tracker.md](news-tracker.md) | Headlines, SEC filings, insider trades, analyst actions | `news/` |
| Earnings tracker | [earnings-tracker.md](earnings-tracker.md) | Dates, implied move, last report and call transcript | `earnings/` |
| Explainer | [explainer.md](explainer.md) | Teaches what the news and earnings could mean | `explainer/` |
| Options Study (existing claude.ai chat, outside the project) | [options-study-handoff.md](options-study-handoff.md) | Analyzes the trade Kerry picks, with screenshots | - |

## How information flows

```
                 /mnt/project-files/opptions/watchlist.md
                                   |
      +--------------+-------------+-------------+----------------+
      v              v                           v                v
 GEX tracker    Flow tracker               News tracker    Earnings tracker
      |              |                           |                |
      |              |                           +---- hand-off --+
      |              |                                  v
      |              |                              Explainer
      |              |                                  |
      |              |                                  v
      |              |                Kerry reads, picks stock + option idea
      |              |                                  |
      +--------------+------------------+---------------+
                                        v
          packet (python3 -m opptions packet SYM) + filled hand-off template
                    + chart and option-chain screenshots
                                        |
                                        v
                          Options Study chat (claude.ai)
```

## Folder layout (shared project folder, never the repo)

```
/mnt/project-files/opptions/
  watchlist.md                          Kerry's tickers: one per line, "NVDA - note"
  gex/<SYMBOL>-<YYYY-MM-DD>.md          GEX tracker
  flow/<SYMBOL>-<YYYY-MM-DD>.md         Flow tracker (midday refresh appended)
  news/<SYMBOL>-<YYYY-MM-DD>.md         News tracker, ends with "Hand-off to the Explainer"
  earnings/<SYMBOL>-<YYYY-MM-DD>.md     Earnings tracker, ends with "Hand-off to the Explainer"
  explainer/<SYMBOL>-<YYYY-MM-DD>.md    Explainer
  packets/<SYMBOL>-<YYYY-MM-DD>.md      opptions packet output (the command prints the path)
  notes/                                optional: Kerry's own notes from Options Study
```

Dates are the US Eastern trading date: `TZ=America/New_York date +%F`.
This repo is public, so the watchlist, positions and picks live only in the
project folder. Agents never write them into the repo.

## Daily rhythm (US Eastern time)

| When | Thread | What |
|---|---|---|
| 7:30-8:30 pre-market | News, Earnings | Overnight headlines and filings, upcoming dates, any report since the last run |
| 8:00-9:00 pre-market | Flow | Recap of yesterday's full session, and whether yesterday's unusual contracts became new open interest (OI updates overnight) |
| 8:30-9:15 | Explainer | Reads today's News and Earnings hand-offs, writes the explanations |
| about 9:15 | Kerry | Reads the Explainer files, decides what to study |
| about 12:30 (optional) | Flow | Midday refresh: volume has built up (data is 15 minutes delayed) |
| after 4:15 post-close | GEX | Maps tomorrow's levels from today's close. OI only updates overnight, so levels barely change intraday except through price; an optional pre-market recheck picks up the fresh OI |
| evening (when needed) | Earnings, Explainer | A watchlist name reported after the close |

Kerry then follows [options-study-handoff.md](options-study-handoff.md).

## Starting each thread

1. In the Claude Project, start one thread per agent and name it (GEX tracker,
   Flow tracker, News tracker, Earnings tracker, Explainer).
2. As the first message, paste the full brief for that agent, or send:
   "Your instructions are agents/gex-tracker.md in the clode repo. Read it now
   and follow it for this whole thread."
3. Then send a command:
   - `run`: every ticker on the watchlist
   - `run NVDA AMD`: only these tickers
   - `refresh`: Flow tracker's midday update
   - `flags`: just today's flags, no new fetch
   - Explainer only: `prep NVDA <trade idea>` drafts the Options Study message
4. Edit the watchlist file in the project folder (or ask any thread to).
   Every tracker re-reads it on each run.

## Data access (one-time setup)

The toolkit CLI (`python3 -m opptions gex|flow|news|earnings SYM [--json]` and
`python3 -m opptions packet SYM ... --out DIR`) needs these hosts allowed in
the Project's network settings:

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

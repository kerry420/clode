# opptions

opptions is a small research toolkit for a retail options and swing trader.
It is built only on free, public, delayed data. Four trackers each look at one
thing:

- **gex**: dealer gamma exposure
- **flow**: unusual options activity
- **news**: news headlines and SEC filings
- **earnings**: earnings dates, the implied move and the EPS beat history

Each tracker fetches its sources, analyzes them offline and writes a Markdown
section. After each number there is one plain-English line on what it usually
means for price. The `packet` command combines all four sections into one file
for the **Options Study** chat. opptions is for research and learning. It does
not place trades and does not say what to buy or sell.

## The agent team

The trackers are run by a small team of Claude agents, each in its own
project thread:

- The **GEX** and **Flow** trackers report straight to Kerry.
- The **News** and **Earnings** trackers hand off to an **Explainer**, which
  says in plain English what the news and the earnings report could mean.
- Kerry reads that, picks a stock and an option idea, and sends the packet,
  chart screenshots and a filled-in template to the Options Study chat.

The briefs, the daily schedule and the hand-off template are in
[agents/README.md](agents/README.md).

## Quick start

You need Python 3.10 or newer (tested on 3.10 to 3.13). Only the standard
library is used, so there is nothing to install.

```sh
git clone https://github.com/kerry420/clode && cd clode
export OPPTIONS_SEC_UA="Your Name you@example.com"   # SEC EDGAR asks for a contact

python3 -m opptions gex SPY                  # gamma walls, flip and regime
python3 -m opptions flow NVDA --json         # unusual volume, as JSON
python3 -m opptions news TSLA                # headlines and SEC filings, last 7 days
python3 -m opptions earnings AAPL            # next report, implied move, beat history
python3 -m opptions packet NVDA AMD --out packets/   # one packet per symbol: packets/NVDA-YYYY-MM-DD.md
```

- Index symbols such as `SPX`, `NDX`, `RUT` and `VIX` are mapped to Cboe's
  index chains automatically.
- Packet files are named by the US Eastern trading date. `packets/` is
  git-ignored.
- The exit status is 1 when a tracker could not fetch any of its sources.

If a host is blocked, the command prints `could not reach <host>` (or
`<host> answered HTTP <code>`) and exits with status 1. The news and earnings
trackers still give partial results when only some of their sources fail, and
they name the missing ones.

To see what a packet looks like without any network access, open
[examples/sample-packet.md](examples/sample-packet.md). It is made from
synthetic test data for a made-up ticker, DEMO. To rebuild it, run
`python3 examples/build_sample.py`.

This repository is public. Keep your watchlist, positions and notes in your
project folder, not here.

## Data sources

| Tracker | What | Source | Host | Freshness |
|---|---|---|---|---|
| gex | Gamma by strike, call and put walls, gamma flip | Cboe delayed option chain | `cdn-api.cboe.com` | Quotes about 15 minutes delayed. Open interest is as of the prior close (updated once a day). |
| flow | Volume vs open interest, put/call ratios, premium, estimated side | Cboe delayed option chain (the same request) | `cdn-api.cboe.com` | Same as gex. Volume is the running total for the session. |
| news | Headlines | Yahoo Finance RSS | `feeds.finance.yahoo.com` | Whenever the feed updates. Stories can arrive late. |
| news | Headlines | Google News RSS search | `news.google.com` | Whenever the feed updates. Links are Google redirects. |
| news, earnings | Filings (8-K items, offerings, 13D, Form 4/144), latest earnings 8-K | SEC EDGAR | `www.sec.gov` (ticker to CIK), `data.sec.gov` (filings) | Close to real time once a filing is accepted. Filing dates only, no times. |
| earnings | Next report date and timing, consensus EPS, EPS surprise history | Nasdaq (data from Zacks) | `api.nasdaq.com` | Dates can be estimates until the company confirms them. |
| earnings | Implied move and IV around the report | Cboe delayed option chain | `cdn-api.cboe.com` | About 15 minutes delayed. |

The CLI does not fetch earnings-call transcripts. The Earnings agent finds
them on the web.

## Hosts a sandbox must allow

```
cdn-api.cboe.com
api.nasdaq.com
feeds.finance.yahoo.com
news.google.com
www.sec.gov
data.sec.gov
```

Until these hosts are allowed in the project's network settings, every command
fails with `could not reach <host>`, and the agents fall back to web search.

## Configuration

Only these environment variables are read. A value that cannot be parsed
falls back to the default.

| Variable | Read by | Default | Meaning |
|---|---|---|---|
| `OPPTIONS_SEC_UA` | `sec.py` (news, earnings) | `opptions research tool admin@example.com` | User-Agent sent to SEC EDGAR. The SEC asks for a real name and email, so set it. Keep it in your environment or secrets, not in the repo. |
| `OPPTIONS_TIMEOUT` | `http.py` | `20` | Seconds to wait for each request. |
| `OPPTIONS_RATE` | `gex.py` | `0.04` | Risk-free rate for the gamma-flip model, as a decimal (`0.04` means 4%, while `4` would mean 400%). |
| `OPPTIONS_MIN_VOLUME` | `flow.py` | `250` | Minimum contracts traded for a contract to count as unusual. |
| `OPPTIONS_MIN_PREMIUM` | `flow.py` | `50000` | Minimum premium in dollars for a contract to count as unusual. |
| `OPPTIONS_NEWS_DAYS` | `news.py` | `7` | How many days back the headline and filing window reaches. |

## Honest limits

- **Free, delayed data.** Option quotes are about 15 minutes old. Open interest
  updates once a day, so positions opened today are not in it yet. This is not
  a real-time or paid feed.
- **The GEX model is naive.** It assumes dealers are long calls and short puts.
  Real dealer positioning is not public and can be the opposite. The gamma
  flip is a Black-Scholes estimate from each contract's IV. Treat every level
  as a rough zone.
- **Flow is volume-based, not true sweeps.** "Unusual" is worked out from each
  contract's total volume for the day. It cannot see individual trades,
  sweeps, blocks or spreads, or tell whether a trade opened or closed a
  position. The bought/sold side is estimated from the last trade only. True
  flow needs a paid feed, such as Unusual Whales, Cheddar Flow or
  Polygon/Massive.
- **News tags come from headline keywords**, so read the story before trusting
  a tag. Form 4 counts do not say whether insiders bought or sold. Exchange
  holidays are not taken into account.
- **Earnings dates can be estimates.** When the earnings expiry is weeks away,
  the straddle also prices ordinary movement, not only the report. Past
  actual earnings moves are not included.
- **The data shapes have not been checked against live responses.** The field
  names for Cboe, Nasdaq, Yahoo and Google were confirmed from public docs and
  open-source code, because the build sandbox blocked those hosts. Check the
  first live run. It is also not confirmed whether Cboe's timestamp is in
  Eastern time or UTC. If it is UTC, days to expiry can be one day short late
  in the US evening.
- **Not financial advice.** The output describes what numbers tend to mean, not
  what will happen. The trading decisions are yours.

## Running the tests

All tests run offline on small, hand-built, synthetic fixtures in
`tests/fixtures/`.

```sh
python3 -m unittest discover -s tests -t . -v   # everything
python3 -m unittest tests.test_gex -v           # one tracker: test_gex, test_flow, test_news, test_earnings
python3 examples/build_sample.py --check        # is examples/sample-packet.md current?
```

`tests/test_integration.py` checks the following:

- every tracker follows the `fetch` / `analyze` / `to_markdown` contract
- `analyze` returns strict JSON
- a packet still builds when one tracker fails
- the CLI exits cleanly when a host is blocked
- the sample packet is up to date

## Layout

```
opptions/
  __main__.py   CLI: python3 -m opptions <gex|flow|news|earnings|packet> SYMBOL...
  http.py       stdlib fetch helpers; FetchError names the host
  cboe.py       Cboe delayed chain: fetch and normalize
  sec.py        EDGAR ticker -> CIK, recent filings, 8-K item meanings
  gex.py flow.py news.py earnings.py   the four trackers
  packet.py     one Markdown packet per symbol from all trackers
agents/         briefs for the Claude agent team
examples/       build_sample.py and the synthetic sample-packet.md
tests/          unittest suites and fixtures
```

Every tracker has the same three functions: `fetch(symbol) -> dict` (network),
`analyze(raw) -> dict` (pure: no network or clock, JSON-safe output) and
`to_markdown(result) -> str` (starts with a `## ` heading). To add a tracker,
write those three functions and add the module's name to `TRACKERS` in
`opptions/__init__.py`.

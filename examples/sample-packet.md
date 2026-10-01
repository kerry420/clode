# Research packet: DEMO (synthetic sample)

> **Synthetic sample, not market data.** Built by `examples/build_sample.py`
> from the hand-made test fixtures in `tests/fixtures/`, relabeled as the
> made-up ticker DEMO. No network was used and no real company is described.
> Each section comes from its own fixture, so spot prices, times and stories do
> not line up across sections the way they do in a real packet (for example,
> the news fixture has an earnings release while the earnings fixture's next
> report is three weeks away). Links, CIK and filing numbers are placeholders.
> The "Generated" time below is fixed so the file is identical on every run.

Generated 2026-10-01 14:30 UTC (US Eastern date 2026-10-01) by the opptions
trackers. Option data is Cboe's 15-minute-delayed chain; open interest updates
once per day. Treat every section as raw input for analysis, not a
recommendation.

## Gamma Exposure (GEX): DEMO

Spot **$100.00** (last price) as of 2026-10-01 15:45 (Cboe timestamp, about 15 minutes delayed). 20 of 20 contracts have open interest and count.

**Net GEX: +$3.47M per 1% move (positive gamma regime).** Calls +$15M, puts -$11.6M.
Dealers are estimated to be net long gamma: their hedging tends to sell rallies and buy dips, which usually dampens moves and can pin price near large strikes.

### Key levels

| Level | Price | vs spot | GEX per 1% | Tends to |
|---|---|---|---|---|
| Call wall | 105.00 | +5.00% | +$6.94M | act as resistance or a magnet |
| Put wall | 95.00 | -5.00% | -$5.43M | act as support; moves can speed up below it |
| Gamma flip | 98.79 | -1.21% | 0 | separate calmer trading (above) from faster moves (below) |

Spot is 1.21% above the flip, on the side where model net gamma is positive, so hedging tends to dampen moves there.

### Top strikes by |net GEX|

| Strike | vs spot | Call GEX | Put GEX | Net GEX |
|---|---|---|---|---|
| 105.00 | +5.00% | +$6.94M | -$325K | +$6.61M |
| 95.00 | -5.00% | +$724K | -$5.43M | -$4.71M |
| 100.00 | +0.00% | +$6.24M | -$5.07M | +$1.16M |
| 110.00 | +10.00% | +$1.08M | -$35.4K | +$1.05M |
| 90.00 | -10.00% | +$68.6K | -$706K | -$638K |

Large positive strikes tend to slow price down or pin it nearby; large negative strikes are where moves tend to accelerate.

### Expiry concentration

- Nearest expiry 2026-10-09 (8 DTE): 44% of gross gamma.
- 0DTE: 0% of gross gamma.

| Expiry | DTE | Net GEX | Share of gross |
|---|---|---|---|
| 2026-10-09 | 8 | +$790K | 44% |
| 2026-10-30 | 29 | +$2.68M | 56% |

When a big share of gamma expires soon, the levels above tend to lose their pull after that expiry and price can move more freely.

**How to read this:** positive net GEX tends to mean calmer, range-bound trading with price drawn toward big strikes; negative net GEX tends to mean larger, faster moves. The call wall often behaves like resistance, the put wall like support, and the flip marks the rough line between the two regimes. These are tendencies from hedging flows, not predictions or trade signals.

_Caveat: free Cboe data, about 15 minutes delayed; open interest updates once a day, so today's new positions are not in it. Signs assume dealers are long calls and short puts (customers sell calls and buy puts); real dealer positioning is not public and can be the opposite. GEX is dollars of hedging per 1% move. The flip uses a Black-Scholes model on each contract's IV (r = 4.00%, q = 0), so treat all levels as rough zones._

## Options Activity (unusual volume): DEMO

Spot **$100** as of 2026-09-30 16:15 (Cboe timestamp, about 15 minutes delayed). Session 2026-09-30: 12 of 13 contracts traded.
_Built from each contract's total volume for the day, not trade-by-trade prints, so it shows where activity was unusual, not individual sweeps or blocks._

### Summary

- **Volume:** calls 6,850 vs puts 1,949 (put/call 0.28).
  Call-heavy trading, which tends to reflect bullish speculation; very lopsided call activity sometimes shows up near short-term tops when optimism gets crowded.
- **Premium:** calls ~$559K vs puts ~$375K (put/call 0.67).
  More money went into calls. Premium counts dollars, so it tends to show where the bigger bets went better than raw contract counts do.
- **Open interest:** calls 13,600 vs puts 12,160 (put/call 0.89).
  More open calls than puts. Open interest is positioning built over days or weeks (updated overnight), so this ratio moves slowly.
- **Estimated lean: bullish.** ~$417K bullish-looking (calls at ask, puts at bid) vs ~$232K bearish-looking (puts at ask, calls at bid); 70% of premium could be called bullish or bearish.
  More premium looks bullish (calls paid at the ask, puts sold at the bid). That tends to lean toward upside bets, but a hedge or one leg of a spread can look the same.
- **Time value:** ~$809K of the ~$934K premium (87%) is time value; the rest is built-in value of in-the-money options. Time-value lean: bullish (~$417K bullish-looking vs ~$232K bearish-looking, deep in-the-money contracts left out).
  Time value is the part of the price that can be lost by expiry, so it tends to show the size of a bet better than total premium, which deep in-the-money options inflate.
- **Short-dated:** 49% of premium is in options expiring within 7 days.
  The money is split roughly evenly between options expiring within a week and longer-dated ones, so neither quick bets nor longer positions clearly dominate.
- **Implied volatility:** front expiry 2026-10-02 (2 DTE) ATM IV 51% at the 100 strike; IV30 48%.
  Options price a typical move of about ±$3.78 (±3.8%) by 2026-10-02; price tends to stay inside that range about two times out of three when nothing unexpected happens.
  Front IV is close to IV30, so no special near-term event appears priced in.

### Unusual contracts

4 contracts met the unusual rule, ranked by time value (extrinsic premium).

| Contract | Expiry (DTE) | Strike | Type | Volume | OI | Vol/OI | ~Premium | ~Extrinsic | Side | IV | Delta | vs spot |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DEMO261002C00105000 | 2026-10-02 (2) | 105 | call | 2,000 | 500 | 4.0x | $230K | $230K | ask (likely bought) | 55% | +0.25 | +5.0% (OTM) |
| DEMO261016C00100000 | 2026-10-16 (16) | 100 | call | 400 | 300 | 1.3x | $124K | $124K | bid (likely sold) | 47% | +0.52 | at spot (ATM) |
| DEMO261016P00100000 | 2026-10-16 (16) | 100 | put | 300 | 250 | 1.2x | $90K | $90K | ask (likely bought) | 49% | -0.48 | at spot (ATM) |
| DEMO261016C00110000 | 2026-10-16 (16) | 110 | call | 800 | 0 | new (OI 0) | $84K | $84K | ask (likely bought) | 46% | +0.25 | +10.0% (OTM) |

Deep in-the-money options (|delta| 0.90 or more, marked "deep ITM") carry mostly built-in value, often from stock replacement or rolls rather than a fresh bet, so the desk weighs the time-value part (~Extrinsic) more than the total premium.
Side: "ask" means the last trade printed near the ask (likely bought), "bid" near the bid (likely sold), "mid" in between, "stale" means the last trade is not from this session, "unknown" means there was no usable quote.
Volume above open interest tends to mean many of today's trades opened new positions, which is why traders watch it. Check tomorrow's open interest: if it rises by about the volume, positions were likely opened. Any one contract can also be a hedge or one leg of a spread, so treat these as clues, not conclusions.

### Positioning (open interest by strike, all live expiries)

| Type | Strike | Open interest | Today's volume | vs spot | Largest expiry (share) |
|---|---|---|---|---|---|
| call | 110 | 8,000 | 850 | +10.0% | 2026-11-20 (100%) |
| call | 120 | 4,000 | 0 | +20.0% | 2026-11-20 (100%) |
| call | 100 | 1,000 | 1,000 | at spot | 2026-10-02 (70%) |
| call | 105 | 500 | 2,000 | +5.0% | 2026-10-02 (100%) |
| call | 115 | 100 | 3,000 | +15.0% | 2026-10-02 (100%) |
| put | 90 | 6,000 | 100 | -10.0% | 2026-11-20 (100%) |
| put | 95 | 3,000 | 1,000 | -5.0% | 2026-10-02 (100%) |
| put | 85 | 2,000 | 100 | -15.0% | 2026-10-02 (100%) |
| put | 100 | 1,150 | 500 | at spot | 2026-10-02 (78%) |
| put | 105 | 10 | 249 | +5.0% | 2026-10-16 (100%) |

Strikes with large open interest can act like magnets or walls, especially when price is near them and their main expiry is close; their pull tends to fade after that expiry.

_Caveat: free Cboe data, about 15 minutes delayed. This is unusual activity from each contract's cumulative volume for the session, not trade-by-trade flow: it cannot see sweeps, blocks, multi-leg spreads, or whether trades opened or closed positions. "Side" credits the whole day's volume to where the last trade printed against the current bid/ask, which is weak evidence. Open interest updates once a day. Unusual rule: volume ≥ 250, volume above open interest, premium ≥ $50K. True flow needs a paid feed (Unusual Whales, Cheddar Flow, Polygon/Massive or similar), which can be wired in later. Nothing here is a trade recommendation._

## News and Filings: DEMO

**12 headlines in the last 7 days** (since 2026-09-24 01:50 ET): 5 high, 3 medium, 4 low impact. Feeds: Yahoo Finance RSS (7 items), Google News RSS (11 items); 3 duplicates merged, 2 older items left out, 1 undated item left out.
A week with several high-impact headlines tends to bring bigger moves and higher implied volatility than usual.

### High impact (5)

- 2026-09-30 21:10 ET (after hours) · GlobeNewswire · offering · [Demo Robotics prices $250 million offering of convertible senior notes](https://news.google.com/rss/articles/CBMiAAA2?oc=5)
- 2026-09-30 16:15 ET (after hours) · Yahoo Finance · earnings, guidance · [Demo Robotics Q3 earnings beat estimates; company raises full-year outlook](https://finance.yahoo.com/news/demo-robotics-q3-earnings-beat-201500123.html?.tsrc=rss) (also: Investing.com)
- 2026-09-29 08:45 ET (pre-market) · Barron's · upgrade/downgrade · [Demo Robotics upgraded at Morgan Stanley on warehouse demand](https://news.google.com/rss/articles/CBMiAA10?oc=5) (also: MSN)
- 2026-09-29 08:30 ET (pre-market) · Yahoo Finance · upgrade/downgrade · [Morgan Stanley upgrades Demo Robotics to Overweight, lifts price target to $85](https://finance.yahoo.com/news/morgan-stanley-upgrades-demo-123000456.html?.tsrc=rss)
- 2026-09-24 14:00 ET (market hours) · The Wall Street Journal · legal/regulatory · [FTC opens antitrust probe into Demo Robotics warehouse deal](https://news.google.com/rss/articles/CBMiAAA8?oc=5)

These are the headlines most likely to move the stock and its implied volatility; the size of the move tends to depend on how far the news is from what was expected.

### Headlines (newest first)

| When (ET) | Source | Impact | Tags | Headline |
|---|---|---|---|---|
| 2026-09-30 21:10 (after hours) | GlobeNewswire | high | offering | [Demo Robotics prices $250 million offering of convertible senior notes](https://news.google.com/rss/articles/CBMiAAA2?oc=5) |
| 2026-09-30 16:15 (after hours) | Yahoo Finance | high | earnings, guidance | [Demo Robotics Q3 earnings beat estimates; company raises full-year outlook](https://finance.yahoo.com/news/demo-robotics-q3-earnings-beat-201500123.html?.tsrc=rss) |
| 2026-09-30 11:00 (market hours) | Reuters | low | macro | [Stocks slip as Fed signals fewer rate cuts; tariff worries weigh](https://news.google.com/rss/articles/CBMiAAA7?oc=5) |
| 2026-09-29 17:00 (after hours) | MarketBeat | medium | management, insider | [Demo Robotics CEO Jane Doe sells 50,000 shares under 10b5-1 plan](https://news.google.com/rss/articles/CBMiAAA6?oc=5) |
| 2026-09-29 08:45 (pre-market) | Barron's | high | analyst | [Demo Robotics upgraded at Morgan Stanley on warehouse demand](https://news.google.com/rss/articles/CBMiAA10?oc=5) |
| 2026-09-29 08:30 (pre-market) | Yahoo Finance | high | analyst | [Morgan Stanley upgrades Demo Robotics to Overweight, lifts price target to $85](https://finance.yahoo.com/news/morgan-stanley-upgrades-demo-123000456.html?.tsrc=rss) |
| 2026-09-28 06:00 (pre-market) | Yahoo Finance | low | - | [Why Demo Robotics stock is a long-term hold](https://finance.yahoo.com/news/why-demo-long-term-hold-100000987.html?.tsrc=rss) |
| 2026-09-28 03:15 (pre-market) | MarketBeat | low | fund filing | [Vanguard Group Inc. Acquires 12,345 Shares of Demo Robotics Inc. (NASDAQ:DEMO)](https://news.google.com/rss/articles/CBMiAAA5?oc=5) |
| 2026-09-27 05:00 (weekend) | PR Newswire | low | legal/regulatory, law-firm ad | [Shareholder Alert: Smith & Jones Law Firm investigates claims on behalf of Demo Robotics investors](https://news.google.com/rss/articles/CBMiAAA4?oc=5) |
| 2026-09-26 09:05 (weekend) | Business Wire | medium | product | [Demo Robotics unveils new warehouse robot line](https://news.google.com/rss/articles/CBMiAAA1?oc=5) |
| 2026-09-25 12:45 (market hours) | Yahoo Finance | medium | management | [Demo Robotics CFO to step down at year end](https://finance.yahoo.com/news/demo-cfo-step-down-164500321.html?.tsrc=rss) |
| 2026-09-24 14:00 (market hours) | The Wall Street Journal | high | legal/regulatory | [FTC opens antitrust probe into Demo Robotics warehouse deal](https://news.google.com/rss/articles/CBMiAAA8?oc=5) |

News that lands after hours, before the open or on a weekend tends to show up as a gap at the next open rather than as a move during the session. Exchange holidays are not marked.

### What these tags tend to mean

- **earnings** (1): Earnings news tends to move a stock most when results differ from what was expected, and implied volatility usually drops right after the report (IV crush).
- **guidance** (1): A guidance change often matters more than the quarter itself, because it resets what analysts expect for the months ahead.
- **analyst** (2): Upgrades and downgrades tend to move a stock for a day or two, more when the firm is large or the call is a surprise; a price-target change alone usually moves it less.
- **offering** (1): A share or convertible offering adds supply and can dilute holders, so the stock often slips toward the offer price for a few days.
- **legal/regulatory** (2): Legal and regulatory news can weigh on a stock and keep implied volatility elevated while the outcome is unknown; most cases resolve slowly.
- **product** (1): Product and contract news usually moves price less than earnings, unless it changes the revenue outlook.
- **management** (2): A surprise CEO or CFO exit tends to add uncertainty and can pressure the stock; a planned succession usually matters less.
- **insider** (1): Insider buying is often read as confidence; insider selling is common (pay, taxes, pre-planned 10b5-1 plans) and usually says less.
- **macro** (1): Macro news (Fed, inflation data, tariffs, rates) moves the whole market, so the stock may follow its sector and the index more than its own story.
- **law-firm ad** (1): Law-firm 'shareholder alert' releases are advertisements that usually follow a drop rather than cause one; they rarely move price on their own.
- **fund filing** (1): Fund-holding reports describe 13F filings that are weeks old; they rarely move price.

### SEC filings (EDGAR)

**7 filings since 2026-09-24** for Demo Robotics Inc. (CIK 9999999).

- 8-K filed 2026-09-30: Earnings results
- 8-K filed 2026-09-25: Executive or director change
- Possible dilution: 424B5 x1 (filed 2026-09-30)
- Activist or >5% stake: SC 13D filed 2026-09-24
- 2 Form 4 insider transaction filings
- 1 Form 144 notice of planned insider sales

Offerings add new shares; stocks often drift lower toward the offer price for a few days, though a well-received raise can be absorbed quickly.
A 13D means a holder above 5% may push for changes; stocks often rise on activist news and volatility tends to stay elevated.
An executive change adds uncertainty; a sudden CFO exit tends to worry the market more than a planned retirement.
Item 2.02 is the earnings release itself; the price reaction usually depends on guidance and on how results compare with expectations.
Form 4s do not say buy or sell in this feed; check https://www.sec.gov/cgi-bin/own-disp?action=getissuer&CIK=0009999999. Several insiders buying on the open market tends to be read as confidence; sales usually say less.
A Form 144 announces an intended insider sale; it is a notice, not proof the sale happened.

| Filed | Form | What it means | Link |
|---|---|---|---|
| 2026-09-30 | 424B5 | Prospectus for an offering (possible dilution) | [filing](https://www.sec.gov/Archives/edgar/data/9999999/000119312526210001/d123456d424b5.htm) |
| 2026-09-30 | 8-K | Current report: item 2.02: Earnings results | [filing](https://www.sec.gov/Archives/edgar/data/9999999/000199999926000041/demo-20260930.htm) |
| 2026-09-25 | 8-K | Current report: item 5.02: Executive or director change | [filing](https://www.sec.gov/Archives/edgar/data/9999999/000199999926000037/demo-20260924.htm) |
| 2026-09-24 | SC 13D | Activist or >5% holder stake | [filing](https://www.sec.gov/Archives/edgar/data/9999999/000095010326012345/d98765dsc13d.htm) |

### Caveats

Free RSS headlines and EDGAR data, not a paid news feed: stories can arrive late, paywalled outlets are thin, and Google links are redirects. Tags and impact come from keywords in the headline only, so read the story before trusting a tag. Times are US Eastern; EDGAR filing dates are dates only. Nothing here is a trade signal.

Next step: send this to the Explainer agent for a plain-English read.

## Earnings: DEMO

Fetched 2026-10-01 14:30 UTC (US Eastern date 2026-10-01).

**Next report: Thu Oct 22, 2026, after close (in 21 days).** Source: Nasdaq/Zacks "expected" date.
Fiscal quarter ending Sep 2026; consensus EPS $1.42 from 8 analysts (Zacks); same quarter last year $1.10.
The price reaction tends to hinge on results versus this consensus and, often more, on guidance for the next quarter.
Dates from Nasdaq/Zacks can be estimates until the company confirms them; the company's investor-relations page is the final word.

### Implied earnings move

Expiry used: **2026-10-23** (22 DTE), the first expiry that includes the reaction to the report (an after-close report first trades the next session).

| Item | Value |
|---|---|
| Spot | $152.40 |
| ATM strike | 150 |
| Call mid + put mid | $10.00 + $7.50 |
| Straddle | $17.50 |
| Implied move | +/-11.48% (+/-$17.50) |
| Implied range | $134.90 to $169.90 |
| ATM IV, 2026-10-16 (ends before the report) | 40.0% |
| ATM IV, 2026-10-23 | 58.0% |
| ATM IV, next expiry 2026-10-30 | 50.0% |
| IV difference | +8.0 pts |

Options are pricing about an 11.5% move either way (roughly $134.90 to $169.90) by 2026-10-23. That is the market's estimate of size, not direction, and the actual move can be larger or smaller.
Because this expiry is 22 days away, that price also covers 22 days of ordinary movement, not only the report, so the report's own share is smaller. It becomes a cleaner read on the report in the last few days before it, which is when it compares fairly with past earnings-day moves.
The earnings expiry's IV is 8.0 points above the next expiry's. Both expiries include the report, so both carry event premium; the nearer one shows more because the report's jump is packed into fewer days.
The 2026-10-16 expiry ends before the report and sits at 40.0% (vs 58.0% for the earnings expiry), a rough guide to the usual level. After the report, IV tends to fall back toward that level (the "IV crush"), so option prices often drop even when the stock moves.

### EPS surprise history (last 4 quarters)

| Fiscal quarter | Reported | Consensus EPS | Actual EPS | Surprise | Result |
|---|---|---|---|---|---|
| Jun 2026 | 2026-07-23 | $1.31 | $1.38 | +5.34% | beat |
| Mar 2026 | 2026-04-23 | $1.30 | $1.25 | -3.85% | miss |
| Dec 2025 | 2026-01-28 | $1.45 | $1.52 | +4.83% | beat |
| Sep 2025 | 2025-10-23 | $1.02 | $1.10 | +7.84% | beat |

Beat the consensus in 3 of the last 4 quarters. A steady beat record tends to be expected already, so the reaction often depends more on guidance and the call than on the beat itself.

### Latest earnings release (SEC 8-K, item 2.02)

- [8-K filed 2026-07-23](https://www.sec.gov/Archives/edgar/data/9999999/000123456726000045/demo-20260723.htm), 70 days ago; items 2.02, 9.01.
- The press release is usually exhibit 99.1 in that filing's folder.

### Earnings call

The earnings-call transcript is not gathered by this script. The Earnings agent reads it from the web (for example Motley Fool's transcript pages or the company's investor-relations site) and summarizes it for the Explainer.

_Caveat: free data. Dates and consensus come from Nasdaq/Zacks and can be estimates; consensus differs by source. Option prices are Cboe's 15-minute-delayed quotes, and mids of wide bid/ask spreads are rough. The implied move describes what options price in, not what will happen, and nothing here is a trade recommendation._

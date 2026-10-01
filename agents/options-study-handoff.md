# Hand-off to Options Study

How Kerry brings a picked trade idea to the **Options Study** chat on
claude.ai. Options Study is a separate chat outside this project, so it cannot
read the project folder: everything it needs is pasted or attached.

Kerry picks. The agents only gather and explain. The Explainer can draft this
message when Kerry sends `prep <SYMBOL> <trade idea>` in its thread.

## Steps

1. Read today's Desk Report: `/mnt/project-files/opptions/desk/<DATE>/desk-report.md`
   (and the Explainer file for a watchlist name, if there is one). Decide which
   ticker and which trade idea you want analyzed (strike, expiry, call or put,
   or a stock swing).
2. Build the packet (all four tracker sections in one file). Either send
   `prep <SYMBOL> <idea>` to the Explainer, or run from the clode repo root:
   ```
   python3 -m opptions packet <SYMBOL> --out /mnt/project-files/opptions/packets/
   ```
   The command prints the packet path (`packets/<SYMBOL>-<DATE>.md`, dated by
   the US Eastern trading date like the tracker files).
3. Take screenshots:
   - Daily chart, 3 to 6 months, with your support and resistance drawn
   - A shorter chart (1h or 15m) if timing an entry for a swing
   - The option chain around your strike: bid, ask, IV, volume, open interest
   - Your broker's risk graph or profit/loss at expiry, if it has one
4. Open the Options Study chat. Attach the screenshots and the packet file,
   then paste the filled template below.
5. Afterwards, if you want, save what you learned in the project folder (for
   example `/mnt/project-files/opptions/notes/`). Never in the repo.

## Template (copy, fill in, paste)

```
I'm considering a trade and want you to analyze it the way you usually do:
clear, patient, and explain any term I might not know.

TICKER: <SYMBOL>
PRICE NOW: <price> as of <time ET>

TRADE IDEA I'M CONSIDERING:
<buy/sell> <qty> <SYMBOL> <expiry YYYY-MM-DD> <strike> <call/put> at about $<price>
(format example: buy 1 XYZ 2026-11-20 50 call at about $2.10; or a stock swing:
buy shares near <price>, idea of a stop near <price>)
- My thesis in my own words: <why I think it could work>
- How long I plan to hold: <days or weeks>
- The most I'm willing to lose on this: $<amount>

ATTACHED:
1. Daily chart (<timeframe>)
2. <short-term chart>
3. Option chain around <strike>
4. Risk graph (if any)
5. Research packet: <SYMBOL>-<DATE>.md

--- DESK REPORT (desk/<DATE>/desk-report.md)
Conviction <High/Medium> | Lean <...> | Timeframe <...> | Catalyst <...>
Seat views: News <view> | Earnings <view> | Flow <view> | GEX <view>
The desk's read: <paste>
What would prove the desk wrong: <paste>

--- GEX LEVELS (gex/<SYMBOL>-<DATE>.md, data as of <time ET>)
Spot <price> | Gamma flip <price> | Call wall <price> | Put wall <price>
Net GEX: <positive/negative>
Flags: <any GEX flags, or none>

--- OPTIONS FLOW SUMMARY (flow/<SYMBOL>-<DATE>.md)
Put/call volume ratio <x> | Biggest contracts: <1-3 lines>
Opened or closed (from OI change): <...>
Flags: <any flow flags, or none>

--- EXPLAINER SUMMARY (explainer/<SYMBOL>-<DATE>.md)
Short version: <paste>
Bull case: <paste>  Bear case: <paste>
What would change the picture: <paste>

--- EARNINGS TIMING AND IMPLIED MOVE (earnings/<SYMBOL>-<DATE>.md)
Next report: <date>, <before open/after close>, <confirmed/estimated>
Implied move: +/-<x>% (+/-$<y>), expiry used <date>
My expiry is <before/after> the report.

Data notes: free, delayed data (Cboe chain 15 minutes delayed, open interest
as of the prior close). Missing pieces: <list from each file's Gaps>.

QUESTIONS
1. How does this trade tend to behave around the GEX levels above? What
   happens to it if price gets pinned near <level>, or breaks through it?
2. IV crush risk: how much of this option's price is event premium? What
   happens to it if the stock moves less than the implied move?
3. Max loss, breakeven at expiry, and how much theta costs per day. What does
   my P&L look like if the stock goes nowhere for a week?
4. What would invalidate this idea: a price level, a close below or above
   something, a date, or news?
5. Are there other strikes, expiries or structures (like a spread) that fit
   my thesis with less IV risk? Explain the trade-offs, I'll decide.
6. What in this data argues against my thesis, and what did I miss?
```

## Tips

- One ticker per message keeps the analysis focused.
- If the GEX or flow file is from yesterday, say so in the message; levels and
  volume can change after the open.
- If a section is missing, write "not available" rather than deleting it, so
  Options Study knows it was checked.

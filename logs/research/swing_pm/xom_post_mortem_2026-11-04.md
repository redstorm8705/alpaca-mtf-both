# Post-mortem — Swing breakout XOM, 3 sh, −$34.98 (signed: Claude, 2026-11-04)

Sources: trade_events.jsonl entry/exit events, data/state/swing_breakout.json, Alpaca fills, SIP daily bars,
FMP /stable/earnings, replay `breakout_remedies.py` (output `breakout_remedies_out_2026-11-04.txt`).

| Question | Finding |
|---|---|
| Why we entered then | 55-day closing-high breakout (close 183.21 on 10-23 > 178.98; > SMA200 153.16). Bought 10-26 07:12 PT at $183.52 (limit 183.90). |
| Should we have entered earlier | Yes. XOM made new 55-day closing highs on 10-12, 10-13, 10-14, 10-16 and 10-23. It was not in the Swing universe until the SWING-OWNER-1025 universe change took effect on 10-26 (no XOM in the 10-13…10-23 universe lists), so the bot's first entry was the 5th breakout, +15% into a run from $159 (Aug). The replay's first-breakout entry (10-13, ≈$172) is still open today. |
| Conviction | No per-trade score: the family enters every qualifying breakout; rank 36 of 77 is the dollar-volume rank, not conviction. Size from the replay prior (no live trades yet): 1,053 trades, mean +0.18 R, Kelly 0.059 → r = 1.48% of equity → 3 sh ($550), $35 at risk (1.4% of equity). |
| Target realistic | There is no price target: exits are a close below the prior 20-session low, 30 sessions, or the stop. Best move after entry: high $188.48 on 10-30 (+2.7%, +0.5 R) — earnings day — then −10% in three sessions. |
| Stop placement | 2.5 × ATR14 ($3.93) = $9.81 (5.3%) below entry → $173.71: a fixed multiple of XOM's own daily range, not of its own adverse-move distribution. The open today was $173.82 (above the stop); it broke at 09:32 ET. Not noise: XOM is $169.47 now, 1.1 ATR below the stop. |
| Concentration | XOM was the book's only energy name (others: MSFT, AAPL Swing; GE, BRK.B, NVDA, V and others in QHM/F6) → it diversified. |
| What actually happened | Oil fell: USO −7.2% on 11-02 and −7.6% on 11-04, XLE −10% from 10-30, while SPY rose 11-04. USO was already falling 10-23 → 10-27 while XOM broke out. Earnings 10-30 (beat, $4.23 vs $4.12) came 4 sessions after entry. |
| What would have made it a winner | (1) Entering the first breakout (10-13). (2) Not entering a 5th breakout 4 sessions before earnings with the sector's commodity falling. |

## Remedy replay (48 names, 2020-26, live rule = highest close of prior 55; next-open entry vs live intraday limit; 27 candidates, chosen on 2020-23, 2024-26 read once each)
| Rule | 2020-23 trades / mean R / win% | 2024-26 trades / mean R / win% |
|---|---|---|
| Live rule | 723 / +0.374 / 45.5% | 555 / +0.592 / 45.0% |
| Skip if earnings within 10 sessions | 695 / +0.390 / 46.5% | 542 / +0.586 / 45.2% |
| Skip if 4+ breakouts in the prior 20 sessions (late) | 621 / +0.382 / 45.7% | 440 / +0.680 / 46.8% |
| Sector ETF above its 50-day average | 693 / +0.393 / 46.0% | 533 / +0.587 / 45.4% |
| All three | 560 / +0.427 / 47.1% | 392 / +0.721 / 47.7% |
| Break-even stop at +2 ATR / chandelier 2.5-3 ATR | worse (+0.20 to +0.31) | worse (+0.30 to +0.43) |
All three filters together would have blocked the XOM 10-26 entry (earnings 10-30; four prior breakouts).
**Caveat that decides it:** they raise R per trade but cut trades 25-30%, and total R falls (2024-26: 555 × 0.592 = 329 R vs
392 × 0.721 = 283 R) unless Swing capacity (10 slots, overnight K room) is binding — then the freed room goes to better
trades. Fewer trades needs the owner. Next test: a capacity-constrained replay with live slots and K room.
Tighter or trailing stops cut the winners that pay for the losers; the 2.5-ATR stop stays.
Survivorship: today's top-50 universe.

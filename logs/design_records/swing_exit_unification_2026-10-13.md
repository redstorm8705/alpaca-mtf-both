# Swing exit unification — replay + BGGN (design record, 2026-10-13, Claude-signed)

## Question
CEO directive 2026-10-13: one Swing tier, one exit and profit-taking rulebook. The proposed default was to give
breakout lots (MSFT) the legacy 12-point engine's breakeven / profit lock / trail. Because that changes the
breakout strategy's tested exit, it was replayed first.

## Replay
Script + outputs (OCI, git-ignored): `logs/lab/c2/swing_exit_replay.py`, `swing_exit_replay_summary.json`,
`swing_exit_replay_{A,B,B2,C,C2,D}.csv`. Same 387 breakout entries, Oct 2016 - Sep 2026, point-in-time
S&P 500 + NDX membership, daily top-10 by 60-day median dollar volume, close > prior 55-day closing high and > SMA200,
entry next open. Daily bars; stops in force on day t are computed through t-1; gaps fill at the open; 5 bp per side.

| Variant | mean %/trade | win | PF | paired vs A (t) | slot-limited total, 1 slot |
|---|---|---|---|---|---|
| A as live (2.5 ATR stop, 20-day-low close, 30 sessions) | +2.80 | 50% | 1.91 | - | +312% |
| B legacy engine (1.5 ATR stop, T1/T2 locks, T3 exit) | +1.17 | 50% | 1.55 | -1.66 (-3.0), worse 9/11 yrs | +178% |
| B2 legacy locks on the 2.5 ATR stop + 30 sessions | +1.48 | 62% | 1.59 | -1.34 (-2.7), worse 9/11 yrs | +167% |
| D A + breakeven at +1 ATR | +2.14 | 31% | 2.03 | -0.66 (-2.2), worse 8/11 yrs | +194% |
| C2 A + nightly broker stop at the 20-day low | +2.64 | 48% | 1.88 | -0.15 (-1.1) | +270% |

Capacity matters: at 2 slots B2 leads (+391% vs A +214%), at 4 slots A leads again. Measured live capacity is ONE
lot: production `trade_events.jsonl` since 2026-10-03 shows 6 signals, MSFT held, and AMD (2026-10-07) refused
"no size" (total-overnight room $200; 2% risk cap $50 < one share's $63.30 stop distance).
Limits: overlapping trades overstate t; daily-bar fills; not beta-adjusted.

## BGGN (round 3, aligned 4/4)
Board Thorp + Taleb (masked-loss seat), Gro (gpt-oss-120b), GAI (gemini-3.1-flash-lite). Rounds 1-2 needed
counter-prompts on false premises (kill switch closing positions; PLTR "in loss"; invented capacity weights and
"B2 no losing year").
- Q1 single rulebook: **A** (breakout exits) for every new Swing trade. No legacy locks on breakout lots.
- Q2 MSFT stop to breakeven: **No** (variant D costs -0.66%/trade).
- Q3 PLTR: **finishes on its legacy engine**; its stop never goes below $206.78.
- CORRECTION (same day): the legacy exit engine is NOT PLTR-only. Day-tier lots promoted to Swing
  (`execution/day_promotion.py` adopt_promotions, DAYTRADE_PROMOTION_ENABLED) become Swing tracker trades and are
  managed by `exit_logic.py`; mean-reversion entries would be too (MR_ENABLED=False today). The replay above tested
  breakout entries only; it says nothing about promoted Day lots. The legacy engine cannot be retired until promoted
  lots have their own tested rule.
- Re-examine B2 if live capacity reaches 2+ lots (Taleb); reassess C2 at the real-money conversion (Thorp).

## Status
No code change. Awaiting Rafael's decision on the package (keep A instead of the planned unification).

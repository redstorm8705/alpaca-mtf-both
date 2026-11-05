# Sizing audit — which size controls can actually change the share count (2026-11-05, Claude signed)

Read-only code audit of every sizing path, at `origin/main` `152de542`. Example inputs: equity $2,700, prices $20-$500,
config values as committed. Live state files (`logs/kelly_stats.json`, `data/state/overnight_k.json`,
`data/state/earned_sizing.json`) are not in the repo, so **how often** each control binds live is unmeasured; whether
it **can** bind is from the code arithmetic. Key claims re-checked at source by the author (marked ✓).

## A. Controls that can never block or cut the way they are described (safety-relevant first)
1. **Swing margin-buffer check always buys at least 1 share.** ✓ `execution/swing_breakout_manager.py:401`
   `new = max(1, min(int(qty), q_max))` — when the gap check finds the margin buffer already used up (`q_max ≤ 0`), it
   still returns 1 share; its error paths also return 1 (`:395-396`, `:404-406`). A check meant to keep a one-day gap
   inside the margin buffer can never stop an entry.
2. **Core stacked size multiplier** (event, regime, time of day, SPY, session P&L, overnight; "half size" in high vol)
   changes no shares unless it is 0. ✓ `execution/entry_logic.py:1371` vs `:1397-1398`. REGIME-SIZE-1104.
3. **Core earnings half-size can end up larger.** ✓ The Leg-2 rescue (`execution/entry_logic.py:1458-1478`) lifts a
   0-1 share result to 2 shares whenever `size_multiplier > 0`, with no earnings check. Example: score 10, $450 stock
   near earnings: halved budget buys 0 → rescued to 2 shares ($900), where without earnings it bought 1 and skipped.
4. **Day-tier conviction sizing has no effect on shares.** ✓ `execution/day_trade_manager.py:532-535`:
   `safe_qty = min(risk_qty, notional_qty)`; the conviction share count (`requested_qty`) is only a "≥ 1" check. Only
   conviction ≤ 0 (no trade) acts.
5. **Day 10/10 "maximum size" and Track M's "0.5× risk at start"** match normal sizing unless the stop is wider than
   about 2.3-4.2% of price; the per-name dollar room binds first (`day_trade_manager.py:481-551`; config
   `DAYTRADE_TRACK_M_*`). [example-based; frequency unverified]
6. **Core leveraged ETFs**: the 5%-of-equity ring-fence always binds (`entry_logic.py:1339-1346`), so Kelly, TSMOM,
   volume, FVG, earnings and score sizing never act on them; a leveraged ETF above about $67.50 can never trade on the
   core tier (fewer than 2 shares, and Leg-2 excludes leveraged names). [current ETF prices unverified]
7. **Core Kelly above its warm-up level** and most budget nudges (TSMOM up-leg, FVG, volume cap) are overridden by the
   volatility cap (VOTE-5) for names with 20-day vol above roughly 18-21% at score ≥ 11.
8. **Swing 2% per-lot cap** can never bind: the risk fraction is capped at 2% before it (`swing_breakout_manager.py:314`,
   `:346-350`). `SWING_BREAKOUT_SLOT_PCT` ("one slot = 20% of equity") is not read by Swing sizing at all; a lot can
   reach about 28% of equity.
9. **trend_night quarter-Kelly budget above 7%** never binds (the gap limit is at most the 7% kill); the 5% floor binds
   only when the rest of the book's overnight gap risk is small (`execution/swing_overnight.py:428-475`).
10. **QHM dip-add 1.375× ceiling** is below the 20% per-name cap only at a 10% target, so it never acts at 15% or 20%
    targets (`execution/quarterly_hold_manager.py:184`, `:2709-2733`).
11. **Day Track-B budget cap** is off by the owner's 10-28 decision (`config.py:800`), but the docstring at
    `day_trade_manager.py:423` still says "default ON". CONV-1018 "up-sizing" mostly tightens the stop rather than
    adding shares when the dollar room binds; `earned_sizing.py:10-11` says a losing band "takes no entry" but the code
    trades it at today's size (`day_trade_manager.py:2678-2690`).

## B. Controls that do the sizing (bind as designed)
Core: score gate (< 10 skips), value cap `dollar_cap / price`, VOTE-5 vol cap, Kelly per-trade risk clamp, Leg-1/2.
Day: per-trade risk basis (wide stops) and the per-name / gross / buying-power / maintenance rooms.
Swing: stop-based `q_risk`, K-limb room, 1-share floor. trend_night: K room, gap limit, buying power.
QHM: tranche target × available equity, 40% aggregate cap, overnight room. Overnight K: binds as designed.

## C. Recommended order (each through the normal gate; risk-path where it changes size)
1. **Swing gap check (A1)** — let it return 0 when the buffer is used up or its inputs are unknown. Narrows the
   envelope (fewer, not larger, entries in a margin squeeze); owner approval (fewer trades).
2. **Earnings vs Leg-2 (A3)** — the rescue should not undo the earnings cut. Fewer shares only on earnings names.
3. **Honest logs** — every entry logs which term set its share count (REGIME-SIZE-1104 item 2 extended to Day and
   Swing), and the misleading notes/docstrings (A2, A4, A5, A11) are corrected.
4. **Owner decisions, after a replay of logged entries:** whether the inert stress cuts (A2, A5) should start working,
   whether Day conviction should size (A4), and the leveraged-ETF core path (A6).

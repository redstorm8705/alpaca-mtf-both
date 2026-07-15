# GEX flip-strike bug — root-cause diagnosis (2026-07-14)

**Symptom:** SPY 2026-07-14 spot $751-752, but computed `flip_strike` $670-701 all day (~10% BELOW
spot). Not a usable S/R level. Regime label also inverted vs realized (labeled NEGATIVE/NEAR-FLIP =
trend/amplify, but SPY was dead-flat 0.62% range = pin). GEX Layer-8 fired 35×, raised MIN_SCORE,
filtered 10+ signals → 0 entries. Wrong data actively costs trades.

**File:** `data/gex.py` (465 lines). `get_gex_regime()` feeds `kelly.py` + `run_cycle` Layer-8 —
EXECUTION-CONSUMED, so any fix is a gated change needing board + Gro + GAI.

## ROOT CAUSE (exact lines)
1. **Strike survivor bias — `gex.py:280`** `(_MAX_SPREAD_RATIO=0.25)` + **`276-277`** zero-bid skip.
   ATM options carry the WIDEST relative bid-ask on Alpaca's *indicative* feed, so the 25% spread
   filter + zero-bid filter disproportionately drop ATM/near-spot strikes, while deep-ITM puts
   (tighter spreads) survive. ~50-60% of contracts skipped, biased toward low strikes.
2. **Fragile flip definition — `gex.py:316-327`.** Flip = the LOWEST strike where the ascending
   cumulative-GEX sum first changes sign. Puts get `sign=-1.0` (line 299); the surviving deep-ITM
   low-strike puts dominate the early cumulative sum → it crosses zero at a depressed strike far
   below spot. Standard practice is the crossing NEAREST spot (or peak-|gamma| strike), not the lowest.
3. **Prior partial fix — `gex.py:100-110`.** Expiry window was narrowed to this-week-only (Rafael
   2026-07-06) targeting THIS exact symptom ($670 vs $752). It didn't fully work because the real
   drivers are #1 + #2, not the expiry window.
4. Gamma is computed locally via Black-Scholes (`_bs_gamma` L205; IV bisection `_implied_vol` L210)
   from quote mid — feed carries no greeks/OI on the snapshot endpoint. r=0.045, q=0.012. This part
   is sound; the problem is WHICH strikes survive to feed it.

## FIX OPTIONS (board design round needed — Open Question Protocol)
- **A. Relax the ATM spread filter** (e.g. a wider `_MAX_SPREAD_RATIO` or a moneyness-aware
  threshold near spot) so ATM strikes are not systematically dropped.
- **B. Symmetric strike window around spot** — only compute the flip over strikes within ±X% of spot
  so far-ITM survivors can't drag it.
- **C. Redefine "flip"** as the zero-crossing NEAREST spot (or the peak-total-gamma strike) instead of
  the lowest crossing.
- Likely a combination (B + C). All change an execution-consumed signal → full gate + board.

## BACKTEST (Rafael's ask — AFTER the fix, else it measures noise)
Raw material: `logs/gex_history.jsonl` (15-min snapshots since ~07-03) + Alpaca SPY bars.
(1) level-respect: touch→reject vs break on subsequent bars; (2) regime predictive power: realized
range conditioned on label (does NEGATIVE actually precede wider range?); (3) recalibrate the
_NEAR_FLIP_RATIO / label thresholds from data (no static).

# Regime object Phase 2 — board design alignment (2026-09-14)

**Status:** DESIGN aligned by the board (BGG). NOT built, NOT approved to build. This records the
unanimous board design + the non-negotiable guardrail so the eventual build is pre-scoped. The
decision to build (risk-path) is Rafael's.

**Board pass:** 2 cold seats (López de Prado + Simons; Thorp + Taleb) + Gro (openai/gpt-oss-120b).
GAI unavailable this pass (transient 503 across models) — per the Gro-skip-if-unavailable rule, board
majority + one external suffices for a DESIGN alignment; GAI to be included on the eventual build diff.

## What Phase 2 was proposed to do
Replace the STATIC regime thresholds (vol LOW<12 / HIGH>25 driving size_mult 1.25/1.0/0.5 + stop_mult 1.5;
market-MR mean_reverting iff variance_ratio<1.0 OR Hurst<0.5) with ROLLING EMPIRICAL DISTRIBUTIONS derived
from accumulated data — per the standing "no static regimes" principle.

## Unanimous board conclusions (3/3 substantive voices independently converged)

1. **Confine Phase 2 to the VOL signal only. Do NOT empiricize variance_ratio / Hurst.** VR=1.0 and
   Hurst=0.5 are the THEORETICAL random-walk nulls (absolute character), not arbitrary constants.
   Percentile-izing them can INVERT the signal (in a persistently trending tape the least-trending
   tercile is still trending, yet gets stamped "mean-reverting" → green-lights bad shorts). The MR
   estimators are already noisy at 60 bars; terciling noise = noise-on-noise. Keep the anchors.

2. **Build the vol distribution from HISTORICAL VIX/SPY series, NOT the 1/day ledger.** Realized-vol/VIX
   are deterministic functions of series the bot already fetches on demand (data.fetcher, arbitrary
   num_bars; VIX history to 1990). This collapses the "wait months for samples" problem — decades of the
   distribution exist today. The regime_history ledger (Phase 1) is a freshness/monitoring trail, NOT the
   primary distribution source. (Correction to an earlier framing: Phase 2 is NOT data-blocked.)

3. **THE NON-NEGOTIABLE GUARDRAIL — one-sided clamp (all 3 voices, verbatim-aligned):**
   `size_mult_final = min(empirical, static(vol))` and `stop_mult_final = max(empirical, static(vol))`.
   The empirical path may ONLY ever make sizing MORE conservative than the static path, NEVER more
   aggressive — mathematically impossible to widen the size/stop envelope, while still free to tighten it.
   Plus: (a) the 1.25× low-vol size-up stays gated behind the ABSOLUTE vol<12 anchor (empirical can never
   be the SOLE reason to lever up — requires agreement of empirical-bottom-tercile AND vol<12);
   (b) defense (0.5×/1.5×) fires on EITHER empirical-top-tercile OR vol>25 (take the more defensive);
   (c) size-cut and stop-widen stay atomically coupled; (d) fall back to static on insufficient/stale
   samples (the ledger's *_fresh flags drive this).
   Rationale: Thorp — overbetting past Kelly drives long-run growth negative / risk of ruin; underbetting
   is merely suboptimal, so every risk-path estimator must bias smaller. Taleb — never wire a fragile
   quantile estimate to a convex (leverage) decision; adaptive thresholds "normalize danger" (a
   crash-elevated vol becomes the new normal tercile, and the bot stops de-risking exactly at peak tail
   risk) — the min() clamp + absolute anchors prevent both the size-up-into-turbulence and the
   withhold-defense vectors.

4. **Statistic + window:** empirical terciles / order statistics (33rd/67th percentile), NOT z-scores
   (vol is right-skewed / fat-tailed — moments mislead). Long EWMA-weighted window (~250 trading days /
   1yr min, 504–756 / 2–3yr target). Effective-sample gating: daily vol is strongly autocorrelated
   (ρ~0.6–0.94), so raw N must be haircut to N_eff (VIF); the repo already reasons this way (config.py:565
   GEX ρ~0.7 VIF 5.67; KELLY_MIN_SAMPLE_SIZE=30 warmup-with-static-fallback precedent).

5. **Hysteresis / dwell + dead-band** around each boundary (require k consecutive days in a tercile before
   the label flips) — regimes are persistent; the classifier must be too, or the size_mult whipsaws
   1.0↔1.25 on noise. (RenTech's early regime work used HMM/transition matrices — persistence modeled, not
   assumed away.)

## Sequence (board: Option B — design now, HOLD the risk-path build)
1. **Prereq — fix the field-mixing bug** (logged tb_audit_log 2026-09-14): volatility_regime.py:118-130
   stores VIX level vs SPY-realized-vol into the SAME `realized_vol` field depending on which fetch wins —
   contaminates any distribution over that field. Key by source or pick one series first. (Risk-path — its
   own gate.)
2. **Build a SHADOW empirical vol classifier from historical VIX/SPY** — LOG-ONLY, changes nothing on the
   risk path (non-behavioral; standard gate, no board-on-diff needed if the Rule-B trace confirms
   size/frequency/concurrency all zero). Emit the empirical label + the clamped size_mult alongside the
   live static one.
3. **Shadow-validate:** confirm the clamped empirical path NEVER sizes above static, doesn't whipsaw
   (passes hysteresis), and is net-beneficial, over ≥~60 trading days.
4. **Only then — with Rafael's go-ahead + full board-on-diff + Gro + GAI on the exact diff — flip to the
   risk path, conservative-only** (empirical may de-risk; the 1.25× size-up stays gated behind absolute
   vol<12 indefinitely). Gate live-wiring behind ≥~250 fresh daily samples spanning ≥1 LOW→HIGH vol
   transition + ≥60 days shadow-agreement.

## Safety envelope — UNCHANGED
7% paper kill switch, paper=True, never-mask-a-loss, data-source tiers all stay in force. The clamp lives
INSIDE the envelope and only ever pulls exposure inward — "more aggressive within the envelope is NOT what
this does; it can only tighten."

## Load-bearing files (verified)
strategy/volatility_regime.py (12/25 thresholds, size/stop map, the field-mixing bug L118-130);
strategy/run_cycle.py:1049-1050,1677 (regime_size_mult in the 6-factor stack); execution/entry_logic.py:1338-1367
(int(raw_shares * size_multiplier), no per-trade clamp ≤1.0); execution/mr_regime.py:44-108 (VR/Hurst anchors);
config.py:461-486 (regime constants, KELLY_MIN_SAMPLE_SIZE=30 precedent); strategy/regime_state.py (Phase-1
ledger + *_fresh flags); data/fetcher.py (arbitrary-num_bars history available today).

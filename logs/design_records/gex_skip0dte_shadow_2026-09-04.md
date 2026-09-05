# Design record — GEX skip-0DTE shadow validator (read-only soak)

**Date:** 2026-09-04 · **Requested by:** Rafael (approved the GEX day-tier fix) · **Author:** Claude
**Covers:** `scripts/gex_skip0dte_shadow.py` (new) · **Status:** design locked, building through the code gate

## Why
The day-tier placed ZERO trades its first live session because `data.gex.get_gex_regime`/`get_gex_levels`
returned UNKNOWN + no pin for every symbol all day. Verified root cause: `_expiry_range()` returns
`today → coming Friday`, which on expiry/daily-expiry days collapses to a **0DTE-only window**; 0DTE
contracts are gutted by the time-to-expiry floor + zero-bid → 0 valid contracts → UNKNOWN label AND
`pin.kind="none"` (`levels_ok=False`) → Layer-B STAND_DOWN + main-bot MIN_SCORE inflation.

Board (2 cold seats) + Gro + GAI convened 2026-09-04: unanimous to (D) gate the day-tier off the pin
`levels_ok` not the label, unanimous reject (E) loosening the quality gate; split A(roll)/B(skip-0DTE)
on the window fix. Live test confirmed **skip-0DTE restores a valid, near-spot signal** (SPY: NEAR-FLIP,
547 valid, capture 0.63, flip 1.3% from spot, pin centroid at spot, levels_ok=True). The risk seat's
hard requirement: **shadow-soak before it drives live sizing** — prove the fixed-window labels/pins are
sane + near-spot over ≥1 live session incl. an expiry Friday, not just "non-UNKNOWN."

This validator IS that soak + the pre-market readiness sim that should have run before today.

## Feature Design Protocol
1. **Data source / tier.** Reuses `data.gex._fetch_contracts` / `_fetch_snapshots` / `_compute_gex`
   (Alpaca T1, the live GEX pipeline's own functions) with a **skip-0DTE** expiry window (start = the
   next expiry after today, through +9 calendar days). Spot from the Alpaca stock latest-trade REST
   (verified usable during RTH). No new data path.
2. **Output.** One line per symbol per cycle to `logs/gex_skip0dte_shadow.jsonl` (label, contract_count,
   capture_ratio, quality_ok, flip_strike + flip-vs-spot %, pin.kind, levels_ok, spot, window). No Slack
   during the soak (log-only) — a summary is added only if Rafael wants it.
3. **Integration point.** New standalone `scripts/gex_skip0dte_shadow.py`, RTH cron (`*/15`). It **does
   NOT modify `data/gex.py`**, does not write `gex_snapshot.json`, and does not touch live GEX, sizing,
   the day-tier, or any order path. Pure read + append to its own log.
4. **Failure mode.** Read-only + fail-safe: any fetch/compute error is caught per-symbol and logged;
   never raises, never writes shared state, never posts a wrong number anywhere live.
5. **Board vote?** Not for the SHADOW (read-only, logs only, zero execution impact). The eventual LIVE
   window fix (patching `_expiry_range`) IS risk-path — it already has the board+Gro+GAI design pass
   (this session) and will get this soak's evidence + a fresh full-read + cold-2nd + Gro/GAI preship
   before it flips live.

## Success criterion (what the soak must show before we flip the live fix)
Over ≥1 full RTH session including an expiry Friday: skip-0DTE yields `quality_ok=True` /
`levels_ok=True` for the liquid names most cycles, with `flip_strike` and `pin.centroid` within a few
% of spot (no $670-vs-$752 far-from-spot regression). Then the `_expiry_range` change ships live +
the day-tier's Layer-B is confirmed to act on the resolved pin.

## Not in v1
- The live `_expiry_range` patch (follows the soak).
- Part 1 day-tier pin-action tweak (rides with the live flip).

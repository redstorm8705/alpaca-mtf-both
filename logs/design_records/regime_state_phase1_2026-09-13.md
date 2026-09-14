# Authoritative regime object — Phase 1 design record (2026-09-13)

**Feature:** `strategy/regime_state.py` — one authoritative, read-only `RegimeState` aggregation
object that composes the three existing market-wide regime views into a single object and writes a
canonical ledger. **Phase 1 is NON-BEHAVIORAL / NON-RISK-PATH:** it changes NO consumer, NO gating,
NO sizing — it only ADDS the aggregation object + the ledger. Consumers migrate in Phase 3; rolling
empirical distributions (killing the static thresholds) are Phase 2 (risk-path, separately gated).

**Origin:** the Layer-3 "no authoritative regime object" + Layer-8 "no canonical current-regime /
history ledger" gaps (regime-architecture review, 2026-09-13). Rafael approved the Phase-1-first
phasing ("Proceed with phase 1").

## What it aggregates (all READ-ONLY)

1. **Volatility (market-wide, real-time):** instantiate `strategy.volatility_regime.RegimeDetector`,
   call `get_regime()` (LOW/NORMAL/HIGH + size/stop mults) + `get_composite_regime()`
   (BULL/BEAR/HIGH_VOL/NEUTRAL, VIX/VIX3M term, SPY/50SMA, 20d rvol). Fetches VIX/SPY bars.
2. **Macro (market-wide, structural, weekly):** read the existing cache `logs/macro_regime_latest.json`
   (label / confidence / composite_score / risk_tier) — do NOT re-run the weekly macro analysis. Its
   `generated_at` drives the freshness flag (bound ~9 days, since it is a Sunday-weekly cron).
3. **Market mean-reversion (market-wide, real-time):** `execution.mr_regime.regime_state(SPY_daily_df)`
   (variance_ratio / hurst / mean_reverting) on SPY daily bars — the MARKET MR state (mr_regime is
   otherwise per-symbol; feeding it SPY yields the market-level view).

## Contract

- **Object:** `RegimeState` = `{ts, vol:{...,fresh,age_s}, macro:{...,fresh,age_s}, market_mr:{...,fresh},
  summary:{vol_regime, macro_label, mean_reverting, any_stale}}`. Each component carries its own
  freshness/staleness flag + age.
- **Freshness/fail-safe (per the "shared freshness + normalization contract" goal):** each component is
  independently guarded. A failed/stale component is marked `fresh=False` + its value `UNKNOWN`/`None` —
  NEVER a spurious value. The aggregate never raises; a total failure yields an all-UNKNOWN object
  (safe for a display/ledger; Phase-3 consumers must treat UNKNOWN as neutral, same as today's
  per-signal fail-safes).
- **Output ledger:** `logs/regime_state.json` (current authoritative state, atomic tmp->replace) +
  `logs/regime_history.jsonl` (append one compact line per computation — the history ledger). `logs/`
  only.

## Feature Design Protocol — the 5 answers

1. **Data source / tier.** No new source: the vol regime's existing VIX/SPY (via data.fetcher T1 +
   the vol module's own VIX fetch), the macro cache file, and SPY T1 bars for the MR read. No new tier.
2. **Output.** `logs/regime_state.json` + `logs/regime_history.jsonl`, atomic writes, `logs/` only.
3. **Integration point.** NONE in Phase 1 — additive. `strategy/regime_state.py` defines the object +
   a `main()` snapshot runner; NO existing consumer imports or calls it yet. It is invoked on demand
   (or an optional read-only cron). Consumers are migrated in Phase 3.
4. **Failure mode.** Each component fail-safe (UNKNOWN/stale); the aggregate never raises; a missing
   macro cache -> macro UNKNOWN+stale; a VIX/bar fetch failure -> that component UNKNOWN. The ledger
   write is atomic and best-effort (a write failure logs, never crashes).
5. **Board vote required?** **NO for Phase 1** (non-behavioral aggregation — no sizing/scoring/gating/
   order change; nothing on the trading path calls it). Gated as ordinary new bot-code: design record
   (this) + statics + cold-2nd + Gro/GAI preship + log-exempt. **Phase 2 (rolling distributions) and
   Phase 3 (consumer migration) ARE risk-path and get the full board gate.**

## Why it is genuinely non-risk-path

`strategy/regime_state.py` is a new module that NOTHING on the trading path imports in Phase 1 (grep-
verified at ship). It reads regime signals and writes a ledger. It cannot change an entry, exit, size,
or gate because no consumer reads it yet. The three underlying regimes keep their exact current wiring.

## Gate plan

statics (py3.14 + OCI py3.10) -> cold-2nd PASS -> Gro/GAI preship APPROVE (Gro may be waived if the
file exceeds Groq's 8k TPM, per the standing rule) -> this design record -> log-exempt. Ran as a
snapshot to validate before ship. No OCI service restart (not wired into any service).

# Tier Refactor + Risk-Governance Redesign — Design & BGG Consensus (2026-07-14)

**Owner mandate (2026-07-14):** "Bucket A and Bucket B should be DELETED and removed. The tiers
are: intraday/intraweek, QHM, and F6. That's it." + earlier same session: remove the hard
position-count cap (be in as many responsible positions as sensible for the $25K goal).

**Status:** Design COMPLETE + BGG-aligned (Gro + GAI + 2 cold board seats: ruin[Thorp/Taleb],
architecture[Simons/Harris]). Split into TWO staged patches. Stage 1 approved + building.
Stage 2 QUEUED on TWO owner decisions (below).

---

## SHIPPED this session (LIVE)
- **Slack-relief** (`b2f79db`): `scripts/memory_watchdog.sh` — was the PRIMARY un-throttled Slack
  spammer (<200MB */30). Added `alert_once()` 30-min per-category throttle + flock; restart action
  unchanged. Gate: bash -n, Gro+GAI APPROVE. Cleared stale root-owned `/tmp/mtf_planned_restart`
  (Jul-3, nightly CATASTROPHIC #2). Deployed OCI. **Follow-ups still open:** the `*/5` "bot DOWN"
  watchdog needs a consecutive-fail grace; `ram_watch.sh`/`rss_sampler.sh` consolidation; and the
  ROOT cure = the main.py memory leak (Option A). Also: sentinel ownership bug (bot runs as ubuntu,
  can't rm a root-owned sentinel) — the cleanup path needs a consistent-ownership fix.

## Audit findings this session (diagnosis)
- **Position-count drift (nightly CATASTROPHIC #1) = FALSE ALARM.** Board 4/4: over-entry is
  doubly-guarded (main.py P0-STARTUP Alpaca-authoritative override + entry_logic CYCLE-SYNC-up
  before every entry). The `0` after SIGTERM is a transient corrected before any entry decision.
  Residual: the refuse-to-reconcile-DOWN branch is a correct safety asymmetry; the endorsed
  improvement (3-cycle-persistent Alpaca-authoritative downward reconcile + alert) is a low-pri
  optimization, NOT a capital fix. `logger.critical` does NOT route to Slack (so the drift CRITICAL
  is log-noise only, not a Slack source).
- **GEX/S&R levels are NOT trustworthy yet (bug).** 2026-07-14: SPY spot $749-753 but computed
  `flip_strike` sat $670-701 (~10% BELOW spot) all day — not a usable S/R level. Likely root =
  strike-set truncation (~50-60% of contracts skipped: zero_bid/wide_spread/no_iv on the Alpaca
  indicative feed) → net-gamma-zero solved from a downward-biased strike subset. Regime label said
  NEGATIVE/NEAR-FLIP (trend/amplify) but SPY was dead-flat 0.62% range (pin behavior) = a miss.
  GEX Layer-8 fired 35×, raised MIN_SCORE, filtered 10+ signals, 0 entries → wrong data actively
  costs trades. **FIX the flip-strike computation BEFORE any GEX backtest** (garbage in → garbage
  out). Raw material for backtest: `logs/gex_history.jsonl` (15-min snapshots since ~07-03) +
  Alpaca SPY bars. Backtest scope: (1) level-respect (touch→reject vs break), (2) regime predictive
  power (realized range conditioned on label), (3) recalibrate thresholds from data (no static).
- **MISSING buying-power pre-flight check = latent ruin/desync bug (independent of the cap).** The
  bot sizes a position and submits the market order with NO check of remaining Alpaca buying power;
  it relies on Alpaca to reject, which then drifts tracker vs risk. Fixed as part of Stage 1.

---

## THE RISK-GOVERNANCE REDESIGN (approved 2026-07-14)
Primary governing variable = **gross-notional / margin headroom**, NOT position count.
- `MAX_OPEN_POSITIONS`: 4 → **20** (runaway-loop circuit-breaker ONLY, never the active limit).
- **NEW buying-power pre-flight check** before every order (fail-closed) — the latent-bug fix.
- **NEW `MAX_GROSS_EXPOSURE_RATIO = 2.5`** (sum |notional| / equity) — the real governor (~$6,950 now).
- **NEW `MAX_OVERNIGHT_EXPOSURE_PCT = 0.40`** (was unset — Invariant #11 gap).
- Keep sector + pairwise-correlation gates. Replace P0-STARTUP HALT-at-MAX with a gross-exposure
  health check. Drift reconcile still matters (state integrity).

## THE TIER COLLAPSE (BGG consensus, 4 voices)
| Fork | Consensus |
|------|-----------|
| 1. Leveraged ETFs (TQQQ/SQQQ/TSLL/NVDL) | KEEP, folded into unified tier; retain long-only + wider vol-stops + BoD-2 3x-panic-block as per-symbol attributes (via `LEVERAGED_TICKERS`/`LEVERAGED_3X_TICKERS`, not a bucket) |
| 2. "intraweek" meaning | **OWNER DECISION.** Rec = Option A: same strategy, hold winners ~1 week via existing trailing-stop/target + overnight budget (NOT a new framework). Option B (separate HTF-confluence swing framework, per the 2026-06-28 mandate) = multi-session build, defer. |
| 3. Sizing | Conviction-linear + Kelly, governed ONLY by gross-exposure 2.5× + 2% risk + BP check. NO per-tier % constant. |
| 4. Migration | Delete BUCKET_A_*/BUCKET_B_* constants + `calculate_bucket_a_size` + `is_bucket_a` routing + power-hour expansion; rename `calculate_bucket_allocation` → `calculate_unified_allocation` (logic unchanged); rename `is_bucket_a` long-only gate → leveraged long-only. |
| 5. **RUIN GUARD (mandatory)** | **OWNER DECISION on value.** Deleting Bucket A WITHOUT a replacement notional cap is LETHAL: a 3x ETF at high conviction could size to ~$70k notional on $2.8k = 25× gross = bankruptcy before the equity kill switch fires. MUST add `LEVERAGED_NOTIONAL_MAX_PCT` in the SAME patch that deletes Bucket A. Rec = **5% per-symbol** (ruin seat; closest to old ~$139/name intent); architecture seat said 15% (looser — old 15% was aggregate across ALL leveraged, so per-symbol 15% is weaker); GAI ~20% aggregate. |

### 🔴 HARD INVARIANT: `LEVERAGED_NOTIONAL_MAX_PCT` must ship in the exact patch that removes Bucket A. Never delete Bucket A first and add the guard later.

---

## STAGING (safest path — GAI)
- **STAGE 1 = the risk-governance redesign UNDER the existing buckets** (approved, building now).
  MAX_OPEN 4→20 + BP pre-flight + gross 2.5× + overnight 0.40 + replace P0-STARTUP HALT. Buckets
  stay intact so Bucket A's 15% still ring-fences leveraged names — NO lethal exposure. Files:
  `config.py`, `execution/risk_manager.py`, `execution/entry_logic.py`, `main.py`. Full patch
  sequence + final Gro+GAI on the diff before ship.
- **STAGE 2 = delete the buckets + add `LEVERAGED_NOTIONAL_MAX_PCT`** (QUEUED on the 2 owner
  decisions). One-patch (ruin+architecture seats) or preparatory-first (GAI) — decide at build.
  Detailed migration blueprint: architecture seat output (config constants to delete, functions to
  rename/delete, exact entry_logic lines). Key: `calculate_bucket_allocation`→`calculate_unified_allocation`,
  delete `calculate_bucket_a_size`, delete `is_bucket_a` branch (entry_logic ~1089-1132), unify
  sizing, add the leveraged notional clamp AFTER all multipliers.

## OWNER, WHEN BACK — two answers unlock Stage 2:
1. **FORK 2:** "intraweek" = A (hold-longer, contained refactor) or B (separate HTF swing framework, deferred)?
2. **FORK 5:** `LEVERAGED_NOTIONAL_MAX_PCT` value — 5% per-symbol (rec) / 15% / 20% aggregate?
Reply e.g. **"A, 5%"** → build + gate + ship Stage 2.

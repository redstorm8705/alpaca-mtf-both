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

---

## STAGE 1 — EXACT PATCH SPEC (pre-scoped, ready to build+gate+ship)

**CRITICAL: the bot runs `--profile paper`, so the paper-profile override is what's LIVE.
Patch the profile dict, not just the base constant.**

### config.py
- **L206** base: `MAX_OPEN_POSITIONS = 4` → `20` (circuit-breaker; base/live-default).
- **L265** paper profile: `"MAX_OPEN_POSITIONS": 7,` → `"MAX_OPEN_POSITIONS": 20,`  ← **THE ACTIVE VALUE**.
- Leave `live` profile (L248) at 4 (conservative, not active; revisit at live launch).
- **After L210** (`MAX_DAILY_LOSS_PCT`), add module-level (not profile-overridden):
  ```python
  # AGGREGATE EXPOSURE GOVERNANCE (2026-07-14, board+Gro+GAI) — the PRIMARY governor.
  # Gross notional is the real limit on position count; the count cap is only a
  # runaway-loop circuit-breaker. Applies across profiles.
  MAX_GROSS_EXPOSURE_RATIO   = 2.5   # sum |position notional| / equity — block entry if breach
  MAX_OVERNIGHT_EXPOSURE_PCT = 0.40  # overnight notional / equity (Invariant #11 gap; was unset)
  ```
- validate_config() bucket-sum check (L540-547) UNCHANGED in Stage 1 (buckets still exist).

### execution/risk_manager.py — add 2 methods (after can_open_position, ~L288)
```python
def check_buying_power_for_order(self, shares: int, entry_price: float) -> bool:
    """Live Alpaca buying-power pre-flight. Reject if remaining BP can't cover
    notional + 10% cushion. FAIL-CLOSED (return False) on any error — never over-commit.
    Uses the same requests + os.getenv pattern as _qhm_unrealized_pl()."""
    # notional = shares*entry_price; require buying_power >= notional*1.10; else False.
```
```python
def check_gross_exposure_for_order(self, tracker, entry_price: float, shares: int) -> bool:
    """Sum |open-position notional| (from tracker.open_trades non-closed) + new notional;
    reject if > portfolio_value * config.MAX_GROSS_EXPOSURE_RATIO. Log the breach."""
```

### execution/entry_logic.py — in execute_entries, immediately BEFORE `order = submit_market_order(...)` (~L1317, after the `if shares < 1: continue` + short-block-cache checks)
```python
# ── Buying-power pre-flight (fail-closed) — closes the latent over-commit/desync bug ──
if not risk.check_buying_power_for_order(shares, entry_price):
    logger.warning(f"[{symbol}] BP pre-flight failed — skipping entry.")
    _rc8_clear_buffers(symbol, "bp-insufficient")
    continue
# ── Aggregate gross-exposure gate (2.5x equity) — the primary count governor ──
if not risk.check_gross_exposure_for_order(tracker, entry_price, shares):
    logger.warning(f"[{symbol}] gross-exposure cap reached — skipping entry.")
    _rc8_clear_buffers(symbol, "gross-exposure-cap")
    continue
```

### main.py — P0-STARTUP block (~L799-865)
- With MAX_OPEN_POSITIONS=20, the existing HALT-at-MAX rarely fires (keep as backstop).
- ADD a startup gross-exposure health check: compute live gross notional / equity; if
  `>= config.MAX_GROSS_EXPOSURE_RATIO`, log CRITICAL + `_set_halt_entries(True)` (fail-closed,
  matches the block's existing exception handling).

### Gate to run before ship (mandatory)
py_compile + mypy --warn-unreachable + ruff (E,W,F,B) on all 3 .py files; cold-2nd agent on the
combined diff; code-review-graph impact; board-on-diff (ruin + execution seats); FINAL Gro+GAI on
the exact diff. Then apply → commit → push → OCI `git pull --ff-only` + `systemctl restart mtf-bot`
(this DOES restart — RTH-execution code) → health check. On paper=True; zero live-money risk.

### Behavior after Stage 1 (validated by board): ~3-5 concurrent positions in practice (sizing +
BP self-limit), count cap no longer the binding constraint, over-commit bug closed, gross exposure
capped at 2.5x. No lethal leveraged exposure (Bucket A's 15% ring-fence still intact until Stage 2).

---

## STAGE 2 — CRITICAL STRUCTURAL FINDING (from the live-code read, 2026-07-15)

**Owner-delegated decisions locked (board defaults, "then stage 2" + trust-the-board):**
- FORK 2 intraweek = **Option A** (hold winners longer under existing exit logic; contained refactor).
- FORK 5 leveraged cap = **`LEVERAGED_NOTIONAL_MAX_PCT = 0.05` per-symbol** (ruin-seat value; safest,
  ≈ the old ~$139/name ring-fence on a $2.8K account). Adjustable later.

**THE STRUCTURE (entry_logic.py, current line numbers post-Stage-1):**
- L592-593: `is_bucket_a = symbol in config.BUCKET_A_TICKERS`.
- L607-611: `if is_bucket_a and direction == "short":` long-only skip.
- **L1088-1132: the sizing split.** `if is_bucket_a:` (L1089-1110) sizes leveraged names by
  `calculate_bucket_a_size()` + the 15%/leverage notional cap ONLY. `else:` (L1111+) is the FULL
  unified-sizing path — conviction-linear (L1112-1132) **AND the entire multiplier chain**
  (Kelly L1134, AB-3 TQI L1147, then TSMOM, earnings, VOTE-3, FVG, min-lot … through ~L1265) is
  ALL inside this `else:` block. **Bucket A SKIPS every multiplier.**

**⇒ The unification is a ~150-line restructure, not a branch delete.** All symbols must flow
through the `else:`-block sizing; then the leveraged clamp applies AFTER the multiplier chain.

**EXACT EDIT PLAN (safest, avoids a 150-line manual de-indent):**
1. **entry_logic L592-593:** replace `is_bucket_a = symbol in config.BUCKET_A_TICKERS` with
   `is_leveraged = symbol in config.LEVERAGED_TICKERS` (LEVERAGED_TICKERS already exists, == the old
   Bucket-A set {TSLL,NVDL,TQQQ,SQQQ}).
2. **entry_logic L607-611:** long-only gate → `if is_leveraged and direction == "short":`.
3. **entry_logic L1088-1111:** DELETE the whole `if is_bucket_a:` A-branch (1089-1110) AND the
   `else:` line (1111) — promote the else-body to run unconditionally. Cleanest mechanical form:
   replace `        if is_bucket_a:\n …A… \n        else:` with just the sizing comment header, and
   the former else-body (L1112+) must lose ONE indent level. Do this as a single Read-the-block →
   Write-the-dedented-block edit (NOT many small edits), then `py_compile` catches any indent slip.
4. **entry_logic — AFTER the last size multiplier (FVG, ~L1215, before `raw_shares`):** add the
   leveraged clamp:
   `if is_leveraged: dollar_cap = min(dollar_cap, risk.portfolio_value * config.LEVERAGED_NOTIONAL_MAX_PCT)`
   with a log line. THIS IS THE RING-FENCE REPLACEMENT — must be present or the deletion is LETHAL.
5. **entry_logic L1094-1095:** remove the `_main._BUCKET_A_LEVERAGE` / `_main._BUCKET_A_MAX_NOTIONAL_PCT`
   refs (they die with the A-branch).
6. **config.py L49-56:** delete BUCKET_A_TICKERS, BUCKET_A_ALLOCATION_PCT, BUCKET_A_MIN_HOLD_DAYS,
   BUCKET_B_ALLOCATION_PCT, BUCKET_B_MAX_POSITIONS, BUCKET_B_MAX_POSITIONS_POWER. Add
   `LEVERAGED_NOTIONAL_MAX_PCT = 0.05`. NOTE: BUCKET_B_ALLOCATION_PCT (0.85) is USED at L1115-1116
   for `_pct_min/_pct_max` — replace those with literals (0.425 / 0.85) or a new
   `INTRA_ALLOCATION_PCT = 0.85`. VOLUME_CONFIRMATION comment at config L430 references BUCKET_A —
   cosmetic. `validate_config()` L540-547 bucket-sum check → delete.
7. **risk_manager.py:** delete `calculate_bucket_a_size` (~L930 post-Stage-1); rename
   `calculate_bucket_allocation`→`calculate_unified_allocation` (or leave — it's only called by
   is_bucket_a path? verify callers). Also `calculate_bucket_a_size` caller was entry_logic L1093 (deleted).
8. **DEAD-REF SWEEP (mandatory):** `grep -rn "BUCKET_A\|BUCKET_B\|calculate_bucket_a\|_BUCKET_A_LEVERAGE\|is_bucket_a"`
   across the repo — catch main.py `_main._BUCKET_A_LEVERAGE`/`_BUCKET_A_MAX_NOTIONAL_PCT` defs,
   quarterly_hold, scan_to_html, volume-confirmation Bucket-A auto-pass (config comment L430-431).
9. **Gate:** py_compile (catches de-indent), ruff, mypy; cold-2nd MUST verify the leveraged clamp is
   present + correct (ring-fence); ruin/masked-loss seat (LETHAL path); Gro+GAI; preship markers.
   Ship after close (or dark) — this changes live sizing for leveraged names.

**RISK NOTE:** this is a delicate ring-fence refactor with a bankruptcy-class downside if the clamp
is dropped. Build with care + lean on py_compile (indent) + cold-2nd (clamp presence). Per the
INTERACTIVE-vs-API cost protocol this is the "identified major build" for a focused implement pass.

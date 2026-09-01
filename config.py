# ruff: noqa: E501
"""
Central configuration for the MTF Confluence Bot.
All strategy parameters live here — tweak without touching logic files.
"""

# ─── UNIVERSE ────────────────────────────────────────────────────────────────

SP500_URL  = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
EXCLUSIONS     = ["BRK.B", "BRK.A"]
MIN_AVG_VOLUME = 1_000_000

# ─── WATCHLIST ────────────────────────────────────────────────────────────────────────────

# Core watchlist — always scanned every cycle regardless of premarket
# Expanded 2026-06-30 (board 5-0 + Gro + GAI): from 24→36 tickers.
# Added high-volatility tech, semis, and EV names for more intraday signal
# opportunities. Scan time estimate: ~54s per cycle — within Alpaca limits.
WATCHLIST = [
    # Bucket B — original core
    "NVDA", "AAPL", "TSLA", "AMD",  "PLTR",
    "COIN", "SMCI", "TOST", "SOFI", "MSTR",
    "CRWD", "CRM",  "PANW", "UBER", "AMZN",  # AMZN re-added 2026-08-03: external/manual-close
    # re-entry cooldown (PR #80) is now LIVE, which is the systemic fix for the churn that caused
    # the 2026-08-03 emergency removal. AMZN is also a FOREVER6_UNIVERSE name (separate tier).
    "META", "NFLX", "SPY",  "QQQ",
    # Bucket B — expansion (2026-06-30): high-vol momentum / vol arb names
    "MSFT", "GOOGL", "NET",  "SNOW", "DDOG",
    "ADBE", "AVGO",  "RBLX", "RIVN", "HOOD",
    "MARA", "SOXL",  "SOXS",
    # Bucket A — leveraged ETF swing holds (allocation PCT below)
    "TSLL", "NVDL", "TQQQ", "SQQQ",
]

# Leveraged ETF tickers — bot applies a wider stop/target for these
# 1.5x–2x underlying (TSLL, NVDL): 2.5x stop/target multiplier
LEVERAGED_TICKERS           = {"TSLL", "NVDL", "TQQQ", "SQQQ"}
LEVERAGED_STOP_MULTIPLIER   = 2.5   # TSLL, NVDL: 1.5x–2x underlying
LEVERAGED_TARGET_MULTIPLIER = 2.5   # TSLL, NVDL: 1:1 R:R minimum

# 3x leveraged ETFs (TQQQ, SQQQ): wider stop/target to absorb 3x daily moves
LEVERAGED_3X_TICKERS           = {"TQQQ", "SQQQ"}
LEVERAGED_3X_STOP_MULTIPLIER   = 3.0   # 3x underlying — extra cushion
LEVERAGED_3X_TARGET_MULTIPLIER = 3.0   # maintain 1:1 R:R

# ─── UNIFIED TIER ALLOCATION (Bucket A/B collapsed 2026-07-15, board + Gro + GAI) ─────
# Active tiers are: intraday/intraweek (this), QHM, and Forever-6. The old Bucket A (leveraged
# ETF, 15%) / Bucket B (85%) ALLOCATION split is DELETED — all symbols size via the unified
# conviction + Kelly path; leveraged names are ring-fenced by LEVERAGED_NOTIONAL_MAX_PCT and
# aggregate exposure is governed by MAX_GROSS_EXPOSURE_RATIO. The leveraged-ETF SET now lives in
# LEVERAGED_TICKERS (defined above) — BUCKET_A_TICKERS is removed.
INTRA_ALLOCATION_PCT           = 0.85   # full-conviction per-position dollar-cap (was BUCKET_B_ALLOCATION_PCT)
LEVERAGED_NOTIONAL_MAX_PCT     = 0.05   # 3x/2x ETF hard notional ceiling as % of equity (ring-fence — board FORK-5)
LEVERAGED_MIN_HOLD_DAYS        = 1      # leveraged ETFs: min 1 trading day before exit (was BUCKET_A_MIN_HOLD_DAYS)
# Position-COUNT mechanism (NOT an allocation tier) — power-hour slot expansion. Count is now
# governed primarily by MAX_OPEN_POSITIONS(=20 circuit-breaker) + MAX_GROSS_EXPOSURE_RATIO.
# (Cosmetic follow-up: rename these off the "BUCKET_B" prefix.)
BUCKET_B_MAX_POSITIONS         = 999    # std position-count placeholder
BUCKET_B_MAX_POSITIONS_POWER   = 5      # power-hour / AH slot expansion (≥3:30 PM ET)
TOD_EXPANSION_WINDOW_START     = 15 * 60 + 30  # 3:30 PM ET — power-hour expansion window (minutes-since-midnight)

# ── Conviction tiers ─────────────────────────────────────────────────────────
# 11-12: full allocation (up to 95% of portfolio as dollar risk cap)
# 10:    half allocation (up to 47.5% of portfolio as dollar risk cap)
# below 10: skip — not enough confluence
CONVICTION_FULL_MIN      = 9      # 9+/12 = full size (lowered from 11 — board 5-0 2026-06-30)
CONVICTION_HALF_MIN      = 8      # 8/12 = half size (lowered from 10)
CONVICTION_SKIP_BELOW    = 8      # below 8/12 = no trade (lowered from 10)

# ─── PRE-MARKET MOVER FILTER ────────────────────────────────────────────────────────────

# Any ticker moving more than this % in pre-market gets added to the scan
# alongside the standing watchlist above
PREMARKET_MOVER_THRESHOLD_PCT = 2.0   # ±2% pre-market move qualifies
PREMARKET_MOVE_MAX_PCT        = 35.0  # sanity cap — moves > ±35% treated as bad data, skip

# Pre-market reversal-pause gate (board 2/2 + GAI, 2026-07-01; Gro deferred — TPD limit).
# Consumed by run_premarket_gate.py (9:25 AM ET cron) and run_cycle.py 10:05 re-validation.
PREMARKET_RETRACE_THRESHOLD  = 0.50  # gap must retrace ≥50% from pm_high toward prior_close
PREMARKET_MIN_SCORE          = 8     # min MTF score at 10:05 re-validation (matches CONVICTION_HALF_MIN)
PREMARKET_KELLY_MULT         = 0.50  # size = 50% of normal Kelly output (Rafael locked)
PREMARKET_ATR_MULT           = 0.625 # stop = 0.625× normal ATR multiplier (Rafael locked)

# ─── VOLATILITY FILTER (ATR-based expected move) ───────────────────────────────

# Only scan stocks whose 14-day ATR is at least ATR_MIN_PCT of price.
# Filters out dead/low-vol stocks that won’t move enough to cover spread.
# Example: $100 stock needs ATR ≥ $1.50/day to pass the filter.
ATR_PERIOD    = 14    # standard ATR lookback
ATR_MIN_PCT   = 1.5   # minimum ATR as % of price to be scannable
ATR_BOOST_PCT = 3.0   # ATR% above this → logged as "high volatility"


# ─── TIMEFRAMES ──────────────────────────────────────────────────────────────

TF_1M    = "1Min"
TF_5M    = "5Min"
TF_15M   = "15Min"
TF_30M   = "30Min"
TF_1H    = "1Hour"
TF_4H    = "4Hour"
TF_12H   = "12Hour"
TF_DAILY = "1Day"
TF_WEEKLY= "1Week"
# TF_MONTHLY (2026-07-21) — added for the confluence-scanner MONTHLY horizon tier
# (Rafael-approved scanner tiering: intraday/weekly/monthly × bull/bear). The monthly
# state uses a 10-month SMA + 12-1 monthly momentum, so ~36 bars gives the 10-month SMA
# a full history plus headroom. Alpaca supports TimeFrameUnit.Month.
TF_MONTHLY = "1Month"

BARS_TO_FETCH = {
    TF_15M:   150, TF_30M:  400, TF_1H:   100,  # S43: 500→150, 300→100 — RAM leak fix (DS+GAI Q7; EMA30 needs ~107 bars, MACD26 needs ~93)
    TF_4H:    200, TF_12H:  150, TF_DAILY: 365, TF_WEEKLY: 104,
    TF_MONTHLY: 36,   # ~3 years — 10-month SMA + 12-1 momentum need 13+; 36 gives headroom
}

# ─── ALPACA BAR FETCH: GLOBAL RATE LIMITER + SHARED TTL CACHE ─────────────────
# Board + Gro + GAI consensus 2026-07-06 (RC: scanner + main bot raced for the
# shared Alpaca quota → 429 backoff cascades → ~21-min scanner gaps). A GLOBAL
# rate limiter (data/fetcher.py) caps TOTAL bar throughput across scanner AND
# main bot so they can no longer race past the quota; a shared TTL cache stops
# them double-fetching identical bars (~halves requests).
# Conservative default: 175/min stays under the 200/min free-tier ceiling even
# if "unlimited paper" is stale. Raise only AFTER empirically verifying the real
# 429 threshold (parallelization is deferred until then).
ALPACA_MAX_REQ_PER_MIN   = 175   # global cap across all bar fetchers
# Cache TTL (secs): dedupes scanner/main-bot fetches of the same (symbol,tf,bars)
# within a cycle. 180s is well inside the Data-Quality Contract's 15-min bar-
# staleness bound, and the real-time SPY entry gate uses alpaca_data.py (quotes),
# NOT this bar path — so scoring bars up to 180s old are safe.
ALPACA_BAR_CACHE_TTL_SECS = 180

# ─── MOVING AVERAGES ─────────────────────────────────────────────────────────

EMA_FAST = 13
EMA_SLOW = 30
SMA_20   = 20
SMA_150  = 150
SMA_200  = 200
SMA_325  = 325

# ─── MACD ────────────────────────────────────────────────────────────────────

MACD_STANDARD = {"fast": 12, "slow": 26, "signal": 9,  "label": "MACD_STD"}
MACD_FAST     = {"fast": 3,  "slow": 15, "signal": 3,  "label": "MACD_FAST"}

MACD_INTRADAY_TFS = [TF_15M, TF_30M, TF_1H]
MACD_SWING_TFS    = [TF_4H, TF_12H, TF_DAILY]

# ─── RSI ─────────────────────────────────────────────────────────────────────

RSI_PERIOD       = 14
RSI_OVERBOUGHT   = 70
RSI_OVERSOLD     = 30
RSI_NEUTRAL_HIGH = 60
RSI_NEUTRAL_LOW  = 40

# ─── VWAP ────────────────────────────────────────────────────────────────────

VWAP_PROXIMITY_PCT = 0.015   # Within 1.5% of VWAP counts as "near" (was 0.5% — too tight)

# ─── CONFLUENCE SCORING ──────────────────────────────────────────────────────

MIN_LONG_SCORE  = 4   # lowered from 5 — catches more signals in choppy/bear markets
MIN_SHORT_SCORE = 4   # lowered from 5 — mirrors long threshold

SCORE_WEIGHTS = {
    "daily_above_150sma":  2,
    "daily_above_200sma":  1,
    "ema13_above_ema30":   2,
    "macd_bullish_cross":  2,
    # Shadow mode (VOLUME_CONFIRMATION_ENABLED=False): RSI scores using this key.
    # Live toggle: change "rsi_in_range": 1 -> "volume_confirmed": 1 simultaneously
    # with setting VOLUME_CONFIRMATION_ENABLED = True (two-step atomic toggle, Kim R2).
    "rsi_in_range":        1,
    "price_near_vwap":     2,
    # Jegadeesh-Titman (1993, 2023): 30+ years of peer-reviewed evidence
    # 12-month minus 1-month lookback — proven factor, equal weight to SMA conditions
    "momentum_12_1":       2,
}

# Momentum lookback parameters (Jegadeesh-Titman standard formulation)
MOMENTUM_LONG_LOOKBACK  = 252   # ~12 months of trading days
MOMENTUM_SHORT_LOOKBACK = 21    # ~1 month skip (avoids short-term reversal)
MOMENTUM_MIN_PCT        = 0.0   # price must be higher than N days ago to pass (long)

# ─── DELTA-OF-SIGNAL SHADOW (Cedar "trade the delta, not the level") ──────────
# Board + Gro + GAI consensus 2026-07-06. Mirrors the VOLUME_CONFIRMATION shadow
# pattern (gated flag, log-only until proven). DELTA_SCORING_ENABLED=False =
# SHADOW: strategy/delta_shadow.py logs the bar-over-bar change in the
# feature-isolated (non-momentum_12_1) confluence score to trade_events.jsonl,
# with ZERO impact on scoring / sizing / entries. Flip to True ONLY after >=50
# shadow samples show independent edge (LdP feature-importance) — and that flip
# requires a FRESH board vote (Architecture Invariant #1: SPY 5-min bar-over-bar
# stays the sole entry gate; a proven delta only ever adjusts the quality bar).
DELTA_SCORING_ENABLED     = False
DELTA_OFF_FENCE_THRESHOLD = 3   # non-momentum score jump that flags "off the fence"
DELTA_PERSISTENCE_BARS    = 2   # consecutive scans a jump must hold (5-min noise filter)

# ─── TRADE MODES ─────────────────────────────────────────────────────────────

class TradeMode:
    INTRADAY = "intraday"
    SWING    = "swing"

SWING_SCORE_BONUS_REQUIRED          = 2
INTRADAY_CLOSE_MINUTES_BEFORE_EOD   = 15

# ─── RISK MANAGEMENT ────────────────────────────────────────────────────────────────────────────

# Per-trade risk: 2% of portfolio per trade — professional standard, do not raise
MAX_PORTFOLIO_RISK_PCT = 0.02

# Max concurrent positions: now a RUNAWAY-LOOP CIRCUIT-BREAKER only (2026-07-14, board+Gro+GAI).
# It is NO LONGER the active limit on how many positions the account carries — the real governor
# is MAX_GROSS_EXPOSURE_RATIO (aggregate notional) + the buying-power pre-flight check. Count is a
# poor proxy for risk (4 correlated names can be riskier than 20 uncorrelated ones). Raised 4→20
# so it never binds in normal operation but still stops a bug that tries to open hundreds.
MAX_OPEN_POSITIONS     = 20

# Daily kill switch: tightened 5% → 3% for first 30 days of live running
# Once bot behavior is confirmed, raise back to 5%
MAX_DAILY_LOSS_PCT     = 0.03

# ─── AGGREGATE EXPOSURE GOVERNANCE (2026-07-14, board + Gro + GAI) ────────────────
# PRIMARY governor of position count now that MAX_OPEN_POSITIONS is a circuit-breaker.
# Module-level (applies across profiles). Enforced in execution/entry_logic.py before each
# order via RiskManager.check_gross_exposure_for_order / check_buying_power_for_order.
MAX_GROSS_EXPOSURE_RATIO   = 2.5   # sum(|open position notional|) / equity — block new entry if breach
MAX_OVERNIGHT_EXPOSURE_PCT = 0.40  # overnight notional / equity (Architecture Invariant #11 gap; was unset)

# ─── STOP / TARGET (ATR-based multipliers) ───────────────────────────────────────────

# Stops/targets expressed as ATR multiples — auto-widen on volatile days
# Intraday R:R = 1:2  |  Swing R:R = 1:3
INTRADAY_STOP_ATR_MULT   = 1.0   # raised from 0.5x — backtested evidence shows 0.5x stopped by noise on large-caps
INTRADAY_TARGET_ATR_MULT = 2.0   # raised from 1.0x — maintains 1:2 R:R with wider stop

SWING_STOP_ATR_MULT      = 1.5   # 1.5x daily ATR — survives normal pullbacks
SWING_TARGET_ATR_MULT    = 4.5   # 4.5x daily ATR — 1:3 R:R

# Fallback fixed-pct stops — only used if ATR data unavailable
INTRADAY_STOP_PCT   = 0.008   # raised from 0.5% — more room on $50–$200 stocks
INTRADAY_TARGET_PCT = 0.016   # 1:2 R:R maintained
SWING_STOP_PCT      = 0.025   # raised from 2%
SWING_TARGET_PCT    = 0.075   # 1:3 R:R maintained

# ─── EXECUTION ───────────────────────────────────────────────────────────────

ORDER_TYPE         = "market"
LIMIT_SLIPPAGE_PCT = 0.001

# RC-4 fill-reconciliation retry window, in minutes (2026-07-16 root-cause fix;
# board + Gro + GAI). How long a fill_unverified exit stays eligible for the
# reconciler to keep retrying its Alpaca fill lookup before it is declared EXPIRED
# (CRITICAL + Slack). This is a RETRY BUDGET only — it does NOT widen the Alpaca
# query itself, which is independently bounded by the trade's own entry_time inside
# fill_helpers._derive_close_lower_bound (plus a protective-side filter and a ±50%
# sanity band). So a wider window cannot match a fill a narrower one wouldn't; it
# only buys more attempts.
# WHY 90: the window MUST comfortably exceed the real cycle cadence or the reconciler
# is structurally unable to fire. SCAN_INTERVAL_INTRADAY is 5 min but OBSERVED cycles
# run ~5.5-6 min (13:30:24 / 13:36:22 / 13:41:53 on 2026-07-16), so the old hardcoded
# 5-min window gave AT MOST ONE attempt and usually ZERO — RIVN's open-auction cover
# (fills at 13:32:51-13:34:15, recoverable) was first touched at 14:06 and found
# already "expired" → real +$0.51 stayed recorded as $0.00; the same mechanism recorded
# RIVN's real -$41 as $0.00 on 7/7 plus 6 other trades. 90 min ≈ 15 attempts and
# survives a stalled cycle or a restart. Anything below ~15 is structurally broken.
# FLOOR: values < 5 are clamped by fill_reconciler — portfolio_tracker.mark_fill_expired
# hardcodes a 5-min floor guard, and a smaller window there would silently skip expired
# trades and re-queue them forever.
RC4_RECONCILE_WINDOW_MINUTES = 90

# ─── SCAN SCHEDULE ───────────────────────────────────────────────────────────

SCAN_INTERVAL_INTRADAY = 5    # minutes
SCAN_INTERVAL_SWING    = 60

# ─── TRADING PROFILES ────────────────────────────────────────────────────────
# Switch via: python3 main.py --profile paper   (aggressive, for testing)
#             python3 main.py --profile live    (conservative, real money)
# All other config values are the DEFAULT (live-safe) values.
# Paper profile overrides are applied at runtime in main.py.

PROFILES = {
    "live": {
        # Conservative — real $1,000, protect capital first
        "MAX_PORTFOLIO_RISK_PCT":  0.02,   # 2% per trade
        "MAX_OPEN_POSITIONS":      4,
        "MAX_DAILY_LOSS_PCT":      0.03,   # 3% kill switch
        "INTRADAY_STOP_ATR_MULT":  1.0,
        "INTRADAY_TARGET_ATR_MULT":2.0,
        "SWING_STOP_ATR_MULT":     1.5,
        "SWING_TARGET_ATR_MULT":   4.5,
        "SCAN_INTERVAL_INTRADAY":  5,      # every 5 min
        "MIN_LONG_SCORE":          4,
        "MIN_SHORT_SCORE":         4,
        "KELLY_FRACTION":          0.25,   # quarter-Kelly — conservative
        "KELLY_MAX_RISK_PCT":      0.02,   # 2% per-trade cap — closes the 6%-inversion (2026-08-02, BGG):
                                           # live MUST be <= paper (0.045) and < its own 3% daily kill so one
                                           # trade can't blow the day. Coherent with live's 2% baseline +
                                           # quarter-Kelly. Live posture is re-ratified by the board at the
                                           # $25K real-capital launch; this only removes the inverted 6% ghost.
        "MR_AGG_RISK_CAP_PCT":     0.015,  # MR correlated-basket sub-cap: < the 3% live daily kill, ~1x the
                                           # 2% live clamp (masked-loss seat 2026-08-03) — a real sub-cap.
        "PARTIAL_EXIT_ENABLED":    True,
        "PARTIAL_EXIT_RATIO":      0.5,    # close 50% at first target
        "PARTIAL_EXIT_ATR_MULT":   1.0,    # take first half at 1x ATR
    },
    "paper": {
        # Bucket-aware paper profile
        "MAX_PORTFOLIO_RISK_PCT":  0.04,   # fallback only — bucket sizing overrides this
        "MAX_OPEN_POSITIONS":      20,     # 7→20 (2026-07-14): now a circuit-breaker only; real limit is MAX_GROSS_EXPOSURE_RATIO + BP pre-flight (board+Gro+GAI)
        "MAX_DAILY_LOSS_PCT":      0.07,   # 7% kill switch — board vote 2026-04-22 (25-1, Thorp dissent 0.10)
        "INTRADAY_STOP_ATR_MULT":  1.20,  # tightened 1.25→1.20 S52 — DS/GAI floor for 2x ETF universe; board floor was 1.10
        "INTRADAY_TARGET_ATR_MULT":2.5,   # 2:1 R:R minimum
        "SWING_STOP_ATR_MULT":     1.2,
        "SWING_TARGET_ATR_MULT":   5.0,
        "SCAN_INTERVAL_INTRADAY":  5,      # every 5 min — now scanning 36 tickers
        "MIN_LONG_SCORE":          8,      # lowered 10→8 — board 5-0 + Gro + GAI 2026-06-30 (aggressiveness mandate)
        "MIN_SHORT_SCORE":         8,      # mirrors long — board 5-0 2026-06-30
        "KELLY_FRACTION":          0.50,   # raised 0.35→0.50 half-Kelly — board 5-0 + Gro + GAI 2026-06-30
        "KELLY_MAX_RISK_PCT":      0.045,  # 4.5% hard cap — board vote S52 (unchanged)
        "MR_AGG_RISK_CAP_PCT":     0.035,  # MR correlated-basket sub-cap: = half the 7% paper daily kill,
                                           # < the 4.5% clamp (masked-loss seat 2026-08-03). This is the value
                                           # the RUNNING paper bot uses — UNCHANGED from the prior module default.
        "PARTIAL_EXIT_ENABLED":    True,
        "PARTIAL_EXIT_RATIO":      0.5,
        "PARTIAL_EXIT_ATR_MULT":   0.8,
    },
}

# Active profile — set by --profile flag at runtime, default live
ACTIVE_PROFILE = "live"

# ─── TIME-OF-DAY FILTERS ─────────────────────────────────────────────────────
# Markets have distinct behavioral phases. Avoid the noise windows.

# Opening 30 min (9:30–10:00 ET): high spread, algo wars, fakeouts
# We wait until 10:00 AM for the first real signal
TOD_MARKET_OPEN_BUFFER_MINS  = 30   # 30-min no-entry window (9:30–10:00 ET) — P5-H4 fix, aligns with AB-4

# Power hour start (3:00–3:30 PM ET): momentum resumes — re-enable entries
# Closing 15 min (3:45–4:00 ET): EOD flatten zone — no new entries
TOD_EOD_NO_ENTRY_MINS        = 15   # stop new entries 15 min before close
TOD_POWER_HOUR_START         = 15 * 60        # 3:00 PM ET in minutes-since-midnight
TOD_MARKET_CLOSE             = 16 * 60        # 4:00 PM ET

# Pre-close stop-coverage sweep window (board 3-0 + Gro + GAI, 2026-09-01): in the final
# PRECLOSE_SWEEP_MINUTES before the REAL close (Alpaca clock next_close — half-day aware, NOT
# the hardcoded 16:00 above), run_cycle fires reconcile_protection(session="rth", place=True) once
# per cycle to guarantee every open intraday/daytrade position has a live DAY stop covering its
# full qty before the overnight GTC window. 15 (not a tighter value) so the window spans ≥2 of the
# observed ~5.5-6 min RTH cycles and cannot silently never-fire (config.py TOD comment precedent).
PRECLOSE_SWEEP_MINUTES       = 15

# Mid-day doldrums (12:00–2:00 PM ET): low volume, choppy, spreads widen
# Bot will still run but applies a 0.75x size multiplier in this window
TOD_MIDDAY_START             = 12 * 60        # 12:00 PM ET
TOD_MIDDAY_END               = 14 * 60        # 2:00 PM ET
TOD_MIDDAY_SIZE_MULT         = 0.75           # 75% size during doldrums

# ─── DIAGNOSTIC ENTRY RESTRICTIONS ──────────────────────────────────────────
SHORTS_BANNED = False  # session-scoped mechanism pending (S13); legacy fallback — do not use

# ─── VOLATILITY REGIME ───────────────────────────────────────────────────────
# Bot detects current market volatility regime and adjusts behavior.
# ─── VOLATILITY TIER CLASSIFICATION ─────────────────────────────────────────
# Auto-classified at runtime using 20-day realized volatility from momentum data
# Thresholds based on annualized vol: extreme >80%, high 50-80%, standard <50%
VOLATILITY_TIER_EXTREME_THRESHOLD = 0.80   # annualized rvol > 80% = extreme
VOLATILITY_TIER_HIGH_THRESHOLD    = 0.50   # annualized rvol > 50% = high

# Stop multipliers by volatility tier (intraday / overnight)
VOL_TIER_EXTREME_STOP_INTRADAY  = 2.5   # MSTR, COIN, NVDL, TSLL, TQQQ, SQQQ
VOL_TIER_EXTREME_STOP_OVERNIGHT = 3.5
VOL_TIER_HIGH_STOP_INTRADAY     = 2.0   # TSLA, NVDA, PLTR, SMCI, AMD
VOL_TIER_HIGH_STOP_OVERNIGHT    = 2.5
VOL_TIER_STD_STOP_INTRADAY      = 1.25  # AAPL, AMZN, META, NFLX, CRM, etc.
VOL_TIER_STD_STOP_OVERNIGHT     = 2.0

# ATH proximity gate — dynamic MIN_SCORE raise near 52w high
ATH_MIN_SCORE_RAISE_PCT = 1.0  # raise dynamic MIN_SCORE +1 when SPY within 1% of 52w high
# AWP audit fix (2026-06-30): was 2.0. At 1.7% from ATH with VIX=16.5, the old
# threshold combined with the market-top compound (+1 at Orange zone_tier 2) to
# push the effective floor to 12/12, locking out all entries in a normal bull
# market for an entire session. Board 4/4 + Gro + GAI unanimous: 1.7% from ATH
# is normal bull market noise, not a meaningful distance constraint.


# VIX-based stop widening — replaces size reduction
# Size multiplier removed: high VIX validates directional conviction
VIX_STOP_WIDEN_THRESHOLD_1 = 25.0   # VIX > 25: stops widen to 1.5x
VIX_STOP_WIDEN_THRESHOLD_2 = 30.0   # VIX > 30: stops widen to 2.0x
VIX_STOP_WIDEN_MULT_1      = 1.5
VIX_STOP_WIDEN_MULT_2      = 2.0

# VIX-adjusted overnight breakeven buffer (Shaw — board vote 2026-04-21)
VIX_BE_WIDEN_THRESHOLD_1 = 20.0   # VIX ≥ 20 → 0.40×ATR buffer
VIX_BE_WIDEN_THRESHOLD_2 = 30.0   # VIX ≥ 30 → 0.50×ATR buffer

# ── FOREVER-6 STARTER TIER (Rafael 2026-07-13, BGG-locked: logs/f6_starter_bgg_2026-07-13.md) ──
# A never-sell conviction tier ABOVE QHM. The STARTER rule ESTABLISHES 1-3 anchor names on a
# market-wide dip. CASH-ONLY (no margin — the cold board proved margin makes the never-sell book a
# hostage to any other strategy's worst day: a maintenance call would force-liquidate the anchors).
# Ships DARK until the module is wired + live-validated.
FOREVER6_ENABLED = False                       # master flag — DARK until validated live
FOREVER6_UNIVERSE = ["TSLA", "GOOGL", "AMZN", "CRWD", "META", "NVDA"]  # curated, grows manually
# Dynamic starter trigger: SPY down ≥ max(2.0, 0.15×VIX)% on the CLOSE — keeps it a ~2σ event across
# regimes (VIX 13→2% floor, 20→3%, 27→4%). Board Shaw/Dalio formula; NOT a static −2% (no-static rule).
FOREVER6_STARTER_TRIGGER_FLOOR_PCT = 2.0
FOREVER6_STARTER_TRIGGER_VIX_SLOPE = 0.15
FOREVER6_STARTER_MAX_NAMES = 3                 # fund at most 3 names per starter event (breadth-first)
FOREVER6_STARTER_MAX_EVENTS_PER_MONTH = 4      # per-month event cap (anti-overtrade)
# Segregated starter budget: the starter may spend at most this fraction of SETTLED CASH per event
# (Thorp/Taleb catch — the shallow starter must never cannibalize the deep crash-ladder's dry powder),
# and never draw settled cash below the floor reserve.
FOREVER6_STARTER_CASH_FRAC_PER_EVENT = 0.20
FOREVER6_STARTER_CASH_FLOOR = 200.0

# Forever-6 EXIT — trims only, never a full sell (logs/forever6_integration_map_2026-07-09.md
# §6 + logs/forever6_scenario_board_2026-07-09.md, board-locked constants carried over unbuilt
# until 2026-08-05). Trim 25% of current forever6 holdings at +1000% (10x) unrealized gain,
# another 25% of what remains at +2000% (20x) — the house-money core shrinks, never disappears.
# No stops, no max-hold, no other exit path exists for this tier.
FOREVER6_TRIM_1_MULT = 10.0                    # first trim trigger: 10x unrealized gain (+1000%)
FOREVER6_TRIM_1_FRAC = 0.25                    # trim this fraction of CURRENT forever6 qty
FOREVER6_TRIM_2_MULT = 20.0                    # second trim trigger: 20x unrealized gain (+2000%)
FOREVER6_TRIM_2_FRAC = 0.25                    # trim this fraction of the POST-trim-1 remaining qty

# Overnight hold — reversal confirmation scan count
# Bot requires this many consecutive reversal scans before closing overnight position
# At 5-min intervals: 10 scans = 50 min, 15 scans = 75 min
OVERNIGHT_REVERSAL_SCAN_MIN = 10   # minimum scans to confirm reversal (~7:20 AM PDT)
OVERNIGHT_REVERSAL_SCAN_MAX = 15   # maximum scans before forced exit (~7:45 AM PDT)
RTH_REVERSAL_SCAN_MIN       = 6    # RTH positions: 6 scans = 30 min sustained reversal before exit

# Overnight 20%+ profit — trailing stop activates at entry profit level
OVERNIGHT_RUNNER_THRESHOLD  = 0.20  # 20% profit triggers runner mode

# Portfolio review thresholds — revisit stop/size rules at each increment
PORTFOLIO_REVIEW_INCREMENTS = [7000, 12000, 17000, 22000, 27000]

# Based on SPY 10-day realized volatility as VIX proxy (no options feed needed).

VIX_API_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d"

REGIME_LOW_VOL_THRESHOLD     = 12.0   # annualized vol% below this = low vol grind
REGIME_HIGH_VOL_THRESHOLD    = 25.0   # annualized vol% above this = panic/expansion

# Size multipliers per regime
REGIME_LOW_VOL_SIZE_MULT     = 1.25   # trending low-vol = size up slightly
REGIME_NORMAL_SIZE_MULT      = 1.0    # baseline
REGIME_HIGH_VOL_SIZE_MULT    = 0.5    # panic = half size

# In high-vol regime, widen stops automatically
REGIME_HIGH_VOL_STOP_MULT    = 1.5    # multiply stop distance by this in panic

# ─── KELLY CRITERION ─────────────────────────────────────────────────────────
# Dynamic position sizing based on observed edge per signal type.
# Kelly fraction = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
# We use fractional Kelly (25–50%) to avoid over-betting.
# Minimum 10 trades per signal type required before Kelly kicks in.
# Falls back to MAX_PORTFOLIO_RISK_PCT until enough data exists.

KELLY_FRACTION               = 0.25   # default: quarter-Kelly (overridden by profile at runtime)
KELLY_MIN_SAMPLE_SIZE        = 30     # per-type warmup threshold (board vote Apr 2026: LdP + Simons)
KELLY_MAX_RISK_PCT           = 0.045  # conservative fallback default (2026-08-02, BGG): was 0.06 (S23) — an
                                      # inversion, since the paper profile (S52) tightened to 0.045 while the
                                      # live profile inherited this higher 6% default. Both profiles now set
                                      # KELLY_MAX_RISK_PCT explicitly (paper 0.045, live 0.02); this default is
                                      # the fail-safe floor for any un-profiled run — never above paper again.
KELLY_MIN_RISK_PCT           = 0.0075 # floor: always risk at least 0.75% (board vote Apr 2026: Thorp)

# ─── A2: ATH DRAWDOWN ADAPTATION ─────────────────────────────────────────────
# Kelly fraction scales DOWN linearly from 1.0× at DD_START to MULT_FLOOR at DD_MAX.
# Scale-DOWN only — 1.0× is the ceiling. Board vote 2026-05-16, unanimous DS+GAI+board.
KELLY_A2_DD_START   = 0.05   # drawdown below this → A2_mult=1.0 (inactive)
KELLY_A2_DD_MAX     = 0.15   # drawdown at or above this → A2_mult=KELLY_A2_MULT_FLOOR
KELLY_A2_MULT_FLOOR = 0.33   # minimum Kelly multiplier at peak drawdown

# ─── ORB GATE (Feature Gate 9 — board vote 2026-05-17, 26/26 unanimous) ───────
# Opening Range Breakout: require SPY 5-min bar-close confirmation above/below
# the 9:30–9:44 ET high/low before allowing long/short entries.
# Fail-closed: any feed failure → BLOCK_ALL entries for the session.
ORB_ENABLED               = True
ORB_WINDOW_MINUTES        = 15      # opening range duration: 9:30–9:44 ET (15 min)
ORB_COMPUTE_AFTER_MIN     = 595     # 9:55 AM ET — 1 full bar after range closes
ORB_BLOCK_ON_FEED_FAILURE = True    # fail-closed: BLOCK_ALL if SPY bars unavailable

# ─── VOLUME CONFIRMATION (Feature Gate 10 — board vote 2026-05-17, 19/19 CONDITIONAL APPROVE) ─
# Replace rsi_in_range (1pt) with volume_confirmed (1pt) in the 12pt scoring system.
#
# TWO-STEP LIVE TOGGLE (Kim R2 — do both changes atomically):
#   Step 1: VOLUME_CONFIRMATION_ENABLED = True
#   Step 2: SCORE_WEIGHTS — change "rsi_in_range": 1  ->  "volume_confirmed": 1
# Partial toggle (only one step) leaves scoring key mismatch — always do both together.
#
# Shadow mode (VOLUME_CONFIRMATION_ENABLED=False):
#   - RSI scores normally (zero behavior change to live system)
#   - Volume ratio computed + logged as VOLSHADOW (structured JSON) — zero score impact
# Live mode (VOLUME_CONFIRMATION_ENABLED=True):
#   - Bucket A (BUCKET_A_TICKERS: TQQQ/TSLL/NVDL/SQQQ): auto-pass 1pt (volume unreliable)
#     C1 TODO: suspend Bucket A auto-pass when MRI >= STRESSED+ (requires MRI threading)
#   - Bucket B: pass if current_volume >= VOLUME_THRESHOLD x avg(last 20 bars, excl. today)
#     Uses iloc[-21:-1].dropna() — ascending order guaranteed by Alpaca T1 daily bars
#   - Insufficient history (< VOLUME_MIN_VALID_BARS): skip, do not penalize
#
# Shadow review protocol (Kim R2) — run after 5 trading days:
#   grep "VOLSHADOW" logs/mtf_bot.log | python3 -c "
#     import sys, json, statistics
#     rows = [json.loads(l.split('VOLSHADOW ')[-1]) for l in sys.stdin if 'VOLSHADOW' in l]
#     ratios = [r['vol_ratio'] for r in rows if r.get('vol_ratio')]
#     if ratios:
#         print(f'n={len(ratios)} median={statistics.median(ratios):.2f} '
#               f'pct_pass={sum(r>=1.5 for r in ratios)/len(ratios):.1%}')
#   "
# LdP CPCV: 60-session minimum before treating 1.5x as validated threshold.
# Future graded option: 1.2x=1pt, 1.8x=2pt — S25 board vote + 60-session shadow data required.
VOLUME_CONFIRMATION_ENABLED  = False   # False=shadow (RSI scores); True=live (volume scores)
VOLUME_THRESHOLD             = 1.5     # current vol >= 1.5x 20-day avg to pass (Bucket B)
VOLUME_MIN_VALID_BARS        = 15      # min non-NaN bars in iloc[-21:-1] required (B2 McKinney)
VOLUME_REQUIRE_TWO_BAR       = False   # Levitt C2: 2 consecutive above-threshold days (deferred)
# VOLUME_GRADED_ENABLED = False        # reserved S25 — do not implement until 60-session shadow

# ─── GEX (GAMMA EXPOSURE) — RE-ARMED LIVE (2026-07-26, board+Gro+GAI, Rafael-approved) ──
# STATUS: LIVE — full strength. NEGATIVE regime -> Kelly x1.30 (momentum) + Layer-8 +1
# MIN_SCORE; POSITIVE -> x1.15 (mean-reversion). Re-armed from the 2026-07-19 SHADOW
# demotion after a board+Gro+GAI review (2 rounds, 5 voices) established: (a) the consumed
# signal is the scale-INVARIANT regime LABEL, not raw_gex_m or the tautological flip; (b)
# re-arm stays INSIDE the risk envelope (the mult is UPSTREAM of the 4.5%/trade Kelly clamp;
# 7% kill switch intact) — aggressiveness within caps, NOT envelope-widening; (c) the one
# uncaught label-corruption path (a plausible-but-wrong SPY spot) is closed by the
# SPOT-CONSISTENCY GUARD below (data/gex.py). Design: logs/gex_rearm_2026-07-26.md.
# The 2026-07-19 shadow rationale (flip tautology, unmeasurable accuracy, book-wide blast
# radius) is retained below as history — label-only consumption + the guard address it.
#
# WHY (BGG aligned 2026-07-19 — Board 4/4 + Gro APPROVE + GAI APPROVE; full design
# in logs/gex_0dte_evaluator_design_2026-07-19.md):
#   1. The flip level is currently TAUTOLOGICAL, not a measurement. _compute_gex
#      (data/gex.py:384-397) sweeps STRIKES at a FIXED spot and then argmin-selects
#      the crossing NEAREST SPOT. A real gamma flip is the hypothetical spot S* at
#      which net dealer gamma, REPRICED at S*, crosses zero — gamma must be
#      recomputed at each candidate spot, and it never is. Under Black-Scholes,
#      gamma peaks at K≈spot, so the crossing lands near spot BY CONSTRUCTION.
#      Evidence: post-fix SPY flip printed 755.0/755.0/755.0 against spot 754.68.
#      (The earlier flip=694 was a separate, already-CLOSED pre-fix artifact —
#      commit aad518a landed 2026-07-15 10:02 PT, the 694 record is 06:37 PT.)
#   2. Accuracy is UNMEASURED and cannot be measured soon. Effective sample is
#      ~1.2 observations/day (open interest is T+1-constant intraday, so the 26
#      snapshots are not 26 observations; SPY and QQQ are highly correlated), and
#      label serial correlation rho~0.7 gives a variance inflation factor of 5.67
#      => ~960 trading days for a defensible verdict on the current design.
#   3. The blast radius is LEVERAGE, not one trade. get_gex_regime feeds kelly.py's
#      edge multiplier, so a mis-specified GEX edge is a sizing error applied
#      MULTIPLICATIVELY ACROSS THE ENTIRE BOOK — not just to GEX-motivated entries.
#      Errors in bet sizing are not symmetric with errors in signal generation.
#
# WHAT STILL RUNS (deliberately — the evidence clock must keep accruing):
#   - refresh_gex() is called from live_data_writer.py:97 and is NOT gated on this
#     flag, so logs/gex_snapshot.json + logs/gex_history.jsonl keep being written.
#   - run_cycle.py:1571-1584 reads and logs the Layer-8 shadow record BEFORE the
#     GEX_ENABLED check at 1585, so the label timeline keeps accumulating.
#   - scripts/gex_daily_audit.py (cron 4:30 PM ET) keeps producing the daily audit.
#
# RE-ARMING IS A HUMAN DECISION, NOT AN AUTOMATIC ONE. Do not flip this back on a
# favorable-looking early sample: the board's guard is that any re-arm requires
# (a) _compute_gex re-specified as a proper root-find in S*, (b) the flip-on-spot
# regression showing the flip carries information independent of spot (slope~1 and
# R^2~1 proves it does not), and (c) a fresh board vote. Changing any threshold in
# this block resets the evidence clock to zero.
GEX_ENABLED             = True    # LIVE (re-armed 2026-07-26). Arms BOTH consumers: kelly.py edge
                                  # multiplier (NEGATIVE->x1.30 / POSITIVE->x1.15) AND run_cycle.py
                                  # Layer-8 MIN_SCORE +1 on NEGATIVE. Both fail-NEUTRAL on
                                  # NEAR-FLIP/STALE/UNKNOWN and on a missing attr (getattr(...,False)).
GEX_STALE_MINUTES       = 30      # base stale window: 30 min = 2 missed 15-min refreshes -> STALE

# ── Counter-trend bounce / falling-knife gate (2026-08-02, Rafael + BGG) ──────────────────────
# LIVE (Rafael chose staged-live + daily decision-impact audits over shadow). Blocks SHORTING a
# structurally-bearish name that is UP over the last month (bouncing — the SMCI re-short-the-rip
# failure) AND the mirror LONG (structurally-bullish, DOWN over the month = falling knife).
# Parameter-free (reuses MOMENTUM_SHORT_LOOKBACK); fail-open. INSTANT KILL = flip to False +
# restart (no deploy needed). Grep "COUNTER-TREND GATE" for the daily block audit.
# See execution/counter_trend.py.
COUNTER_TREND_GATE_ENABLED = True

# ─── EXTERNAL / MANUAL CLOSE RE-ENTRY COOLDOWN (2026-08-03, Rafael) ───────────
# When a position is closed OUTSIDE the bot (a manual sell in Alpaca, or any external/broker
# close — recorded by exit_logic as reason="external_close"), arm the SAME session re-entry
# cooldown a stop-out arms: do NOT re-enter that (symbol, direction) for the rest of the PT day.
# Closes the gap where the bot re-bought a name minutes after Rafael manually took profit (AMZN
# 2026-08-03). Same scope/duration/fail-open as the stop-out cooldown. Kill switch = flip to False.
EXTERNAL_CLOSE_REENTRY_COOLDOWN_ENABLED = True

# ─── MEAN-REVERSION / REGIME LAYER (item 2 — Rafael + BGG 2026-08-02) ─────────
# A SEPARATE entry path that LONGs a crashed name's CONFIRMED bounce and SHORTs an
# overextended name's CONFIRMED rollover — the complement to the counter-trend gate
# (which BLOCKS the bad trend-trade; this GENERATES the good reversal-trade). Detector
# + score live in execution/mr_regime.py (pure, unwired until diff 3). Front-loaded sim
# validated: LONG +1.78% fwd10 / 52% win; SHORT +0.85% / 57% (short REQUIRES a
# mean-reverting regime). All thresholds below are sim-validated FIRST-GUESSES; a
# data-derivation pass is roadmapped (per the no-static-regimes rule).
MR_ENABLED            = False   # master kill flag — DIFF-3 wiring is inert until this is True
MR_LONG_ONLY          = True    # ROLLOUT: long-first (short leg has the fatter squeeze/gap tail);
                                # flip to False to enable MR shorts (own Kelly key, intraday-only)
                                # only AFTER >=30 MR-long trades measure the long edge (board)
MR_SIZE_MULT          = 0.5     # reduced staged sizing: 0.5x the resolved per-trade fraction. Costs
                                # ZERO measurement fidelity (edge = size-invariant R-multiples)
MR_MIN_SCORE          = 1       # min mean_reversion_confluence score (0-3) to fire (eligibility
                                # already requires the confirmed trigger; this is a headroom knob)
MR_AGG_RISK_CAP_PCT   = 0.015   # MANDATORY correlation sub-cap: total OPEN MR per-trade risk of equity.
                                # PROFILE-AWARE (masked-loss seat 2026-08-03): paper=0.035 (= half the 7%
                                # kill, < the 4.5% clamp), live=0.015 (< the 3% live daily kill, ~1x the
                                # 2% live clamp). This MODULE default is the CONSERVATIVE fallback (0.015)
                                # for any un-profiled run — never above a profile's daily kill. In a broad
                                # selloff many crashed names bounce together -> correlated MR-long basket;
                                # the gross cap is correlation-BLIND. This is the account-ender guard.
MR_SUPPRESS_LONGS_IN_SPY_DOWNTREND = True  # market-regime gate: don't catch the index knife
# Detector thresholds — made explicit here for auditability/tuning (mr_regime.py reads them via
# getattr with these same defaults). RSI(14): oversold/overbought = the confirmed-reversal trigger
# band; extreme = the +1 score bonus. STRETCH = |price/SMA-1| for the +1 stretch point.
MR_SMA_PERIOD         = 150
MR_RSI_OVERSOLD       = 35.0
MR_RSI_OVERBOUGHT     = 65.0
MR_RSI_EXTREME_LOW    = 25.0
MR_RSI_EXTREME_HIGH   = 75.0
MR_REVERSAL_LOOKBACK  = 3       # bars (excl. today) the oversold/overbought must occur within
MR_STRETCH_PCT        = 0.10    # >=10% from the 150-SMA = the +1 "stretched" score point
MR_VR_WINDOW          = 60      # variance-ratio / Hurst trailing window (bars)
MR_VR_Q               = 5       # Lo-MacKinlay variance-ratio horizon q
MR_HURST_MAXLAG       = 20
MR_SPY_DOWNTREND_SMA  = 50      # SPY < this SMA (days) = intermediate downtrend → suppress MR LONGS when
                                # MR_SUPPRESS_LONGS_IN_SPY_DOWNTREND (masked-loss seat: the 5-min SPY gate is a
                                # micro-gate, not a slow-bleed regime brake, and MR bypasses counter_trend, so
                                # MR longs otherwise catch the index knife). 50d = standard intermediate trend
                                # filter (non-fitted); FAIL-CLOSED (suppress on downtrend OR unknown SPY state).

# Full-strength values (Rafael-locked 2026-07-26). NEGATIVE = high-vol / momentum-amplified
# -> size UP x1.30; POSITIVE = mean-reversion backdrop -> x1.15. NEUTRAL stays 1.00 (the
# fail-safe value on NEAR-FLIP/STALE/UNKNOWN). All applied UPSTREAM of KELLY_MAX_RISK_PCT.
GEX_EDGE_MULT_MOMENTUM  = 1.30    # NEGATIVE regime — full (was 1.00 shadow; 1.10 staged)
GEX_EDGE_MULT_MR        = 1.15    # POSITIVE regime — full (was 1.00 shadow; 1.05 staged)
GEX_EDGE_MULT_NEUTRAL   = 1.00    # edge multiplier when GEX=NEAR-FLIP or STALE/UNKNOWN (fail-safe)
GEX_MIN_SCORE_NEG_BUMP  = 1       # Layer-8: +1 MIN_SCORE on GEX=NEGATIVE (pickier on high-vol days)

# ─── GEX SPOT-CONSISTENCY GUARD (Diff A — 2026-07-26, board 5 seats + Gro + GAI) ──────────
# The consumed GEX signal is SPY's regime LABEL, which depends on the underlying SPOT price
# (BS gamma + the ATM/capture/flip moneyness windows). A plausible-but-wrong spot (a bad
# last-trade tick / a Monday gap) can FLIP the label and drive book-wide x1.30 on a false
# signal — the one path the STALE + strike-quality gates do NOT catch. Guard (data/gex.py):
#   1. Cross-check the latest-TRADE spot against an INDEPENDENT same-instant latest-QUOTE mid.
#   2. DYNAMIC band = max(SPREAD_MULT x live_spread, FLOOR_PCT x mid) — spread-scaled, not a
#      fixed %; a broken/crossed quote (spread > SPREAD_SANITY_PCT of mid) is not a usable
#      reference and is treated as suspect.
#   3. A SUSPECT spot NEVER computes a fresh label. The last CONFIRMED-GOOD label is carried
#      forward PRESERVING its confirmed-good timestamp, so the STALE clock keeps aging (never
#      reset) — neutralization is REACHED dynamically via STALE, never on a single read.
#   4. COLD-START (no prior good label) + suspect spot -> UNKNOWN -> neutral x1.0 (fail-safe).
# ROADMAP (Diff B, dynamic fast-follows): self-calibrate the band from rolling p95 of
# |trade-mid|/spread; add in-cycle re-poll + a cadence-derived persistence counter + a 1-min
# bar third reference (2-of-3). Starting constants below — recalibrate from gex_history.
GEX_SPOT_GUARD_ENABLED     = True
GEX_SPOT_BAND_SPREAD_MULT  = 5.0    # trade may sit within this many live bid-ask spreads of mid
GEX_SPOT_BAND_FLOOR_PCT    = 0.005  # ...but never a tighter tolerance than 0.5% of mid (liquid SPY
                                    # spread ~1c would else over-flag every normal timing skew)
GEX_SPOT_SPREAD_SANITY_PCT = 0.02   # a quote whose spread exceeds 2% of mid is broken/crossed and
                                    # cannot validate the trade -> suspect (fail-safe)
GEX_STALE_MINUTES_NEG      = 20     # direction-asymmetric demote: the risk-INCREASING NEGATIVE
                                    # (x1.30) leg ages to neutral faster than the 30-min base
                                    # (never hold "bigger" on stale data as long as "normal")

# ─── TSMOM VOL-SCALING (board vote 2026-04-22, 17-0) ─────────────────────────
# Vol-scaled sizing multiplier: target_vol / ewma_vol_60d, capped to [FLOOR, CAP].
# Active at paper stage for sizing only. Scoring activation gated on 90-day log + CPCV.
TSMOM_TARGET_VOL     = 0.25   # 25% annualized target vol/instrument (Taleb recommendation)
# 2026-07-03 STAGED ACTIVATION (Thorp board condition): the tsmom field tagging
# fix makes this multiplier live for the first time (it was a silent no-op —
# signal dicts never carried tsmom_vol_mult). Thorp's deployment rule: with the
# rolling 30-trade precision below 45% (currently ~16% WR), gate the range to
# [0.75, 1.25] for the first 20 TSMOM-scaled trades, then board-review reverting
# to the original [0.50, 1.50].
TSMOM_VOL_MULT_FLOOR = 0.75   # staged (original 0.50) — revert after 20-trade review
TSMOM_VOL_MULT_CAP   = 1.25   # staged (original 1.50) — revert after 20-trade review

# ─── PARTIAL EXITS ───────────────────────────────────────────────────────────
# Take partial profits at first target, let remainder run with trailing stop.
# This locks in profit while keeping exposure to extended moves.

PARTIAL_EXIT_ENABLED         = True
PARTIAL_EXIT_RATIO           = 0.5    # close this fraction at first target (50%)
PARTIAL_EXIT_ATR_MULT        = 1.0    # first target = 1x ATR from entry
TRAIL_STOP_ATR_MULT          = 0.5    # trailing stop = 0.5x ATR behind current price

# ─── TRADING BEHAVIOR FLAGS (S52) ────────────────────────────────────────────
# Defined here; set True ONLY after the enforcing logic lands in entry/exit_logic.py

SQQQ_TQQQ_MUTUAL_EXCLUDE    = False  # DS P1: prevent simultaneous SQQQ+TQQQ (inverse ETF blowup risk)
                                      # Set True after entry_logic.py mutual exclusion guard added
RUNNER_MODE_MOMENTUM_CHECK  = False  # Derman gate: EMA13>EMA30 required at +20% to activate trailing runner
                                      # Set True after exit_logic.py runner gate added
MIN_POSITION_VALUE_ADVISORY  = 200   # Advisory floor for non-fractionable symbols (whole-share only)
                                      # Warning logged when 1 share risk < this; does not block entry

# ─── OWNERSHIP NEVER-SELL FLOOR — broker chokepoint enforcement (increment 4a) ─
# Board 6-0 (Harris/Peterffy/Katsuyama + Kim/Majors/Taleb) + Gro + GAI, 2026-07-12.
# When True, broker.close_position / partial_close_position route every position-
# REDUCING order through execution.ownership_guard.check_never_sell_floor, so an
# intraday exit can NEVER sell a qhm/forever6 share: a non-protected symbol still gets
# a full close; a protected multi-tier symbol is bounded to the caller's tier via a
# partial; a floor-binding case is REJECTED rather than breach the floor.
# When False (DEFAULT), the chokepoint is DORMANT — close paths behave EXACTLY as
# before (raw full close, zero behavior change). Flip True ONLY after: (1) the OCI
# ledger is confirmed populated by run_ledger_sync with the inc3 code (protected_
# symbols.json present + NVDA/GOOGL show a qhm floor), AND (2) the QHM manager's own
# close passes tier="qhm" — else a genuinely-protected symbol reads floor=0 and the
# guard is a silent no-op. This is the one-line kill switch for the guard itself.
OWNERSHIP_GUARD_ENFORCE = False


# ─── CONFIG VALIDATION ────────────────────────────────────────────────────────

def validate_config():
    """
    Validate all critical config values at bot startup.
    Logs each issue. Raises SystemExit(1) if any critical check fails.
    Call this immediately after applying profile overrides in main().
    """
    import os as _os
    import logging as _logging
    log = _logging.getLogger("config")

    errors   = []
    warnings = []

    # (Bucket A/B allocation-sum check removed 2026-07-15 — the buckets were collapsed into a
    #  unified intraday/intraweek tier; aggregate exposure is governed by MAX_GROSS_EXPOSURE_RATIO,
    #  not a per-bucket % that must sum to ≤100%.)

    # R:R — stop must always be narrower than target
    if INTRADAY_STOP_ATR_MULT >= INTRADAY_TARGET_ATR_MULT:
        errors.append(
            f"Intraday R:R inverted: stop {INTRADAY_STOP_ATR_MULT}x "
            f">= target {INTRADAY_TARGET_ATR_MULT}x"
        )
    if SWING_STOP_ATR_MULT >= SWING_TARGET_ATR_MULT:
        errors.append(
            f"Swing R:R inverted: stop {SWING_STOP_ATR_MULT}x "
            f">= target {SWING_TARGET_ATR_MULT}x"
        )

    # Leveraged ETF stop bounds — extreme stops mean positions are too large
    lev_stop = LEVERAGED_STOP_MULTIPLIER * INTRADAY_STOP_ATR_MULT
    if lev_stop > 4.0:
        errors.append(
            f"Leveraged stop too wide: {LEVERAGED_STOP_MULTIPLIER} * "
            f"{INTRADAY_STOP_ATR_MULT} = {lev_stop:.2f}x (max 4.0)"
        )
    lev_3x_stop = LEVERAGED_3X_STOP_MULTIPLIER * INTRADAY_STOP_ATR_MULT
    if lev_3x_stop > 5.0:
        errors.append(
            f"3X leveraged stop too wide: {LEVERAGED_3X_STOP_MULTIPLIER} * "
            f"{INTRADAY_STOP_ATR_MULT} = {lev_3x_stop:.2f}x (max 5.0)"
        )

    # Score bounds — minimum must be achievable
    max_possible = sum(SCORE_WEIGHTS.values())
    if MIN_LONG_SCORE > max_possible:
        errors.append(
            f"MIN_LONG_SCORE ({MIN_LONG_SCORE}) > max possible score ({max_possible})"
        )
    if MIN_SHORT_SCORE > max_possible:
        errors.append(
            f"MIN_SHORT_SCORE ({MIN_SHORT_SCORE}) > max possible score ({max_possible})"
        )

    # AWP audit fix (2026-06-28): VOLUME_CONFIRMATION_ENABLED is a two-step
    # atomic toggle -- flipping it to True requires SCORE_WEIGHTS to swap
    # "rsi_in_range" for "volume_confirmed" in the same edit. The max_possible
    # check above can't catch an incomplete toggle since the dict's sum is
    # identical either way. OPEN QUESTION PROTOCOL ran: board (Beck/McKinney)
    # + Gro + GAI unanimously recommended this hardened cross-check. Rafael
    # approved. Found during the strategy/confluence.py board redo.
    if VOLUME_CONFIRMATION_ENABLED:
        if "volume_confirmed" not in SCORE_WEIGHTS:
            errors.append(
                "VOLUME_CONFIRMATION_ENABLED=True requires "
                '"volume_confirmed" in SCORE_WEIGHTS — incomplete toggle '
                '(missing the paired SCORE_WEIGHTS edit)'
            )
        if "rsi_in_range" in SCORE_WEIGHTS:
            errors.append(
                "VOLUME_CONFIRMATION_ENABLED=True requires removing "
                '"rsi_in_range" from SCORE_WEIGHTS — incomplete toggle '
                '(shadow RSI key still present)'
            )

    # Kill switch range
    if not (0 < MAX_DAILY_LOSS_PCT < 1.0):
        errors.append(
            f"MAX_DAILY_LOSS_PCT ({MAX_DAILY_LOSS_PCT}) must be between 0 and 1"
        )

    # Stop tightness warning
    if INTRADAY_STOP_ATR_MULT < 0.5:
        warnings.append(
            f"INTRADAY_STOP_ATR_MULT ({INTRADAY_STOP_ATR_MULT}) < 0.5 — "
            f"stop may be too tight for large-cap noise"
        )

    # API keys present
    if not _os.getenv("ALPACA_API_KEY"):
        errors.append("ALPACA_API_KEY not set in environment (.env not loaded?)")
    if not _os.getenv("ALPACA_SECRET_KEY"):
        errors.append("ALPACA_SECRET_KEY not set in environment (.env not loaded?)")

    # Log results
    for w in warnings:
        log.warning(f"[CONFIG] {w}")

    if errors:
        for e in errors:
            log.critical(f"[CONFIG ERROR] {e}")
        log.critical(
            f"Config validation FAILED ({len(errors)} error(s)) — bot will not start."
        )
        raise SystemExit(1)

    log.info(
        f"Config validation passed — profile={ACTIVE_PROFILE}, "
        f"{len(warnings)} warning(s), 0 errors."
    )

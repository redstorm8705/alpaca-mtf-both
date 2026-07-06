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
    "CRWD", "CRM",  "PANW", "UBER", "AMZN",
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

# ─── BUCKET ALLOCATION ───────────────────────────────────────────────────────
# Bucket A: leveraged ETF swing holds — 5% of portfolio, min 1-day hold
# Bucket B: swing trades — 95% of portfolio, conviction-sized

BUCKET_A_TICKERS         = {"TSLL", "NVDL", "TQQQ", "SQQQ"}
BUCKET_A_ALLOCATION_PCT  = 0.15   # 15% of portfolio (raised from 5% — board 5-0 + Gro + GAI 2026-06-30)
BUCKET_A_MIN_HOLD_DAYS   = 1      # minimum 1 full trading day before exit

BUCKET_B_ALLOCATION_PCT        = 0.85   # 85% of portfolio (adjusted from 95% to keep A+B=100%)
BUCKET_B_MAX_POSITIONS         = 999    # placeholder; actual cap is MAX_OPEN_POSITIONS=4
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

BARS_TO_FETCH = {
    TF_15M:   150, TF_30M:  400, TF_1H:   100,  # S43: 500→150, 300→100 — RAM leak fix (DS+GAI Q7; EMA30 needs ~107 bars, MACD26 needs ~93)
    TF_4H:    200, TF_12H:  150, TF_DAILY: 365, TF_WEEKLY: 104,
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

# ─── TRADE MODES ─────────────────────────────────────────────────────────────

class TradeMode:
    INTRADAY = "intraday"
    SWING    = "swing"

SWING_SCORE_BONUS_REQUIRED          = 2
INTRADAY_CLOSE_MINUTES_BEFORE_EOD   = 15

# ─── RISK MANAGEMENT ────────────────────────────────────────────────────────────────────────────

# Per-trade risk: 2% of portfolio per trade — professional standard, do not raise
MAX_PORTFOLIO_RISK_PCT = 0.02

# Max concurrent positions: lowered 10 → 4
# On a $1,000 account, >4 positions means most size to 1 share anyway
MAX_OPEN_POSITIONS     = 4

# Daily kill switch: tightened 5% → 3% for first 30 days of live running
# Once bot behavior is confirmed, raise back to 5%
MAX_DAILY_LOSS_PCT     = 0.03

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
        "PARTIAL_EXIT_ENABLED":    True,
        "PARTIAL_EXIT_RATIO":      0.5,    # close 50% at first target
        "PARTIAL_EXIT_ATR_MULT":   1.0,    # take first half at 1x ATR
    },
    "paper": {
        # Bucket-aware paper profile
        "MAX_PORTFOLIO_RISK_PCT":  0.04,   # fallback only — bucket sizing overrides this
        "MAX_OPEN_POSITIONS":      7,      # raised 4→7 — board 5-0 + Gro + GAI 2026-06-30 (max capital deployment)
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
KELLY_MAX_RISK_PCT           = 0.06   # hard cap: Kelly can never risk > 6% (paper); board vote S23 2026-05-16
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

# ─── GEX (GAMMA EXPOSURE) ─────────────────────────────────────────────────────
# Board unanimous S50b originally gated activation on a 30-session shadow review.
# Rafael mandate 2026-07-03 (sole mandate authority): activate now — the shadow
# clock never ran (data pipeline was dead until the 2026-07-03 repair), and the
# operator wants live impact data instead. Compensating control: mandatory DAILY
# audit via scripts/gex_daily_audit.py (cron 4:30 PM ET) — labels timeline,
# Layer-8 floor raises, Kelly edge-mult applications, and affected entries.
# Effects when True: run_cycle Layer 8 raises MIN_SCORE +1 while SPY GEX=NEGATIVE;
# kelly.py multiplies kelly_risk by the staged values below for signal types past
# 30-trade warmup (both are, as of 2026-07-03), capped by KELLY_MAX_RISK_PCT;
# no multiplier on Fridays (0DTE carve-out).
GEX_ENABLED             = True    # ACTIVE (Rafael 2026-07-03) — daily audit mandated
GEX_STALE_MINUTES       = 30      # 45→30 at activation (Kyle + GAI condition: 45 min = 3 stale
                                  # bars on the 15-min cadence before STALE triggers; 30 = 2)
# STAGED MULTIPLIERS (Thorp + GAI activation condition, 2026-07-03): full values
# 1.30 / 1.15 were board-approved S50b, but the label is freshly repaired and the
# account is in a sub-20%-WR stretch — the multiplier only ever INCREASES risk.
# Staged to 1.10 / 1.05 until rolling 20-trade WR >= 35% or board review, then
# revert to 1.30 / 1.15. All decision paths and daily-audit data flow either way.
GEX_EDGE_MULT_MOMENTUM  = 1.10    # staged (S50b full value 1.30) — see note above
GEX_EDGE_MULT_MR        = 1.05    # staged (S50b full value 1.15) — see note above
GEX_EDGE_MULT_NEUTRAL   = 1.00    # edge multiplier when GEX=NEAR-FLIP or STALE/UNKNOWN
GEX_MIN_SCORE_NEG_BUMP  = 1       # MIN_SCORE +1 when GEX=NEGATIVE (requires stronger signal)

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

    # Capital allocation — buckets cannot exceed 100%
    total_alloc = BUCKET_A_ALLOCATION_PCT + BUCKET_B_ALLOCATION_PCT
    if total_alloc > 1.0:
        errors.append(
            f"BUCKET_A_ALLOCATION_PCT ({BUCKET_A_ALLOCATION_PCT:.0%}) + "
            f"BUCKET_B_ALLOCATION_PCT ({BUCKET_B_ALLOCATION_PCT:.0%}) = "
            f"{total_alloc:.0%} > 100%"
        )

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

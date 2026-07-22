# ruff: noqa: E501
"""
strategy/horizon_state.py
Three-horizon directional state for the confluence-scanner tiering view.

DISPLAY-ONLY. Nothing in this module gates entries, sizes positions, or touches
capital. Architecture Invariant #1 stands: SPY 5-min bar-over-bar remains the sole
entry gate. Rafael's Q3 decision (logs/scanner_tiering_design_2026-07-20.md) is that
v1 AUGMENTS and does not replace the 12pt gate; wiring any of this into the bot is a
separate board vote.

WHAT THIS COMPUTES
------------------
For one symbol, three independent states — INTRADAY / WEEKLY / MONTHLY — each of
{BULL, BEAR, NEUTRAL} plus a signed strength in [-1, +1]:

    strength_h = clip( (price - anchor_h) / ATR14_abs / SAT_h , -1, +1 )
    NEUTRAL    iff |strength_h| < NEUTRAL_BAND (0.25, uniform across horizons)

SAT (saturation) = {intraday: 2, weekly: 4, monthly: 8} — the locked Q2 answer. The
1:2:4 progression is sqrt-time volatility scaling: a month's noise dwarfs a session's,
so the monthly state must clear 4x the raw ATR distance the intraday state does to
register the same strength. That is what stops the positioning tier from whipsawing
on a single loud day.

ATR is the 14-day DAILY ATR (config.ATR_PERIOD) for ALL THREE horizons — deliberately
one common yardstick, so the three strengths are directly comparable and the SAT ladder
is the only thing that differentiates them. (It is also the only ATR the live code
computes; see data/premarket.calculate_atr.)

COMPLETED BARS ONLY
-------------------
Every function here drops the in-progress bar before computing anything. This is risk
#3 in the design doc: a partial bar makes the state flicker intra-period, which makes
the triple-confluence badge blink, which produces churn. `_drop_partial` is applied to
every frame on entry and there is no path around it.

WHY THIS DOES NOT CALL signal_generator._get_weekly_bias
--------------------------------------------------------
The design doc says "reuse `_get_weekly_bias`" for the weekly state AND says
"completed bars only everywhere". Those conflict in the current source:
`_get_weekly_bias` (strategy/signal_generator.py:67-144) takes `closes.values[-1]` and
calls it "latest completed weekly bar", but `fetch_bars` returns the in-progress week
as the last row — so it is actually reading a PARTIAL bar. That is blindspot #5 in the
design doc, confirmed against source.

This module therefore reimplements the same RULE (2 consecutive weekly closes vs the
10-week SMA, with the split-hold anti-whipsaw) on completed bars only, instead of
importing the function. Two reasons:
  1. Importing it would inherit the partial-bar read, violating the completed-bars-only
     invariant this whole module exists to uphold.
  2. `_get_weekly_bias` also carries entry-gate concerns irrelevant to a display tier —
     it bypasses LEVERAGED_TICKERS entirely (returns None) and inverts SQQQ against QQQ.
     A horizon VIEW wants SQQQ's own actual weekly trend, not a gate decision about it.
Critically, this leaves `_get_weekly_bias` UNTOUCHED, so the bot's live entry gate is
not modified by this file in any way.

>>> OPEN FOR BGG ON THE DIFF: the anchor choice. Q2 locked the formula as
>>> (price - SMA_h)/ATR/SAT but named an explicit SMA only for weekly (10wk) and
>>> monthly (10mo). For intraday this module uses the 30-EMA on 15-min bars — the same
>>> slow EMA the intraday direction gate already uses — so the strength measures
>>> distance from the horizon's own trend anchor in all three cases. Flagging rather
>>> than silently choosing: if the board prefers VWAP as the intraday anchor, it is a
>>> one-line change to `_INTRADAY_ANCHOR`.
"""

from __future__ import annotations

import logging

import pandas as pd

import config
from data.premarket import calculate_atr
from indicators.macd import add_both_macds, dual_macd_agreement
from indicators.moving_averages import add_all_mas
from indicators.vwap import add_vwap

logger = logging.getLogger(__name__)

BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"

# Locked Q2 (2026-07-20): saturation denominators, sqrt-time scaled 1:2:4.
SAT_INTRADAY = 2.0
SAT_WEEKLY = 4.0
SAT_MONTHLY = 8.0

# Locked Q2: uniform neutral dead-band on the normalized strength.
NEUTRAL_BAND = 0.25

# Locked Q4: triple-confluence needs directional agreement AND real magnitude.
# 0.50 is the median of the [0.25, 1.0] non-neutral band, so the loudest badge on the
# page means a genuinely strong aligned trend rather than three 0.25 whispers.
TRIPLE_MIN_MEAN_STRENGTH = 0.50

# Minimum completed bars per horizon.
_MIN_BARS_INTRADAY = 40   # 30-EMA needs runway; MACD-26 needs ~30
_MIN_BARS_1H_CONFIRM = 30  # MACD-26 on the 1h confirm leg is degenerate below ~30 bars
_MIN_BARS_WEEKLY = 12     # 10-wk SMA + the 2-bar confirmation
_MIN_BARS_MONTHLY = 13    # 10-mo SMA + 12-1 momentum (needs 13 completed months)
_MIN_BARS_DAILY_ATR = config.ATR_PERIOD + 2   # ATR-14 + the dropped partial

# Intraday trend anchor — see the BGG flag in the module docstring.
_INTRADAY_ANCHOR = f"ema_{config.EMA_SLOW}"


def _drop_partial(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Drop the in-progress (most recent, still-forming) bar.

    Every frame entering this module goes through here. Alpaca returns the current,
    incomplete period as the last row for every timeframe — including monthly, where
    the live 2026-07 bar is present mid-month. Returns None if the frame is unusable
    or would be emptied, so callers fail to NEUTRAL rather than reading a partial bar.
    """
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
        return None
    return df.iloc[:-1]


def _atr_abs(daily_df: pd.DataFrame) -> float | None:
    """Absolute 14-day ATR in price units, from COMPLETED daily bars only.

    data/premarket.calculate_atr returns ATR as a PERCENT of the last close, so this
    converts back to price units the same way the live sizing path does
    (execution/entry_logic.py:853 — `(calculate_atr(df, ATR_PERIOD) / 100) * price`).
    Reusing the canonical implementation rather than adding a fifth ATR to the repo.
    """
    closed = _drop_partial(daily_df)
    if closed is None or len(closed) < _MIN_BARS_DAILY_ATR:
        return None
    try:
        atr_pct = calculate_atr(closed, config.ATR_PERIOD)
        last_close = float(closed["close"].iloc[-1])
        if not atr_pct or atr_pct <= 0 or last_close <= 0:
            return None
        return (float(atr_pct) / 100.0) * last_close
    except Exception as e:
        logger.debug("_atr_abs failed (non-critical): %s", e)
        return None


def _strength(price: float, anchor: float, atr_abs: float, sat: float) -> float:
    """Signed, ATR-normalized, saturation-scaled distance from the trend anchor."""
    if atr_abs is None or atr_abs <= 0 or sat <= 0:
        return 0.0
    raw = (price - anchor) / atr_abs / sat
    return max(-1.0, min(1.0, raw))


def _state_from(strength: float, direction_ok_bull: bool, direction_ok_bear: bool) -> str:
    """Combine the magnitude dead-band with the horizon's own direction gates.

    A horizon is only BULL/BEAR when BOTH the normalized magnitude clears the dead-band
    AND that horizon's confirming condition agrees. Direction comes from the GATE flags,
    NOT from the sign of `strength`. That distinction matters for the weekly split-hold:
    when the anti-whipsaw rule HOLDS a prior BULL while the latest completed close has
    dipped below the SMA (a negative strength), the held bias must still win — gating on
    sign first would silently return NEUTRAL and defeat the hold. Callers guarantee the
    two gate flags are mutually exclusive, so a true flag pair never both fires.
    """
    if abs(strength) < NEUTRAL_BAND:
        return NEUTRAL
    if direction_ok_bull:
        return BULL
    if direction_ok_bear:
        return BEAR
    return NEUTRAL


def _result(state: str, strength: float, reason: str, **extra) -> dict:
    """Self-describing result. Never a bare string or bare number."""
    out = {
        "state": state,
        "strength": round(float(strength), 3),
        "reason": reason,
    }
    out.update(extra)
    return out


def _unavailable(reason: str) -> dict:
    return _result(NEUTRAL, 0.0, reason, available=False)


def intraday_state(df_15m: pd.DataFrame, df_1h: pd.DataFrame,
                   atr_abs: float | None) -> dict:
    """INTRADAY horizon: 15-min primary, 1-hour confirmation.

    BULL requires all three: 13-EMA > 30-EMA on 15m, dual-timeframe MACD agreement
    >= 2 (the 15m and 1h MACD pair both leaning the same way), and price above VWAP.
    BEAR is the exact mirror. Anything else is NEUTRAL.
    """
    d15 = _drop_partial(df_15m)
    d1h = _drop_partial(df_1h)
    if d15 is None or len(d15) < _MIN_BARS_INTRADAY:
        return _unavailable("insufficient completed 15m bars")
    if d1h is None or len(d1h) < _MIN_BARS_1H_CONFIRM:
        return _unavailable("insufficient completed 1h bars for MACD confirm")
    if atr_abs is None or atr_abs <= 0:
        return _unavailable("ATR unavailable")

    try:
        d15 = add_vwap(add_both_macds(add_all_mas(d15)))
        d1h = add_both_macds(d1h)

        row = d15.iloc[-1]
        price = float(row["close"])
        anchor = row.get(_INTRADAY_ANCHOR)
        vwap = row.get("vwap")
        if anchor is None or pd.isna(anchor):
            return _unavailable("intraday anchor unavailable")

        ema_fast = float(row[f"ema_{config.EMA_FAST}"])
        ema_slow = float(row[f"ema_{config.EMA_SLOW}"])

        agree_long = (dual_macd_agreement(d15, "long")["agreement_score"]
                      + dual_macd_agreement(d1h, "long")["agreement_score"])
        agree_short = (dual_macd_agreement(d15, "short")["agreement_score"]
                       + dual_macd_agreement(d1h, "short")["agreement_score"])

        above_vwap = bool(vwap is not None and not pd.isna(vwap) and price > float(vwap))
        below_vwap = bool(vwap is not None and not pd.isna(vwap) and price < float(vwap))

        ok_bull = (ema_fast > ema_slow) and agree_long >= 2 and above_vwap
        ok_bear = (ema_fast < ema_slow) and agree_short >= 2 and below_vwap

        s = _strength(price, float(anchor), atr_abs, SAT_INTRADAY)
        return _result(
            _state_from(s, ok_bull, ok_bear), s,
            "15m EMA13/30 + dual-TF MACD>=2 + VWAP, completed bars",
            available=True, macd_agree_long=agree_long, macd_agree_short=agree_short,
            above_vwap=above_vwap, anchor=round(float(anchor), 4),
        )
    except Exception as e:
        logger.debug("intraday_state failed (non-critical): %s", e)
        return _unavailable(f"error: {type(e).__name__}")


def weekly_state(df_weekly: pd.DataFrame, atr_abs: float | None,
                 prior_state: str | None = None) -> dict:
    """WEEKLY horizon: the `_get_weekly_bias` RULE, on completed bars only.

    Two consecutive completed weekly closes on the same side of the 10-week SMA
    confirm the bias. If the two disagree (a "split"), hold `prior_state` rather than
    flipping — that is the anti-whipsaw behavior of the original rule. With no prior
    state (cold start) a split falls back to the most recent completed bar.

    See the module docstring for why this reimplements rather than imports.
    """
    wk = _drop_partial(df_weekly)
    if wk is None or len(wk) < _MIN_BARS_WEEKLY:
        return _unavailable("insufficient completed weekly bars")
    if atr_abs is None or atr_abs <= 0:
        return _unavailable("ATR unavailable")

    try:
        closes = wk["close"].dropna()
        if len(closes) < _MIN_BARS_WEEKLY:
            return _unavailable("insufficient weekly closes after dropna")

        sma10 = float(closes.tail(10).mean())
        price_curr = float(closes.values[-1])
        price_prev = float(closes.values[-2])

        bias_curr = BULL if price_curr > sma10 else BEAR
        bias_prev = BULL if price_prev > sma10 else BEAR

        if bias_curr == bias_prev:
            confirmed, split = bias_curr, False
        else:
            confirmed = prior_state if prior_state in (BULL, BEAR) else bias_curr
            split = True

        s = _strength(price_curr, sma10, atr_abs, SAT_WEEKLY)
        # On a split the magnitude may point the other way from the held bias; the
        # held bias wins on direction, so gate on it rather than on sign alone.
        return _result(
            _state_from(s, confirmed == BULL, confirmed == BEAR), s,
            "2 completed weekly closes vs 10wk SMA, split-hold",
            available=True, sma10=round(sma10, 4), split=split, confirmed_bias=confirmed,
        )
    except Exception as e:
        logger.debug("weekly_state failed (non-critical): %s", e)
        return _unavailable(f"error: {type(e).__name__}")


def monthly_state(df_monthly: pd.DataFrame, atr_abs: float | None) -> dict:
    """MONTHLY horizon (net-new): Faber 10-month SMA + 12-1 monthly momentum.

    BULL requires the last completed monthly close above the 10-month SMA AND positive
    12-1 momentum (the 12-month return excluding the most recent month, the standard
    Jegadeesh-Titman skip that avoids short-term reversal). BEAR is the mirror.
    Requires 13 completed months: 12 for the momentum lookback plus the skip.
    """
    mo = _drop_partial(df_monthly)
    if mo is None or len(mo) < _MIN_BARS_MONTHLY:
        return _unavailable("insufficient completed monthly bars")
    if atr_abs is None or atr_abs <= 0:
        return _unavailable("ATR unavailable")

    try:
        closes = mo["close"].dropna()
        if len(closes) < _MIN_BARS_MONTHLY:
            return _unavailable("insufficient monthly closes after dropna")

        sma10 = float(closes.tail(10).mean())
        price = float(closes.values[-1])

        # 12-1 momentum on completed months: return from t-13 to t-2, i.e. skip the most
        # recent completed month (values[-1]) to avoid short-term reversal — an 11-month
        # holding window, the standard Jegadeesh-Titman 12-1 formulation.
        p_start = float(closes.values[-13])
        p_end = float(closes.values[-2])
        mom_12_1 = ((p_end - p_start) / p_start) if p_start > 0 else None
        if mom_12_1 is None:
            return _unavailable("12-1 momentum undefined (non-positive start price)")

        ok_bull = price > sma10 and mom_12_1 > 0
        ok_bear = price < sma10 and mom_12_1 < 0

        s = _strength(price, sma10, atr_abs, SAT_MONTHLY)
        return _result(
            _state_from(s, ok_bull, ok_bear), s,
            "completed monthly close vs 10mo SMA + 12-1 momentum",
            available=True, sma10=round(sma10, 4),
            momentum_12_1=round(mom_12_1 * 100, 2),
        )
    except Exception as e:
        logger.debug("monthly_state failed (non-critical): %s", e)
        return _unavailable(f"error: {type(e).__name__}")


def compute_horizon_states(daily_df: pd.DataFrame, df_15m: pd.DataFrame,
                           df_1h: pd.DataFrame, df_weekly: pd.DataFrame,
                           df_monthly: pd.DataFrame,
                           prior_weekly_state: str | None = None) -> dict:
    """All three horizon states for one symbol, plus the triple-confluence flag.

    PURE: takes DataFrames, does no fetching and no I/O. The caller owns data
    acquisition, which keeps this unit-testable with synthetic frames.

    The triple flag is Q4-locked: all three non-NEUTRAL, all the same sign, AND mean
    |strength| >= 0.50.
    """
    atr_abs = _atr_abs(daily_df)

    intraday = intraday_state(df_15m, df_1h, atr_abs)
    weekly = weekly_state(df_weekly, atr_abs, prior_weekly_state)
    monthly = monthly_state(df_monthly, atr_abs)

    states = [intraday, weekly, monthly]
    non_neutral = [h for h in states if h["state"] != NEUTRAL]
    triple = False
    triple_dir = None
    if len(non_neutral) == 3:
        dirs = {h["state"] for h in non_neutral}
        if len(dirs) == 1:
            mean_strength = sum(abs(h["strength"]) for h in non_neutral) / 3.0
            if mean_strength >= TRIPLE_MIN_MEAN_STRENGTH:
                triple = True
                triple_dir = non_neutral[0]["state"]

    # Horizon DISAGREEMENT (design risk #2): longest-timeframe-wins can file a name
    # under MONTHLY_BULL while it is breaking down intraday. Surfacing this lets the
    # UI flag it in the row so the intraday book never loses sight of a reversal.
    directional = [h["state"] for h in states if h["state"] != NEUTRAL]
    disagreement = len(set(directional)) > 1

    return {
        "intraday": intraday,
        "weekly": weekly,
        "monthly": monthly,
        "atr_abs": round(atr_abs, 4) if atr_abs else None,
        "triple": triple,
        "triple_direction": triple_dir,
        "disagreement": disagreement,
    }


def assign_tier(states: dict) -> dict:
    """LONGEST-TIMEFRAME-WINS tier assignment (Rafael's locked Q1).

    The dominant timeframe owns the tape (Weinstein/Brandt), so the longest
    non-NEUTRAL horizon claims the symbol. Strongest-wins was rejected: it biases
    toward intraday noise, which turns a positioning tool into a churn machine.
    Exactly one tier per symbol — the mutual-exclusivity requirement.
    """
    for horizon, prefix in (("monthly", "MONTHLY"), ("weekly", "WEEKLY"),
                            ("intraday", "INTRADAY")):
        st = states.get(horizon, {}).get("state", NEUTRAL)
        if st != NEUTRAL:
            return {
                "tier": f"{prefix}_{st}",
                "horizon": horizon,
                "direction": st,
                "triple": states.get("triple", False),
                "disagreement": states.get("disagreement", False),
            }
    return {
        "tier": "UNTIERED",
        "horizon": None,
        "direction": None,
        "triple": False,
        "disagreement": states.get("disagreement", False),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Self-test (synthetic frames, no network). Run: python3 -m strategy.horizon_state
# Proves the pure math: ATR conversion, dead-band, weekly/monthly rules, tiering,
# triple/disagreement flags. Intraday is exercised as a smoke check (its MACD/EMA/VWAP
# gates need live-shaped microstructure that synthetic monotone data can't fake cleanly).
# ─────────────────────────────────────────────────────────────────────────────
def _synth_daily(close: float, tr: float, n: int = 20) -> pd.DataFrame:
    """Daily OHLC with a constant true range `tr` -> ATR == tr, atr_abs == tr."""
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"high": [close + tr / 2] * n, "low": [close - tr / 2] * n,
         "close": [close] * n, "volume": [1000] * n}, index=idx,
    )


def _synth_closes(values: list) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=len(values), freq="D")
    return pd.DataFrame({"close": values}, index=idx)


def _selftest() -> None:
    # ATR: constant TR=4 on a $100 name -> atr_abs ~= 4.0 (one bar dropped as partial).
    atr = _atr_abs(_synth_daily(100.0, 4.0))
    assert atr is not None and 3.9 <= atr <= 4.1, f"atr_abs={atr}"

    # _strength + dead-band. monthly SAT=8: (100-90)/4/8 = 0.3125 -> BULL (>0.25).
    s = _strength(100.0, 90.0, 4.0, SAT_MONTHLY)
    assert abs(s - 0.3125) < 1e-9, s
    assert _state_from(s, True, False) == BULL
    # inside band: (3)/4/8 = 0.09375 -> NEUTRAL regardless of direction gate.
    assert _state_from(_strength(100.0, 97.0, 4.0, SAT_MONTHLY), True, False) == NEUTRAL
    # magnitude clears band but direction gate fails -> NEUTRAL.
    assert _state_from(s, False, False) == NEUTRAL
    # clip saturates at +/-1.
    assert _strength(1e6, 0.0, 4.0, SAT_INTRADAY) == 1.0

    # WEEKLY rule: 13 completed rising closes above a rising SMA10 -> BULL, not split.
    wk = _synth_closes([90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103])
    wr = weekly_state(wk, 4.0)
    assert wr["state"] == BULL and wr["split"] is False, wr
    # WEEKLY bear mirror.
    wb = weekly_state(_synth_closes([120, 118, 116, 114, 112, 110, 108, 106, 104, 102, 100, 98, 96, 94]), 4.0)
    assert wb["state"] == BEAR, wb
    # split-hold: last COMPLETED bar dips below SMA10 while the prior was above -> hold
    # prior BULL. The trailing 999 is the in-progress bar _drop_partial strips, so 80
    # lands as the last completed close.
    split_closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 80, 999]
    ws = weekly_state(_synth_closes(split_closes), 4.0, prior_state=BULL)
    assert ws["split"] is True and ws["confirmed_bias"] == BULL, ws
    # Regression guard (cold-2nd 2026-07-21): the HELD bias must be the reported state,
    # even though the latest completed close is far below SMA10 (negative strength). The
    # earlier sign-first _state_from returned NEUTRAL here, silently defeating split-hold.
    assert ws["state"] == BULL, ws

    # MONTHLY rule: strong uptrend, close>SMA10 AND 12-1 momentum>0 -> BULL.
    mo = _synth_closes([50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 115])
    mr = monthly_state(mo, 4.0)
    assert mr["state"] == BULL and mr["momentum_12_1"] > 0, mr
    # conflict: year-long decline then a recent pop back above the (now-low) SMA10, while
    # the 12-mo momentum (t-13 -> t-1) is still negative -> gate needs BOTH, so NEUTRAL.
    # (last value is the in-progress bar _drop_partial strips.)
    mc = _synth_closes([200, 190, 180, 170, 160, 150, 140, 130, 120, 110, 130, 135, 140, 999])
    assert monthly_state(mc, 4.0)["state"] == NEUTRAL, monthly_state(mc, 4.0)

    # TIERING: longest-wins. monthly BULL beats intraday BEAR.
    states_lw = {
        "intraday": {"state": BEAR}, "weekly": {"state": NEUTRAL},
        "monthly": {"state": BULL}, "triple": False, "disagreement": True,
    }
    t = assign_tier(states_lw)
    assert t["tier"] == "MONTHLY_BULL" and t["horizon"] == "monthly", t
    # intraday-only.
    assert assign_tier({"intraday": {"state": BULL}, "weekly": {"state": NEUTRAL},
                        "monthly": {"state": NEUTRAL}})["tier"] == "INTRADAY_BULL"
    # all neutral -> UNTIERED.
    assert assign_tier({"intraday": {"state": NEUTRAL}, "weekly": {"state": NEUTRAL},
                        "monthly": {"state": NEUTRAL}})["tier"] == "UNTIERED"

    # compute_horizon_states end-to-end with synthetic frames (intraday unavailable ->
    # NEUTRAL). weekly+monthly BULL, same sign -> disagreement False, triple False
    # (intraday NEUTRAL breaks the all-three requirement).
    tiny_15m = _synth_closes([1, 2])  # too few bars -> intraday unavailable/NEUTRAL
    tiny_1h = _synth_closes([1, 2])
    cs = compute_horizon_states(_synth_daily(100.0, 4.0), tiny_15m, tiny_1h, wk, mo)
    assert cs["weekly"]["state"] == BULL and cs["monthly"]["state"] == BULL, cs
    assert cs["intraday"]["state"] == NEUTRAL, cs["intraday"]
    assert cs["triple"] is False and cs["disagreement"] is False, cs
    assert isinstance(cs["triple"], bool) and isinstance(cs["disagreement"], bool)

    # disagreement flag: weekly BULL + monthly BEAR -> True.
    cs2 = compute_horizon_states(_synth_daily(100.0, 4.0), tiny_15m, tiny_1h, wk,
                                 _synth_closes([115, 110, 105, 100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50]))
    assert cs2["monthly"]["state"] == BEAR and cs2["disagreement"] is True, cs2

    print("horizon_state self-test: ALL PASS")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _selftest()

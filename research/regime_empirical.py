# ruff: noqa: E501  — long design/docstring lines (matches the sibling regime modules)
"""research/regime_empirical.py — SHADOW empirical vol-regime classifier (regime object Phase 2, step 2).

SHADOW / LOG-ONLY / NON-BEHAVIORAL / NON-RISK-PATH. Nothing on the trading path imports this module. It
changes NO gating, sizing, entry, exit, or stop. It only COMPUTES what an adaptive (empirical-tercile)
vol-regime classifier WOULD say and LOGS it — with the board's one-sided safety clamp applied — alongside
the current STATIC classifier's label, so the two can be compared over time before any live wiring.

Board design: logs/design_records/regime_state_phase2_design_2026-09-14.md (unanimous: LdP+Simons, Thorp+Taleb, Gro).
Key design points honored here:
  - VOL SIGNAL ONLY. The mean-reversion signal (variance_ratio/Hurst) is NOT empiricized — its VR<1.0 /
    Hurst<0.5 theoretical anchors stay; percentile-izing them can invert the signal. This module does not
    touch MR at all.
  - Distribution from HISTORY, not a 1/day ledger. Trailing daily SPY realized-vol (T1 Alpaca bars via
    data.fetcher) — a single clean population, which also sidesteps the volatility_regime.py realized_vol
    field-mixing bug (VIX vs SPY-realized-vol under one field; logged, risk-path, fixed separately before
    any live flip).
  - ONE-SIDED CLAMP: the empirical path may only ever make sizing MORE conservative than the static path.
    clamped_size_mult = min(empirical_size_mult, static_size_mult); clamped_stop_mult = max(empirical, static).
    The 1.25x size-up requires BOTH empirical-LOW and absolute vol < 12 (would_size_up); empirical can never
    be the sole reason to lever up. This module only LOGS these — it wires nothing.
  - EFFECTIVE-N gate: daily realized vol (overlapping 10d windows) is heavily autocorrelated, so the raw
    sample count is haircut by the AR(1) variance-inflation factor VIF=(1+rho)/(1-rho); a row is flagged
    sufficient only when N_eff clears MIN_EFF_SAMPLES. Terciles are computed strictly-trailing (the current
    observation is excluded from the distribution it is classified against).
  - Whipsaw is logged (flipped_from_prev) but NOT enforced here; hysteresis/dwell is a live-wiring concern.

Usage: python3 research/regime_empirical.py   (snapshot -> appends one shadow row)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _ROOT / "logs"
# NOTE on sys.path: the standalone-CLI path needs the repo root on sys.path so the lazy imports below
# resolve; that setup lives in main() (NOT at module import), so importing this module has no side effect.
PT = ZoneInfo("America/Los_Angeles")
logger = logging.getLogger("regime_empirical")

_SHADOW_PATH = _LOGS / "regime_empirical_shadow.jsonl"

# History + estimator parameters (board design record).
_HISTORY_BARS = 520          # trailing SPY daily bars to fetch (~2yr) — distribution source
_RVOL_LOOKBACK = 10          # realized-vol lookback in returns (matches volatility_regime._fetch_spy_realized_vol)
_MIN_RAW_SAMPLES = 252       # hard floor of usable realized-vol observations (~1yr) before "sufficient"
_MIN_EFF_SAMPLES = 30        # effective-sample floor after the AR(1) VIF haircut (Gro/LdP)
_LOW_PCTILE = 33.3333        # empirical tercile boundaries
_HIGH_PCTILE = 66.6667


def _rvol_series(closes) -> "list | None":
    """Trailing rolling annualized realized-vol % series from a close series. None on failure. Never raises."""
    try:
        import numpy as np
        c = closes.astype(float)
        logret = np.log(c / c.shift(1))
        rv = logret.rolling(_RVOL_LOOKBACK).std() * np.sqrt(252) * 100.0
        vals = [float(x) for x in rv.dropna().tolist() if x == x and x not in (float("inf"), float("-inf"))]
        return vals or None
    except Exception as e:
        logger.warning("regime_empirical: rvol series failed (%s).", e)
        return None


def _effective_n(series) -> "tuple[int, float, float]":
    """(n_eff, lag1_autocorr, VIF) for an AR(1)-style haircut. Degrades to n_eff=len on any failure."""
    try:
        import numpy as np
        x = np.asarray(series, dtype=float)
        n = len(x)
        if n < 3:
            return n, 0.0, 1.0
        x0, x1 = x[:-1], x[1:]
        x0m, x1m = x0 - x0.mean(), x1 - x1.mean()
        denom = float(np.sqrt((x0m ** 2).sum() * (x1m ** 2).sum()))
        rho = float((x0m * x1m).sum() / denom) if denom > 0 else 0.0
        rho = max(-0.999, min(0.999, rho))
        vif = (1.0 + rho) / (1.0 - rho) if rho > 0 else 1.0
        n_eff = int(n / vif) if vif > 0 else n
        return max(1, n_eff), round(rho, 4), round(vif, 3)
    except Exception as e:
        logger.warning("regime_empirical: effective-N failed (%s).", e)
        return len(series), 0.0, 1.0


def _label_from_terciles(current: float, low_b: float, high_b: float) -> str:
    if current < low_b:
        return "LOW_VOL"
    if current > high_b:
        return "HIGH_VOL"
    return "NORMAL"


def _static_label(current: float, low_thr: float, high_thr: float) -> str:
    if current < low_thr:
        return "LOW_VOL"
    if current > high_thr:
        return "HIGH_VOL"
    return "NORMAL"


def _size_mult(label: str, low_m: float, norm_m: float, high_m: float) -> float:
    return {"LOW_VOL": low_m, "HIGH_VOL": high_m}.get(label, norm_m)


def _stop_mult(label: str, high_stop_m: float) -> float:
    return high_stop_m if label == "HIGH_VOL" else 1.0


def _prev_empirical_label() -> "str | None":
    """Last shadow row's empirical label (for a whipsaw indicator). None if no prior/parse fails."""
    try:
        if not _SHADOW_PATH.exists():
            return None
        last = None
        with open(_SHADOW_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if not last:
            return None
        return (json.loads(last) or {}).get("empirical_label")
    except Exception:
        return None


def compute_empirical_shadow() -> dict:
    """Compute the shadow empirical-vol classification vs the static classifier. NEVER RAISES."""
    ts = datetime.now(PT).isoformat()
    base = {"ts": ts, "ts_utc": datetime.now(timezone.utc).isoformat(), "fresh": False, "note": ""}
    try:
        import config
        from data.fetcher import fetch_bars
        low_thr = float(getattr(config, "REGIME_LOW_VOL_THRESHOLD", 12.0))
        high_thr = float(getattr(config, "REGIME_HIGH_VOL_THRESHOLD", 25.0))
        low_m = float(getattr(config, "REGIME_LOW_VOL_SIZE_MULT", 1.25))
        norm_m = float(getattr(config, "REGIME_NORMAL_SIZE_MULT", 1.0))
        high_m = float(getattr(config, "REGIME_HIGH_VOL_SIZE_MULT", 0.5))
        high_stop_m = float(getattr(config, "REGIME_HIGH_VOL_STOP_MULT", 1.5))

        df = fetch_bars("SPY", config.TF_DAILY, num_bars=_HISTORY_BARS)
        if df is None or getattr(df, "empty", True) or "close" not in df or len(df) < _RVOL_LOOKBACK + 2:
            base["note"] = "SPY history unavailable"
            return base
        series = _rvol_series(df["close"])
        if not series or len(series) < 2:
            base["note"] = "rvol series empty"
            return base

        current = series[-1]
        hist = series[:-1]              # strictly-trailing: exclude the observation being classified
        n_raw = len(hist)
        n_eff, rho, vif = _effective_n(hist)

        import numpy as np
        low_b = float(np.percentile(hist, _LOW_PCTILE))
        high_b = float(np.percentile(hist, _HIGH_PCTILE))

        emp_label = _label_from_terciles(current, low_b, high_b)
        stat_label = _static_label(current, low_thr, high_thr)

        emp_size = _size_mult(emp_label, low_m, norm_m, high_m)
        stat_size = _size_mult(stat_label, low_m, norm_m, high_m)
        emp_stop = _stop_mult(emp_label, high_stop_m)
        stat_stop = _stop_mult(stat_label, high_stop_m)

        # Board one-sided clamp (LOG-ONLY here): empirical may only tighten vs static.
        clamped_size = min(emp_size, stat_size)
        clamped_stop = max(emp_stop, stat_stop)
        would_size_up = bool(emp_label == "LOW_VOL" and current < low_thr)  # requires empirical AND absolute agreement

        sufficient = bool(n_raw >= _MIN_RAW_SAMPLES and n_eff >= _MIN_EFF_SAMPLES)
        prev = _prev_empirical_label()

        base.update({
            "fresh": True,
            "current_rvol": round(current, 3),
            "n_raw": n_raw,
            "n_eff": n_eff,
            "autocorr_lag1": rho,
            "vif": vif,
            "sufficient_samples": sufficient,
            "tercile_low": round(low_b, 3),
            "tercile_high": round(high_b, 3),
            "empirical_label": emp_label,
            "static_label": stat_label,
            "empirical_size_mult": emp_size,
            "static_size_mult": stat_size,
            "clamped_size_mult": clamped_size,        # = min(empirical, static) — never > static
            "empirical_stop_mult": emp_stop,
            "static_stop_mult": stat_stop,
            "clamped_stop_mult": clamped_stop,          # = max(empirical, static) — never < static
            "would_size_up": would_size_up,
            "labels_agree": bool(emp_label == stat_label),
            "flipped_from_prev": bool(prev is not None and prev != emp_label),
            "note": "" if sufficient else "insufficient_effective_samples (shadow logs anyway; not trustworthy for live)",
        })
        return base
    except Exception as e:
        logger.warning("regime_empirical: compute failed (%s) — UNKNOWN shadow row.", e)
        base["note"] = "compute failed: %s" % e
        return base


def write_shadow_row(row: dict) -> bool:
    """Append one shadow row to logs/regime_empirical_shadow.jsonl. Best-effort, never raises."""
    try:
        _LOGS.mkdir(parents=True, exist_ok=True)
        with open(_SHADOW_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except Exception as e:
        logger.warning("regime_empirical: shadow write failed (%s).", e)
        return False


def run_shadow(write: bool = False) -> dict:
    row = compute_empirical_shadow()
    if write:
        write_shadow_row(row)
    return row


def main() -> int:
    import sys
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    # Load .env so a standalone/cron snapshot has the Alpaca keys data.fetcher reads via os.getenv
    # (matches strategy/regime_state.py + run_macro_regime.py). CLI-entry only; import stays side-effect-free.
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except Exception as _e:
        logger.debug("regime_empirical: dotenv not loaded (%s).", _e)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    row = run_shadow(write=True)
    print("Empirical vol-regime shadow @ %s" % row.get("ts"))
    if row.get("fresh"):
        print("  current_rvol=%s | empirical=%s static=%s (agree=%s) | clamped_size=%s would_size_up=%s | n_eff=%s sufficient=%s" % (
            row.get("current_rvol"), row.get("empirical_label"), row.get("static_label"),
            row.get("labels_agree"), row.get("clamped_size_mult"), row.get("would_size_up"),
            row.get("n_eff"), row.get("sufficient_samples")))
    else:
        print("  UNKNOWN (%s)" % row.get("note"))
    print("Shadow log: logs/regime_empirical_shadow.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

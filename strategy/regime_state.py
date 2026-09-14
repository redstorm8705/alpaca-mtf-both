# ruff: noqa: E501  — long design/docstring lines (matches the sibling regime modules mr_regime.py / run_macro_regime.py)
"""strategy/regime_state.py — authoritative RegimeState aggregation object (Phase 1).

Composes the three existing MARKET-WIDE regime views into ONE read-only object and writes a canonical
ledger (logs/regime_state.json + logs/regime_history.jsonl). This is the Layer-3 "authoritative regime
object" + the Layer-8 "current-regime / regime-history ledger" the architecture review called for.

PHASE 1 IS NON-BEHAVIORAL / NON-RISK-PATH: nothing on the trading path imports this module. It changes
NO gating, NO sizing, NO entry/exit — it only ADDS the aggregation object + the ledger. The three
underlying regimes keep their exact current wiring. Consumers migrate in Phase 3; replacing the static
thresholds with rolling empirical distributions is Phase 2 (risk-path, separately gated).

Design record: logs/design_records/regime_state_phase1_2026-09-13.md

Components (all READ-ONLY, each independently fail-safe -> UNKNOWN/stale, never a spurious value):
  - vol       : strategy.volatility_regime.RegimeDetector (VIX/SPY-vol regime + composite)   [real-time]
  - macro     : the weekly cache logs/macro_regime_latest.json (label/confidence/risk_tier)  [structural]
  - market_mr : execution.mr_regime.regime_state(SPY daily bars) (variance-ratio / Hurst)     [real-time]

Usage: python3 strategy/regime_state.py   (snapshot -> writes the ledger)
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _ROOT / "logs"
# NOTE on sys.path: the standalone-CLI path needs the repo root on sys.path so the lazy imports in the
# component functions resolve; that setup lives in main() (NOT at module import), so importing this
# module has NO side effect on interpreter state. A Phase-3 consumer that imports this module is
# already running with the repo root on sys.path, so the import path needs no mutation.
PT = ZoneInfo("America/Los_Angeles")

logger = logging.getLogger("regime_state")

_MACRO_CACHE = _LOGS / "macro_regime_latest.json"
_STATE_PATH = _LOGS / "regime_state.json"
_HISTORY_PATH = _LOGS / "regime_history.jsonl"
_MACRO_STALE_DAYS = 9   # macro is a Sunday-weekly cron -> stale if older than ~9 days


def _age_seconds(iso_ts) -> "float | None":
    """Whole seconds since an ISO-8601 timestamp; None if unparseable. Never raises."""
    if not iso_ts:
        return None
    s = str(iso_ts).replace("Z", "+00:00")
    for parse in (s, s[:19] + "+00:00", s[:10]):
        try:
            dt = datetime.fromisoformat(parse)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            continue
    return None


def _vol_component() -> dict:
    """Read-only volatility regime + composite. Fail-safe -> UNKNOWN. Lazy-imported.

    RegimeDetector.get_regime() NEVER returns None and NEVER raises on a data-feed outage: on a
    total VIX+SPY failure its _refresh() early-returns (volatility_regime.py:125-127) WITHOUT
    updating state, and get_regime() then hands back the __init__ defaults (regime=NORMAL,
    realized_vol=0.0) — a plausible value that was never measured. The detector's own freshness
    signal is _last_check: it is set ONLY when a real VIX or SPY fetch succeeds
    (volatility_regime.py:120/129) and stays None when both fail. Gate `fresh` on it so a
    fabricated default is never written to the ledger as fresh (fail-safe -> UNKNOWN instead)."""
    try:
        from strategy.volatility_regime import RegimeDetector
        det = RegimeDetector()
        r = det.get_regime() or {}
        if getattr(det, "_last_check", None) is None:
            # Both the VIX and the SPY-realized-vol fetch failed -> r holds __init__ defaults, not a
            # measurement. Do NOT present it as fresh.
            logger.warning("regime_state: vol detector got no fresh data (VIX+SPY both failed) — UNKNOWN.")
            return {"fresh": False, "regime": "UNKNOWN"}
        comp: dict = {}
        try:
            comp = det.get_composite_regime() or {}
        except Exception as _ce:
            logger.debug("regime_state: composite regime failed (%s)", _ce)
        return {
            "fresh": True,
            "regime": r.get("regime"),
            "realized_vol": r.get("realized_vol"),
            "size_mult": r.get("size_mult"),
            "stop_mult": r.get("stop_mult"),
            "composite": comp.get("composite_regime"),
            "vix_term_ratio": comp.get("vix_term_ratio"),
            "spy_vs_50sma_pct": comp.get("spy_vs_50sma_pct"),
        }
    except Exception as e:
        logger.warning("regime_state: vol component failed (%s) — UNKNOWN.", e)
        return {"fresh": False, "regime": "UNKNOWN"}


def _macro_component() -> dict:
    """Read the weekly macro cache (do NOT re-run the analysis). Fail-safe -> UNKNOWN/stale."""
    try:
        if not _MACRO_CACHE.exists():
            return {"fresh": False, "label": "UNKNOWN", "note": "macro cache absent"}
        data = json.loads(_MACRO_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"fresh": False, "label": "UNKNOWN", "note": "macro cache not a dict"}
        age = _age_seconds(data.get("generated_at"))
        fresh = age is not None and age <= _MACRO_STALE_DAYS * 86400
        return {
            "fresh": bool(fresh),
            "label": data.get("regime_label", "UNKNOWN"),
            "confidence": data.get("confidence"),
            "composite_score": data.get("composite_score"),
            "risk_tier": data.get("risk_tier"),
            "age_days": round(age / 86400, 1) if age is not None else None,
        }
    except Exception as e:
        logger.warning("regime_state: macro component failed (%s) — UNKNOWN.", e)
        return {"fresh": False, "label": "UNKNOWN"}


def _market_mr_component() -> dict:
    """Market-wide mean-reversion = execution.mr_regime.regime_state(SPY daily bars). Fail-safe."""
    try:
        import config
        from data.fetcher import fetch_bars
        from execution.mr_regime import regime_state as _mr_state
        df = fetch_bars("SPY", config.TF_DAILY, num_bars=70)
        if df is None or getattr(df, "empty", True):
            return {"fresh": False, "mean_reverting": None, "note": "SPY bars unavailable"}
        st = _mr_state(df) or {}
        computed = st.get("variance_ratio") is not None or st.get("hurst") is not None
        return {
            "fresh": bool(computed),
            "mean_reverting": st.get("mean_reverting"),
            "variance_ratio": st.get("variance_ratio"),
            "hurst": st.get("hurst"),
        }
    except Exception as e:
        logger.warning("regime_state: market-MR component failed (%s) — UNKNOWN.", e)
        return {"fresh": False, "mean_reverting": None}


def compute_regime_state() -> dict:
    """Aggregate the 3 market-wide regime views into one authoritative object. NEVER RAISES."""
    vol = _vol_component()
    macro = _macro_component()
    mr = _market_mr_component()
    any_stale = not (bool(vol.get("fresh")) and bool(macro.get("fresh")) and bool(mr.get("fresh")))
    return {
        "ts": datetime.now(PT).isoformat(),
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "vol": vol,
        "macro": macro,
        "market_mr": mr,
        "summary": {
            "vol_regime": vol.get("regime"),
            "vol_composite": vol.get("composite"),
            "macro_label": macro.get("label"),
            "market_mean_reverting": mr.get("mean_reverting"),
            "any_stale": any_stale,
        },
    }


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _history_record(state: dict) -> dict:
    """One compact history line: the summary LABELS plus the RAW numeric signal values and per-component
    freshness. The raw numbers (realized_vol, variance_ratio, hurst, term/50sma ratios, macro score) are what
    a future rolling-empirical-distribution recalibration must read — logging only the statically-derived
    labels would be circular (a threshold cannot be re-derived from the labels it produced). The `*_fresh`
    flags let that consumer exclude stale samples from the distribution, exactly as the shadow trackers do.
    All values are `.get()` with None defaults, so a partial/UNKNOWN state still yields a valid line."""
    def _d(key: str) -> dict:
        # Robust to a non-dict outer state AND a non-mapping sub-value (e.g. state["vol"] not a dict):
        # either yields {}, so the .get()s below and `**summary` never raise. The sole producer
        # compute_regime_state() always emits dict sub-values; this is defense-in-depth.
        v = state.get(key) if isinstance(state, dict) else None
        return v if isinstance(v, dict) else {}
    vol, macro, mr, summary = _d("vol"), _d("macro"), _d("market_mr"), _d("summary")
    return {
        "ts": state.get("ts") if isinstance(state, dict) else None,
        **summary,
        # raw numeric signal values — Phase-2 rolling distributions read THESE, not the labels
        "realized_vol": vol.get("realized_vol"),
        "vix_term_ratio": vol.get("vix_term_ratio"),
        "spy_vs_50sma_pct": vol.get("spy_vs_50sma_pct"),
        "variance_ratio": mr.get("variance_ratio"),
        "hurst": mr.get("hurst"),
        "macro_composite_score": macro.get("composite_score"),
        "macro_confidence": macro.get("confidence"),
        # per-component freshness — a Phase-2 consumer filters stale samples out of the distribution
        "vol_fresh": bool(vol.get("fresh")),
        "macro_fresh": bool(macro.get("fresh")),
        "mr_fresh": bool(mr.get("fresh")),
    }


def write_regime_ledger(state: dict) -> bool:
    """Atomically write the current state + append one compact history line. Best-effort, never raises."""
    ok = True
    try:
        _LOGS.mkdir(parents=True, exist_ok=True)
        _atomic_write(_STATE_PATH, json.dumps(state, indent=2))
    except Exception as e:
        logger.warning("regime_state: current-state write failed (%s).", e)
        ok = False
    try:
        line = json.dumps(_history_record(state))
        with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning("regime_state: history append failed (%s).", e)
        ok = False
    return ok


def get_regime_state(write: bool = False) -> dict:
    """Public accessor: compute the authoritative regime state (optionally persist the ledger).

    Phase 1: no bot consumer calls this yet — it is here for the Phase-3 migration + the snapshot CLI."""
    state = compute_regime_state()
    if write:
        write_regime_ledger(state)
    return state


def main() -> int:
    # Standalone CLI only: `python3 strategy/regime_state.py` puts strategy/ (not the repo root) on
    # sys.path[0], so the lazy config/data.fetcher/strategy.*/execution.* imports in the component
    # functions would ModuleNotFoundError. Prepend the repo root HERE (not at module import) so a
    # standalone snapshot resolves them; importing this module stays side-effect-free.
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    # Load .env so a standalone/cron snapshot has the Alpaca keys the market-MR SPY fetch needs
    # (data.fetcher reads os.getenv("ALPACA_API_KEY")); without it market_mr fail-safes to UNKNOWN.
    # Matches the sibling cron run_macro_regime.py. Runs ONLY in the CLI entry, never on import; an
    # in-process bot caller already has the env loaded.
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except Exception as _e:
        logger.debug("regime_state: dotenv not loaded (%s) — market_mr may be UNKNOWN without env.", _e)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    state = get_regime_state(write=True)
    s = state["summary"]
    print("Regime snapshot @ %s" % state["ts"])
    print("  vol=%s composite=%s | macro=%s | market_MR=%s | any_stale=%s" % (
        s.get("vol_regime"), s.get("vol_composite"), s.get("macro_label"),
        s.get("market_mean_reverting"), s.get("any_stale")))
    print("Ledger: logs/regime_state.json + logs/regime_history.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

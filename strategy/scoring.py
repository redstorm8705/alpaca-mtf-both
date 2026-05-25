"""
strategy/scoring.py
Cycle-scoped live score cache and RC-8 buffer clear — extracted from main.py Phase 2.

Owns:
  - _live_score_cache           P1-SCORE-CACHE: per-cycle {(symbol, direction): score}
  - get_live_score()            fetch + cache 12-pt score for an open position
  - clear_live_score_cache()    reset cache at run_cycle() start
  - rc8_clear_buffers()         RC-8: clear entry gate buffers on disqualification

B2 fix: _live_score_cache was a bare main.py global shared between run_cycle() and
execute_entries(). Moving it here breaks the dependency that was blocking lifecycle.py
extraction (Extraction 7). GateState remains in main.py — passed explicitly to
rc8_clear_buffers() as a parameter (DS Condition 1, Phase 2 board vote).
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# P1-SCORE-CACHE: cycle-scoped live score cache
# Reset at run_cycle() start; populated lazily by get_live_score().
# ---------------------------------------------------------------------------

_live_score_cache: dict = {}


def clear_live_score_cache() -> None:
    """Reset per-cycle score cache. Call at the top of each run_cycle() pass."""
    _live_score_cache.clear()


# ---------------------------------------------------------------------------
# Live score fetch
# ---------------------------------------------------------------------------

def get_live_score(symbol: str, direction: str, trade_mode: str):
    """Fetch current 12-point confluence score for an open position.

    Called only when a reversal signal is active — not on every scan cycle.
    Returns int score or None on failure (fail-open: caller skips score gate).

    P1-SCORE-CACHE: return cached result if already computed this cycle.
    Cache key = (symbol, direction) — direction-aware since long/short scoring differs.
    """
    _cache_key = (symbol, direction)
    if _cache_key in _live_score_cache:
        return _live_score_cache[_cache_key]
    try:
        from strategy.signal_generator import _analyze_symbol_full
        result = _analyze_symbol_full(symbol, trade_mode)
        if result is None:
            return None
        side_key = "long" if direction == "long" else "short"
        sig = result.get(side_key, {})
        score = sig.get("score")
        _result = int(score) if score is not None else None
        _live_score_cache[_cache_key] = _result   # P1-SCORE-CACHE
        return _result
    except Exception as _lse:
        logger.warning(f"[{symbol}] get_live_score failed: {_lse}")
        return None


# ---------------------------------------------------------------------------
# RC-8 buffer clear
# ---------------------------------------------------------------------------

def rc8_clear_buffers(sym: str, reason: str, gate_state) -> None:
    """RC-8 compliance: clear entry gate buffers when a symbol exits consideration.

    Must be called whenever a symbol is disqualified for any reason — not only on
    daily reset or trade close. Prevents unbounded scan-buffer accumulation.

    gate_state: GateState instance (conviction_streak + entry_confirm_buffer).
                Passed explicitly — no main.py global access (DS Condition 1, Phase 2).

    Persist: calls state.persistence.write_confirm_gate() only if buffers were non-empty
             (avoids unnecessary disk writes on every disqualification).
    """
    _prev_buf = gate_state.entry_confirm_buffer.pop(sym, None)
    _prev_str = gate_state.conviction_streak.pop(sym, None)
    if _prev_buf or _prev_str:
        logger.info(
            f"[{sym}] RC-8 BUFFER CLEAR ({reason}): "
            f"confirm_buf={_prev_buf} streak={_prev_str} — "
            f"reset; fresh confirmation required on re-entry."
        )
        from state.persistence import write_confirm_gate
        write_confirm_gate(
            gate_state.entry_confirm_buffer,
            gate_state.conviction_streak,
        )

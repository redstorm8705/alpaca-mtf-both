#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale/docstring + aligned-dict rows run long (matches research/ic_engine.py convention)
"""
research/trade_record_reducer.py — replay lifecycle event logs -> one row per CLOSED trade.
Edge-discovery Step 1 (design: logs/design_records/edge_discovery_2026-09-19.md, BGGN 5/5).

WHY: research/ic_engine.py, research/deflated_sharpe.py and research/regime_empirical.py are built
and starved — they need one labeled row PER CLOSED TRADE, tier-agnostic, with confidence +
component decomposition + realized R-multiple + (where available) MAE/MFE. The tiers log
ASYMMETRICALLY today, so nothing is measurable across tiers. This reducer normalizes them.

Reads (READ-ONLY, offline; writes logs/ only; RTH block removed):
  - logs/trade_events.jsonl     (INTRADAY/swing tier; NO trade_id yet -> joined by symbol + FIFO time order)
  - logs/day_tier_events.jsonl  (DAY tier; trade_id-keyed; price_samples -> MAE/MFE; decision-joined
                                 via entry_fill.decision_id == decision.decision_id)
  - 1-min T1 bars via data.fetcher.fetch_bars_window (a READ-ONLY DATA import, NOT an execution
    import — the only Alpaca client lives in data/fetcher.py) to reconstruct the SWING tier's MAE/MFE
    over each closed trade's [ts_entry, ts_exit] span. This is the ONLY network dependency; it is
    fully fail-safe (any miss -> honest null MAE/MFE, never a fabricated 0.0) and skippable with
    --no-bars. The swing tier has no live price_sample stream (unlike the day tier), and a live
    5-min water-mark would be blind to the overnight gap (the bot restarts nightly) and un-backfillable
    — bar reconstruction is accurate to 1-min and backfills the full closed-trade history on run 1
    (board 3/3 + Gro, 2026-09-21; design record edge_discovery_2026-09-19.md).
Writes:
  - logs/trade_records.jsonl    (ONE row per closed trade, tier-tagged; DERIVED => safe to delete +
                                 regenerate; BACKFILLS the existing day-tier history)

This is a measurement substrate. It never computes or masks P&L — realized_pnl is read from the
logs (which sourced it from Alpaca fills). Records with a known bad/orphan join are counted and
reported, never silently emitted as if clean.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Event ts in this system are PT-aware ISO strings (trade_logger.PT). Used ONLY as a
# defensive fallback when a legacy ts is tz-NAIVE — a naive ts is assumed PT before
# converting to UTC, never compared naive against the UTC-indexed bar frame.
_PT = ZoneInfo("America/Los_Angeles")

# Anchor to repo root (parent of research/) so `import trade_logger` works whether this is run as
# `python3 -m research.trade_record_reducer` or `python3 research/trade_record_reducer.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trade_logger import (  # noqa: E402  (path insert must precede this import)
    compute_realized_R,
    make_components,
    make_entry_record,
    make_exit_record,
    make_trade_record,
)

_LOGS = _REPO_ROOT / "logs"
_INTRADAY_EVENTS = _LOGS / "trade_events.jsonl"
_DAYTIER_EVENTS = _LOGS / "day_tier_events.jsonl"
_OUT = _LOGS / "trade_records.jsonl"

# Intraday lifecycle events the reducer consumes (everything else — delta_shadow, mri_refresh,
# halt_eval, breadth_refresh, signal, drift_detected, stop_promotion, breakeven_push — is ignored).
_INTRADAY_EXIT_EVENTS = {"exit", "stop_hit"}

# Frozen 12-pt weight snapshot for weights_version "12pt-2026-09" (config.py:173 SCORE_WEIGHTS as
# of 2026-09). Hardcoded — NOT read from live config — so a backfill is DETERMINISTIC across the
# documented live rsi_in_range->volume_confirmed toggle (config.py:178-181): the same input log
# always yields the same components, and the stamped weights_version stays truthful. If this
# snapshot ever changes, bump _INTRADAY_WEIGHTS_VERSION so old rows remain attributable.
_INTRADAY_WEIGHTS_2026_09 = {
    "daily_above_150sma": 2, "daily_above_200sma": 1, "ema13_above_ema30": 2,
    "macd_bullish_cross": 2, "rsi_in_range": 1, "price_near_vwap": 2, "momentum_12_1": 2,
}
_INTRADAY_WEIGHTS_VERSION = "12pt-2026-09"

# Day-tier lifecycle events are ALSO dual-written to the shared trade_events.jsonl by
# day_trade_manager (data_source/tier="daytrade") — the reducer must NOT ingest them on the
# intraday path (they are handled canonically by reduce_day_tier from day_tier_events.jsonl).
_DAYTRADE_TAGS = {"daytrade"}


def _num(x):
    """float(x) or None — never raises. A missing/garbage numeric field becomes an honest None
    (recorded as 'unknown'), never a silent 0.0 that would mask a real loss."""
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """Read a JSONL file tolerating a torn trailing line (kill-9 before fsync). Returns
    (rows, skipped). Missing file -> ([], 0)."""
    rows: list[dict] = []
    skipped = 0
    if not path.exists():
        return rows, skipped
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                skipped += 1
    return rows, skipped


def _by_ts(rows: list[dict]) -> list[dict]:
    """Stable-sort by ts (ISO-8601 strings sort chronologically); missing ts sinks to front."""
    return sorted(rows, key=lambda r: r.get("ts") or "")


# ── SWING-TIER MAE/MFE reconstruction from 1-min T1 bars (Inc 2 Piece 1c) ──────────────────────
# The swing tier logs NO per-observation price stream (unlike the day tier's price_sample path), so
# MAE/MFE are reconstructed OFFLINE from 1-min bars over each closed trade's [ts_entry, ts_exit].
# Board 3/3 + Gro (2026-09-21) chose this over a live 5-min water-mark: the live path would be blind
# to the overnight gap (the bot restarts nightly), un-backfillable, and would mutate two RTH hotspots
# with a per-scan fsync — bar reconstruction is 1-min accurate, backfills all history, and never
# touches the trading thread. compute_mae_mfe_R (trade_logger) orients the raw min/max by side.

# One config.TF_1M bar spans 60s. _MAE_BOUNDARY_SECS MUST equal the fetched bar's span — if the
# reconstruction timeframe ever changes from config.TF_1M, update this with it (the interior filter
# drops exactly the two partial boundary bars of this width).
_MAE_BOUNDARY_SECS = 60      # PROV:bar-1min-secs
# Split / corporate-action / glitch guard: an interior bar beyond [0.5x, 2.0x] the entry fill is a
# >=2:1 (reverse) split or a bad datum (raw bars vs a raw entry that predates a mid-hold split),
# which would FABRICATE a huge multi-R excursion on a populated row. A real large-cap (S&P500/NDX100)
# hold never halves or doubles within the position -> emit honest null instead of a fabricated fat
# tail. Same data-integrity class as the RC-4 +/-50% fill-price sanity band.
_MAE_SPLIT_LO_MULT = 0.5     # PROV:split-sanity-band
_MAE_SPLIT_HI_MULT = 2.0     # PROV:split-sanity-band


def _parse_utc(ts):
    """Parse an ISO ts (PT-aware in this system) to a tz-aware UTC datetime, or None.
    Defensive: a tz-NAIVE legacy ts is assumed PT (the house display zone) BEFORE converting,
    so a naive value is never compared against the UTC-indexed bar frame (the board's tz trap)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_PT)
    return dt.astimezone(timezone.utc)


def _reconstruct_mae_mfe(symbol, entry_price, exit_price, ts_entry, ts_exit, *, bar_fetch=None):
    """Reconstruct (min_price, max_price) over a CLOSED swing trade's [ts_entry, ts_exit] span from
    1-min T1 bars. Returns raw prices; trade_logger.compute_mae_mfe_R orients them into mae_R/mfe_R
    by side. The WHOLE body is fail-safe -> (None, None): a parse/fetch/tz failure yields an honest
    null (recorded as 'unknown'), NEVER aborts the reduce and NEVER fabricates a 0.0.

    Correctness (board 3/3 acceptance gates + adversarial gate + live-probed conventions, 2026-09-21):
      - TZ: event ts are PT-aware; Alpaca 1-min bars are UTC-indexed with bar_ts = START-of-bar
        (probed on OCI). Both endpoints normalized to UTC (via the zone object -> DST-safe) and
        compared aware-to-aware against the UTC index.
      - BOUNDARY bars: a 1-min bar's OHLC aggregates the whole minute, so the bars straddling
        ts_entry/ts_exit carry pre-entry / post-exit prints. Keep ONLY bars fully inside the span:
        bar_ts >= entry_utc AND bar_ts <= exit_utc - one_bar. min/max are SEEDED with the actual
        entry & exit fills so the band contains the realized endpoints (keeps the invariant).
      - HONEST NULL over a fabricated value: a fetch miss/empty, OR an EMPTY interior (a hold too short
        to contain a full bar, or all-boundary), is UNMEASURABLE at bar granularity -> (None, None).
        We do NOT fall back to a seed-only band, because for a long winner that yields mae_R=0.0 — a
        FABRICATED "the stop was never threatened", the single most dangerous input to stop calibration.
      - SPLIT/GLITCH: an interior bar beyond [0.5x, 2.0x] the entry fill is a >=2:1 split or bad datum
        (raw bars vs a raw pre-split entry) -> honest null, never a fabricated multi-R fat tail.
      - COVERAGE: fetch_bars_window pins feed=SIP + adjustment=RAW; SIP returns ~100% of RTH minutes
        AND extended-hours bars for the S&P500/NDX100 universe (probed on OCI: AAPL 391 RTH incl 229
        pre-market; IEX also 390), so an overnight swing hold's gap / pre-market extreme IS captured.

    bar_fetch is injectable (tests pass a stub / main() passes --no-bars); default lazily imports the
    real fetcher so this module imports cleanly where the Alpaca SDK is absent (local/CI)."""
    try:
        e = _num(entry_price)
        x = _num(exit_price)
        u0 = _parse_utc(ts_entry)
        u1 = _parse_utc(ts_exit)
        if e is None or e <= 0 or u0 is None or u1 is None or u1 <= u0:
            return None, None

        fetch = bar_fetch
        if fetch is None:
            import config
            from data.fetcher import fetch_bars_window

            def fetch(sym, s, en):
                return fetch_bars_window(sym, config.TF_1M, s, en)

        df = fetch(symbol, u0, u1)
        if df is None or len(df) == 0:
            return None, None  # fetch miss / no bars -> honest null (never a seed-only guess)

        # INTERIOR bars only (drop the two boundary minutes). df.index is a tz-aware UTC DatetimeIndex
        # (verified at source) -> aware-to-aware comparison; a tz-naive index would raise here and be
        # caught by the outer guard -> (None, None). An EMPTY interior is unmeasurable -> honest null
        # (NOT a seed-only 0.0 fabrication).
        cutoff = u1 - timedelta(seconds=_MAE_BOUNDARY_SECS)
        interior = df[(df.index >= u0) & (df.index <= cutoff)]
        if len(interior) == 0:
            return None, None
        ilo = _num(interior["low"].min())
        ihi = _num(interior["high"].max())
        if ilo is None or ihi is None:
            return None, None

        # Split / corp-action / glitch guard -> honest null (see _MAE_SPLIT_*_MULT).
        if ilo < _MAE_SPLIT_LO_MULT * e or ihi > _MAE_SPLIT_HI_MULT * e:
            return None, None

        # Seed with the actual entry & exit fills (the band must contain the realized endpoints ->
        # keeps -mae_R <= realized_R <= mfe_R), then fold in the interior extremes.
        lo = min(e, x, ilo) if x is not None else min(e, ilo)
        hi = max(e, x, ihi) if x is not None else max(e, ihi)
        return round(lo, 4), round(hi, 4)  # PROV:feat-units-4dp
    except Exception:
        return None, None


# ── INTRADAY: symbol + FIFO time-order join (no trade_id in the current schema) ────────────────

def reduce_intraday(rows: list[dict], mae_mfe_fn=None) -> tuple[list[dict], dict]:
    """Join intraday entry -> exit/stop_hit per symbol in FIFO time order. partial_exit accumulates
    onto the oldest open lot; exit_pnl_correction rewrites the matching closed row's P&L (never mask
    a loss — the correction IS the true fill). Returns (records, stats).

    mae_mfe_fn(symbol, entry_price, exit_price, ts_entry, ts_exit) -> (min_px, max_px) reconstructs
    the swing-tier MAE/MFE (Inc 2 Piece 1c). DEFAULT is a null stub (this library function stays PURE
    / offline / network-free, so it is deterministic and the ReducerIntraday tests need no network);
    main() opts INTO the real 1-min-bar reconstruction (_reconstruct_mae_mfe) explicitly, and tests
    inject their own stub. Whatever is passed is fail-safe (never raises), so a miss just yields
    honest-null MAE/MFE on that row — a signal is never dropped for a bar-fetch failure."""
    if mae_mfe_fn is None:
        def mae_mfe_fn(*_a, **_k):
            return None, None
    open_lots: dict[str, list[dict]] = defaultdict(list)   # symbol -> FIFO list of open entry dicts
    last_closed: dict[str, dict] = {}                       # symbol -> most-recent emitted closed row
    records: list[dict] = []
    stats = {"entries": 0, "closed": 0, "orphan_exits": 0, "partials": 0, "orphan_partials": 0,
             "corrections": 0, "correction_unmatched": 0, "daytrade_skipped": 0,
             "orphan_wins": 0, "orphan_losses": 0, "orphan_pnl_sum": 0.0,
             "mae_populated": 0, "mae_null": 0}

    for ev in _by_ts(rows):
        # TIER ISOLATION: day-tier trades are dual-written to this shared file by
        # day_trade_manager (data_source/tier="daytrade"). Skip them here — they are reduced
        # canonically from day_tier_events.jsonl. Ingesting them would double-count the trade,
        # mask its loss ($0 because it logs realized_pnl=/exit_reason=, not pnl=/reason=), and
        # let a day-tier entry FIFO-pair with an intraday exit for the same symbol.
        if ev.get("data_source") in _DAYTRADE_TAGS or ev.get("tier") in _DAYTRADE_TAGS:
            stats["daytrade_skipped"] += 1
            continue

        et = ev.get("event")
        sym = ev.get("symbol", "")

        if et == "entry":
            stats["entries"] += 1
            open_lots[sym].append({
                "entry_price": ev.get("price"),
                "stop":        ev.get("stop"),
                "target":      ev.get("target"),
                "side":        ev.get("direction", ""),
                "qty":         ev.get("size"),          # ORIGINAL entry size (immutable — the trade's qty)
                "score":       ev.get("score"),
                "mri_level":   ev.get("mri_level", "UNKNOWN"),
                "conditions":  ev.get("conditions") or {},
                "ts_entry":    ev.get("ts"),
                "trade_id":    ev.get("trade_id"),   # Inc 2: real minted id when present; None for legacy
                "regime":      ev.get("regime"),     # Inc 2 Piece 1b: swing-tier daily regime label
                "indicators":  {**{k: ev[k] for k in (
                    "tsmom_12m", "tsmom_6m", "tsmom_ewma_vol", "tsmom_vol_mult",
                    "tsmom_direction", "score_16pt") if k in ev},
                    # Inc 2 Piece 1b-ii: raw continuous entry-TF indicators (rsi/ema/macd/vwap)
                    **(ev["indicators_raw"] if isinstance(ev.get("indicators_raw"), dict) else {}),
                    # Inc 2 Piece 1b: regime freshness/detail for age-filtering in research
                    **({"regime_ts": ev["regime_ts"]} if "regime_ts" in ev else {}),
                    **({"regime_age_sec": ev["regime_age_sec"]} if "regime_age_sec" in ev else {}),
                    **({"regime_detail": ev["regime_detail"]} if "regime_detail" in ev else {})},
            })

        elif et == "partial_exit":
            lots = open_lots.get(sym)
            if lots:
                # Count for traceability only. P&L is NOT accumulated here: the FINAL exit's `pnl`
                # is already the TOTAL (remaining leg + ALL partial tranches) per
                # portfolio_tracker.record_exit (_total_pnl, line ~1830/1916). Re-adding partials
                # would double-count them (a losing trade could read as a gain).
                stats["partials"] += 1
            else:
                stats["orphan_partials"] += 1

        elif et in _INTRADAY_EXIT_EVENTS:
            lots = open_lots.get(sym)
            if not lots:
                stats["orphan_exits"] += 1          # exit with no matching entry -> no entry features
                _op = _num(ev.get("pnl"))
                if _op is not None:
                    stats["orphan_pnl_sum"] += _op
                    if _op > 0:
                        stats["orphan_wins"] += 1
                    elif _op < 0:
                        stats["orphan_losses"] += 1
                continue
            lot = lots.pop(0)
            reason = ev.get("reason") or et
            realized = _num(ev.get("pnl"))          # ALREADY the trade total (leg + all partials); honest None if absent
            entry_rec = make_entry_record(
                # Inc 2: prefer the real minted id (deterministic cross-log join key); fall
                # back to the synthesized symbol+ts id for legacy rows that predate the mint.
                trade_id=lot.get("trade_id") or f"INTRA-{sym}-{lot.get('ts_entry')}",
                tier="intraday", symbol=sym, side=lot["side"],
                entry_price=lot["entry_price"], stop_price=lot["stop"],
                target_price=lot["target"], qty=lot["qty"],
                score_raw=lot["score"], score_max=12,
                components=make_components(lot["conditions"], _INTRADAY_WEIGHTS_2026_09),
                indicators=lot["indicators"], mri_level=lot["mri_level"],
                regime=lot.get("regime") or "UNKNOWN",   # Inc 2 Piece 1b
                weights_version=_INTRADAY_WEIGHTS_VERSION, model_version="score_raw",
                ts_entry=lot["ts_entry"],
            )
            # Inc 2 Piece 1c: reconstruct swing-tier MAE/MFE from 1-min bars over [ts_entry, ts_exit].
            # Fail-safe (never raises) -> honest-null min/max on any miss; compute_mae_mfe_R orients
            # the raw prices by side. A miss NEVER drops the row (the trade still records, mae/mfe null).
            try:
                _min_px, _max_px = mae_mfe_fn(
                    sym, lot["entry_price"], ev.get("price"), lot["ts_entry"], ev.get("ts"))
            except Exception:
                # Defense-in-depth: the real reconstruction is already internally fail-safe, but a
                # future/injected non-fail-safe fn must still never abort the reduce or drop this row.
                _min_px = _max_px = None
            if _min_px is not None and _max_px is not None:
                stats["mae_populated"] += 1
            else:
                stats["mae_null"] += 1
            exit_rec = make_exit_record(
                trade_id=entry_rec["trade_id"], tier="intraday", symbol=sym,
                side=lot["side"], entry_price=lot["entry_price"], stop_price=lot["stop"],
                exit_price=ev.get("price"), qty=lot["qty"], realized_pnl=realized,
                exit_reason=reason, min_price=_min_px, max_price=_max_px, ts_exit=ev.get("ts"),
            )
            row = make_trade_record(entry_rec, exit_rec)
            records.append(row)
            last_closed[sym] = row
            stats["closed"] += 1

        elif et == "exit_pnl_correction":
            crow = last_closed.get(sym)   # distinct name: `row` above is inferred non-None dict
            if crow is None:
                stats["correction_unmatched"] += 1
                continue
            stats["corrections"] += 1
            corrected_px = ev.get("corrected_exit_price")
            corrected_pnl = ev.get("corrected_pnl")
            orig_pnl = ev.get("original_pnl")
            if corrected_pnl is not None:
                try:
                    # Apply as a DELTA (corrected - original) when the original is known,
                    # so any folded partial-exit P&L already on this row is PRESERVED
                    # (a plain overwrite would silently drop it). If original is absent,
                    # fall back to overwrite. A correction only ever moves realized_pnl
                    # toward the TRUE fill — never masks a loss.
                    if orig_pnl is not None and crow.get("realized_pnl") is not None:
                        delta = float(corrected_pnl) - float(orig_pnl)
                        crow["realized_pnl"] = round(float(crow["realized_pnl"]) + delta, 2)
                    else:
                        crow["realized_pnl"] = round(float(corrected_pnl), 2)
                except (TypeError, ValueError):
                    pass
            if corrected_px is not None:
                try:
                    crow["exit_price"] = round(float(corrected_px), 4)
                    crow["realized_R"] = compute_realized_R(
                        crow.get("entry_price"), crow.get("stop_price"),
                        corrected_px, crow.get("side"))
                    # Piece 1c: the corrected exit is a REAL point on the price path, so the
                    # MAE/MFE excursion band (seeded on the ORIGINAL exit) must contain it —
                    # else realized_R could fall just outside [-mae_R, mfe_R] on a corrected row.
                    # Re-widen to include the corrected realized_R; leave a null (bar-miss) null.
                    _rr = crow.get("realized_R")
                    if _rr is not None:
                        if crow.get("mfe_R") is not None:
                            crow["mfe_R"] = round(max(crow["mfe_R"], _rr, 0.0), 4)  # PROV:feat-units-4dp
                        if crow.get("mae_R") is not None:
                            crow["mae_R"] = round(max(crow["mae_R"], -_rr, 0.0), 4)  # PROV:feat-units-4dp
                except (TypeError, ValueError):
                    pass
            crow["pnl_corrected"] = True

    return records, stats


# ── DAY TIER: trade_id-keyed; decision joined via decision_id; MAE/MFE from price_samples ──────

def reduce_day_tier(rows: list[dict]) -> tuple[list[dict], dict]:
    """Group day-tier events by the entry_fill's trade_id. decision joins via
    entry_fill.decision_id == decision.decision_id. MAE/MFE derive from the price_sample path.
    confidence = decision.conviction (already [0,1]); recorded as score_raw=conviction, score_max=1.
    Returns (records, stats)."""
    decisions_by_did: dict[str, dict] = {}
    ef_by_tid: dict[str, dict] = {}
    stop_by_tid: dict[str, dict] = {}
    tgt_by_tid: dict[str, dict] = {}
    exit_by_tid: dict[str, dict] = {}
    partials_by_tid: dict[str, list] = defaultdict(list)
    samples_by_tid: dict[str, list] = defaultdict(list)

    for ev in rows:
        et = ev.get("event")
        tid = ev.get("trade_id")
        if et == "decision":
            did = ev.get("decision_id") or tid
            if did:
                decisions_by_did[str(did)] = ev   # str on STORE to match the str() lookup below
            continue
        if not tid:
            continue
        if et == "entry_fill":
            ef_by_tid[tid] = ev
        elif et == "stop_placed":
            stop_by_tid[tid] = ev
        elif et == "target_placed":
            tgt_by_tid[tid] = ev
        elif et == "exit_fill":
            exit_by_tid[tid] = ev
        elif et == "partial_exit_fill":
            partials_by_tid[tid].append(ev)
        elif et == "price_sample":
            samples_by_tid[tid].append(ev)

    records: list[dict] = []
    stats = {"entries": len(ef_by_tid), "closed": 0, "open": 0, "decision_joined": 0}

    for tid, ef in ef_by_tid.items():
        xf = exit_by_tid.get(tid)
        if xf is None:
            stats["open"] += 1
            continue  # still open -> not a closed-trade row yet

        sym = ef.get("symbol", "")
        side = ef.get("side", "")
        entry_price = ef.get("fill_price")
        stop_price = (stop_by_tid.get(tid) or {}).get("stop_price")
        target_price = (tgt_by_tid.get(tid) or {}).get("target_price")

        dec_ev = decisions_by_did.get(str(ef.get("decision_id") or ""))
        conviction = None
        indicators: dict = {}
        gex_regime = "UNKNOWN"
        if dec_ev:
            stats["decision_joined"] += 1
            d = dec_ev.get("decision") or {}
            t = dec_ev.get("trigger") or {}
            conviction = d.get("conviction")
            gex_regime = str(d.get("gex_label", "UNKNOWN"))
            indicators = {
                "gex_action":    d.get("gex_action"),
                "gex_label":     d.get("gex_label"),
                "strength":      d.get("strength"),
                "side_score":    d.get("side_score"),
                "mode":          t.get("mode"),
                "vol_confirmed": t.get("vol_confirmed"),
                "entry_ref":     t.get("entry_ref"),
                "wall_ref":      t.get("wall_ref"),
            }

        # MAE/MFE from the price path while open (min/max of the sampled market prices).
        prices = [s.get("market_price") for s in samples_by_tid.get(tid, [])
                  if isinstance(s.get("market_price"), (int, float))]
        min_px = min(prices) if prices else None
        max_px = max(prices) if prices else None

        # Day-tier exit_fill / partial_exit_fill realized_pnl are logged LEG-ONLY (day_trade_manager
        # realized = (fill-entry)*qty), so summing them is correct (NOT double-counted, unlike the
        # intraday total-basis path). Honest None if the base exit somehow dropped its P&L.
        base_pnl = _num(xf.get("realized_pnl"))
        if base_pnl is None:
            realized = None
        else:
            realized = base_pnl
            for pe in partials_by_tid.get(tid, []):
                realized += (_num(pe.get("realized_pnl")) or 0.0)

        entry_rec = make_entry_record(
            trade_id=tid, tier="daytrade", symbol=sym, side=side,
            entry_price=entry_price, stop_price=stop_price, target_price=target_price,
            qty=ef.get("fill_qty"),
            # Day-tier's "score" IS its conviction on a [0,1] scale, so score_max=1.0 always
            # (not the intraday 12-pt default). confidence = conviction; None if no decision-join.
            score_raw=conviction, score_max=1.0,
            confidence=conviction, indicators=indicators, gex_regime=gex_regime,
            weights_version="gex-daytier", model_version="conviction",
            notional=ef.get("notional"), ts_entry=ef.get("ts"),
        )
        exit_rec = make_exit_record(
            trade_id=tid, tier="daytrade", symbol=sym, side=side,
            entry_price=entry_price, stop_price=stop_price,
            exit_price=xf.get("fill_price"), qty=ef.get("fill_qty"),
            realized_pnl=realized, exit_reason=xf.get("exit_reason", ""),
            min_price=min_px, max_price=max_px, ts_exit=xf.get("ts"),
        )
        records.append(make_trade_record(entry_rec, exit_rec))
        stats["closed"] += 1

    return records, stats


def _atomic_write_jsonl(path: Path, records: list[dict]) -> None:
    """tmp -> os.replace atomic write so a reader never sees a half-written file."""
    os.makedirs(path.parent, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _summary(records: list[dict]) -> str:
    """Concise measurement summary (per tier: N, MATCHED-ONLY win rate by P&L sign, avg realized_R).
    Win rate is over trades whose entry could be joined to an exit; orphan exits (no matched entry)
    are excluded and reported separately by main() so the conditioning is auditable (Finding 3)."""
    by_tier: dict[str, list] = defaultdict(list)
    for r in records:
        by_tier[r.get("tier", "?")].append(r)
    lines = ["  (win rate is MATCHED-ONLY — see orphan-exit distribution below for selection skew)"]
    for tier, rs in sorted(by_tier.items()):
        rmults = [r["realized_R"] for r in rs if isinstance(r.get("realized_R"), (int, float))]
        pnls = [r["realized_pnl"] for r in rs if isinstance(r.get("realized_pnl"), (int, float))]
        wins = sum(1 for p in pnls if p > 0)
        avg_r = round(sum(rmults) / len(rmults), 3) if rmults else None
        tot_pnl = round(sum(pnls), 2) if pnls else 0.0
        wr = f"{wins}/{len(pnls)} ({round(100 * wins / len(pnls))}%)" if pnls else "n/a"
        lines.append(f"  {tier:9s}  N={len(rs):3d}  win={wr:14s}  avgR={avg_r}  totP&L=${tot_pnl}")
    return "\n".join(lines) if lines else "  (no closed trades)"


def _mae_null_audit(records: list[dict]) -> str:
    """Selection-bias guard for the swing-tier MAE/MFE bar reconstruction (board 3/3, 2026-09-21).
    A null mae_R comes from a bar-fetch MISS (halt / delist / data outage), and those cluster on
    exactly the fat-tail ADVERSE trades (LdP) — so silently dropping nulls from calibration would
    truncate the adverse tail and re-introduce a too-tight-stop bias through the back door. Report
    the null set's realized_R vs the populated set so any such skew is auditable, never assumed away."""
    intr = [r for r in records if r.get("tier") == "intraday"]

    def _rstats(rows: list[dict]) -> str:
        rs = [r["realized_R"] for r in rows if isinstance(r.get("realized_R"), (int, float))]
        if not rs:
            return "n=0"
        wins = sum(1 for v in rs if v > 0)
        return f"n={len(rs)} avgR={round(sum(rs) / len(rs), 3)} win={wins}/{len(rs)}"

    null_set = [r for r in intr if r.get("mae_R") is None]
    pop_set = [r for r in intr if r.get("mae_R") is not None]
    return (
        f"  swing MAE/MFE: populated={len(pop_set)} null={len(null_set)}\n"
        f"    populated realized_R: {_rstats(pop_set)}\n"
        f"    null      realized_R: {_rstats(null_set)}  "
        f"(if the null set skews to losses, a halt/delist fetch-miss is truncating the adverse "
        f"tail — widen the fetch before trusting stop calibration)"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reduce trade lifecycle logs to one row per closed trade.")
    ap.add_argument("--events", default=str(_INTRADAY_EVENTS), help="intraday trade_events.jsonl")
    ap.add_argument("--day-tier", default=str(_DAYTIER_EVENTS), help="day_tier_events.jsonl")
    ap.add_argument("--out", default=str(_OUT), help="output trade_records.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="print summary, do not write")
    ap.add_argument("--no-bars", action="store_true",
                    help="skip the swing-tier MAE/MFE 1-min-bar reconstruction (no network; "
                         "mae_R/mfe_R come out null). Use for a fast offline structural run.")
    args = ap.parse_args(argv)

    intr_rows, intr_skipped = _read_jsonl(Path(args.events))
    day_rows, day_skipped = _read_jsonl(Path(args.day_tier))

    # main() opts INTO the real 1-min-bar reconstruction (the library default is a null stub so the
    # function stays pure/network-free for tests). --no-bars keeps it null for a fast offline run.
    # Fail-safe either way — a miss is honest-null MAE/MFE, never a dropped row.
    _mae_fn = (lambda *a, **k: (None, None)) if args.no_bars else _reconstruct_mae_mfe
    intr_recs, intr_stats = reduce_intraday(intr_rows, mae_mfe_fn=_mae_fn)
    day_recs, day_stats = reduce_day_tier(day_rows)
    records = sorted(intr_recs + day_recs, key=lambda r: r.get("ts_entry") or "")

    print("=== trade_record_reducer ===")
    print(f"intraday events: {len(intr_rows)} (skipped torn: {intr_skipped}) -> {intr_stats}")
    print(f"day-tier events: {len(day_rows)} (skipped torn: {day_skipped}) -> {day_stats}")
    print(f"closed-trade records: {len(records)}")
    print(_summary(records))
    # Orphan-exit selection skew (Finding 3): matched-only win rate is conditional; surface the
    # excluded mass so a skew (e.g. an entry-logging outage during a losing streak) is auditable.
    ow, ol = intr_stats.get("orphan_wins", 0), intr_stats.get("orphan_losses", 0)
    print(f"  intraday orphan exits excluded: {intr_stats.get('orphan_exits', 0)} "
          f"(wins={ow} losses={ol} pnl_sum=${round(intr_stats.get('orphan_pnl_sum', 0.0), 2)}) "
          f"| daytrade rows skipped from shared file: {intr_stats.get('daytrade_skipped', 0)}")
    # Swing-tier MAE/MFE bar-reconstruction coverage + null selection-bias audit (Piece 1c).
    print(_mae_null_audit(records))

    if not args.dry_run:
        _atomic_write_jsonl(Path(args.out), records)
        print(f"wrote {len(records)} records -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

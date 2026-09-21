#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale/docstring + aligned-dict rows run long (matches research/ic_engine.py convention)
"""
research/trade_record_reducer.py — replay lifecycle event logs -> one row per CLOSED trade.
Edge-discovery Step 1 (design: logs/design_records/edge_discovery_2026-09-19.md, BGGN 5/5).

WHY: research/ic_engine.py, research/deflated_sharpe.py and research/regime_empirical.py are built
and starved — they need one labeled row PER CLOSED TRADE, tier-agnostic, with confidence +
component decomposition + realized R-multiple + (where available) MAE/MFE. The tiers log
ASYMMETRICALLY today, so nothing is measurable across tiers. This reducer normalizes them.

Reads (READ-ONLY, offline — no execution imports, writes logs/ only, RTH block removed):
  - logs/trade_events.jsonl     (INTRADAY tier; NO trade_id yet -> joined by symbol + FIFO time order)
  - logs/day_tier_events.jsonl  (DAY tier; trade_id-keyed; price_samples -> MAE/MFE; decision-joined
                                 via entry_fill.decision_id == decision.decision_id)
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
from pathlib import Path

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


# ── INTRADAY: symbol + FIFO time-order join (no trade_id in the current schema) ────────────────

def reduce_intraday(rows: list[dict]) -> tuple[list[dict], dict]:
    """Join intraday entry -> exit/stop_hit per symbol in FIFO time order. partial_exit accumulates
    onto the oldest open lot; exit_pnl_correction rewrites the matching closed row's P&L (never mask
    a loss — the correction IS the true fill). Returns (records, stats)."""
    open_lots: dict[str, list[dict]] = defaultdict(list)   # symbol -> FIFO list of open entry dicts
    last_closed: dict[str, dict] = {}                       # symbol -> most-recent emitted closed row
    records: list[dict] = []
    stats = {"entries": 0, "closed": 0, "orphan_exits": 0, "partials": 0, "orphan_partials": 0,
             "corrections": 0, "correction_unmatched": 0, "daytrade_skipped": 0,
             "orphan_wins": 0, "orphan_losses": 0, "orphan_pnl_sum": 0.0}

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
                "indicators":  {k: ev[k] for k in (
                    "tsmom_12m", "tsmom_6m", "tsmom_ewma_vol", "tsmom_vol_mult",
                    "tsmom_direction", "score_16pt") if k in ev},
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
                weights_version=_INTRADAY_WEIGHTS_VERSION, model_version="score_raw",
                ts_entry=lot["ts_entry"],
            )
            exit_rec = make_exit_record(
                trade_id=entry_rec["trade_id"], tier="intraday", symbol=sym,
                side=lot["side"], entry_price=lot["entry_price"], stop_price=lot["stop"],
                exit_price=ev.get("price"), qty=lot["qty"], realized_pnl=realized,
                exit_reason=reason, ts_exit=ev.get("ts"),
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reduce trade lifecycle logs to one row per closed trade.")
    ap.add_argument("--events", default=str(_INTRADAY_EVENTS), help="intraday trade_events.jsonl")
    ap.add_argument("--day-tier", default=str(_DAYTIER_EVENTS), help="day_tier_events.jsonl")
    ap.add_argument("--out", default=str(_OUT), help="output trade_records.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="print summary, do not write")
    args = ap.parse_args(argv)

    intr_rows, intr_skipped = _read_jsonl(Path(args.events))
    day_rows, day_skipped = _read_jsonl(Path(args.day_tier))

    intr_recs, intr_stats = reduce_intraday(intr_rows)
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

    if not args.dry_run:
        _atomic_write_jsonl(Path(args.out), records)
        print(f"wrote {len(records)} records -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# ruff: noqa: E501
"""scripts/loss_postmortem.py — LOSS-PM-1112: a post-mortem for every losing trade, every trading day.

Owner 2026-11-12: "we need the post mortem analysis for every losing trade" and "the loss day tier post mortem ...
needs to be wired to go live". Runs after the close (OCI cron). READ-ONLY on trading state; writes logs/ only.

For each trade CLOSED on the target PT date with realized P&L < 0 (all tiers the trade-record reducer covers), it
reports from the logs and T1 bars:
  - why it entered: tier, track/mode, conviction, the 10/10 tag, entry time (minutes after the 10:00 ET buffer end);
  - the plan: stop distance in daily ATR, target in R;
  - what happened: realized R, best move while held (MFE R), worst move (MAE R), and the best move in the trade's
    direction after the exit, same session (was the stop noise or right?);
  - a one-line verdict per trade (failed at once / noise stop / gave back a winner / stop right).
Plus the running pattern table over all closed trades to date (by tier+mode and by entry minute bucket), so a
candidate fix has its in-sample numbers in front of the replay that must confirm it.

Outputs: logs/loss_postmortem/<date>.json (atomic) and one Slack message (alerts.notify, heads-up), which the nightly
slack-report-actions task turns into a loss-review issue. P&L is read from the logs (sourced from Alpaca fills);
nothing is recomputed or masked. Data: T1 Alpaca bars via data.fetcher only; any bar miss -> that field is null.

Usage: python3 scripts/loss_postmortem.py [--date YYYY-MM-DD] [--no-slack] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
_OUT_DIR = _ROOT / "logs" / "loss_postmortem"
_DAY_EVENTS = _ROOT / "logs" / "day_tier_events.jsonl"
_TRADE_EVENTS = _ROOT / "logs" / "trade_events.jsonl"


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _ts(x):
    try:
        t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=PT)
    except (TypeError, ValueError):
        return None


def _decisions() -> dict:
    """trade_id -> the Day-tier decision that opened it (track, mode, conviction, 10/10)."""
    dec, link = {}, {}
    if not _DAY_EVENTS.exists():
        return {}
    for line in _DAY_EVENTS.open():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "decision":
            dec[e.get("decision_id")] = e.get("decision") or {}
        elif e.get("event") == "entry_fill":
            link[e.get("trade_id")] = e.get("decision_id")
    return {t: dec.get(d, {}) for t, d in link.items()}


def _records() -> list:
    """Closed-trade rows from the existing reducer (research/trade_record_reducer.py), all tiers it covers."""
    from research import trade_record_reducer as r
    intr, _ = r._read_jsonl(_TRADE_EVENTS)
    day, _ = r._read_jsonl(_DAY_EVENTS)
    a, _ = r.reduce_intraday(intr, mae_mfe_fn=lambda *x, **k: (None, None))   # bars only for today's losers
    b, _ = r.reduce_day_tier(day)
    return a + b


def _daily_atr(symbol: str, before: datetime):
    """14-day ATR from T1 daily bars ending before the entry day, or None."""
    try:
        from data.fetcher import fetch_bars_window
        import config
        d = fetch_bars_window(symbol, config.TF_DAILY, before - timedelta(days=40), before)
        if d is None or len(d) < 15:
            return None
        h, lo, c = d["high"].astype(float), d["low"].astype(float), d["close"].astype(float)
        tr = [max(h.iloc[i] - lo.iloc[i], abs(h.iloc[i] - c.iloc[i - 1]), abs(lo.iloc[i] - c.iloc[i - 1]))
              for i in range(1, len(d))]
        atr = statistics.fmean(tr[-14:])
        return atr if atr == atr and atr > 0 else None
    except Exception as exc:  # noqa: BLE001 — a bar miss is an honest null
        print(f"daily ATR unavailable for {symbol}: {exc}", file=sys.stderr)
        return None


def _after_exit_best(symbol: str, side: str, exit_px: float, ts_exit: datetime):
    """Best move in the trade's direction after the exit, to the 16:00 ET close, in dollars per share, or None."""
    try:
        from data.fetcher import fetch_bars_window
        close = ts_exit.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
        if ts_exit >= close:
            return None
        d = fetch_bars_window(symbol, "1Min", ts_exit.astimezone(timezone.utc), close.astimezone(timezone.utc),
                              feed="iex")
        if d is None or len(d) == 0:
            return None
        best = float(d["high"].max()) - exit_px if side == "long" else exit_px - float(d["low"].min())
        return max(best, 0.0)
    except Exception as exc:  # noqa: BLE001
        print(f"after-exit bars unavailable for {symbol}: {exc}", file=sys.stderr)
        return None


def _minutes_after_buffer(ts_entry: datetime):
    """Minutes after 10:00 ET (the end of the 30-minute opening buffer); negative = before it."""
    e = ts_entry.astimezone(ET)
    return round((e - e.replace(hour=10, minute=0, second=0, microsecond=0)).total_seconds() / 60, 1)


def _verdict(r: dict) -> str:
    mfe, after = r.get("mfe_R"), r.get("after_exit_best_R")
    if mfe is not None and mfe >= 1.0:
        return "gave back a winner: it was up 1R or more before losing"
    if after is not None and after >= 1.0:
        return "noise exit: price came back 1R or more after the exit"
    if mfe is not None and mfe < 0.35 and after is not None and after < 0.5:
        return "failed at once and did not come back: the entry was wrong, the exit was right"
    if mfe is not None and mfe < 0.35:
        return "failed at once (after-exit move unknown or partial)"
    return "no clear single cause; see the numbers"


def _bucket(m):
    if m is None:
        return "unknown"
    if m < 0:
        return "before 10:00 ET"
    if m <= 10:
        return "10:00-10:10 ET"
    if m <= 60:
        return "10:10-11:00 ET"
    return "after 11:00 ET"


def _pattern(records: list, dec: dict) -> dict:
    """Running table over all closed trades: count, total R, mean R, losers' share with MFE < 0.35R."""
    groups: dict = {}
    for r in records:
        R = _f(r.get("realized_R"))
        pnl = _f(r.get("realized_pnl"))
        if R is None or pnl is None:
            continue
        d = dec.get(r.get("trade_id"), {})
        mode = (d.get("mode") or (r.get("indicators") or {}).get("mode") or "?")
        keys = [f"{r.get('tier')}/{d.get('track') or '-'}/{mode}"]
        te = _ts(r.get("ts_entry"))
        keys.append("entry " + _bucket(_minutes_after_buffer(te) if te else None))
        for k in keys:
            g = groups.setdefault(k, {"n": 0, "sum_R": 0.0, "losers": 0, "losers_fast_fail": 0})
            g["n"] += 1
            g["sum_R"] += R
            if pnl < 0:   # net P&L sign (a scaled-out winner's final leg can have R < 0)
                g["losers"] += 1
                mfe = _f(r.get("mfe_R"))
                if mfe is not None and mfe < 0.35:
                    g["losers_fast_fail"] += 1
    for g in groups.values():
        g["mean_R"] = round(g["sum_R"] / g["n"], 3)
        g["sum_R"] = round(g["sum_R"], 2)
    return dict(sorted(groups.items(), key=lambda kv: kv[1]["sum_R"]))


def build(date: str) -> dict:
    dec = _decisions()
    records = _records()
    losers = []
    for r in records:
        te, tx = _ts(r.get("ts_entry")), _ts(r.get("ts_exit"))
        pnl = _f(r.get("realized_pnl"))
        if tx is None or pnl is None or pnl >= 0 or tx.astimezone(PT).strftime("%Y-%m-%d") != date:
            continue
        d = dec.get(r.get("trade_id"), {})
        entry, stop, exit_px = _f(r.get("entry_price")), _f(r.get("stop_price")), _f(r.get("exit_price"))
        risk = abs(entry - stop) if entry is not None and stop is not None else None
        atr = _daily_atr(r["symbol"], te.astimezone(timezone.utc).replace(hour=0, minute=0)) if te else None
        after = (_after_exit_best(r["symbol"], r.get("side"), exit_px, tx)
                 if exit_px is not None and r.get("side") in ("long", "short") else None)
        tgt = _f(r.get("target_price"))
        mfe_R, mae_R = _f(r.get("mfe_R")), _f(r.get("mae_R"))
        if risk and entry is not None and exit_px is not None and te and tx:
            # logged samples are sparse (the Day tier samples every 30 min): rebuild from 1-min bars with the
            # reducer's own helper and keep the wider band of the two
            from research import trade_record_reducer as _red
            lo, hi = _red._reconstruct_mae_mfe(r["symbol"], entry, exit_px, te, tx)
            if lo is not None and hi is not None:
                fav, adv = (hi - entry, entry - lo) if r.get("side") == "long" else (entry - lo, hi - entry)
                b_mfe, b_mae = round(max(fav, 0.0) / risk, 2), round(max(adv, 0.0) / risk, 2)
                mfe_R = b_mfe if mfe_R is None else max(mfe_R, b_mfe)
                mae_R = b_mae if mae_R is None else max(mae_R, b_mae)
        row = {
            "trade_id": r.get("trade_id"), "tier": r.get("tier"), "symbol": r.get("symbol"), "side": r.get("side"),
            "track": d.get("track"), "mode": d.get("mode") or (r.get("indicators") or {}).get("mode"),
            "conviction": d.get("conviction", r.get("confidence")), "ten_of_ten": d.get("ten_of_ten"),
            "reason": (d.get("reason") or "")[:240],
            "entry_pt": te.astimezone(PT).strftime("%H:%M") if te else None,
            "exit_pt": tx.astimezone(PT).strftime("%H:%M"),
            "minutes_after_buffer": _minutes_after_buffer(te) if te else None,
            "entry": entry, "stop": stop, "target": tgt, "exit": exit_px, "qty": r.get("qty"),
            "exit_reason": r.get("exit_reason"), "realized_pnl": round(pnl, 2), "realized_R": _f(r.get("realized_R")),
            "stop_in_daily_atr": round(risk / atr, 2) if risk and atr else None,
            "target_R": round(abs(tgt - entry) / risk, 2) if tgt is not None and risk else None,
            "mfe_R": mfe_R, "mae_R": mae_R,
            "after_exit_best_R": round(after / risk, 2) if after is not None and risk else None,
        }
        row["verdict"] = _verdict(row)
        losers.append(row)
    return {"date": date, "generated_at": datetime.now(PT).isoformat(), "losing_trades": losers,
            "total_loss": round(sum(x["realized_pnl"] for x in losers), 2),
            "pattern_all_closed_trades": _pattern(records, dec),
            "change": "loss-pm-1112"}


_CAUSE = {   # verdict -> (plain cause, what it means for the fix review)
    "wrong_entry": ("Wrong entry: it never got going", "the entry was the mistake; the stop limited the loss"),
    "gave_back": ("Gave back a winner", "it was up more than its risk first; a profit lock would have saved it"),
    "noise_exit": ("Stopped out, then came back", "the stop was too close for this stock's normal moves"),
    "unclear": ("No single clear cause", "see the detail line"),
}
_MODE_WORDS = {"PULLBACK": "pullback buy", "DRIVE": "momentum buy", "FADE": "fade trade", "RIDE": "breakout ride"}


def _cause(row: dict) -> str:
    v = row.get("verdict") or ""
    if v.startswith("gave back"):
        return "gave_back"
    if v.startswith("noise"):
        return "noise_exit"
    if v.startswith("failed at once and did not come back"):
        return "wrong_entry"
    return "unclear"


def _x(v) -> str:
    return "?" if v is None else f"{v:+.2f}x"


def _t12(hhmm) -> str:
    try:
        return datetime.strptime(str(hhmm), "%H:%M").strftime("%-I:%M %p")
    except ValueError:
        return str(hhmm)


def _blocks(rep: dict) -> tuple[str, list]:
    """LOSS-PM-1112 / SLK16 (owner 2026-11-12: "readable and actionable for a CEO"): bottom line first, plain words,
    then one short card per trade (rules/slack_format.md SLK01-SLK16). Returns (notification text, Block Kit blocks)."""
    trades = sorted(rep["losing_trades"], key=lambda t: t["realized_pnl"])   # biggest loss first (SLK13)
    n, total = len(trades), rep["total_loss"]
    day = datetime.strptime(rep["date"], "%Y-%m-%d").strftime("%a %b %-d")
    counts: dict = {}
    for t in trades:
        counts[_cause(t)] = counts.get(_cause(t), 0) + 1
    parts = []
    for key, words in (("wrong_entry", "wrong entr{}"), ("gave_back", "winner{} given back"),
                       ("noise_exit", "stop{} hit then recovered"), ("unclear", "unclear")):
        k = counts.get(key, 0)
        if k:
            parts.append(f"{k} " + words.format(("ies" if k > 1 else "y") if key == "wrong_entry" else
                                                ("s" if k > 1 and key != "unclear" else "")))
    saved = -sum(t["realized_pnl"] for t in trades if _cause(t) == "gave_back")
    bluf = f"*{n} losing trade{'s' if n != 1 else ''} today: ${-total:.2f} lost.* " + ", ".join(parts) + "."
    means = []
    if counts.get("gave_back"):
        means.append(f"• A break-even profit lock could have saved up to about *${saved:.2f}* (being tested on past trades).")
    if counts.get("wrong_entry"):
        means.append("• Wrong entries: the stop did its job; the entry rule is under review.")
    if counts.get("noise_exit"):
        means.append("• Stops hit then recovered: stop distance for those stocks is under review.")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Loss review · {day}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": bluf}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(means) if means else "• See the trades below."}},
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": "*Your action:* none. Every loss is filed for a fix review; no change "
                                             "goes live without your approval."}},
        {"type": "divider"},
    ]
    for t in trades:
        cause = _CAUSE[_cause(t)][0]
        style = _MODE_WORDS.get(str(t.get("mode") or "").upper(), "trade")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"🔴 *{t['symbol']}*  *−${-t['realized_pnl']:.2f}*\n{cause}\n"
                       f"{str(t.get('side') or 'trade').capitalize()} {_t12(t.get('entry_pt'))} → out {_t12(t.get('exit_pt'))} PT"}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"{(t.get('tier') or '').replace('daytrade', 'Day tier')} · {style} · "
                       f"best {_x(t.get('mfe_R'))} risk · after exit {_x(t.get('after_exit_best_R'))}"}]})
    weak = [(k, v) for k, v in rep["pattern_all_closed_trades"].items()
            if k.startswith("entry ") and v["n"] >= 5 and v["sum_R"] < 0]
    foot = []
    if weak:
        k, v = weak[0]
        foot.append(f"Weakest entry time so far: {k[6:]}, {v['n']} trades, {v['sum_R']:+.1f}x risk")
    foot.append(f"Source: Alpaca fills + 1-min bars · {datetime.now(PT).strftime('%-I:%M %p')} PT")
    blocks += [{"type": "divider"}, {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(foot)}]}]
    text = f"Loss review {day}: {n} losing trade{'s' if n != 1 else ''}, ${-total:.2f} lost · " + ", ".join(parts)
    return text, blocks


def _slack_text(rep: dict) -> tuple[str, str]:
    n = len(rep["losing_trades"])
    head = f"Loss post-mortem {rep['date']}: {n} losing trade(s), ${rep['total_loss']:+.2f}"
    lines = []
    for t in rep["losing_trades"]:
        lines.append(
            f"- {t['symbol']} {t['tier']} {t.get('mode') or ''} {t['side']} {t['entry_pt']} PT: "
            f"${t['realized_pnl']:+.2f} ({t['realized_R']}R). Stop {t['stop_in_daily_atr']}x daily range; "
            f"best while held {t['mfe_R']}R; after the exit {t['after_exit_best_R']}R. {t['verdict']}.")
    worst = list(rep["pattern_all_closed_trades"].items())[:3]
    if worst:
        lines.append("Weakest groups so far: " + "; ".join(
            f"{k}: {v['n']} trades, {v['sum_R']}R" for k, v in worst) + ".")
    return head, "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--date", default=datetime.now(PT).strftime("%Y-%m-%d"))
    ap.add_argument("--no-slack", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing, send nothing")
    a = ap.parse_args(argv)
    rep = build(a.date)
    head, body = _slack_text(rep)
    print(head)
    print(body)
    if a.dry_run:
        if rep["losing_trades"]:
            print(json.dumps(_blocks(rep), indent=1))
        return 0
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"{a.date}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, indent=2, default=str))
    os.replace(tmp, out)
    if rep["losing_trades"] and not a.no_slack:
        from alerts import send_slack_blocks
        text, blocks = _blocks(rep)
        send_slack_blocks(blocks, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

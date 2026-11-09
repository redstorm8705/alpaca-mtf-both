#!/usr/bin/env python3
# ruff: noqa: E501
"""
scripts/morning_watch.py — MORNING-1029 (owner-approved 2026-11-08): the trigger watcher behind the 06:46 PT watchlist.

Modes:
  (default)              06:47 PT -> the session close minus the broker's same-day cut-off (15:30 ET on a 16:00 close,
                         from the Alpaca calendar, so early-close days end earlier). Watches every side of the 06:46 plan
                         (logs/morning_rundown_plan_<date>.json) and sends ONE TRIGGERED alert per symbol and side.
  --check-heartbeat      cron-run alarm: the watcher's last poll is older than the poll timeout during the watch window
                         -> one Slack alarm per day (v9 item 6, "no silent watcher death").
  --weekly-performance   weekend job after the weekly post-mortem: "0DTE recommendation performance" (v8) from
                         logs/0dte_recommendations.jsonl and the per-minute option-quote archive.

Spec §3b (v6/v7/v9.2/v9.3):
  * Gap-safe, chronological processing: per symbol `last_processed_bar_ts` is persisted; every poll and every startup
    fetches ALL completed IEX one-minute bars after it and processes them oldest first. A missing minute VOIDs every
    still-open side of that symbol for the day (one-line Slack warning, once).
  * Per-bar order for a side not yet triggered: (1) the bar touches the invalidation -> VOID (wins even if it also closes
    beyond the trigger); (2) the bar CLOSES beyond the trigger -> TRIGGERED at that close, target not evaluated on that bar;
    (3) otherwise the bar touches the target -> STALE. Touches use the venue buffers: an up-touch of L is IEX high >=
    L - b_high, a down-touch is IEX low <= L + b_low.
  * At the break the idea is rebuilt from scratch: fresh quotes for every candidate strike polled at that moment, the $1.25
    cap on the fresh ask, the v9.2 rank, and the v9 model stop. A failed / empty quote fetch is retried until the break bar's
    age reaches t_max; the backfill pass re-runs after every retry wait and immediately before an ACTIONABLE send.
  * ACTIONABLE requires $0.05 < model stop < the latest fresh bid (v9.3; re-checked right before the send).
  * After TRIGGERED the first post-trigger bar touching the invalidation or the target sets the ONE terminal outcome
    (STOP | TARGET; both on one bar -> STOP) and sends only that alert (v9.2).
  * State: logs/morning_watch_state_<date>.json (atomic), so a restart never re-alerts. Singleton: fcntl.flock on a file
    in /tmp (released by the kernel when the process dies). The pass, the state write and the send decision run in one
    process under that lock.

SAFETY: a REPORT. It places NO orders, imports nothing from execution/, writes no bot state. T1 data only (via
scripts/morning_rundown.py -> data/fetcher.py, data/gex.py). Every time shown to the owner is PT.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import morning_rundown as mr               # noqa: E402  (shared T1 access, selection, logging)
from alerts import send_slack, send_slack_blocks, send_slack_delivery        # noqa: E402

import pandas as pd                                     # noqa: E402

logger = logging.getLogger("morning_watch")

PT, ET, UTC = mr.PT, mr.ET, mr.UTC
ONE_MINUTE = mr.ONE_MINUTE
BROKER_ZERO_DTE_CUTOFF = timedelta(minutes=30)   # broker rule (spec §7): Alpaca stops new opening orders on same-day
#                                                  contracts 30 min before the close (15:30 ET on a 16:00 close)
WATCH_POLL_SLEEP_SECONDS = 15.0                  # plumbing: spacing between watcher polls (correctness does not depend on it)
WATCH_POLL_TIMEOUT_SECONDS = 180.0               # plumbing: heartbeat alarm when the last poll is older than this
QUOTE_RETRY_SLEEP_SECONDS = 10.0                 # plumbing: spacing of quote retries at a break (budget = t_max)
PLAN_WAIT_SLEEP_SECONDS = 15.0                   # plumbing: spacing while waiting for the 06:46 plan file

LOCK_PATH = Path("/tmp/morning_watch.lock")
_LOGS = mr._LOGS
OPEN_STATUSES = ("WATCHING", "PENDING")


def state_path(day: date) -> Path:
    return _LOGS / f"morning_watch_state_{day.isoformat()}.json"


def quotes_archive_path(day: date) -> Path:
    return _LOGS / f"morning_watch_quotes_{day.isoformat()}.jsonl"


EVENTS = _LOGS / "morning_watch_events.jsonl"


# ══ per-bar rule (one rule shared with the phase-2 grader, spec §3b / §8) ═════════════════════════════════════════

def touches_up(bar: dict, lvl: float, b_high: float) -> bool:
    return float(bar["high"]) >= lvl - b_high


def touches_down(bar: dict, lvl: float, b_low: float) -> bool:
    return float(bar["low"]) <= lvl + b_low


def touches(side: str, which: str, bar: dict, lvl: float, stats: dict) -> bool:
    """CALL: target is above (up-touch), invalidation below (down-touch). PUT mirrors."""
    up = (side == "CALL") == (which == "target")
    return touches_up(bar, lvl, stats["b_high"]) if up else touches_down(bar, lvl, stats["b_low"])


def closes_beyond(side: str, bar: dict, trigger: float) -> bool:
    return float(bar["close"]) > trigger if side == "CALL" else float(bar["close"]) < trigger


def step(s: dict, bar: dict, stats: dict) -> str | None:
    """Apply ONE completed bar to one side. Mutates `s`; returns an event name or None."""
    side, ts = s["side"], bar["ts"]
    if s["status"] == "WATCHING":
        if touches(side, "invalidation", bar, s["invalidation"], stats):
            s.update(status="VOID", void_reason="invalidation printed before the trigger", void_ts_utc=ts)
            return "void"
        if closes_beyond(side, bar, s["trigger"]):
            s.update(status="PENDING", break_bar_ts=ts, break_close=float(bar["close"]))
            return "break"
        if touches(side, "target", bar, s["target"], stats):
            s.update(status="STALE", stale_reason="target printed before a close beyond the trigger", stale_ts_utc=ts)
            return "stale"
        return None
    if s["status"] == "PENDING":
        for which in ("invalidation", "target"):
            if touches(side, which, bar, s[which], stats):
                s.update(status="STALE", stale_reason=f"{which} printed after the break, before the alert",
                         stale_ts_utc=ts, stale_after_break=True)
                return "stale_after_break"
        return None
    if s["status"] == "TRIGGERED" and s.get("outcome") is None and str(ts) > str(s.get("break_bar_ts") or ""):
        inv = touches(side, "invalidation", bar, s["invalidation"], stats)
        tgt = touches(side, "target", bar, s["target"], stats)
        if inv or tgt:
            s.update(outcome="STOP" if inv else "TARGET", outcome_ts_utc=ts)   # both on one bar -> STOP (conservative)
            return "outcome"
    return None


# ══ the watcher ════════════════════════════════════════════════════════════════════════════════════════════════════

class Watcher:
    def __init__(self, plan: dict, state: dict, day: date, watch_end: datetime,
                 now_fn: Callable[[], datetime] = mr.utcnow, sleep_fn: Callable[[float], None] = time.sleep,
                 dry_run: bool = False):
        self.plan, self.state, self.day, self.watch_end = plan, state, day, watch_end
        self.now_fn, self.sleep_fn, self.dry_run = now_fn, sleep_fn, dry_run
        self.session_close = mr.parse_ts(plan["session_close"]) or watch_end
        self.sent: list[str] = []

    # ── persistence / delivery ────────────────────────────────────────────────────────────────────────────────────
    def save(self) -> None:
        mr.atomic_write_text(state_path(self.day), json.dumps(self.state, indent=1, default=str))

    def send(self, text: str) -> bool:
        self.sent.append(text)
        if self.dry_run:
            logger.info("morning_watch: dry run — not sent:\n%s", text)
            return True
        try:
            ok = bool(send_slack_blocks([{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}],
                                        text.split("\n")[0]))
        except Exception as e:
            logger.warning("morning_watch: Slack send raised (%s)", e)
            ok = False
        logger.info("morning_watch: Slack %s: %s", "sent" if ok else "FAILED", text.split("\n")[0])
        return ok

    def event(self, row: dict) -> None:
        try:
            mr.locked_append_jsonl(EVENTS, [{**mr.stamp(self.now_fn()), **mr._jsonable(row)}])
        except Exception as e:
            logger.error("morning_watch: event log write failed (%s)", e)

    def stats(self, sym: str) -> dict:
        return self.plan["symbols"][sym]["stats"]

    # ── backfill: every completed bar after last_processed_bar_ts, oldest first, with a coverage check ─────────────
    def settle_minutes(self, sym: str) -> float | None:
        """How long a trailing unpublished minute may stay unpublished before it counts as a hole: the side's own
        freshness bound t_max (a break older than t_max is unusable anyway). None -> trailing minutes never VOID."""
        return self.stats(sym).get("t_max_min")

    def backfill(self, sym: str) -> bool:
        """Process every published, completed bar after last_processed_bar_ts, oldest first. Returns True when caught
        up through the newest completed minute. Coverage (T2): an EMPTY / failed fetch is a retry (nothing advances);
        a missing minute followed by a later published bar is a hole -> VOID; a TRAILING missing minute is waited for
        (never processed past) and only becomes a hole once older than the settle delay (t_max)."""
        st = self.state["symbols"][sym]
        self._resend_triggers(sym)
        self._resend_outcomes(sym)
        open_sides = [s for s in st["sides"].values() if s["status"] in OPEN_STATUSES
                      or (s["status"] == "TRIGGERED" and s.get("outcome") is None)]
        if not open_sides:
            return True
        now = self.now_fn()
        last = mr.parse_ts(st["last_processed_bar_ts"])
        newest_done = mr.floor_minute(now) - ONE_MINUTE
        if last is None or newest_done <= last:
            return True
        try:
            bars = mr.completed(mr.get_bars(sym, last + ONE_MINUTE, now, "iex"), now)
        except Exception as e:
            logger.warning("morning_watch: %s bar fetch failed (%s) — retry next poll", sym, e)
            return False
        bars = bars[(bars.index > pd.Timestamp(last)) & (bars.index <= pd.Timestamp(newest_done))]
        miss = mr.missing_minutes(bars, last, newest_done)
        first_gap = miss[0] if miss else None
        settle = self.settle_minutes(sym)
        if len(bars) == 0:
            # an empty / failed fetch is a retry; it becomes a hole only once the first expected minute is older than
            # the settle delay (t_max) — by then any break inside it would be unusable anyway
            empty_age_seconds = (now - (last + ONE_MINUTE + ONE_MINUTE)).total_seconds()
            if settle is not None and empty_age_seconds / 60 > settle:
                return self._void_incomplete(sym, open_sides, miss, now, False, settle)
            logger.info("morning_watch: %s empty bar fetch after %s — retry next poll (not a hole)", sym,
                        mr.iso_z(last))
            return False
        if first_gap is not None:
            bounded = bool((bars.index > pd.Timestamp(first_gap)).any())
            gap_age_seconds = (now - (first_gap + ONE_MINUTE)).total_seconds()
            aged = settle is not None and gap_age_seconds / 60 > settle
            if bounded or aged:
                return self._void_incomplete(sym, open_sides, miss, now, bounded, settle)
            logger.info("morning_watch: %s minute %s not published yet (age %.0fs, settle %s min) — waiting", sym,
                        mr.iso_z(first_gap), gap_age_seconds, "n/a" if settle is None else round(settle, 1))
            bars = bars[bars.index < pd.Timestamp(first_gap)]     # the contiguous prefix only (none past the gap)
        stats = self.stats(sym)
        events: list[tuple[dict, str, dict]] = []
        for ts, row in bars.iterrows():
            bar = {"ts": mr.iso_z(ts.to_pydatetime()), "open": float(row["open"]), "high": float(row["high"]),
                   "low": float(row["low"]), "close": float(row["close"])}
            for sd in st["sides"].values():
                ev = step(sd, bar, stats)
                if ev:
                    if ev == "outcome":
                        # persisted in the same save as the outcome, so a crash before send_outcome() still leaves
                        # _resend_outcomes() the bar it needs (Codex pre-ship 11-09)
                        sd["outcome_bar"] = bar
                    events.append((sd, ev, bar))
                    self.event({"event": ev, "symbol": sym, "side": sd["side"], "bar": bar, "status": sd["status"]})
            st["last_processed_bar_ts"] = bar["ts"]
        self.save()
        for sd, ev, bar in events:
            if ev == "outcome" and sd.get("alert_state") == "ACTIONABLE":
                self.send_outcome(sym, sd, bar)
        return first_gap is None

    def _void_incomplete(self, sym: str, open_sides: list, miss: list, now: datetime, bounded: bool,
                         settle: float | None) -> bool:
        st = self.state["symbols"][sym]
        why = "hole bounded by a later published bar" if bounded else f"unpublished for more than {settle:.1f} min (t_max)"
        for sd in open_sides:
            sd.update(status="VOID", void_reason="watch history incomplete", void_detail=why, void_ts_utc=mr.iso_z(now),
                      missing_minutes=[mr.iso_z(m) for m in miss])
        logger.warning("morning_watch: %s watch history incomplete (%s) — missing %s", sym, why, [mr.iso_z(m) for m in miss])
        self.event({"event": "void_incomplete", "symbol": sym, "why": why, "missing": [mr.iso_z(m) for m in miss]})
        st["last_processed_bar_ts"] = mr.iso_z(mr.floor_minute(now) - ONE_MINUTE)
        self.save()
        if not st.get("void_warned"):
            st["void_warned"] = True
            self.save()
            self.send(f"⚠️ {sym}: watch history incomplete ({len(miss)} IEX minute(s) missing from "
                      f"{mr.fmt_pt(miss[0])}; {why}) — open {sym} sides VOID for today, no further alerts.")
        return False

    def _resend_outcomes(self, sym: str) -> None:
        """At-least-once exit alerts: an outcome set but not confirmed delivered (crash or Slack failure) is re-sent;
        the key rec_id + outcome dedupes it once delivery is confirmed."""
        for sd in self.state["symbols"][sym]["sides"].values():
            if sd.get("outcome") and sd.get("alert_state") == "ACTIONABLE" and not sd.get("outcome_alerted") \
                    and sd.get("outcome_bar"):
                self.send_outcome(sym, sd, sd["outcome_bar"])

    def send_outcome(self, sym: str, s: dict, bar: dict) -> None:
        key = f"{s['rec_id']}:{s['outcome']}"
        delivered = self.state.setdefault("delivered_keys", [])
        if s.get("outcome_alerted") or key in delivered:
            s["outcome_alerted"] = True
            return
        s["outcome_bar"] = bar
        self.save()
        bar_pt = mr.fmt_pt(mr.parse_ts(bar["ts"]))
        if s["outcome"] == "STOP":
            txt = (f"🛑 {sym} {s['side']} — invalidation printed — exit · 1-min bar {bar_pt} reached "
                   f"{s['invalidation']:.2f} (bar ts_utc {bar['ts']})")
        else:
            txt = (f"🎯 {sym} {s['side']} — target printed · 1-min bar {bar_pt} reached {s['target']:.2f} "
                   f"(bar ts_utc {bar['ts']})")
        if not self.send(txt):
            logger.warning("morning_watch: %s %s outcome alert not delivered — will re-send", sym, s["side"])
            return
        s["outcome_alerted"] = True
        delivered.append(key)
        self.save()
        self.event({"event": "outcome_alert", "symbol": sym, "side": s["side"], "outcome": s["outcome"], "bar": bar})
        self.recommendation({"event": "outcome", "rec_id": s["rec_id"], "symbol": sym, "side": s["side"],
                             "outcome": s["outcome"], "outcome_bar_ts_utc": bar["ts"]})

    def recommendation(self, row: dict) -> None:
        try:
            mr.locked_append_jsonl(mr.STATE["recommendations"], [{**mr.stamp(self.now_fn()), "date": self.day.isoformat(),
                                                                  **mr._jsonable(row)}])
        except Exception as e:
            logger.error("morning_watch: recommendation log write failed (%s)", e)

    # ── the break ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _break_age_seconds(self, s: dict) -> float:
        """Age of the break bar's CLOSE (bar start + 1 minute)."""
        end = (mr.parse_ts(s["break_bar_ts"]) or self.now_fn()) + ONE_MINUTE
        return (self.now_fn() - end).total_seconds()

    def _break_age_min(self, s: dict) -> float:
        age_seconds = self._break_age_seconds(s)
        return age_seconds / 60

    def _invalidated(self, s: dict) -> bool:
        return s["status"] != "PENDING"

    def handle_break(self, sym: str, s: dict) -> None:
        stats = self.stats(sym)
        t_max = stats.get("t_max_min")
        if t_max is None or self._break_age_min(s) >= t_max:
            return self._stale(sym, s, f"break bar too old (age {self._break_age_min(s):.1f} min >= t_max "
                                        f"{'n/a' if t_max is None else round(t_max, 1)} min)")
        chain = {c["occ"]: {"strike": float(c["strike"]), "is_call": bool(c["is_call"])} for c in s["candidates"]}
        poll: dict = {}
        while True:
            poll = mr.poll_quotes(list(chain), now_fn=self.now_fn, sleep_fn=self.sleep_fn)
            usable = any(q is not None and q.get("current") and mr.two_sided(q)[0] for q in poll["quotes"].values())
            if usable:
                break
            if self._break_age_min(s) >= t_max:
                return self._triggered(sym, s, mr.NO_QUOTE, None, None, poll,
                                       f"quote fetch failed or empty until the break bar reached t_max ({t_max:.1f} min)")
            self.sleep_fn(QUOTE_RETRY_SLEEP_SECONDS)
            self.backfill(sym)                     # v7: re-validate after every retry wait
            self.heartbeat_poll()
            if self._invalidated(s):
                return self._after_invalid(sym, s)
        setup = {"side": s["side"], "trigger": {"price": s["trigger"]}, "target": {"price": s["target"]},
                 "invalidation": {"price": s["invalidation"]}}
        sel = mr.select_strike(chain, setup, s["break_close"], poll["window_end"], self.session_close, poll)
        pick = sel["pick"]
        if pick is None:
            return self._triggered(sym, s, sel["hint"], sel, None, poll, "fresh rebuild found no eligible strike")
        # v7: the backfill pass immediately before an ACTIONABLE send, caught up through the newest completed minute
        while not self.backfill(sym):
            if self._invalidated(s):
                return self._after_invalid(sym, s)
            if self._break_age_min(s) >= t_max:
                return self._stale(sym, s, "watch history not caught up before the break bar reached t_max")
            self.sleep_fn(QUOTE_RETRY_SLEEP_SECONDS)
        if self._invalidated(s):
            return self._after_invalid(sym, s)
        if self._break_age_min(s) >= t_max:
            return self._stale(sym, s, f"break bar reached t_max ({t_max:.1f} min) before the send")
        if self.now_fn() >= self.watch_end:
            return self._stale(sym, s, "past the broker's same-day order cut-off")
        # v9.3 + T1: re-check the bid and the cap on a fresh quote right before the send. Only an UNCHANGED quote keeps
        # the poll quote; a fetch error is retried within t_max; a bad quote is never actionable.
        while True:
            kind, latest = self._latest_quote(pick["occ"], pick["quote"])
            if kind != "error":
                break
            if self._break_age_min(s) >= t_max:
                return self._triggered(sym, s, mr.NO_QUOTE, sel, None, poll,
                                       "pre-send quote re-check failed until the break bar reached t_max")
            self.sleep_fn(QUOTE_RETRY_SLEEP_SECONDS)
            while not self.backfill(sym):
                if self._invalidated(s) or self._break_age_min(s) >= t_max:
                    break
                self.sleep_fn(QUOTE_RETRY_SLEEP_SECONDS)
            if self._invalidated(s):
                return self._after_invalid(sym, s)
        if self._break_age_min(s) >= t_max:
            return self._stale(sym, s, f"break bar reached t_max ({t_max:.1f} min) during the pre-send re-check")
        if self.now_fn() >= self.watch_end:   # the re-check loop can sleep past the cut-off (cold review 11-08)
            return self._stale(sym, s, "past the broker's same-day order cut-off")
        if kind == "zero_bid":
            return self._triggered(sym, s, "REFERENCE ONLY — no placeable stop", sel, None, poll,
                                   "pre-send re-check: zero bid (a sell-stop cannot sit below it)")
        if kind != "ok" or latest is None:
            return self._triggered(sym, s, mr.NO_QUOTE, sel, None, poll, f"pre-send re-check: {kind}")
        pick = {**pick, "bid": latest["bid"], "ask": latest["ask"], "quote": latest}
        sel["pick"] = pick
        if pick["ask"] > mr.PREMIUM_CAP_PER_SHARE:
            sel["pick"] = None
            return self._triggered(sym, s, mr.NO_CONTRACT, sel, None, poll, f"fresh ask {pick['ask']:.2f} > cap")
        T = mr.years_between(self.session_close, self.now_fn())
        stop = mr.model_stop(pick, s["invalidation"], T, pick["bid"])
        stop["stop_check_bid_ts"] = (pick.get("quote") or {}).get("t")
        if not stop["valid"]:
            self.state.setdefault("stop_invalid", 0)
            self.state["stop_invalid"] += 1
            return self._triggered(sym, s, "REFERENCE ONLY — no placeable stop", sel, stop, poll, stop["reason"])
        return self._triggered(sym, s, "ACTIONABLE", sel, stop, poll, "all checks passed")

    def _latest_quote(self, occ: str, poll_q: dict) -> tuple[str, dict | None]:
        """('ok', quote) for a quote at least as new as the poll quote (the poll quote itself when unchanged);
        ('error', None) on a failed fetch / missing snapshot (retry); ('zero_bid' | 'zero ask' | 'locked or crossed' |
        'older than the poll quote', None) otherwise — never actionable."""
        try:
            q = mr.quote_from_snapshot((mr.fetch_snapshots([occ]) or {}).get(occ))
        except Exception as e:
            logger.warning("morning_watch: pre-send quote re-check failed (%s)", e)
            return "error", None
        if q is None:
            return "error", None
        qt, pt = mr.parse_ts(q.get("t")), mr.parse_ts(poll_q.get("t"))
        if qt is None or pt is None or qt < pt:
            return "older than the poll quote", None
        if qt == pt and q["bid"] == poll_q["bid"] and q["ask"] == poll_q["ask"]:
            return "ok", poll_q
        ok, why = mr.two_sided(q)
        if not ok:
            return ("zero_bid" if why == "zero bid" else why), None
        q["current"] = True
        return "ok", q

    def _after_invalid(self, sym: str, s: dict) -> None:
        if s["status"] == "STALE":
            return self._stale(sym, s, s.get("stale_reason") or "target or invalidation printed", already=True)
        return None   # VOID (incomplete history): the one-line warning already went out

    def _stale(self, sym: str, s: dict, reason: str, already: bool = False) -> None:
        if not already:
            s.update(status="STALE", stale_reason=reason, stale_ts_utc=mr.iso_z(self.now_fn()))
        if s.get("alerted"):
            return
        s["alerted"] = True
        s["alert_state"] = "STALE"
        self.save()
        icon = "📈" if s["side"] == "CALL" else "📉"
        text = (f"🔔 {sym} {s['side']} {icon} TRIGGERED — STALE, no entry · {mr.fmt_pt(self.now_fn())}\n"
                f"   1-min bar {mr.fmt_pt(mr.parse_ts(s['break_bar_ts']))} closed {s['break_close']:.2f} vs trigger "
                f"{s['trigger']:.2f} · {reason}")
        self._deliver_trigger(s, text, text)
        self._log_trigger(sym, s, "STALE", None, None, reason)

    def _triggered(self, sym: str, s: dict, alert_state: str, sel: dict | None, stop: dict | None, poll: dict,
                   reason: str) -> None:
        s.update(status="TRIGGERED", alert_state=alert_state, alerted=True,
                 decision_cutoff_bar_ts=self.state["symbols"][sym]["last_processed_bar_ts"])
        pick = (sel or {}).get("pick")
        if pick:
            s["contract"] = pick["occ"]
        text = self.trigger_text(sym, s, alert_state, pick, stop, reason, poll)
        rec = {"side": s["side"], "state": alert_state if alert_state == "ACTIONABLE" else "REFERENCE ONLY",
               "setup": {"complete": True}, "strike": {"pick": pick if alert_state == "ACTIONABLE" else None},
               "stop": stop}
        keys = mr.check_violations(rec, text)
        if keys:
            logger.error("morning_watch: %s %s TRIGGERED alert withheld — violations %s", sym, s["side"], keys)
            s["violations"] = keys
            s["alert_state"] = "REFERENCE ONLY"
            text = (f"🔔 {sym} {s['side']} TRIGGERED — REFERENCE ONLY — alert withheld (check failed: "
                    f"{', '.join(keys)}) · no entry")
            self.state.setdefault("violations", {})
            for k in keys:
                self.state["violations"][k] = self.state["violations"].get(k, 0) + 1
        self.save()
        # A re-send happens later, when the quote check is no longer current, so it never carries ACTIONABLE terms.
        late = text if s["alert_state"] != "ACTIONABLE" else (
            f"🔔 {sym} {s['side']} TRIGGERED at {mr.fmt_pt(mr.parse_ts(s['break_bar_ts']))} — the alert was delayed "
            f"(Slack failed) · its entry quote check is no longer current — no new entry from this alert")
        self._deliver_trigger(s, text, late)
        self._log_trigger(sym, s, s["alert_state"], sel, stop, reason)

    def _deliver_trigger(self, s: dict, text: str, late_text: str) -> None:
        """At-least-once TRIGGERED / STALE alerts (Codex pre-ship 11-08): a failed Slack send stays pending and is
        re-sent (as late_text) on the next poll, instead of being recorded as delivered."""
        s["pending_alert_text"] = late_text   # saved BEFORE the send, so a crash mid-send still leaves it pending
        self.save()
        if self.send(text):
            s.pop("pending_alert_text", None)
            self.save()
        else:
            logger.warning("morning_watch: %s TRIGGERED alert not delivered — will re-send", s["side"])

    def _resend_triggers(self, sym: str) -> None:
        for sd in self.state["symbols"][sym]["sides"].values():
            pending = sd.get("pending_alert_text")
            if pending and self.send(pending):
                sd.pop("pending_alert_text", None)
                self.save()

    def trigger_text(self, sym: str, s: dict, alert_state: str, pick: dict | None, stop: dict | None, reason: str,
                     poll: dict) -> str:
        side, icon = s["side"], ("📈" if s["side"] == "CALL" else "📉")
        rel = ">" if side == "CALL" else "<"
        now = self.now_fn()
        bar_ts = mr.parse_ts(s["break_bar_ts"])
        head = (f"🔔 {sym} {side} {icon} {'✅ ' if alert_state == 'ACTIONABLE' else ''}TRIGGERED — {alert_state} · "
                f"{mr.fmt_pt(now)}\n   1-min bar {mr.fmt_pt(bar_ts)} closed {s['break_close']:.2f} {rel} trigger "
                f"{s['trigger']:.2f} (bar age {self._break_age_seconds(s):.0f}s)")
        if alert_state != "ACTIONABLE" or pick is None or stop is None:
            if alert_state == mr.NO_QUOTE:
                why = (f"no candidate quote updated inside {mr.fmt_pt(poll.get('window_start'), True)}–"
                       f"{mr.fmt_pt(poll.get('window_end'), True)}; {reason}")
            elif alert_state.startswith("REFERENCE ONLY"):
                why = "the model stop is not placeable (at or below the floor, or at/above the bid)"
            else:
                why = reason
            return f"{head}\n   {why} · no entry"
        q = pick["quote"]
        kind = "call" if side == "CALL" else "put"
        lbl = mr._strike_label(pick)
        cap = mr.PREMIUM_CAP_PER_SHARE
        maxloss = pick["ask"] * mr.SHARES_PER_CONTRACT
        worth = mr.intrinsic(pick["is_call"], pick["strike"], s["target"])
        back = "below" if side == "CALL" else "above"
        word = "above" if side == "CALL" else "below"
        hours_left = (self.session_close - now).total_seconds() / 3600
        iv_txt = f"IV {pick['iv']:.1%}" if pick.get("iv") else "IV n/a"
        return (f"{head}\n"
                f"   {sym} closed {word} ${s['trigger']:.2f} → buy the {sym} ${pick['strike']:g} {kind}\n"
                f"   {lbl} · bid {pick['bid']:.2f} / ask {pick['ask']:.2f} (quote {mr.fmt_pt(mr.parse_ts(q.get('t')), True)}, "
                f"venue {q.get('ask_venue') or '?'}) · Limit ${pick['ask']:.2f}, never above ${cap:.2f} · max loss ${maxloss:.0f}\n"
                f"   Target {s['target']:.2f} → worth ≥ ${worth:.2f} there\n"
                f"   Stop: sell-stop {lbl} at ${stop['stop']:.2f} (day, model: Black-Scholes at the invalidation "
                f"{s['invalidation']:.2f}, T {hours_left:.1f}h, {iv_txt}) — place right after the fill\n"
                f"   Exit line: {sym} back {back} {s['invalidation']:.2f}\n"
                f"   If the {lbl} bid is at or below ${stop['stop']:.2f} when you are filled: exit, or re-run the stop on "
                f"the live bid. Confirm the stop is still below the bid when you place it.")

    def _log_trigger(self, sym: str, s: dict, alert_state: str, sel: dict | None, stop: dict | None, reason: str) -> None:
        pick = (sel or {}).get("pick") or {}
        q = pick.get("quote") or {}
        self.event({"event": "triggered_decision", "symbol": sym, "side": s["side"], "alert_state": alert_state,
                    "reason": reason, "side_state": s, "candidates": (sel or {}).get("candidates"),
                    "ranked": [{k: v for k, v in r.items() if k != "quote"} for r in (sel or {}).get("ranked") or []],
                    "pick": {k: v for k, v in pick.items() if k != "quote"} or None, "pick_quote": q or None,
                    "stop": stop, "decision_at": (sel or {}).get("decision_at"), "T_years": (sel or {}).get("T_years"),
                    "decision_cutoff_bar_ts": s.get("decision_cutoff_bar_ts"),
                    "r": mr.gex._RISK_FREE_RATE, "q": mr.gex._DIV_YIELD})
        self.recommendation({
            "event": "triggered", "rec_id": s["rec_id"], "symbol": sym, "side": s["side"], "triggered": True,
            "triggered_at": s.get("break_bar_ts"), "break_close": s.get("break_close"), "alert_state": alert_state,
            "contract": pick.get("occ"), "ask_at_trigger": pick.get("ask"), "limit": pick.get("ask") if alert_state == "ACTIONABLE" else None,
            "stop": (stop or {}).get("stop") if alert_state == "ACTIONABLE" else None, "stop_valid": (stop or {}).get("valid"),
            "stop_check_bid": (stop or {}).get("stop_check_bid"), "stop_check_bid_ts": (stop or {}).get("stop_check_bid_ts"),
            "bid_size_top_of_book": q.get("bid_size"),
            "bid_depth_at_stop": "n/a — the indicative feed carries top of book only",
            "trigger": s["trigger"], "target": s["target"], "out": s["invalidation"],
            "decision_cutoff_bar_ts": s.get("decision_cutoff_bar_ts"), "r": mr.gex._RISK_FREE_RATE, "q": mr.gex._DIV_YIELD})

    # ── loop pieces ────────────────────────────────────────────────────────────────────────────────────────────────
    def process_symbol(self, sym: str) -> None:
        self.backfill(sym)
        while True:
            pend = [s for s in self.state["symbols"][sym]["sides"].values() if s["status"] == "PENDING"]
            if not pend:
                break
            self.handle_break(sym, pend[0])
        for s in self.state["symbols"][sym]["sides"].values():
            if s["status"] == "STALE" and s.get("stale_after_break") and not s.get("alerted"):
                self._stale(sym, s, s.get("stale_reason") or "", already=True)

    def archive_quotes(self) -> None:
        """v8: the option quote of every triggered contract each minute after the trigger (Alpaca has no historical
        option quotes, so the weekly performance job needs this archive)."""
        minute = mr.iso_z(mr.floor_minute(self.now_fn()))
        if self.state.get("last_archive_minute") == minute:
            return
        live = [(sym, s) for sym, st in self.state["symbols"].items() for s in st["sides"].values()
                if s["status"] == "TRIGGERED" and s.get("contract")]
        if not live:
            return
        try:
            snaps = mr.fetch_snapshots(sorted({s["contract"] for _sym, s in live})) or {}
        except Exception as e:
            logger.warning("morning_watch: quote archive fetch failed (%s) — retrying this minute", e)
            return
        if not snaps:
            # nothing usable came back: leave the minute unmarked so the next poll retries it
            logger.warning("morning_watch: quote archive fetch returned no snapshots — retrying this minute")
            return
        rows = []
        for sym, s in live:
            q = mr.quote_from_snapshot(snaps.get(s["contract"]))
            rows.append({**mr.stamp(self.now_fn()), "rec_id": s["rec_id"], "symbol": sym, "side": s["side"],
                         "contract": s["contract"], "quote": q})
        try:
            mr.locked_append_jsonl(quotes_archive_path(self.day), rows)
        except Exception as e:
            logger.warning("morning_watch: quote archive write failed (%s) — retrying this minute", e)
            return
        self.state["last_archive_minute"] = minute    # only after the minute's rows are archived

    def heartbeat_poll(self) -> None:
        now = self.now_fn()
        self.state["last_poll_utc"] = mr.iso_z(now)
        try:
            mr.update_heartbeat({"watcher": {
                "date": self.day.isoformat(), "last_poll_utc": mr.iso_z(now), "last_poll_local": now.astimezone(PT).isoformat(),
                "watch_end_utc": mr.iso_z(self.watch_end),
                "stop_invalid": self.state.get("stop_invalid", 0), "violations": self.state.get("violations", {}),
                "symbols": {sym: {"last_processed_bar_ts": st["last_processed_bar_ts"],
                                  "sides": {side: {"watch_state": s["status"], "alert_state": s.get("alert_state"),
                                                   "outcome": s.get("outcome"), "void_reason": s.get("void_reason"),
                                                   "void_ts_utc": s.get("void_ts_utc")}
                                            for side, s in st["sides"].items()}}
                            for sym, st in self.state["symbols"].items()}}})
        except Exception as e:
            logger.warning("morning_watch: heartbeat write failed (%s)", e)

    def summary(self) -> str:
        lines = [f"🧾 Morning watch summary · {self.day.strftime('%a %b %d')} · ended {mr.fmt_pt(self.now_fn())}"]
        for sym, st in self.state["symbols"].items():
            for side, s in st["sides"].items():
                fs = s["status"]
                if fs == "WATCHING":
                    fs = "WATCHING-expired"
                elif fs == "TRIGGERED":
                    fs = f"TRIGGERED ({s.get('alert_state')})" + (f" → {s['outcome']}" if s.get("outcome") else "")
                elif fs == "VOID":
                    fs = f"VOID ({s.get('void_reason')})"
                elif fs == "STALE":
                    fs = f"STALE ({s.get('stale_reason')})"
                lines.append(f"• {sym} {side}: {fs}")
        return "\n".join(lines)

    def run_loop(self) -> None:
        while self.now_fn() < self.watch_end:
            for sym in self.state["symbols"]:
                try:
                    self.process_symbol(sym)
                except Exception as e:   # per-symbol isolation
                    logger.error("morning_watch: %s poll failed (%s)", sym, e, exc_info=True)
            self.archive_quotes()
            self.heartbeat_poll()
            self.save()
            if self.now_fn() >= self.watch_end:
                break
            self.sleep_fn(WATCH_POLL_SLEEP_SECONDS)
        for st in self.state["symbols"].values():
            for s in st["sides"].values():
                if s["status"] == "PENDING":   # a break still undecided at the cut-off never becomes an entry
                    s.update(status="STALE", stale_reason="past the broker's same-day order cut-off")
        for sym in self.state["symbols"]:   # a TRIGGERED or exit alert that failed in the last poll gets one more try
            try:
                self._resend_triggers(sym)
                self._resend_outcomes(sym)
            except Exception as e:   # per-symbol isolation, as in the loop; the summary below still goes out
                logger.error("morning_watch: %s end-of-watch resend failed (%s)", sym, e, exc_info=True)
        if not self.state.get("summary_sent"):
            self.state["summary_sent"] = True
            self.save()
            text = self.summary()
            self.send(text)
            try:
                mr.update_heartbeat({"watcher": {"end_summary": text, "ended_utc": mr.iso_z(self.now_fn())}})
            except Exception as e:
                logger.warning("morning_watch: heartbeat summary write failed (%s)", e)
        self.save()


def init_state(plan: dict, day: date) -> dict:
    st: dict = {"date": day.isoformat(), "symbols": {}}
    for sym, p in plan["symbols"].items():
        sides = {}
        for side, ps in p["sides"].items():
            sides[side] = {"side": side, "status": "WATCHING" if ps.get("watch") else "NOT_WATCHED",
                           "not_watched_reason": None if ps.get("watch") else f"06:46 state {ps.get('state_0646')}",
                           "trigger": ps.get("trigger"), "target": ps.get("target"), "invalidation": ps.get("invalidation"),
                           "candidates": ps.get("candidates") or [], "rec_id": ps.get("rec_id"), "outcome": None}
        st["symbols"][sym] = {"last_processed_bar_ts": p.get("iex_cutoff_bar_ts"), "sides": sides, "void_warned": False}
    return st


def _state_shape_ok(st: Any) -> bool:
    """A usable state: {'symbols': {SYM: {'sides': {SIDE: {...}}}}} with every level an object."""
    if not isinstance(st, dict) or not isinstance(st.get("symbols"), dict):
        return False
    for sym in st["symbols"].values():
        if not isinstance(sym, dict) or not isinstance(sym.get("sides"), dict):
            return False
        if not all(isinstance(sd, dict) for sd in sym["sides"].values()):
            return False
    return True


def load_state(plan: dict, day: date) -> dict:
    """The day's state file; a CORRUPT file is backed up and the state is rebuilt from the plan, with every side already
    logged as triggered / outcome in logs/0dte_recommendations.jsonl restored so it is never alerted again."""
    path = state_path(day)
    if not path.exists():
        return init_state(plan, day)
    st = mr.read_json(path)
    if isinstance(st, dict) and _state_shape_ok(st):   # valid JSON of the wrong shape is corrupt too (Codex pre-ship 11-08)
        return st
    backup = path.with_name(f"{path.name}.corrupt-{mr.utcnow().strftime('%H%M%S')}")
    try:
        path.replace(backup)
    except OSError as e:
        logger.error("morning_watch: corrupt state backup failed (%s)", e)
    logger.error("morning_watch: corrupt state file backed up to %s — rebuilding dedupe from the recommendations log",
                 backup.name)
    st = init_state(plan, day)
    rows = [r for r in _read_jsonl(mr.STATE["recommendations"]) if r.get("date") == day.isoformat()]
    by_id = {sd["rec_id"]: sd for sym in st["symbols"].values() for sd in sym["sides"].values()}
    for r in rows:
        rid = r.get("rec_id")
        sd = by_id.get(rid) if isinstance(rid, str) else None
        if sd is None:
            continue
        if r.get("event") == "triggered":
            sd.update(status="TRIGGERED", alerted=True, alert_state=r.get("alert_state"), contract=r.get("contract"),
                      break_bar_ts=r.get("triggered_at"), break_close=r.get("break_close"), restored=True)
        elif r.get("event") == "outcome":
            sd.update(outcome=r.get("outcome"), outcome_alerted=True, outcome_ts_utc=r.get("outcome_bar_ts_utc"))
    st["restored_from_corrupt"] = backup.name
    return st


def acquire_lock(path: Path | None = None) -> Any:
    """Singleton: fcntl.flock on a /tmp file; the kernel releases it when the process dies. None if already held."""
    fh = open(path or LOCK_PATH, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def today_session(now: datetime) -> dict | None:
    today = now.astimezone(ET).date()
    cal = mr.calendar(today, today)
    if cal is None:
        return None
    return next((c for c in cal if c["date"] == today), {"date": today, "closed": True})


def run_watch(now_fn: Callable[[], datetime] = mr.utcnow, sleep_fn: Callable[[float], None] = time.sleep,
              dry_run: bool = False, lock_path: Path | None = None) -> int:
    lock = acquire_lock(lock_path)
    if lock is None:
        logger.info("morning_watch: another watcher holds the lock — exiting")
        return 0
    try:
        now = now_fn()
        clock = mr.clock_record(now, "09:47")
        logger.info("morning_watch: start now_utc=%s now_et=%s et_offset=%s wrapper_target_et=%s",
                    clock["now_utc"], clock["now_et"], clock["detected_et_offset"], clock["wrapper_target_et"])
        sess = today_session(now)
        if sess is None:
            logger.warning("morning_watch: calendar unavailable — no watch")
            if not dry_run:
                send_slack(":warning: Morning watcher: Alpaca calendar unavailable — no TRIGGERED alerts today")
            return 1
        if sess.get("closed"):
            logger.info("morning_watch: market closed today — exiting")
            return 0
        day = sess["date"]
        watch_end = sess["close"].astimezone(UTC) - BROKER_ZERO_DTE_CUTOFF
        if now >= watch_end:
            logger.info("morning_watch: past the watch end (%s) — exiting without alerting", mr.fmt_pt(watch_end))
            return 0
        plan = None
        while now_fn() < watch_end:
            plan = mr.read_json(mr.plan_path(day))
            if plan is not None:
                break
            sleep_fn(PLAN_WAIT_SLEEP_SECONDS)
        if plan is None:
            logger.warning("morning_watch: no 06:46 plan by the watch end — nothing watched")
            return 1
        state = load_state(plan, day)
        w = Watcher(plan, state, day, watch_end, now_fn=now_fn, sleep_fn=sleep_fn, dry_run=dry_run)
        w.save()
        w.run_loop()
        return 0
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except OSError as e:
            logger.warning("morning_watch: lock release failed (%s)", e)


# ══ heartbeat alarm ════════════════════════════════════════════════════════════════════════════════════════════════

def check_heartbeat(now: datetime | None = None, dry_run: bool = False) -> int:
    """Alarm once per day when, inside the watch window (from the 06:46 plan's time to the watch end), the watcher's
    last poll is older than WATCH_POLL_TIMEOUT_SECONDS (or there has been no poll at all)."""
    now = now or mr.utcnow()
    sess = today_session(now)
    if not sess or sess.get("closed"):
        return 0
    day = sess["date"]
    end = sess["close"].astimezone(UTC) - BROKER_ZERO_DTE_CUTOFF
    hb = mr.read_json(mr.STATE["heartbeat"]) or {}
    plan = mr.read_json(mr.plan_path(day))
    if plan is None:
        # T3: the 06:46 watchlist went out but the watcher has no plan -> no TRIGGERED alerts today; alarm once
        sent = ((hb.get("sends") or {}).get("watchlist") or {})
        if sent.get("date") == day.isoformat() and sent.get("slack_ok") and now < end \
                and hb.get("plan_alarm_date") != day.isoformat():
            msg = ("⚠️ Morning watcher: the 06:46 watchlist was sent but no plan file exists — no TRIGGERED alerts "
                   "today. No TRIGGERED message = no entry.")
            logger.warning("morning_watch: %s", msg)
            # recorded only after a confirmed Slack delivery, never on a dry run, so a failed send or a dry run
            # cannot suppress the day's real alarm (Codex pre-ship 11-09)
            if not dry_run and send_slack_delivery(msg):
                mr.update_heartbeat({"plan_alarm_date": day.isoformat(), "plan_alarm_utc": mr.iso_z(now)})
            return 1
        return 0
    start = mr.parse_ts(plan.get("ts_utc"))
    if start is None or not (start <= now < end):
        return 0
    w = hb.get("watcher") or {}
    last = mr.parse_ts(w.get("last_poll_utc")) if w.get("date") == day.isoformat() else None
    ref = last or start
    if (now - ref).total_seconds() <= WATCH_POLL_TIMEOUT_SECONDS:
        return 0
    if hb.get("watcher_alarm_date") == day.isoformat():
        return 0
    msg = (f"⚠️ Morning watcher silent: last poll {mr.fmt_pt(last, True) if last else 'never'} "
           f"(> {WATCH_POLL_TIMEOUT_SECONDS:.0f}s) — no TRIGGERED alerts while it is down. No TRIGGERED message = no entry.")
    logger.warning("morning_watch: %s", msg)
    # recorded only after a confirmed Slack delivery, never on a dry run (Codex pre-ship 11-09)
    if not dry_run and send_slack_delivery(msg):
        mr.update_heartbeat({"watcher_alarm_date": day.isoformat(), "watcher_alarm_utc": mr.iso_z(now)})
    return 1


# ══ weekly "0DTE recommendation performance" (v8) ══════════════════════════════════════════════════════════════════

def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning("morning_watch: bad JSONL line in %s (%s)", path.name, e)
                    continue
                if isinstance(row, dict):   # a valid non-object line is skipped, never crashes the reader's callers
                    out.append(row)
                else:
                    logger.warning("morning_watch: non-object JSONL line in %s skipped", path.name)
    except FileNotFoundError:
        return []
    return out


def weekly_performance(now: datetime | None = None, dry_run: bool = False) -> int:
    """For each logged 06:46 line of the ISO week: triggered?; after a trigger the option's highest archived bid (max
    potential gain vs the limit); the result at the printed stop or target, whichever printed first; for lines that never
    triggered, the move the underlying made past the trigger later anyway (SIP bars, T1). Totals by symbol, side, week."""
    now = now or mr.utcnow()
    today = now.astimezone(ET).date()
    iso = today.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    marker = _LOGS / f"0dte_performance_sent_{week}.json"
    if marker.exists():
        logger.info("morning_watch: weekly performance %s already sent", week)
        return 0
    monday = today - timedelta(days=today.weekday())
    rows = [r for r in _read_jsonl(mr.STATE["recommendations"])
            if str(r.get("date", "")) >= monday.isoformat() and str(r.get("date", "")) <= today.isoformat()]
    printed = {r["rec_id"]: r for r in rows if r.get("event") == "printed"}
    trig = {r["rec_id"]: r for r in rows if r.get("event") == "triggered"}
    outc = {r["rec_id"]: r for r in rows if r.get("event") == "outcome"}
    cal = mr.calendar(monday, today) or []
    closes = {c["date"].isoformat(): c for c in cal}
    lines: list[dict] = []
    for rid, p in sorted(printed.items()):
        t, o = trig.get(rid), outc.get(rid)
        row: dict = {"rec_id": rid, "date": p["date"], "symbol": p["symbol"], "side": p["side"], "state_0646": p.get("state"),
                     "trigger": p.get("trigger"), "triggered": bool(t), "alert_state": (t or {}).get("alert_state"),
                     "limit": (t or {}).get("limit"), "stop": (t or {}).get("stop"), "outcome": (o or {}).get("outcome"),
                     "max_bid_after": None, "max_gain_per_share": None, "result_per_share": None, "move_past_trigger": None}
        if t and t.get("contract"):
            quotes = [q for q in _read_jsonl(quotes_archive_path(date.fromisoformat(p["date"]))) if q.get("rec_id") == rid]
            bids = [float((q.get("quote") or {}).get("bid") or 0) for q in quotes if (q.get("quote") or {}).get("bid")]
            if bids:
                row["max_bid_after"] = max(bids)
            if row["limit"] is not None and row["max_bid_after"] is not None:
                row["max_gain_per_share"] = row["max_bid_after"] - row["limit"]
            if row["limit"] is not None and o:
                if o.get("outcome") == "STOP" and row["stop"] is not None:
                    row["result_per_share"] = row["stop"] - row["limit"]
                elif o.get("outcome") == "TARGET":
                    at = mr.parse_ts(o.get("outcome_bar_ts_utc"))
                    after = [q for q in quotes if at is not None and (mr.parse_ts(q.get("ts_utc")) or at) >= at
                             and (q.get("quote") or {}).get("bid")]
                    if after:
                        row["result_per_share"] = float(after[0]["quote"]["bid"]) - row["limit"]
        elif p.get("trigger") is not None and p["date"] in closes:
            c = closes[p["date"]]
            start = mr.parse_ts(p.get("ts_utc"))
            if start is not None:
                bars = mr._norm(mr.get_bars(p["symbol"], start, c["close"].astimezone(UTC), "sip"))
                if len(bars):
                    if p["side"] == "CALL":
                        row["move_past_trigger"] = max(0.0, float(bars["high"].max()) - float(p["trigger"]))
                    else:
                        row["move_past_trigger"] = max(0.0, float(p["trigger"]) - float(bars["low"].min()))
        lines.append(row)
    groups: dict[str, dict] = {}
    for r in lines:
        for key in (f"{r['symbol']} {r['side']}", "week"):
            g = groups.setdefault(key, {"lines": 0, "triggered": 0, "actionable": 0, "stop": 0, "target": 0,
                                        "result_per_share": 0.0, "results": 0, "move_missed": 0.0})
            g["lines"] += 1
            g["triggered"] += int(r["triggered"])
            g["actionable"] += int(r["alert_state"] == "ACTIONABLE")
            g["stop"] += int(r["outcome"] == "STOP")
            g["target"] += int(r["outcome"] == "TARGET")
            if r["result_per_share"] is not None:
                g["result_per_share"] += r["result_per_share"]
                g["results"] += 1
            if r["move_past_trigger"]:
                g["move_missed"] += r["move_past_trigger"]
    md = [f"# 0DTE recommendation performance — {week}", "",
          "| Group | Lines | Triggered | Actionable | Stop | Target | Result $/contract (n) | Move past trigger, untriggered |",
          "|---|---|---|---|---|---|---|---|"]
    for key, g in sorted(groups.items(), key=lambda kv: (kv[0] == "week", kv[0])):
        md.append(f"| {key} | {g['lines']} | {g['triggered']} | {g['actionable']} | {g['stop']} | {g['target']} | "
                  f"{g['result_per_share'] * mr.SHARES_PER_CONTRACT:+.0f} ({g['results']}) | {g['move_missed']:.2f} |")
    md += ["", "| Line | Triggered | Alert | Outcome | Limit | Max bid after | Result $/sh | Move past trigger |",
           "|---|---|---|---|---|---|---|---|"]
    for r in lines:
        res = "—" if r["result_per_share"] is None else f"{r['result_per_share']:+.2f}"
        mv = "—" if r["move_past_trigger"] is None else f"{r['move_past_trigger']:.2f}"
        lim = "—" if r["limit"] is None else f"{r['limit']:.2f}"
        mb = "—" if r["max_bid_after"] is None else f"{r['max_bid_after']:.2f}"
        md.append(f"| {r['rec_id']} | {'yes' if r['triggered'] else 'no'} | {r['alert_state'] or '—'} | "
                  f"{r['outcome'] or '—'} | {lim} | {mb} | {res} | {mv} |")
    md_text = "\n".join(md) + "\n\n_Result at the printed stop is the stop price minus the limit (a gap through the stop " \
                              "loses more); max bid after uses the per-minute archive of the watcher. No historical option quotes exist at Alpaca._\n"
    try:
        mr.atomic_write_text(_LOGS / f"0dte_performance_{week}.md", md_text)
    except Exception as e:
        logger.warning("morning_watch: performance md write failed (%s)", e)
    wk = groups.get("week") or {"lines": 0, "triggered": 0, "actionable": 0, "stop": 0, "target": 0, "result_per_share": 0.0,
                                "results": 0, "move_missed": 0.0}
    head = (f"📊 0DTE recommendation performance · {week}: {wk['lines']} lines · {wk['triggered']} triggered · "
            f"{wk['actionable']} actionable · stop {wk['stop']} / target {wk['target']} · result "
            f"{wk['result_per_share'] * mr.SHARES_PER_CONTRACT:+.0f}$/contract over {wk['results']}")
    body = "\n".join(f"• {k}: {g['lines']} lines, {g['triggered']} triggered, {g['actionable']} actionable, "
                     f"stop {g['stop']} / target {g['target']}, result {g['result_per_share'] * mr.SHARES_PER_CONTRACT:+.0f}$ "
                     f"({g['results']}), untriggered move past trigger {g['move_missed']:.2f}"
                     for k, g in sorted(groups.items()) if k != "week")
    if dry_run:
        logger.info("morning_watch: weekly performance dry run\n%s\n%s", head, body)
        return 0
    ok = bool(send_slack_blocks([{"type": "section", "text": {"type": "mrkdwn", "text": str(head + "\n" + body)[:2900]}}], head))
    if ok:
        mr.atomic_write_text(marker, json.dumps({**mr.stamp(now), "week": week}))
        logger.info("morning_watch: weekly performance Slack sent (%s)", week)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    ap = argparse.ArgumentParser(description="MORNING-1029 trigger watcher (report only; places no orders)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check-heartbeat", action="store_true")
    g.add_argument("--weekly-performance", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.check_heartbeat:
            return check_heartbeat(dry_run=args.dry_run)
        if args.weekly_performance:
            return weekly_performance(dry_run=args.dry_run)
        return run_watch(dry_run=args.dry_run)
    except Exception as e:
        logger.error("morning_watch: unexpected failure (%s)", e, exc_info=True)
        if not args.dry_run:
            try:
                send_slack(f":warning: Morning watcher FAILED: {str(e)[:120]}")
            except Exception as e2:
                logger.error("morning_watch: failure alert not sent (%s)", e2)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

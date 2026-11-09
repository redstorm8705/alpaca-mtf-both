#!/usr/bin/env python3
"""
scripts/gex_weekend_report.py — WEEKLY GEX support/resistance report (Rafael request 2026-09-06).

SCHEDULE (MORNING-1029, owner-approved 2026-11-08): the report moved from Saturday to the FIRST TRADING DAY OF
THE WEEK at 09:00 ET (06:00 PT). Cron fires Monday and Tuesday; the run sends only when today is a trading day
(Alpaca calendar) AND this ISO week's marker logs/gex_weekly_sent_<ISO-year>-W<week>.json is absent. The marker is
written only after a successful Slack send, so a Monday holiday OR a failed Monday send both lead to a Tuesday send
(logged "Monday send missing"), and a successful Monday send stops Tuesday. Spot is the latest pre-market IEX bar
(price + time + source), replacing "Friday close as spot".

Delivers, in plain English, the support & resistance levels to watch Mon–Fri of the UPCOMING week
for **SPY and QQQ only** (Rafael 2026-09-06 — index-level, not single-name), from prior-close dealer-
gamma. Per the board+Gro+GAI design (2026-09-06, unanimous), each name shows AT MOST 3 levels:
  🔴 Resistance = call wall · 🟢 Support = put wall · ⚖️ Pivot = gamma flip,
with a regime-colored one-line read (positive gamma → walls are brakes/mean-revert; negative gamma →
walls are breakout/acceleration; near-flip → coin-flip) and the honest "snapshot re-forms
daily" caveat. Also carries the regime label, pin/centroid, net GEX, confidence as supporting detail.

WHY A SEPARATE PATH (not refresh_gex): the live GEX writer computes spot from the real-time
last-trade + an NBBO spot-consistency GUARD (board+Gro+GAI 2026-07-26). On a weekend, single-name
quotes are missing or wide, so that guard (correctly) refuses them and every single name fails to
UNKNOWN — a live-safety behaviour we must NOT weaken. For a REPORT the spot is the latest IEX bar
(a pre-market bar at 06:00 PT on the first trading day; MORNING-1029). This module therefore reuses data.gex's vetted compute
internals (_fetch_contracts / _fetch_snapshots / _compute_gex) with a latest-IEX-bar spot, and
NEVER touches the live gex_snapshot.json cache, the live guard, or any execution path.

SAFETY: READ-ONLY. No orders, no execution imports, no writes to data/state or the GEX cache. It
reads the Alpaca option chain (T1) + a daily bar (T1) and posts to Slack. A per-symbol failure is
isolated (that name renders UNKNOWN); the report never crashes the process.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PT = ZoneInfo("America/Los_Angeles")
logger = logging.getLogger("gex_weekend_report")

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # python-dotenv absent (non-OCI env) → rely on ambient environment
except Exception as _dotenv_err:
    # A present-but-unreadable .env must not kill the report at import (above main()'s guard).
    logger.warning("gex_weekend_report: load_dotenv skipped (%s) — using ambient environment", _dotenv_err)

import requests                                         # noqa: E402  (T1 REST, same as data.gex)
import data.gex as gex                                  # noqa: E402  (read-only: compute internals only)
from alerts import send_slack, send_slack_blocks        # noqa: E402

_HEARTBEAT = _ROOT / "logs" / "gex_weekend_report_heartbeat.json"
_LOGS = _ROOT / "logs"
ET = ZoneInfo("America/New_York")
_WRAPPER_TARGET_ET = "09:00"   # the cron_tz_wrapper target this job is scheduled at (logged for DST checks)
_REQUEST_TIMEOUT_SECONDS = 6.0


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp -> fsync -> replace, in the target directory (atomic on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as e:
            logger.warning("gex_weekend_report: tmp cleanup failed (%s)", e)
        raise


def _iso_week_marker(day: date) -> Path:
    """logs/gex_weekly_sent_<ISO-year>-W<week>.json for the ISO week containing `day`."""
    iso = day.isocalendar()
    return _LOGS / f"gex_weekly_sent_{iso[0]}-W{iso[1]:02d}.json"


def _calendar_days(start: date, end: date) -> list | None:
    """Trading days in [start, end] from the Alpaca calendar (trading API, read-only), or None on any failure."""
    try:
        resp = requests.get(f"{gex._BASE_TRADING}/v2/calendar", headers=gex._headers(),
                            params={"start": start.isoformat(), "end": end.isoformat()},
                            timeout=_REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.warning("gex_weekend_report: calendar HTTP %s", resp.status_code)
            return None
        body = resp.json()
        if not isinstance(body, list):
            return None
        return [str(d.get("date")) for d in body if isinstance(d, dict) and d.get("date")]
    except Exception as e:
        logger.warning("gex_weekend_report: calendar fetch failed (%s)", e)
        return None


def _clock_record(now_utc: datetime) -> dict:
    """The clock this run detected (DST check): now_utc, now_et, the ET UTC offset and the wrapper target."""
    now_et = now_utc.astimezone(ET)
    return {"now_utc": now_utc.isoformat().replace("+00:00", "Z"), "now_et": now_et.isoformat(),
            "detected_et_offset": _utc_offset_text(now_et), "wrapper_target_et": _WRAPPER_TARGET_ET}


def _utc_offset_text(dt: datetime) -> str:
    """'-04:00' (EDT) / '-05:00' (EST): the '+HH:MM' tail of the ISO string."""
    return str(dt.isoformat(timespec="seconds"))[-6:]


def send_gate(now_utc: datetime) -> tuple[bool, str]:
    """(send?, reason). Send only on a trading day whose ISO week has no successful-send marker yet.
    A Tuesday run reads Monday's marker: absent -> 'Monday send missing' -> send; present -> exit."""
    today = now_utc.astimezone(ET).date()
    marker = _iso_week_marker(today)
    if marker.exists():
        return False, f"marker present ({marker.name}) — this week's report already sent"
    days = _calendar_days(today, today)
    if days is None:
        return False, "calendar unavailable — cannot confirm a trading day"
    if today.isoformat() not in days:
        return False, "market closed today (Alpaca calendar)"
    if today.weekday() >= 1:   # Tuesday (or later): the Monday send did not happen this week
        return True, "Monday send missing"
    return True, "first trading day of the week"


def _write_marker(now_utc: datetime) -> None:
    today = now_utc.astimezone(ET).date()
    _atomic_write_text(_iso_week_marker(today), json.dumps({
        "sent_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "sent_local": now_utc.astimezone(PT).isoformat(), "tz": "America/Los_Angeles",
        "trading_day": today.isoformat()}))


def _latest_spot(symbol: str) -> tuple[float, str] | None:
    """Latest pre-market price = spot for the report, with its bar time. Alpaca latest bar (T1 REST, IEX feed,
    real time); at 06:00 PT on a trading day this is a pre-market bar (verified 2026-10-29). Returns
    (price, bar time ISO UTC) or None on any failure — NEVER raises."""
    try:
        resp = requests.get(
            f"{gex._BASE_DATA}/v2/stocks/{symbol}/bars/latest",
            headers=gex._headers(), params={"feed": "iex"}, timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            data = resp.json()
            bar = data.get("bar") if isinstance(data, dict) else None
            c = bar.get("c") if isinstance(bar, dict) else None
            t = bar.get("t") if isinstance(bar, dict) else None
            if isinstance(c, (int, float)) and c > 0:
                return float(c), str(t or "")
            return None
        logger.warning("gex_weekend_report: bar HTTP %s for %s", resp.status_code, symbol)
    except Exception as e:
        logger.warning("gex_weekend_report: bar fetch failed for %s (%s)", symbol, e)
    return None


def _fmt_bar_time_pt(ts: str) -> str:
    """'2026-11-09T13:59:00Z' -> '05:59 AM PT'; '' -> 'time n/a'."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(PT).strftime("%I:%M %p PT")
    except (TypeError, ValueError):
        return "time n/a"


def _universe() -> list[str]:
    """SPY and QQQ only (Rafael 2026-09-06 — the weekly S/R report is index-level, not single-name).
    Kept as a function so the set is one obvious edit point."""
    return ["SPY", "QQQ"]


def _compute_symbol(symbol: str, date_gte: str, date_lte: str) -> dict:
    """Compute latest-pre-market-spot GEX for one symbol. Returns a normalized row dict; on any miss/error
    returns {'symbol', 'label': 'UNKNOWN', 'reason': ...}. NEVER raises (per-symbol isolation)."""
    try:
        got = _latest_spot(symbol)
        if not got:
            return {"symbol": symbol, "label": "UNKNOWN", "reason": "no_spot"}
        spot, spot_ts = got
        oi_map = gex._fetch_contracts(symbol, date_gte, date_lte)
        if not oi_map:
            return {"symbol": symbol, "label": "UNKNOWN", "reason": "no_contracts", "spot": spot}
        snapshots = gex._fetch_snapshots(list(oi_map))
        if not snapshots:
            return {"symbol": symbol, "label": "UNKNOWN", "reason": "no_snapshots", "spot": spot}
        gx = gex._compute_gex(snapshots, oi_map, spot)
        pin = gx.get("pin", {}) if isinstance(gx.get("pin"), dict) else {}
        return {
            "symbol": symbol,
            "label": gx.get("label", "UNKNOWN"),
            "spot": round(spot, 2),
            "spot_time_pt": _fmt_bar_time_pt(spot_ts),
            "spot_source": "IEX latest bar (Alpaca T1)",
            "centroid": pin.get("centroid"),
            "call_wall": pin.get("call_wall"),
            "put_wall": pin.get("put_wall"),
            "confidence": pin.get("confidence"),
            "flip_strike": gx.get("flip_strike"),
            "raw_gex_m": gx.get("raw_gex_m"),
            "contract_count": gx.get("contract_count"),
        }
    except Exception as e:  # per-symbol isolation — one bad name never blanks the report
        logger.warning("gex_weekend_report: compute failed for %s (%s)", symbol, e)
        return {"symbol": symbol, "label": "UNKNOWN", "reason": f"error:{str(e)[:60]}"}


def _regime_glyph(label: str | None) -> str:
    """Non-color regime markers so 🔴/🟢 stay reserved for resistance/support levels (avoids the
    green-POSITIVE / red-NEGATIVE collision with the red-resistance / green-support legend)."""
    s = str(label or "").upper()
    if s == "POSITIVE":
        return "🧲"   # pinned / mean-revert
    if s == "NEGATIVE":
        return "⚡"    # accelerant / trending
    if s == "NEAR-FLIP":
        return "🔀"   # coin-flip
    return "⚪"


def _num(v, dp: int = 2) -> str:
    if isinstance(v, (int, float)):
        return f"{v:,.{dp}f}"
    return "—"


def _gex_m(v) -> str:
    if isinstance(v, (int, float)):
        return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}M"
    return "—"


def _sec(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _ctx(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _hdr(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": str(text)[:150], "emoji": True}}


_DIV = {"type": "divider"}


def _regime_plain(label: str | None) -> str:
    """Plain-English regime name for the report header."""
    s = str(label or "").upper()
    if s == "POSITIVE":
        return "calm · mean-revert week"
    if s == "NEGATIVE":
        return "volatile · trending week"
    if s == "NEAR-FLIP":
        return "coin-flip week"
    return "regime unknown"


def _fmt_lvl(v) -> str:
    """A price level -> "$1,234.56", or "—" when the level is missing."""
    if isinstance(v, (int, float)):
        return f"${v:,.2f}"
    return "—"


def _walls_converged(cw, pw, spot) -> bool:
    """True when the call & put walls collapse onto ~one strike (gamma pinned near spot). In that
    state the 'resistance above / support below' bracket is degenerate (both walls at the same
    price), so the read switches to a single-magnet framing instead of 'break of $X (down) or $X
    (up)'. Band = 0.2% of spot (e.g. SPY $770 -> ~$1.54)."""
    if not (isinstance(cw, (int, float)) and isinstance(pw, (int, float))):
        return False
    ref = spot if isinstance(spot, (int, float)) and spot > 0 else max(abs(cw), abs(pw), 1.0)
    return abs(cw - pw) <= 0.002 * ref


def _sr_read(label: str | None, cw, pw, flip, spot) -> str:
    """Regime-colored one-line 'what to watch M–F' read (board+Gro+GAI 2026-09-06). References ONLY
    the levels actually present, and switches to a single-magnet framing when the walls converge, so
    it never prints a broken 'break of $X (down) or $X (up)' or a bare '—' inside a sentence."""
    s = str(label or "").upper()
    cwp, pwp, fp = _fmt_lvl(cw), _fmt_lvl(pw), _fmt_lvl(flip)
    have_walls = isinstance(cw, (int, float)) and isinstance(pw, (int, float))
    have_flip = isinstance(flip, (int, float))
    converged = _walls_converged(cw, pw, spot)
    bracket = have_walls and not converged and cw > pw   # proper resistance-above / support-below
    if converged:
        lvl = cwp   # walls are ~equal; cw is the magnet strike
        if s == "POSITIVE":
            return f"Gamma is pinned at {lvl} (near spot) — price likely magnetizes to {lvl}; expect a tight range."
        if s == "NEGATIVE":
            return f"Gamma is concentrated at {lvl} (near spot) with dealers short gamma — expect a volatile fight around {lvl}; a decisive break either way likely ACCELERATES."
        if s == "NEAR-FLIP":
            return f"Gamma concentrated at {lvl} — the first decisive break away from it sets the week's character."
        return f"Gamma concentrated at {lvl} — watch for a decisive break from it."
    if bracket:
        if s == "POSITIVE":
            tail = f" while above the {fp} pivot" if have_flip else ""
            return f"Expect chop between {pwp} and {cwp}; rallies to {cwp} and dips to {pwp} likely fade back toward the middle{tail}."
        if s == "NEGATIVE":
            tail = f" Below the {fp} pivot, moves speed up." if have_flip else ""
            return f"Not a fade market — a break of {pwp} (down) or {cwp} (up) likely ACCELERATES.{tail}"
        if s == "NEAR-FLIP":
            piv = f"the {fp} pivot" if have_flip else "either edge"
            return f"Direction unresolved — the first clean move through {piv} (range {pwp}–{cwp}) sets the week's character."
    # walls missing or inverted (cw <= pw): don't assert a bracket that isn't there
    if have_flip:
        return f"Levels thin this week — watch the {fp} gamma-flip pivot as the dividing line."
    return "Regime unknown or levels thin — not strongly actionable this week."


def build_report() -> tuple[str, list, str]:
    """Returns (markdown_archive, slack_blocks, slack_fallback_text). Pure formatting over the
    read-only computed rows — SPY/QQQ support/resistance to watch Mon–Fri. Block Kit per
    rules/slack_format.md (SLK01/SLK02)."""
    date_gte, date_lte = gex._expiry_range()
    today_iso = datetime.now(PT).strftime("%Y-%m-%d")
    rows = [_compute_symbol(sym, date_gte, date_lte) for sym in _universe()]
    ok = [r for r in rows if r.get("label") not in (None, "UNKNOWN")]

    # ── Slack (Block Kit) — S/R levels to watch this week ──
    blocks: list = [_hdr(f"GEX Weekly Levels · {today_iso}")]
    blocks.append(_ctx(f"Support/resistance to watch Mon–Fri (this week's expiry {date_lte}) · "
                       f"spot = latest pre-market IEX bar · {len(ok)}/{len(rows)} names\n"
                       f"🔴 resistance (call wall) · 🟢 support (put wall) · ⚖️ pivot (gamma flip)\n"
                       f"regime: 🧲 calm/mean-revert · ⚡ volatile/trending · 🔀 coin-flip"))
    for r in rows:
        g = _regime_glyph(r.get("label"))
        sym = r.get("symbol")
        blocks.append(_DIV)
        if r.get("label") in (None, "UNKNOWN"):
            blocks.append(_sec(f"{g} *{sym}* — levels unavailable · _{r.get('reason', 'no data')}_"))
            continue
        cw, pw, flip, spot = r.get("call_wall"), r.get("put_wall"), r.get("flip_strike"), r.get("spot")
        if _walls_converged(cw, pw, spot):
            lvl_lines = f"🎯 Gamma magnet (walls converged near spot): *{_fmt_lvl(cw)}*"
            if isinstance(flip, (int, float)):
                lvl_lines += f"\n⚖️ Pivot (gamma flip): *{_fmt_lvl(flip)}*"
        else:
            lvl_lines = (f"🔴 Resistance (call wall): *{_fmt_lvl(cw)}*\n"
                         f"🟢 Support (put wall): *{_fmt_lvl(pw)}*\n"
                         f"⚖️ Pivot (gamma flip): *{_fmt_lvl(flip)}*")
        blocks.append(_sec(f"{g} *{sym}* — {_regime_plain(r.get('label'))} · spot {_fmt_lvl(spot)} "
                           f"({r.get('spot_time_pt', 'time n/a')}, IEX)\n{lvl_lines}"))
        blocks.append(_sec(f"_Watch M–F:_ {_sr_read(r.get('label'), cw, pw, flip, spot)}"))
        blocks.append(_ctx(f"pin {_num(r.get('centroid'))} · net GEX {_gex_m(r.get('raw_gex_m'))} · "
                           f"confidence {_num(r.get('confidence'))}"))
    blocks.append(_DIV)
    blocks.append(_ctx("This morning's snapshot (prior-close open interest) for this week's expiry — sharpest early in the week, re-forms daily as "
                       "options trade. Weight by confidence; re-check midweek. Read-only report; not the "
                       "live RTH GEX signal (which uses a guarded real-time spot)."))

    # ── Fallback (SLK02 — carries the answer for the phone/notification) ──
    fb_bits = []
    for r in rows:
        if r.get("label") in (None, "UNKNOWN"):
            continue
        cw, pw, spot = r.get("call_wall"), r.get("put_wall"), r.get("spot")
        if _walls_converged(cw, pw, spot):
            fb_bits.append(f"{r.get('symbol')} {r.get('label')} magnet {_fmt_lvl(cw)}")
        else:
            fb_bits.append(f"{r.get('symbol')} {r.get('label')} S{_fmt_lvl(pw)}/R{_fmt_lvl(cw)}")
    fallback = (f"GEX Weekly Levels {today_iso}: " + " · ".join(fb_bits)) if fb_bits else f"GEX Weekly Levels {today_iso}: no computable names"

    # ── Markdown archive (S/R-led; Slack uses Block Kit) ──
    md_lines = [
        f"# GEX Weekly Levels — {today_iso}",
        f"_Support/resistance to watch Mon–Fri · this week's expiry {date_lte} · spot = latest pre-market IEX bar · "
        f"{len(ok)}/{len(rows)} names · read-only_",
        "",
    ]
    for r in rows:
        sym = r.get("symbol")
        if r.get("label") in (None, "UNKNOWN"):
            md_lines += [f"## {sym} — levels unavailable ({r.get('reason', '')})", ""]
            continue
        cw, pw, flip, spot = r.get("call_wall"), r.get("put_wall"), r.get("flip_strike"), r.get("spot")
        if _walls_converged(cw, pw, spot):
            lvl_md = [f"- 🎯 **Gamma magnet (walls converged near spot):** {_fmt_lvl(cw)}"]
            if isinstance(flip, (int, float)):
                lvl_md.append(f"- ⚖️ **Pivot (gamma flip):** {_fmt_lvl(flip)}")
        else:
            lvl_md = [f"- 🔴 **Resistance (call wall):** {_fmt_lvl(cw)}",
                      f"- 🟢 **Support (put wall):** {_fmt_lvl(pw)}",
                      f"- ⚖️ **Pivot (gamma flip):** {_fmt_lvl(flip)}"]
        md_lines += [
            f"## {sym} — {r.get('label')} ({_regime_plain(r.get('label'))}) · spot {_fmt_lvl(spot)} "
            f"({r.get('spot_time_pt', 'time n/a')}, {r.get('spot_source', '')})",
            *lvl_md,
            f"- pin {_num(r.get('centroid'))} · net GEX {_gex_m(r.get('raw_gex_m'))} · "
            f"confidence {_num(r.get('confidence'))}",
            f"- **Watch M–F:** {_sr_read(r.get('label'), cw, pw, flip, spot)}",
            "",
        ]
    md_lines += ["> This morning's snapshot (prior-close open interest) for this week's expiry — sharpest early in the week, re-forms daily as options "
                 "trade. Weight by confidence; re-check midweek.",
                 "> Read-only report; not the live RTH GEX signal (which uses a guarded real-time spot).", ""]
    md = "\n".join(md_lines) + "\n"
    return md, blocks, fallback


def _write_heartbeat(ok: bool, note: str, clock: dict | None = None) -> None:
    try:
        _atomic_write_text(_HEARTBEAT, json.dumps({"last_run_pt": datetime.now(PT).isoformat(), "ok": ok,
                                                   "note": note, **(clock or {})}))
    except Exception as e:
        logger.warning("gex_weekend_report: heartbeat write failed (%s)", e)


def main(now_utc: datetime | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    now_utc = now_utc or datetime.now(timezone.utc)
    clock = _clock_record(now_utc)
    logger.info("gex_weekend_report: clock now_utc=%s now_et=%s et_offset=%s wrapper_target_et=%s",
                clock["now_utc"], clock["now_et"], clock["detected_et_offset"], clock["wrapper_target_et"])
    send, why = send_gate(now_utc)
    if not send:
        logger.info("gex_weekend_report: no send — %s", why)
        _write_heartbeat(not why.startswith("calendar unavailable"), f"skipped: {why}", clock)
        return 0
    logger.info("gex_weekend_report: sending — %s", why)
    try:
        md, blocks, fallback = build_report()
    except Exception as e:
        logger.error("gex_weekend_report: build failed (%s)", e, exc_info=True)
        try:
            send_slack(f":warning: GEX weekend report FAILED to build: {e}")
        except Exception as _se:
            logger.warning("gex_weekend_report: failure alert not sent (%s)", _se)
        _write_heartbeat(False, f"build failed: {e}", clock)
        return 1

    # Slack-first (delivery decoupled from the file write). send_slack_blocks returns real status.
    try:
        slack_ok = bool(send_slack_blocks(blocks, fallback))
    except Exception as e:
        slack_ok = False
        logger.warning("gex_weekend_report: Slack send raised unexpectedly (%s)", e)
    if slack_ok:
        logger.info("gex_weekend_report: Slack blocks sent")
        try:
            _write_marker(now_utc)   # only after a successful send (a failed send leaves Tuesday to retry)
            logger.info("gex_weekend_report: weekly marker written (%s)", _iso_week_marker(now_utc.astimezone(ET).date()).name)
        except Exception as e:
            logger.warning("gex_weekend_report: weekly marker write failed (%s) — Tuesday run may resend", e)
    else:
        logger.warning("gex_weekend_report: Slack delivery FAILED or not configured (see alerts warnings above)")

    md_ok = False
    out = _LOGS / f"gex_weekend_report_{now_utc.astimezone(PT).strftime('%Y-%m-%d')}.md"
    try:
        _atomic_write_text(out, md)
        md_ok = True
        logger.info("gex_weekend_report: wrote %s", out)
    except Exception as e:
        logger.warning("gex_weekend_report: md write failed (%s)", e)

    _write_heartbeat(slack_ok or md_ok, f"slack={'ok' if slack_ok else 'FAIL'} md={'ok' if md_ok else 'FAIL'} ({why})", clock)
    return 0 if (slack_ok or md_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())

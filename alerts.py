# ruff: noqa: E501
"""
alerts.py
Real-time push notifications for the MTF bot.

Supports two transports — configure whichever you prefer via .env:

  NTFY_TOPIC=your_private_topic_name   # ntfy.sh (free, mobile push via ntfy app)
  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # Slack incoming webhook

Both are optional and independent — you can enable one, both, or neither.
If neither is configured, alerts are logged only (graceful no-op).

ntfy.sh setup:
  1. Install the ntfy app on iOS or Android
  2. Pick any private topic name (e.g. "alpaca-mtf-abc123" — keep it random)
  3. Add NTFY_TOPIC=your_topic to .env
  4. Subscribe to that topic in the app — alerts arrive instantly

Slack setup:
  1. Create an Incoming Webhook in your Slack workspace
  2. Add SLACK_WEBHOOK_URL=https://hooks.slack.com/... to .env
  3. Bot posts to whichever channel the webhook targets

Priority levels (ntfy):
  5 = max (red banner, bypasses do-not-disturb)   → CRITICAL: kills switch, crash
  4 = high (orange)                               → ENTRY, EXIT with significant PnL
  3 = default                                     → routine entries, partials
  2 = low (grey, silent)                          → info/debug only
"""

import os
import time
import json
import logging
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Mobile-clean formatter (Slack WS2): the single chokepoint _send_slack_chunked routes EVERY outbound
# Slack message through this, so markdown tables / multi-line LLM replies render readably on mobile
# (tables → "a · b · c" lines, separators dropped, newlines preserved) instead of wrapping into
# garbage. It is pure, deterministic, idempotent, and formatting-only. Defensive import with an
# identity fallback — alerts.py must NEVER fail to alert if the formatter is somehow unavailable.
try:
    from slack_format import mobile_clean as _mobile_clean
except Exception:  # pragma: no cover — keep alerting alive even if the formatter can't import
    def _mobile_clean(text, max_chars=None):  # type: ignore[misc]
        # Degraded identity fallback: mirror mobile_clean's contract closely enough to never
        # surprise a caller — any falsy input (None / "" / 0 / False / []) -> "" (matching
        # mobile_clean's `if not text: return ""`), any other non-str -> str(text), str -> as-is.
        # Never raises. Reachable only if slack_format (pure-stdlib) fails to import.
        return "" if not text else (text if isinstance(text, str) else str(text))

# macOS Python 3.10 ships without system CA certs — use certifi bundle so
# Slack (and ntfy) HTTPS calls don't fail with CERTIFICATE_VERIFY_FAILED.
try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()   # fallback: default context

logger = logging.getLogger("alerts")
PT = ZoneInfo("America/Los_Angeles")

# ── Config — read at import time, hot-reload not needed ──────────────────────
_NTFY_TOPIC       = os.getenv("NTFY_TOPIC", "").strip()
_SLACK_WEBHOOK    = os.getenv("SLACK_WEBHOOK_URL", "").strip()
_NTFY_BASE        = "https://ntfy.sh"

# ── State file paths for alert throttling ────────────────────────────────────
_HERE      = Path(__file__).resolve().parent
_STATE_DIR = _HERE / "data" / "state"

# ── Phase-1 Slack UX (2026-07-04, board+GAI; Gro waived by Rafael — TPD) ──────
# Enriched-text-first (Block Kit deferred to Increment 2). Everything here is
# feature-flagged and exception-safe: a formatting error must NEVER fail to
# alert — it falls back to the raw text (GAI de-risk mandate for the RTH path).
SLACK_V2_ENABLED = os.getenv("SLACK_V2_ENABLED", "1").strip() != "0"

SEV_CRITICAL = "🚨 CRITICAL"   # immediate operator action / capital-risk
SEV_WARNING  = "⚠️ WARNING"    # awareness; investigate
SEV_INFO     = "ℹ️"            # routine (entries/exits/health) — existing emoji already signals this

# Operator-hostile jargon → plain English. Applied centrally to EVERY outbound
# Slack string so all ~32 message types benefit without touching call sites.
# Deliberately conservative: only unambiguous internal terms (no risk of
# garbling a symbol or a legitimate label).
_JARGON = (
    ("held_for_orders", "order still holding shares (awaiting fill)"),
    ("fail-closed",     "kept protection on (did not drop the position)"),
    ("PHANTOM ENTRY",   "UNVERIFIED ENTRY"),
    ("phantom entry",   "unverified entry"),
    ("GTC-RACE",        "GTC order conflict"),
    ("RC-4",            "Position-Mismatch check"),
)


def _sanitize(text: object) -> str:
    """De-jargon an outbound Slack string. NEVER raises — returns a sendable string on any input.

    Param is typed `object`: a None/float can leak from an f-string, so we coerce
    to str BEFORE any string op rather than relying on the except backstop
    (Gro+GAI pre-ship consensus 2026-07-04 — hardening, not a correctness fix).
    """
    if not isinstance(text, str):
        text = str(text)
    if not SLACK_V2_ENABLED:
        return text
    try:
        for term, plain in _JARGON:
            if term in text:
                text = text.replace(term, plain)
        return text
    except Exception:
        # Any unforeseen error — return the (already str-coerced) text. NEVER fail to alert.
        return text


def _atomic_write(path: Path, data: dict) -> None:
    """
    Atomic write via tmp→fsync→replace (RC-5, SIGKILL-safe).
    Creates parent dirs if missing. Writes JSON so keys with
    newlines or special chars are handled safely.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"_atomic_write failed for {path.name}: {e}")


def _ntfy(title: str, body: str, priority: int = 3, tags: list | None = None) -> bool:
    """POST to ntfy.sh. Returns True on success."""
    if not _NTFY_TOPIC:
        return False
    url = f"{_NTFY_BASE}/{_NTFY_TOPIC}"
    headers: dict[str, str] = {
        "Title":    title,
        "Priority": str(priority),
        "Tags":     (",".join(tags) if tags else "chart_with_upwards_trend"),
        "Content-Type": "text/plain",
    }
    try:
        req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=4, context=_SSL_CTX) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"ntfy send failed: {e}")
        return False


# ── Slack chunking (Rafael 2026-08-18: long audit summaries were TRUNCATED in-client — a
# cut-off message is useless. Split into (i/N) parts on line boundaries, sent in order, each
# safely under Slack's display limit, so a message is NEVER cut off). ─────────────────────
_SLACK_CHUNK_LIMIT = 3500  # safe margin under Slack's ~4000-char in-client truncation


def _chunk_text(text: object, limit: int = _SLACK_CHUNK_LIMIT) -> list:
    """Split text into <=limit-char chunks on NEWLINE boundaries (never mid-line, unless a
    single line itself exceeds limit → hard-wrapped). LOSSLESS — including interior blank
    lines and a trailing newline: uses a None sentinel to distinguish 'nothing pending' from
    'a pending blank/empty line', so blank separators are never dropped at a flush boundary
    (cold-2nd 2026-08-18). Never raises."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return [text]
    chunks: list = []
    cur = None                              # None = nothing pending; "" = a pending blank line
    for line in text.split("\n"):
        while len(line) > limit:            # a single over-long line → hard-wrap
            if cur is not None:
                chunks.append(cur)
                cur = None
            chunks.append(line[:limit])
            line = line[limit:]
        if cur is None:
            cur = line
        elif len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}"
    if cur is not None:
        chunks.append(cur)
    return chunks


def _post_slack_text(text: str) -> bool:
    """POST one Slack webhook message. Returns True on HTTP 200. Never raises.

    unfurl_links/unfurl_media are disabled so a URL in the body (e.g. a billing
    or docs link inside an error string) never balloons into a large preview
    card — the #1 source of channel noise (Rafael 2026-08-26)."""
    try:
        req = urllib.request.Request(
            _SLACK_WEBHOOK,
            data=json.dumps({"text": text, "unfurl_links": False, "unfurl_media": False}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4, context=_SSL_CTX) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Slack send failed: {e}")
        return False


def _post_slack_payload(payload: dict) -> bool:
    """POST an arbitrary Slack webhook JSON payload (text and/or Block Kit `blocks`). True on HTTP
    200. Never raises. Sibling of _post_slack_text (kept separate so the existing text path is
    untouched).

    unfurl_links/unfurl_media default OFF (merged so a caller key still wins) to kill large link
    preview cards — the same channel-noise fix as _post_slack_text (Rafael 2026-08-26)."""
    try:
        body = {"unfurl_links": False, "unfurl_media": False, **(payload or {})}
        req = urllib.request.Request(
            _SLACK_WEBHOOK,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4, context=_SSL_CTX) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Slack send failed: {e}")
        return False


def send_slack_blocks(blocks: list, fallback_text: str) -> None:
    """Send a Block Kit message per rules/slack_format.md (SLK01/SLK02): structured `blocks` plus a
    top-level `fallback_text` that carries the answer (the phone notification preview + screen-reader
    string). Chunks on BLOCK boundaries (Slack caps 50 blocks/message) so nothing is dropped. Falls
    back to a plain-text send if there are no blocks, and to logging if Slack is not configured.
    Never raises — a formatting error must never fail to alert (mirrors the module's fail-safe rule)."""
    fallback_text = _sanitize(fallback_text)
    if not _SLACK_WEBHOOK:
        logger.warning(f"[ALERT no-op] {fallback_text[:120]}")
        return
    try:
        blocks = list(blocks or [])
    except Exception:
        blocks = []
    if not blocks:
        # No blocks → never silently drop: send the fallback as plain text.
        if not _send_slack_chunked(f"{fallback_text}\n— {datetime.now(PT).strftime('%H:%M PT')}"):
            logger.warning(f"send_slack_blocks failed (text fallback): {fallback_text[:120]}")
        return
    CHUNK = 45   # headroom under Slack's 50-blocks/message hard limit
    groups = [blocks[i:i + CHUNK] for i in range(0, len(blocks), CHUNK)]
    n = len(groups)
    for i, grp in enumerate(groups, 1):
        text = fallback_text if n == 1 else f"{fallback_text} ({i}/{n})"
        if not _post_slack_payload({"text": text, "blocks": grp}):
            logger.warning(f"send_slack_blocks failed (part {i}/{n}): {fallback_text[:120]}")
        if i < n:
            time.sleep(0.4)                 # preserve delivery order; avoid a rate burst


def _send_slack_chunked(text: str) -> bool:
    """Send `text` as 1+ Slack messages, chunked so nothing truncates. When >1 part, each is
    prefixed '(i/N)' and sent IN ORDER (short sleep between). Returns True iff EVERY part
    posted, so a caller's failure logging still fires if any part drops. Never raises."""
    if not _SLACK_WEBHOOK:
        return False
    text = _mobile_clean(text)   # WS2 chokepoint: every outbound message is mobile-clean (idempotent)
    parts = _chunk_text(text)
    n = len(parts)
    ok = True
    for i, part in enumerate(parts, 1):
        prefix = f"({i}/{n})\n" if n > 1 else ""
        if not _post_slack_text(prefix + part):
            ok = False
        if i < n:
            time.sleep(0.4)                 # preserve delivery order; avoid a rate burst
    return ok


def _slack(title: str, body: str, emoji: str = ":chart_with_upwards_trend:") -> bool:
    """POST to Slack incoming webhook (auto-chunked so long bodies never truncate). True on success."""
    if not _SLACK_WEBHOOK:
        return False
    title = _sanitize(title)
    body  = _sanitize(body)
    return _send_slack_chunked(f"{emoji} *{title}*\n{body}")


def _send(title: str, body: str, priority: int = 3, tags: list | None = None, emoji: str = ":robot_face:") -> None:
    """
    Fire alert to all configured transports.
    Always logs — transports are best-effort (never raise).
    """
    ts = datetime.now(PT).strftime("%H:%M PT")
    full_body = f"{body}\n— {ts}"

    ntfy_ok  = _ntfy(title, full_body, priority, tags)
    slack_ok = _slack(title, full_body, emoji)

    if not ntfy_ok and not slack_ok:
        if _NTFY_TOPIC or _SLACK_WEBHOOK:
            logger.warning(f"[ALERT FAILED] {title}: {body}")
        else:
            logger.debug(f"[ALERT no-op — no transport configured] {title}: {body}")


def send_slack(message: str) -> None:
    """
    Send a pre-formatted Slack message directly.
    Used by main.py for ad-hoc operational alerts (watchdog, partial fail, GTC cancel).
    Falls back to logger.warning if Slack is not configured.
    """
    if not _SLACK_WEBHOOK:
        logger.warning(f"[ALERT no-op] {message[:120]}")
        return
    message = _sanitize(message)
    ts = datetime.now(PT).strftime("%H:%M PT")
    if not _send_slack_chunked(f"{message}\n— {ts}"):
        logger.warning(f"send_slack failed (one or more parts): {message[:120]}")


# ── Public API ───────────────────────────────────────────────────────────────

def alert_entry(symbol: str, direction: str, shares: int, price: float,
                score: int, size_mult: float,
                overnight: bool = False, spy_ath_dist_pct: float | None = None,
                mri_level: str | None = None) -> None:
    """Fire on confirmed entry order submission."""
    dir_arrow = "▲ LONG" if direction == "long" else "▼ SHORT"
    title = f"ENTRY {dir_arrow} {symbol}"
    _ctx_parts = [f"Score: {score}/12", f"Size mult: {size_mult:.2f}x"]
    if overnight:
        _ctx_parts.append("🌙 FORCED OVERNIGHT")
    if spy_ath_dist_pct is not None:
        _ath_flag = f"⚠️ ATH-{spy_ath_dist_pct:.1f}%" if spy_ath_dist_pct < 2.0 else f"ATH-{spy_ath_dist_pct:.1f}%"
        _ctx_parts.append(_ath_flag)
    if mri_level:
        _ctx_parts.append(f"MRI:{mri_level}")
    body  = (
        f"{shares} shares @ ${price:.2f}\n"
        f"{' | '.join(_ctx_parts)}"
    )
    tags = ["arrow_up" if direction == "long" else "arrow_down", "moneybag"]
    _send(title, body, priority=4, tags=tags,
          emoji=":rocket:" if direction == "long" else ":chart_with_downwards_trend:")


def alert_exit(symbol: str, direction: str, pnl: float, reason: str,
               tqi: int | None = None, unverified: bool = False) -> None:
    """Fire on full position close.

    unverified=True (2026-09-03): the close fill could not be recovered inside the exit
    poll budget, so `pnl` is the entry_price fallback (~$0.00), NOT the real P&L — the
    fill reconciler patches the true number within the RC-4 window. Render "P&L pending
    reconciliation" instead of a definitive "+$0.00" so a real (possibly losing) exit is
    never shown to the operator as a flat/scratch trade. Display-only, non-risk-path; the
    default False keeps every non-opted-in caller byte-identical."""
    tqi_str  = f" | TQI: {tqi}/100" if tqi is not None else ""
    if unverified:
        title = f"EXIT {symbol} — P&L pending reconciliation"
        body  = (
            f"Reason: {reason}{tqi_str}\nDirection: {direction}\n"
            f"⏳ Fill unverified — real P&L is being reconciled (this is NOT $0.00)."
        )
        _send(title, body, priority=4, tags=["hourglass_flowing_sand", "door"],
              emoji=":hourglass_flowing_sand:")
        return
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_tag  = "white_check_mark" if pnl >= 0 else "x"
    title = f"EXIT {symbol} {pnl_sign}${pnl:.2f}"
    body  = f"Reason: {reason}{tqi_str}\nDirection: {direction}"
    priority = 4 if abs(pnl) >= 20 else 3
    _send(title, body, priority=priority, tags=[pnl_tag, "door"],
          emoji=":white_check_mark:" if pnl >= 0 else ":x:")


def alert_partial(symbol: str, tranche: int, pnl: float, qty: int, price: float,
                  unverified: bool = False) -> None:
    """Fire on partial tranche close.

    unverified=True: the partial-close fill was not recovered, so `pnl` is a fallback
    (~$0.00), not real — show "pending reconciliation" instead of a definitive $0.00
    (mirrors alert_exit). Default False keeps existing callers byte-identical."""
    title = f"PARTIAL T{tranche} {symbol}"
    _pnl_str = "pending reconciliation" if unverified else f"${pnl:+.2f}"
    body  = f"{qty} shares @ ${price:.2f} | PnL: {_pnl_str}"
    _send(title, body, priority=3, tags=["scissors", "chart_with_upwards_trend"],
          emoji=":scissors:")


def alert_kill_switch(daily_pnl: float, limit_pct: float, portfolio: float) -> None:
    """Fire when daily loss kill switch trips."""
    title = f"{SEV_CRITICAL} — KILL SWITCH TRIPPED"
    body  = (
        f"Daily PnL: ${daily_pnl:+.2f} ({daily_pnl/portfolio:.1%})\n"
        f"Limit: {limit_pct:.0%} of ${portfolio:,.2f}\n"
        f"No new entries this session."
    )
    _send(title, body, priority=5, tags=["rotating_light", "no_entry"],
          emoji=":rotating_light:")


def alert_spy_event(event_type: str, magnitude: float, scans_left: int) -> None:
    """Fire when hybrid engine triggers a new SPY risk event."""
    is_extreme = event_type == "EXTREME"
    # GAI severity refinement: EXTREME and BROAD_* are both CRITICAL (market-wide
    # risk); narrower SECTOR events are WARNING.
    _is_critical = is_extreme or str(event_type).startswith("BROAD")
    title = f"{SEV_CRITICAL if _is_critical else SEV_WARNING} — SPY {event_type}: {magnitude:+.2f}%"
    body  = (
        f"New entries {'ALL BLOCKED' if is_extreme else 'directionally gated'}.\n"
        f"Clears in {scans_left} clean scans."
    )
    priority = 5 if is_extreme else 4
    _send(title, body, priority=priority,
          tags=["rotating_light" if is_extreme else "warning", "chart_with_downwards_trend"],
          emoji=":rotating_light:" if is_extreme else ":warning:")


def alert_venue_halt(venue_status: dict, spy_5m_pct: float, qqq_5m_pct: float) -> None:
    """Fire when Build F's venue-state check confirms a REAL halt (exchange clock
    reports closed mid-session, SPY itself is untradable, or session-cumulative
    SPY decline crosses a real MWCB -7/-13/-20 band). Entries-only block — never
    liquidates; see events/handlers.py:safe_close_all for the (user-shutdown-only)
    mass-close path."""
    title = f"{SEV_CRITICAL} — VENUE HALT CONFIRMED"
    body  = (
        f"is_open={venue_status.get('is_open')} "
        f"SPY_tradable={venue_status.get('spy_tradable')} "
        f"MWCB={venue_status.get('mwcb_band')}\n"
        f"SPY session: {venue_status.get('spy_session_pct', 0):+.2f}% | "
        f"SPY 5m: {spy_5m_pct:+.2f}% | QQQ 5m: {qqq_5m_pct:+.2f}%\n"
        f"New entries BLOCKED this cycle. Existing positions managed by stops. "
        f"No liquidation (mass-close is user-shutdown-only)."
    )
    _send(title, body, priority=5, tags=["rotating_light", "no_entry"],
          emoji=":rotating_light:")


def alert_stop_breach(symbol: str, direction: str, current_price: float,
                      stop: float, gtc_submitted: bool = True) -> None:
    """Fire when stop is breached and position cannot be closed immediately:
    GTC stop placed for next RTH open, or close_position() hard-failed.
    """
    title = f"{SEV_CRITICAL} — STOP BREACH BLOCKED — {symbol}"
    if gtc_submitted:
        action = "GTC stop placed — fires next RTH open."
    else:
        action = "⛔ GTC stop FAILED — MANUAL ACTION REQUIRED in Alpaca app."
    body  = (
        f"Price ${current_price:.2f} breached stop ${stop:.2f}\n"
        f"{action}"
    )
    _send(title, body, priority=5, tags=["warning", "lock"],
          emoji=":warning:")


def alert_crash(reason: str, open_positions: list) -> None:
    """Fire on SIGTERM or unhandled exception — dumps open positions."""
    _sentinel = "/tmp/mtf_planned_restart"
    if os.path.exists(_sentinel):
        try:
            age_s = time.time() - os.path.getmtime(_sentinel)
            if age_s < 300:
                os.remove(_sentinel)
                logger.info("alert_crash: planned restart sentinel — suppressed")
                return
            os.remove(_sentinel)   # stale — clean up but send alert
        except FileNotFoundError:
            logger.debug("alert_crash: no restart sentinel found — expected; proceed with crash alert")
        except OSError as _sentinel_e:
            logger.warning("alert_crash: sentinel stat/remove failed — %s", _sentinel_e)

    title   = f"{SEV_CRITICAL} — BOT SHUTDOWN"
    pos_str = ", ".join(open_positions) if open_positions else "none"
    body    = (
        f"Reason: {reason}\n"
        f"Open positions: {pos_str}\n"
        f"Check Alpaca dashboard immediately."
    )
    # Restore PT timestamp footer (previously injected by _send(); bypassed here
    # because ntfy and Slack are called directly to allow independent throttling).
    ts        = datetime.now(PT).strftime("%H:%M PT")
    full_body = f"{body}\n— {ts}"

    # ntfy (phone): always fires — never throttle phone notification for a crash
    _ntfy(title, full_body, priority=5, tags=["sos", "rotating_light"])

    # Slack: reason-based dedup to prevent SIGKILL restart-cycle spam.
    # JSON state avoids newline-injection issues in reason strings.
    # Fires unless: same crash reason AND elapsed < 60 min since last Slack alert.
    # Different crash reasons (new problem) always fire Slack regardless of timing.
    _crash_flag = _STATE_DIR / "last_crash_slack.json"
    _send_slack = True
    try:
        if _crash_flag.exists():
            state       = json.loads(_crash_flag.read_text(encoding="utf-8"))
            last_reason = state.get("reason", "")
            last_ts     = datetime.fromisoformat(state.get("ts", "2000-01-01T00:00:00+00:00"))
            elapsed     = (datetime.now(timezone.utc) - last_ts).total_seconds()
            if last_reason == reason and elapsed < 3600:
                _send_slack = False
                logger.critical(
                    f"alert_crash: Slack suppressed — same reason '{reason}' "
                    f"within {elapsed/60:.0f} min. ntfy sent. Check phone."
                )
    except Exception as e:
        logger.warning(f"alert_crash: crash-flag read failed — {e} — sending Slack anyway")

    if _send_slack:
        slack_ok = _slack(title, full_body, emoji=":sos:")
        if slack_ok:
            _atomic_write(_crash_flag, {
                "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            })


def alert_stale_bar(symbol: str, age_min: float) -> None:
    """Log stale bar skip — entry already blocked upstream. Log-only (no Slack/ntfy)."""
    logger.info(f"[STALE BAR] {symbol}: last 15m bar is {age_min:.0f} min old — entry skipped.")


def alert_gtc_failed(symbol: str, side: str, stop_px: float, reason: str) -> None:
    """Fire when an overnight GTC stop order cannot be placed — position is unprotected."""
    title = f"{SEV_CRITICAL} — GTC STOP FAILED — {symbol}"
    body  = (
        f"Could not place {side.upper()} stop @ ${stop_px:.2f}.\n"
        f"Reason: {reason}\n"
        f"Position is UNPROTECTED overnight. Manual action required."
    )
    _send(title, body, priority=5, tags=["sos", "rotating_light", "stop_sign"],
          emoji=":rotating_light:")


def alert_floor_blind(symbol: str, tier: str, kind: str, detail: str, critical: bool) -> bool:
    """Operator page when the never-sell floor GUARD fails CLOSED on AMBIGUITY — it cannot
    read the ownership ledger, cannot read the live Alpaca net qty, detects ledger↔Alpaca
    drift, or hits a type-corrupt ledger value. A refused protected exit is a capital-risk
    fault, so this fires the PHONE (ntfy) too, not Slack-only (mirrors alert_crash).

    TRANSPORT ONLY — returns True on a CONFIRMED send (ntfy OR Slack ACK). Throttling and
    the never-raises contract are the CALLER's responsibility
    (execution.ownership_guard.page_floor_blind), which stamps its dedup only when this
    returns True — so a failed delivery retries next time instead of being swallowed.
    """
    sev = SEV_CRITICAL if critical else SEV_WARNING
    title = f"{sev} — NEVER-SELL GUARD BLIND — {symbol}"
    body = (
        f"Guard failed CLOSED on {symbol} [{tier}] — {kind}.\n"
        f"{detail}\n"
        f"A protected exit may be refused. Check Alpaca/ledger connectivity; "
        f"run sync_ledger to heal if drift/corruption."
    )
    prio = 5 if critical else 4
    ntfy_ok = _ntfy(title, body, priority=prio,
                    tags=["rotating_light" if critical else "warning", "lock"])
    slack_ok = _slack(title, body, emoji=":rotating_light:" if critical else ":warning:")
    return bool(ntfy_ok or slack_ok)


def alert_startup_test() -> bool:
    """
    Fire a low-priority ping to all configured transports at bot startup.
    Returns True if at least one transport ACKs successfully.
    Used to confirm push delivery before the trading session begins.
    """
    title = "✅ MTF Bot Online"
    body  = "Alert system self-test — push delivery confirmed."
    ntfy_ok  = _ntfy(title, body, priority=2, tags=["white_check_mark"])
    slack_ok = _slack(title, body, emoji=":white_check_mark:")
    return ntfy_ok or slack_ok


def alert_systemic_stale_feed(median_age: float, cycles: int) -> None:
    """Fire when median bar age > 20 min for 3 consecutive cycles — systemic yfinance degradation."""
    title = f"{SEV_CRITICAL} — SYSTEMIC FEED DEGRADATION"
    body  = (
        f"Median bar age {median_age:.0f} min > 20 min threshold\n"
        f"for {cycles} consecutive cycles — yfinance feed degraded.\n"
        f"All stale-bar entries are blocked. Check Alpaca data feed status."
    )
    _send(title, body, priority=4, tags=["satellite", "warning", "chart_with_downwards_trend"],
          emoji=":satellite:")

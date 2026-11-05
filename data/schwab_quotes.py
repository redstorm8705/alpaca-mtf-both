# ruff: noqa: E501
"""
data/schwab_quotes.py
Read-only full-market (NBBO) quotes from the owner's Schwab account — Schwab Market Data API.

Approval-ID: SCHWAB-QUOTES-1103. Design: logs/design_records/schwab_quotes_2026-11-03.md (rev 3).
Data tier: T1b (Schwab Market Data quotes endpoint). Bars stay Alpaca T1 (data/fetcher.py).

What this module does and does not do:
  - It READS quotes. It never places, changes or cancels an order and it imports nothing from
    execution/ (a unit test inspects this file's imports and fails otherwise).
  - A quote from here is a GATE INPUT ONLY. It never reaches a stop/target, record_exit, P&L,
    kill-switch equity or the entry price of record (that stays the Alpaca fill).
  - Every failure returns no quote for the symbol, with a reason in last_reasons(); the caller
    keeps its existing source. No exception escapes nbbo().

Public API:
  nbbo(symbols, max_age_s) -> dict[str, Quote]   keyed by the ALPACA symbol
  last_reasons() -> dict[str, str]                fallback reason per symbol for the last nbbo() call
  index_last_with_reasons(symbols, max_age_s, deadline_s) -> (dict[str, IndexQuote], dict[str, str])
                                                  CBOE index values ($VIX, $VIX3M, ...) — REGIME-SIZE-1104 rev 3.6
  status() -> dict                                operator view (no secrets)

Plain `requests`, no OAuth library. HTTP calls use the module attribute `requests.get/post`
(the unit tests patch it).
"""

import base64
import json
import logging
import math
import os
import re
import statistics
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from data.alpaca_data import _TIMEOUT as _ALPACA_QUOTE_TIMEOUT

try:
    import fcntl
except ImportError:  # pragma: no cover — the bot runs on Linux/macOS; fcntl is POSIX-only
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# ── Kill flag ─────────────────────────────────────────────────────────────────
# False → nbbo() returns {} with reason "disabled" for every symbol; callers use their current source.
SCHWAB_QUOTES_ENABLED = True
# Per-feature kill for index values (REGIME-SIZE-1104 rev 3.5 item 7). False → index_last_with_reasons()
# returns reason "disabled" for every symbol without any network call; the caller uses its next tier.
SCHWAB_INDEX_ENABLED = True

# ── Endpoints ─────────────────────────────────────────────────────────────────
_API = "https://api.schwabapi.com"
TOKEN_URL = f"{_API}/v1/oauth/token"
AUTHORIZE_URL = f"{_API}/v1/oauth/authorize"
ACCOUNTS_URL = f"{_API}/trader/v1/accounts/accountNumbers"
QUOTES_URL = f"{_API}/marketdata/v1/quotes"
REDIRECT_URI = "https://127.0.0.1"

# ── State / log paths (anchored to the repo, never CWD; read at call time so tests can redirect) ──
_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = _ROOT / "data" / "state"
TOKEN_FILE = STATE_DIR / "schwab_token.json"
LOCK_FILE = STATE_DIR / "schwab_token.lock"
KILL_FILE = STATE_DIR / "schwab_kill.json"
BUDGET_FILE = STATE_DIR / "schwab_budget.json"
PAGE_DEDUPE_FILE = STATE_DIR / "schwab_page_dedupe.json"
REVOKED_FILE = STATE_DIR / "schwab_revoked.json"   # set by --revoke; only a successful login clears it

# Schwab refresh tokens live 7 days and cannot be extended (vendor fact, third-party docs). The login
# reminder goes out once the remaining life is below one day.
REFRESH_LIFE_S = 7 * 86400
REFRESH_REMIND_S = 86400
QUOTE_LOG = _ROOT / "logs" / "schwab_quotes.jsonl"

_FILE_MODE = 0o600

# ── Response shape — UNVERIFIED ASSUMPTION ────────────────────────────────────
# Field names below come from third-party client libraries and docs, NOT from a live Schwab
# response or Schwab's own published schema: body = {"<SCHWAB SYMBOL>": {"symbol": str,
# "quote": {"bidPrice", "askPrice", "bidSize", "askSize", "lastPrice", "quoteTime" (epoch ms)}}},
# plus an optional top-level "errors" object (e.g. {"invalidSymbols": [...]}) that is not a symbol.
# The runtime shape check below catches a mismatch on the first live call (→ fallback + daily page).
_QUOTE_FIELDS = ("bidPrice", "askPrice", "bidSize", "askSize", "lastPrice", "quoteTime")
_NON_SYMBOL_KEYS = ("errors",)

# ── Rate-limit headers — UNVERIFIED ASSUMPTION ────────────────────────────────
# Schwab's own docs were not checked; these are the conventional names. When none is present the
# budget uses the most conservative limit seen in any previous response; if none was ever seen it
# allows one batch per nbbo() call and logs BUDGET_UNKNOWN.
_LIMIT_HEADERS = ("X-RateLimit-Limit", "RateLimit-Limit", "X-Rate-Limit-Limit")
_REMAINING_HEADERS = ("X-RateLimit-Remaining", "RateLimit-Remaining", "X-Rate-Limit-Remaining")
_RETRY_AFTER_HEADER = "Retry-After"
# Unit of the reported limit: third parties report "120 requests per minute" [unverified].
_LIMIT_WINDOW_S = 60.0

# ── Timeouts: derived from this module's own observed latencies ───────────────
# Cold start = the Alpaca quote timeout in use today (data/alpaca_data.py _TIMEOUT), for both
# connect and read. Once enough successful calls are observed: read = max(observed) + median
# (headroom of one typical call), never above the cold-start value (the trading thread must not
# wait longer than it does today). A timeout clears the history → back to cold start.
_LAT_WINDOW = 50          # rolling sample of recent successful call latencies
_LAT_MIN_SAMPLES = 5      # samples needed before the derived timeout replaces the cold start
_latencies: deque = deque(maxlen=_LAT_WINDOW)
_refresh_latencies: deque = deque(maxlen=_LAT_WINDOW)

_mem_lock = threading.Lock()
_last_reasons: dict = {}
_known_secrets: set = set()


@dataclass(frozen=True)
class Quote:
    """One validated Schwab NBBO quote. Gate input only — never a price of record."""
    symbol: str            # Alpaca symbol
    schwab_symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    last: float
    quote_time: datetime   # tz-aware UTC (from Schwab epoch ms)
    fetch_time: datetime   # tz-aware UTC, when the response arrived
    age_s: float           # fetch_time - quote_time
    max_age_s: float       # caller's freshness bound used
    latency_s: float
    spread: float          # ask - bid (0.0 when locked)
    locked: bool
    source: str = "schwab_nbbo"


# ══════════════════════════════════════════════════════════════════════════════
# Redaction
# ══════════════════════════════════════════════════════════════════════════════
_REDACT_PATTERNS = (
    (re.compile(r"(code=)[^&\s'\"]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"((?:access_token|refresh_token|id_token|client_secret)['\"]?\s*[:=]\s*['\"]?)[^'\"&\s,}]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(Authorization['\"]?\s*[:=]\s*['\"]?)(?:(?:Bearer|Basic)\s+)?[^'\"\s,}]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I), r"\1 [REDACTED]"),
)


def _remember_secret(value) -> None:
    if isinstance(value, str) and len(value) > 1:
        with _mem_lock:
            _known_secrets.add(value)


def redact(text) -> str:
    """Strip code=, access/refresh/id tokens, Authorization values, the app secret/key and any
    token value this process has handled. Never raises; returns a string."""
    try:
        s = text if isinstance(text, str) else str(text)
        for pat, repl in _REDACT_PATTERNS:
            s = pat.sub(repl, s)
        with _mem_lock:
            secrets_ = set(_known_secrets)
        for env in ("SCHWAB_APP_SECRET", "SCHWAB_APP_KEY"):
            v = os.getenv(env, "")
            if len(v) > 1:
                secrets_.add(v)
        # the Basic-auth form of the pair is also a secret
        key, sec = os.getenv("SCHWAB_APP_KEY", ""), os.getenv("SCHWAB_APP_SECRET", "")
        if key and sec:
            secrets_.add(base64.b64encode(f"{key}:{sec}".encode()).decode())
        for v in sorted(secrets_, key=len, reverse=True):
            if v in s:
                s = s.replace(v, "[REDACTED]")
        return s
    except Exception:
        return "[REDACTION-FAILED]"


class _RedactFilter(logging.Filter):
    """Belt and braces: every record from this module's logger is redacted, including exception
    text. Tracebacks are flattened into the redacted message (never emitted raw)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if record.exc_info:
                import traceback
                msg = msg + "\n" + "".join(traceback.format_exception(*record.exc_info))
                record.exc_info = None
                record.exc_text = None
            record.msg = redact(msg)
            record.args = ()
        except Exception:
            record.msg = "[REDACTION-FAILED]"
            record.args = ()
        return True


logger.addFilter(_RedactFilter())


def _err(e: BaseException) -> str:
    """Error class + redacted message (for logs). Slack gets the class only."""
    return f"{type(e).__name__}: {redact(e)}"


@dataclass(frozen=True)
class IndexQuote:
    """One validated CBOE index value (e.g. $VIX). Gate / regime input only — never a price of record."""
    symbol: str            # Schwab index symbol, e.g. "$VIX"
    last: float
    trade_time: datetime   # tz-aware UTC (from Schwab tradeTime epoch ms) — the as-of time
    fetch_time: datetime   # tz-aware UTC, when the response arrived
    age_s: float           # fetch_time - trade_time, corrected for a lagging local clock
    max_age_s: float       # caller's freshness bound used
    latency_s: float
    source: str = "schwab_index"


# ══════════════════════════════════════════════════════════════════════════════
# Symbol map  Alpaca <-> Schwab
# ══════════════════════════════════════════════════════════════════════════════
# Share-class separator: Alpaca "." ↔ Schwab "/" (BRK.B ↔ BRK/B) [hypothesis — confirmed against
# the live response on first login]. Strictly one-to-one: an Alpaca symbol containing "/" (crypto
# pairs) or a Schwab symbol containing "." is unmapped, so the two directions cannot collide.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+([./][A-Z0-9]+)?$")


def to_schwab(alpaca_symbol: str):
    """Alpaca symbol → Schwab symbol, or None if unmapped."""
    s = str(alpaca_symbol or "").strip().upper()
    if "/" in s or not _SYMBOL_RE.match(s):
        return None
    return s.replace(".", "/")


def to_alpaca(schwab_symbol: str):
    """Schwab symbol → Alpaca symbol, or None if unmapped."""
    s = str(schwab_symbol or "").strip().upper()
    if "." in s or not _SYMBOL_RE.match(s):
        return None
    return s.replace("/", ".")


# ══════════════════════════════════════════════════════════════════════════════
# Files: atomic mode-600 writes + the shared lock
# ══════════════════════════════════════════════════════════════════════════════
def _atomic_write_json(path: Path, obj: dict) -> None:
    """tmp created O_WRONLY|O_CREAT|O_EXCL mode 600 → fsync → os.replace → verify mode 600.
    Raises on failure (callers inside nbbo() catch)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    mode = os.stat(str(path)).st_mode & 0o777
    if mode != _FILE_MODE:
        os.chmod(str(path), _FILE_MODE)
        mode = os.stat(str(path)).st_mode & 0o777
        if mode != _FILE_MODE:
            raise PermissionError(f"{path.name} mode {oct(mode)} is not 600")


def _read_json(path: Path):
    try:
        with open(str(path)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"SCHWAB state read failed ({Path(path).name}): {_err(e)}")
        return None


def _unlink(path: Path) -> None:
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        pass


class _LockTimeout(Exception):
    pass


def _lock_deadline_s() -> float:
    """How long to wait for another process's token/budget cycle: the holder can spend at most a
    refresh call plus a scope-proof call, each bounded by the current (latency-derived) timeouts."""
    connect, read = _timeouts("token")
    return 2.0 * (connect + read)


class _FileLock:
    """fcntl.flock on LOCK_FILE, acquired with a deadline. A lock held by a crashed process is
    released by the kernel when it exits. Separate opens conflict (also across threads)."""

    def __init__(self, deadline_s=None):
        self.deadline_s = _lock_deadline_s() if deadline_s is None else deadline_s
        self.fd = None

    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, _FILE_MODE)
        if fcntl is None:  # pragma: no cover
            return self
        end = time.monotonic() + self.deadline_s
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as e:
                if not isinstance(e, (BlockingIOError, PermissionError)):
                    os.close(self.fd)
                    self.fd = None
                    raise
                if time.monotonic() >= end:
                    os.close(self.fd)
                    self.fd = None
                    raise _LockTimeout() from None
                time.sleep(0.02)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Day helpers, kill state, Slack page dedupe
# ══════════════════════════════════════════════════════════════════════════════
def _et_today() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _kill_reason():
    """Reason string if the kill state is active for today's ET date, else None."""
    k = _read_json(KILL_FILE)
    if k and k.get("date") == _et_today():
        return str(k.get("reason") or "killed")
    return None


def _write_kill(reason: str) -> None:
    try:
        _atomic_write_json(KILL_FILE, {"date": _et_today(), "reason": reason,
                                       "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        logger.error(f"SCHWAB kill state write failed: {_err(e)}")


def _send_slack(text: str) -> bool:
    """Lazy alerts import (keeps this module's import graph free of the alert stack). True only on
    confirmed delivery (alerts.send_slack swallows failures, so it cannot be used for dedupe)."""
    from alerts import send_slack_delivery
    return bool(send_slack_delivery(text))


def _page(kind: str, text: str) -> None:
    """Slack once per ET day per kind (state-file dedupe). Never raises."""
    try:
        today = _et_today()
        d = _read_json(PAGE_DEDUPE_FILE) or {}
        if d.get("date") != today:
            d = {"date": today, "kinds": []}
        if kind in d.get("kinds", []):
            return
        if not _send_slack(redact(text)):   # a failed delivery must not mark the page as sent
            logger.warning(f"SCHWAB page '{kind}' not delivered — will retry on the next trigger")
            return
        d.setdefault("kinds", []).append(kind)
        _atomic_write_json(PAGE_DEDUPE_FILE, d)
    except Exception as e:
        logger.warning(f"SCHWAB page '{kind}' failed: {_err(e)}")


# ══════════════════════════════════════════════════════════════════════════════
# Timeouts + latency history
# ══════════════════════════════════════════════════════════════════════════════
def _timeouts(kind: str = "quote"):
    """(connect, read) seconds for a quote call, or for a token/scope call (kind="token", derived from
    the refresh-call latencies). Cold start = the Alpaca quote timeout; then derived (see header)."""
    cold = float(_ALPACA_QUOTE_TIMEOUT)
    with _mem_lock:
        hist = list(_refresh_latencies if kind == "token" else _latencies)
    if len(hist) < _LAT_MIN_SAMPLES:
        return cold, cold
    derived = min(cold, max(hist) + statistics.median(hist))
    return derived, derived


def _record_latency(hist: deque, seconds: float) -> None:
    with _mem_lock:
        hist.append(float(seconds))


def _on_timeout() -> None:
    with _mem_lock:
        _latencies.clear()
        _refresh_latencies.clear()


def _is_timeout(e: BaseException) -> bool:
    return "Timeout" in type(e).__name__


# ══════════════════════════════════════════════════════════════════════════════
# Token: read / write / refresh / scope proof
# ══════════════════════════════════════════════════════════════════════════════
def _app_creds():
    key = os.getenv("SCHWAB_APP_KEY", "").strip()
    sec = os.getenv("SCHWAB_APP_SECRET", "").strip()
    return key, sec


def read_token():
    tok = _read_json(TOKEN_FILE)
    if tok:
        _remember_secret(tok.get("access_token"))
        _remember_secret(tok.get("refresh_token"))
        _remember_secret(tok.get("id_token"))
    return tok


def write_token(tok: dict) -> None:
    """Atomic mode-600 write of the token file. Caller should hold _FileLock."""
    _remember_secret(tok.get("access_token"))
    _remember_secret(tok.get("refresh_token"))
    _remember_secret(tok.get("id_token"))
    _atomic_write_json(TOKEN_FILE, tok)


def token_from_response(body: dict, prior=None) -> dict:
    """Build the stored token from a Schwab token-endpoint response.
    Field names (access_token, refresh_token, expires_in, id_token) are UNVERIFIED against Schwab's
    own docs (taken from OAuth2 convention + third-party clients). A refresh that omits
    refresh_token keeps the prior one and its issue time."""
    now = time.time()
    prior = prior or {}
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise ValueError("token response has no access_token")
    expires_in = body.get("expires_in")
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) or expires_in <= 0:
        raise ValueError("token response has no usable expires_in")
    new_refresh = body.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh and new_refresh != prior.get("refresh_token"):
        refresh, issued = new_refresh, now
    else:
        refresh, issued = prior.get("refresh_token"), prior.get("refresh_issued_at", now)
    if not refresh:
        raise ValueError("token response has no refresh_token")
    return {"access_token": access, "refresh_token": refresh, "token_type": body.get("token_type", "Bearer"),
            "access_expires_at": now + float(expires_in), "refresh_issued_at": issued,
            "scope_ok": False, "refresh_dead": False, "written_at": now}


def scope_proof(access_token: str) -> str:
    """Call the trader accounts endpoint read-only; Schwab must refuse it (401/403).
    Returns "refused", "ALLOWED" (HTTP 200 — the key can see the account) or "error:<status|class>".
    On ALLOWED: deletes the token, writes the kill state, pages Slack once. Never raises, never exits."""
    connect, read = _timeouts("token")
    try:
        resp = requests.get(ACCOUNTS_URL, headers={"Authorization": f"Bearer {access_token}"},
                            timeout=(connect, read))
        code = int(resp.status_code)
    except Exception as e:
        if _is_timeout(e):
            _on_timeout()
        logger.warning(f"SCHWAB scope proof call failed: {_err(e)}")
        return f"error:{type(e).__name__}"
    if code in (401, 403):
        logger.info(f"SCHWAB_SCOPE_PROOF refused HTTP {code} (expected: key cannot reach the account)")
        return "refused"
    if code == 200:
        logger.critical("SCHWAB_SCOPE_PROOF FAILED: accounts endpoint answered HTTP 200 — token deleted, module disabled")
        _write_kill("scope_allowed")   # kill first: a failed delete must not leave the module live
        try:
            _unlink(TOKEN_FILE)
        except OSError as e:
            logger.critical(f"SCHWAB token delete after failed scope proof FAILED: {_err(e)} — module stays killed")
        _page("scope_allowed", "🚨 CRITICAL Schwab quotes: scope proof FAILED — the accounts endpoint answered HTTP 200. "
                               "Token deleted and Schwab quotes disabled; the bot uses its existing quotes. "
                               "Follow the revoke runbook (logs/design_records/schwab_quotes_2026-11-03.md).")
        return "ALLOWED"
    logger.warning(f"SCHWAB scope proof inconclusive: HTTP {code}")
    return f"error:{code}"


def _refresh_locked(tok: dict):
    """POST grant_type=refresh_token. Caller holds the lock. Returns (new_tok, reason)."""
    key, sec = _app_creds()
    connect, read = _timeouts("token")
    t0 = time.monotonic()
    try:
        resp = requests.post(TOKEN_URL, auth=(key, sec),
                             data={"grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", "")},
                             headers={"Content-Type": "application/x-www-form-urlencoded"},
                             timeout=(connect, read))
        code = int(resp.status_code)
    except Exception as e:
        if _is_timeout(e):
            _on_timeout()
        logger.warning(f"SCHWAB token refresh call failed: {_err(e)}")
        return None, "refresh_error"
    _record_latency(_refresh_latencies, time.monotonic() - t0)
    if code != 200:
        logger.error(f"SCHWAB token refresh HTTP {code}")
        if code in (400, 401):
            # refresh token rejected (7-day life ended or revoked) — stop calling until the next login
            try:
                if not REVOKED_FILE.exists():   # never write a token back after a revoke
                    dead = dict(tok)
                    dead["refresh_dead"] = True
                    write_token(dead)
            except Exception as e:
                logger.error(f"SCHWAB token mark-dead write failed: {_err(e)}")
            _page("refresh_dead", f"⚠️ Schwab quotes: token refresh refused (HTTP {code}). The bot uses its existing "
                                  f"quotes. Log in again: ssh mtf-bot \"cd mtf-bot && venv/bin/python3 scripts/schwab_login.py\"")
        return None, f"refresh_http_{code}"
    try:
        new_tok = token_from_response(resp.json(), prior=tok)
    except Exception as e:
        logger.error(f"SCHWAB token refresh response unusable: {_err(e)}")
        return None, "refresh_shape"
    if REVOKED_FILE.exists():   # revoked while this refresh was in flight: never write it back
        logger.warning("SCHWAB token refresh finished after a revoke — discarded")
        return None, "revoked"
    write_token(new_tok)
    logger.info("SCHWAB token refreshed (access token); running scope proof")
    return new_tok, None


def _ensure_token_locked():
    """Caller holds the lock. Expiry check → refresh → write → scope proof → write.
    Returns (access_token, None) or (None, reason)."""
    if REVOKED_FILE.exists():
        return None, "revoked"
    tok = read_token()
    if not tok or not tok.get("refresh_token"):
        return None, "no_token"
    issued = float(tok.get("refresh_issued_at") or 0)
    if issued and REFRESH_LIFE_S - (time.time() - issued) < REFRESH_REMIND_S:
        _page("refresh_expiring", "⏰ Schwab quotes: the weekly login expires within a day. Run: "
                                  "ssh mtf-bot \"cd mtf-bot && venv/bin/python3 scripts/schwab_login.py\"")
    if tok.get("refresh_dead"):
        return None, "refresh_dead"
    connect, read = _timeouts()
    margin = connect + read  # the access token must outlive the quote call that follows
    if float(tok.get("access_expires_at") or 0) - margin <= time.time() or not tok.get("access_token"):
        tok, reason = _refresh_locked(tok)
        if reason:
            return None, reason
    if not tok.get("scope_ok"):
        proof = scope_proof(tok["access_token"])
        if proof == "ALLOWED":
            return None, "scope_allowed"
        if proof != "refused":
            return None, "scope_unproven"
        if REVOKED_FILE.exists():
            return None, "revoked"
        tok = dict(tok)
        tok["scope_ok"] = True
        tok["scope_proven_at"] = time.time()
        write_token(tok)
    return tok["access_token"], None


# ══════════════════════════════════════════════════════════════════════════════
# Shared request budget (token bucket; state file under the same lock)
# ══════════════════════════════════════════════════════════════════════════════
def _budget_capacity(b: dict):
    last = b.get("last_limit")
    if isinstance(last, (int, float)) and last > 0:
        return float(last)
    seen = b.get("min_limit_seen")
    if isinstance(seen, (int, float)) and seen > 0:
        return float(seen)
    return None


def _take_budget_locked():
    """Caller holds the lock. Returns (allowed, reason)."""
    now = time.time()
    b = _read_json(BUDGET_FILE) or {}
    if float(b.get("blocked_until") or 0) > now:
        return False, "rate_backoff"
    cap = _budget_capacity(b)
    if cap is None:
        logger.info("SCHWAB BUDGET_UNKNOWN: no rate-limit header seen yet — allowing one batch for this call")
        return True, None
    tokens = float(b.get("tokens", cap))
    updated = float(b.get("updated") or now)
    tokens = min(cap, tokens + max(0.0, now - updated) * cap / _LIMIT_WINDOW_S)
    if tokens < 1:
        b.update(tokens=tokens, updated=now)
        _atomic_write_json(BUDGET_FILE, b)
        return False, "rate_budget"
    b.update(tokens=tokens - 1, updated=now)
    _atomic_write_json(BUDGET_FILE, b)
    return True, None


def _header_num(headers, names):
    for n in names:
        try:
            v = headers.get(n) if headers is not None else None
        except Exception:
            v = None
        if v is None:
            continue
        try:
            return float(str(v).split(",")[0].strip())
        except ValueError:
            continue
    return None


def _update_budget_locked(headers, status_code: int) -> None:
    now = time.time()
    b = _read_json(BUDGET_FILE) or {}
    limit = _header_num(headers, _LIMIT_HEADERS)
    b["last_limit"] = limit if (limit and limit > 0) else None
    if limit and limit > 0:
        prev = b.get("min_limit_seen")
        b["min_limit_seen"] = min(float(prev), limit) if isinstance(prev, (int, float)) and prev > 0 else limit
    cap = _budget_capacity(b)
    remaining = _header_num(headers, _REMAINING_HEADERS)
    if cap is not None:
        tokens = float(b.get("tokens", cap))
        if remaining is not None:
            tokens = min(tokens, remaining)
        b["tokens"] = min(cap, tokens)
        b["updated"] = now
    if status_code == 429:
        today = _et_today()
        c = b.get("count_429") or {}
        if c.get("date") != today:
            c = {"date": today, "n": 0}
        c["n"] = int(c.get("n", 0)) + 1
        b["count_429"] = c
        retry = _header_num(headers, (_RETRY_AFTER_HEADER,))
        if retry is not None and retry > 0:
            b["blocked_until"] = now + retry
        elif cap is not None:
            b["tokens"] = 0.0
            b["updated"] = now
        else:
            b["blocked_until"] = now + _LIMIT_WINDOW_S
    _atomic_write_json(BUDGET_FILE, b)


# ══════════════════════════════════════════════════════════════════════════════
# Shape check, validity, quote log
# ══════════════════════════════════════════════════════════════════════════════
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _shape_ok(body) -> bool:
    if not isinstance(body, dict):
        return False
    for key, entry in body.items():
        if key in _NON_SYMBOL_KEYS:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("quote"), dict):
            return False
        if entry.get("symbol") != key:
            return False
        q = entry["quote"]
        if not all(_is_num(q.get(f)) for f in _QUOTE_FIELDS):
            return False
    return True


def crc_line(body: dict) -> str:
    """JSON line = body + crc (CRC32 of the canonical JSON of body)."""
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    out = dict(body)
    out["crc"] = zlib.crc32(canon.encode()) & 0xFFFFFFFF
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def verify_line(line: str) -> bool:
    """True iff a quote-log line parses and its crc matches its body."""
    try:
        d = json.loads(line)
        crc = d.pop("crc")
        canon = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return int(crc) == (zlib.crc32(canon.encode()) & 0xFFFFFFFF)
    except Exception:
        return False


def _append_quote_lines(lines: list) -> None:
    if not lines:
        return
    try:
        QUOTE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(str(QUOTE_LOG), "a") as f:
            for ln in lines:
                f.write(ln + "\n")
            f.flush()
    except Exception as e:
        logger.warning(f"SCHWAB quote log append failed: {_err(e)}")


def _validate(q: dict):
    """Returns (reason or None, locked)."""
    bid, ask = float(q["bidPrice"]), float(q["askPrice"])
    bsz, asz = float(q["bidSize"]), float(q["askSize"])
    last = float(q["lastPrice"])
    if not all(math.isfinite(v) for v in (bid, ask, bsz, asz, last)):
        return "non_finite", False
    if last <= 0:
        return "bad_last", False
    if bid <= 0 or ask <= 0:
        return "one_sided", False
    if bsz <= 0 or asz <= 0:
        return "zero_size", False
    if bid > ask:
        return "crossed", False
    return None, bid == ask


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════
def last_reasons() -> dict:
    """Fallback reason per symbol from the most recent nbbo() call (symbols that got a quote are absent)."""
    with _mem_lock:
        return dict(_last_reasons)


def nbbo_with_reasons(symbols, max_age_s: dict, deadline_s=None):
    """(quotes, reasons) for THIS call — reasons are not shared across threads like last_reasons().
    With `deadline_s`, the call runs in a worker thread and returns ({}, {sym: "deadline"}) when it
    does not finish in time (the caller never waits longer; the worker finishes on its own).
    Callers on the 5-minute trading-cycle thread must pass a deadline."""
    holder: dict = {}
    try:
        syms = [str(s).strip().upper() for s in list(symbols or [])]   # materialise once (generators)
    except Exception as e:
        logger.warning(f"SCHWAB nbbo symbols unreadable: {_err(e)}")
        return {}, {}

    def _run():
        reasons: dict = {}
        holder["quotes"] = _nbbo_safe(syms, max_age_s, reasons)
        holder["reasons"] = reasons
    if deadline_s is None:
        _run()
        return holder.get("quotes", {}), holder.get("reasons", {})
    try:
        limit = float(deadline_s)
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("deadline must be a positive finite number of seconds")
        t = threading.Thread(target=_run, name="schwab-nbbo", daemon=True)
        t.start()
        t.join(timeout=limit)
    except Exception as e:
        logger.warning(f"SCHWAB nbbo deadline path failed: {_err(e)} — fallback")
        return {}, {s: "internal_error" for s in syms}
    if t.is_alive():
        logger.warning(f"SCHWAB nbbo exceeded the caller's {limit:.2f}s deadline — fallback")
        return {}, {s: "deadline" for s in syms}
    return holder.get("quotes", {}), holder.get("reasons", {})


def _nbbo_safe(symbols, max_age_s, reasons: dict) -> dict:
    out: dict = {}
    syms: list = []
    try:
        syms = [str(s).strip().upper() for s in (symbols or [])]
        out = _nbbo_impl(syms, dict(max_age_s or {}), reasons)
    except Exception as e:
        logger.warning(f"SCHWAB nbbo internal error: {_err(e)}")
        out = {}
        for s in syms:
            reasons.setdefault(s, "internal_error")
    return out


def nbbo(symbols, max_age_s: dict) -> dict:
    """Validated NBBO quotes keyed by Alpaca symbol. Any symbol without a usable quote is absent
    and has a reason in last_reasons(). Never raises."""
    reasons: dict = {}
    out: dict = {}
    syms: list = []
    try:
        syms = [str(s).strip().upper() for s in (symbols or [])]
        out = _nbbo_impl(syms, dict(max_age_s or {}), reasons)
    except Exception as e:
        logger.warning(f"SCHWAB nbbo internal error: {_err(e)}")
        out = {}
        for s in syms:
            reasons.setdefault(s, "internal_error")
    finally:
        with _mem_lock:
            _last_reasons.clear()
            _last_reasons.update(reasons)
    return out


def _all(syms, reason, reasons) -> dict:
    for s in syms:
        reasons.setdefault(s, reason)
    return {}


def _nbbo_impl(syms: list, bounds: dict, reasons: dict) -> dict:
    if not syms:
        return {}
    bounds = {str(k).strip().upper(): v for k, v in bounds.items()}
    if not SCHWAB_QUOTES_ENABLED:
        return _all(syms, "disabled", reasons)
    key, sec = _app_creds()
    if not key or not sec:
        return _all(syms, "no_key", reasons)
    _remember_secret(sec)
    killed = _kill_reason()
    if killed:
        return _all(syms, f"killed:{killed}", reasons)

    mapped = {}  # schwab -> alpaca
    for s in syms:
        sch = to_schwab(s)
        if sch is None:
            reasons[s] = "unmapped"
            logger.info(f"SCHWAB [{s}] unmapped symbol — fallback")
        else:
            mapped[sch] = s
    if not mapped:
        return {}

    # Token cycle + budget take under one lock acquisition.
    try:
        with _FileLock():
            access, reason = _ensure_token_locked()
            if reason is None:
                _allowed, reason = _take_budget_locked()
    except _LockTimeout:
        logger.warning("SCHWAB LOCK_TIMEOUT — token/budget lock not acquired in time; fallback")
        return _all(mapped.values(), "LOCK_TIMEOUT", reasons)
    if reason is not None:
        return _all(mapped.values(), reason, reasons)

    connect, read = _timeouts()
    t0 = time.monotonic()
    try:
        resp = requests.get(QUOTES_URL, params={"symbols": ",".join(mapped), "fields": "quote"},
                            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
                            timeout=(connect, read))
        code = int(resp.status_code)
    except Exception as e:
        latency = time.monotonic() - t0
        if _is_timeout(e):
            _on_timeout()
        logger.warning(f"SCHWAB_FETCH n={len(mapped)} latency={latency:.3f}s ok=0 fallbacks={len(mapped)} "
                       f"error={_err(e)}")
        return _all(mapped.values(), "timeout" if _is_timeout(e) else "network_error", reasons)
    latency = time.monotonic() - t0
    fetch_time = datetime.now(timezone.utc)

    try:
        with _FileLock():
            _update_budget_locked(getattr(resp, "headers", None), code)
    except _LockTimeout:
        logger.warning("SCHWAB LOCK_TIMEOUT on budget update — header state not recorded")
    except Exception as e:
        logger.warning(f"SCHWAB budget update failed: {_err(e)}")

    if code in (401, 403):
        _write_kill(f"quote_http_{code}")
        _page("quote_auth", f"⚠️ Schwab quotes: quote call refused (HTTP {code}). Disabled for the rest of today; "
                            f"the bot uses its existing quotes.")
        logger.error(f"SCHWAB_FETCH n={len(mapped)} latency={latency:.3f}s HTTP {code} — kill state written for today")
        return _all(mapped.values(), f"auth_{code}", reasons)
    if code == 429:
        logger.warning(f"SCHWAB_FETCH n={len(mapped)} latency={latency:.3f}s HTTP 429 — fallback, counted, backing off")
        return _all(mapped.values(), "http_429", reasons)
    if code != 200:
        logger.warning(f"SCHWAB_FETCH n={len(mapped)} latency={latency:.3f}s HTTP {code} — fallback")
        return _all(mapped.values(), f"http_{code}", reasons)
    _record_latency(_latencies, latency)

    try:
        body = resp.json()
    except Exception as e:
        logger.warning(f"SCHWAB quote body not JSON: {_err(e)}")
        body = None
    if not _shape_ok(body):
        _page("shape", "⚠️ Schwab quotes: response shape unexpected (API change?). Fallback for affected calls; "
                       "the bot uses its existing quotes.")
        logger.warning(f"SCHWAB_FETCH n={len(mapped)} latency={latency:.3f}s unexpected response shape — fallback")
        return _all(mapped.values(), "shape", reasons)

    out: dict = {}
    lines: list = []
    parsed = []
    for sch, alp in mapped.items():
        entry = body.get(sch)
        if entry is None:
            reasons[alp] = "MISSING_IN_RESPONSE"
            continue
        q = entry["quote"]
        try:
            qtime = datetime.fromtimestamp(float(q["quoteTime"]) / 1000.0, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            reasons[alp] = "bad_timestamp"
            continue
        lines.append(crc_line({
            "symbol": alp, "quote_time": qtime.isoformat(), "bid": float(q["bidPrice"]), "ask": float(q["askPrice"]),
            "bid_size": float(q["bidSize"]), "ask_size": float(q["askSize"]), "last": float(q["lastPrice"]),
            "fetch_time": fetch_time.isoformat()}))
        parsed.append((sch, alp, q, qtime))
    # Clock offset (design rev 3): a quote stamped after our own fetch time means the local clock runs
    # behind Schwab by at least that much. Every age in this batch is corrected by the largest such
    # lead, so a lagging clock can never make a quote look younger than it is.
    clock_ahead = max([0.0] + [(qt - fetch_time).total_seconds() for _s, _a, _q, qt in parsed])
    for sch, alp, q, qtime in parsed:
        bad, locked = _validate(q)
        if bad:
            reasons[alp] = bad
            continue
        raw_bound = bounds.get(alp)
        if (not isinstance(raw_bound, (int, float)) or isinstance(raw_bound, bool)
                or not math.isfinite(raw_bound) or raw_bound <= 0):
            reasons[alp] = "no_bound"
            continue
        bound = float(raw_bound)
        if clock_ahead > bound:
            # the clocks disagree by more than the freshness check can tolerate → do not use quotes
            reasons[alp] = "clock_skew"
            _page("clock_skew", f"⚠️ Schwab quotes: server clock behind Schwab by {clock_ahead:.1f}s, more than a "
                                f"quote freshness bound; quotes unused until the clock is fixed (check chrony).")
            continue
        age = (fetch_time - qtime).total_seconds() + clock_ahead
        if age > bound:
            reasons[alp] = "stale"
            continue
        if locked:
            logger.info(f"SCHWAB [{alp}] locked book bid=ask={float(q['bidPrice'])} — valid, spread 0")
        bid, ask = float(q["bidPrice"]), float(q["askPrice"])
        out[alp] = Quote(symbol=alp, schwab_symbol=sch, bid=bid, ask=ask, bid_size=float(q["bidSize"]),
                         ask_size=float(q["askSize"]), last=float(q["lastPrice"]), quote_time=qtime,
                         fetch_time=fetch_time, age_s=age, max_age_s=bound, latency_s=latency,
                         spread=0.0 if locked else ask - bid, locked=locked)
    _append_quote_lines(lines)
    fb = {s: r for s, r in reasons.items() if s in mapped.values()}
    logger.info(f"SCHWAB_FETCH n={len(mapped)} latency={latency:.3f}s ok={len(out)} fallbacks={len(fb)} "
                f"clock_ahead_s={clock_ahead:.3f} reasons={fb}")
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Index values ($VIX, $VIX3M, $SPX) — REGIME-SIZE-1104 rev 3.3-3.6
# ══════════════════════════════════════════════════════════════════════════════
# Index quotes carry no bid/ask/size, so they never go through _shape_ok / _validate / to_schwab (the NBBO path
# would reject them). Field names pinned from the live response read on the server 2026-11-04:
# {"$VIX": {"symbol": "$VIX", "assetMainType": "INDEX", "realtime": true,
#           "quote": {"lastPrice": 16.61, "tradeTime": 1793826901329, ...}}}
_INDEX_RE = re.compile(r"^\$[A-Z0-9]{1,10}$")   # local to this path; the shared _SYMBOL_RE is unchanged


def _num_or_none(v):
    """float(v) for a finite int/float that is not a bool, else None."""
    if not _is_num(v):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def index_last_with_reasons(symbols, max_age_s: dict, deadline_s=None):
    """(values, reasons) for THIS call: {symbol: IndexQuote} for every requested index symbol whose own trade time
    is within the caller's bound, and a reason for every other one. Reasons are per call (the shared
    last_reasons() of nbbo() is never touched). With `deadline_s` the call runs in a worker thread and returns
    ({}, {sym: "deadline"}) when it does not finish in time. Never raises."""
    holder: dict = {}
    try:
        syms = [str(s).strip().upper() for s in list(symbols or [])]
    except Exception as e:
        logger.warning(f"SCHWAB index symbols unreadable: {_err(e)}")
        return {}, {}

    def _run():
        reasons: dict = {}
        try:
            holder["quotes"] = _index_impl(syms, dict(max_age_s or {}), reasons)
        except Exception as e:
            logger.warning(f"SCHWAB index internal error: {_err(e)}")
            holder["quotes"] = {}
            for s in syms:
                reasons.setdefault(s, "internal_error")
        holder["reasons"] = reasons
    if deadline_s is None:
        _run()
        return holder.get("quotes", {}), holder.get("reasons", {})
    try:
        limit = float(deadline_s)
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("deadline must be a positive finite number of seconds")
        t = threading.Thread(target=_run, name="schwab-index", daemon=True)
        t.start()
        t.join(timeout=limit)
    except Exception as e:
        logger.warning(f"SCHWAB index deadline path failed: {_err(e)} — fallback")
        return {}, {s: "internal_error" for s in syms}
    if t.is_alive():
        logger.warning(f"SCHWAB index exceeded the caller's {limit:.2f}s deadline — fallback")
        return {}, {s: "deadline" for s in syms}
    return holder.get("quotes", {}), holder.get("reasons", {})


def _index_impl(syms: list, bounds: dict, reasons: dict) -> dict:
    if not syms:
        return {}
    bounds = {str(k).strip().upper(): v for k, v in bounds.items()}
    if not (SCHWAB_QUOTES_ENABLED and SCHWAB_INDEX_ENABLED):
        return _all(syms, "disabled", reasons)
    key, sec = _app_creds()
    if not key or not sec:
        return _all(syms, "no_key", reasons)
    _remember_secret(sec)
    killed = _kill_reason()
    if killed:
        return _all(syms, f"killed:{killed}", reasons)
    wanted = []
    for s in syms:
        if _INDEX_RE.match(s):
            if s not in wanted:
                wanted.append(s)
        else:
            reasons[s] = "not_index_symbol"
    if not wanted:
        return {}

    try:
        with _FileLock():
            access, reason = _ensure_token_locked()
            if reason is None:
                _allowed, reason = _take_budget_locked()
    except _LockTimeout:
        logger.warning("SCHWAB LOCK_TIMEOUT (index) — token/budget lock not acquired in time; fallback")
        return _all(wanted, "LOCK_TIMEOUT", reasons)
    if reason is not None:
        return _all(wanted, reason, reasons)

    connect, read = _timeouts()
    t0 = time.monotonic()
    try:
        resp = requests.get(QUOTES_URL, params={"symbols": ",".join(wanted), "fields": "quote"},
                            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
                            timeout=(connect, read))
        code = int(resp.status_code)
    except Exception as e:
        latency = time.monotonic() - t0
        if _is_timeout(e):
            _on_timeout()
        logger.warning(f"SCHWAB_INDEX_FETCH n={len(wanted)} latency={latency:.3f}s ok=0 error={_err(e)}")
        return _all(wanted, "timeout" if _is_timeout(e) else "network_error", reasons)
    latency = time.monotonic() - t0
    fetch_time = datetime.now(timezone.utc)

    try:
        with _FileLock():
            _update_budget_locked(getattr(resp, "headers", None), code)
    except _LockTimeout:
        logger.warning("SCHWAB LOCK_TIMEOUT on budget update (index) — header state not recorded")
    except Exception as e:
        logger.warning(f"SCHWAB budget update failed (index): {_err(e)}")

    if code in (401, 403):
        # same token as the NBBO path: an auth refusal disables the whole module for the day
        _write_kill(f"index_http_{code}")
        _page("index_auth", f"⚠️ Schwab index values: call refused (HTTP {code}). Schwab disabled for the rest of "
                            f"today; the bot uses its next VIX source.")
        logger.error(f"SCHWAB_INDEX_FETCH n={len(wanted)} latency={latency:.3f}s HTTP {code} — kill state written for today")
        return _all(wanted, f"auth_{code}", reasons)
    if code == 429:
        logger.warning(f"SCHWAB_INDEX_FETCH n={len(wanted)} latency={latency:.3f}s HTTP 429 — fallback, counted")
        return _all(wanted, "http_429", reasons)
    if code != 200:   # incl. 400 / invalid symbols: never a kill
        logger.warning(f"SCHWAB_INDEX_FETCH n={len(wanted)} latency={latency:.3f}s HTTP {code} — fallback")
        return _all(wanted, f"http_{code}", reasons)
    _record_latency(_latencies, latency)

    try:
        body = resp.json()
    except Exception as e:
        logger.warning(f"SCHWAB index body not JSON: {_err(e)}")
        body = None
    if not isinstance(body, dict):
        _page("index_shape", "⚠️ Schwab index values: response shape unexpected (API change?). The bot uses its "
                             "next VIX source.")
        return _all(wanted, "shape", reasons)

    parsed = []
    for sym in wanted:
        entry = body.get(sym)
        if entry is None:
            reasons[sym] = "MISSING_IN_RESPONSE"
            continue
        q = entry.get("quote") if isinstance(entry, dict) else None
        if not isinstance(q, dict) or entry.get("symbol") != sym:
            reasons[sym] = "shape"
            _page("index_shape", "⚠️ Schwab index values: response shape unexpected (API change?). The bot uses "
                                 "its next VIX source.")
            continue
        if entry.get("assetMainType") != "INDEX":
            reasons[sym] = "not_index"
            continue
        if entry.get("realtime") is not True:
            reasons[sym] = "not_realtime"
            continue
        last = _num_or_none(q.get("lastPrice"))
        ttime = _num_or_none(q.get("tradeTime"))
        if last is None or last <= 0:
            reasons[sym] = "bad_last"
            continue
        if ttime is None:
            reasons[sym] = "bad_timestamp"
            continue
        try:
            trade_time = datetime.fromtimestamp(ttime / 1000.0, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            reasons[sym] = "bad_timestamp"
            continue
        parsed.append((sym, last, trade_time))
    # Same clock rule as nbbo(): a value stamped after our fetch time means our clock lags Schwab by at least that
    # much; every age is corrected by the largest such lead, so a lagging clock never makes a value look younger.
    clock_ahead = max([0.0] + [(tt - fetch_time).total_seconds() for _s, _l, tt in parsed])
    out: dict = {}
    for sym, last, trade_time in parsed:
        bound = _num_or_none(bounds.get(sym))
        if bound is None or bound <= 0:
            reasons[sym] = "no_bound"
            continue
        if clock_ahead > bound:
            reasons[sym] = "clock_skew"
            _page("clock_skew", f"⚠️ Schwab quotes: server clock behind Schwab by {clock_ahead:.1f}s, more than a "
                                f"quote freshness bound; values unused until the clock is fixed (check chrony).")
            continue
        age = (fetch_time - trade_time).total_seconds() + clock_ahead
        if age > bound:
            reasons[sym] = "stale"
            continue
        out[sym] = IndexQuote(symbol=sym, last=last, trade_time=trade_time, fetch_time=fetch_time, age_s=age,
                              max_age_s=bound, latency_s=latency)
    logger.info(f"SCHWAB_INDEX_FETCH n={len(wanted)} latency={latency:.3f}s ok={len(out)} "
                f"values={ {k: v.last for k, v in out.items()} } clock_ahead_s={clock_ahead:.3f} "
                f"reasons={ {k: v for k, v in reasons.items() if k in wanted} }")
    return out


def status() -> dict:
    """Operator view. Contains no token, key or secret value."""
    st: dict = {"enabled": SCHWAB_QUOTES_ENABLED, "index_enabled": SCHWAB_INDEX_ENABLED}
    try:
        key, sec = _app_creds()
        st["has_key"] = bool(key and sec)
        tok = read_token()
        st["has_token"] = bool(tok)
        if tok:
            now = time.time()
            st["access_expires_in_s"] = round(float(tok.get("access_expires_at") or 0) - now, 1)
            st["refresh_token_age_days"] = round((now - float(tok.get("refresh_issued_at") or now)) / 86400, 2)
            st["scope_ok"] = bool(tok.get("scope_ok"))
            st["refresh_dead"] = bool(tok.get("refresh_dead"))
            st["token_file_mode"] = oct(os.stat(str(TOKEN_FILE)).st_mode & 0o777)
        st["killed"] = _kill_reason()
        b = _read_json(BUDGET_FILE) or {}
        st["budget"] = {"capacity": _budget_capacity(b), "tokens": b.get("tokens"),
                        "blocked_until": b.get("blocked_until"), "count_429": b.get("count_429")}
        connect, read = _timeouts()
        with _mem_lock:
            st["latency_samples"] = len(_latencies)
            st["last_fallbacks"] = len(_last_reasons)
        st["timeouts_s"] = {"connect": connect, "read": read}
    except Exception as e:
        st["error"] = type(e).__name__
    return st

# ruff: noqa: E501  — long prompt strings and P5 queue text are intentionally long
"""
nightly_audit.py
Sends last-24h bot logs + modified source files to Gemini Flash for adversarial
code and behaviour review. Posts a structured Slack summary. Fully async from
the trading bot — read-only, no shared state, no execution imports.

Schedule: midnight PT daily via cron or launchd.
  crontab entry:  30 13 * * * cd /path/to/bot && /usr/local/bin/python3.10 nightly_audit.py
"""

import os
import sys
import json
import ssl
import logging
import importlib
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv  # must precede module-level os.getenv() calls (E402 fix)

load_dotenv()

PT  = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")  # RC-1 fix: used to make strptime results tz-aware
_now = datetime.now(PT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nightly_audit")

# ── Config ───────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "").strip()
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL", "").strip()
BASE_DIR        = Path(__file__).parent
LOGS_DIR        = BASE_DIR / "logs"
BOT_LOG         = LOGS_DIR / "mtf_bot.log"
TRADE_EVENTS    = LOGS_DIR / "trade_events.jsonl"
GEMINI_MODEL    = "gemini-3.1-flash-lite"
MAX_LOG_LINES   = 500   # cap — avoid blowing context on noisy INFO lines
MAX_FILE_CHARS  = 20_000 # cap per source file
AUDIT_DATE      = _now.strftime("%Y-%m-%d")

# ── SSL context (macOS 3.10 cert fix) ────────────────────────────────────────
try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# ── P5 open bug queue (updated 2026-04-21) — Gemini checks if any were touched ─
P5_BUG_QUEUE = """
OPEN P5 BUG QUEUE (updated 2026-04-21):

CRITICAL:
  P5-C1 | FVG MultiIndex silent crash | strategy/signal_generator.py + main.py _find_recent_fvgs()
         | pd.DataFrame from dict of dicts → MultiIndex columns → silent KeyError on column access
  P5-C2 | run_movers.py ImportError | run_movers.py:19 imports DataFetcher which doesn't exist
         | Will crash on any premarket movers scan — UNRESOLVED

HIGH:
  P5-H1 | OBSOLETE — PDT enforcement permanently deleted from codebase (S63 sweep,
         SEC rule change). This item no longer applies to anything. Do NOT investigate
         or flag PDT-related causes for any event — the mechanism does not exist.
  P5-H2 | Fill price crosstalk | order fill price ambiguity when multiple orders in flight simultaneously
  P5-H3 | Orphan GTC 404 path | order-not-found returns WARNING but doesn't clear tracker → stranded stops
  P5-H4 | Rule 2 dead code | TOD_MARKET_OPEN_BUFFER_MINS referenced but possibly overridden to no-op
  P5-H5 | FIXED 2026-04-21 | GTC stop suppressed at PDT=3/3 + opened_today | now skips + logs; morning restart handles exchange stop

MEDIUM:
  P5-M1 | Cycle watchdog restart loop | os.execv() re-entry without clearing lockfile → instant re-lock
  P5-M2 | MRI score cached stale across sessions | mri_state.json not validated on load
  P5-M3 | score_comparison JSON unbounded growth | appends forever, never pruned

LOW:
  P5-L1 | _classify_loss_driver() returns empty label in some edge paths | reporting only, no trade impact
  P5-L2 | eod_YYYY-MM-DD.json written before positions closed | final day P&L understated
  P5-L3 | live_data_writer.py no reconnect logic | process dies on network drop, restart loop in launch_bots.sh added as mitigation

KNOWN ARCHITECTURAL ISSUES (not P5, require board vote):
  A-1  | overnight_breakeven exit fires at entry-0.25×ATR, not at entry price — name is misleading
  A-2  | trail_stop not floor-bounded by promoted stop after partial exit — can exit below entry
  A-3  | nightly_audit trade_log.json reference was stale (file never existed) — fixed 2026-04-21
  A-4  | Alpaca paper fills API returns empty for same-day closures until overnight settlement
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _collect_log_lines() -> str:
    """Pull last 24h of bot.log, filtered to WARNING/ERROR/CRITICAL + key events."""
    if not BOT_LOG.exists():
        return "(bot.log not found)"
    cutoff = datetime.now(PT) - timedelta(hours=24)
    keep_keywords = ("WARNING", "ERROR", "CRITICAL", "KILL SWITCH",
                     "ENTRY", "EXIT", "STOP", "HALT", "position size",
                     "Daily reset", "score", "SKIPPED", "BLOCKED",
                     "Exception", "Traceback", "stale", "rejected")
    lines = []
    try:
        with open(BOT_LOG, "r", errors="replace") as f:
            for line in f:
                # crude timestamp parse: "2026-04-07 21:11:23,909 | ..."
                try:
                    ts_str = line[:23].replace(",", ".")
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
                if any(k in line for k in keep_keywords):
                    lines.append(line.rstrip())
    except Exception as e:
        return f"(Error reading bot.log: {e})"

    if not lines:
        return "(No relevant log lines in last 24h)"
    # cap
    if len(lines) > MAX_LOG_LINES:
        lines = lines[-MAX_LOG_LINES:]
        lines.insert(0, f"[truncated to last {MAX_LOG_LINES} lines]")
    return "\n".join(lines)


def _collect_trade_events() -> str:
    """Read trade_events.jsonl — structured entry/exit/stop events, last 7 days.
    Excludes verbose mri_refresh events to stay within prompt budget."""
    if not TRADE_EVENTS.exists():
        return "(trade_events.jsonl not found)"
    try:
        cutoff = (datetime.now(PT) - timedelta(days=7)).strftime("%Y-%m-%d")
        keep_types = {"entry", "exit", "stop_hit", "partial_exit", "signal"}
        events = []
        with open(TRADE_EVENTS, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("ts", "")[:10] >= cutoff and ev.get("event") in keep_types:
                        # Annotate T1/T2/T3 levels on entry events for Gemini
                        if ev.get("event") == "entry":
                            _ep  = float(ev.get("price") or 0)
                            _tgt = float(ev.get("target") or 0)
                            if _ep and _tgt:
                                ev["T1"] = round(_ep + 0.20 * (_tgt - _ep), 2)
                                ev["T2"] = round(_ep + 0.40 * (_tgt - _ep), 2)
                                ev["T3"] = _tgt
                        events.append(ev)
                except json.JSONDecodeError:
                    continue
        if not events:
            return "(No trade events in last 7 days)"
        return json.dumps(events, indent=2, default=str)
    except Exception as e:
        return f"(Error reading trade_events.jsonl: {e})"


def _eod_pnl_provenance(eod: dict) -> tuple[dict, str]:
    """Return (llm_safe_copy, provenance_header) for an EOD dict so the STATELESS
    LLM auditor can never read a PRE-HEAL self-check `pnl_drift` as P&L corruption.

    Keys off the `_healed_by` provenance stamp + `pnl_unreconciled` — NEVER off
    `pnl_drift` magnitude (BGG 2026-07-27: masked-loss + reliability + data-integrity
    cold seats + Gro + GAI; mirrors scripts/audit_slack.build_pnl_fields). Rationale:
    `pnl_drift`/`alpaca_pnl`/`tracker_pnl` are a PRE-HEAL dual-compute self-check —
    the authoritative ledger heal runs 8:30pm ET, AFTER this 4:05pm ET audit, so a
    nonzero drift is EXPECTED and self-heals overnight. That telemetry has NO reader
    in any kill-switch/sizing/exposure path (grep-proven; severed after the 2026-07-07
    incident), so it can never be "P&L corruption affecting live risk decisions." The
    authoritative same-day realized P&L is `pnl_today`; the ONLY genuine reconciliation
    signal is `pnl_unreconciled` (+ reason).

    Relabels the self-check telemetry into a nested, self-documenting key so the LLM
    cannot latch onto a raw top-level `pnl_drift` as a loss, while keeping the
    authoritative + genuine-alarm fields (`pnl_today`, `pnl_unreconciled`, `_healed_by`)
    fully visible — this never masks a real loss (the real loss signal stays surfaced).
    Fails safe: on any malformed input it returns the dict unchanged with an empty
    header (the raw dump + the prompt-level rule still apply)."""
    try:
        safe = dict(eod)
        healed = bool(safe.get("_healed_by"))
        unreconciled = bool(safe.get("pnl_unreconciled"))
        pnl_today = safe.get("pnl_today", safe.get("alpaca_pnl"))
        selfcheck = {}
        for k in ("pnl_drift", "alpaca_pnl", "tracker_pnl", "alpaca_per_trade"):
            if k in safe:
                selfcheck[k] = safe.pop(k)
        if selfcheck:
            selfcheck["_note"] = ("PRE-HEAL self-check telemetry (Alpaca-fills vs internal "
                                  "tracker), sampled before the 8:30pm ET ledger heal. NOT a "
                                  "loss, NOT P&L corruption, NO reader in any risk/sizing/kill "
                                  "path. Authoritative same-day P&L is `pnl_today`.")
            safe["_pnl_selfcheck_telemetry_NOT_A_LOSS"] = selfcheck
        if unreconciled:
            header = ("EOD P&L PROVENANCE: pnl_unreconciled=TRUE — GENUINE ledger-flagged "
                      f"reconciliation state (reason: {safe.get('pnl_unreconciled_reason', 'n/a')}). "
                      "Flag as WARN (heal-pending) and cite `pnl_unreconciled_reason`; do NOT "
                      "escalate to CATASTROPHIC on drift magnitude. Authoritative same-day "
                      f"P&L = pnl_today={pnl_today}.")
        elif healed:
            header = (f"EOD P&L PROVENANCE: RECONCILED (healed via {safe.get('_healed_by')}). "
                      f"Same-day realized P&L = pnl_today={pnl_today} is authoritative. Any "
                      "self-check drift is a healed residual — NOT a loss, NOT corruption.")
        else:
            header = ("EOD P&L PROVENANCE: PRE-HEAL / PROVISIONAL — snapshot taken before "
                      "tonight's 8:30pm ET ledger heal (this audit runs 4:05pm ET). Any drift is "
                      f"EXPECTED and self-heals. Authoritative same-day P&L = pnl_today={pnl_today}. "
                      "Do NOT treat drift as corruption/CATASTROPHIC.")
        return safe, header
    except Exception as e:
        logger.warning("EOD P&L provenance annotation failed (%s) — dumping raw EOD "
                       "(prompt-level drift rule still applies)", e)
        return eod, ""


def _collect_eod() -> str:
    """Today's EOD snapshot, with a deterministic P&L-provenance annotation so the
    stateless LLM auditor can never read a PRE-HEAL self-check `pnl_drift` as P&L
    corruption (BGG 2026-07-27). Keys off `_healed_by`/`pnl_unreconciled`, never drift."""
    eod_path = LOGS_DIR / f"eod_{AUDIT_DATE}.json"
    if not eod_path.exists():
        # try yesterday
        yesterday = (datetime.now(PT) - timedelta(days=1)).strftime("%Y-%m-%d")
        eod_path = LOGS_DIR / f"eod_{yesterday}.json"
    if not eod_path.exists():
        return "(No EOD snapshot found)"
    try:
        with open(eod_path) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            safe, header = _eod_pnl_provenance(raw)
            body = json.dumps(safe, indent=2)
            return f"{header}\n\n{body}" if header else body
        return json.dumps(raw, indent=2)
    except Exception as e:
        return f"(Error reading EOD: {e})"


def _collect_modified_files() -> dict[str, str]:
    """Source .py files modified in the last 24h."""
    cutoff = datetime.now(UTC).timestamp() - 86_400
    modified = {}
    # `.claude`/`tests` excluded (BGG 2026-07-27): nightly's job is post-close review of
    # the day's EXECUTION commits — tooling/harness and test files are not the trading
    # path and only add review noise (per-report value/noise audit, GAI concurrence).
    skip_dirs = {"__pycache__", "backtest", ".git", "logs", "venv", ".venv", ".claude", "tests"}
    for py_file in BASE_DIR.rglob("*.py"):
        if any(part in skip_dirs for part in py_file.parts):
            continue
        if py_file.stat().st_mtime >= cutoff:
            try:
                content = py_file.read_text(errors="replace")
                if len(content) > MAX_FILE_CHARS:
                    content = content[:MAX_FILE_CHARS] + f"\n... [truncated at {MAX_FILE_CHARS} chars]"
                modified[str(py_file.relative_to(BASE_DIR))] = content
            except Exception as e:
                modified[str(py_file.relative_to(BASE_DIR))] = f"(read error: {e})"
    return modified


def _build_config_constants_block() -> str:
    """Resolve live config values at audit-build time instead of hand-typing a
    snapshot. S68 fix (2026-06-27, board+Gro+GAI): the prior hardcoded block
    silently drifted from reality for months — MIN_SCORE doesn't exist as a flat
    constant (it's MIN_LONG_SCORE/MIN_SHORT_SCORE inside PROFILES["paper"], value
    10 not the claimed 9), KELLY_FRACTION was claimed 0.25 but paper profile is
    0.35, INTRADAY_STOP_ATR_MULT was claimed 1.25 but paper profile is 1.20, and
    OVERNIGHT_ENTRIES_ENABLED was claimed True but isn't defined in config.py at
    all (resolves to False via getattr default in main.py). A wrong "ground
    truth" fed to an LLM auditor is worse than no ground truth — it can cite a
    real-looking number to justify a false finding. Resolve live; if a name
    can't be found, say so explicitly rather than asserting a guessed value.
    """
    try:
        sys.path.insert(0, str(BASE_DIR))
        import config as _live_cfg
        importlib.reload(_live_cfg)
    except Exception as e:
        return f"(config.py import failed: {e} — constants below are UNVERIFIED, do not audit against them)"

    profile = _live_cfg.PROFILES.get("paper", {}) if hasattr(_live_cfg, "PROFILES") else {}

    def _resolve(name: str) -> str:
        if name in profile:
            return f"{name} = {profile[name]!r}  (paper profile)"
        if hasattr(_live_cfg, name):
            return f"{name} = {getattr(_live_cfg, name)!r}  (module-level default)"
        return f"{name} = NOT FOUND in config.py — do not assert a value for this"

    names = [
        "MIN_LONG_SCORE", "MIN_SHORT_SCORE", "KELLY_FRACTION", "MAX_OPEN_POSITIONS",
        "BUCKET_B_MAX_POSITIONS_POWER",
        "INTRADAY_STOP_ATR_MULT", "INTRADAY_TARGET_ATR_MULT", "MAX_DAILY_LOSS_PCT",
        "VIX_BE_WIDEN_THRESHOLD_1", "VIX_BE_WIDEN_THRESHOLD_2",
        "VIX_STOP_WIDEN_THRESHOLD_1", "VIX_STOP_WIDEN_THRESHOLD_2",
        "VIX_STOP_WIDEN_MULT_1", "VIX_STOP_WIDEN_MULT_2",
        "VOLATILITY_TIER_HIGH_THRESHOLD", "VOLATILITY_TIER_EXTREME_THRESHOLD",
        "VOL_TIER_STD_STOP_INTRADAY", "VOL_TIER_HIGH_STOP_INTRADAY",
        "VOL_TIER_EXTREME_STOP_INTRADAY",
        "OVERNIGHT_ENTRIES_ENABLED", "LEVERAGED_NOTIONAL_MAX_PCT",
        "TOD_MARKET_OPEN_BUFFER_MINS", "TOD_EOD_NO_ENTRY_MINS",
    ]
    return "\n".join(_resolve(n) for n in names)


def _build_prompt(log_lines: str, trade_events: str, eod: str,
                  modified_files: dict[str, str]) -> str:
    files_section = ""
    if modified_files:
        for path, content in modified_files.items():
            files_section += f"\n\n### {path}\n```python\n{content}\n```"
    else:
        files_section = "\n(No source files modified in last 24h)"

    return f"""You are an adversarial code and trading-behaviour auditor for a live
Alpaca paper-trading bot (MTF confluence scoring system, 12-point score, $2.5K
paper account). Your job is to find problems — bugs, logic errors, P&L recording
issues, risk management gaps, silent failures, and anything that looks wrong.

## VERIFICATION DISCIPLINE — MANDATORY, READ FIRST

1. Before flagging any bug: (1) state the exact function name and variable involved,
   (2) trace the code path — what does the runtime produce vs. what is intended,
   (3) state the exact failure condition. Do NOT flag something as a bug if you
   cannot complete step (2).
2. Do not hallucinate code, function names, or state/enum names not shown to you.
   If you reference a state name, function, or variable, it MUST appear verbatim
   in the source provided below. If you cannot find it, say "not found in provided
   source" — do not guess a plausible-sounding name.
3. When citing any numeric field (price, stop, score, P&L) for a specific trade,
   you MUST pull every field for that citation from the SAME trade_events JSON
   record (matched by its exact "ts" timestamp). Never combine a price/stop/score
   from one record with a price/stop/score from a different record for the same
   symbol, even if they look related — different timestamps mean different trades.
4. Check CONFIG CONSTANTS below before flagging any configuration violation —
   these are resolved live from config.py at the moment this report was built,
   not a hand-typed snapshot. If a constant shows "NOT FOUND," do not assert any
   value for it. Intentional behaviors in KNOWN BENIGN PATTERNS must not be flagged.
5. If you disagree with a prior audit finding or a proposed fix: (a) trace the exact
   code path that disproves the finding, (b) cite the relevant CONFIG CONSTANT with
   its actual resolved value, (c) provide an alternative fix with file name and line
   context, (d) flag it as "METHODOLOGY DISAGREEMENT" — not as a new bug.

## ARCHITECTURE INVARIANT — PDT DOES NOT EXIST

Pattern Day Trader (PDT) enforcement was PERMANENTLY DELETED from this codebase
(SEC rule change, S63 sweep). There is zero PDT logic anywhere in execution code.
Any "pdt_used" field you see in trade data is a dead/legacy field hardcoded to 0 —
it is not a live signal. NEVER attribute any halt, block, or anomaly to PDT. If you
see something that superficially resembles a day-trade-count pattern, the actual
cause is something else — find it in the provided log/code, do not default to PDT.

---

## AUDIT DATE: {AUDIT_DATE}

---

## BOT LOG (last 24h — WARNING/ERROR/CRITICAL/key events only)
```
{log_lines}
```

---

## TRADE EVENTS (last 7 days — entries/exits/stops with stop/T1/T2/T3 levels)
Entry events include: price, stop, T1, T2, T3 (target) levels, score, MRI level.
Use these to verify: exit prices relative to stated stop/target levels, whether partial
exits fired at T1/T2, whether stop was moved after partial exit, P&L sign correctness.
Remember: cite all fields for one trade from ONE record (same "ts"), never mix records.
```json
{trade_events}
```

---

## EOD SNAPSHOT
```json
{eod}
```

---

## BOT CONFIG CONSTANTS (resolved live from config.py at report-build time — verify against these before flagging a violation; "NOT FOUND" means do not assert a value)
{_build_config_constants_block()}

---

## KNOWN BENIGN PATTERNS (do NOT flag these as bugs)
- "EOD summary written" or "Alpaca FIFO EOD" lines every ~5 min: periodic crash-safety flushes — intentional. Flag ONLY if spacing > 20 min (missed flush) or < 1 min (runaway loop).
- "A-4 paper fills gap: Alpaca returned 0 fills": Alpaca paper accounts do not expose same-day fills until next-day settlement. Bot falls back to tracker P&L correctly. NOT a trade accounting failure.
- EOD `pnl_drift` / any "reconciliation mismatch" / "P&L drift" between alpaca_pnl and tracker_pnl: PRE-HEAL dual-compute self-check TELEMETRY sampled at 4:05pm ET, BEFORE the authoritative 8:30pm ET ledger heal. A nonzero drift is EXPECTED and self-heals overnight; it has NO reader in any kill-switch/sizing/exposure path (severed after 2026-07-07). It is NEVER P&L corruption and NEVER CATASTROPHIC. The authoritative same-day P&L is `pnl_today`. The ONLY genuine reconciliation signal is `pnl_unreconciled=true` (+ `pnl_unreconciled_reason`) — flag THAT as WARN (heal-pending), never CATASTROPHIC on drift magnitude. See the EOD P&L PROVENANCE header in the EOD SNAPSHOT section.
- 5 simultaneous open positions: BUCKET_B_MAX_POSITIONS_POWER=5 — intentional power-hour expansion during 9:35–10:00 AM ET. NOT a max-positions violation.
- Error 42210000 from GTC stop submission during extended hours (pre-RTH / after RTH close): known open bug OM-BUG-1 in orphan_manager.py, tracked. Do NOT flag pre-RTH occurrences. DO flag if 42210000 occurs during RTH (9:30–16:00 ET) as that indicates a halt/circuit-break anomaly.
- MRI refresh events every 2 minutes: normal macro regime sampling, not a loop.
- BUCKET_A positions (TQQQ, SQQQ, TSLL): leverage ETFs — higher dollar volatility is intentional.

---

## SOURCE FILES MODIFIED IN LAST 24H
{files_section}

---

## OPEN P5 BUG QUEUE
{P5_BUG_QUEUE}

---

## YOUR TASKS

1. **Log anomalies**: Identify any ERROR/WARNING/CRITICAL patterns that indicate a
   recurring or unresolved problem. Flag any KILL SWITCH triggers, stale data events,
   or entry blocks that seem suspicious — trace the actual gating condition in the
   log/code, do not assume a cause.

2. **Performance audit — entry/exit/P&L**: Review the last 10 trades for:
   - P&L sign correctness (long: exit > entry = profit; short: entry > exit = profit)
   - pnl_pct computed from original full position value, not remaining qty
   - partial_pnl accumulated correctly across T1/T2/T3 tranches
   - Trades with pnl=0.0 where entry ≠ exit price (recording bug)
   - Entry/exit prices plausible for the symbol, date, and time of day
   - Score→outcome correlation: do higher-score entries outperform lower-score?
   - Stop-hit rate vs. win rate: if stops > 50% of entries, flag regime mismatch
   - Exit reason consistency: stop_hit exit should be near stop price, not far from it
   - Profit factor and R:R vs. stated 2.5× target — is the bot capturing its edge?

3. **Modified file audit**: For each modified source file, audit the full visible
   content for pre-existing bugs, not just the recently changed lines. For every bug
   you flag: (a) state the exact function name and line context, (b) trace the code
   path — which variable holds the wrong value, which conditional is incorrectly
   evaluated, what the runtime produces vs. what is intended, (c) do NOT flag
   something as a bug unless you can state the exact failure condition, (d) check
   CONFIG CONSTANTS above before flagging a configuration violation. Look for:
   - Off-by-one errors, wrong comparison operators (> vs >= etc.)
   - Race conditions or state mutation bugs
   - Silent fallback paths that mask real errors (bare except, wrong default return)
   - Any function that returns a wrong type or None unexpectedly
   - Any risk management bypass or unconditional override
   - Exit price fallback paths — verify stop/target price used, not entry_price

4. **P5 queue cross-check**: For each P5 item, determine:
   - Was this file touched today? If so, was the bug fixed, made worse, or untouched?
   - Is there any new evidence in the logs that a P5 bug was triggered today?

5. **New bugs**: Flag any issues NOT in the P5 queue that you found. Give each a
   severity (CRITICAL / HIGH / MEDIUM / LOW) and exact location.

---

## OUTPUT FORMAT

Respond in this exact structure. The CATASTROPHIC ALERT section comes FIRST,
before anything else, so it cannot be missed or buried under lower-severity
findings — this report feeds an unattended overnight review pipeline.

### CATASTROPHIC ALERT: [count]
List ONLY items meeting the CATASTROPHIC bar below, or write "None — no
catastrophic conditions detected." One line each: file/function | exact
failure condition | impacted symbol/trade if applicable.
  CATASTROPHIC = position left naked (no stop), silent trading halt, P&L
  corruption affecting live risk decisions (NOTE: a pre-heal `pnl_drift` /
  "reconciliation mismatch" is NOT this — it has no reader in any live-risk/
  sizing/kill path; only `pnl_unreconciled=true` is a genuine reconciliation
  signal, and that is WARN, not CATASTROPHIC, unless independently confirmed to
  affect a live risk decision), kill switch triggered but not
  respected, any order lifecycle break (submitted but never tracked).

### VERDICT: [PASS | WARN | FAIL]
(PASS = no new issues, P5 stable; WARN = issues found but bot operational;
FAIL = CATASTROPHIC count > 0, OR active data corruption, OR critical bug confirmed active)

### LOG ANOMALIES
(bullet list, or "None detected")

### PERFORMANCE AUDIT
(entry/exit integrity, P&L sign checks, score→outcome correlation, stop rate analysis, R:R vs. target — or "All trades look correct")

### TRADE INTEGRITY
(recording bugs, pnl=0.0 anomalies, partial exit errors — or "No issues")

### MODIFIED FILE FINDINGS
(per-file findings, or "No issues found")

### P5 STATUS
(one line per open P5 item: UNTOUCHED / FIXED / WORSENED / TRIGGERED)

### NEW BUGS FOUND
For each: CATEGORY | SEVERITY | file | description | exact failure condition
CATEGORY: EXECUTION BUG | ALPHA ISSUE | INFRASTRUCTURE
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
  (CATASTROPHIC-tier items belong in CATASTROPHIC ALERT above, not here —
   do not duplicate them in this list.)
  CRITICAL = silent trading halt risk, P&L corruption in a non-live-risk path
  HIGH = incorrect fill price, failed stop submission, wrong exit triggered
  MEDIUM = missing cleanup/reset, edge-case state error, logging gap
  LOW = cosmetic logging issue, minor redundancy
(or "None")

### FIX VALIDATION
For any bug you flag for which a fix was proposed (in a prior session or report):
  FIX VALIDATION: PASS / FAIL / PARTIAL
  If FAIL: exact reason (e.g., "fix causes watchdog restart when X occurs")
  Alternative fix: file name + code pattern
(or "No prior fixes to validate")

### RECOMMENDED ACTIONS
(ordered by urgency — max 5 items; if CATASTROPHIC count > 0, item #1 must
address it)
"""


def _call_gemini(prompt: str) -> str:
    """Send prompt to Gemini, return text response. Falls back through model tiers."""
    models_to_try = [GEMINI_MODEL, "gemini-2.0-flash-lite"]
    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)
    last_err = None
    for model in models_to_try:
        try:
            logger.info(f"  Trying model: {model}")
            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )
            logger.info(f"  Success with {model}")
            return response.text or ""
        except Exception as e:
            logger.warning(f"  {model} failed: {e}")
            last_err = e
    logger.error(f"All Gemini models exhausted. Last error: {last_err}")
    return f"(Gemini API error — all models failed. Last: {last_err})"


def _slack(title: str, body: str, emoji: str = ":robot_face:") -> bool:
    """Post to Slack webhook."""
    if not SLACK_WEBHOOK:
        logger.warning("No SLACK_WEBHOOK_URL configured — skipping Slack post.")
        return False
    payload = json.dumps({"text": f"{emoji} *{title}*\n{body}"}).encode()
    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Slack send failed: {e}")
        return False


def _extract_verdict(report: str) -> str:
    """Pull verdict line from Gemini response."""
    for line in report.splitlines():
        if "VERDICT:" in line:
            if "FAIL" in line:
                return "FAIL"
            if "WARN" in line:
                return "WARN"
            if "PASS" in line:
                return "PASS"
    return "UNKNOWN"


# ── Issue-lifecycle suppression (signal-to-noise) ────────────────────────────
# The nightly LLM auditor is STATELESS — it re-flags the same known-benign and
# already-acknowledged items as fresh CATASTROPHIC/FAIL every day, training the
# operator to ignore a verdict that fires 5/7 days (alert fatigue). This adds a
# DETERMINISTIC post-filter driven by a curated lifecycle file so a FAIL means
# "new + real + act-now". Design (board Majors + Gro + GAI, 2026-07-15):
#   false_alarm  → removed from the card (benign, no operational impact)
#   acknowledged → kept VISIBLE but no longer forces FAIL (known, fix pending)
#   resolved     → if it REAPPEARS it is a REGRESSION: not suppressed, keeps FAIL,
#                  tagged so the operator sees a fixed item came back
# Safety invariants: (1) a finding matching NO directive is NEVER touched;
# (2) FAIL→WARN downgrade happens ONLY when zero real (unsuppressed) catastrophics
# AND zero unsuppressed CRITICAL new-bugs remain; (3) fail-open — any load/parse
# error posts the audit UNFILTERED; (4) the full report FILE keeps the LLM's
# original verdict (audit trail) — only the Slack card consumes the filtered view.
SUPPRESSIONS_FILE = LOGS_DIR / "audit_suppressions.jsonl"


def _load_suppressions() -> list[dict]:
    """Load curated lifecycle directives. Fail-open: any error → [] (audit unfiltered).
    A directive needs status in {false_alarm, acknowledged, resolved} and match_keywords."""
    if not SUPPRESSIONS_FILE.exists():
        return []
    out: list[dict] = []
    try:
        for ln in SUPPRESSIONS_FILE.read_text(errors="replace").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if (isinstance(d, dict)
                    and d.get("status") in ("false_alarm", "acknowledged", "resolved")
                    and isinstance(d.get("match_keywords"), list)):
                # Keep only non-empty STRING keywords — a malformed directive with a
                # numeric/None keyword is skipped, never stringified into a match target.
                d["match_keywords"] = [k for k in d["match_keywords"]
                                       if isinstance(k, str) and k.strip()]
                if d["match_keywords"]:
                    out.append(d)
    except Exception as e:
        logger.warning("suppressions load failed (%s) — auditing UNFILTERED (fail-open)", e)
        return []
    return out


# Hard masked-loss guard (2026-07-27, masked-loss cold seat): a finding LINE that
# mentions a genuine-alarm token can NEVER be keyword-suppressed — even if it also
# co-mentions a suppressed keyword (e.g. a real `pnl_unreconciled=true` finding whose
# visible symptom is a `pnl_drift` number). Keyword-on-line suppression is a blunt
# instrument; this makes silently dropping a genuine reconciliation alarm structurally
# impossible. Fails toward visibility (a protected line stays real/unfiltered).
_NEVER_SUPPRESS_TOKENS = ("pnl_unreconciled",)


def _match_directive(line: str, sup: list[dict]) -> dict | None:
    """Return the first directive with a match_keyword that is a case-insensitive
    substring of `line`, else None. Conservative: only curated keywords match.
    A line carrying any _NEVER_SUPPRESS_TOKENS is protected — it matches NO directive,
    so a genuine alarm can never be dropped or downgraded (masked-loss invariant)."""
    low = line.lower()
    if any(tok in low for tok in _NEVER_SUPPRESS_TOKENS):
        return None
    for d in sup:
        # match_keywords are guaranteed non-empty strings by _load_suppressions.
        for kw in d.get("match_keywords", []):
            if kw.lower() in low:
                return d
    return None


def _apply_suppressions(report: str, verdict: str) -> tuple[str, str, int, int]:
    """Deterministic post-filter. Returns
    (filtered_report_for_card, adjusted_verdict, n_suppressed, n_acknowledged).
    The full report FILE is written separately with the ORIGINAL report+verdict."""
    sup = _load_suppressions()
    if not sup:
        return report, verdict, 0, 0

    section = None            # 'cat' | 'bugs' | None
    n_suppressed = 0
    n_ack = 0
    real_cat_remaining = 0    # catastrophics NOT false_alarm/acknowledged (incl. regressions)
    real_crit_remaining = 0   # NEW-BUGS lines tagged CRITICAL that are NOT suppressed/acked
    declared_cat = None       # count the header declares (### CATASTROPHIC ALERT: N)
    cat_findings_seen = 0     # catastrophic finding LINES we actually processed
    out_lines: list[str] = []

    for raw in report.splitlines():
        s = raw.strip().lstrip("#").strip()
        up = s.upper()
        if up.startswith("CATASTROPHIC ALERT"):
            section = "cat"
            # Parse the declared count so the downgrade can be blocked if a catastrophic
            # is unaccounted (e.g. LLM put a finding on the header line, or malformed output).
            _tail = up.split(":", 1)[1].strip() if ":" in up else ""
            _tok = _tail.split()[0] if _tail else ""
            if _tok.isdigit():
                declared_cat = (declared_cat or 0) + int(_tok)
            out_lines.append(raw)
            continue
        if up.startswith("NEW BUGS FOUND"):
            section = "bugs"
            out_lines.append(raw)
            continue
        if raw.lstrip().startswith("###"):
            section = None
            out_lines.append(raw)
            continue

        is_finding = (section in ("cat", "bugs")
                      and raw.strip()
                      and not raw.strip().lower().startswith("none"))
        if is_finding and section == "cat":
            cat_findings_seen += 1
        if is_finding:
            d = _match_directive(raw, sup)
            if d is not None:
                st = d.get("status")
                if st == "resolved":
                    out_lines.append(raw.rstrip() + "  [REGRESSION — previously resolved]")
                    if section == "cat":
                        real_cat_remaining += 1
                    elif "CRITICAL" in up:
                        real_crit_remaining += 1
                    continue
                if st == "false_alarm":
                    n_suppressed += 1
                    continue  # drop entirely
                if st == "acknowledged":
                    n_ack += 1
                    out_lines.append(raw.rstrip() + "  [ACKNOWLEDGED — tracked, fix pending]")
                    continue  # kept visible, does NOT count as real
            else:
                if section == "cat":
                    real_cat_remaining += 1
                elif "CRITICAL" in up:
                    real_crit_remaining += 1
        out_lines.append(raw)

    filtered = "\n".join(out_lines)

    # Downgrade guard: block FAIL→WARN if the header declared more catastrophics than we
    # accounted for (unaccounted catastrophic → treat as real, keep FAIL). Fails safe.
    cat_unaccounted = declared_cat is not None and declared_cat > cat_findings_seen

    adjusted = verdict
    if (verdict == "FAIL" and (n_suppressed or n_ack)
            and real_cat_remaining == 0 and real_crit_remaining == 0
            and not cat_unaccounted):
        adjusted = "WARN"

    return filtered, adjusted, n_suppressed, n_ack


def _build_slack_summary(report: str, verdict: str, modified_count: int) -> str:
    """Extract key sections for Slack — keep it scannable."""
    verdict_emoji = {"PASS": ":white_check_mark:", "WARN": ":warning:",
                     "FAIL": ":red_circle:", "UNKNOWN": ":grey_question:"}[verdict]

    lines = [f"{verdict_emoji} *Verdict: {verdict}*",
             f"Modified files audited: {modified_count}",
             ""]

    # CATASTROPHIC ALERT is pulled first and uncapped — it must never be
    # truncated or buried behind lower-severity sections (S68 redesign).
    sections = ["CATASTROPHIC ALERT", "LOG ANOMALIES", "PERFORMANCE AUDIT",
                "TRADE INTEGRITY", "NEW BUGS FOUND", "RECOMMENDED ACTIONS"]
    section_caps = {"CATASTROPHIC ALERT": 50}  # everything else defaults to 8 below
    in_section = None
    section_lines: dict[str, list[str]] = {s: [] for s in sections}

    for line in report.splitlines():
        stripped = line.strip("# ").strip()
        if any(stripped.startswith(s) for s in sections):
            in_section = next(s for s in sections if stripped.startswith(s))
            continue
        if in_section:
            if line.startswith("###") and not any(line.strip("# ").startswith(s) for s in sections):
                in_section = None
                continue
            cap = section_caps.get(in_section, 8)
            if len(section_lines[in_section]) < cap:
                if line.strip():
                    section_lines[in_section].append(line.strip())

    catastrophic = section_lines.get("CATASTROPHIC ALERT", [])
    has_catastrophic = bool(catastrophic) and not any(
        ln.lower().startswith("none") for ln in catastrophic
    )
    if has_catastrophic:
        lines.insert(0, ":rotating_light::rotating_light: *CATASTROPHIC FINDING(S) — IMMEDIATE REVIEW* :rotating_light::rotating_light:")

    for section in sections:
        content = section_lines[section]
        if content:
            lines.append(f"*{section}*")
            lines.extend(f"  {ln}" for ln in content)
            lines.append("")

    ts = datetime.now(PT).strftime("%b %d · %I:%M %p PT")
    lines.append(f"_Full report: logs/gemini_audit_{AUDIT_DATE}.txt — {ts}_")
    return "\n".join(lines)


# ── Watchdog heartbeat freshness (who watches the watchdog) ──────────────────
# Board follow-up 2026-07-16: every watchdog's failure mode is SILENCE — if cron
# stops, a unit is disabled, or the script breaks, the alerting is simply GONE and
# nothing says so. scripts/service_watchdog.sh touches this file on EVERY */5 run
# (before any branching), so a fresh mtime proves exactly one thing: the watchdog
# ran. This nightly assert turns "the watchdog died" from invisible into one Slack
# a day. Deterministic (mtime math) — never an LLM judgment.
HEARTBEAT_FILE = LOGS_DIR / "svc_watchdog.heartbeat"
HEARTBEAT_MAX_AGE_MIN = 15   # watchdog runs */5 → >15min = 3 missed runs = dead


def _check_watchdog_heartbeat() -> None:
    """Alert if the service watchdog's heartbeat is missing or stale. Never raises."""
    try:
        if not HEARTBEAT_FILE.exists():
            msg = (f"WATCHDOG HEARTBEAT MISSING ({HEARTBEAT_FILE.name}) — "
                   f"scripts/service_watchdog.sh has not run. The service-DOWN "
                   f"watchdog may not be running at all (cron disabled? script "
                   f"missing/not executable?). Nothing is watching the services.")
            logger.critical(msg)
            _slack("Watchdog heartbeat MISSING", msg, emoji=":rotating_light:")
            return
        age_min = (datetime.now(UTC).timestamp() - HEARTBEAT_FILE.stat().st_mtime) / 60.0
        if age_min > HEARTBEAT_MAX_AGE_MIN:
            msg = (f"WATCHDOG HEARTBEAT STALE — last run {age_min:.0f} min ago "
                   f"(expected <= {HEARTBEAT_MAX_AGE_MIN}; it runs every 5 min). "
                   f"The service-DOWN watchdog has stopped. Nothing is watching "
                   f"the services. Check: crontab -l | grep service_watchdog")
            logger.critical(msg)
            _slack("Watchdog heartbeat STALE", msg, emoji=":rotating_light:")
        else:
            logger.info("Watchdog heartbeat OK (%.1f min old)", age_min)
    except Exception as _hb_e:
        # Never let the heartbeat check break the audit.
        logger.warning("watchdog heartbeat check failed: %s", _hb_e)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set in .env — aborting.")
        sys.exit(1)

    logger.info(f"=== Nightly Gemini Audit — {AUDIT_DATE} ===")

    # Who watches the watchdog: assert the service watchdog actually ran recently.
    # Runs first + independently so a dead watchdog is reported even if the audit
    # itself later fails (Gemini down, etc.).
    _check_watchdog_heartbeat()

    logger.info("Collecting bot.log...")
    log_lines = _collect_log_lines()

    logger.info("Collecting trade_events.jsonl...")
    trade_log = _collect_trade_events()

    logger.info("Collecting EOD snapshot...")
    eod = _collect_eod()

    logger.info("Collecting modified source files...")
    modified_files = _collect_modified_files()
    logger.info(f"  {len(modified_files)} file(s) modified in last 24h: "
                + ", ".join(modified_files.keys()) if modified_files else "  (none)")

    logger.info("Building prompt...")
    prompt = _build_prompt(log_lines, trade_log, eod, modified_files)

    logger.info(f"Calling Gemini ({GEMINI_MODEL})...")
    report = _call_gemini(prompt)

    verdict = _extract_verdict(report)
    # Deterministic signal-to-noise post-filter (curated lifecycle) — the report
    # FILE keeps the ORIGINAL verdict (audit trail); the CARD consumes the adjusted view.
    filtered_report, card_verdict_adj, n_sup, n_ack = _apply_suppressions(report, verdict)
    if n_sup or n_ack or card_verdict_adj != verdict:
        logger.info("suppressions: %d false-alarm removed, %d acknowledged; verdict %s → %s",
                    n_sup, n_ack, verdict, card_verdict_adj)
    logger.info(f"Verdict: {verdict} (card: {card_verdict_adj})")

    # ── Write full report ────────────────────────────────────────────────────
    # Original report + verdict preserved verbatim (Schneier: durable audit trail);
    # the suppression outcome is appended as a note, never overwriting the LLM output.
    report_path = LOGS_DIR / f"gemini_audit_{AUDIT_DATE}.txt"
    _tmp_report = report_path.with_suffix(".txt.tmp")
    _sup_note = (f"\n\n{'='*80}\nSUPPRESSION POST-FILTER: {n_sup} false-alarm removed, "
                 f"{n_ack} acknowledged | card verdict {verdict} → {card_verdict_adj} "
                 f"(see logs/audit_suppressions.jsonl)\n") if (n_sup or n_ack) else ""
    _tmp_report.write_text(
        f"Nightly Gemini Audit — {AUDIT_DATE}\n"
        f"Model: {GEMINI_MODEL} | Verdict: {verdict}\n"
        f"Modified files: {list(modified_files.keys())}\n"
        f"{'='*80}\n\n"
        + report + _sup_note,
        encoding="utf-8",
    )
    _tmp_report.replace(report_path)
    logger.info(f"Full report written → {report_path}")

    # ── Slack summary — Block Kit card (Rafael format-lock 2026-07-02) ───────
    # Card first; legacy text summary only as fallback so an audit never goes
    # silent if the renderer or its import fails.
    sent = False
    try:
        from scripts.audit_slack import (build_pnl_fields, render_card,
                                         validate_no_pnl_rewrite, post_to_slack,
                                         findings_from_report)
        try:
            eod_dict = json.loads(eod)
            if not isinstance(eod_dict, dict):
                eod_dict = {}
        except (json.JSONDecodeError, TypeError):
            eod_dict = {}  # "(No EOD snapshot found)" etc. → mismatch-free empty card
        pnl = build_pnl_fields("nightly", eod_dict)
        findings = findings_from_report(filtered_report)
        dist = [f"✅ full report — logs/gemini_audit_{AUDIT_DATE}.txt",
                f"✅ modified files audited — {len(modified_files)}"]
        if n_sup or n_ack:
            dist.append(f"⏭️ {n_sup} false-alarm suppressed · {n_ack} acknowledged "
                        f"(logs/audit_suppressions.jsonl)")
        card_verdict = card_verdict_adj if card_verdict_adj != "UNKNOWN" else "WARN"
        payload = render_card("nightly", AUDIT_DATE, card_verdict, pnl,
                              findings, dist_footer=dist)
        ok, reason = validate_no_pnl_rewrite(payload, pnl["injected_numbers"])
        if not ok:
            raise RuntimeError(f"P&L-rewrite validator blocked card: {reason}")
        sent = post_to_slack(payload) in (200, 204)
        if sent:
            logger.info("Slack Block Kit card posted.")
    except Exception as _card_err:
        logger.warning(f"Card render/post failed ({_card_err}) — falling back to text summary.")
    if not sent:
        # Text fallback also consumes the filtered/adjusted view for consistency.
        slack_body = _build_slack_summary(filtered_report, card_verdict_adj, len(modified_files))
        slack_emoji = {
            "PASS": ":white_check_mark:",
            "WARN": ":warning:",
            "FAIL": ":rotating_light:",
            "UNKNOWN": ":grey_question:",
        }[card_verdict_adj]
        title = f"Nightly Audit {AUDIT_DATE} — {card_verdict_adj}"
        sent = _slack(title, slack_body, emoji=slack_emoji)
        if sent:
            logger.info("Slack summary posted (legacy text fallback).")
        else:
            logger.warning("Slack post failed — check logs for full report.")

    logger.info("=== Audit complete ===")
    return verdict


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result in ("PASS", "WARN") else 1)

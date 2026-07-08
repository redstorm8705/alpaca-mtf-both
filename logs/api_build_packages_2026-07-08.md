# API Build Packages — 2026-07-08 session

**Purpose:** Pre-scoped patch packages produced in the INTERACTIVE session (flat-rate) so the
OCI headless API executor only IMPLEMENTS the diff, runs the board+Gro+GAI gate ON THE DIFF,
and ships. Per the Interactive-vs-API cost protocol (CLAUDE.md).

**Slate (Rafael 2026-07-08):** F + A + B + E this session. Sequence: **F + A co-top** (both are
mass-liquidation guards; F fired twice today) → **B** → **E**. C (event gate) + D (reliability) queued.
**QHM sizing decision (Rafael):** fixed $ slice per dip. **QHM per-name ceiling:** 20%.

**Status of THIS doc:** SCOPE + DESIGN locked. Still required in the interactive session before
handoff to API: (1) full-read gate on each file, (2) 10-pt audit + RC-1..8 → tb_audit_log.md,
(3) cold board-on-DESIGN incl. the risk-asymmetry masked-loss seat (A, B, E are all risk-path),
(4) Gro+GAI on the design. Then the API does board+Gro+GAI ON THE DIFF + ship (one board run each).

---

## Build A — Data-Integrity Safe Mode (risk-path; cold masked-loss board MANDATORY)

**Intent (plain English):** When Alpaca sends a bad data snapshot (positions vanish with no fills,
equity mis-reads), the bot must recognize it as a *glitch* and PAUSE — not trip the kill switch,
persist it, and freeze for hours (yesterday's cascade links 2–5). But it must NEVER mistake a
*real* loss for a glitch. The detector is explained-P&L reconciliation.

**Design (locked — from `logs/safe_mode_spec_2026-07-07.md`):**
- **Glitch iff** the equity drop is catastrophic AND `equity_pnl` (equity − start-of-day) diverges
  beyond tolerance from `explained_pnl = realized_today + unrealized_delta`, where
  `realized_today` = FIFO realized P&L from today's fills (reuse
  `risk_manager.update_daily_pnl_from_alpaca` logic, ~L447-663) and `unrealized_delta` = Σ current
  positions (current MV − SOD/last-good basis).
- **Safety asymmetry (HARD):** override / enter safe-mode ONLY on positively-confirmed divergence.
  On ANY ambiguity or API error → return RAW equity so the kill switch stays sensitive. Never mask
  a loss. (This is why the Phase-0 boundary cache FAILED — Scenario 7.)
- **Corroborators (together, not equity==cash+MV alone):** equity vs sma/last_equity contradiction
  + position-count collapse with zero fills + the explained-P&L divergence.

**Files & change points:**
- **A1 — NEW `execution/data_integrity.py`:** `DataIntegrityValidator` — single gate, computes
  `explained_pnl`, returns `(is_glitch: bool, reason: str)`. Fail-safe: any error → `is_glitch=False`.
- **A2 — `strategy/run_cycle.py`:** on suspect data enter SAFE MODE — block entries / order-mutations
  / external-close / state-writes, but KEEP scanning / monitoring / HTML / alerting (fixes the frozen
  scanner). Auto-recover when sane. NON-persistent.
  - **INTEGRATION DETAIL (from full read, 1865 lines):**
    - **Insertion point:** immediately after `portfolio_value = get_portfolio_value()` (L188), BEFORE
      `risk.update_portfolio_value()` (L189) and the kill-switch check (L191). Run the validator here;
      it already has equity + can fetch positions + fills.
    - **Carrier:** `gate_state` is already threaded through `run_cycle()` and into `execute_entries()`
      / `check_exits()`. Add `gate_state.safe_mode: bool` (default False) — set it here. Non-persistent
      (GateState is per-process; nothing to persist). Auto-recovers when the next cycle reads clean data.
    - **On glitch:** do NOT let the kill switch persist-trip (hand the flag to A3's `check_kill_switch`);
      log CRITICAL + Slack ONCE; then let the cycle CONTINUE its read-only work (scan HTML L1599/1783,
      dashboard L1804, news, MRI) but SKIP the mutation sites below.
    - **Mutation sites to gate (skip when `gate_state.safe_mode`):** premarket `_cancel_and_reconcile_gtc_stops`
      (L232); AH `record_exit`+`register_close` (L652/659), `submit_limit_order` cover (L701),
      `submit_gtc_stop_order` (L739); `_submit_rth_day_stops` (L827); `cancel_order` (L945);
      `_safe_close_all` news-halt (L1029); `check_partial_exits` (L1480), `_apply_mri_breakeven_push`
      (L1491), `check_exits` (L1494); `_run_fill_recon` (L1502); `qhm.maybe_enter_positions` (L1571);
      `execute_entries` (L1703). Keep: all `write_scan_html`, `generate_dashboard`, `news.scan_breaking_news`,
      `mri.refresh`, `_touch_cycle_ts` (watchdog must keep beating).
    - **NOTE (design fork for the board):** `check_exits`/`check_partial_exits` are legitimate exit
      management, but during a 404-style glitch they may see positions as vanished and fire false
      external-closes — which is why A4 gates the *recording* inside orphan_manager rather than only
      skipping the call here. Board to confirm: gate the exit CALLS in run_cycle (coarse, safe) vs. only
      gate the record_exit inside orphan (fine, keeps benign exit checks running). Leaning: gate both —
      skip the calls in safe-mode AND fail-closed inside orphan (defense in depth).
- **A3 — `execution/risk_manager.py`:** `check_kill_switch` (~L183-210) — tag kills glitch-vs-real;
  glitch kills do NOT persist/restore (yesterday they did → multi-hour halt). KEEP a secondary
  immediate trip at a conservative threshold so debounce can't delay a real fast crash.
- **A4 — `execution/orphan_manager.py` (+ `trade_engine.py` if applicable):** do NOT record
  external_close / mutate orders on suspect data.
  - **INTEGRATION DETAIL (from full read, 1624 lines):** two existing guards already defend the
    external-close sweep in `reconcile_positions()` — **Guard A** (L1231) fail-closes only when the
    Alpaca batch is *fully* empty while the tracker holds positions; **Guard B** (L1271-1308)
    re-verifies each symbol individually and fail-closes on batch/single *disagreement*. Yesterday
    beat both: 8/9 vanished (batch non-empty → Guard A silent) and the 404s were *consistent* across
    batch+single (Guard B saw agreement). **A4 = a third guard keyed on the explained-P&L divergence,
    not on emptiness/disagreement:** if `DataIntegrityValidator.is_glitch()` → skip the `_ext_closed`
    sweep (before L1269) AND skip the Patch-1 phantom-close (before L701/L714), alert once, retry next
    cycle. This catches the partial-vanish-with-consistent-404 case the other two guards structurally
    cannot.
- **Note:** `broker.get_portfolio_value()` (broker.py ~L66-69) stays `float(account.equity)` — do
  NOT resurrect the boundary-cache guard there. The validator lives in `data_integrity.py`, not broker.

**RC / invariants to hold:** RC-1 (tz-aware now), RC-3 (no silent except), RC-5 (atomic state writes),
never-mask-a-loss rule, paper=True untouched, SPY-5min sole entry gate untouched.

**Acceptance tests (must ship with the build):**
- Yesterday's case (positions gone, no fills, equity $751 vs SOD ~$2,844): explained≈0, equity_pnl≈−$2,093
  → divergence huge → GLITCH → safe-mode, kill NOT persisted. ✓
- **Scenario 7** (real: TSLA force-liquidated no *today* fill + AAPL/MSFT genuinely down; equity legit
  $2,100): explained≈−$744 ≈ equity_pnl → NOT a glitch → real loss halts. ✓ (MUST classify REAL.)
- API error mid-check → treat as REAL (raw equity through). ✓

**API gate:** cold board incl. Thorp/Taleb masked-loss seat with a concrete counterexample hunt;
Gro+GAI on diff; static (py_compile+mypy+ruff); cold second-agent; impact radius. Ship only on 3-way align.

---

## Build B — Orphan-stop root (risk-path; contained)

**Intent (plain English):** When a position is closed externally, the bot records the exit but leaves
its GTC stop order live. That stranded stop can later fill against nothing → a naked short (the
"inadvertent short bug, AGAIN"). Cancel the stop BEFORE recording the exit, fail-closed.

**Design (locked):** In each external-close path, call `cancel_open_gtc_orders(symbol)` (or the
existing broker cancel) and CONFIRM cancellation BEFORE `record_exit()`. If cancel fails → do NOT
proceed to record_exit; log CRITICAL + Slack (fail-closed, leaves position tracked, not stranded).
Plus a periodic orphaned-order sweep (order exists with no matching position → cancel + alert).

**Files & change points (confirmed via full read of orphan_manager.py):**
- `execution/orphan_manager.py` **L714** — Patch-1 phantom-close (`if _p1_alpaca_pos is None`, L701).
  Before `record_exit()`, cancel any stored `gtc_stop_order_id` / `rth_day_stop_order_id`; fail-closed.
- `execution/orphan_manager.py` **L1320** — `reconcile_positions()` external-close sweep (double-confirmed
  absent, L1294-1309). Same: cancel stored stop id(s) before `record_exit()`; fail-closed.
- `execution/portfolio_tracker.py` **~L965** — the in-tracker external-close path (needs a confirming
  full read of that file before implement; not yet re-read this session).
- Periodic sweep: `orphan_manager.py` (new small function on the existing cycle cadence) — an open order
  whose symbol has no matching Alpaca position → cancel + alert. Reuse `get_open_orders()` (already imported).

**RC / invariants:** RC-3 (log, don't swallow), RC-6 (Alpaca order/position field names verified against
live response — orphan_manager already reads `order.status`/`filled_avg_price`/`type`/`time_in_force`),
order-lifecycle integrity. Must not cancel a stop that belongs to a STILL-open partial (these 3 sites are
all FULL external-closes — position absent — so no live partial remains; the periodic sweep must check
position-absence per symbol before cancelling). Interaction note: at L714/L1320 the position is already
gone from Alpaca, so the stop is either (a) already filled/gone — cancel is a harmless no-op, or (b) a true
orphan — cancel prevents the naked-short. Cancel-fails → do NOT record_exit; retain + CRITICAL + Slack.

**Acceptance tests:** external close with a live stop → stop cancelled first, then exit recorded, no
stranded order. Cancel-fails path → exit NOT recorded, CRITICAL logged. Sweep finds a real orphan →
cancels it; finds a valid stop (position present) → leaves it.

**API gate:** cold board (execution-risk + reliability seats) + Gro+GAI on diff + static + second-agent + impact.

---

## Build E — QHM accumulation (never-sell + buy-dips + real sizing) (risk-path)

**Intent (plain English, Rafael's spec):** QHM holds are buy-and-hold-into-earnings. The bot must
NEVER sell them, and should BUY MORE on dips ahead of earnings, in real share sizes. Right now the
opposite is happening — GOOGL & NVDA (never-sell holds) have live SELL-STOPS on them, and adds are
1-share-floored.

**Design (locked):**
1. **NEVER SELL** — disable/remove every QHM auto-sell / exit / stop / force-close path in
   `quarterly_hold_manager.py`. Also cancel any EXISTING sell-stop on a QHM symbol on startup
   (GOOGL/NVDA have them now). QHM holds are exempt from `safe_close_all` / `check_exits`.
2. **BUY MORE on DIPS into earnings** — add-on when price dips ahead of the hold's earnings date.
3. **Real share sizing (fixed $ slice per dip):**
   `add_shares = max(1, floor(0.03 · equity ÷ price))`, gated by a per-name weight ceiling.
   - **Per-name weight ceiling = 20% (Rafael confirmed 2026-07-08).** At $2.8K equity a 3% slice
     ($83) buys 0 whole shares of GOOGL ($361) and one share is already ~13% of equity, so the
     ceiling must permit ≥1 share of the priciest hold. 20% per name (2-name book = 40% concentrated)
     is acceptable for explicit buy-and-hold conviction (Sosnoff), not intraday risk-per-trade, and
     auto-relaxes as equity grows and the slice buys more shares (Thorp). Revisit the ceiling per
     equity tier — do NOT hard-code it as permanent; log it as a tier-reviewable constant.
   - NO calendar tranches, NO stop-recalc, NO concentration gymnastics (Rafael: keep it SIMPLE).
4. Buys only fire when the bot is un-halted — safe to build now while halted.

**Files & change points:** `execution/quarterly_hold_manager.py` (1954 lines — **full read still owed**
before implement; this is the most invasive of the three). Startup sell-stop cancellation for QHM symbols.
- **STRUCTURE FOUND (grep, pre-full-read — must be confirmed by the full read):** the current file is
  built on exactly what the spec removes — `_TRANCHE_FRACTIONS=[1/3,1/3,1/3]` + `_TRANCHE_DAYS=[1,3,5]`
  calendar tranches (L71-72), `target_equity_pct=0.10` (L303/324) → becomes **0.20** (confirmed ceiling),
  tranche sizing at L1496-1540 (`target_notional = available_equity * pos.target_equity_pct * tranche_frac`),
  entry at `maybe_enter_positions()` (L691), and sell/exit via `run_weekly_check()` (L606) +
  `broker.close_position` (per header L31). E must: rip out the sell/exit/force-close paths, replace the
  calendar-tranche sizing with the fixed-$ dip-add `max(1, floor(0.03·equity ÷ price))` capped at 20%,
  and drop the 1-share floor. **This is a rewrite of the entry/exit core, not a surgical edit — it needs
  the full 1954-line read + the heaviest board-on-design pass of the three.**

**RC / invariants:** must not reuse the intraday risk-per-trade floor; QHM tracked separately from
trade_log; RC-4 (fill price from fills, not estimate) on any add record; never-sell is safety-positive.

**Acceptance tests:** QHM hold never gets a sell/stop/force-close; existing sell-stop on GOOGL/NVDA
cancelled on startup; qualifying dip → `max(1, floor(...))` shares bought; no add when at/over ceiling;
halted bot fires no buys.

**API gate:** cold board (Sosnoff/Thorp/Harris) + Gro+GAI on diff + static + second-agent + impact.

---

## INCIDENT 2026-07-08 07:53 PT — FALSE NEWS HALT mass-liquidation (NEW root cause)
`news_monitor` classified the Currents headline *"Can US President Trump 'cut off all trade' with
Spain?"* as **HALT** on keyword `national emergency` → `news.get_news_size_multiplier()` returned 0.0
→ `run_cycle` L1027 fired `_safe_close_all(circuit_breaker=True)` → liquidated all 6 non-QHM positions
(MARA −$24.00, MSTR +$7.22, HOOD −$11.16, SNOW −$4.67, AVGO +$10.80, MS −$4.65; net ≈ −$26.46) in 11s.
No real circuit breaker, no price confirmation. QHM (NVDA/GOOGL) correctly skipped as protected. This is
DISTINCT from the 2026-07-07 Alpaca desync — a second, independent mass-liquidation vector.

### Build F-INTERIM ✅ SHIPPED 2026-07-08 (commit 9d03be1) — disarm news-keyword liquidation
**STATUS: LIVE on OCI.** Gate: cold board 3/3 (masked-loss/cross-strategy/reliability) + Gro APPROVE +
GAI APPROVE. Final pre-ship: GAI REJECT (2 misreads: local-var scoping + assumed-real-halt-breaker) →
withdrawn on counter-prompt → APPROVE; Gro REJECT (inverted QHM-placement read) → withdrawn on
counter-prompt → APPROVE; preship_audit marker clean (gro=APPROVE gai=APPROVE). Static: py_compile +
ruff(E,W,F,B) + mypy clean. Deployed git-single-channel (push → OCI ff-only pull → restart → DEPLOY_OK →
health-verified, no ImportError, live RTH cycle running). Removed now-unused `_safe_close_all` import.
Remaining: **Build F full redesign** (price/exchange-signal HALT) still on the slate; then A, B, E.


**Intent:** stop the live false-HALT liquidation immediately with the smallest safety-positive change.
A news-keyword HALT must block NEW ENTRIES only — never liquidate the book.
**Change (run_cycle.py ONLY — file fully read this session):**
- L1025-1032: replace the `news_size_mult == 0.0 → _safe_close_all(circuit_breaker=True) → return` block
  with: set `news_halt_block_entries = True`, log CRITICAL ("news-keyword HALT — blocking NEW ENTRIES,
  positions retained, no liquidation"), and DO NOT close / DO NOT early-return (so exit management at
  L1480-1494 still runs for existing positions).
- Add near the EXTREME/BV-5 entry blocks (~L1576, before `run_scan`/`execute_entries`):
  `if news_halt_block_entries: log; _touch_cycle_ts(); return`.
- `_safe_close_all` itself is UNCHANGED (still available for a real price/exchange trigger in the F
  redesign). No `news_monitor.py` change (it still returns 0.0 on keyword HALT; run_cycle just stops
  liquidating on it).
**Cross-strategy:** removes the only news-triggered `safe_close_all(circuit_breaker=True)` call — no more
QHM/Bucket-A/Movers interaction on this path. Existing-position protection unchanged (GTC stops + SPY
EXTREME engine intact). **Risk-path — cold masked-loss board mandatory** (claim to stress-test: removing
a FALSE liquidation is strictly safety-positive because real halts are covered by stops + the price engine).
**Gate = LAST STEP before API:** cold board (incl. Thorp/Taleb masked-loss seat) + Gro + GAI on this exact
diff → on 3-way alignment → API implements + final pre-ship Gro/GAI + ships. Then the full F redesign.

**GATE RESULT (2026-07-08) — 3-WAY ALIGNED, SHIP:**
- Masked-loss seat (Thorp/Taleb): **APPROVE** — no masked-loss path found; keyword liquidation is a
  state-independent, negative-EV self-inflicted left-tail; change is a Pareto improvement (exits now run).
- Reliability/logic seat (Peterffy/Kim): **APPROVE** — no inversion, flag defined on all paths (NameError
  impossible), both branches complete, watchdog covered by the return's own `_touch_cycle_ts()`.
- Cross-strategy seat (Harris): **APPROVE-WITH-CHANGES** — QHM/Bucket-A/Movers all safe; the session
  halt-flag NOT being set is CORRECT (per-cycle self-clearing kills the entry-blocking half of the same
  false-positive); 2 required doc-only refinements below.
- GAI: **APPROVE** (clean). Gro: **APPROVE-WITH-CHANGES** (soft validation only, no code change).
**REQUIRED refinements folded into the final diff (comment-only, no logic change):**
1. At the new entry-block: explicit comment that blocking QHM here is a DELIBERATE divergence from
   EXTREME/BV-5 orthogonality, INTERIM-only, revisit in Build F (don't "fix" it back to orthogonal).
2. At change 1: comment that the interim deliberately does NOT set the session `_halt_entries_for_session`
   flag — the block is per-cycle and self-clears when the (false) keyword ages out of the news window.
**Optional (deferred to Build F, matches EXTREME's behavior):** the new return skips the `scan_results.html`
write (BV-5 writes it). Left out to keep the interim minimal; noted for Build F observability parity.

### Build F (SLATE — co-top with A, Rafael 2026-07-08) — HALT & MASS-LIQUIDATION ARCHITECTURE REDESIGN
**Rafael 2026-07-08: "revisit the ENTIRE architecture around halts and mass liquidations."** Expanded
from a keyword tweak to an architecture redesign after the 2nd false mass-liquidation this week.

**ROOT CAUSE (code-verified this session — full read of the relevant surfaces):**
- `news_monitor._classify()` (L446-452) classifies HALT by **raw substring match** (`if k in lower`)
  against `KEYWORDS_HALT` (L104-109), which contains `"national emergency"`. Trump tariffs are enacted
  via national-emergency/IEEPA declarations → the phrase appears in routine tariff coverage → a
  *question* headline ("Can Trump cut off all trade with Spain?") matched → HALT.
- `get_news_size_multiplier(price_change_pct=0.0)` (L1679): the price param is **"Unused — kept for
  call-site compatibility"** (docstring L1686); the HALT branch (L1700-1701) returns 0.0
  **unconditionally** ("always enforce"), ignoring price. `PRICE_CONFIRM_THRESHOLD = 0.5` (L75) is a
  **dead constant**. `run_cycle` L993 calls it with **no price**. → **The price-confirmation gate that
  once existed was stripped from the HALT path in the April-2026 rebuild.** HALT is the ONLY tier that
  bypasses the market-reaction-first principle every other tier follows.
- FALSE NEGATIVE too: "revokes Iran oil license" matches NO keyword set (GEO_ENERGY has "oil embargo"/
  "oil supply"/"opec cut", not "oil license"/"revokes") → a real supply shock is invisible. The keyword
  approach is too loose for false emergencies AND too narrow for real ones.

**REDESIGN QUESTIONS (for the board + Gro + GAI architecture review — MODE 2):**
1. Should a NEWS signal ever trigger mass liquidation at all, or only block new ENTRIES? (Existing
   positions already have stops; price action manages exits — the architecture's own thesis.)
2. If a real "close everything" reflex is kept, what should trigger it — a real PRICE circuit-breaker
   (SPY level-1/2/3 % move), an Alpaca exchange-halt/clock signal, or corroborated price + news — never
   a raw keyword? (Wire the vestigial `price_change_pct`/`PRICE_CONFIRM_THRESHOLD` into a real gate.)
3. Keyword-set overhaul: eliminate substring false-positives (question headlines, "national emergency"
   in tariff coverage) and false-negatives (real energy/supply shocks). Consider phrase/context matching
   or dropping keyword-driven HALT entirely in favor of price/exchange signals.
4. Cross-strategy: `safe_close_all` interacts with QHM (protected — held today), Bucket A (circuit_breaker
   closes it), and retired-Movers lots — map all before changing the trigger.

**Files:** `events/news_monitor.py` (`_classify`, `KEYWORDS_HALT`, `get_news_size_multiplier`,
`PRICE_CONFIRM_THRESHOLD`), `strategy/run_cycle.py` L1025-1032 (`news_size_mult == 0.0` → close-all),
`events/handlers.py` (`safe_close_all`, 131 lines). **Risk-path — cold masked-loss board mandatory**
(must NOT weaken response to a REAL exchange halt while removing the false-positive liquidation).

## PROCESS DIRECTIVE (Rafael 2026-07-08) — the board+Gro+GAI audit is the ABSOLUTE LAST STEP
No proposal goes to the API until the FULLY-MAPPED proposal has been audited by the cold board + Gro +
GAI as the final step. "Fully mapped" = 100% scope, explicitly accounting for: **cross-strategy
implications** (e.g., `safe_close_all` ↔ QHM ↔ Bucket A ↔ retired-Movers shared Alpaca lots — the
2026-06-30 cross-strategy audit roadmap item), **existing/pre-existing bugs**, and **hotspot files**
(`portfolio_tracker.py`, `main.py`, `broker.py`). Not just the target diff in isolation.

### Build F — DESIGN REVIEW RESULT (autonomous session, 2026-07-08) — 6/6 UNANIMOUS, queued for Rafael

**Full read gate satisfied this session:** `events/news_monitor.py` (1828L, Explore subagent verbatim),
`events/handlers.py` (132L, direct read), `strategy/run_cycle.py` L980-1619 (re-confirmed). 10-pt audit +
RC-1..8 appended to `logs/tb_audit_log.md`. MODE 2 board (Taleb/masked-loss, Harris/microstructure,
Simons/signal-consistency, Peterffy+Kim/reliability — 4 cold parallel seats) + Gro + GAI (direct API, same
lean prompt, no leading conclusions) all independently reviewed the 4 design forks. Full decision package
with plain-English recap: `logs/pending_claude_session_2026-07-08.md`.

**Result — 6/6 unanimous on all 4 forks, no disagreement to resolve:**
1. News signals must NEVER trigger mass liquidation — entries-only, permanently. Confirms/hardens the
   interim (commit 9d03be1).
2. A real "close everything" trigger, if kept, must be built FRESH on real SPY-price-threshold and/or
   Alpaca exchange-halt signals — never news. The dead `PRICE_CONFIRM_THRESHOLD`/`price_change_pct` should
   be REMOVED as dead code, not resurrected/rewired as-is (4/4 board seats explicit on this; Gro's answer
   was ambiguous/less specific — noted, doesn't break consensus since Gro also rejects news-alone as a
   trigger).
3. Keyword-driven HALT should be retired entirely — not improvable via better phrase-matching, since the
   false-positive (Spain tariff question) and false-negative (Iran oil-license) failures are the same
   structural defect (unbounded natural language vs. a bounded, high-consequence decision), not two
   independent bugs. Keywords retained for CAUTION/MONITOR (already zero size impact, Architecture
   Invariant #2 — unchanged).
4. Cross-strategy: no NEW collision found. Flagged for explicit re-verification (not silent inheritance):
   the QHM guard in `safe_close_all()` is a QHM-specific allowlist, not a general ownership system — the
   retired Movers strategy's dormant, untagged lots will be swept by any future real circuit-breaker call
   exactly like main-bot lots. Multiple board seats (Taleb, Kim) say this is probably the desired behavior
   but must be an explicit, tested, documented decision before ship — not assumed. Board (Taleb, citing the
   existing "Audit Efficacy Not Presence" project rule from the Movers incident) also flags: re-verify the
   QHM/Bucket-A guards actually fire correctly under a REAL circuit-breaker call, cross-process, not just
   confirm the code exists.

**Additional board-only findings (informational, not required for Rafael's decision, feed eventual
implementation):** (a) Harris — simultaneous market-order liquidation of multiple names is itself an
execution-quality cost independent of trigger validity; consider staggered/sequenced unwinds in the
eventual build. (b) Kim — the new trigger should emit pre-action telemetry (structured event at the
decision point, not just after execution) + an "amber zone" pre-warning Slack alert before the hard
trigger crosses. (c) Kim/Peterffy — implement as a single named gate function (e.g.
`confirm_liquidation_trigger(spy_move, exchange_signal)`) with both signals as MANDATORY arguments, not
optional/unused params (avoids reproducing this exact class of silent-strip failure). (d) Kim — the
already-logged dormant ThreadPoolExecutor reliability gap in `scan_breaking_news()` could be bundled into
this same session's file-touch (cheap now, expensive to reopen) — Rafael's call, not required.

**Status: awaiting Rafael's confirmation (5 questions in `pending_claude_session_2026-07-08.md`).** No
code has been proposed or changed. Once confirmed, this becomes a pre-scoped patch package for the API-side
implementation (board + Gro + GAI re-review the actual diff before ship, per standing protocol).

---

## Queued (NOT this session's API slate)
- **C — per-symbol event gate** (FMP /corporate-actions → entry block + hold flag). Effort S/M.
- **D — autonomous reliability** (heartbeat/dead-man's-switch + Groq UA header). Effort S.
- **F — CLAUDE.md decomposition** (lean binding-core + move append-only history to linked docs/) —
  execution-governing doc → needs the Gro+GAI gate; lowers cost for the code-patching API runs.

## Handoff mechanics (per build, on the API side)
Each build = its own full sequence (RULE C-6, one file fully through before the next). API executor
implements the pre-scoped diff → cold board-on-DIFF (incl. masked-loss seat for A/B/E) → Gro+GAI on
diff (Gro-skip if unavailable + GAI APPROVE) → static → second-agent → impact → `preship_audit.py`
→ commit → push → OCI `git pull --ff-only` + restart → verify. Auto-ship on 3-way alignment.

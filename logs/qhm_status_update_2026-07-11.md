# QHM Status Update — July 11, 2026
**Prepared:** CCR autonomous session (Jul 11 00:05 ET) | **Boards:** 4 cold agents, parallel
**Basis:** Original S47f research (2026-06-04) + Q3 board votes (2026-06-20) + this session's web research + fresh board vote
**DS/GAI:** NOT available this session (no .env). Integration code changes require Gro+GAI in next session.

---

## ⚡ EXECUTIVE SUMMARY — ACTION THIS WEEK

| Item | Action | When | Who | Gate |
|------|--------|------|-----|------|
| **Minimum safety gate** | Add `QHM_MANUAL_HOLDS` to `config.py` + 1-line `get_quarterly_hold_symbols()` fix | BEFORE any manual entry | Gated session (Steps 1–9, Gro+GAI) | RTH chain — Gro+GAI mandatory |
| **GS Q2 earnings** | Watch July 14. If beats $14.47 EPS: enter manually July 15, 10:05 AM gate | Jul 15 | Manual | Minimum gate must be live |
| **GE Q2 earnings** | Watch July 16. Entry July 25 (config `not_before_date`). Day 3 post-earnings. | Jul 25 | Manual | Minimum gate + not_before_date fix |
| **GEV Q2 earnings** | Watch July 22. Entry July 29 earliest (2-day stagger post-GE). Conditional on GEV earnings call addressing wind losses and tariff cap. | Jul 29+ | Manual | Minimum gate |
| **LLY Q2 earnings** | Aug 5. Entry Aug 7. R:R revised target $1,350 (old $1,215 broken). | Aug 7 | QHM auto (if P0-a ships) or Manual | P0-a steps (a)–(d) — start now |
| **P0-a wiring** | Begin `launch_init` (step a) → fill attribution (step b) immediately. LLY Aug 7 is achievable if P0-a starts this week. | Start now | Gated sessions | Full Steps 1–9 each file |

---

## Updated Market Data (July 11, 2026)

| Symbol | June Research Price | July 11 Price | Delta | Key New Info |
|--------|--------------------|--------------|----|------|
| **GS** | ~$1,097 (52-wk high) | ~$1,050-1,061 | −3.5% | Earnings Monday Jul 14. EPS consensus $14.47. |
| **GE** | ~$360 | ~$359 | Flat | ATH $377 (Jul 2), pulled back. Analyst PTs: Citi $431, Jefferies $455. Earnings Jul 16. |
| **GEV** | ~$1,113 | ~$1,075 | −3.4% (from research); −10.1% from ATH $1,196 (Jul 6) | **Unexplained -10% on Jul 7.** Turbines sold out through 2030. Earnings Jul 22. |
| **LLY** | ~$1,098 | ~$1,189 | **+8.3%** | NEW: Medicare GLP-1 Bridge began Jul 1. Retatrutide Phase 3: 28.3% weight loss. ATH close $1,236 (Jul 7). |

---

## 3-Point Board Synthesis

### POINT 1 — Alignment Across 4 Boards

| Finding | Analytics | BoD | Technical | Execution | Consensus |
|---------|-----------|-----|-----------|-----------|-----------|
| GEA + LLY = top 2 | ✓ | ✓ | — | ✓ | **3/3 voting** |
| GS post-earnings entry valid | ✓ (if beats + Day 3 closes above $1,097) | ✓ (marginally, beta-dominant) | ✓ (GS in config) | ✓ (10:05 AM gate) | **4/4 CONDITIONAL** |
| GEV: wait for earnings confirmation | ✓ | ✓ | ✓ | ✓ | **4/4 unanimous** |
| Manual entry only (GS/GE/GEV) | — | — | ✓ | ✓ | Integration timeline makes it only viable path |
| Minimum safety gate required FIRST | — | — | ✓ (HARD) | ✓ (HARD) | 2/2 technical voices — BLOCKING |
| LLY target must be raised to $1,350 | ✓ (implied) | ✓ (explicit — old R:R broken) | — | — | 2/4 flagged; affects all |

### POINT 2 — What Boards Agree On That Original Research Missed

1. **GS is already excluded in `quarterly_holds_config.json`** — The June 20 research memo said "GS entry July 15+ (post-earnings)." The code already reflects the board's decision from S61 (documented as "GS window closed"). A manual GS position without the minimum safety gate would be adopted as an **orphan intraday trade** — no QHM protection whatsoever. This is P0 before any entry.

2. **GE's `not_before_date` is July 25, NOT July 21** — The June 20 memo said "GE entry July 21 (Day 3)." The config enforces July 25. If Rafael places GE manually before July 25, QHM is in PENDING_ENTRY state and GE shares are unprotected from the orphan adoption race (startup-to-10:05 AM window).

3. **LLY R:R is broken at $1,189 with $1,215 target** — BoD math: ($1,215 − $1,189) / ($1,189 − $980) = $26 / $209 = 0.12:1. This is effectively negative expected value. **Old target is stale.** Board requires target raised to $1,350 minimum (reflecting Retatrutide Phase 3 + Medicare bridge) before LLY entry is board-approvable.

4. **RC-7 guard creates 43-48% of account per tranche** for GEV ($1,075) and LLY ($1,189) — The `max(int(raw_qty), 1)` guard in `quarterly_hold_manager.py:1518` was built to prevent zero-share ghost orders. At $2,500 account: a 15% tranche target = $125 per tranche. At $1,075, raw_qty = 0.116 → int = 0 → max = **1 share = $1,075 = 43% of account**. First tranche alone consumes nearly half the account. This needs a code fix before automated QHM entry for GEV or LLY.

### POINT 3 — New Forward-Looking Issues From This Session's Boards

1. **GEV -10% single-day drop (Jul 7) is an unresolved Bayesian signal** (BoD: HIGH priority). BoD ruling: "Informed observers read this as a Bayesian update — the -10% contains information about who is selling and why." Binding BoD ruling: if GEV's July 22 earnings call reveals wind EBITDA losses worse than -$300M guidance OR tariffs above $350M ceiling → skip GEV for Q3 2026 entirely.

2. **P0-a must start immediately for LLY Aug 7 automated entry** (Technical Board). 27 days minus the strict sequential gate (9–12 days) leaves a 15–18 day buffer. That buffer disappears if any mid-build incident triggers a drift-freeze. Technical Board: CONDITIONAL GO if P0-a is prioritized with no parallel builds.

3. **GS beta dominance** (BoD: Kyle). "When 60-70% of revenue variance is explained by SPY and XLF, the alpha component of any quarterly trade is dominated by factor exposure." GS is a SpaceX-fee trade, not a structural moat trade. If the SpaceX fee is already in consensus ($14.47 vs prior $13.75 guidance), the beat thesis is weaker than the June board model assumed. GS is the lowest conviction pick — account size already makes it the odd pick out.

---

## Full 4-Board Vote Table

### A. Validity Check (as of July 11)

| Symbol | Analytics | BoD | Technical | Execution | Verdict |
|--------|-----------|-----|-----------|-----------|---------|
| GE Aerospace | **VALID, Upgraded** | **VALID** (lowest tail risk) | Config correct, `not_before_date` Jul 25 | Config-confirmed (Jul 25) | ✅ VALID |
| LLY | **VALID, Upgraded** 8.25→8.5/10 | **VALID** — but R:R broken (raise target to $1,350) | Not_before_date Aug 7, auto entry wired | RC-7 sizing bug at $1,189 | ✅ VALID w/ target revision |
| GS | Conditional valid | Beta-dominant, marginally valid | Already excluded from config | Config-excluded | ⚠️ CONDITIONAL (manual only; minimum gate required) |
| GEV | **Flagged — watch $1,075** | **CONDITIONAL** — -10% is unexplained signal | PENDING_ENTRY race risk | Day 3 (Jul 25+), not Day 1 | ⚠️ CONDITIONAL — earnings call July 22 must address wind/tariffs |

### B. GS Post-Earnings Entry

| Board | Position |
|-------|---------|
| Analytics (Brandt) | Enter if Day 3 closes **above $1,097** 52-wk high. If beats but can't reclaim $1,097 → pass. |
| Analytics (Thorp) | Current $1,050-1,061 IMPROVES R:R vs. June. Lower entry = larger expected fractional profit. |
| BoD (Taleb) | Beta-dominant, but prior "concave payoff" objection REDUCED now that stock is off 52-wk high. |
| BoD (Kyle) | If SpaceX fee already in $14.47 consensus, PEAD signal is weaker. Confirmation gate still required. |
| Execution (Harris) | Entry at **10:05 AM ET** on July 15. First 30-min bar close > prior close × 0.85. NOT at open. |
| Technical (Beck) | GS is config-excluded. Manual entry requires minimum safety gate (Q-B) deployed first. |
| **Synthesis** | **Enter manually July 15 at 10:05 AM if** (1) Q2 beats $14.47 EPS, (2) minimum gate is live, (3) Day 3 close ≥ $1,097. At $2,500 account: 1 share GS = ~$1,055 = 42% equity (too large for a 3rd position alongside GEA+LLY). |

### C. GEV -10% Drop

| Board | Position |
|-------|---------|
| Analytics (Thorp) | Lower price improves Kelly fraction IF fundamentals intact. BUT higher realized vol → reduce size. |
| Analytics (Asness) | -10% broke short-term momentum. Wait for recovery to $1,100-1,120 before July 22 entry. |
| Analytics (Brandt) | Observe 5-7 days. If sellers exhaust and price recovers → confirmed opportunity. |
| BoD (BoD unanimous) | Binding ruling: if July 22 call reveals wind EBITDA worse than -$300M or tariffs above $350M → **skip GEV Q3 entirely**. |
| Execution (Harris) | Day 3 (Jul 25+), not Day 1. Pre-ATH correction demands confirmation gate. |
| Technical | PENDING_ENTRY race: if bot restarts after manual buy but before 10:05 AM → orphan adoption. Minimum gate closes this. |
| **Synthesis** | **WATCH through July 22 earnings. Enter July 29 (GE+2 day stagger) ONLY IF** (1) July 22 call does not flag worse-than-guided wind or tariffs, (2) price has stabilized/recovering toward $1,100+. Minimum gate must be live. |

### D. LLY at New Highs

| Board | Position |
|-------|---------|
| Analytics (Thorp) | New information increases win probability AND payoff. Kelly fraction has increased even at $1,189. |
| Analytics (Asness) | Quality-Minus-Junk: high-quality companies at premium prices outperform on risk-adjusted basis. Prior targets are stale. |
| BoD (BoD) | **R:R is BROKEN at $1,189 with $1,215 target.** $26 gain / $209 loss = 0.12:1. Raise target to $1,350 minimum OR defer to post-Aug 5 reset. |
| BoD (Taleb) | Medicare bridge = convex positive + concave CMS tail. Tail is 2027+ event, not in Q3 hold window. |
| **Synthesis** | **VALID** but requires target revision: $980 stop (unchanged), $1,350 target (revised from $1,215 using Retatrutide + Medicare upside). R:R at revised target: ($1,350 − $1,189) / ($1,189 − $980) = $161 / $209 = **0.77:1**. Improved over the 0.12:1 broken model but below the June 1.29:1. Accept given secular thesis strength. Entry Aug 7 unchanged. |

### E. Account Sizing (2 of 4 picks)

| Board | Top 2 |
|-------|------|
| Analytics | **GEA (3 shares @ ~$1,077) + LLY (1 share @ ~$1,189)** |
| BoD | **GE + LLY** (highest alpha concentration, lowest pairwise correlation) |
| Execution | GE (1 share = $359 ≈ 14% of account, correct sizing). LLY/GEV need fractional code fix. |
| **Synthesis** | **GEA + LLY** as top 2. GS is the odd pick out at this account size (1 share = 42%). GEV is conditional on July 22 earnings. |

---

## Entry Calendar (Updated July 11, 2026)

```
Today:    July 11, 2026 (Saturday)

PREREQUISITE (all entries):
  Minimum safety gate PR — Steps 1-9, Gro+GAI — must ship BEFORE any entry
  Config: add QHM_MANUAL_HOLDS = frozenset({"GS", "GE", "GEV", "LLY"})
  quarterly_hold_manager.py: return frozenset(_qhm_syms) | config.QHM_MANUAL_HOLDS

GS Q2 earnings:    July 14, 2026 (Monday)
GS entry:          July 15, 2026, 10:05 AM ET — IF beats $14.47 EPS AND Day 3 ≥ $1,097
                   NOTE: GS = 42% of account → blocks GEA or LLY entry until sold
                   ALPHA WARNING (BoD): GS is beta-dominant, not alpha
                   ⚠ DO NOT combine GS + GEA at $2,500 account

GE Q2 earnings:    July 16, 2026
GE entry:          July 25, 2026 (config not_before_date — Day 3 per QHM logic)
                   Entry: 1 share ≈ $359 ≈ 14% of account

GEV Q2 earnings:   July 22, 2026
GEV entry:         July 29+ (BoD stagger: GE first, GEV 2+ trading days later)
                   CONDITIONAL: GEV call must not flag wind > -$300M EBITDA or tariffs > $350M
                   Entry: 1 share ≈ $1,075 ≈ 43% — RC-7 sizing bug, oversizes position
                   If GEV flags wind/tariffs: SKIP Q3 2026 entirely

LLY Q2 earnings:   August 5, 2026
LLY entry:         August 7, 2026 (Day 2 post-earnings)
                   REVISED TARGET: $1,350 (from $1,215 — Medicare + Retatrutide)
                   R:R at $1,189 entry: 0.77:1 (revised), acceptable given secular thesis
                   SIZING BUG: 1 share = $1,189 = 48% of account (RC-7 — needs code fix for fractional)
                   QHM automation achievable if P0-a ships by Aug 1-3

RECOMMENDED COMBINATION at $2,500 account:
  GEA (Jul 25) + LLY (Aug 7) — 4/4 boards top 2
  Combined at 3 shares GE ($1,077) + 1 share LLY ($1,189) = $2,266 = 81% deployed
  Buffer: $516 ($2,782 current equity - $2,266)
  ⚠ Do NOT add GS to GEA+LLY — account too small for 3 positions
```

---

## Pre-Entry Safety Gate — REQUIRED BEFORE FIRST MANUAL ENTRY

**Two-file minimal change. Steps 1-9 required (RTH chain). Gro+GAI mandatory (Rule C-5).**

### What breaks without it (Technical Board):

| Vector | Risk | Exposed Symbols |
|--------|------|-----------------|
| Orphan adoption | Bot restarts after manual buy → Alpaca position not in `_quarterly_hold_symbols` → adopted as intraday orphan with ATR stop | **GS** (zero config entry), GE pre-Jul 25, GEV pre-10:05 AM |
| Intraday exits | `check_exits()` fires on adopted orphan → stop-loss close at ATR floor | All manually-held symbols not in protected set |
| `safe_close_all()` | User shutdown (circuit_breaker=True) closes ALL non-QHM tracker positions | Any manual hold not recognized as QHM |

### The fix (2 changes, 1 commit):

**config.py** — add:
```python
QHM_MANUAL_HOLDS: frozenset = frozenset({"GS", "GE", "GEV", "LLY"})
```

**execution/quarterly_hold_manager.py** — line ~143, change:
```python
# BEFORE:
return frozenset(_quarterly_hold_symbols)

# AFTER:
return frozenset(_quarterly_hold_symbols) | getattr(config, "QHM_MANUAL_HOLDS", frozenset())
```

This propagates through all four existing protection layers (orphan_manager, exit_logic, entry_logic, handlers.safe_close_all) with zero changes to those files.

**Cannot be done in CCR (no .env → no Gro+GAI). Requires in-person session.**

---

## P0-a Integration Roadmap (LLY Aug 7 window)

**Goal: QHM automated entry for LLY on Aug 7. 27-day window. Timeline is viable IF P0-a starts immediately with no parallel builds.**

| Step | Target | Action | Files | Days | Gate |
|------|--------|--------|-------|------|------|
| a | Jul 15 | `launch_init` — seed ownership_ledger.json from current Alpaca book (all→intraday, NVDA/GOOGL→QHM manual) | `execution/ownership_guard.py:launch_init()` + new script | 1–2 | Steps 1–9, Gro+GAI |
| b | Jul 20 | Fill→tier attribution: `client_order_id` prefix, portfolio_tracker ~15 call sites, untagged→halt | `portfolio_tracker.py` (~965L), orphan_manager/run_cycle/main/QHM callers | 5–6 | Steps 1–9 each file, Gro+GAI |
| c | Jul 25 | Per-cycle `reconcile_drift()` in `run_cycle.py` after (b) verified live | `strategy/run_cycle.py` | 2–3 | Steps 1–9, Gro+GAI |
| d | Aug 1 | Route 23 reducing-order sites through `check_never_sell_floor()` | `exit_logic.py` (2269L), `broker.py`, `entry_logic.py` | 4–5 | Steps 1–9 each file, Gro+GAI |
| Heal | Aug 3–5 | `sync_ledger` full-replay to backfill NVDA/GOOGL attribution pre-Phase-0 | `run_ledger_sync.py` | 1 | Verification run |
| RC-7 fix | ~Aug 3 | Add fractional qty support OR value-cap skip in `_submit_tranche()` for $1,000+ stocks | `quarterly_hold_manager.py` | 1 | Steps 1–9, Gro+GAI |
| **LLY entry** | **Aug 7** | QHM automated 3-tranche entry at 10:05 AM | Live, no manual action | — | P0-a complete |

**Buffer: Aug 3–7 (4 days)** for drift-freeze debugging, property-based FIFO tests, golden-master fill replay validation.

---

## Pending Approvals Summary

All queued files are BLOCKED. None can proceed without Rafael's in-person review.

| File | Queue Date | Gate Status | Blocker |
|------|-----------|-------------|---------|
| `strategy/run_cycle.py` (RC-3 desync) | 2026-07-01 | **BLOCKED** | Board 2-1 split; mypy + ruff + second-agent ALL FAIL |
| `execution/portfolio_tracker.py` (P&L drift) | 2026-07-01 | **BLOCKED** | Gro APPROVE / GAI REJECT split |
| `events/calendar.py` (Columbus Day semantics) | 2026-07-02 | **BLOCKED** | Gro REJECT / GAI REJECT |
| `strategy/run_cycle.py` (multiple EXIT events) | 2026-07-06 | **BLOCKED** | Gro REJECT / GAI REJECT |
| `scan_to_html.py` (NaN guard v1) | 2026-07-08 | **BLOCKED** | GAI REJECT (malformed diff); raw Groq also REJECT |
| `scan_to_html.py` (NaN guard v2) | 2026-07-09 | **BLOCKED** | Gro REJECT / GAI REJECT |

**Note on scan_to_html.py:** The handoff from 2026-07-08 flagged this as "not urgent — display-layer, fails-closed." The Jul 09 Gemini REJECT on the NaN guard cited a malformed diff (function signature repeated multiple times) and a new RC-2 introduced by the proposed fix. This needs a clean redraft, not an iteration on the rejected diff.

---

## Key Decisions for Rafael

**Q1: Minimum safety gate** — Approve in next in-person session before any quarterly hold entry. Low code footprint. Mandatory prerequisite.

**Q2: GS July 15 entry** — If Q2 beats $14.47 EPS:
- Option A: Enter manually July 15 at 10:05 AM (1 share ≈ $1,055 = 42% of account). GS only. No room for GEA + GS simultaneously.
- Option B: Skip GS. Preserve capital for GEA (July 25) + LLY (Aug 7). BoD notes GS is beta-dominant.
- Board recommendation: **Option B unless SpaceX beat is significantly above consensus.**

**Q3: LLY price target** — Board requires target revision to $1,350 (from $1,215). Approve at $1,350 target, $980 stop, August 7 entry. R:R = 0.77:1 (secular thesis compensates).

**Q4: P0-a priority** — Start immediately if LLY automated entry on Aug 7 is the goal. Cannot delay past July 14 without jeopardizing the window.

**Q5: RC-7 fractional fix** — The `max(int(raw_qty), 1)` guard at `quarterly_hold_manager.py:1518` causes GEV/LLY first tranche to be 1 whole share (43-48% of account). Approve a fix before GEV or LLY entry: either enable fractional quantities in `submit_limit_order`/`submit_gtc_stop_order`, or add a value-cap skip in `_submit_tranche()`. Board-vote-required (RTH execution).

---

## What Was and Was Not Done in This CCR Session

### Done
- Full read: `handoff.md`, `quarterly_holds_research_2026-06-04.md`, `quarterly_holds_research_2026-07-07.md`, `quarterly_holds_research_Q3_2026-06-20.md`, all 6 pending/queued files
- Web research: GS, GE, GEV, LLY current prices + earnings dates confirmed
- 4 cold board agents (Analytics, BoD, Technical, Execution) — all parallel, cold, independent
- This memo written to `logs/qhm_status_update_2026-07-11.md`

### NOT Done (requires Rafael + in-person session)
- Gro/GAI audit (no .env)
- Any code changes (no DS/GAI clearance)
- Minimum safety gate (needs Steps 1-9 + Gro+GAI)
- GS config entry (requires board vote on adding GS to config)
- P0-a wiring steps

*DS/GAI integration audit deferred to next in-person session as required by CLAUDE.md Rule C-3.*

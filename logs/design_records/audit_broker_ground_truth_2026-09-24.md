# Design Record — Audit broker ground truth (audit-alert false alarms, increment 1)

**Feature slug:** `audit_broker_ground_truth`
**Owner decision:** Rafael APPROVED Proposal 1 + Proposal 2 (2026-09-23 chat).
**BGG record:** `logs/tb_audit_log.md` → "2026-09-23 — Audit-alert false alarms: board (2 cold seats)
converged" and "Audit-alert false alarms: Gro + GAI on the fork (2 rounds) → ALIGNED".

## Problem (verified at source)
- `nightly_audit.py` (16:05 ET) and the meta audit (`auto_ai_audit.py --meta-audit`, 16:35 ET) infer
  "position naked" from LOG TEXT. The 2026-09-22 nightly card posted CATASTROPHIC "SOFI … naked": the
  sweep line "placed MISSING buy stop … broker-held 0" was read as "no stop". Alpaca order history:
  SOFI core-intraday entry 12:53 ET, software stop until the pre-close sweep placed a broker DAY stop at
  15:49 ET (by design: execution/entry_logic.py GTC-at-entry only >= 15:30 ET).
- `broker-held N` (execution/stop_protection.py:533-545) = placements the broker REFUSED because a
  protective order already held the qty (i.e. PROTECTED), not "stops held".

## Correction to the approved package (verified after approval, before any code)
- POSITION_COUNT_DRIFT is NOT a masked real fault: all 40 of the last 40 lines
  (`risk.open_positions=0 vs tracker=N`) fall in the same minute as an mtf-bot service start
  (journalctl) — the startup counter initialises at 0 and is corrected. Its `false_alarm` status is
  correct → NOT reclassified (reclassifying would ADD a daily false alarm).
- FIFO-orphan lines are not GOOGL-only (AMZN too; daily counts 1-102) and are currently NOT suppressed
  by any directive. Guard still added: `fifo orphan` joins `_NEVER_SUPPRESS_TOKENS` so it can never be
  muted (the approved "can't be muted in future"). Root cause = separate gated item 2.

## Feature Design Protocol answers
1. **Data source:** T1 Alpaca Paper Trading REST, read-only, via `reporting.pnl_ledger._get_json`
   (no SDK client; Execution Isolation respected): `/v2/calendar`, `/v2/positions`, `/v2/orders`
   (status=all, FULL history paged by `until`, legs counted toward the page limit, plus status=open now), `/v2/account/activities/FILL`
   (after=session open, page_token). Cycle liveness from `logs/mtf_bot.log` `[CYCLE] duration=` lines.
2. **Output:** a text block in the audit prompt + report file, and deterministic card findings.
   No new state file.
3. **Integration point:** new `reporting/broker_ground_truth.py`; `nightly_audit.py` calls
   `collect(session)` once, renders it into the prompt (rule: stop coverage is owned by code; on an UNKNOWN
   day a quoted bot self-report may still be reported "broker-unverified"), and ADDS critical/high alarms to
   the card (lows to the footer) via `card_alarms()` + `_escalate_card_verdict()` (can only RAISE the verdict).
   **FINAL DESIGN (r5+): no code path removes, hides, or lowers an LLM finding** — r1-r4 tried a prose-parsing
   downgrade and every fresh cold-2nd found a reachable input where it hid a real catastrophic claim. A
   detect-only counter (`naked_claims_on_cleared` → `logs/gt_compliance.jsonl`) measures whether the LLM still
   calls covered/by-design symbols naked; reversal criterion: >= 2 of the last 5 sessions → revisit.
   (midday + meta audit wiring = their own file sequences, same module.)
4. **Failure mode:** every read failure / truncated page / unparseable time / unreadable cycle log →
   status or class UNKNOWN; UNKNOWN never clears a naked claim (masked-loss invariant) and is shown on
   the card. The module never raises into the audit.
5. **Board vote:** done (observability + masked-loss seats) + Gro + GAI 2 rounds. Not risk-path (does
   not touch size/frequency/concurrency; runs post-close, not in the trade loop). FINAL design never
   downgrades an LLM finding (masked-loss seat reviewed every revision).

## Classes (code-computed, per symbol, RTH window from the calendar)
COVERED · SOFTWARE-ONLY-BY-DESIGN (core `IN-` owner only, loop running) · SOFTWARE-ONLY+CYCLE-GAP
(core, loop gap > 15 min) · NAKED (uncovered and not solely core-owned) · UNKNOWN.
Coverage requires resting stop qty >= position qty (partial coverage = uncovered). Tolerance 2 min.
Cycle-gap threshold 15 min DERIVED from data: 2,636 RTH cycle gaps 2026-07-19..09-23 → median 6.8,
p99 10.6, p99.9 14.2, max 43.5 min.

## Rule-C simulation (prototype on production data, OCI)
2026-09-22: SOFI uncovered 12:53-15:49 (core, by design); AAPL/INTC/MARA/PLTR carried core positions
uncovered 09:30-09:40 (morning window before the 09:35 DAY-stop placement — by design, loop running);
GEV/LLY (QHM) initially showed 390m uncovered because their GTC stops pre-date a 4-day lookback → fixed
by reading the FULL order history + status=open (a 45-day lookback still missed GE's 07-27 stop); AMZN day-tier OCO stop leg appeared uncovered because
`submitted_at` was used → fixed to `created_at` (OCO legs rest at the broker from creation).
Expected effect (FINAL design): the LLM is told stop coverage is owned by code, so the SOFI-type false claim should stop appearing (measured by the compliance counter); code never lowers an LLM finding; a genuinely
unprotected non-core position (e.g. a QHM hold whose GTC stop was cancelled) is forced onto the card
as NAKED even if the LLM misses it. Reversal criterion: any live NAKED/CYCLE-GAP card line that the
broker order history contradicts, or any real naked position the block classes COVERED/BY-DESIGN.
Size/frequency/concurrency delta: 0/0/0 (alerting only).

## A/B Gemini simulation (real 2026-09-22 inputs, run 2026-09-24)
Production prompt: false "SOFI naked" CATASTROPHIC in 1 of 1 completed runs (FAIL). New prompt: 0 of 2 completed
runs (WARN, "None — no catastrophic conditions"). Planned 5 runs per prompt did NOT complete (Gemini free-tier daily
quota 429 + 503 capacity) — partial evidence; the compliance counter measures it live from the first nightly run.

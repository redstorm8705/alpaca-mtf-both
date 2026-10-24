# Kill switch counts Day + Swing only, by exact shares — 2026-10-23 (signed: Claude)

Approval-ID: KILL-ATTR-1023
Covers: execution/risk_manager.py, tests/test_kill_switch_tier_attribution.py

## Owner direction (2026-10-23)
"It needs to exclude QHM, and F6. If we're -7% it should only be because of day and swing tier." On the co-held gap:
"that's a gap that needs to be corrected. It needs to parse out the calculation though so that it is excluding QHM vs.
Day or Swing in that -7% kill switch P/L." And the dashboard gauge must show the number, not "unavailable".

## Facts (code at origin/main 1e1c09a; read-only probe on OCI 2026-10-23 after the close)
- risk_manager.check_kill_switch measures intraday trading P&L with _intraday_trading_pnl:
  open leg (current - prior close) x active shares + every today fill repriced from prior close.
- With config.OWNERSHIP_GUARD_ENFORCE = False (live), "active shares" = 0 for ANY symbol QHM holds
  (`active_qty = 0.0 if sym in excluded else broker_qty`), so an OPEN Day/Swing lot in a QHM symbol is invisible to the
  open leg until it closes (its fills still count once closed). Example: QHM 2 GOOGL + Day short 10 GOOGL open, GOOGL
  up $5: the Day lot's -$50 open loss is not in the measure while it is open.
- With OWNERSHIP_GUARD_ENFORCE = True, the same function removes only the ledger's exact QHM + F6 share count
  (_protected_held_qty) and skips QHM/F6-tagged fills; a ledger/broker mismatch returns ok=False and
  check_kill_switch raises (entries blocked as a control fault, exits keep running).
- _publish_kill_snapshot sets attribution_exact only when the enforced path ran, so the dashboard (#741) shows
  "unavailable" every day under the live setting. Live snapshot: attribution_exact=false, trading_pnl=6.77.
- OWNERSHIP_GUARD_ENFORCE also turns on order-blocking ownership guards elsewhere (broker chokepoint, F6), so flipping
  it is a wider change than this fix.
- Probe (OCI, in-process flag patch, no writes): live ledger = AAPL Swing 1, MSFT Swing 1, GE QHM 3, NVDA QHM 2,
  GOOGL QHM 1, every row drift 0; exact path ok=True, trading P&L $6.77 = the legacy value (no co-held lot today).
- Swing never co-holds a QHM name (swing_breakout_manager isolation); the Day tier can (co-hold lifted 2026-10-11;
  GOOGL is in DAYTRADE_UNIVERSE and in QHM).

## Design
1. The kill measure uses the exact per-tier path whenever the ownership ledger proves it — independent of
   OWNERSHIP_GUARD_ENFORCE: _protected_held_qty no longer returns None just because the flag is off; it runs its existing
   checks (whole shares, complete claims, ledger net = broker net, no drift) and returns the exact QHM + F6 quantities.
2. _intraday_trading_pnl then removes only those exact shares from the open leg and skips fills tagged qhm/forever6
   (existing enforced-path code, unchanged).
3. If the ledger cannot prove it (unreadable, drifted, broker mismatch): with the flag off, fall back to today's legacy
   measure (whole-symbol QHM exclusion) and log `change=kill-attr-1023 exact=false reason=...`; with the flag on, the
   existing raise (control fault) is unchanged. The fallback is never worse than today.
4. _publish_kill_snapshot: attribution_exact = the exact path succeeded (not the flag), so the dashboard shows the
   Day + Swing number whenever the measure is exact, and "unavailable" only when it fell back.
5. Kill threshold (7%), latch, SOD baseline, alerting and the equity fallback are unchanged.
No new number.

## Replay
Today (10/23): exact = legacy = +$6.77 (no co-held lot). The difference appears only while a Day lot is open in a
QHM-held symbol; Day trades in QHM symbols so far: GOOGL 9/23 and 9/24 and NVDA 9/18 (closed same day — their fills
count either way; their intraday open legs were excluded under the legacy measure while open).

## Required questions (RQ-4)
Q1: Data collection unaffected; makes the only daily brake measure exactly what the owner defined.
Q2: Trade count unchanged except a kill trips when Day/Swing truly lose 7% (could trip earlier than legacy on a
    co-held day; never later).
Q3: Reuses the existing exact path; no new mechanism.
Q4: Existing #741 snapshot/gauge, ownership ledger (ownership_ledger_design_2026-07-10.md), KillVerdict causes.
Q5: No new number.
Q6: Exposes the flag coupling (one switch for order-blocking and for measurement); forward pass: per-tier kill
    reporting on the dashboard (Day vs Swing split).
Q7: Feeds the dashboard gauge and the post-market card; fails to today's legacy measure.
Q8: Probe output above; code lines quoted from origin/main.
Q9: Replay today identical; expected effect only on co-held days; wrong if a kill trips on a day Day+Swing were not
    down 7% (check: trading_pnl in the kill state vs Alpaca fills).
Q10: N/A.
Q11: Kill switch = safety envelope; can only make the kill MORE accurate; masked-loss seat reviews.
Q12: Touches the 7% kill measurement only; never widens it (fallback = today).
Q13: Ledger unreadable/drifted -> today's legacy measure, logged; gauge "unavailable".
Q14: Not a shadow.
Q15: Kill state and snapshot carry trading_pnl, attribution_exact and the fallback reason.
Q16: `change=kill-attr-1023` log line each cycle when exact; post-live replay on the first session.
Q17: Read-only OCI probe (this record) and the first live snapshot showing attribution_exact=true.
Q18: The always-"unavailable" gauge is treated as a defect, not "fails safe so fine".
Q19: Risk path: owner approves the exact design.
Q20: Prompts neutral; board + Gro + GAI.
Q21: Tests mock positions/fills/ledger; no broker call.
Q22: Read-only OCI probe done.
Q23: Unit tests + OCI probe before ship.
Q24: N/A. Q25: N/A.
Q26: T1 Alpaca positions + fills; ownership ledger (Alpaca-fill replay); fallback legacy.
Q27: Existing logs/kill_switch_snapshot.json atomic write (unchanged).
Q28: check_kill_switch on run_cycle and run_day_tier; no new I/O (ledger load already used by the enforced path).
Q29: P0 owner item (safety measure accuracy).
Q30: A desk measures loss limits per book; this does it per tier.
Q31-Q37: N/A (measurement correctness, not an edge).
Q38: N/A.
Q39: N/A.
Q40: Outcome: kill measure = Day + Swing P&L exactly; check: daily compare of trading_pnl vs Alpaca fills split by
     ledger tier (weekly report).
Q41: Split the dashboard gauge into Day and Swing lines.
Q42: Alpaca positions, fills and the fill-replayed ledger.
Q43: Per-book P&L attribution by exact quantity.
Q44: First live snapshot + post-live replay.

## Review (2026-10-23) — board Thorp + Taleb seats, Gro, GAI: aligned after 4 rounds
Round 1: Thorp APPROVE-WITH-CHANGES (T1-T3); Taleb REJECT without L1 (constructed masked-loss input: an untagged
stop leg sold QHM shares and the ledger replay booked QHM's gain to Day); Gro REJECT and GAI APPROVE-WITH-CHANGES, both
against the legacy fallback. Round 2 counter-evidence: ledger drift occurred once in 12,000+ checks over 10/13-10/23; an
L1 day (owner manual QHM buy, 10/21 and 10/23) would block a whole session under "block entries". Round 3: the board
replaced the per-symbol rule with "the more negative of two numbers". Round 4: Gro's remaining input (a loss from the
prior day) was refuted from the code (the kill measures today only); Gro confirmed no hidden loss.

## FINAL design (supersedes items 1-4 above)
F1. Exact per-tier path whenever the ledger proves it, independent of OWNERSHIP_GUARD_ENFORCE, with added validation
    L1: a symbol with protected qty > 0 is UNPROVEN if any today fill on it has no tier tag, or any tier claim on its
    row is negative. T1: the kill state's attribution_exact = the exact path succeeded. T2: the RTH equity fallback
    (_buy_and_hold_intraday_pl) also removes exact quantities when they are proven.
F2. Per UNPROVEN symbol (ledger mismatch, drift, L1):
    a. compute state-based (active = broker qty - QHM state qty from get_quarterly_hold_quantities(); QH/F6-tagged
       fills skipped) and count-all (every share, every fill); use the MORE NEGATIVE;
    b. exclude the symbol only if broker qty == QHM state qty AND no Day (DT) or Swing (IN) tagged fill today on it;
    c. QHM state unreadable or missing the symbol -> count-all;
    d. an unproven F6 name -> count-all (F6 keeps no own quantity record).
F3. Whole-ledger read failure -> the existing raise -> entries-only block for that tick (both callers already do this;
    exits continue; nothing latches).
F4. Log each cycle: exact=true/false, each unproven symbol with both numbers and the one used (`change=kill-attr-1023`,
    rate-limited).
Dashboard gauge (#741) shows the Day + Swing number whenever F1 succeeds; "unavailable" only on F2/F3.
Tests: Taleb's GOOGL input (true -$90 counted), the lagging-QHM-state input, GE 10/23 (excluded, entries continue),
negative claim, untagged fill, unreadable state, F6 unproven, ledger unreadable (raise).

# Day-tier halt audit and fix — 2026-10-19 (Claude)

Approval-ID: HALT-1019
Covers: execution/day_trade_manager.py, strategy/day_tier_logger.py, tests/test_day_halts.py

## Why (owner, 2026-10-19)
"There seem to be a ton of things that halt the day tier and that makes me uncomfortable. Audit all things that cause
a halt for the day tier and debug to ensure any halts are legit."

## Audit (code read in full this session; server logs and state)
Per-tick blocks (re-checked every 2 minutes, never latched) in run_day_tier.py: account kill / trading-blocked,
account fields invalid, account-kill read error. History: 9/16 185 ticks account_halted (the false kill-switch trip
fixed in PR #327); 10/12 170 ticks account_invalid (Alpaca reported buying power $0.00 from 10:00 AM to 3:38 PM ET; no
order could have filled — the cause of the $0 buying power is a separate open question).
Day-long latched halts in execution/day_trade_manager.py — none has ever fired (no _tier_halted_date /
_tier_killed_date / _unresolved_exit marker in the never-pruned state file; no log line):
- K1 tier kill (realized + open <= -5% of start-of-day equity): legitimate, owner rule.
- K2 start-of-day equity unreadable -> halt for the day: legitimate to block, wrong to latch.
- K3 realized-loss journal "unreadable": any malformed line in the whole journal history -> halt every day. False brake.
- K4 an open lot's P&L unreadable -> halt for the day: legitimate to block, wrong to latch.
- K5 _halt_unresolved_exit (about 20 call sites: an exit fill not recorded/readable, or a Day lot gone with no
  recorded exit, e.g. closed by hand) -> halt for the day, even after the exit is booked a tick later.
- Missing (fail-open): tier_kill_check's outer `except Exception: return False` — e.g. a 429 on the positions read
  lets entries proceed with the open-lot loss never measured.

## Design (Gro, GAI, Taleb seat; 2 rounds; aligned with the changes below)
1. Unresolved exits (K5): a marker per trade (_unresolved_exit::<symbol>::<trade_id>). It clears ONLY when that trade's
   exit is booked in the journal (exit_fill, or partials covering the lot) — never on "retired". While open: no new Day
   entry on that symbol, and the lot is charged at its worst case (its full entry notional) in the Day budget and the
   tier kill until booked — so a large unknown loss blocks everything, a small one only what it must.
2. Old torn journal line (K3): a malformed line blocks only if it could be from today (no valid record dated before
   today, ET, follows it); an older one is reported once and ignored for today's realized loss only. The ownership
   reader keeps its strict check.
3. Day lot closed outside a recorded order (K5): book the exit from Alpaca FILL activities (fully paged) only when the
   symbol is flat, the Day exit legs are confirmed cancelled with no live Day stop, and opposite-side fills after the
   entry — excluding fills whose order is already journaled and fills tagged to another tier — cover the owned qty; if
   more fills than needed, the worst-priced ones are attributed. Logged with fill ids. Otherwise item 1 applies.
4. K2 and K4 block only while unreadable (re-checked every tick), no day latch.
5. tier_kill_check errors block that tick (no latch) instead of letting entries through.
K1 unchanged. Not adopted: a 30-minute forced resolution (masks a loss); a shadow/observation period (no shadows);
moving the journal aside on corruption (it is the open-trade source; would orphan live lots); a 60-minute auto-flatten.

## Required questions
Q1: More trading days (no false day-long halts) with losses still counted: data collection up.
Q2: Trade count goes up (fewer false halts); one blocked tick on a broker read error (item 5).
Q3: Yes; the simpler alternative (remove halts) would mask losses.
Q4: Builds on the existing halt code in day_trade_manager.py; no duplicate; CONV-1018 (#709/#712) interacts via exits.
Q5: No new number; the worst-case charge is the lot's own notional; dates from the journal.
Q6: Static assumption "any doubt = whole day off"; forward: per-trade resolution state.
Q7: Feeds the Day budget and tier kill (worst-case charge); fails safe (unknown = blocked/charged).
Q8: Audit facts from the code and the server state/logs gathered this session.
Q9: Replayed against history: none of K1-K5 fired; 9/16 and 10/12 blocks were per-tick and legitimate/fixed.
Q10: No.
Q11: Entry gating (frequency) — risk-path: board masked-loss seat reviewed.
Q12: Stops untouched; kill strengthened (item 5); never-mask kept (items 1, 3).
Q13: Every new read fails closed for that tick.
Q14: No shadow.
Q15: Each block/clear/booking logged with trade id, reason, fill ids.
Q16: Log lines change=halt-1019-*; audited after the first session.
Q17: Read-only run of item 3 on the server against real fills before live.
Q18: The 10/12 $0 buying power is flagged as a separate anomaly, not explained away.
Q19: Owner approves this exact design (HALT-1019).
Q20: Prompts carried facts; false reviewer claims (fabricated citations, a 48-hour shadow) discarded.
Q21: Broker reads mocked in tests.
Q22: Item 3 run read-only on the server first.
Q23: Full-path tests of each halt/clear path before ready.
Q24: N/A.
Q25: N/A.
Q26: T1 Alpaca fills and positions; unreadable -> blocked.
Q27: State file (atomic, existing) and the journal (append + fsync, existing).
Q28: run_day_tier tick: reconcile, then tier_kill_check, then entries.
Q29: Owner-requested; protects trading days.
Q30: Blind spot found: the fail-open kill check (item 5).
Q31: N/A (no edge claim).
Q32: N/A.
Q33: External-close bookings use real fill prices.
Q34: N/A.
Q35: Gap-through loss handled by the worst-case notional charge.
Q36: N/A.
Q37: N/A.
Q38: N/A.
Q39: N/A.
Q40: Outcome: no Day entry while a loss is unknown, and no day lost to a resolved one; check: weekly count of halts with
     their cause and clear time vs booked exits.
Q41: Expert: per-trade resolution state with worst-case reservation — adopted.
Q42: Broker fills and positions.
Q43: Professional method: reconcile to broker fills, reserve unknown exposure at worst case.
Q44: Independent: the weekly halt/clear count against Alpaca fills.

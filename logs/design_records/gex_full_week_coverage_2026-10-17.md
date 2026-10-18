# GEX full-week coverage — never blind on Thursday or Friday (2026-10-17, Claude)

Approval-ID: GEX-1017
Covers: data/gex.py, execution/kelly.py, tests/test_gex_full_week.py

## Owner request (2026-10-17)
"We need full GEX readings every trading day extending into the upcoming week. I don't want the bot skipping trades on
Friday because there isn't enough data available. There should always be more than enough data available."

## Problem (measured, logs/day_tier_shadow.jsonl + logs/live_writer.log, RTH 6:30-13:00 PT only)
- Day-tier GEX readings labelled UNKNOWN during market hours: Mon 10/12 1 of 143, Wed 10/14 17, Thu 10/15 32, Fri 10/16
  77 of 143 (54%), including AAPL/GOOGL/MSFT/AMZN (9-10 each).
- Cause (live_writer.log): the signal window is [today, this Friday] Mon-Thu, and on a MONTHLY OpEx Friday (10/16 was
  the 3rd Friday) it collapses to that day's expiring options. As that expiry approaches, the free indicative feed
  shows zero bids / no IV: AAPL valid strikes 104/236 Tue 10/13 -> 49/154 Thu -> 15-35/156 Fri, so the quality gate
  (data/gex.py:624-628) returns UNKNOWN and the Day tier stands down (day_tier_gex_action STAND_DOWN).
- The weekly (non-OpEx) Friday already skips 0DTE (data/gex.py:293-295, board 2026-09-08); the monthly-OpEx Friday and
  Thursday thinning were left as-is.
- Correction of an earlier claim: the "45% STALE" figure counted readings after the close; STALE during RTH is only
  the first reading at the open.

## Front-loaded replay (2026-10-17, /tmp/gex_replay.py on OCI, the bot's own _compute_gex)
Rebuilt readings at 10:00, 12:00 and 15:00 ET on Thu 10/15 and Fri 10/16 from Alpaca 15-min option bars (bar close as
the quote mid), using the following week's expiries (2026-10-19 .. 10-23):
- every one of 24 readings (SPY, AAPL, NVDA, MSFT x 6 times) passed the quality gate: valid strikes 92-721, windowed
  capture 0.50-0.96 (gate 0.40), atm 6-155 (gate 3);
- labels: SPY NEGATIVE all six; AAPL POSITIVE; MSFT POSITIVE/NEAR-FLIP; NVDA POSITIVE/NEAR-FLIP.
Limit: Alpaca no longer lists the expired 10/16 contracts, so the CURRENT window could not be rebuilt (it shows 0/0);
the live logs above are the "before". Next-week chains on Fri 10/16 (probe): AAPL 125 strikes with OI, 98 traded,
208,364 contracts; NVDA 123/95/576,819; MSFT 161/136/83,414.

## The change (revised after board round 1 — options seat, Thorp, Taleb masked-loss; all APPROVE-WITH-CHANGES)
1. WINDOW ONLY EVER WIDENS (F2): end = the later of the current end (the coming Friday, today if Friday) and the
   second listed expiry after today. Mon-Wed is unchanged; Thursday and Friday extend into the following week. The
   separate monthly-OpEx stand-down and the weekly-Friday calendar skip are replaced by rule 2. Expiries come from the
   contracts listing (verified 2026-10-17: SPY lists every weekday; AAPL/NVDA/MU Mon-Wed-Fri; EWY Fridays only).
2. PER-EXPIRY ADMISSION (F1): each expiry is gated on its own with the EXISTING gate (atm, windowed capture, windowed
   count; same constants). Only admitted expiries enter the label sum (plain OI-gamma sum). None admitted -> UNKNOWN.
   A thin or dying same-day strip drops out on data, a liquid one counts — no calendar rule, no new weight.
3. Contracts under the existing 1-hour floor (_MIN_T_YEARS) are removed from the OI map before any gate, so a dead
   strip never dilutes or inflates capture.
4. TRUNCATION (F3): if the page cap is hit, the last (possibly partial) expiry in the fetch is dropped — no label from a
   half-fetched expiry.
5. Pin/levels use the nearest ADMITTED expiry; `dte` follows it. Pin confidence is measured on that expiry (as today).
6. PROVENANCE (R4): the snapshot records `expiries_used` with each expiry's pass/fail and counts; kelly's INFO line
   adds the label's confirmed_ts and expiries.
2b. POOL FALLBACK (Gro round 2): if no expiry is admitted on its own, pool all NON-same-day expiries in the window and
   apply the existing gate; pass -> label from that pool; fail -> UNKNOWN with an explicit reason
   ("no_contracts_outside_same_day" / "no_expiry_admitted_and_pool_failed").
   Build review (cold 2nd, FAIL -> fixed): a weekly-only chain on Friday must reach the next listed expiry (window end
   = the one expiry after today when only one is listed); a page-capped first fetch never extends and ends at its last
   complete expiry; the window fetch is wrapped so one symbol's error cannot abort the refresh. Coverage is never narrower than today except where today's label passed
   only because of censored same-day contracts.
7. Unchanged: gate constants, STALE windows, spot guard, carry-forward, every consumer's UNKNOWN fail-safe.
8. Build requirements (review): explicit early return for zero admitted expiries (no reliance on the per-symbol
   try/except); UNKNOWN always carries a reason; unit tests for zero-admitted -> pool, pool-fail -> UNKNOWN, empty set
   -> no exception, page-cap truncation -> partial expiry dropped, Mon-Wed window byte-identical to today; snapshot
   stays atomic (data/gex.py:979-982).

## Review record (2026-10-17)
- Round 1: options-microstructure seat, Thorp seat, Taleb masked-loss seat — all APPROVE-WITH-CHANGES (per-expiry
  admission; window only widens; drop truncated expiry; drop <1h contracts; provenance; pre-ship SPY comparison and RTH
  replay). Gro APPROVE-WITH-CHANGES, GAI APPROVE-WITH-CHANGES (their halt / normalisation / kelly-decay / 1.5% items
  rejected by the board with evidence above).
- Round 2: GAI APPROVE-WITH-CHANGES; Gro REJECT (per-expiry admission could reduce coverage on thin chains) -> rule 2b.
- Round 3: GAI APPROVE-WITH-CHANGES (log UNKNOWN reasons). Gro REJECT citing non-existent code (day_tier_gex_action.py
  147-152) -> counter-prompt with evidence + build requirement 8.
- Round 4: Gro APPROVE-WITH-CHANGES (explicit empty guard, UNKNOWN reason, atomic snapshot — all in 2b/8; "make the
  1-hour floor and page cap rolling" logged as a forward item: both are existing mechanics, unchanged here).
Alignment: board 3/3, Gro, GAI — APPROVE-WITH-CHANGES, all changes incorporated.
Rejected (board, with reasons): halting all entries on gate failure (contradicts the paper frame; UNKNOWN already
fails safe); a fixed 1.5% spot-move blanking rule (static; spot guard + 15-min recompute exist); per-expiry OI
normalisation (gives a thin strip the vote of a deep one); a kelly confidence-decay (separate file and behaviour;
forward pass is outcome-conditioned per-label Kelly from fills).

## Sizing and trade-count effect (risk path, Rule E)
kelly.py:361-410 reads the SPY label Mon-Thu (Fridays carved out). Live upsize is only NEGATIVE -> x1.30 on momentum
trades (POSITIVE x1.15 applies to mean reversion, MR_ENABLED=False). The multiplier is applied before the 4.5% risk
clamp (kelly.py:410) and the gross/overnight caps; uplift is at most min(1.30, 0.045/pre-risk) — e.g. 2% pre-risk ->
2.6%, about +$320 notional per trade at $2.5K equity. With rule 1, Mon-Wed windows are unchanged, so the SPY label
change is mainly Thursday (UNKNOWN -> a label) plus any Mon-Wed effect of per-expiry admission. Layer 8 adds +1
minimum score on NEGATIVE days (run_cycle.py:1683-1684): main-book trades get pickier and larger on those days while
Day-tier trades increase. Before ship: one read-only OCI comparison of old vs new SPY labels at several times on
Monday 10/19 (label agreement rate, how often NEGATIVE), and an RTH replay with today's expiry included (R5).

## Required questions
Q1: Yes — the Day tier stood down on 54% of Friday readings; full coverage restores trades on Thursdays and Fridays, which collects the data the paper account exists to collect.
Q2: Day-tier trades rise (fewer GEX stand-downs). Main-book trades can fall on days SPY newly reads NEGATIVE, because Layer 8 raises the minimum score by 1 (run_cycle.py:1683-1684) while those trades get the x1.30 size; this is disclosed to the owner for approval.
Q3: It should exist (owner request); the simpler alternative (buy the $99 OPRA feed) is a separate owner decision and not needed for coverage per the replay.
Q4: Builds on the 2026-09-08 skip-0DTE Friday window (data/gex.py:271-297) and the 2026-07-06 this-week narrowing; checked origin/main — no parallel GEX window change; C2 registry has no GEX-window item.
Q5: The old window was a fixed calendar rule; the new one is derived from listed expiries and widens on measured data quality, with no new fixed numbers.
Q6: Static assumption exposed: "this week's chain is always liquid enough". Forward pass: a per-expiry liquidity score feeding which expiries the pin uses, and the optional OPRA feed if gaps remain.
Q7: Connected paths: Day-tier act/stand-down, Day-tier entry walls, kelly.py SPY multiplier, run_cycle Layer 8, options scanner. Each keeps its UNKNOWN/STALE fail-safe; the kelly path is called out as risk-path.
Q8: Measured: live UNKNOWN counts per day from day_tier_shadow.jsonl and valid-strike counts from live_writer.log, cited above.
Q9: Replay over Thu/Fri 10/15-16 at 3 times x 4 symbols, all passing the gate; expected effect: Thu/Fri UNKNOWN rate falls from 22-54% to under 5%; wrong if next Thursday/Friday UNKNOWN stays above 10% in RTH.
Q10: No calibration perfectionism blocks it; the existing gate thresholds are reused unchanged.
Q11: Yes — the SPY label feeds kelly.py's x1.15/x1.30 multiplier Mon-Thu, so size can rise on Thursdays; routed to the board with the masked-loss seat.
Q12: Stops, the 7% kill switch, never-mask-a-loss, paper=True and the T1 data tier are untouched.
Q13: Missing or thin data still fails safe: if no expiry passes the gate within the page budget, the label is UNKNOWN exactly as today, and consumers stand down/neutral.
Q14: No shadow: the change ships live after review and approval; nothing is computed without being used.
Q15: Each snapshot already logs label, valid/total, skips, pin expiry; the new field `expiries_used` records which expiries were read.
Q16: Proof it ran: live_writer.log lines showing exp= the next weekly on Thu/Fri and valid counts above 40; first-session audit compares Thu/Fri UNKNOWN rate with last week.
Q17: Verified in its real process: the live_data_writer on OCI writes gex_snapshot.json; the post-live check reads that file during RTH.
Q18: The Friday blackout was previously explained as "monthly OpEx stand-down by design"; it is re-examined here against the actual cost (54% stand-downs).
Q19: It changes when the Day tier trades and main-book size via kelly; owner approval of this exact design is required before commit.
Q20: Review prompts carry facts only; the logs and code were read in full (data/gex.py 1129 lines); the design goes to the board, Gro and GAI before build.
Q21: Tests mock all HTTP calls (no broker or data API is reached); the window logic is a pure date function tested directly.
Q22: Before calling it live, run refresh_gex read-only on OCI against the real chains on Monday pre-open and compare labels with the replay.
Q23: The replay above is the full-path simulation of the compute; a pre-market run on OCI is the second check before it is called ready.
Q24: No absence claims; the "expired contracts are not listed" limit was observed directly in the replay output.
Q25: All statistics above are measured from logs and Alpaca data, not quoted from memory.
Q26: Data source: T1 Alpaca options contracts (/v2/options/contracts) and snapshots (indicative feed); no fallback source is added; failure falls back to UNKNOWN.
Q27: Output: logs/gex_snapshot.json (atomic tmp->replace, unchanged) and logs/gex_history.jsonl (append).
Q28: Integration: data.gex.refresh_gex on the live_data_writer thread (not the run_cycle trading thread); readers unchanged.
Q29: Value rank: top — it directly restores Day-tier trades two days a week, the largest measured cause of missed trades.
Q30: Whitespace: standard GEX desks aggregate across expiries weighted by open interest; the bot used one week only. Blind spot: an expiry-day quality collapse looked like "no edge" rather than "no data".
Q31: Not a fitted model; the label logic is unchanged, only the input window. Out-of-sample check is next week's live Thu/Fri.
Q32: Replay used bars at or before each checkpoint time (no look-ahead); expired-contract survivorship limit is stated above.
Q33: Costs unchanged: more contracts per refresh mean more API calls within the existing page budget; no trading cost change.
Q34: Not a return stream; it is a regime input. Its effect on returns is through Day-tier entries and the kelly multiplier.
Q35: Regimes: replay covered a NEGATIVE-SPY two days; monthly OpEx Friday is specifically included; tails handled by UNKNOWN fail-safe.
Q36: Capacity: no change to position limits; liquidity of the options used is higher (next weekly) than today's expiring strip.
Q37: Coverage, not edge, is what changes; the sample is 24 replay readings plus every live reading from next week onward.
Q38: Decay: if next week's Thu/Fri UNKNOWN rate does not fall, the design is wrong and comes back to the owner.
Q39: Sizing stays tied to the existing Kelly path; the only sizing effect is the existing SPY multiplier firing more often, which the board reviews.

## Prior work
- handoff.md: the Day tier's GEX stand-down and the 2026-10-17 weekly audit that surfaced it.
- growth_engine_regime_2026-09-19.md: Day-tier bracket exits use the GEX pin/walls (FADE TP = pin, RIDE TP = next wall) and its Q2 asked for a GEX-coverage check; full-week coverage keeps those targets available Thu/Fri. No overlap with its sizing regime.
- day_tier_v2_design_2026-08-29.md: the Day-tier GEX Layer B design this window feeds.
- co_audit_and_anomaly_2026-10-09.md: anomaly rule — the Friday blackout is investigated, not explained away.
- c2_day_tier_methodology_2026-10-04.md and c2_evidence_2026-10-05.md: Day-tier evidence methodology; no GEX-window item.

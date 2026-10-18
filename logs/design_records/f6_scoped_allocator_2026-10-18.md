# F6 scoped capital allocator — 2026-10-18

**Author:** OpenAI Codex
**Status:** exact design awaiting Rafael approval; no commit; F6 remains disabled
Approval-ID: F6-ALLOC-1018
Covers: config.py, execution/forever_hold_manager.py, execution/overnight_capacity.py,
  execution/tier_capital_allocator.py, tests/test_f6_bounded_entry.py,
  tests/test_overnight_capacity.py, tests/tests_tier_capital_allocator.py,
  logs/design_records/f6_scoped_allocator_2026-10-18.md, handoff.md,
  logs/tb_audit_log.md

## Prior work

- `logs/design_records/f6_golive_2026-09-26.md` defines F6's cash-only accumulation lifecycle, shared-symbol ownership, and launch prerequisites.
- `logs/design_records/allocator_reenable_2026-10-02.md` documents why the shared allocator remains off for Day, Swing, and QHM: stale ownership can block active-tier entries.
- `logs/design_records/f6_pending_buy_reservation_2026-10-12.md` supplies the shipped exact-ID protected-buy reservation that this capital lease runs beside.
- `logs/design_records/f6_instrument_and_kill_policy_2026-10-15.md` requires the dynamic trigger, excludes F6 from a genuine 7% trading-loss brake, and identifies allocator/emergency integration as blocking.
- `logs/design_records/f6_bounded_entry_2026-10-17.md` supplies the bounded extended-hours order boundary this allocator admits.
- `logs/design_records/f6_trim_resync_2026-10-17.md` shipped the preceding queue item and names allocator/emergency-flatten integration as next.
- `logs/design_records/dynamic_capacity_2026-10-14.md` defines the data-derived, tighten-only total overnight K reused here.
- `logs/design_records/c2_swing_breakout_tier_2026-09-29.md` is the original shared allocator consumer and proves why this diff must not change active Swing admission.
- `logs/design_records/co_audit_and_anomaly_2026-10-09.md` requires cross-tier connections to preserve explicit isolation and fail-safe boundaries.
- `handoff.md` declares allocator/emergency-flatten integration as the next F6 prerequisite after trim resync.
- Current parallel-work check: `origin/main` is `7e8ef69` after Claude's GEX PR #693; its five files do not overlap this diff. Open PR #694 changes only `handoff.md`, so the ship step must rebase after it and preserve its pick-up block before adding this result.

## Measured problem

On `origin/main`, `config.TIER_CAPITAL_ALLOCATOR_ENABLED=False`, while the F6 money boundary calls
`live_admit`; the disabled bridge returns an approval without a lease. This is a source measurement,
not a live-loss claim: F6 is still off at `config.py:420`, so it has produced no live F6 orders. The
front-loaded empirical-trigger replay already recorded that the existing event rules alone could
spend cash down toward the $200 reserve (`logs/design_records/f6_instrument_and_kill_policy_2026-10-15.md`).

## Exact design

1. Arm the existing allocator for F6 only. The global switch stays false, so Day, Swing, and QHM
   continue returning through their existing disabled path and cannot repeat September's all-entry lockout.
2. Require every F6 request to declare overnight exposure at the allocator's request validator, not
   only at the current caller.
3. Use the existing regime-selected F6 envelope: 10% of equity outside a verified crash and 30% in a
   verified crash. These existing bounds are paired with two independent dynamic checks: current-session
   canonical regime evidence and the current-session data-derived total overnight K.
4. F6 alone uses a strict current-session K reader. Missing, corrupt, stale, nonfinite, nonpositive, or
   above-ceiling K refuses the buy. Existing Swing/QHM callers retain their already-approved last-good fallback.
5. Retain the independent cash-only checks: exact limit-price reserve, settled-cash floor, F6-owned
   notional, unresolved leases, pending order gross, maintenance cushion, and canonical zero-drift ownership.
6. Refuse `FOREVER6_ENABLED=True` during config validation if the F6-scoped allocator is off.
7. Keep `FOREVER6_ENABLED=False`. This is one launch prerequisite, not the F6 enablement change.

## Plain-English example

At $2,500 equity, the existing normal F6 envelope is $250. If F6 already owns two $100 shares of
NVDA, another $100 share is refused because it would raise F6 to $300. In a verified crash, the
existing envelope expands to $750, but the buy still needs settled cash, today's dynamic total-book
capacity, current ownership, and a durable lease. No Day, Swing, or QHM order is routed through this
scoped switch.

## Reversal criterion

The component is wrong if an F6 request can be accepted without a durable lease, without
`overnight=True`, with prior-session regime/K evidence, above the selected F6 envelope, or if a Day,
Swing, or QHM request starts using the allocator while the global switch is false. Any one is an
immediate disable-and-repair defect; it is not a condition to observe in shadow mode.

## Required questions

Q1. Yes—this removes an unbounded F6 launch defect so future F6 fills can produce valid paper-account evidence; the measured source path is `config.py:1015` plus `execution/forever_hold_manager.py:1008-1014`, and no return uplift is claimed.
Q2. It changes no current trade count because `FOREVER6_ENABLED=False` (`config.py:420`); after separate F6 enablement it may refuse an F6 buy that exceeds verified capacity, which is why this exact sizing design requires Rafael's approval.
Q3. Yes, it should exist: the simpler alternative—turning on the shared allocator—would expose Day/Swing/QHM to the documented stale-ledger lockout; the scoped bridge at `execution/tier_capital_allocator.py:944-953` gets to yes without that regression.
Q4. Prior records above cover the trigger, reservation, bounded order, dynamic K, and trim lifecycle; `git diff HEAD..origin/main` measured no code overlap with PR #693, and open PR #694 touches only `handoff.md`, which must be preserved on final rebase.
Q5. No new numeric trading threshold is introduced; the existing 10%/30% envelopes are regime-selected and paired with current-session regime evidence plus data-derived K (`execution/tier_capital_allocator.py:397-415,497-520`).
Q6. The exposed static assumption is that a disabled shared allocator is acceptable for a future F6 launch; the forward pass is exact emergency-flatten attribution, followed by ETF wind-down detection and the full-path simulation listed in `handoff.md`.
Q7. It connects F6 entry, canonical regime state, dynamic overnight capacity, broker snapshot, ownership ledger, and durable capital state; each connection refuses unknown input and is isolated in `TierCapitalAllocator.admit_entry` (`execution/tier_capital_allocator.py:177-221`).
Q8. The cause is source-verified, not inferred from P&L: global allocation is off while F6 calls the bridge (`config.py:1015`, `execution/forever_hold_manager.py:1008-1014`); F6 is off, so performance impact is explicitly not claimed.
Q9. Front-loaded deterministic cases cover normal and crash envelopes, stale regime, missing/stale/corrupt/NaN/Inf/nonpositive/above-ceiling K, unresolved leases, and active-tier isolation; measured result is 113 tests plus 11 subtests passed, and any reversal-criterion breach above disproves it.
Q10. No—F6 is held back by declared execution prerequisites, not calibration perfectionism (`handoff.md` F6 queue); this component removes one prerequisite without enabling the tier.
Q11. Yes—future F6 position size/exposure changes, so this is risk-path; the exact diff received a Thorp/Taleb board approval and masked-loss adversarial PASS after closing stale-evidence and NaN-cap bypasses (review measurement, 2026-10-18).
Q12. It does not change stops, the 7% daily brake, never-mask-a-loss, `paper=True`, or data-source permissions; F6 remains cash-only and off (`config.py:420,436-437`).
Q13. Missing/stale/wrong regime, K, broker, ledger, cash, order, or allocator state refuses only the F6 buy; strict K validation is `execution/overnight_capacity.py:98-121` and admission failure is `execution/tier_capital_allocator.py:497-520`.
Q14. No part is shadow: after the separately approved F6 enablement, the allocator directly admits or refuses each buy; today the entire tier is disabled pending prerequisites, not computed log-only (`config.py:420`).
Q15. Yes—the existing allocator audit records timestamp, full request, verdict, reason, lease, and broker snapshot hash with flush/fsync (`execution/tier_capital_allocator.py:887-910`).
Q16. `logs/tier_capital_allocator.jsonl` will contain the F6 request, approval/reason, lease, and snapshot hash; first enabled-session audit must join that row to the exact `F6-` Alpaca order and canonical zero-drift replay, while this dormant ship is verified by config readback and a read-only admission probe.
Q17. Runtime proof is a read-only OCI probe against the real account/ledger followed by the declared full-path pre-market simulation; deployment alone will not be called ready (`handoff.md` queue and Q23 below).
Q18. No anomaly is dismissed as by design: the prior disabled-auto-approval is treated as a launch defect even though it never produced a live F6 order (`logs/design_records/f6_instrument_and_kill_policy_2026-10-15.md`).
Q19. Yes—this changes F6 sizing/admission; no commit is permitted until Rafael approves exact design `F6-ALLOC-1018`.
Q20. The board and adversarial prompts requested rejection/findings rather than a preferred verdict; relevant current source, handoff, audit tail, prior records, `origin/main`, and open PRs were read, while no broad performance/log-corpus claim is made.
Q21. Tests set `MTF_TEST_MODE=1`, mock broker state/order boundaries, and the repository broker refuses real clients under test; focused execution completed without a network order (`tests/test_f6_bounded_entry.py:13-31`).
Q22. Not yet called live—F6 is off; before the later enablement package, the new strict control must pass a read-only OCI probe against the real broker, ownership ledger, current regime record, and current K, as required in Q17.
Q23. Component simulation passes (113 tests plus 11 subtests), but the F6 full-path pre-market simulation has not yet run; therefore this design does not call F6 ready and does not flip the flag.
Q24. N/A—this change makes no claim that a symbol, asset, or ETF does not exist or is untradable; instrument classification remains in `execution/instrument_policy.py`.
Q25. N/A—this change quotes no stock volatility, typical move, return, or market-behavior statistic; its dollar example is arithmetic from $2,500 × existing allocation bounds.
Q26. T1 Alpaca supplies account/positions/orders; canonical local regime state and the upstream K record are inputs, with K's upstream VIX exception already governed by T4; this F6 boundary has no fallback and refuses unreadable/current-session-missing evidence.
Q27. Existing allocator reservations write `data/state/tier_capital_allocator.json` atomically with fsync/replace, and decisions append/fsync `logs/tier_capital_allocator.jsonl` (`execution/tier_capital_allocator.py:866-910`); the new strict readers do not write.
Q28. The integration point is inside `ForeverHoldManager.execute_starter` after live asset/open-reducer/cash checks and before event/ownership reservation and broker submit (`execution/forever_hold_manager.py:938-1035`); trims and `check_exits` are untouched.
Q29. It ranks first in the F6 queue because the empirical replay measured that starter cash rules alone do not enforce the tier envelope, and `handoff.md` lists allocator/emergency integration immediately after the now-shipped trim resync.
Q30. A professional multi-strategy executor requires atomic pre-order capital reservation, current exposure snapshots, tier ownership, and portfolio-level capacity; the underweighted audit risk was a valid per-event cash check repeatedly consuming cash without a tier envelope.
Q31. N/A—this is an execution/admission invariant, not an alpha estimate; no Sharpe or return significance is claimed, so deterministic boundary and failure-state tests replace walk-forward/deflated-Sharpe analysis.
Q32. The component tests use synthetic broker/ledger/regime states and no price-bar strategy replay, so look-ahead, survivorship, and bar-data snooping do not enter this correctness result.
Q33. N/A—no return backtest or execution-cost claim is made; pending, partial, ambiguous, and exact-price reservations are counted conservatively from broker order state (`execution/tier_capital_allocator.py:421-455`).
Q34. N/A—this change claims no alpha or expected return; it caps capital after another component has selected an F6 candidate.
Q35. The allocator is exercised in normal and verified-crash synthetic regimes and total-book exposure includes every tier; no worst-day or correlation performance claim is made because this is capacity control, not a signal backtest.
Q36. At the $25K goal, the existing envelopes equal $2,500 normal and $7,500 verified-crash before stricter cash/total-book limits; liquidity remains the existing live instrument/order-path gate, and this change makes no ADV or IEX-liquidity claim.
Q37. N/A—exact admission correctness must hold on every request and is not inferred from a sample mean; strategy statistical power remains part of the separate F6 thesis/enablement review.
Q38. N/A—this is not an edge model and has no decay estimate; any reversal-criterion violation immediately makes the control non-working and forces it off pending owner-approved repair.
Q39. N/A—the change sets a portfolio capacity ceiling rather than choosing an edge/Kelly order size; F6 has no realized closed-trade payoff distribution while disabled, and future actual quantities/costs must come from exact Alpaca fills.

## Verification before approval

- Focused tests: 113 passed plus 11 parameter subtests.
- Python compilation: passed.
- Ruff E/W/F/B: passed.
- mypy `--warn-unreachable`: passed.
- Cold/adversarial review: PASS after fixing mandatory overnight declaration, current-session regime/K evidence, and nonfinite K handling.
- Board Thorp/Taleb: APPROVE on the corrected exact diff.
- Groq and Google AI Studio exact-diff preship reviews remain required after Rafael approves this design.

## Still outside this approval

Emergency-flatten attribution, ETF wind-down detection, full-path pre-market simulation, and
`FOREVER6_ENABLED=True` are separate designs/approvals. This approval cannot enable F6.

Signed: **OpenAI Codex**

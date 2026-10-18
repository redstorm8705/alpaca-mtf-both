# F6 trim fill and ownership reconciliation — 2026-10-17

**Author:** OpenAI Codex
**Status:** implemented behind `FOREVER6_ENABLED=False`; F6 remains off
**Scope:** F6 exit plumbing only; no trigger, fraction, universe, or enablement change
Approval-ID: F6-TRIM-1017
Covers: execution/broker.py,
  execution/forever_hold_manager.py

## Prior work

- `logs/design_records/qhm_earnings_trim_ledger_autoconfirm_GATE_RESULT_2026-08-16.md` establishes the precedent that a broker-side reduction must be confirmed before ownership is changed.
- `logs/design_records/f6_golive_2026-09-26.md` defines the original F6 lifecycle and trim rungs this patch repairs.
- `handoff.md` declares trim resync as the next F6 prerequisite and keeps F6 disabled until the remaining readiness work passes.
- `logs/design_records/weekly_postmortem_tier_split_2026-10-11.md` reinforces canonical tier attribution in post-trade evidence.
- `logs/design_records/observability_and_ops_diagnostic_2026-08-18.md` requires execution outcomes to remain reconstructable from durable operational evidence.
- `logs/design_records/f6_pending_buy_reservation_2026-10-12.md` provides the shipped reserve-before-submit and exact-identity recovery pattern reused for F6 trims.

## Problem

The legacy 10x/20x trim path treated a successful broker submission as an executed
sale. It wrote the rung `done` immediately even when Alpaca reported only an accepted
DAY market order. The next 20x rung then sized from the pre-sale ownership ledger.
This was especially unsafe because F6 trim evaluation runs after hours, when a DAY
market order can remain queued until the next regular session.

## Contract

1. A cross-process trim-state lock serializes the complete rung eligibility,
   reservation, broker submission, and identity-bind transition. A second process sees
   the first reservation and cannot submit a duplicate sell.
2. Before broker submission, the manager creates and durably records the deterministic
   F6 client order ID, requested quantity, and baseline F6 quantity. A lost response or
   crash is recovered by that exact client ID.
3. A successful submission returns the broker acknowledgement. The manager validates
   exact order ID, F6 client ID, symbol, sell side, whole-share quantity, status, and
   cumulative filled quantity, then durably binds that identity to the rung.
4. Accepted or partially filled orders stay `reserved`. They are never called fills,
   never resubmitted, and never unlock the next rung.
5. Only coherent terminal evidence can resolve a reservation. A positive terminal
   fill is applied to F6's ownership claim through
   `apply_authorized_tier_fill_reduction`, including on co-held symbols, and then must
   survive the canonical full ledger replay with zero drift.
6. Only after that replay does the rung become `done`. The next rung reloads the
   post-fill F6 quantity and sizes from what remains.
7. A terminal zero-fill can return the rung to `none` only after a clean canonical
   replay proves ownership did not move. Missing, active, malformed, mismatched, or
   contradictory evidence remains reserved.
8. A crash after the ledger write but before the rung-state write is replay-safe: the
   next pass recognizes the already-reduced, zero-drift claim and completes the state
   transition without subtracting the fill twice.
9. The broker refuses a trim if the live net is not a readable long position or if the
   ownership guard would change the pre-reserved quantity. It never turns an F6 trim
   into a buy-to-cover or silently submits a different share count.
10. Startup interlocks still block new F6 actions, while the exact reserved reducer is
    allowed to reconcile. A successful terminal reconciliation reruns the full startup
    ownership preflight before any new trim can be admitted.

## Failure direction

Every ambiguous path blocks another F6 sell. The cost is a delayed discretionary
profit trim. It cannot widen a position, sell another tier's shares, or reduce the
F6 claim on inference. Legacy `reserved` rows without an exact order identity remain
manual-review cases; the patch does not guess which historical order they meant.

## Validation

- focused regression covers acknowledged-but-unfilled, exact terminal fill, exact
  terminal zero-fill, contradictory identity, and crash/replay idempotency;
- existing F6 protected-buy, startup-reconcile, and ownership-ledger suites remain in
  the focused run;
- F6 stays disabled until allocator/emergency-flatten integration, ETF wind-down
  detection, full-path pre-market simulation, and Rafael's final enablement approval.

## Required questions

Q1. This repairs accounting and execution integrity required before F6 can collect valid paper-account evidence; it does not claim return improvement by itself.
Q2. It does not change intended trade count. It prevents a second trim while the first sell is unresolved and permits the next rung only after the first fill is proven.
Q3. It must exist because an accepted order is not a fill. The simplest safe design is one durable reservation keyed by the exact broker client order ID, followed by terminal reconciliation.
Q4. It extends the canonical ownership ledger v2 and the shipped protected-buy reservation pattern; it does not overlap Claude's strategy work or change Swing, Day, or QHM logic.
Q5. It introduces no market threshold, multiplier, or fitted number. Quantities come from the live F6 ownership claim and cumulative broker fills.
Q6. It exposes the static assumption that submit success equals execution. The forward pass is allocator/emergency-flatten integration using the same reconciled ownership state.
Q7. It connects the F6 manager, broker boundary, ownership ledger, startup preflight, Slack fills, and later-rung sizing. Each connection is exact-identity, fail-closed, and directly tested.
Q8. The causal defect is evidenced by the legacy code writing `done` immediately after a broker ACK. Performance impact is not claimed; the design record labels this execution correctness work.
Q9. Replays cover accepted/unfilled, partial/terminal, zero-fill, mismatched identity, crash recovery, co-holds, and two-rung sizing. Any duplicate sell, foreign-tier reduction, or premature `done` disproves the contract.
Q10. No validated edge is delayed for calibration. This is a bounded prerequisite to making the already-approved F6 design executable.
Q11. It can affect trim timing and quantity, so it received the board and masked-loss review. It cannot exceed the pre-reserved F6-owned quantity.
Q12. It preserves stops, the 7% treatment already approved for F6, never-mask-a-loss, `paper=True`, and existing data-source rules. It does not widen any safety envelope.
Q13. Missing, stale, malformed, ambiguous, or contradictory broker/ledger evidence leaves the rung reserved and blocks another sell.
Q14. Nothing is shadow-only. When F6 is later enabled, reconciliation directly governs whether a rung completes or remains blocked; F6 itself remains off pending its declared prerequisites.
Q15. Reservation, order identity, requested quantity, baseline quantity, status, and cumulative fill are persisted; terminal fills also produce the existing fill and Slack records.
Q16. The trim state and ownership ledger prove the transition, and `F6 trim fill reconciled` / zero-fill reconciliation logs identify the outcome. The first live paper session must be replayed against Alpaca order history.
Q17. Runtime proof is the full-path pre-market simulation plus the first enabled paper-account terminal fill reconciled by exact client order ID with zero ledger drift.
Q18. The accepted-order behavior was re-examined and rejected; the patch removes the flawed design rather than calling it expected broker behavior.
Q19. Rafael authorized the F6 prerequisite sequence and trim-resync scope before implementation; this changes exit plumbing and therefore went through the full risk gate.
Q20. Review prompts asked reviewers to find failure modes and did not state a preferred verdict. The complete touched paths, design record, focused suite, board, cold review, adversarial review, Groq waiver, and GAI preship were completed.
Q21. Tests mock every Alpaca order and position read. The test broker path remains blocked by the repository's existing no-live-broker protections.
Q22. This is not a new live fail-closed control while F6 is disabled. The full-path simulation and read-only live ownership preflight remain prerequisites before enablement.
Q23. The component simulation passes, but F6 is explicitly not called ready. A full-path pre-market simulation remains required before the owner enablement package.
Q24. No claim about symbol existence, tradability, or ETF availability is introduced.
Q25. No stock or market statistic is used or asserted by this patch.
Q26. Broker order state and positions use T1 Alpaca. There is no fallback: unreadable or inconsistent data leaves the trim reserved.
Q27. It writes the existing F6 trim-state JSON and canonical ownership-ledger JSONL under the existing configured paths, using their locked durable-write mechanisms; notifications use the existing Slack path.
Q28. It connects inside `ForeverHoldManager` after rung eligibility and before advancing trim state; broker validation runs at submission, reconciliation runs before new actions, and startup preflight runs again after resolution.
Q29. It is the next handoff prerequisite because the old path could duplicate sells or size rung two from stale ownership, invalidating both risk and evidence once F6 is enabled.
Q30. A professional execution framework requires order lifecycle state, durable idempotency, partial-fill accounting, position-side validation, and cross-process serialization. The largest underweighted risk was a crash between broker mutation and local state.
Q31. Walk-forward and deflated Sharpe are not applicable to an order-lifecycle correctness invariant. Multiple deterministic crash, fill, co-hold, and identity variants were tested instead.
Q32. The tests use synthetic broker events and ownership state, so they contain no bar data, universe selection, or fitted sample that could introduce look-ahead, survivorship, or snooping.
Q33. This patch does not estimate alpha or replay prices. It explicitly models missed, partial, delayed, canceled, expired, and duplicate fills; broker cumulative fill quantity is authoritative.
Q34. No expected-return claim is made, so alpha/beta attribution is not applicable.
Q35. Market regime and correlation do not alter the execution invariant. Worst-case ambiguous or contradictory evidence blocks another F6 sell rather than guessing.
Q36. Whole-share quantities at the $25K target remain far below liquidity capacity for the current F6 universe; the contract still fails closed on unreadable live position state.
Q37. Statistical power is not applicable to exact lifecycle correctness. Every supported state transition must satisfy the invariant on every occurrence.
Q38. There is no edge-decay parameter. Any duplicate submission, ownership drift, wrong-tier reduction, or ACK marked as a fill is an immediate defect requiring halt and repair.
Q39. This patch does not choose entry size or Kelly fraction. Trim quantity derives from the approved rung plan and reconciled F6-owned shares; realized quantity comes from Alpaca fills.

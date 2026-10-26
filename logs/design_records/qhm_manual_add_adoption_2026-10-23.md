# QHM manual-add adoption — 2026-10-23 (OpenAI Codex)

Approval-ID: QHM-ADD-1023

Covers: execution/broker.py, execution/manual_sale_proof.py, execution/ownership_guard.py, execution/quarterly_hold_manager.py, tests/test_broker_stop_replace.py, tests/test_qhm_owner_buy_autoheal.py, tests/test_qhm_owner_sale_autoheal.py, tests/test_qhm_stop_quantity_reconcile.py, tests/test_qhm_tranche_adds.py, tests/test_ownership_ledger_v2.py, tests/tests_tier_capital_allocator.py, logs/design_records/qhm_manual_add_adoption_2026-10-23.md, handoff.md, logs/tb_audit_log.md

## Problem and measured incident

At 2026-10-23 16:04:34 UTC, Alpaca filled one owner-placed GE buy (`source=null`, UUID client order ID
`3d6ddcae-...`) at $286.74. The broker then held 3 GE shares at a $302.496667 average while QHM still
owned 2 at $310.375 and its stop covered only 2 at $272.71. The canonical replay assigned the untagged share to
Swing, producing `broker=3, Swing=1, QHM=2` and repeated `[unmanaged-position]` pages. A one-time repair on OCI
resized the existing QHM stop in place to 3 shares at $272.71, updated QHM to 3 shares at the broker average, ran
ledger sync, and verified `broker=3, Swing=0, QHM=3, protected_floor=3`. The dynamic QHM stop recalculated from 14
weekly bars was $257.12; the existing $272.71 stop stayed because loosening protection after an add is forbidden.

## Exact design

1. Extend the existing raw Alpaca owner-order proof with a buy evaluator. A manual add is proven only when every
   filled order after a persisted proof timestamp is readable, the positive quantity change is explained exactly by
   terminal, fully filled BUY orders with `source` present and null, random UUID client IDs, and no tier tag, and the same response contains
   a bot-tagged `source=access_key` order proving that Alpaca's source field is live. Any sell, API buy, unknown source,
   nonterminal/partial order, truncated page, fractional quantity, quantity mismatch, or transport error refuses
   adoption. The existing bounded raw-order read rejects a full page rather than treating it as complete. Network
   failures retry on the next QHM cycle and never clear a staged adoption.
2. Persist a QHM manual-add baseline only after one broker read proves: long whole-share position; broker quantity,
   QHM state quantity, QHM ledger quantity, and total ledger quantity are identical; and no other tier owns the
   symbol. This lets later cycles prove which shares existed before an owner add. A larger broker quantity never
   advances the baseline before the increase is explained.
3. On a proven increase, enter the existing cross-process `symbol_order_gate` and reread position and the complete
   open-order inventory. Require the same quantity and average cost, exactly one stable QH-tagged GTC reducing stop
   with finite price and exact baseline quantity, **no live foreign order capable of changing net quantity**
   (buy/cover entries as well as stops, targets and OCO children), and no newly filled API order. The position, direction, finite positive current price, and complete owner-order proof
   are reread under the symbol gate; changed evidence refuses adoption. A foreign order refuses adoption; it is never cancelled. Compute
   the ordinary data-derived QHM
   design stop from the new broker average and weekly ATR. For a long, choose `max(existing live stop,
   recalculated design stop)`: a higher sell stop is tighter for a long, so the add can tighten protection but cannot
   loosen it. If the current market is already at/below the candidate or stop state is unstable, refuse and page.
4. **Durable intent before broker mutation:** atomically/fsync persist a staged adoption containing the baseline,
   exact terminal owner order IDs, delta, expected broker quantity/average, prior stop identity/price, candidate stop,
   phase, and a pre-generated QH replacement client order ID. Extend `replace_stop_order` to accept that exact ID so
   recovery can query it whether the request was lost before Alpaca or only the reply was lost. While staged, QHM blocks ordinary coverage resize, close accounting, stray cleanup, dip-add, trailing,
   earnings and other stop mutation for that symbol. Recovery resolves the live replacement chain and exact fills
   before any ordinary work; an unavailable history read preserves full existing coverage and the stage.
5. Stop first: use the existing `replace_stop_order` Alpaca PATCH path for the exact QHM stop and complete new QHM
   quantity. It preserves the old stop until Alpaca swaps it. Then reread broker position **and direction** plus all
   open orders and require exactly one live QH stop with the replacement client identity, quantity and price, no
   live foreign order, and a still-long broker position. The exact happy path commits automatically. Any post-PATCH
   quantity change, side flip, foreign order, double-live descendant, unreadable shape, or otherwise ambiguous state
   abandons automatic adoption: persist an ABORTING phase, retain only an exact baseline-sized QHM stop when aggregate
   live sell reservations are broker-proven safe, otherwise terminal-confirm cancellation of every QH descendant, then reread the broker position and complete order book and
   restore the exact pre-add QHM stop inside the same symbol-gated transaction whenever the long baseline remains
   broker-proven safe; only then clear the stage into ordinary QHM reconciliation. The proven owner order IDs are durably blocked from another
   automatic attempt; the added share remains unassigned and the existing unmanaged-position page stays actionable.
   Replacement-chain terminal fills are summed across the directly read old and replacement IDs before any close is
   booked; a complete stop-out emits the normal structured QHM exit evidence. This fallback never assigns ambiguous
   exposure, never submits a sell against a short, and never leaves the symbol permanently fenced or knowingly
   unprotected behind a containment stage. Containment sandwiches its complete order-book read between two fresh
   broker position reads and retains a baseline stop only when quantity and long direction are identical across the
   proof. If ambiguous QHM stops must be canceled, baseline reprotection runs only after a second independent
   position/book/position sandwich also proves stable long quantity and safe aggregate sell reservations. A directly resolved live replacement that is omitted by the list endpoint preserves the stage and blocks
   every new stop. Once the ownership-commit phase is durably staged, containment is forbidden: an interrupted
   ledger/journal projection must finish idempotently rather than regress QHM state or stop coverage. If the exact
   verified replacement later reads terminal-canceled or expired with zero fills, recovery completes the idempotent
   ownership projection and establishes a new exact full-quantity QHM stop before finalizing state. Any weaker
   terminal evidence preserves the stage. The durable recovery client identity is accepted as the exact live stop
   after restart and is included with the old/replacement chain in terminal-fill accounting; a crash after recovery
   submission therefore neither strands the stage nor loses a later stop fill. Every direct status without exact
   terminal proof, including broker transitional/unknown statuses, preserves the stage; a `replaced` order is followed
   through a bounded, cycle-checked broker descendant chain rather than treated as terminal by itself. During an
   already-durable commit phase, an explicit foreign BUY does not prevent restoring protection for the currently
   proven long quantity. The stop helper takes one last complete order-book snapshot adjacent to submit and refuses
   if any foreign reducing order has appeared. Position reads immediately before and after recovery must still prove
   long ownership. If a second terminal owner BUY fills across that recovery window, exact raw-order proof expands
   the same durable transaction, treats the recovered stop as its new protected baseline, and stages the next atomic
   resize for the full broker quantity. The stage preserves separate ownership and live-stop baseline quantities,
   and records that ownership projection has begun so later replacement phases cannot regress through containment.
   Any unexplained increase remains fenced and paged. An unavailable broker/order read does not abandon or mutate: it
   preserves the stage and current broker protection for a later exact retry.

6. Ownership second: write an atomic, replay-safe owner-assignment journal keyed by the exact terminal Alpaca order
   IDs. New rows are first fsynced as `pending`; canonical replay ignores them until the immediate ledger projection
   succeeds and a second fsync promotes them to `committed`. A failed ledger save therefore cannot leave an abandoned
   adoption that later changes tier ownership without its matching QHM stop. Recovery idempotently commits a pending
   row when the ledger already has the exact final projection. If another proven owner order joins an in-flight
   transaction after an earlier order was committed, the ledger accepts the exact intermediate quantity credited by
   those already-committed order IDs before projecting the expanded final quantity. Canonical `sync_ledger` consumes only committed rows so every rebuild assigns those fills to QHM instead of its
   legacy untagged-to-Swing default. Under a **strict** cross-process ledger lock that refuses on timeout (the current
   best-effort lock may proceed unlocked), move only the proven delta from the replay-default Swing
   residual to QHM (or credit it when the ledger has not replayed yet). Require broker net to equal the full ledger
   total, the stored sole-QHM baseline, and the proven delta; require every non-QHM claim to be either zero or exactly
   the proven untagged residual. Record the exact owner order IDs for replay safety. Any mismatch refuses the write.
7. State last: atomically persist QHM quantity, broker average cost, replacement stop ID/price, proof time and new
   baseline, then mark the staged intent complete. Emit one `owner_buy_autoheal` trade event carrying owner order IDs,
   pre/post stop IDs and the ledger assignment key, plus one plain-English Slack confirmation containing shares,
   average and stop. A later canonical sync must reproduce QHM ownership and zero drift. If the process crashes at
   any phase, the durable stage makes recovery idempotent: it adopts an already-correct replacement/ledger row instead
   of duplicating it, and it never lets ordinary reconciliation shrink the full-coverage stop back to the old count.
8. This design auto-adopts owner buys only into an already active, exclusively owned QHM symbol. Day and Swing can
   have OCO/target/trade-lot semantics, and F6 remains disabled; those need tier-specific adapters rather than a blind
   ledger reassignment. A co-held or otherwise ambiguous symbol continues to page with no mutation.
9. Reversal criterion: disable automatic buy adoption on any false attribution, a null-source API order, a stop
   replacement whose confirmed shape differs, or a post-live case where ledger/QHM/broker quantities do not converge
   in the same cycle.

## Front-loaded replay and result checks

- GE 2026-10-23: QHM-only baseline 2, one proven owner buy at $286.74, broker 3 at $302.496667, live stop 2 at
  $272.71 -> adopt 1, retain tighter $272.71, replace to 3, QHM/ledger/broker all 3.
- Counter-case: a QH-/DT-/IN-/F6-tagged API buy after the baseline -> refuse.
- Counter-case: QHM and another tier already co-hold the symbol -> refuse.
- Counter-case: owner buy and owner sell in the proof window, fractional fill, truncated response, unreadable broker,
  or two QHM stops -> refuse.
- Crash splits: intent before replacement resumes safely; after replacement but before ledger write, recovery adopts
  the exact replacement and blocks ordinary resize; after ledger write but before state save, recovery finds the
  assignment key and finalizes QHM state without duplicating either mutation.
- Foreign-order case: any IN-/DT-/F6-/untagged reducing reservation beside the QH stop refuses adoption and leaves it
  for operator resolution; the QH stop is never expanded on top of another tier's reservation.
- Foreign-entry case: a pre-existing non-QH buy/cover refuses a new adoption. During recovery of an ownership commit,
  a newly filled owner BUY is re-proven from the original proof boundary and expands the same staged transaction;
  its next cycle atomically grows the recovered QHM stop from the prior proven quantity to the new broker quantity.
  An API buy, partial fill, changed earlier proof, or unexplained increase remains fenced and cannot expand QHM.
- Late foreign-reducer case: a reducing order that appears after the outer recovery read is caught by the stop
  helper's final complete-book snapshot, and no additional QHM sell stop is submitted.
- Post-PATCH reduction case: the automatic adoption is abandoned; an exact safe baseline stop is retained or every QH descendant is confirm-cancelled, and the owner order is blocked from automatic replay.
- Post-PATCH foreign-reservation case: the foreign order is never touched; the automatic adoption is abandoned and QH descendants are retained only when baseline coverage plus foreign reservations is broker-proven safe.
- Double-live replacement case: neither descendant is called the sole good stop; automatic adoption is abandoned and each QH descendant is terminal-confirm cancelled before ordinary recovery resumes.
- Journal crash case: a ledger-save failure leaves only a pending assignment that canonical replay ignores; a retry
  can commit it only after the exact ledger projection succeeds.
- Replaced-ancestor case: terminal accounting requires both the ancestor and its exact client-ID-bound replacement;
  an unreadable or mismatched descendant leaves the durable stage in place.
- Flat-after-replace case: recovery resolves the replacement chain and exact fill quantity before booking a close, so
  it cannot report only the stale pre-add QHM size.
- Machine result: mocked full-path test asserts exact broker qty/average, stop-first call order, one live stop covering
  all QHM shares, QHM state == ledger QHM == broker net, zero non-QHM ownership, one event, and one alert.
- Final exact-diff validation on OCI Python 3.10: 294 focused tests and 82 subtests passed. The full suite recorded
  2,791 passed, 1 skipped and 183 subtests; its one Day-promotion fixture failure reproduced unchanged on clean
  `origin/main` at `ef7fd74`, so it is not caused by this QHM diff.

## Required questions

Q1: Yes—this prevents a paid-for QHM share from becoming unmanaged while preserving its participation in the paper account; the measured GE share was ~$287 and temporarily lacked QHM protection.
Q2: No entry signal or bot trade frequency changes; only an owner buy already filled at Alpaca is adopted.
Q3: Yes—the simpler manual repair worked once, but exact broker proof plus automatic stop/ledger reconciliation removes recurring operational risk without blocking owner adds.
Q4: `qhm_owner_sale_autoheal_2026-10-17.md` supplies owner-source proof and stop-first ordering; `ownership_guard.py:963` supplies exact fill credit; PR #542 supplies canonical ledger v2; open PR #727 is documentation-only and does not overlap.
Q5: No trading threshold is added; quantity is the exact broker delta and the candidate stop is derived by the existing weekly-ATR design, with the current live stop as the monotonic protection floor.
Q6: The exposed static assumption is that every untagged buy belongs to Swing; next, the canonical replay should preserve explicit owner-assignment evidence instead of a default tier.
Q7: It connects Alpaca raw orders, QHM state, broker stop lifecycle, the canonical ownership ledger, unmanaged-position detection, trade events and Slack; every connection is exact and fail-closed.
Q8: Yes—live GE evidence measured broker 3/QHM 2, one null-source UUID buy at $286.74, a 2-share $272.71 stop, and repeated unmanaged pages before repair.
Q9: Replay includes the GE incident plus API-buy, co-hold, mixed-order, fractional, truncated, double-stop and crash-boundary counter-cases; a false attribution or nonconvergent quantity disproves it.
Q10: N/A—this reconciles ownership and protection; it does not claim or calibrate an alpha edge.
Q11: The bot does not initiate or increase exposure, but the stop quantity changes, so this is risk-path and requires broker-lifecycle and masked-loss board seats.
Q12: It strengthens the stop-on-every-position envelope, does not touch the 7% daily brake, never lowers a live stop, and leaves `paper=True` unchanged.
Q13: Missing/stale/wrong order, position, ledger, state or stop data refuses adoption and leaves the existing alert active; a persisted in-flight stage blocks ordinary mutators until exact recovery.
Q14: No shadow path—the approved rule will mutate the live paper account only after all proof predicates pass.
Q15: Yes—`owner_buy_autoheal` records symbol, delta, broker quantity/average, old/new stop, order IDs and proof window.
Q16: A `QHM <symbol> owner-buy adopted` line plus event/ledger rows proves execution; the first full session is replayed against broker orders, QHM state and stop coverage.
Q17: OCI read-only preflight will run the detector against live broker/state before enablement; a mocked full path and post-deploy broker/state/ledger read prove runtime behavior.
Q18: No—the default-to-Swing behavior caused the measured GE misattribution and is treated as a faulty design assumption for this proven case.
Q19: Yes—it changes protective-stop quantity and requires Rafael's approval of `QHM-ADD-1023` before implementation commit.
Q20: Prompts will present facts and failure cases neutrally; live logs, raw orders, state, ledger and stops were reviewed before design, followed by board/Gro/GAI review.
Q21: All new broker calls are mocked in tests; tests patch the raw REST fetch and broker methods and never load live credentials.
Q22: Yes—the detector and proposed decision will be run read-only on OCI against the repaired GE facts before live code is enabled.
Q23: A full-path simulation must prove manual fill -> proof -> stop replace -> ledger transfer -> state save -> zero unmanaged residual before readiness.
Q24: N/A—no claim about a security's existence, tradability or ETF status is made.
Q25: The only market statistic is QHM's existing 14-week ATR, calculated from Alpaca bars; live GE replay measured 19 weekly bars, ATR $20.3812 and design stop $257.12.
Q26: T1 Alpaca raw orders, positions, open orders and weekly bars; any missing input refuses adoption with no fallback estimate.
Q27: QHM state, owner-assignment journal and canonical ledger use atomic fsync/replace writers; trade evidence appends JSONL; the broker stop is changed with Alpaca's in-place PATCH replacement API.
Q28: It runs inside QHM's existing reconciliation before ordinary stop coverage; proof and stop replacement run before ledger/state mutation, then ordinary reconciliation verifies coverage.
Q29: P0 operational integrity—today an owner-funded GE share was misattributed and initially under-covered, so this ranks ahead of new F6 capability work.
Q30: Professional execution requires immutable fill attribution, a durable phase machine, idempotent reconciliation and stop-before-book ordering; cross-process ledger sync and broker-order races are covered by a strict ledger lock, symbol gate and replay-safe assignment IDs.
Q31: N/A—no return edge or fitted model is introduced; walk-forward Sharpe is irrelevant to ownership reconciliation.
Q32: N/A—no market backtest; event replay uses timestamped broker orders and persisted proof boundaries without future data.
Q33: N/A—the owner fill has already occurred; no simulated execution or cost claim is made.
Q34: N/A—no expected return or factor exposure claim is made.
Q35: N/A—the rule is regime-independent bookkeeping and protection after an externally initiated fill.
Q36: Yes—whole-share exact matching and broker-native stop replacement work at the current ~$2.5K account and target $25K scale; no liquidity claim is needed.
Q37: N/A—this is an invariant, not a statistical edge; one counterexample is enough to trigger reversal.
Q38: N/A—no strategy edge is admitted; health is exact convergence and false-attribution count, with any false attribution disabling the path.
Q39: N/A—the bot does not size the owner order; it adopts the exact Alpaca-filled quantity.
Q40: It protects full QHM ownership and stop coverage; the machine check requires QHM state == replayed ledger QHM == broker net, zero foreign claim/reservation, one live QH stop remaining qty == that value, and no unresolved adoption stage.
Q41: A professional would also persist explicit owner-assignment events so future ledger rebuilds never infer tier from an untagged order; this design writes exact order IDs as the first step toward that source of truth.
Q42: Raw Alpaca order JSON, position, open-order objects, QHM state and canonical ledger are used, not Slack or report summaries.
Q43: The professional method is event-sourced reconciliation with a durable phase journal, idempotency keys, broker-truth rereads, symbol serialization, stop-first ordering and atomic local writes; the design follows it.
Q44: Independent board seats, Groq and Google AI Studio review the design/diff; CI and the post-deploy broker/QHM/ledger/stop invariant independently prove runtime behavior.

## Prior work

- `handoff.md` — current cross-agent queue, shipped ownership/stop prerequisites and F6 boundary.
- `logs/design_records/qhm_owner_sale_autoheal_2026-10-17.md` — raw Alpaca owner-order proof, fresh reread, stop-first sequencing and fail-closed behavior.
- `logs/design_records/qhm_stop_quantity_reconciliation_2026-09-29.md` — QHM stop quantity must equal QHM-owned shares rather than account net.
- `logs/design_records/live_tier_capital_allocator_2026-09-27.md` — canonical ownership and protected-floor invariants.
- `logs/design_records/dashboard_protection_truth_2026-10-22.md` and `dashboard_held_stop_status_2026-10-23.md` — broker-native protection reporting.
- `logs/design_records/pnl_untagged_lot_tier_2026-10-17.md` — durable tier attribution for untagged owner activity.
- `logs/design_records/f6_trim_resync_2026-10-17.md` — exact-fill ledger resync and idempotent recovery.
- `logs/design_records/f6_scoped_allocator_2026-10-18.md` — cross-tier allocator and ownership boundaries.
- `logs/design_records/f6_etf_winddown_2026-10-21.md` — current four-tier product boundary and startup reconciliation.
- `logs/design_records/f6_emergency_flatten_attribution_2026-10-18.md` — broker-truth stop attribution and fail-closed shutdown proof.
- `logs/design_records/canonical_four_tier_identity_2026-10-08.md` — canonical Day, Swing, QHM and F6 naming/identity contract.
- `logs/design_records/audit_triage_2026-10-16.md` — current audit ordering and operational severity framework.

## BGGN design review round 1

- Broker-lifecycle board: REJECT. It found that a replay-created Swing stop could coexist with the QH stop and make
  expansion reject or over-reserve, and that a crash after PATCH could let ordinary reconcile shrink coverage back
  to the stale state. Adopted complete foreign-reducer refusal and a durable pre-PATCH stage that blocks all ordinary
  QHM mutators until exact recovery. Round 2 found post-PATCH reduction could leave an oversized stop if ordinary
  shrink failed; adopted a mandatory persisted recovery that must confirm shrink or cancel every exact descendant.
  Round 3 found an owner-app reducer could appear after PATCH without changing net yet; adopted the same mandatory
  aggregate-reservation recovery based on QH plus exact foreign remaining quantities.
- Masked-loss board: REJECT. It found stale close accounting after a replace/crash, non-durable canonical replay
  attribution, partially filled order ambiguity, lack of whole-transaction symbol serialization, and insufficient
  live-stop validation. Adopted exact-terminal-fill proof, a replay-consumed assignment journal, full symbol gate,
  stable stop validation and replacement-chain recovery before close accounting. Round 2 found a pre-existing
  foreign buy could fill while the gate was held; adopted refusal of every foreign live order plus the rule that a
  quantity increase can never expand QHM ownership/stop without exact fill attribution.
- Final broker-lifecycle review found a reducer could arrive between the outer order read and the recovery helper's
  submit. The helper now performs the adjacent complete-book refusal itself and accepts only a positively parsed
  nonreducing side; an unreadable side refuses submission. Final masked-loss review found an owner
  BUY could fill during the commit-recovery sandwich and leave the recovered stop one share short indefinitely. The
  same raw-order proof now expands the staged transaction, and the ledger recognizes only the exact intermediate
  quantity already credited by committed owner-order IDs. A follow-up replay found the expanded stage confused the
  original ownership baseline with the recovered stop's quantity; these are now separate fields, projection-started
  state forbids regression containment, and a two-cycle regression proves stop 3→4 plus ledger/QHM/broker convergence.
  A terminal-fill follow-up found fill accounting still validated that recovered stop against the ownership baseline;
  it now validates the old order against the separate live-stop baseline, with full-fill and partial-fill replays that
  commit the exact residual ownership and restore its stop.
- Google AI Studio: APPROVE-WITH-CHANGES. Its proposed global symbol lock maps to the repository's existing
  cross-process `execution.order_admission.symbol_order_gate`; adopted it around the complete transaction, plus exact
  post-PATCH identity/shape verification and a durable staged state. Local ledger/state cannot be one atomic file, so
  the phase journal supplies ordered idempotent convergence instead of an impossible cross-file transaction. Its
  counter-review accepted the actual ledger-after-stop sequence and PATCH recovery, then required a pre-dispatch
  replacement client ID plus explicit double-live descendant handling; both are adopted. Its proposed pre-PATCH
  foreign-order check was already present, while the post-PATCH race has the mandatory recovery above. A flock never
  needs manual lock-file cleanup because the kernel releases the lock on process death.
- Groq: APPROVE-WITH-CHANGES. Adopted valid findings: durable idempotency, post-replace exact-one-stop proof,
  baseline refresh, full-page refusal, richer audit evidence and crash tests. Rejected suggestions contradicted by
  measured/platform facts: Alpaca server assigns `source` (a third-party API order is `access_key`; 3,092/3,092
  measured), app buys cannot carry a bot HMAC; `replace_stop_order` already uses Alpaca PATCH and preserves the old
  stop until swap; a higher sell stop is tighter, not looser; tests are forbidden from reaching the real broker; QHM
  is whole-share only; the existing next-cycle retry is safer than an in-thread sleep; Redis is unnecessary because
  the repo already has kernel-released cross-process flock locks.

## Approval

Rafael approved `QHM-ADD-1023` in this chat on 2026-10-23. The shared approval capture is checked again immediately
before commit so its file coverage matches the exact final diff.

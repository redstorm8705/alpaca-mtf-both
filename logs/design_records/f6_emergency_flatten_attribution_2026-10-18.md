# Stop-preserving explicit shutdown — revised F6 prerequisite

**Author:** OpenAI Codex
**Status:** Rafael approved the final read-only design on 2026-10-20; ready for commit/CI/deploy
Approval-ID: F6-FLAT-1018
Covers: events/handlers.py, execution/stop_protection.py, main.py, tests/test_shutdown_preserves_swing.py, logs/design_records/f6_emergency_flatten_attribution_2026-10-18.md, logs/post_live/expect/f6-stop-preserving-shutdown.json, handoff.md, logs/tb_audit_log.md

## Prior work

- `logs/design_records/f6_scoped_allocator_2026-10-18.md` names emergency shutdown/F6 attribution as a launch prerequisite.
- `logs/design_records/f6_pending_buy_reservation_2026-10-12.md` and `logs/design_records/f6_shared_symbol_residual_stop_2026-10-12.md` define protected claims and shared-symbol stop bounds.
- `logs/design_records/ownership_ledger_v2_2026-10-09.md` defines canonical tier ownership.
- `logs/design_records/same_direction_cohold_2026-10-10.md` permits same-direction co-holds.
- `logs/design_records/f6_trim_resync_2026-10-17.md` defines exact, replay-safe F6 reduction accounting.
- `logs/design_records/gex_full_week_coverage_2026-10-17.md` is adjacent signal work and does not alter shutdown execution.
- `logs/design_records/day_tier_internet_strategy_audit_2026-10-05.md` is adjacent Day research and does not own Swing shutdown.
- `logs/design_records/day_tier_v2_design_2026-08-29.md` defines earlier Day execution boundaries and does not authorize Swing shutdown liquidation.
- `logs/design_records/audit_triage_2026-10-16.md` separates this work from Claude's active queue.
- `handoff.md` identifies F6 shutdown integration as open.

## Measured blocker in the approved flatten design

The exact-diff execution, lifecycle, and masked-loss reviews produced executable counterexamples against the forced-flatten design. After a market reducer crosses Alpaca's mutation boundary but its response is lost, the API can temporarily return no order. At that point the bot cannot prove whether the sell exists. A replacement stop can coexist with the hidden reducer against account-level co-held shares and both can fill; not placing it leaves Swing without a stop. Alpaca's official replace-order endpoint can change quantity, price, time-in-force, and client ID, but it does not turn a stop into a market order atomically. This is a broker-interface limitation, not an untested performance claim.

Concrete case: Alpaca holds four NVDA shares: Swing 1, QHM 2, F6 1. A one-share Swing market sell is accepted but its response is lost. A second one-share stop is individually allowed against the four account shares. If both execute, net holdings become two and one protected QHM/F6 share has been sold. No later reconciliation can undo that sale.

## Revised exact design

1. Explicit `KeyboardInterrupt` no longer invokes `safe_close_all(..., circuit_breaker=True)` for Swing positions. Circuit-breaker, kill-switch, Day, QHM, and F6 behavior elsewhere is unchanged.
2. Add `preserve_swing_on_shutdown(tracker, risk)` in `events/handlers.py`. It calls the existing production stop enforcer `execution.stop_protection.reconcile_protection` with `session="closed"`, `place=False`, and exactly the current Swing symbols. Shutdown is a read-only proof: it never submits a competing exit or stop while the operator is trying to terminate the process.
3. The existing enforcer remains the sole stop authority. It reads the full broker order book and positions once, applies canonical co-hold ownership bounds, recognizes QHM/F6 exclusions, and proves that every current Swing remainder already has stable, live, correctly attributed GTC coverage at least as protective as its stored stop. Missing protection stays under the normal running manager; shutdown is deferred. Any QHM/F6 exclusion is reported as unresolved rather than mislabeled successful. Missing or contradictory broker/ownership data fails closed and pages.
4. The wrapper logs a machine-readable summary. Any `paged`, `skipped`, protected-tier exclusion, breached naked position, or non-GTC-only coverage produces a critical alert and a non-success result; it never fabricates a close or cancels a tier's order.
   A broker `PROTECTION_ALREADY_HELD` sentinel is also unresolved in shutdown mode because it proves reserved shares but not the hidden order's time-in-force; shutdown waits for a later readable snapshot that proves GTC.
   Shutdown submits no stop, so an ambiguous broker response cannot create duplicate protection. Any pending/non-open tracker row blocks shutdown rather than disappearing from the proof.
   Transitional or unknown broker order states, including `pending_cancel` and `pending_replace`, cannot prove shutdown-safe coverage; only an explicitly stable `new` or `accepted` GTC stop qualifies.
   The broker position quantity and current price must both be finite and positive, so an unreadable market price cannot bypass the stored-stop breach check.
   The signed broker quantity and any reported broker side must agree with the tracker direction; a stop that would increase the live position cannot prove protection.
   Every counted broker stop must itself remain unbreached at the finite current price; a stale working order beyond the market cannot authorize shutdown.
   The wrapper requires exactly one `already_protected` result for every Swing symbol in its initial snapshot; missing, duplicate, or unexpected proof rows block shutdown.
   A repeated Ctrl-C during the broker proof is converted to an unresolved result, so it cannot bypass protection and terminate the process.
5. `main.py` calls the wrapper on explicit user shutdown. If protection is unresolved, shutdown is deferred and the live process continues managing positions. Only a successful GTC-protection result proceeds to QHM persistence and process exit. SIGTERM stays save-and-restart. The 7% daily kill switch and its live-session liquidation route are unchanged.
6. F6 remains disabled. This change does not enable F6, add an entry, change sizing, or alter a trading signal.
7. Deterministic tests prove: no close or submit API is called; correctly protected Swing is left untouched; uncovered, stale-price, breached, transitional, pending, and unreadable cases block shutdown; enforcer failure and repeated Ctrl-C are contained; QHM/F6 persistence calls remain downstream in `main.py`; `paper=True` remains unchanged.
8. Reversal criterion: revert if explicit shutdown submits a Swing close, cancels a live Swing stop, mutates QHM/F6 claims, or exits without a proven/paged stop-enforcement result.

## Required questions

Q1. Yes — it keeps paper-account Swing positions managed instead of risking protected-share corruption; evidence is the executable four-share NVDA counterexample above.
Q2. No strategy trade frequency changes; only explicit user shutdown stops forcing Swing exits (`main.py:1113-1116` on origin/main).
Q3. Yes; reusing the existing sole stop enforcer is simpler than a second crash-recoverable close state machine (`execution/stop_protection.py:449`).
Q4. It reuses the cited co-hold, ledger, stop, allocator, and handoff records; origin/main and open-PR review found no parallel stop-preserving shutdown patch.
Q5. No new threshold or multiplier is added; quantities and stops come from broker state, canonical claims, and each trade's stored dynamic stop.
Q6. The exposed static assumption is that explicit process exit must liquidate Swing; the next adaptive capability is broker-supported atomic reduce-only execution if Alpaca exposes it.
Q7. It connects shutdown to the production stop enforcer, broker positions/orders, ownership snapshot, tracker, risk, and Slack; unreadable connections page and refuse mutation.
Q8. The causal claim is backed by three independent exact-diff counterexamples and the cited source path; no return claim is made.
Q9. Simulation covers protected, uncovered, co-held, unreadable, and enforcement-failure cases; any close submission or silent unprotected remainder falsifies it.
Q10. N/A — this is execution safety, not delayed alpha calibration.
Q11. It removes only explicit-shutdown forced liquidation; no size, concurrency, Kelly, or gross-cap change occurs during trading.
Q12. It strengthens the every-position-has-a-stop rule; 7% kill switch and `paper=True` remain unchanged.
Q13. Missing/stale/contradictory data follows `reconcile_protection` fail-closed paging and never triggers a close.
Q14. No shadow path; the approved behavior becomes live on deployment.
Q15. The wrapper logs the full enforcer summary, whose per-symbol action buckets are already machine-readable.
Q16. `SHUTDOWN-SWING-PRESERVE` proves invocation; first explicit shutdown is replayed from broker orders, tracker, and alerts.
Q17. A process-level test calls the real wrapper with mocked broker boundaries; OCI readiness imports the exact deployed function with F6 still disabled.
Q18. Yes; repeated adversarial failures caused rejection of the original force-flatten design instead of explaining them away.
Q19. Yes — Rafael explicitly approved the final read-only `F6-FLAT-1018` design on 2026-10-20 after the exact-diff revisions were presented.
Q20. Reviews are neutral and require counterexamples; all three independent seats rejected the old approach before this revision.
Q21. Tests mock `reconcile_protection`; no test reaches Alpaca.
Q22. The underlying fail-closed control is already live; OCI validation is read-only and checks imports/config.
Q23. Not ready until the full-path shutdown simulation passes.
Q24. Alpaca replace-order capabilities were checked against official Alpaca documentation in this turn.
Q25. N/A — no market statistic is used.
Q26. T1 Alpaca positions/orders feed the existing enforcer; no lower-tier fallback may authorize an order.
Q27. N/A — shutdown verification writes no stop ID or trade state; normal stop management retains its existing durable tracker writes.
Q28. It runs only inside `main.py`'s `KeyboardInterrupt` handler before QHM safe-stop and final save.
Q29. It ranks first because the prior F6 prerequisite was blocked by a protected-share P0 and this removes the unsafe mutation surface.
Q30. Professional execution requires reduce-only/atomic cancel-replace semantics; absent that venue primitive, preserving verified protection is the defensible boundary.
Q31. N/A — deterministic process/order invariants, not alpha, so walk-forward/DSR do not apply.
Q32. N/A — no bars, fitted sample, or future data enter the decision.
Q33. No fill assumption exists because the revised path submits no exit or stop; it trusts only a stable live GTC order visible in Alpaca's broker snapshot.
Q34. N/A — no return attribution claim.
Q35. The invariant is regime-independent; broker/ownership uncertainty pages in every regime.
Q36. Whole-share quantities and co-held floors already scale beyond the $25K target; no liquidity claim is made.
Q37. N/A — one protected-share sale or silent naked remainder falsifies the invariant.
Q38. N/A — this is not a decaying edge; any invariant breach triggers immediate reversal.
Q39. N/A — no sizing change; no P&L estimate is used.
Q40. It protects “explicit shutdown leaves every Swing share stopped without selling QHM/F6”; tests assert zero close calls and an enforcer result for every Swing symbol.
Q41. The expert step is to require a venue-native reduce-only or atomic conversion primitive before reintroducing forced flatten.
Q42. Raw broker positions/orders and canonical tracker/ownership records feed the existing enforcer, not Slack summaries.
Q43. The professional method is preserve the last proven protective order when atomic replacement cannot be guaranteed; the design does that.
Q44. Cold second, adversarial, board, Groq, GAI, deterministic tests, CI, and OCI read-only verification must all pass on the exact diff.

## Plain-English example

If the account holds four NVDA shares — Swing 1, QHM 2, F6 1 — pressing Ctrl-C leaves the Swing share's verified stop working and saves state. It does not send an unconditional one-share market sell that might race another stop and accidentally reduce QHM or F6. If the stop cannot be proved GTC, the bot refuses to stop running.

## Exact-diff review record

The first force-flatten implementation was rejected because an accepted market reducer with a lost response could race a replacement stop and sell protected QHM/F6 shares. The first stop-preserving revision was then rejected for treating hidden DAY stops, malformed acknowledgements, pending tracker rows, transitional orders, stale stop prices, unreadable prices, tracker/broker direction conflicts, already-breached orders, repeated interrupts, and incomplete multi-symbol results as proof. Each executable counterexample became a regression.

The final read-only proof received PASS from the independent execution-safety, broker-lifecycle, and masked-loss seats on the exact staged production bytes. Their final validations covered 167 focused tests plus 10 subtests, with the execution-safety seat independently running 317 focused tests plus 10 subtests. No reviewer found a remaining P0/P1 counterexample. External Groq and Google AI Studio preship verdicts, mechanical markers, CI, and OCI validation remain required after owner approval.

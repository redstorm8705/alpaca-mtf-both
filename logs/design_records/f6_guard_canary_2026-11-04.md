# F6 ownership-guard live prerequisite — 2026-11-04

**Author:** OpenAI Codex
**Status:** owner approved 2026-11-05; exact pre-deploy reconciliation PASS
Approval-ID: F6-GUARD-1104
Owner numbers: config.py OWNERSHIP_GUARD_ENFORCE=1; config.py FOREVER6_ENABLED=0
Covers: config.py, execution/broker.py, execution/ownership_guard.py,
  execution/risk_manager.py,
  research/f6_guard_activation_probe.py, tests/test_f6_guard_activation_probe.py,
  tests/test_broker_limit_sell_ownership.py,
  tests/test_broker_tier_safety.py, tests/test_f6_pending_buy_reservation.py,
  tests/test_stop_protection.py,
  logs/evidence/f6_guard_activation_2026-11-05.json,
  logs/design_records/f6_guard_canary_2026-11-04.md,
  logs/design_records/f6_live_activation_2026-11-04.md, handoff.md, logs/tb_audit_log.md

## Decision requested

Ship the ownership floor as a live paper-account prerequisite while F6 remains off:

1. Fix the limit-sell wrapper so a transient position-read failure reaches the existing ownership
   guard with `alpaca_net_qty=None`. A proven non-protected symbol preserves its exit under the
   guard's fail-open keystone; a protected, cached-protected, corrupt or unresolved symbol remains
   blocked. The wrapper never guesses quantity or protection.
2. Set `OWNERSHIP_GUARD_ENFORCE=True` while leaving `FOREVER6_ENABLED=False`.
3. Exercise the actual limit-order wrapper, full/partial/stop boundaries, canonical ledger, open
   orders, pending protected-buy journal and reduction serializer in a broker-hermetic activation
   probe. Persist the redacted result bound to the candidate source bytes and OCI paper-account
   read-only facts.

This is live enforcement, not shadow mode. It changes Day, Swing and QHM reducing-order boundaries
immediately. F6 remains unable to enter. After the guard deployment passes startup, position/stop
reconciliation and the activation probe, the separate `F6-LIVE-1104` package may enable F6 while
leaving the guard on.

## Prior work

- `logs/design_records/f6_golive_2026-09-26.md:70-81` orders ownership enforcement before F6.
- `logs/design_records/f6_shared_symbol_residual_stop_2026-10-12.md` owns verified Swing stop
  coverage when QHM/F6 shares coexist.
- `logs/design_records/f6_pending_buy_reservation_2026-10-12.md` owns the unresolved protected-buy
  reduction block.
- `logs/design_records/f6_full_path_simulation_2026-10-23.md` proves F6 routing while the production
  flag remains disabled; it does not validate an enabled production config blob.
- `logs/design_records/f6_crash_trigger_restoration_2026-10-28.md` owns the crash-only entry gate.
- `logs/design_records/dashboard_protection_truth_2026-10-22.md` owns broker-stop/dashboard truth.
- `logs/design_records/qhm_manual_add_adoption_2026-10-23.md` owns QHM manual-fill adoption and
  stop/ledger convergence on the same protected ownership floor.
- `logs/design_records/audit_triage_2026-10-16.md` records the earlier ownership and cross-tier
  reconciliation risks this canary must not regress.
- `logs/design_records/day_tier_v2_design_2026-08-29.md` defines the Day-tier exit and kill-switch
  contract whose attribution must remain exact when the ownership guard is active.
- `logs/design_records/f6_live_activation_2026-11-04.md` records the rejected simultaneous-flip
  design and its safe rollback correction; this design supersedes its launch sequence.
- `handoff.md` records F6 disabled and the shipped prerequisites.
- Open PRs #834/#804/#727 do not touch `config.py`, `execution/broker.py` or this test.

## Measured blocker

The masked-loss seat traced a reachable exit-loss interval in `execution/broker.py:752-770`.
With ownership enforcement on, every limit sell reads the broker position before consulting the
guard. A transient non-404 error returns `None` from the wrapper, even when the canonical ledger
proves the symbol has no QHM/F6 floor. Day after-hours exits call this wrapper after their prior stop
is terminal (`execution/day_trade_manager.py:2161-2204`), so the failure can leave a losing lot open.
The previous OCI probe called the guard directly and did not exercise this wrapper.

The correction does not swallow the broker failure. It passes `alpaca_net_qty=None` and side `sell`
to `check_never_sell_floor`. That function checks unresolved F6 intent first, then consults the
current ledger and last-known-good backup. It approves the requested quantity only when the symbol
is proven non-protected; protected or ambiguous state rejects. A successful broker read retains the
existing exact net/side path. Any guard exception still blocks.

The guard-on full suite also exposed that kill attribution treated enforcement itself as proof that
every measured symbol was exact. The corrected logic requires the measurement's own `exact=True`;
an unproven symbol stays inexact even when the guard is enabled, so it cannot masquerade as an
F6-eligible genuine-loss verdict.

## Activation and rollback

Deployment flips only `OWNERSHIP_GUARD_ENFORCE=True`; `FOREVER6_ENABLED=False` stays binding. Success
requires `DEPLOY_OK`, all services active, paper profile, clean startup GTC/position/pending-order
reconciliation, exact current broker/ledger claims, and the broker-hermetic activation artifact PASS.
The first live stop-protection and ledger-sync cycles must complete without a protected-floor breach
or stranded non-protected exit.

The first 2026-11-05 read-only OCI snapshot found one pre-deploy protected mismatch: the ledger
still carried one QHM GE share while Alpaca was flat GE. Ordinary reconciliation subsequently
removed that stale claim. The refreshed source-bound artifact reports `PASS` with
`reconciliation_status=EXACT`, 11/11 hermetic cases and zero live submissions, cancels,
replacements or closes. Rafael explicitly approved `F6-GUARD-1104` on 2026-11-05.

Rollback keeps F6 off and sets the guard back to `False` only if no F6 claim/reservation exists. QHM
already has protected claims, so an ordinary rollback of this live prerequisite requires a separately
reviewed QHM disposition or a code rollback that preserves equivalent QHM share bounds. A transient
runtime fault is handled by the guard's existing protected fail-closed/non-protected fail-open rules,
not by silently disabling enforcement.

## Required questions

Q1. Yes — this removes the final unsafe exit blocker before F6 can collect crash-event paper fills toward $25K; it also prevents another tier from selling QHM/F6 shares.
Q2. It does not enable F6 entries, but it can change whether Day/Swing/QHM reductions submit when ownership is protected or ambiguous; non-protected trade frequency is preserved on position-read failure.
Q3. Yes — the guard must exist for shared symbols; the simplest safe correction reuses its ledger/.bak keystone instead of adding a second protection classifier.
Q4. It activates the shipped ownership, residual-stop and reservation work; it supersedes the simultaneous-flip sequence in `f6_live_activation_2026-11-04.md` and does not overlap open PRs #834/#804/#727.
Q5. No threshold or multiplier is added; the only number change is an existing boolean activation, and quantity bounds derive from current broker/ledger state.
Q6. It exposes the static tier-name bridge (`swing` ledger ownership maps to broker tier `intraday`); a future canonical-tier adapter should remove that legacy naming seam after launch.
Q7. It connects broker limit sells, full/partial closes, stops, stop protection, ownership replay, pending protected buys, kill attribution and F6 launch readiness; every ambiguity remains explicit.
Q8. Yes — the blocker is traced to `broker.py:752-770` and the Day exit call order at `day_trade_manager.py:2161-2204`; the live direct-guard probe's blind spot is documented.
Q9. Front-loaded cases cover non-protected position-read failure, protected failure, co-held own-quantity sells, corrupt/unreadable ledger, pending reservations and ordinary successful reads; the live reversal criterion is any protected leak or stranded non-protected exit.
Q10. No validated edge is delayed by calibration; this prerequisite fixes a reachable execution failure before activating F6.
Q11. Yes — global exit bounding is risk-path and requires execution-board plus masked-loss review.
Q12. It preserves paper=True, all stops, the daily brake and F6 off; it changes only the ownership safety envelope by enforcing the already-reviewed floor.
Q13. Missing position data proceeds only for a proven non-protected symbol; protected, cached-protected, pending, corrupt or unresolved ownership blocks.
Q14. No shadow behavior: the guard becomes live immediately; the activation artifact is a pre-deploy proof, not a log-only rollout.
Q15. Broker refusals log symbol, tier and exact GuardResult reason; the activation artifact records each expected wrapper verdict without credentials or orders.
Q16. Startup/stop/ledger logs prove runtime; the first complete post-deploy cycle gets a post-live audit before the separate F6 activation.
Q17. Tests call the real decorated wrapper with a mocked broker; the OCI phase reads the real paper broker/ledger but never submits.
Q18. The prior “guard direct probe passed” conclusion was re-examined and rejected because it skipped the broker wrapper.
Q19. Yes — this changes exit behavior and the exact `F6-GUARD-1104` design requires Rafael's named approval before commit.
Q20. Neutral reviews must attempt both masked protected-share loss and stranded ordinary exits; board/Groq/GAI review exact bytes after implementation.
Q21. All tests replace the Alpaca client and assert broker submission counts; the read-only OCI probe never calls a submit/cancel endpoint.
Q22. The guard itself was probed against live broker/ledger; the corrected wrapper receives a broker-hermetic candidate-tree probe before deployment and a read-only OCI verification after deploy.
Q23. The guard activation gets its own source-bound proof; the existing F6 20/20 disabled artifact is retained as prior routing evidence, not misrepresented as activation-byte evidence.
Q24. N/A — no absence, tradability or ETF claim is made.
Q25. N/A — no stock or market statistic is used by this ownership/exit change.
Q26. T1 Alpaca supplies positions/orders; the canonical local ledger and `.bak` supply ownership fallback; missing protected evidence fails closed.
Q27. The guard flag is in `config.py`; the activation artifact is atomic redacted JSON; existing ownership/reservation journals retain fsync/lock behavior.
Q28. The wrapper runs inside the existing serialized symbol-order decorator immediately before request construction; upstream Day/Swing/QHM exit logic and downstream mocked/live broker submission are unchanged.
Q29. This is next because board review proved it is the immediate safety blocker to enabling the already-complete F6 route.
Q30. Professional execution requires testing the actual money-moving wrapper, not only its helper; the underweighted risk was a fail-closed guard stranding an unrelated losing exit.
Q31. N/A — no return model or parameter trial is introduced; this is execution correctness.
Q32. N/A — no historical market replay is used to justify the wrapper behavior.
Q33. N/A — no performance estimate is made; live broker rejection/latency remains observable in normal exit logs.
Q34. N/A — no alpha/beta claim is made.
Q35. Market regime does not alter the ownership invariant; high-volatility broker-read failure is explicitly the tested case.
Q36. Quantity reads and comparisons are whole-share/current-position bounded and remain suitable at $25K; no market-capacity assumption is added.
Q37. N/A — deterministic safety invariants do not require trade-level statistical power.
Q38. Non-working means a protected share can leak, a proven non-protected exit is blocked by position-read failure, or wrapper/artifact outcomes differ; owner disables new F6 work and repairs/replaces the guard path.
Q39. N/A — no entry sizing changes; exit quantity is bounded by exact owned shares and broker net when protected.
Q40. It owns “protected shares never leak and non-protected exits are not stranded by guard activation”; wrapper tests and the source-bound activation artifact machine-check both halves.
Q41. The best expert tests helper, wrapper, serializer and live-book inputs separately, deploys the guard before the strategy, and preserves a non-destructive rollback; this design does so.
Q42. It uses raw broker positions/orders, canonical ledger/.bak, pending journals and actual wrapper calls rather than dashboard summaries.
Q43. The professional method is a chokepoint invariant with failure-mode partitioning and canary activation before dependent strategy launch; this design follows it.
Q44. Independent board/masked-loss, cold/adversarial, Groq/GAI, CI, source-bound wrapper artifact and OCI read-only verification must agree before deployment.

Signed: **OpenAI Codex**

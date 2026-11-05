# Forever-6 live activation — 2026-11-04

**Author:** OpenAI Codex
**Status:** rejected/superseded by `F6-GUARD-1104`; simultaneous activation will not ship
Approval-ID: F6-LIVE-1104
Owner numbers:
Covers: config.py, logs/design_records/f6_live_activation_2026-11-04.md,
  handoff.md, logs/tb_audit_log.md

## Decision requested

Activate the already-built Forever-6 tier on the paper account by changing exactly two existing
configuration booleans in one risk-path diff:

1. `config.FOREVER6_ENABLED = True` so the shipped after-close F6 route may run.
2. `config.OWNERSHIP_GUARD_ENFORCE = True` so F6 and QHM protected shares are ring-fenced at every
   reducing-order boundary. `config.py:1107-1111` already refuses startup if F6 is enabled without
   this guard, so a one-flag activation is invalid and will not ship.

No trigger, universe, sizing, cash, stop, trim, kill-switch, product or timing value changes. F6
remains paper-only, cash-only, common-stock-only and crash-only. It may consider a starter only after
the completed 4:15 PM ET VIX observation is available, and only when both the completed SPY damage
gate and heightened-VIX gate pass. It never buys an ordinary single-stock dip.

## Prior work

- `logs/design_records/f6_golive_2026-09-26.md` declares that F6 and ownership enforcement activate
  together after the shared-symbol prerequisites and full-path proof.
- `logs/design_records/f6_startup_ownership_preflight_2026-10-12.md` ships the fail-closed startup
  ownership/readiness interlock.
- `logs/design_records/f6_shared_symbol_residual_stop_2026-10-12.md` preserves Swing residual stops
  on symbols co-held with protected QHM/F6 shares.
- `logs/design_records/f6_pending_buy_reservation_2026-10-12.md` protects accepted-but-not-yet-
  replayed F6 buys from concurrent reducers.
- `logs/design_records/f6_trim_resync_2026-10-17.md` owns terminal trim reconciliation and restart
  duplicate suppression.
- `logs/design_records/f6_bounded_entry_2026-10-17.md` owns bounded entry submission and truthful
  order-state handling.
- `logs/design_records/f6_scoped_allocator_2026-10-18.md` owns settled-cash allocation and emergency
  cash checks.
- `logs/design_records/f6_emergency_flatten_attribution_2026-10-18.md` preserves verified Swing
  protection during shutdown without selling protected shares.
- `logs/design_records/f6_etf_winddown_2026-10-21.md` removes ETFs and enforces common-stock-only
  classification at startup and order time.
- `logs/design_records/f6_full_path_simulation_2026-10-23.md` ships the production-path, source-bound
  20-scenario proof; its child process exercises the real F6 route with the imported flag enabled
  while tracked production remains disabled.
- `logs/design_records/f6_crash_trigger_restoration_2026-10-28.md` ships the final crash-only
  SPY-damage-plus-VIX classifier, allocator proof and broker-boundary revalidation.
- `logs/design_records/dashboard_protection_truth_2026-10-22.md` owns the dashboard's broker-stop
  and daily-brake truth boundaries; activation does not replace those reporting contracts.
- `handoff.md` records every deployed prerequisite and keeps activation as a separate owner decision.
- Open PRs #834, #804 and #727 do not touch F6 execution or `config.py`; #804 overlaps only handoff
  documentation and will be reconciled before push.

## Current-state measurements

- OCI is on `152de54`; `mtf-bot`, `mtf-writer` and `mtf-http` are all active; live
  `FOREVER6_ENABLED=False` and `OWNERSHIP_GUARD_ENFORCE=False` were read on 2026-11-04 PT.
- The committed source-bound artifact reports `overall=PASS`, 20/20 scenarios PASS and
  `production_flag_remained_disabled=true` (`logs/f6_full_path_simulation_latest.json`).
- The final replay covers 1,715 completed sessions: 25 qualifying crash observations, 20 affordable
  reference plans, A/B/C counts 7/12/6, and zero fires in 2021, 2023 and 2026
  (`logs/design_records/f6_crash_trigger_restoration_2026-10-28.md:247-254`).
- A read-only OCI ownership probe on 2026-11-04 PT returned zero source errors and zero residual
  shares for all six live positions: BRK.B 2 Swing, GOOGL 1 QHM, MSFT 1 Swing, NVDA 2 QHM, V 1
  Swing and WMT 5 Swing.
- The same read-only guard probe approved full QHM-owned exits for GOOGL 1 and NVDA 2, approved full
  non-protected Swing exits for BRK.B/MSFT/V/WMT, and rejected a foreign one-share intraday sell of
  both GOOGL and NVDA. No order was submitted, changed or cancelled.

## Exact activation and rollback

The reviewed implementation diff changes only the two booleans above plus this durable record,
handoff and audit log. Before merge it must pass config validation, the F6 focused suites, the
source-bound artifact check where applicable, cold-second, adversarial, execution-board,
masked-loss, Groq and Google AI Studio review on the exact bytes. Tests remain broker-hermetic.

Deployment uses a fast-forward pull and restarts the three OCI services. `main.py:783-805` performs
the blocking F6 ownership replay before the first `run_cycle` call at `main.py:1024`; a failed
preflight leaves F6 actions blocked. Success requires
`DEPLOY_OK`, all three services active, `profile=paper`, both booleans read back `True`, startup GTC,
position and pending-order reconciliation complete, and no startup error. The first enabled
after-close call must emit `change=f6-sim-1023 after-close shared route active`; absence reverses the
wiring claim. A non-crash session must submit zero F6 orders. The first qualifying event receives a
broker-fill/ledger/order-transcript post-live audit.

Rollback sets only `FOREVER6_ENABLED=False` followed by a restart. It prevents new F6 entries but
does not sell an existing F6 holding. `OWNERSHIP_GUARD_ENFORCE` must remain `True` while any QHM or
F6 protected claim exists; it may be disabled only in a separately reviewed change after the broker
and ledger prove no protected claim remains.

The after-close route does not depend on a broker-stream “EOF” signal, which Alpaca does not expose
as a settlement guarantee. It begins at 4:16 PM ET—at least 16 minutes after the regular close—and
the submission boundary re-reads fresh settled cash, broker positions, open orders, ownership and
the exact current trigger row under its cross-process lock. Any late fill that makes those sources
disagree blocks the F6 order rather than assuming quiescence.

The guard has an explicit runtime audit trail without inventing a new error-code protocol:
`execution/broker.py:2060-2064`, `2369-2372` and `2833-2837` log the symbol, calling tier and the
specific `GuardResult.reason` whenever a stop, trim, partial close or full close is rejected. The
2026-11-04 read-only probes returned `intraday has no sellable qty (own=0.0)` for foreign GOOGL and
NVDA sells; no broker call followed.

## Required questions

Q1. Yes — activation lets the fully tested crash buyer collect real paper fills toward $25K; the 20/20 production-path proof and 25 measured events justify activation rather than continued inactivity.
Q2. Yes — it changes F6 from zero trades to trades only on the measured joint SPY/VIX crash events; no Day, Swing or QHM entry count changes (`strategy/run_cycle.py:991-1001`).
Q3. Yes — the capability exists to buy dislocations; the simplest valid launch is the two required booleans because `config.py:1107-1111` rejects F6 without ownership enforcement.
Q4. It activates, rather than duplicates, the shipped records listed under Prior work; open PRs #834/#804/#727 do not touch F6 execution or `config.py`.
Q5. No new static number is added; activation exposes the already-reviewed dynamic completed-data classifier and its owner-approved historical crash map (`f6_crash_trigger_restoration_2026-10-28.md:65-104`).
Q6. The remaining static assumption is the fixed F6 stock universe (`config.py:421-422`); the forward pass is owner-approved universe replacement only after per-name thesis/quality evidence, not part of activation.
Q7. It connects the crash classifier, allocator, ownership ledger, broker admission, shared-symbol stops, kill attribution, Slack fills and post-live audit through the existing `run_f6_after_close` route (`execution/f6_cycle.py:47-108`).
Q8. Yes — readiness claims use the 20/20 artifact, 1,715-session replay and the 2026-11-04 zero-error live ownership/guard probe; no return claim is made.
Q9. Front-loaded proof covers 20 production-path scenarios plus 1,715 completed sessions; expected effect is zero ordinary-day orders and bounded A/B/C orders on qualifying crashes; either opposite result disproves it.
Q10. Yes — the execution path has passed while activation remains the sole reason it cannot collect paper evidence; waiting for a perfect live sample would prevent the sample from existing.
Q11. Yes — this changes trade frequency/exposure from zero and activates a global sell-floor chokepoint, so execution-board and masked-loss review are mandatory.
Q12. `paper=True` remains locked at `execution/broker.py:182-185`; trading-tier stops and the 7% brake remain; F6 is the documented never-sell tier (`CLAUDE.md:124`) and the enforced ownership floor prevents another tier from selling it.
Q13. Missing/stale/corrupt ownership, cash, trigger, quote, catalyst, order or broker evidence fails closed for F6/protected shares; non-protected exits retain the reviewed fail-open keystone (`execution/ownership_guard.py:696-793`).
Q14. No shadow mode is introduced; once enabled, the real after-close route acts when all live gates pass.
Q15. Yes — the route logs `change=f6-sim-1023`, trigger evidence stores both price/VIX legs and digests, and order/fill ownership uses tier-tagged durable journals.
Q16. `change=f6-sim-1023 after-close shared route active` proves invocation; the existing expectation record requires it after activation and the first qualifying event gets a fill/ledger replay.
Q17. The 20-scenario proof runs the production route, allocator and broker boundary; the read-only OCI probe verified the current broker/ledger ownership contract before activation.
Q18. No anomaly is dismissed; activation is blocked until the previously dormant global guard is explicitly reviewed with its current live-book outcomes.
Q19. Yes — activation changes entry routing and global exit bounding; Rafael must name `F6-LIVE-1104` before commit.
Q20. Neutral cold/adversarial prompts must test both false-blocked exits and protected-share leakage; exact bytes go to board, Groq and GAI before ship.
Q21. Every activation test uses mocks or the hermetic transport-refusing F6 simulator; the only OCI probe is read-only and submitted no order.
Q22. Yes — the new live control was called directly in read-only mode against the current broker and ledger; own/non-protected exits passed and foreign protected sells failed.
Q23. Yes — the committed connected artifact is 20/20 PASS; it exercises the imported enabled route in an isolated guarded child while production stays off.
Q24. N/A — activation makes no absence, tradability or ETF-availability claim; the existing order boundary classifies each candidate and rejects uncertain/leveraged/inverse instruments.
Q25. Yes — crash frequency/depth claims come from the 1,715-session replay, not memory.
Q26. Existing sources remain T1 Alpaca for SPY/broker truth and the approved T4 yfinance exception for VIX/VIX3M; missing or misaligned data sends no order.
Q27. The flag lives in `config.py`; decisions use fsynced JSON/JSONL trigger, allocator, reservation, ownership and post-live records with their existing atomic-write contracts.
Q28. `strategy/run_cycle.py:991-1001` invokes `execution/f6_cycle.py` after close; trims run first, then the crash-gated starter, then broker admission and canonical reconciliation.
Q29. This is the highest-value remaining F6 item because every prerequisite is deployed and the disabled flag is the only reason the tier cannot collect crash-event paper evidence.
Q30. A professional launch requires independent execution controls, source-bound simulation, reconciled ownership, idempotent order lifecycle and live rollback; all are present, while the global guard's false-block risk is the chief residual risk.
Q31. Activation itself has no fitted return model; the classifier disclosed all 25 events and its 20 affordable plans, while the launch size remains bounded because no statistically precise expectancy is claimed.
Q32. The replay uses completed, date-aligned SPY/VIX/VIX3M observations and strictly prior averages; it does not select stocks based on future membership (`f6_crash_trigger_restoration_2026-10-28.md:65-104`).
Q33. The launch proof includes accepted, partial, filled, rejected and ambiguous order states; no performance backtest net of costs is claimed, and Alpaca paper fills will supply live execution evidence.
Q34. F6 intentionally buys long equity beta during broad-market crashes; no residual alpha claim is made.
Q35. The measured gate targets high-VIX selloffs, produced zero fires in calm 2021/2023/2026, and bounds event cash at 10%/20%/30%; shared-symbol ownership tests cover book interaction.
Q36. The eight-name universe is highly liquid relative to a $25K account, and each order is whole-share, quote-live, settled-cash bounded and broker-admitted; no thin-name capacity claim is needed.
Q37. Twenty affordable historical reference plans are insufficient for a tight expectancy interval, so activation uses bounded paper size and a predefined first-event audit rather than claiming significance.
Q38. Non-working means an enabled qualifying event fails to invoke, submits outside its tier/cash bound, violates ownership, or materially diverges from replay; the owner then disables F6 entries while leaving ownership enforcement on for protected claims and replaces the failing component.
Q39. Size follows the pre-existing crash-depth cash ladder rather than win-rate Kelly because no reliable F6 win-rate estimate exists; fresh settled cash is recomputed at the order boundary.
Q40. It owns the outcome “F6 can buy only during a verified crash without selling another tier”; runtime tag, zero-order ordinary-day check and broker/ledger transcript are machine-evaluated result checks.
Q41. The best expert would run a reconciled live-book preflight, activate with exact rollback, and audit the first event end to end; this design requires all three.
Q42. It uses raw completed bars, live broker positions/orders/fills, canonical ledger claims and durable decision rows rather than dashboard summaries.
Q43. The professional method is a controlled production activation after source-bound simulation, independent risk review, reconciled ownership and deterministic rollback; this design follows it.
Q44. Independent board/masked-loss, cold/adversarial, Groq/GAI, CI, the 20/20 artifact and current OCI read-only broker/ledger probe must all agree before activation ships.

Signed: **OpenAI Codex**

## Reviewer counter-evidence

- Codex correctly rejected the first rollback because disabling ownership enforcement while retaining
  an F6 holding would remove its floor. The revised rollback disables only new F6 entries and leaves
  the guard on until a separately reviewed zero-protected-claim proof.
- Google AI Studio requested a blocking ownership reconcile before the route can run. The existing
  implementation provides that behavior under a different name: `main.py:783-805` synchronously
  calls `ForeverHoldManager.reconcile_on_startup()` and records the ready interlock before the first
  `run_cycle` at `main.py:1024`; `execute_starter` independently refuses when that interlock is not
  ready (`execution/forever_hold_manager.py:938-957`).
- Google AI Studio requested a stream “EOF/IDLE” settlement signal. Alpaca exposes no such close-of-
  stream settlement contract, so treating one as proof would be unsafe. The shipped boundary instead
  waits until 4:16 PM ET and re-reads broker positions, open orders, settled cash, ownership and exact
  trigger evidence under the F6 lock immediately before submission; inconsistency blocks the order.
- Google AI Studio requested an invented `ERR_OWNERSHIP_GUARD_PROTECTED` code. The existing broker
  boundary already logs a more informative structured fact: symbol, tier, rejected action and exact
  `GuardResult.reason` (`execution/broker.py:2060-2064,2369-2372,2833-2837`). The read-only live probe
  produced `intraday has no sellable qty (own=0.0)` for GOOGL and NVDA and submitted no order.
- On counter-review, Google AI Studio asked for a last-moment broker/ledger double-check. That is
  already inside the same `symbol_order_gate` plus `durable_evidence_locked` section that reaches
  submission: `live_admit` re-reads account, positions and orders (`tier_capital_allocator.py:626-648`),
  then the manager re-reads open orders, ledger ownership, cash and session before reserving intent
  and calling the broker (`forever_hold_manager.py:1218-1391`). A late fill creates a positions/ledger
  or order-state inconsistency and fails closed.
- Google AI Studio also proposed a 60-second freshness rule for VIX/VIX3M. These inputs are completed
  daily index bars, not intraday quotes, so a 60-second quote-age test would falsely reject every
  legitimate completed bar. The classifier instead requires the expected completed session date,
  all 21 VIX dates aligned to SPY, the current VIX3M session present, and evaluation no earlier than
  4:16 PM ET (`f6_empirical_trigger.py:180-218`; `forever_hold_manager.py:90-94,598-613`).

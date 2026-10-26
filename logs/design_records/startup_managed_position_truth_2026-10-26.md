# Startup managed-position truth — 2026-10-26

Approval-ID: SWING-START-1026
Covers: main.py, execution/swing_breakout_manager.py, tests/test_startup_managed_position_truth.py, logs/design_records/startup_managed_position_truth_2026-10-26.md, logs/post_live/expect/swing-startup-truth-1026.json, handoff.md, logs/tb_audit_log.md
Author: OpenAI Codex

## Problem and measured evidence

OCI startup at `2026-10-26 05:34:37.922 UTC` emitted `P0-STARTUP: In Alpaca but NOT in tracker: ['AAPL', 'MSFT']` and claimed their stops/targets would not fire. A read-only broker/state probe in the same deployed process proved the opposite: `swing_breakout.json` holds AAPL and MSFT as `open`; the ownership ledger assigns one Swing share to each; Alpaca holds one share of each; and live Swing-tagged GTC stops cover AAPL 1 @ $328.70 and MSFT 1 @ $497.10. The cause is the final legacy check in `main.py:857-865`, which compares Alpaca only with `tracker.open_trades`; the dedicated Swing manager deliberately lives outside that tracker and is already excluded from orphan adoption by `execution/orphan_manager.py:1228-1238`.

## Design

At startup, build the expected managed trading-symbol set from the legacy tracker plus the dedicated Day and Swing managers, limited to symbols Alpaca actually reports. A new checked Swing-state reader returns only confirmed holdings plus an explicit readability result; the configured candidate-pool fallback remains confined to orphan prevention and can never masquerade as proof of ownership. Continue using Alpaca's non-QHM live count as the risk counter exactly as today. If the count correction is fully explained by a dedicated manager, log an informational reconciliation instead of a P0. Preserve P0 severity for a broker symbol absent from every manager, preserve the existing stale-legacy-tracker warning, and page when Swing ownership state is unreadable. This changes startup classification and wording only; it does not submit, cancel, replace, size, route, or exit an order.

Machine checks will cover: dedicated Swing holdings are recognized; dedicated Day holdings are recognized; an actually unmanaged broker symbol still produces the P0 set; a stale legacy tracker row remains visible; and the risk counter still converges to Alpaca's live non-QHM count.

## Prior work

- `logs/design_records/swing_owner_design_2026-10-25.md` establishes that current Swing lots live in `data/state/swing_breakout.json` and are managed by `execution/swing_breakout_manager.py`.
- `logs/design_records/dashboard_protection_truth_2026-10-22.md` joins dedicated tier lots to live broker stops and already showed AAPL/MSFT as fully protected.
- `execution/orphan_manager.py:1328-1333` already excludes QHM, F6, Day, and dedicated Swing holdings from legacy orphan adoption.
- `logs/design_records/qhm_manual_add_adoption_2026-10-23.md` is the immediately preceding QHM ownership reconciliation shipment and measured the same live five-position account.
- `handoff.md` names this work as parallel to the approved F6 simulation and records AAPL/MSFT as protected Swing positions.
- `logs/design_records/unmanaged_position_alarm_2026-10-12.md` defines the broker-versus-tier unmanaged-position invariant this startup check reports.
- `logs/design_records/f6_etf_winddown_2026-10-21.md` documents F6's separate ownership state and the requirement not to misroute it through a trading-tier manager.
- `logs/design_records/f6_emergency_flatten_attribution_2026-10-18.md` defines tier-attributed Swing stop proof during shutdown.
- `logs/design_records/dashboard_held_stop_status_2026-10-23.md` establishes broker lifecycle truth for live stops used in the dashboard reconciliation.
- `logs/design_records/audit_triage_2026-10-16.md` requires reconciliation findings to distinguish ownership-state defects from reporting defects.
- `logs/design_records/co_audit_2026-10-16.md` identifies cross-tier state joins as a recurring anti-silo obligation.

## Required questions

Q1. Yes — it restores truthful protection telemetry so operator attention stays on real gaps; OCI measured two false P0 symbols while both had full broker protection.
Q2. No trade count changes; the order and entry paths are untouched (`main.py:823-876` is startup reconciliation/observability).
Q3. Yes; the simplest safe fix is to teach the final startup check about the dedicated managers that orphan reconciliation already recognizes (`execution/orphan_manager.py:1328-1333`).
Q4. It complements SWING-OWNER-1025 and the existing orphan exclusions; `gh pr list` on 2026-10-26 showed no parallel implementation PR.
Q5. No trading threshold or multiplier is added; membership comes from current durable manager state and current Alpaca positions.
Q6. It exposes the static assumption that all trading lots live in `tracker.open_trades`; the forward pass is one canonical four-tier startup ownership snapshot.
Q7. It connects broker startup truth to Day and Swing manager state; the join is read-only, fails without mutating orders, and is independently testable.
Q8. Yes — the 05:34:37.922 UTC P0 log, current state rows, ledger quantities, and live broker stops were measured directly.
Q9. N/A — no strategy or performance effect; deterministic fixtures replay managed Swing, managed Day, unmanaged, and stale-tracker cases.
Q10. N/A — no edge is enabled or withheld.
Q11. No; size, frequency, concurrency, and exposure remain unchanged, and Alpaca still sets the same non-QHM risk count.
Q12. No safety-envelope control changes; existing stops, 7% brake, paper mode, and data-source rules are untouched.
Q13. A dedicated-manager read failure cannot create broker mutations; truly unmatched Alpaca symbols remain P0 and the managers retain their own fail-closed behavior.
Q14. No shadow behavior; corrected startup classification is live immediately.
Q15. Yes — startup logs state whether a counter correction is manager-explained or contains an unmanaged symbol.
Q16. The post-ship proof line is `P0-STARTUP: dedicated tier positions accounted for`; the first restart is checked against broker/state truth.
Q17. Restart on OCI, then verify AAPL/MSFT no longer appear in the unmanaged P0 while their exact live GTC stops remain unchanged.
Q18. No — the warning was traced against raw state, ledger, positions, and orders rather than dismissed as expected noise.
Q19. No entry, exit, sizing, strategy routing, or gate rule changes; exact-design owner approval is not required for this read-only classification repair.
Q20. Yes — the claim is limited to measured false classification; exact production logs and raw broker facts were reviewed before design.
Q21. Tests mock all manager and broker inputs; no test can reach Alpaca.
Q22. N/A — this is not a new fail-closed trading control.
Q23. A full startup-path fixture must pass before readiness; deployed runtime proof must then match live manager and broker state.
Q24. N/A — no absence, tradability, or product claim is made.
Q25. N/A — no market statistic is used.
Q26. T1 Alpaca supplies live positions; local durable Day/Swing state supplies ownership; unreadable dependencies retain existing manager failure behavior.
Q27. No new state file is written; only the existing application log receives corrected startup messages.
Q28. The join runs after legacy, QHM, and dedicated-manager startup reconciliation and before entry scanning (`main.py:775-886`).
Q29. It is P0 ahead of F6 simulation because the current alert falsely says protected positions have no working exits; the fix is narrow and measured.
Q30. A professional control plane reconciles broker inventory against every strategy book, not one legacy book; the blind spot is adding future tiers without extending the inventory join.
Q31. N/A — no return forecast or strategy variant is evaluated.
Q32. N/A — no price-bar replay is used; all fixtures are state-machine cases.
Q33. N/A — no execution or expected-return claim is made.
Q34. N/A — no alpha claim is made.
Q35. N/A — no market-regime behavior changes.
Q36. N/A — no capacity or liquidity behavior changes.
Q37. N/A — this is a deterministic reconciliation invariant, not a statistical edge.
Q38. N/A — no strategy lifecycle changes.
Q39. N/A — no sizing logic changes.
Q40. It protects truthful startup ownership/protection status; a machine check proves every live dedicated Swing/Day symbol is classified managed while a real orphan remains P0.
Q41. The expert extension is a canonical four-tier inventory reconciliation object; this patch records that forward pass without widening the current repair.
Q42. It uses raw Alpaca positions, raw open orders for runtime proof, manager state files, and the canonical ownership ledger rather than dashboard summaries.
Q43. Yes — the professional method is position-level reconciliation between broker inventory and each strategy sub-ledger with explicit unmatched sets.
Q44. Cold review, adversarial review, external reviewers, CI, and an OCI restart against live Alpaca/state evidence independently prove the result.

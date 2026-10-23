# Dashboard held OCO-stop truth — 2026-10-23

Approval-ID: DASH-HELD-1023

Covers: generate_dashboard.py, tests/test_dashboard_protection_truth.py, logs/design_records/dashboard_held_stop_status_2026-10-23.md, handoff.md, logs/tb_audit_log.md

Signed: OpenAI Codex

## Decision

Treat an Alpaca `held` child stop as resting protection in the read-only dashboard only when its tagged owner is an active OCO exit group. Alpaca's live AMZN short OCO at 2026-10-23 08:36 PT contained a six-share BUY stop child at $250.25 with status `held`, parent `DT-AMZN-b-1792762826715-a815daf7` in `new` state; `generate_dashboard.py:396` accepted only `accepted`, `new`, and `partially_filled`, so the page falsely rendered `MISSING`. The fix carries the parent order class and status into each nested child, then accepts a held stop only for an `oco` parent in `accepted`, `new`, or `partially_filled`. A held bracket-entry child, unknown parent, or canceling OCO remains non-protective. Tier identity still must come from the tagged parent, type must be stop/stop-limit/trailing-stop, price and remaining quantity must be valid, side must reduce the live position, and the order page must be complete. Regressions cover the observed short OCO, unfilled bracket entry, and `pending_cancel` OCO. No order, risk, strategy, sizing, or broker code changes.

## Prior work

- `logs/design_records/dashboard_protection_truth_2026-10-22.md` owns the broker-native stop display and already covers nested child tier inheritance.
- PR #741 shipped that read-only protection table; PR #742 recorded deployment.
- `logs/design_records/audit_broker_ground_truth_2026-09-24.md` treats broker-native resting stops as the authoritative protection source.
- `logs/design_records/report_single_source_of_truth_2026-08-20.md` requires one broker-grounded reporting source rather than inferred protection.
- `logs/design_records/f6_etf_winddown_2026-10-21.md` is unrelated F6 product work; its position-state reporting remains unchanged.
- `logs/design_records/audit_triage_2026-10-16.md` requires deterministic severity from current broker facts rather than prose.
- `handoff.md` and `logs/tb_audit_log.md` are updated only after ship.
- Open PR #727 is documentation-only and does not touch the covered code.

## Required questions

Q1. Yes — truthful protection data prevents an unnecessary manual order on the PAPER account; live evidence is the AMZN $250.25 stop above.
Q2. No trades change; this is read-only HTML classification.
Q3. Yes; preserving the OCO parent's class and live status while parsing its held child is the smallest bounded fix.
Q4. It extends DASH-PROTECT-1022/PR #741 and does not duplicate open PR #727.
Q5. No fixed trading number is added; `held` is a broker state enum.
Q6. It exposes the static assumption that every working protective child reports `new`; future adapters should normalize broker lifecycle states centrally.
Q7. It connects nested broker order state to ownership-backed dashboard protection without affecting trading paths.
Q8. Measured: OCI returned AMZN stop child status `held`, qty 6, stop $250.25 while the served page showed `MISSING`.
Q9. Deterministic active-OCO, unfilled-bracket, canceling-OCO, wrong-side, partial, malformed and unavailable fixtures disprove the fix if any false protection appears.
Q10. N/A — no edge or calibration is involved.
Q11. No size, frequency, concurrency or exposure changes.
Q12. It reports the every-position-stop rule but cannot alter a stop or the 7% brake.
Q13. Missing/malformed/incomplete orders remain unavailable or missing; held children fail closed unless an active OCO parent is proven.
Q14. No shadow path; the corrected status renders live after deployment.
Q15. The row continues to show tier, stop, quantity, coverage, distance and next action.
Q16. The served AMZN row must show Day protected at $250.25 after deployment.
Q17. Runtime proof compares the live Alpaca child order with served dashboard HTML.
Q18. The missing label was investigated rather than accepted as designed.
Q19. No entry, exit, sizing, routing or gate code changes; owner requested the dashboard correction directly.
Q20. Review prompts will ask for false-positive protection and lifecycle gaps without suggesting a verdict.
Q21. Tests use dictionaries and cannot reach Alpaca; the separate OCI probe was read-only.
Q22. N/A — no new live control.
Q23. The broker-shaped nested short fixture passes through the same `_position_protection` function used by HTML.
Q24. The stop's existence and status were checked directly on Alpaca in this turn.
Q25. No stock or market statistic is used; the only numeric facts are the live broker's exact stop price and quantity.
Q26. T1 Alpaca paper order API; failure remains explicit unavailable.
Q27. Existing dashboard output stays atomically written; no new state file.
Q28. The change sits only in dashboard generation after Alpaca and ownership reads.
Q29. This is P0 operator truth because the page currently recommends placing a duplicate stop.
Q30. Professional consoles normalize broker lifecycle states while retaining exact identity and coverage checks; this adds the missing state.
Q31. No model, parameter trial, Sharpe or backtest exists in this read-only broker-state correction, so walk-forward testing does not apply.
Q32. N/A — point-in-time broker data only.
Q33. N/A — no fill or performance simulation.
Q34. N/A — no alpha/beta claim.
Q35. Broker lifecycle classification is regime-independent.
Q36. Six AMZN shares and a $25K account use the same bounded current-order join.
Q37. N/A — deterministic correctness, not inference.
Q38. No strategy is added or monitored for decay; a broker enum mapping is replaced only if Alpaca changes its documented/runtime lifecycle.
Q39. Position sizing is unchanged because the dashboard consumes already-submitted order quantities and cannot submit orders.
Q40. The machine-owned outcome is that an exact held reducing stop under an active OCO counts as protection and every other held child does not.
Q41. An expert also tests short direction, parent identity/state, remaining quantity, unfilled bracket entries, cancel races and malformed held orders; the regressions include them.
Q42. Raw nested Alpaca order and ownership ledger are used directly.
Q43. The professional method is broker-state normalization plus strict ownership/side/quantity reconciliation.
Q44. Cold/adversarial review, Groq, GAI, focused tests, CI and live broker/page comparison provide independent evidence.

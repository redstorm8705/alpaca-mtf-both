# F6 ETF wind-down closure — 2026-10-21

Approval-ID: F6-WIND-1021

**Status:** OWNER APPROVED 2026-10-21 — implementation complete; F6 remains disabled.

Covers: config.py,
execution/instrument_policy.py,
execution/forever_hold_manager.py,
execution/swing_breakout_manager.py,
tests/test_f6_instrument_policy.py,
tests/test_f6_startup_reconcile.py,
tests/test_f6_bounded_entry.py,
tests/test_swing_breakout_manager.py,
logs/post_live/expect/f6-etf-winddown.json,
logs/design_records/f6_etf_winddown_2026-10-21.md,
handoff.md,
logs/tb_audit_log.md

Signed: OpenAI Codex

## Decision

F6 will own individual common stocks only. It will refuse every ETF, including ordinary unleveraged
ETFs. DRAM and EWY will be removed from `FOREVER6_UNIVERSE`. The existing instrument policy will gain
an F6-specific `etf_not_admitted` result, and both universe validation and the final money-moving
boundary must enforce it. A runtime override cannot put an ETF back into F6. F6 remains disabled.

Removing DRAM/EWY from the F6 buy universe must not make them newly tradable by Swing. A separate
`SWING_BREAKOUT_F6_RESERVED_SYMBOLS` set will preserve the same ten-symbol Swing exclusion currently
created indirectly by `FOREVER6_UNIVERSE`; Swing will exclude the union of that set and the active F6
universe. DRAM/EWY also remain an immutable code-level exclusion floor, so a runtime override cannot
release the retired ETFs into Swing by replacing both configurable lists. This is a behavior-preserving
decoupling. Swing normalizes runtime symbol text with the same trim-and-uppercase convention used by
F6 configuration validation, and tests prove DRAM/EWY plus a whitespace-padded runtime F6 addition remain refused.

Startup reconciliation will independently inspect every positive canonical F6 ledger claim and every
open F6-tagged order. If its symbol is an ETF or its classification is missing, startup remains blocked,
the durable degraded interlock stays set, and Slack names the symbol and reason. The bot will never
delete, sell, or reattribute such a lot automatically. Activation evidence must prove zero F6 ETF
claims and zero F6 ETF orders at Alpaca and in the canonical ledger.

This replaces the first design, which proposed treating an empty Alpaca corporate-action response as
proof that an ETF had no announced closure. Live counter-evidence disproved that assumption. Alpaca's
corporate-action endpoint did not return the already-public closure for any of ten known 2026 closing
funds. Seven were still `active/tradable` after their sponsors had published a liquidation date. BWOW
and AELV became inactive only after their last trading dates. Therefore the available broker sources
cannot prove an ETF safe before the last liquid exit window. Fail-closed means the bot must not buy an
instrument class whose terminal event it cannot autonomously detect in time.

This is a direct live control, not a shadow rollout. There is no F6-held lot to unwind because F6 is
still off. If Rafael later wants an ETF in F6, it requires a separate approved design with a measured
authoritative sponsor/exchange/SEC feed that detects closures before the last trading day, plus an
approved tier-bounded liquidation path for an already-held fund.

## Measured evidence

- `config.py:420-428` proves F6 is off and DRAM/EWY are the two ETF candidates.
- `execution/instrument_policy.py:38-45` classifies DRAM/EWY as `unleveraged_etf`; its current live
  asset check proves only current identity/status/tradability.
- 2026-10-21 OCI raw Alpaca probe queried each symbol from the sponsor announcement date through
  `9999-12-31`. The endpoint returned HTTP 200 but no terminal action for BWOW, DVAL, USFI, MULT,
  DINE, LQ, NXTI, PCR, TESL, or AELV. DVAL/USFI/MULT returned only routine cash dividends.
- At the same time Alpaca reported DVAL, USFI, MULT, DINE, LQ, NXTI, PCR, and TESL
  `active/tradable=True`, despite official sponsor notices naming future last-trading and liquidation
  dates. BWOW and AELV were already `inactive/tradable=False`, but their official last-trading dates
  were 2026-10-14 and 2026-10-15 respectively, so that signal arrived too late for an orderly exit.
- Official notices used as ground truth: Bitwise BWOW (announced 2026-09-10; last trade 2026-10-14;
  liquidation 2026-10-22), Franklin DVAL/USFI/MULT (announced 2026-09-08; last trade 2026-10-27;
  liquidation 2026-11-03), Simplify DINE/LQ/NXTI/PCR/TESL (announced 2026-10-08; last trade
  2026-10-30; liquidation 2026-11-06), and Harbor AELV (announced 2026-09-18; last trade 2026-10-15;
  liquidation 2026-10-22).
- Board/masked-loss review rejected buy-only checks because an owned fund could become terminal after
  purchase and because the proposed data source failed the known-event replay. This revision removes
  that masked-loss path rather than claiming an unmeasured detector works.

## Machine-owned result

The expectation evaluator will prove: (a) DRAM and EWY are absent from the configured F6 universe but
remain in Swing's explicit reserved-symbol set and immutable retired-ETF floor;
(b) every ETF classification, including a runtime override, is rejected at universe validation and
again at the last order boundary; (c) all eight remaining common-stock symbols pass the ETF invariant;
(d) an unknown or malformed instrument classification fails closed; (e) a positive F6 ETF claim or
open F6 ETF order keeps startup blocked without mutation; (f) Swing still refuses DRAM/EWY; and (g) no
rejected path calls the broker order submitter. The full-path pre-market simulation must inject an ETF candidate and observe
zero F6 reservations and zero F6 orders.

## Reversal criterion

Do not restore an ETF because Alpaca says it is currently active. Restoration requires a historical
known-closure replay showing an authoritative automated source detected every tested closure before its
last trading day, plus an owner-approved response for held shares. Any missed known closure fails the
source.

## Prior work

- `handoff.md` identifies ETF wind-down closure as the prerequisite immediately before the full-path F6 simulation.
- `logs/design_records/f6_trim_resync_2026-10-17.md` keeps F6 trim reconciliation tier-bounded; this change does not alter trim execution.
- `logs/design_records/qhm_earnings_trim_ledger_autoconfirm_GATE_RESULT_2026-08-16.md` records the earlier fail-closed ownership lesson applied to F6 startup claims here.
- `logs/design_records/p04b_limits_replay_2026-09-26.md` is the related limit/order replay evidence; this change preserves its execution bounds.
- `logs/design_records/owner_approval_gates_2026-10-17.md` defines the exact Approval-ID process used for `F6-WIND-1021`.
- `logs/design_records/co_audit_and_anomaly_2026-10-09.md` identifies disconnected tier logic; the Swing/F6 union prevents that class of product-routing drift.
- `logs/design_records/c2_swing_breakout_tier_2026-09-29.md` owns Swing admission; this design preserves its F6-reserved exclusions.
- `logs/design_records/audit_triage_2026-10-16.md` is the current architectural triage context for closing F6 launch prerequisites.
- `logs/design_records/f6_golive_2026-09-26.md` names ETF wind-down handling as a launch prerequisite.
- `logs/design_records/f6_instrument_and_kill_policy_2026-10-15.md` prohibits leveraged/inverse ETFs
  and created the fail-closed classification boundary; this revision makes F6 stricter without changing
  Day, Swing, or QHM eligibility.
- `logs/design_records/f6_bounded_entry_2026-10-17.md` owns the bounded starter order path.
- `logs/design_records/f6_scoped_allocator_2026-10-18.md` owns live capital admission.
- `logs/design_records/f6_emergency_flatten_attribution_2026-10-18.md` preserves Swing stops during
  explicit shutdown and leaves this prerequisite next.
- The superseded first revision of this record proposed an Alpaca corporate-action allow gate. Raw
  known-closure replay and two independent board seats rejected it; no code was built or committed.
- Open PR inventory and `origin/main` contain no parallel implementation of this control.

## Required questions

Q1. Yes — it closes the measured forced-liquidation blind spot while preserving eight common-stock F6 candidates.
Q2. It removes two of ten configured candidates (DRAM/EWY); live broker evidence shows the alternative detector missed 10/10 known closures.
Q3. Yes — a two-boundary product-class invariant is simpler and safer than an unproved sponsor scraper or late broker status.
Q4. Prior records classify products and prohibit leveraged ETFs, but none prohibits ordinary ETFs after the closure-source replay failed.
Q5. No numeric trading threshold is added; ETF identity is a categorical product invariant enforced twice.
Q6. It disproves the assumption that `active/tradable` or empty corporate actions means an ETF is closure-free; the next forward pass is the F6 full-path simulation.
Q7. It connects classification to F6 universe/startup/final submission and decouples Swing's reserved list while mechanically preserving Swing behavior; Day, QHM, scoring, sizing, and exits are unchanged.
Q8. The OCI 10-symbol replay and official sponsor dates above are the measured evidence; the earlier empty-response interpretation is explicitly withdrawn.
Q9. ETF, runtime-override ETF, malformed identity, common stock, and broker-submission spy cases make wrong admission mechanically visible.
Q10. N/A — this is a product-safety invariant, not a fitted return edge.
Q11. It reduces opportunity breadth by 20% of the configured universe but cannot increase exposure; it changes entry routing and requires exact owner approval.
Q12. It preserves paper=True, F6 cash-only controls, trading-tier stops, the daily trading brake, and F6 disabled state.
Q13. Any ETF or unreadable classification blocks F6 startup and order submission; existing F6 ETF claims/orders remain untouched and require operator resolution.
Q14. No — the production universe and last order boundary both refuse the product directly.
Q15. Yes — refusal logs symbol, canonical product class, policy result, and source; configured, claimed, or ordered ETF presence fails startup validation.
Q16. `[F6] instrument refused ... reason=etf_not_admitted` plus zero reservation/order calls proves execution.
Q17. OCI will run exact classification for all current universe names and the full-path simulator will inject an ETF through the order boundary without submission.
Q18. No — the live ten-fund replay specifically attacks the prior active/tradable and corporate-action assumptions.
Q19. Yes — removing DRAM/EWY and tightening F6 admission changes entry routing, so Rafael must approve `F6-WIND-1021` before commit.
Q20. The record retains the failed design, contrary evidence, official dates, opportunity cost, and board objections instead of presenting reassurance.
Q21. Tests inject every product classification, positive ETF claims/open orders, and Swing DRAM/EWY candidates; they assert no F6 reservation/broker call and no accidental Swing admission.
Q22. Yes — raw OCI Alpaca responses were measured against ten official sponsor closure notices and missed terminal actions in all ten.
Q23. No readiness claim is allowed until the separate full-path pre-market simulation passes; deployed remains different from ready.
Q24. Yes — current Alpaca asset and corporate-action endpoints were queried this turn for DRAM/EWY and ten known closure symbols.
Q25. N/A — no volatility, return, or typical-move claim is made.
Q26. Alpaca asset identity is T1 for product classification; because its lifecycle evidence failed replay, it cannot authorize any F6 ETF.
Q27. The policy is deterministic; startup failures persist through the existing atomic sync-degraded interlock, and no new lifecycle cache is introduced.
Q28. Universe validation rejects configuration, startup scans canonical F6 claims/open orders, and the final F6 order path independently reclassifies before reservation/submission.
Q29. It resolves the handoff's ETF prerequisite with measured fail-closed scope before full-path simulation and enablement review.
Q30. Professional long-horizon controls require proven lifecycle data and a held-lot response; until both exist, exclusion is the defensible security-master policy.
Q31. N/A — no Sharpe, parameter search, or return optimization is involved; deterministic product matrices replace inference.
Q32. Product identity is read live and no historical constituent or price sample is used, so this control adds no look-ahead or survivorship claim.
Q33. N/A — rejected orders incur no fill assumption; admitted common-stock orders retain existing quote, cash, and execution controls.
Q34. It removes two thematic/country ETF exposures; no claim is made that the remaining book is market- or sector-neutral.
Q35. The product invariant is regime-independent and removes the terminal sponsor-liquidation tail rather than estimating correlation.
Q36. At $25K it lowers breadth from ten candidates to eight; remaining liquidity checks and sizing are unchanged.
Q37. N/A — no trade sample estimates this categorical invariant; one missed known closure invalidates a lifecycle source.
Q38. The known-closure replay is the measurable source test; a future restoration must pass it rather than rely on an empty response.
Q39. N/A — sizing and allocator logic are unchanged; only eligible product class changes.
Q40. It protects “F6 never owns or sends an ETF unnoticed”; universe, startup claim/order, override, final-boundary, Swing-preservation, and no-submit tests own that outcome.
Q41. A fund-operations expert would require a security master, sponsor/exchange/SEC lifecycle feed, and controlled held-lot liquidation; the unavailable parts remain explicit prerequisites for restoration.
Q42. It uses raw Alpaca asset JSON for product identity and raw official sponsor notices for the source replay, not a model summary.
Q43. The professional fail-closed method is to exclude a product when lifecycle control cannot be proved before loss of liquidity; the design enforces that at configuration and order time.
Q44. Independent cold/adversarial reviewers, two board seats, Groq, GAI, OCI classification output, and the full-path simulator must all agree before readiness.

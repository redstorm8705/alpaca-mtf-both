# Dashboard protection truth — 2026-10-22

Approval-ID: DASH-PROTECT-1022

Covers: generate_dashboard.py, reporting/html_ui.py, execution/risk_manager.py, tests/test_dashboard_protection_truth.py, logs/design_records/dashboard_protection_truth_2026-10-22.md

**Author:** OpenAI Codex
**Status:** preview requested; no production commit or deployment before Rafael approves this exact design.

## Problem and measured evidence

The dashboard is presenting three different kinds of operator misinformation:

1. Position-tier badges repeat share quantities already shown in the Qty column (`reporting/html_ui.py:75-91`; screenshot 2026-10-22 09:23 PT shows `Swing 1`, `Day 4`, and `QHM 2`).
2. The Alpaca loader discards `stop_price`, `limit_price`, `client_order_id`, and order-class facts (`generate_dashboard.py:251-328`), while the position table reads only `trade_log.json` or QHM state (`generate_dashboard.py:640-734`). A 2026-10-22 read-only broker probe found live stops for AAPL `$328.70`, MSFT `$497.10`, GE `$272.71`, GOOGL `$288.97`, NVDA `$192.41`, and AVGO `$326.11`, even though the dashboard rendered `—` for AAPL, MSFT, and AVGO.
3. The dashboard imports the module default 3% limit (`config.py:229`) because the separate writer does not apply the paper profile, whose real limit is 7% (`config.py:320`). It also uses whole-account `equity-last_equity` (`generate_dashboard.py:481-488`) instead of the running kill switch's tier-aware fills/position measure (`execution/risk_manager.py:785-1073`). At 2026-10-22 13:31 ET, the page showed `$-46.72 / 3% = 59.0%`; the production calculation on the same account returned exact trading P&L `$-9.80 / $188.12 = 5.2%`, including Day and Swing and excluding held QHM/F6 quantities.

## Design

1. The dashboard calls `tier_badges(..., show_qty=False)` only for the Open Positions Tier column. The shared default remains quantity-bearing for any other caller. The Qty column remains the broker net quantity.
2. The Alpaca order read keeps immutable order identity, stop/limit prices, order class, fill quantity, and nested legs. A pure display adapter joins reducing stop orders to canonical ownership tiers by the existing client-order-ID protocol. It counts only accepted working quantity, rejects transitional/malformed orders, and marks a stop beyond the current price as `BREACHED`. Each position shows an actual broker stop price plus covered quantity, or an explicit `PARTIAL`, `MISSING`, `BREACHED`, or `UNAVAILABLE` state; it never uses `—` for protection.
3. Replace the raw Open Orders table with **Protection & next action**: one row per owned tier lot with symbol, tier, broker stop, protected shares versus owned shares, distance to stop, and the next resting profit/exit order. Raw orders remain available through Alpaca and are not useful as a second ungrouped table.
4. `RiskManager.check_kill_switch()` queues the exact result it already computed for a single daemon writer after the kill verdict is latched. The worker atomically replaces `logs/kill_switch_snapshot.json` with schema, timestamp, trading P&L, start equity, 7% runtime limit, dollar limit, percentage consumed, attribution quality, latch state, included tiers (`Day`, `Swing`) and excluded held tiers (`QHM`, `F6`). Queue or disk failure cannot alter or delay the kill decision.
5. The dashboard reads that snapshot and does not reimplement risk arithmetic. Wrong-session, future, expired, malformed, non-finite, legacy symbol-level, internally contradictory, or otherwise inexact snapshots render `UNAVAILABLE` instead of substituting whole-account P&L. The `killed` flag must equal the verdict implied by the snapshot's own P&L, start value, and runtime limit. The snapshot's expiry is independently derived from the configured Day/Swing scan cadence. A same-session durable kill latch rejects any contradictory snapshot regardless of age. An expired matching `killed=true` snapshot preserves the visible `TRIGGERED` state, labels its P&L as trigger-time rather than current, and says current loss is unavailable. The label states which tiers are included and excluded. Account P&L remains visible separately in its existing KPI.
6. No page is changed in production until Rafael reviews the generated HTML preview and then approves `DASH-PROTECT-1022`.

## Prior work

- `logs/design_records/html_current_state_ux_2026-09-27.md` established exact ownership-ledger display, shared navigation, and fail-closed UI metrics; this increment corrects the remaining stop and kill-source gaps without replacing that architecture.
- `logs/design_records/ownership_ledger_v2_2026-10-09.md` made `day`, `swing`, `qhm`, and `forever_6` the canonical ownership boundary used for the protection join.
- `logs/tb_audit_log.md`, entry `2026-09-17 — Account kill switch: phantom false-trip fixed`, established `_intraday_trading_pnl()` as the production authority and explicitly rejected `equity-last_equity` as the primary kill numerator.
- `logs/tb_audit_log.md`, entry `2026-08-11 — PR #130`, established that dashboard writers can remain resident on old module bytes and therefore require the writer restart and post-deploy rendered-value verification.
- Open PR #727 is documentation-only (`claude/slack-0930-rq`) and does not touch any covered file; `origin/main` was `a5ebe07` when this record was written.
- `handoff.md` identifies current ownership, concurrent work, and the live deploy pointer; this dashboard diff does not replace those contracts.
- `logs/design_records/report_single_source_of_truth_2026-08-20.md`, `logs/design_records/bot_audit_2026-09-24.md`, and `logs/design_records/audit_broker_ground_truth_2026-09-24.md` require broker-grounded operator reporting and explicit unavailable states.
- `logs/design_records/self_qa_gate4_design_record.md` defines the design-marker obligation used by this new regression file.
- `logs/design_records/f6_pending_buy_reservation_2026-10-12.md` and `logs/design_records/f6_instrument_and_kill_policy_2026-10-15.md` define F6 reservation/kill semantics that the dashboard reports without modifying.
- `logs/design_records/day_tier_v2_design_2026-08-29.md`, `logs/design_records/day_tier_live_build_2026-09-02.md`, and `logs/design_records/bggn_tp_sizing_dynamic_limits_2026-09-25.md` define Day-tier ownership, protection, and dynamic limits that remain upstream of this display adapter.

## Required questions

Q1: This improves loss-control and stop observability, preventing false operator decisions while leaving the PAPER account's trade generation and `$2.5K -> $25K` objective unchanged; measured discrepancy is 59.0% displayed versus 5.2% actual.
Q2: It changes zero trades because it only publishes and renders existing decisions (`generate_dashboard.py`; snapshot write after existing computation).
Q3: It should exist because the operator page currently contradicts broker truth; the simpler safe method is to display the risk engine's result rather than duplicate its calculation.
Q4: It extends `html_current_state_ux_2026-09-27.md`, ownership ledger v2, and the 2026-09-17 kill-switch correction; open PR #727 has no overlapping files.
Q5: It introduces no trading threshold; the displayed limit comes from the runtime profile snapshot and stop coverage comes from live broker orders.
Q6: It exposes the static assumption that a separately imported `config.py` represents runtime configuration; future operator metrics should be published by their owning runtime component.
Q7: It connects broker orders, canonical ownership, the risk engine, and dashboard reporting; each join fails visibly when identity or freshness cannot be proven.
Q8: Every claim is measured: OCI 2026-10-22 read-only probes recorded six broker stops and the `$-46.72/59.0%` versus `$-9.80/5.2%` discrepancy.
Q9: Historical market replay is N/A because no trading decision changes; deterministic cases cover long/short, co-hold, complete/partial/missing stops, malformed orders, exact/stale snapshots, and 3%-versus-7% profile divergence.
Q10: N/A — no edge is calibrated or withheld.
Q11: It changes no size, frequency, concurrency, exposure, multiplier, Kelly input, or cap; `RiskManager`'s computed verdict is not modified.
Q12: It surfaces the every-position-stop rule and the exact 7% brake without changing either; `paper=True` and data tiers remain unchanged.
Q13: Missing/stale/wrong broker or snapshot data displays `UNAVAILABLE`/`MISSING` and never fabricates a stop or loss percentage.
Q14: This is a live operator display after approval, with no shadow path; the preview is a review artifact required by Rafael before production.
Q15: The snapshot records the full displayed decision stack: numerator, denominator, limit, attribution quality, latch, tier inclusions, and timestamp.
Q16: Runtime proof is a fresh `kill_switch_snapshot.json`, generated dashboard rows with broker stop prices, and a first-session comparison to the risk-engine log/account probe.
Q17: Post-deploy verification will inspect the served HTML, snapshot timestamp, and live Alpaca stop orders after restarting `mtf-bot`, `mtf-writer`, and `mtf-http`.
Q18: The 59% display is not accepted as designed: source review proves it uses a different limit and numerator from the live control.
Q19: The change does not alter entry, exit, sizing, routing, or gate verdicts, but it touches gate code for read-only publication; therefore no commit occurs without `APPROVE DASH-PROTECT-1022`.
Q20: Review prompts will neutrally ask whether the UI matches source facts and whether the snapshot can affect the verdict; full relevant source and live evidence are included.
Q21: Tests inject account/order/snapshot data and cannot contact Alpaca; the live evidence command is read-only and separate from tests.
Q22: N/A — no new fail-closed trading control; the UI fails visibly while trading behavior is unchanged.
Q23: Full-path proof renders a dashboard from broker-shaped fixtures and the exact risk snapshot, then asserts tier labels, stops, coverage states, and kill arithmetic in final HTML.
Q24: No absence claim is unverified: every `MISSING` state is derived from the current broker order set for the owned tier/side.
Q25: N/A — no remembered market statistic is used; stop prices and loss measures come from current Alpaca/risk data.
Q26: Stops use T1 Alpaca trading orders; kill data uses the production risk engine's T1 fills/positions calculation; failure renders explicit unavailable states.
Q27: `logs/kill_switch_snapshot.json` is schema-versioned and atomically replaced by a single daemon worker; `dashboard.html` retains its existing atomic writer.
Q28: The verdict is computed and any breached kill is latched/persisted before telemetry is non-blockingly queued; the dashboard writer reads the worker's file during render, independent of entry/exit threads.
Q29: This is P0 operator truth because the page currently overstates daily-brake use by about 11.3x and hides live stops, which can cause incorrect intervention before the next session.
Q30: Professional execution consoles reconcile position ownership to broker-native protection and display coverage, freshness, and control provenance; the existing page lacks those joins.
Q31: N/A — no return model or parameter trial; deterministic correctness replaces walk-forward statistics.
Q32: N/A — no backtest; current broker orders and runtime snapshots are point-in-time reads.
Q33: N/A — no execution simulation or performance claim; order prices are broker-native resting prices.
Q34: N/A — no expected return or factor exposure changes.
Q35: The display works identically across regimes because it renders current orders and the already regime-independent daily control result; no trading tail changes.
Q36: Capacity is immaterial at `$25K`; the join is bounded by current positions/orders and Alpaca's existing 100-order page.
Q37: N/A — no edge inference; deterministic fixture matrix and independent exact-diff review establish correctness.
Q38: N/A — no strategy is added, paused, decayed, or replaced.
Q39: N/A — sizing is unchanged; displayed kill P&L comes from exact Alpaca fills/positions through the production risk engine.
Q40: The owned result is truthful stop coverage and daily-brake status; machine checks assert that the rendered values equal broker orders and the serialized risk verdict, including `$-9.80/$188.12 = 5.2%`.
Q41: A professional would also display partial coverage, tier provenance, data freshness, and explicit unavailable states; all four are included rather than only filling blank stop cells.
Q42: Rawest data is Alpaca position/order/fill data plus the ownership ledger; the design uses those sources rather than Slack, trade summaries, or inferred state.
Q43: The professional method is a single control owner publishing a versioned telemetry snapshot and a presentation layer reconciling that with broker-native protection; the design follows it.
Q44: Independent evidence is exact-diff cold/adversarial review, Groq and Google AI Studio review, deterministic HTML assertions, and a post-deploy broker-versus-served-page comparison.

## Approval and validation plan

After Rafael approves the preview and `DASH-PROTECT-1022`: run Python 3.10 compilation, Ruff E/W/F/B, mypy `--warn-unreachable`, focused and adjacent dashboard/risk suites, cold second review, adversarial review, Groq and Google AI Studio exact-diff preship, CI, signed commit/PR `(OpenAI Codex)`, OCI `git pull --ff-only`, service restarts, and served-page/live-broker verification.

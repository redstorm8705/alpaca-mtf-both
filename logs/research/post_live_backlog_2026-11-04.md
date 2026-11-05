# Post-live audit backlog — sorted (2026-11-04, Claude signed)

Source: `python3 .claude/preship/post_live_due.py` on a full-history checkout of `origin/main` (`8fcd50f`): **86 merged
changes since 2026-10-09 have no post-live audit on record** in `logs/post_live_audits.jsonl` (a `NOT_EXERCISED`
record keeps a change open). The handoff names only a handful. A shallow clone hides most of them: with 50 commits of
history this session's tracker listed 7.

Risk class is by file path (trading paths = HIGH). `later` = number of later main commits touching the same files,
so a large number means the current code differs from what shipped and the audit should check today's behaviour, not
the first session's. Audits need OCI logs and Alpaca fills; this list only sets the order.

## Order of work
1. **Newest trading changes, unchanged since (audit against their first session):** #825 news halt, #823 Swing level,
   #822 trend-night sizing, #817 QHM fill save, #753 7% kill switch by tier, #760 exit/orphan protection, #583 exit
   pricing never uses a Day-tier fill.
2. **Sell-safety between tiers (co-hold), audit as one group against recent sessions:** #577, #579, #584, #585, #587,
   #594, #599, #567 — the rule each enforces is "one tier never sells or cancels another tier's shares/stop". Swing
   exits in the same pass: #554, #558, #560, #622.
3. **Day-tier sizing and halts (CONV-1018, HALT-1019), one group:** #709, #711, #712, #717, #716, #569, #572, #573,
   #578, #597, #603, #605.
4. **QHM / Swing capacity:** #564, #612, #642, #645, #646, #654, #655, #658, #662, #683.
5. **F6 (F6 is OFF):** #650, #651, #667, #685, #699, #738 — cannot run live; record NOT_EXERCISED with that reason, or
   cover them with the F6 full-path simulation (#790) when F6 is enabled.
6. **Medium / low (logging, reports, alerts, tooling):** spot-check each against one session's output.

## Full list
| Risk | PR | Merged | Later | Title (last commit on the PR) | Files |
|---|---|---|---|---|---|
| HIGH | #825 | 2026-11-03 | 0 | fix(news): whole-phrase HALT matching on titles; circuit-breaker-unknown and close-time venue guards (NEWS-VEN | events/news_monitor.py strategy/run_cycle.py |
| HIGH | #823 | 2026-11-02 | 0 | Merge branch 'main' into claude/swing-level-last-trade-1103 | execution/swing_breakout_manager.py |
| HIGH | #822 | 2026-11-02 | 0 | TREND-NIGHT-DAYLOTS-1103: trend_night counts positions after the Day tier's own same-day flatten (signed: Clau | execution/swing_overnight.py |
| HIGH | #817 | 2026-11-02 | 0 | QHM-FILL-SAVE-1102: save QHM state the moment a fill is booked (signed: Claude) | execution/quarterly_hold_manager.py |
| HIGH | #760 | 2026-10-24 | 1 | fix(safety): AUDIT-1025 exit/orphan protection + fills-only P&L (signed: Claude) | execution/exit_logic.py execution/orphan_manager.py |
| HIGH | #758 | 2026-10-24 | 3 | Merge remote-tracking branch 'origin/main' into claude/safe-1024 | execution/day_trade_manager.py execution/exit_logic.py execution/fifo_pnl.py |
| HIGH | #753 | 2026-10-23 | 0 | Merge branch 'main' into claude/kill-attr-1023 | execution/risk_manager.py |
| HIGH | #738 | 2026-10-22 | 9 | fix(f6): prohibit ETF ownership and preserve swing exclusions (OpenAI Codex) | config.py execution/forever_hold_manager.py execution/instrument_policy.py execution/swing_breakout_manager.py |
| HIGH | #725 | 2026-10-21 | 2 | fix: bind shutdown stops to Swing ownership (OpenAI Codex) | events/handlers.py execution/stop_protection.py main.py |
| HIGH | #717 | 2026-10-19 | 4 | fix(day-tier): CONV-1018 room parity — 10/10 Track-B skips the B cap; per-name rooms restored (signed: Claude) | execution/day_trade_manager.py |
| HIGH | #716 | 2026-10-19 | 5 | feat(day-tier): HALT-1019 — halts last only while their cause lasts (signed: Claude) | execution/day_trade_manager.py strategy/day_tier_logger.py |
| HIGH | #712 | 2026-10-19 | 6 | feat(day-tier): CONV-1018 half-at-2R scale-out for proven-band lots (Claude) | execution/day_trade_manager.py |
| HIGH | #711 | 2026-10-19 | 7 | fix(day-tier): CONV-1018 losing bands trade at today's size, never skipped (Claude) | execution/day_trade_manager.py |
| HIGH | #709 | 2026-10-18 | 8 | feat(day-tier): CONV-1018 conviction-band sizing (Claude) | data/alpaca_data.py execution/day_trade_manager.py execution/earned_sizing.py |
| HIGH | #699 | 2026-10-18 | 8 | fix(f6): derive allocator comparison tolerance (OpenAI Codex) | config.py execution/forever_hold_manager.py execution/overnight_capacity.py execution/tier_capital_allocator.py |
| HIGH | #693 | 2026-10-17 | 2 | merge origin/main (Claude) | data/gex.py execution/kelly.py |
| HIGH | #685 | 2026-10-17 | 13 | feat(f6): derive starter from empirical tails (OpenAI Codex) | config.py execution/f6_empirical_trigger.py execution/forever_hold_manager.py strategy/run_cycle.py |
| HIGH | #683 | 2026-10-16 | 2 | docs: handoff — QHM owner-sale auto-heal live (#683), Day-tier replay findings (Claude) | execution/manual_sale_proof.py execution/quarterly_hold_manager.py |
| HIGH | #667 | 2026-10-16 | 20 | Document protected fill invariants (OpenAI Codex) | config.py execution/forever_hold_manager.py execution/instrument_policy.py execution/ownership_guard.py execution/quarterly_hold_manager.py execution/risk_manager.py strategy/run_cycle.py |
| HIGH | #662 | 2026-10-15 | 4 | fix(qhm): credit add fills to the ownership ledger at once (Claude) | execution/ownership_guard.py execution/quarterly_hold_manager.py |
| HIGH | #658 | 2026-10-15 | 5 | docs: handoff pointer for QHM dip zones (#658) (Claude) | execution/dip_zones.py execution/quarterly_hold_manager.py run_dip_zones.py scripts/install_dip_zones_cron.sh |
| HIGH | #655 | 2026-10-14 | 6 | Merge remote-tracking branch 'origin/main' into claude/qhm-owner-trim | execution/quarterly_hold_manager.py scripts/qhm_owner_trim.py |
| HIGH | #654 | 2026-10-14 | 12 | Merge remote-tracking branch 'origin/main' into claude/dynamic-overnight-k-v2 | execution/day_promotion.py execution/overnight_capacity.py execution/quarterly_hold_manager.py execution/swing_breakout_manager.py run_overnight_k.py scripts/install_overnight_k_cron.sh |
| HIGH | #651 | 2026-10-14 | 7 | feat(f6): wire protected-buy broker admission (OpenAI Codex) | execution/broker.py execution/forever_hold_manager.py |
| HIGH | #650 | 2026-10-14 | 6 | feat(f6): add protected-buy reservation foundation (OpenAI Codex) | execution/order_admission.py execution/ownership_guard.py execution/pending_protected_buys.py execution/tier_capital_allocator.py |
| HIGH | #646 | 2026-10-13 | 8 | Merge remote-tracking branch 'origin/main' into claude/qhm-swing-reserve | execution/quarterly_hold_manager.py |
| HIGH | #645 | 2026-10-13 | 11 | config: SWING_RESERVE_LOTS = 2 (Claude) | config.py |
| HIGH | #642 | 2026-10-13 | 15 | chore(config): PROV tag on SWING_BREAKOUT_MIN1_RISK_PCT (Claude) | config.py execution/swing_breakout_manager.py |
| HIGH | #624 | 2026-10-13 | 3 | feat(alarm): page when Alpaca holds shares no tier manages (Claude) | execution/unmanaged_positions.py main.py |
| HIGH | #622 | 2026-10-13 | 4 | fix(swing): lock profit stops at broker same scan (OpenAI Codex) | execution/exit_logic.py execution/orphan_manager.py execution/portfolio_tracker.py |
| HIGH | #619 | 2026-10-12 | 4 | fix(tests): no test can reach the real broker (Claude) | execution/broker.py |
| HIGH | #612 | 2026-10-12 | 24 | fix(qhm): give QHM holds real earnings dates; keep stops on through earnings (Claude) | config.py data/fmp_client.py earnings_preflight.py events/earnings_fetcher.py execution/quarterly_hold_manager.py |
| HIGH | #605 | 2026-10-11 | 24 | fix(day-tier): narrow the Track B watch-day exemption to shorts whose stop already has the room (Claude) | config.py execution/day_trade_manager.py run_day_tier.py |
| HIGH | #603 | 2026-10-11 | 25 | feat(day-tier): Track B — exempt breakdown shorts from the watch-day rule; reserve B's budget while a mover is | config.py execution/day_trade_manager.py run_day_tier.py strategy/day_tier_alignment.py |
| HIGH | #599 | 2026-10-11 | 12 | feat(cohold): unreadable Day share count — retry, then sell only the tier's own shares (Claude) | execution/broker.py execution/entry_logic.py execution/exit_logic.py execution/gtc_manager.py execution/stop_protection.py scripts/cohold_claim_eod.py |
| HIGH | #597 | 2026-10-11 | 15 | feat(day-tier): never hold a Day lot over — work the exit after hours, overnight, then flatten at the open (Cl | data/alpaca_data.py execution/day_trade_manager.py run_day_tier.py |
| HIGH | #594 | 2026-10-11 | 7 | feat(entries): defer a Swing/breakout entry while another tier's opposite-side stop rests (Claude) | execution/entry_logic.py execution/foreign_stop_guard.py execution/swing_breakout_manager.py |
| HIGH | #590 | 2026-10-11 | 20 | docs(handoff): same-direction co-hold live (#589/#590) (Claude) | config.py run_day_tier.py |
| HIGH | #589 | 2026-10-11 | 19 | docs(day-tier): state the bracket poll runtime (Day runner cron, not the trading thread) (Claude) | execution/broker.py execution/day_trade_manager.py |
| HIGH | #587 | 2026-10-10 | 4 | fix(stops): the safety-net stop covers only the Swing tier's own shares (co-hold T3) (Claude) | execution/stop_protection.py |
| HIGH | #585 | 2026-10-10 | 5 | fix(exits): a failed Swing partial never cancels another tier's order (co-hold P4) (Claude) | execution/exit_logic.py |
| HIGH | #584 | 2026-10-10 | 8 | fix(broker): a non-Day partial close never sells the Day tier's shares (co-hold P2) (Claude) | execution/broker.py |
| HIGH | #583 | 2026-10-10 | 0 | fix(fills): a Swing/QHM exit is never priced at a Day-tier fill (co-hold P1) (Claude) | execution/fill_helpers.py |
| HIGH | #581 | 2026-10-10 | 3 | docs(handoff): co-hold pick-up pointer after #577-#579 (Claude) | execution/orphan_manager.py |
| HIGH | #579 | 2026-10-10 | 9 | fix(broker): a non-Day close never sells the Day tier's co-held shares (co-hold D1) (Claude) | execution/broker.py |
| HIGH | #578 | 2026-10-10 | 14 | fix(day-tier): Day exits state their lot side; a refused mass close pages (Claude) | events/handlers.py execution/day_trade_manager.py |
| HIGH | #577 | 2026-10-10 | 10 | fix(broker): a close never cancels another tier's stop; partial closes can state their lot side (Claude) | execution/broker.py |
| HIGH | #573 | 2026-10-09 | 14 | fix(day-tier): 10/10 max-size entries are sized to the stop distance; no stop-room cap (Claude) | execution/day_trade_manager.py |
| HIGH | #572 | 2026-10-09 | 15 | fix(day-tier): a signal whose stop is already crossed is not retried on the same bar (AMZN 10/09) (Claude) | execution/day_trade_manager.py |
| HIGH | #569 | 2026-10-09 | 16 | fix(day-tier): stop room measured on real-time bars including the open (AMZN 10/09) (Claude) | execution/day_trade_manager.py |
| HIGH | #567 | 2026-10-09 | 8 | fix(ledger): promoted Day lots move to the Swing tier in the ownership ledger (Claude) | execution/day_promotion.py execution/ownership_guard.py execution/tier_transfers.py run_ledger_sync.py |
| HIGH | #565 | 2026-10-09 | 6 | fix(alerts): a planned restart no longer pages the phone as a crash (Claude) | main.py |
| HIGH | #564 | 2026-10-09 | 7 | fix(qhm): the QHM broker adapter accepts client_order_id — every QHM buy was failing (NVDA 10/09) (Claude) | main.py |
| HIGH | #562 | 2026-10-09 | 36 | fix(day-tier): promotion page uses the current tier name (Claude) | execution/day_promotion.py execution/day_trade_manager.py execution/exit_logic.py run_day_tier.py strategy/run_cycle.py |
| HIGH | #560 | 2026-10-09 | 7 | fix(swing): a trailing-stop close is booked only when the broker shows the position gone (Claude) | execution/exit_logic.py |
| HIGH | #558 | 2026-10-09 | 10 | fix(swing): a lot promoted from the Day tier keeps its Swing stop, no break-even shake-out (AAPL 10/09) (Claud | execution/exit_logic.py execution/lifecycle.py execution/orphan_manager.py |
| HIGH | #557 | 2026-10-09 | 1 | fix(alerts): stop-breach-at-open Slack never shows a placeholder P&L; correct gap direction (AAPL 10/09) (Clau | execution/gtc_manager.py |
| HIGH | #554 | 2026-10-09 | 9 | fix(swing): profit targets act on the last price, not the IEX bid/ask (Claude) | execution/exit_logic.py |
| MED | #808 | 2026-10-30 | 0 | Merge remote-tracking branch 'origin/main' into claude/earn-source-build-1030 | data/fmp_client.py run_earnings_warm.py |
| MED | #728 | 2026-10-20 | 0 | feat(day-tier): TRIGGER-LOG-1020 — log every entry-trigger WAIT with its inputs (signed: Claude) | strategy/day_tier_entry_trigger.py |
| MED | #691 | 2026-10-17 | 0 | merge origin/main (Claude) | scripts/install_managed_gates.sh scripts/managed_gates/run_gate.py |
| MED | #689 | 2026-10-17 | 1 | revert: Day-tier setup scorecard (#687) — owner-ordered, never approved (Claude) | run_day_setup_scorecard.py run_day_tier.py strategy/day_setup_scorecard.py |
| MED | #687 | 2026-10-17 | 2 | docs: handoff — Day-tier setup scorecard live (#687) (Claude) | run_day_setup_scorecard.py run_day_tier.py strategy/day_setup_scorecard.py |
| MED | #673 | 2026-10-16 | 0 | test(ledger_sync): expect "QHM (long-term hold)" wording | run_ledger_sync.py |
| MED | #660 | 2026-10-15 | 0 | fix(day-tier): a market-wide data gap is not a halt (Claude) | strategy/day_tier_track_b.py |
| MED | #601 | 2026-10-11 | 5 | feat(day-tier): log why each screened Track B mover did not trigger (Claude) | run_day_tier.py strategy/day_tier_momentum_trigger.py |
| MED | #574 | 2026-10-10 | 8 | feat(day-tier): direction rules block again — no-trend, intraday trend against, counter-trend longs (Claude) | run_day_tier.py |
| MED | #570 | 2026-10-09 | 1 | fix(day-tier): name the volume-baseline bar count (static-threshold gate) (Claude) | strategy/day_tier_entry_trigger.py |
| LOW | #813 | 2026-11-01 | 0 | FILL-NOTIF-DST-1101: fill-alert state survives the DST fall-back hour (signed: Claude) | execution/fill_notifications.py |
| LOW | #809 | 2026-10-31 | 0 | PREFLIGHT-ENV-1031: load .env in the Sunday earnings pre-flight and report Slack delivery truthfully (signed:  | earnings_preflight.py |
| LOW | #801 | 2026-10-29 | 1 | earnings_preflight: check every held symbol from the ownership ledger; report plan-blocked names (signed: Clau | earnings_preflight.py |
| LOW | #768 | 2026-10-26 | 0 | Merge remote-tracking branch 'origin/main' into claude/ladder-1025 | gai_client.py |
| LOW | #734 | 2026-10-20 | 0 | feat(audit): ACCOUNT-PNL-1020 — whole-account P&L on the post-market card, cross-checked (signed: Claude) | scripts/audit_slack.py |
| LOW | #733 | 2026-10-20 | 0 | fix(audit): AUDIT-DIFF-1020 — send long modified files to the nightly auditor as their 24h diff (signed: Claud | nightly_audit.py |
| LOW | #724 | 2026-10-20 | 2 | Merge remote-tracking branch 'origin/main' into claude/slack-eod-1020 | nightly_audit.py scripts/audit_slack.py |
| LOW | #702 | 2026-10-18 | 0 | feat(reports): RULES-1018 — two-source P&L check, belief tests, tier goals (Claude) | scripts/belief_tests.py weekly_postmortem.py |
| LOW | #677 | 2026-10-16 | 0 | docs: design record and post-live expectation for the alert wording fixes | alerts.py |
| LOW | #675 | 2026-10-16 | 1 | Merge branch 'main' into claude/gtc-page-dedupe | alerts.py |
| LOW | #670 | 2026-10-16 | 0 | Merge branch 'main' into claude/dazzling-fermi-2dplsg | scripts/install_state_freshness_cron.sh |
| LOW | #669 | 2026-10-16 | 0 | Merge branch 'main' into claude/audit-triage-co-20261016 | midday_audit.py |
| LOW | #668 | 2026-10-16 | 1 | test+docs(monitor): state-freshness alarm tests and design record (Claude) | scripts/install_state_freshness_cron.sh scripts/state_freshness_check.py |
| LOW | #665 | 2026-10-15 | 1 | Merge remote-tracking branch 'origin/main' into claude/audit-reports-plain-english | auto_ai_audit.py scripts/post_live_eval.py |
| LOW | #639 | 2026-10-13 | 3 | fix(alerts): today-only fill cards, 12-hour PT, realized P/L, one card per order (Claude) | alerts.py execution/fill_notifications.py |
| LOW | #592 | 2026-10-11 | 2 | docs(handoff): post-live engine live (#592) (Claude) | scripts/post_live_eval.py |
| LOW | #575 | 2026-10-10 | 0 | fix(names): the fourth tier is "F6" in person-facing output again (Claude) | monthly_review.py tier_names.py |
| LOW | #556 | 2026-10-09 | 1 | fix(reporting): label Day entries by setup (OpenAI Codex) | auto_ai_audit.py |

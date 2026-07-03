# alpaca-mtf-bot — Project State (as of 2026-07-03, session S-GEX, all times PT)

## Deployed this session (OCI HEAD 6eb79f8, all services active, HEALTH OK)
1. **GEX pipeline repair** (commit 414b839): the Alpaca indicative options feed never carries greeks or open interest — the old code read both from snapshots, producing ZERO valid datapoints for 9 weeks (1,269 snapshots), with empty results mislabeled POSITIVE. Fixed: OI from contract objects, gamma computed locally (Black-Scholes bisection from quote mids), UNKNOWN-on-empty, shadow log at INFO. RC-6 instance found+fixed same session. Runtime-verified with real SPY/QQQ labels and flip strikes.
2. **16pt/TSMOM evidence chains** (commit 6513e44): score_16pt now reaches trade_events entry records (was never forwarded — a live entry gate, Layer 9, traded on a score with zero trade-level evidence). TSMOM fields restored to signal dicts: the board-approved (17-0, 2026-04-22) volatility-scaled sizing is LIVE for the first time (it had been a silent no-op — the sizing code read a field that never existed), staged clamp 0.75x–1.25x until a 20-trade review. New scripts/score16_aggregator.py archives shadow-scoring data before the 14-day prune (first run rescued 11 days / 310 rows); cron 4:20 PM ET weekdays.
3. **GEX ACTIVATED** (commit 6eb79f8, Rafael mandate superseding the S50b 30-session shadow condition): GEX_ENABLED=True with staged multipliers 1.10 (NEGATIVE) / 1.05 (POSITIVE) — full S50b values 1.30/1.15 return when rolling 20-trade WR ≥ 35%; stale window 30 min (fail-neutral to 1.0); kelly.py GEX log at INFO + direct-config-access fix (GAI round-3 finding). Daily GEX impact audit: scripts/gex_daily_audit.py, cron 4:30 PM ET, JSON report + Slack digest. GAI dissented 3 rounds; all code-level findings adopted; residual governance-philosophy objection resolved by board 2-0 tie-break (Kyle, Thorp) — split documented in tb_audit_log.

## Decisions made
- Rafael evolution mandate: the bot must LEARN, not just react. Audit found strong reactive dynamism (VIX curve, MRI, 9-layer floor) but Kelly was the only parameter learning from outcomes; all four designed learning loops had severed data pipes (GEX, 16pt, TSMOM, volume-shadow threshold). All pipes now flow; next structural step is the walk-forward/IC recalibration engine (weekly job proposing parameter updates for board approval).
- UX total redesign of the 5 HTML pages: approved direction, QUEUED behind critical bugs. Wroblewski (TB) leads when picked up.
- Standing conditions: Taleb kill-rule (TSMOM scaled-trade #20 with WR<35% → zero multiplier + board review); Kyle GEX label-stability watch (>40% weekly flips → suspend).

## Key findings (open)
- external_close exits = 92% of recent losses (−$406 of −$440 across 32 reconciled trades; 16 trades) — exit-attribution diagnostic is the top queued item.
- Score monotonicity broken in recent sample: score-12 entries performed worse than score-10 (both 12pt and 16pt non-monotonic, small n).
- Shorts 0% WR over last 16 intraday shorts.
- VOLSHADOW: static 1.5x volume threshold passes only 8.8% (median ratio 0.95) — derive percentile threshold when 60 LdP sessions reached (~32 done).
- Power-hour slot expansion is dead code since 06-30 config change (POWER=5 < MAX_OPEN_POSITIONS=7).
- B4 orphan_manager draft from prior session parked in git stash — restart from Step 1 to resume.

## Monday 2026-07-06 session-start verifications
GEX Layer8 INFO reads flowing; first entry event carries score_16pt + non-null tsmom fields; first "TSMOM vol-scale" log; Kelly edge_mult lines (not Friday); both new cron outputs + Slack digests present.

## Queue (priority)
1. P0 fill-matching bug (RC-4): orphan external-close path matches months-old fills → phantom P&L (PANW −$182.79 phantom; eod 07-02 alpaca=$0.00 vs tracker=−$251.12). Fix fill_helpers/orphan_manager, rebuild kelly_stats, relabel GTC-fill exits. Diagnostic complete 7/3 — see tb_audit_log.
2. Walk-forward/IC recalibration engine (evolution mandate)
3. UX redesign (5 pages)
4. Volume threshold derivation (board package)
5. Power-hour config fix
6. Stale-docs sweep; OCI git cleanup (106 untracked files — caused two pull collisions this session)

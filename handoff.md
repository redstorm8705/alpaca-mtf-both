# Handoff — Session 2026-07-03: GEX repair + activation, 16pt/TSMOM evidence chains, 12-pt confluence audit

## LATEST CHANGES (this session — all deployed, OCI HEAD `6eb79f8`)

| Commit | Files | What |
|--------|-------|------|
| `414b839` | `data/gex.py`, `strategy/run_cycle.py` | **GEX pipeline repair (RC-6)**: indicative feed never carries greeks/OI — OI now read from contract objects, gamma computed locally (BS bisection IV from quote mids), UNKNOWN-on-empty label (was mislabeled POSITIVE), Layer-8 shadow log debug→INFO. 1,269 dataless snapshots since Apr 27 explained. Runtime-verified: SPY/QQQ real labels + flip strikes. |
| `6513e44` | `execution/portfolio_tracker.py`, `strategy/signal_generator.py`, `config.py`, `scripts/score16_aggregator.py` (NEW) | **16pt/TSMOM evidence chains**: score_16pt now reaches trade_events entry records; TSMOM fields restored to signal dicts — **TSMOM vol-scaled sizing (board 17-0, Apr 22) live for the first time**, STAGED clamp [0.75x, 1.25x] until 20-trade review. Aggregator rescued 11 days / 310 rows from the 14-day prune; cron 4:20 PM ET weekdays. |
| `6eb79f8` | `config.py`, `execution/kelly.py`, `scripts/gex_daily_audit.py` (NEW) | **GEX ACTIVATED (Rafael mandate, supersedes S50b 30-session shadow)**: GEX_ENABLED=True; STAGED multipliers 1.10 (NEGATIVE) / 1.05 (POSITIVE) until rolling 20-trade WR ≥ 35% (full S50b values 1.30/1.15); stale window 45→30 min; kelly GEX log at INFO + GAI R3 direct-config-access fix; daily GEX audit cron 4:30 PM ET. GAI dissented 3 rounds (all code findings adopted; governance-philosophy residual resolved by board 2-0 tie-break — documented in tb_audit_log). |

Also: audit-log entries for the 12-pt confluence integration audit (see below), bug_counter + CLAUDE.md RC-6 row updated, UX-redesign + evolution-mandate items logged to CLAUDE.md Future Roadmap, `scripts/ram_watch.sh` executable bit committed (was OCI-only drift blocking pulls).

## BOT STATUS
- **Running:** all 4 services active on OCI, HEALTH OK, HEAD `6eb79f8` (clean tree)
- **Open positions:** GOOGL 1sh + NVDA 1sh (QHM tranches), MSTR −1sh + RBLX −1sh (Movers) | Equity ~$2,844
- **GEX live:** STALE label after hours (correct fail-neutral); first live Layer-8 INFO reads Monday
- **Crons added:** score16_aggregator 20:20 UTC wkdays · gex_daily_audit 20:30 UTC wkdays

## ⚠️ MONDAY 2026-07-06 SESSION-START VERIFICATIONS
(July 4th holiday was OBSERVED Friday 7/3 — market fully closed all day 7/3, per Alpaca calendar; zero RTH cycles ran after today's deployments. Next session: Mon 7/6, normal hours.)
1. `grep "GEX Layer8" logs/mtf_bot.log | tail` — INFO reads flowing every cycle
2. First entry event in trade_events.jsonl must show `score_16pt` + non-null `tsmom_*` fields
3. `grep "TSMOM vol-scale" logs/mtf_bot.log` — first real vol-scaled sizing
4. `grep "edge_mult" logs/mtf_bot.log` — Kelly GEX boosts (none on Fridays by design)
5. 4:20/4:30 PM ET cron outputs: logs/score16_report.json + logs/gex_daily_audit_*.json + Slack digests

## STANDING CONDITIONS (board-logged, need Rafael approval to change)
- TSMOM clamp [0.75, 1.25] → revert to [0.50, 1.50] after 20 scaled trades + review. **Taleb kill-rule:** WR < 35% at scaled-trade #20 → zero the multiplier + board model review.
- GEX multipliers 1.10/1.05 → 1.30/1.15 when rolling 20-trade WR ≥ 35% or board review. **Kyle watch:** >40% weekly label flips → suspend + review.

## OPEN ITEMS / QUEUE (priority order)
- [ ] **P0 — Fill-matching bug (RC-4 class, found by exit-attribution diagnostic 7/3)**: orphan external-close path matches MONTHS-OLD fills as "actual close fills" (TSLA phantom exit $347 = April's short-entry fill; PANW phantom −$182.79). Suspect: no time lower-bound in the fills query and/or S47 `direction='asc'` oldest-first. Targets: `execution/fill_helpers.py` + `execution/orphan_manager.py`. Full patch sequence; then **rebuild kelly_stats** (contaminated with phantom R-multiples — phantom exits are NOT _fill_unverified because the wrong fill was a real fill). Also: pull 07-01 evening fills to explain why 5 positions left Alpaca overnight. eod 07-02 proof: alpaca=$0.00 vs tracker=−$251.12.
- [ ] **Exit-reason labeling**: ~14 GTC-stop fills recorded as `external_close` instead of `gtc_stop_triggered` — attribution noise in all analytics. Fix alongside the above.
- [ ] **Walk-forward/IC recalibration engine** (S59 roadmap → Rafael evolution mandate 2026-07-03): weekly job proposing parameter updates for board approval. Raw material now accumulates (score16 history, TSMOM logs, GEX daily audits).
- [ ] **UX total redesign** — 5 HTML pages (dashboard, weekly review, scanner, options, monthly). Queued by Rafael behind critical bugs. Wroblewski leads.
- [ ] **Volume threshold derivation**: VOLSHADOW says static 1.5x passes 8.8% (median ratio 0.95); derive percentile-based threshold, board package (~32 of 60 LdP sessions elapsed).
- [ ] **Power-hour expansion dead code**: BUCKET_B_MAX_POSITIONS_POWER=5 < MAX_OPEN_POSITIONS=7 since 06-30 — branch unreachable; board call: raise or remove.
- [ ] **B4 orphan_manager draft**: prior-session proposal, never approved — parked in `git stash` ("B4 orphan_manager draft"). RULE C-7: restart from Step 1 to resume.
- [ ] **Stale-docs sweep (P2)**: config conviction-tier comments (say 11/10, values 9/8/8); run_cycle "_base_min Paper=10" (actual 8); "16pt log-only" headers (Layer 9 gate is live); CLAUDE.md project context same.
- [ ] Carry-over: OCI git cleanup — NOW CATEGORIZED (60 files, table in tb_audit_log 7/3 evening): 24 stale root .py duplicates, 12 downloaded repos, 7 PDFs, 7 loose docs, 7 keep-logs, 2 live ops scripts to COMMIT (memory_watchdog.sh, rss_sampler.sh), 1 Users/ stray. Rafael decides dispositions. Also: qhm external_close price:null, cross-strategy Phase 3 audit, GE/GEV/LLY QHM entries Jul 22+.
- [ ] Volume threshold: derivation DONE (n=10,494: median 0.81, p75=1.08, 1.5x passes 5.9% vs RSI 80.9%) — board package options: graded ≥1.1x=1pt or rolling-percentile. Vote at 60 LdP sessions (~4 wks) or earlier for graded shadow.
- [ ] Power-hour fix recommendation (board vote next session): raise BUCKET_B_MAX_POSITIONS_POWER 5→9 to restore original intent (was +2 over the standard cap) or delete the branch.

## ⚠️ ANALYTICS CAVEAT (from exit-attribution diagnostic)
Tracker-side loss figures are contaminated by phantom P&L (wrong-fill matching). The "external_close = 92% of losses / −$440" figure overstates real losses — Alpaca FIFO showed $0.00 on the worst day (07-02). Re-run all strategy stats on Alpaca-verified trades AFTER the fill-matching fix + Kelly rebuild. Score-monotonicity and shorts-WR conclusions are PROVISIONAL until then.

## KEY AUDIT FINDINGS THIS SESSION (12-pt confluence integration audit — full detail in tb_audit_log 2026-07-03)
- All 7 live scoring conditions healthy; dynamic floor (9 layers) working.
- Fixed this session: GEX dataless 9 weeks; score_16pt never logged; TSMOM sizing silent no-op; 16pt data self-deleting at 14 days.
- Still true: score-12 entries performed WORSE than score-10 in last 32 trades (both 12pt and 16pt non-monotonic, small n) — walk-forward engine is the structural answer.
- c9_implied_range in 16pt system still hardcoded 0 ("data pending" since Apr 20) — real 16pt max is 19.

## HARD INVARIANTS (unchanged)
- `paper=True` hardcoded in broker.py | SPY 5-min bar-over-bar sole entry gate | PDT abolished | All P&L from Alpaca fills API | Gro/GAI lean prompts only

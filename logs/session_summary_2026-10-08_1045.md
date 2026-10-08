# Session Summary — 2026-10-08 10:45 AM PT
**Project:** alpaca-mtf-bot (Claude, worktree gamma-trading-strategy-308918)
**Duration:** multi-day session (2026-10-07 → 2026-10-08), several usage-limit resumes

## Shipped (all deployed to OCI, services active)
| PR | What it changes |
|----|-----------------|
| #517 | Tier safety: stop/close recoveries cancel only the own tier's orders; wash-trade rejects cancel nothing and page; free-share stop when another tier's stops cover the shares |
| #522 | Day-tier close timing: entries stop 3:40 PM ET, stop live to 3:58, confirmed cancel then market exit (retried), any lot still open after 4:00 gets an extended-hours GTC limit at bid/ask repriced until filled; cron widened to 13-23 UTC |
| #524 | After-hours exit take-over guard (no exit order on a lot another tier took over) |
| #525 | Preship tool: static-facts block (ruff F821/F822/F823 + py_compile on the staged file) + one automatic counter-prompt for bare-name false rejects |
| #526 | Tier names Day / Swing / QHM / F6 everywhere person-facing (tier_names.py + CI gate) |
| #528 | Ledger sync: no OPERATOR CONFIRMATION page when the owner tier's own tagged sell (since the last healed sync) explains the drop |
| #527, #529 | Doc syncs (handoff pointer, audit log) |

## Bugs Fixed
| File | Fix |
|------|-----|
| execution/broker.py | 40310000 recoveries cancelled every tier's orders → tier-scoped |
| execution/day_trade_manager.py | day lots flattened at 3:40 (too early) / no after-close path → 3:58 sequence + AH limit |
| run_ledger_sync.py | paged the operator for QHM's own NVDA stop fill (13:31 UTC) before QHM's heal (14:03) |
| .claude/preship/preship_audit.py | Gro false REJECTs on names defined outside a chunked diff |

## Decisions Made (CEO)
- Day stop live until 3:58, cancel with retries, then market sell; all non-promoted day lots closed by EOD (AH GTC limit at bid).
- ETFs only when the stock is unaffordable (0-1 shares); never a 1-share leveraged ETF position (≥2); 1-share stock fallback when no usable ETF.
- Leveraged cap 5% → 10% (Swing; risk-path gate on implementation); overnight gap limit replaces it for 3x holds.
- Index 3x ETFs for Day and Swing; TQQQ/SQQQ retired as standalone tickers; 2x bear else 1x bear.
- Operator-confirmation page waits for the owner tier's own auto-heal.

## Corrections (Claude was wrong)
- Proposed leaving the day stop in place if the cancel wasn't confirmed by 3:58:30 → CEO: retry is always the default.
- Proposed leaving an after-close day lot for the next open → CEO: extended-hours GTC limit at the bid, closed by EOD.
- Claimed NVDL ~$40 "fits 5 shares" before checking maintenance → verified: NVDL $37.3, Alpaca maintenance 30%, ~3 shares fit the $112 maintenance room.

## Open Items
- [ ] **Item 5a — Track A ETF routing (BUILT, NOT SHIPPED):** patch at `logs/design_records/track_a_etf_route_wip_2026-10-08.patch`. Gate status: statics clean; 22 new tests pass; adjacent suites match main; risk seat APPROVE-WITH-CHANGES (fix applied); cold-2nd round 1 FAIL (both findings fixed: same-signal stock+ETF stacking, stock fallback after a non-size ETF failure); **fresh cold-2nd on the revised diff did not run (weekly limit)**. Remaining: fresh cold-2nd → Gro+GAI preship → adversarial → CI → merge → OCI pull + restart.
- [ ] Item 5 rest: Swing ETF routing, 10% leveraged cap, overnight gap limit, index 3x, 1x bear check, Track A 10/10 max size, Track B 1-share-ETF → stock fallback.
- [ ] Watch first live 3:58 PM ET day-tier exit (META day lot open today) in logs/day_tier_runner_cron.log.
- [ ] Item 4 (5-min ownership refresh), 6, 7 (promotion), 8 (hourly balance reporting).

## User Preferences Observed
- Retries are the default for any exit step; never leave a day lot for the next day.
- No 1-share leveraged ETF positions; ETFs only when the stock is unaffordable.
- Wants false-premise reviewer rejects solved by a mechanism, not by more rule text.

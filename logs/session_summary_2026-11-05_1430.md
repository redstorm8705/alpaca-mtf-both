# Session Summary — 2026-11-05 2:30 PM PT (Claude signed)
**Project:** alpaca-mtf-bot
**Duration:** full day (continued across two usage-limit resets; ends at the weekly limit, resets Nov 8 1 AM PT)

## Shipped and live
| Change | File(s) | What it does |
|---|---|---|
| KILL-PARTIAL-1104 (PR 848) | execution/risk_manager.py | Kill switch judges each fill by its own cum_qty chain; XOM 11-04 false degradation fixed. Verified live 11-05: 6 multi-fill orders validated, attribution_exact true. |
| SCHWAB index quotes (PR 846) | data/schwab_quotes.py | `index_last_with_reasons` reads $VIX/$VIX3M/$SPX (CBOE) — inert until the regime layer uses it. |
| Dashboard held legs + Swing trend label (PR 845) | generate_dashboard.py | Held bracket stop legs shown; Swing "target" now reads "no profit target (trend exit)" and the stop cell shows the trend-exit level. |
| Design records (PRs 843, 847, 850) | logs/design_records/ | REGIME-SIZE-1104 rev 3.6, CATALYST-DYNAMIC-1104 rev 3.1, KILL-PARTIAL-1104 rev 2.2, SWING-RATCHET-1105 rev 9 + break-even ratchet replay. |

## Bugs found / fixed
| ID | File | Fix |
|----|------|-----|
| KILL-PARTIAL-1104 | execution/risk_manager.py | cached partial filled_qty no longer fails later fills (shipped) |
| VIX field mixing (07-23) | strategy/volatility_regime.py | SPY rvol never used as VIX — slice 1 BUILT, NOT SHIPPED (logs/wip/regime_size_1104_slice1_2026-11-05.patch) |
| Cold-2nd found 5 defects in slice 1 across 4 rounds | volatility_regime.py | F1 clock-lag drop of Schwab VIX; F2 yfinance retried every call; F3 15-min vs 10-min freshness gap dropped HIGH_VOL; F4 `now` read before slow Schwab call; F5 removing low_vol would kill the allocator's risk_on mode. All fixed in the patch; round-5 review was cut off by the weekly limit. |

## Decisions made
- Owner approvals typed this session: REGIME-SIZE-1104, CATALYST-DYNAMIC-1104, KILL-PARTIAL-1104.
- SWING-RATCHET-1105 rule (pending owner approval): once a completed daily close reaches +2R, raise the lot's own stop to entry + 0.25R (BRK.B lock $548.80). Only ever a replace of the lot's own stop; never cancel-then-sell. Board (Brandt, Harris, masked-loss seat) + GAI approve rev 9; Codex rejected rev 8 (client-order-id length) and its rev-9 confirmation is still owed (it hit its usage limit).
- Slice 1 keeps the `low_vol` label (allocator risk_on reads it) with no size-up; a stale VIX may only tighten (HIGH_VOL), never loosen.
- FMP ^VIX tier needs a quote function in data/fmp_client.py, which is outside REGIME-SIZE-1104's Covers — slice 1 runs Schwab → yfinance → none; widening Covers needs the owner.

## Corrections (Claude was wrong)
- Swing's "target" column showed the 20-session-low trend exit, which is a stop, not a target (owner) → relabelled.
- Told the owner BRK.B's stop at $527.81 was fine to keep → owner: a stop below entry this far into profit "absolutely should not happen" → SWING-RATCHET-1105.

## Live facts measured
- BRK.B: owner raised its stop in the Alpaca app to $561.85 (untagged order, 11:09 AM PT). The Swing manager logs "re-protecting" every cycle; the broker refuses a second stop (shares held) — no duplicate.
- NVDA: QHM stopped out 2 sh at $192.39 (−$57); Day tier shorted NVDA; a 2-share unmanaged NVDA short appeared 6:43 AM PT and is gone now.
- Kill switch failed closed 3× after the close (4:04/4:36/5:09 PM ET) with "exact tier attribution is unresolved"; ledger shows "reattributed impossible negative protected replay NVDA/qhm=-2". No RTH impact. Root cause owed.
- Schwab $VIX after hours is the 4:15 PM ET settlement (rejected as stale by the 10-min rule); VIX closed 28.18 on 11-05.

## Open Items
- [ ] REGIME-SIZE-1104 slice 1: apply logs/wip/regime_size_1104_slice1_2026-11-05.patch → fresh cold-2nd (round 5) → records → preship (GAI + Codex, context file beside the patch) → ship. Then slice 2 (run_cycle gates on fresh-only vix_value, stop width on basis/prior close, sticky VIX>35, sizing records).
- [ ] SWING-RATCHET-1105: Codex confirmation of rev 9 → one-page approval to the owner → build.
- [ ] CATALYST-DYNAMIC-1104 Phase A build (approved).
- [ ] Kill-switch after-close "attribution unresolved" + NVDA qhm=-2 ledger root cause (post-live #851) + NVDA post-mortem.
- [ ] Owner question still open: was the 11-02 "ship on full BGC alignment" pre-approval a standing rule or one night?
- [ ] Owner: widen REGIME-SIZE-1104 Covers to data/fmp_client.py for the FMP VIX tier (or leave it out).
- [ ] .claude/preship/fork_reminder.py Gro+GAI → BGC text change (gate file; owner approval).
- [ ] Post-live backlog (#753, #760, #583 …), GAP-EXIT / EARN-GAP-REVIEW / LOSS-PM builds, core tier no-entry-since-09-25 diagnosis.

## User Preferences Observed
- A stop below entry on a well-in-profit lot is unacceptable; winners must lock profit.
- Wants the actual CBOE index reading, never a composite or proxy.
- "Ignore reviewer rejection gates as you wrap" applied to the wrap only — unreviewed code is persisted as a patch, not shipped.

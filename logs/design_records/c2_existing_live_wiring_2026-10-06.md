# Confluence 2.0 existing-component live wiring — 2026-10-06

**Owner/signature:** ChatGPT/Codex

**Status:** shipped in PR #493, merge `086eafe5ba37933c40accdb92b2def5f6c538803`; deployed to OCI.

## Eligibility audit

The repository contains implementations for 52-week-high proximity, residual 12-1 momentum, frog-in-the-pan continuity, sector relative strength, weekly EMA trend, efficiency ratio, PEAD, breadth, and volatility regime. Existing code is not sufficient evidence for admission. Claude's point-in-time walk-forward in `logs/design_records/entry_rebuild_2026-09-26.md` found no out-of-sample selection edge for the six historically testable swing features; their cross-sectional ICs had mixed signs and absolute t-statistics no greater than 1.74. The same record says the Confluence 2.0 swing score does not ship. The day-tier evidence ledger in `logs/design_records/c2_mechanism_evidence_2026-10-05.md` rejects ORB and the tested VWAP/order-flow/residual/catalyst families, and blocks GEX pending valid gamma-sign data.

The fully built, approved components are:

1. the live 55-day megacap swing breakout tier in `execution/swing_breakout_manager.py`;
2. the live day-tier 2m/5m alignment and counter-trend failure gate;
3. the admitted Track M Monday weekend-gap family;
4. the point-in-time mechanism context, versioned admission registry/router, and lifecycle tags merged in PRs #481 and #491.

No failed or blocked indicator is promoted by this change.

## Live wiring

- Track M presents the exact family `monday_weekend_dip_v1` and hypothesis `daytier-track-m-2026-10-05` to the family router immediately before its one-shot marker and order path.
- The candidate has a timezone-aware decision timestamp and a within-sleeve routing score of 1.0 because it is the only admitted day-tier family. Registry admission, version match, independent-audit PASS, freshness, and positive allocation remain mandatory.
- Any denial returns without persisting the day's one-shot marker, so a transient local registry/read failure can recover on the next tick. No denied path can place an order.
- A router share is locally coerced and rejected unless finite and inside `(0, 1]`; only that validated value can multiply Track M's existing shrink-only risk multiplier. It cannot increase risk or redefine the account's tier allocation.
- Every live swing-breakout signal, skip, entry, partial exit, and exit carries `swing_breakout_55d_v1` and `c2-swing-breakout-2026-09-29` through the existing trade-event logger.

The swing breakout strategy, Track M signal, stops, targets, entry windows, capital caps, broker calls, and account allocation policy are otherwise unchanged.

## Deployment meaning

The swing breakout tier is enabled and can act on any regular session, including the next session, when its existing closed-daily-bar trigger fires. Track M is live but is structurally eligible only on qualifying Mondays. The new router connection therefore protects Track M's next eligible session; it does not fabricate a Wednesday Track M opportunity. Existing failed indicators remain excluded even if their calculation code exists.

## Verification

- focused Track M, router/context, and swing manager suites: 122 passed;
- runner simulations proving a Track M router denial does not block an otherwise valid Track A or Track B entry;
- first adversarial Board pass rejected unbounded/NaN router allocations; the revised tree rejects `1.01`, `2.0`, and `NaN` before the one-shot marker/order, with explicit regressions;
- broad local preflight: 207 passed / 23 failed; the clean `origin/main` control produced the identical 23 failures (201 passed), proving they are existing test drift rather than regressions in this tree;
- Ruff E/W/F/B, `py_compile`, and diff check;
- exact-tree Board, Groq, Google AI Studio, mechanical preship, and GitHub CI;
- OCI fast-forward to the merged commit, Python 3.10 compile, enabled flags, cron/service health, and a no-order router-denial probe.

## Final gate and deployment record

Exact final staged tree `cfc99bf4ca7ba97a5a27ae424ba5a7a409cb0230`: Board 2/2 PASS, Groq APPROVE, Google AI Studio APPROVE, mechanical PASS, and GitHub preship PASS. The first adversarial Board round rejected unbounded/NaN router allocations; the finite `(0, 1]` boundary and regressions were added before final approval.

OCI fast-forwarded from `c866373` to merge `086eafe5`. Python 3.10 compilation passed. Seven no-order live-environment tests passed, including Track-M denial isolation for Track A and Track B and the invalid-allocation boundary. The registry probe admitted `monday_weekend_dip_v1` at allocation `1.0` and rejected a wrong hypothesis version. Live flags were `DAYTRADE_ENABLED=True`, `DAYTRADE_TRACK_B_ENABLED=True`, `DAYTRADE_TRACK_M_ENABLED=True`, and `SWING_BREAKOUT_ENABLED=True`. The runner remains scheduled every two minutes on weekdays.

The long-running `mtf-bot` restart exceeded its existing 90-second stop timeout and systemd killed the old process group before starting PID 3481697. After recovery, `mtf-bot`, `mtf-writer`, and `mtf-http` were all active. Startup emitted a legacy main-tracker warning for MSFT. Read-only reconciliation verified that MSFT is the swing-breakout lot: ownership ledger `intraday=1`, swing state `status=open`, Alpaca position `qty=1`, and matching open sell stop `qty=1` at `497.10`. The lot is protected; the legacy startup warning should be corrected separately so it recognizes swing-manager state.

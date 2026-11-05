# Open work queue — consolidated (2026-11-04, Claude signed)

One list of every open item scattered across the `handoff.md` pick-up blocks (11-04 back to 10-26), the post-live
tracker, and open GitHub issues/PRs. Each status was checked against `origin/main` at `8fcd50f`, the GitHub
PR/issue lists and `logs/post_live_audits.jsonl` on 2026-11-04.

**Limits of this check:** it was made from a cloud session with no OCI access, no Alpaca keys and no reviewer keys.
Nothing here verifies what is running on the server. "Merged" means merged to `main`, not confirmed deployed.

## A. Handoff corrections — items the handoff still shows as open that are already done

| Item | Handoff says | Actual (source) |
|---|---|---|
| EARN-GAP-REVIEW step 1 `data/earnings_documents.py` | staged, preship blocked on GAI quota | Merged: #836, `b823aac` (11-04) |
| SCHWAB-QUOTES-1103 | staged, needs GAI + reviewer #2 | Merged: #837 `4aee9f5` + follow-up fix #838 `ee7528a` (11-04) |
| NEWS-VENUE-HALT-1103 | "code not started; owner APPROVE first" (11-03 block) | Merged: #825 `9b33222` (11-03); commit records owner-approved design, board, cold-2nd, GAI + Gro |
| SWING-BREAKOUT level fix (11-01 list item 1) | open | Level part merged: #823 `302c16c` (SWING-LEVEL-LAST-1103) |
| News-halt fix (11-01 list item 3) | open | Merged: #825 (same as above) |
| PR 822 post-live "BUGS_OPEN" | recorded | **No record exists** in `logs/post_live_audits.jsonl` for 822; the tracker lists it OVERDUE |

## B. Waiting on the owner (Rafael)

1. **Day ORB data plan** — prior-day SIP spread proxy, or paid real-time SIP (`logs/research/day_pillars_r8_r10_2026-11-02.md`,
   design draft `logs/design_records/day_orb_stocks_in_play_2026-11-03.md`).
2. **Add `ALPHAVANTAGE_API_KEY` to the OCI `.env`** — needed by EARN-GAP-REVIEW-1104 (transcripts).
3. **F6 enablement one-pager** — Codex lane; F6 stays OFF until decided (handoff 10-28/10-26 Codex blocks).
4. **Catalyst bad-news check redesign (CATALYST-DYNAMIC-1104)** — the July guidance-cut patch is replaced by a
   whole-article design; board 5/5 approve-with-changes, GAI + Codex owed. It also found live defects in the current
   gate (newer news cancels a block; 72h fetch vs 30-day window; 25-article cap; watch set misses RIVN/MU/SNDK).
   See `logs/design_records/catalyst_dynamic_2026-11-04.md`.
5. Owner questions from 10-27 still unanswered in the handoff: Track B re-entry after a close; sizing confirmed trades
   via the 2x ETF; a trend-continuation entry (SNDK 10/23, 10/26).

## C. Approved, build queued (needs reviewer quota / Mac session)

1. **GAP-EXIT-1029 rev 3.2** — build not started (`logs/design_records/gap_exit_2026-10-29.md` §3e-3f).
2. **EARN-GAP-REVIEW-1104** — step 1 merged; later steps not started (`logs/design_records/earn_gap_review_2026-11-04.md`).
3. **LOSS-PM-1104** — GAI design review owed before any code, then `scripts/loss_post_mortem.py`.
4. **ALPHA-ENGINE-1104** — design merged #835 (`logs/design_records/alpha_engine_2026-11-04.md`); build status not in handoff.
5. **Swing capacity replay** with point-in-time universe — open PR #834.

## D. Known bugs, not yet fixed

1. Tracker FIFO orphan on trend-night fills (from the 822 review; record not written — see A).
2. QHM overnight-room check fails closed on the unpriced trend-night market buy (same source).
3. Swing breakout earnings check missing — NOW entered 2 days pre-earnings, gapped −25.9% (11-01 list item 1);
   `execution/swing_breakout_manager.py` has no earnings reference today.
4. Swing breakout creates setups for reserved/QHM-held names (TSLA) (11-01 list item 1).
5. Swing breakout K limb also counts Day intraday lots (same class as 822; 11-03 follow-ups).
6. `day_promotion.select_promotions` should count pending trend_night close orders (11-03 follow-ups).
7. Drift detector: `trend_night_symbols()` not in `_drift_exclude`/`_dc_exclude` in `strategy/run_cycle.py`
   (still absent today) — phantom-drift Slack noise.
8. GitHub #756: a QHM PENDING_EXIT close that never fills is only re-checked at startup.
9. Unexplained OCI warning: `ledger_sync_cron.log` "reattributed impossible negative protected replay GOOGL/qhm=-2 to
   intraday" (11-02 13:00 and 13:20) — not investigated.

## E. Post-live audits due (from `.claude/preship/post_live_due.py`)

Overdue: **#823, #822, #817** (due 11-03), **#813, #809, #808** (due 11-02), **#801** (due 10-30).
Due after today's close: **#825** (news-venue-halt). Earlier gaps also named in the handoff: 768, 758, 753, 764 (BUGS_OPEN).
These need OCI logs and Alpaca fills.

## F. Open GitHub items

- **PRs:** (#839 dashboard per-tier rows merged 11-04 as `8fcd50f`), #834 Swing capacity replay (11-04, active), #804 docs Day search r3-r4 (10-30), #727 docs SLACK-0930 answers (10-20).
- **Issues (24 open):** 16 `UNEXERCISED_EXPIRED` post-live expectations (#819, #805, #795, #776, #775, #774, #773,
  #772, #771, #770, #751, #750, #749, #746, #731, #730), 7 `NEW_ERRORS` session reports (#820, #806, #777, #739,
  #736, #672, #636), and #756 (above). Issue #794 (false post-live alarm) is closed.

## G. Small cleanups (no trading effect)

1. Stale docstring: `strategy/day_tier_track_b.py` still describes an 11:05 ET window (line 132); live window ~15:35 ET.
2. Weekly post-mortem clarity card: the 11-01 work was in a local worktree (`claude/pm-clarity-1101`) and is **not
   on GitHub** — likely lost; rebuild if still wanted.
3. Swing tests: pre-existing order-dependent failure `test_same_scan_second_entry_sees_first_lot`.
4. `preship_gate` reads git in the main checkout, not the worktree.
5. Track A follow-ups from #779: noise window includes extended-hours bars; stop room measured from the limit, not the fill.
6. `handoff.md` holds many "⏩ LATEST" headers from earlier dates; archive older blocks (gated file — needs a reviewer pass).
8. Old "Gro + GAI" wording: the external voices are BGC (board, GAI, Codex) since REVIEWER-SWAP-1025; Gro is the
   fallback only. 70 old-wording mentions remain (CLAUDE.md 62, rules/CORE.md 5, rules/OPERATING.md 2,
   rules/behaviour.md 1). The every-prompt reminder ("convene board + Gro + GAI … final Gro+GAI pre-ship audit") is not
   in the repo's `.claude/`, so it lives in the Mac's user-level Claude settings. All are gated / Mac-side edits.
7. 467 of 534 remote branches untouched for 14+ days — inventory for owner-approved deletion (cleanup item 2).

## Update 2026-11-05
- #811 (F6 crash-only trigger restored, OpenAI Codex) merged to main (`fd2f700f`, deployment note `152de542`); F6 stays OFF.
- New design: VIX-FIELD-1105 (`logs/design_records/vix_field_2026-11-05.md`) — VIX rules read SPY realized vol on VIX-outage days; also reaches three exit rules. Board review done (rev 2); GAI + Codex owed.
- Sizing audit across all tiers: `logs/research/sizing_bind_audit_2026-11-05.md` — 11 size controls that cannot act as
  described; the main finding is size controls that never act (no change to the envelope); the 1-share Swing floor is the owner's approved design.

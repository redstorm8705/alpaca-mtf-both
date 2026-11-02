# Session Summary — 2026-11-01 01:30 PM PT
**Project:** alpaca-mtf-bot
**Duration:** ~2.5 days (2026-10-30 evening → 2026-11-01), including an overnight AWP run

## Bugs Fixed
| ID | File | Line | Fix |
|----|------|------|-----|
| PREFLIGHT-ENV-1031 | earnings_preflight.py | main() | Load .env before fmp_client/alerts import; truthful Slack status via send_slack_delivery (every Sunday run since 07-19 was a no-op logged as "sent") |
| EXPECT-REGEX-1031 | logs/post_live/expect/day-universe-1027.json | proof | Escape `[COIN]`/`[EWY]` regex brackets (false UNEXERCISED finding #794) |
| FILL-NOTIF-DST-1101 | execution/fill_notifications.py | _load_state, _RealizedPnl | Instant math in UTC; ET + timedelta dropped fold=1 in the fall-back hour → 55 false STATE CORRUPT pages |
| DST cron | OCI crontab | — | 3 post-close jobs moved to cron_tz_wrapper (16:08/16:20/16:30 ET); catalyst_engine extended to 13-21 UTC |

## Decisions Made
- SWING-OVERNIGHT-1101 approved and shipped live (PR 814 disabled, PR 815 enabled): position-driven order layer — qty = min(own fills, fresh broker position); owner QQQM never traded; no auto-cover; degraded mode when fill activities are down. 13 cold review rounds; GAI APPROVE; Gro waived on the main module.
- FILL-NOTIF-DST-1101 approved and shipped.
- QHM buy-the-dip redesign queued after Swing (owner: buy the dip first on bullish names; strength-only re-confirm is wrong).

## Corrections (Claude was wrong)
- Asked the owner to authorize a Gro waiver → an existing protocol already covered it; apply existing rules without re-asking.
- Planned a Codex re-review on 11-04 → Gro is the backup reviewer; a Gro/waived pass is complete.
- Said NVDA was blocked "by budget" → 10-30 was the strength-only re-confirm; budget (20% per-name cap) blocked 10-14/10-21.

## Confirmed Patterns (user validated)
- Problem / solution / stock-example status format for updates.
- Overnight AWP with in-thread hourly check-ins.

## Open Items
- [ ] trend_night first-night post-live audit after Mon 11-02 close (record 814/815)
- [ ] Confirm Sunday 14:55 PT earnings pre-flight reached Slack (PR 809), then record post-live 801/809
- [ ] Swing breakout SIP-level + earnings check + reserved-setup cleanup (PR 764 BUGS_OPEN) — owner approval
- [ ] QHM buy-the-dip redesign — owner approval
- [ ] News-halt whole-word/title-only fix (logs/research/news_venue_halt_1031.md) — owner approval
- [ ] Weekly post-mortem clearer card (worktree agent-acf91ffba51eb242f, uncommitted) — dry-run blocked by hook
- [ ] run_cycle drift-exclude QQQM (noise)
- [ ] Close issue #794 (owner); remaining overdue post-live audits; issues #806/#777

## User Preferences Observed
- If an existing rule decides a case, apply it — don't re-ask.
- Gro is the sanctioned backup reviewer; no follow-up Codex reviews.
- "Adjust for daylight savings" — schedules must follow ET year-round.
- Weekly post-mortem must show where each stock closed, the missed move after exit, and the max-profit exit.

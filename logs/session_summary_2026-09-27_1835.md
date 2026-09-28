# Session Summary — 2026-09-27 18:35 PT
**Project:** alpaca-mtf-bot  **Duration:** full day (multiple usage-limit resumes)

## Shipped
| PR | What |
|----|------|
| #424 | E0 closed-bar gate (data/fetcher.py fetch_closed_bars / ClosedBars) |
| #426 | Tests can never post to real Slack (alerts._under_test guard) |
| #427 | Lab step 1: point-in-time S&P 500 / Nasdaq-100 membership (research/pit_universe.py) |
| #431 | Lab step 2: 10-year daily bars per membership interval (research/lab_bars.py; fetch_bars_window asof) |
| #433 | QHM weekly report posts the FULL report in Slack; no "sym" labels |

## Decisions Made
- Confluence 2.0 scope aligned (daily/weekly swing + 4H timing; closed bars; replace decayed indicators; rates + market-driver indicator; more trades never fewer).
- QHM report must be CEO takeaways, not data: business summary, class-A P/E vs peers, 1-2 sentence news.
- QHM top-3 queued buys at support APPROVED (design record qhm_top3_action_2026-09-27.md); new buys get their own budget so grandfathered LLY/GEV no longer block them; 20% per-name cap stays.
- GEV: Rafael approved selling; he places it himself Monday.

## Corrections (Claude was wrong)
- Proposals were in code blocks → must be plain text.
- Board-prompt "~4x margin" line produced invented buying power in the report → prompts now carry live account figures.

## Findings verified at source
- Alpaca rejects a limit BUY while a sell STOP rests on the same symbol (wash-trade table) — QHM chunks 2-3 must use the stop-safe add sequence.
- QHM entry ladder never reaches chunk 2 (_compute_and_submit_stop sets ACTIVE after chunk 1).
- SEC frames give every filer's annual EPS in one call; FMP peers/screener unusable (premium / unrelated names).

## Open Items
- [ ] Ship QHM report takeaways (logs/wip patch; needs fresh round-2 reviews + preship).
- [ ] Build QHM top-3 buys (risk-path, design record).
- [ ] Operator-triggered "exit this hold" command (Rafael triggers, bot sells) — recommended next after top-3.
- [ ] 34 pre-existing failing tests on main; horizon_state _drop_partial; NVIDIA substitute model id dead.
- [ ] Lab: confirm lab_bars finished (828/838 files), then purged walk-forward harness (Confluence 2.0 E1/E2).

## User Preferences Observed
- If the CEO directs an action, expects it done; Claude's inability to place trades is an Anthropic limit — explain once, offer the bot-side mechanism.
- Wants the report actionable (top 3, affordable share counts, portfolio-aware), not a running list.

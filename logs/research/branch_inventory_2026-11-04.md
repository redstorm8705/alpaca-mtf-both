# Remote branch inventory (2026-11-04, Claude signed)

533 branches on GitHub besides `main` (checked against `origin/main` `8fcd50f`, full history). Nothing was deleted.

| Group | Count | Test | Recommendation |
|---|---|---|---|
| Already merged into main | 426 | `git branch -r --merged origin/main` | Delete — no work is lost |
| Not merged, but every commit's change is already in main | 39 | `git cherry origin/main <branch>` shows no `+` | Delete — no work is lost |
| Open PRs | 5 | GitHub open PR list | Keep: #834, #804, #727, and this session's branch (#811 merged 2026-11-05) |
| Unmerged work, no open PR | 63 | `git cherry` shows commits not in main | Tag as `archive/<branch>` (keeps every commit), then delete the branch |

Within the 63: 6 are F6 feature branches from the Codex lane (`feat/f6-ownership-preflight`, `feat/f6-residual-stop-coverage`,
`feat/f6-pending-buy-reservation`, `feat/f6-typed-kill-verdict`, `feat/f6-bounded-entry`, `feat/live-tier-capital-allocator`)
— confirm with that lane before archiving.

## Branches with unmerged commits (date of last commit, commits not in main, branch) — 63 plus the 5 open-PR branches
- 2026-06-14, 9, `origin/claude/vibrant-cannon-ppodcr`
- 2026-06-16, 11, `origin/claude/ds-audit-bv5-patches-dqqaqm`
- 2026-06-22, 1, `origin/claude/gracious-keller-gcg0s1`
- 2026-06-22, 1, `origin/claude/youthful-wozniak-xwzoua`
- 2026-06-23, 1, `origin/claude/youthful-wozniak-09cp4p`
- 2026-06-23, 3, `origin/claude/youthful-wozniak-j56fj3`
- 2026-06-24, 1, `origin/claude/youthful-wozniak-9lwvk1`
- 2026-06-25, 1, `origin/claude/youthful-wozniak-m5xiuj`
- 2026-06-26, 1, `origin/claude/youthful-wozniak-z8azh0`
- 2026-06-29, 2, `origin/claude/youthful-wozniak-8nbqoi`
- 2026-06-30, 1, `origin/claude/youthful-wozniak-ydygau`
- 2026-07-03, 1, `origin/claude/youthful-wozniak-ibusvt`
- 2026-07-04, 1, `origin/claude/gracious-keller-8tg4fw`
- 2026-07-06, 1, `origin/claude/youthful-wozniak-mo2oqq`
- 2026-07-07, 4, `origin/claude/youthful-wozniak-w249j9`
- 2026-07-09, 3, `origin/claude/youthful-wozniak-bvfh5o`
- 2026-07-10, 1, `origin/claude/youthful-wozniak-ttye4o`
- 2026-07-11, 1, `origin/claude/gracious-keller-uv5gly`
- 2026-07-13, 1, `origin/claude/youthful-wozniak-q3o5p5`
- 2026-07-14, 2, `origin/claude/youthful-wozniak-wgjmgu`
- 2026-07-15, 1, `origin/claude/youthful-wozniak-wsztv1`
- 2026-07-16, 1, `origin/claude/youthful-wozniak-pkptoj`
- 2026-07-17, 1, `origin/claude/youthful-wozniak-6vz73f`
- 2026-07-18, 1, `origin/claude/gracious-keller-v19r71`
- 2026-07-21, 1, `origin/claude/youthful-wozniak-slwohl`
- 2026-07-23, 1, `origin/claude/youthful-wozniak-mnbpr7`
- 2026-07-29, 12, `origin/fix/audit-slack-provenance-gate-2026-07-27`
- 2026-08-03, 1, `origin/docs/durable-sync-2026-08-03`
- 2026-08-03, 2, `origin/claude/youthful-wozniak-x3eazk`
- 2026-08-04, 1, `origin/claude/youthful-wozniak-e6feoy`
- 2026-08-05, 1, `origin/claude/youthful-wozniak-xn51gq`
- 2026-08-05, 2, `origin/claude/youthful-wozniak-wnuo6h`
- 2026-08-06, 1, `origin/fix/killswitch-qhm-intraday-pl-2026-08-06`
- 2026-08-08, 2, `origin/fix/ledger-heal-human-confirmed-2026-08-08`
- 2026-08-10, 1, `origin/claude/youthful-wozniak-jbxrj8`
- 2026-08-11, 2, `origin/claude/youthful-wozniak-ca0j0i`
- 2026-08-13, 1, `origin/claude/youthful-wozniak-h1jnrv`
- 2026-08-14, 1, `origin/claude/youthful-wozniak-jsx12y`
- 2026-08-18, 2, `origin/claude/youthful-wozniak-8bvqqv`
- 2026-08-19, 1, `origin/claude/youthful-wozniak-86ydpl`
- 2026-08-24, 1, `origin/fix/mri-news-noise-wordboundary`
- 2026-08-24, 1, `origin/fix/mri-rate-driven-bond-leg`
- 2026-08-24, 3, `origin/feat/preship-option-c-resilience`
- 2026-08-25, 1, `origin/claude/youthful-wozniak-2x7tzp`
- 2026-09-29, 1, `origin/feat/live-tier-capital-allocator`
- 2026-09-30, 1, `origin/claude/handoff-2026-09-29-allocator`
- 2026-10-03, 1, `origin/claude/c2-breakout-k175`
- 2026-10-05, 2, `origin/docs/day-tier-mechanism-foundation`
- 2026-10-05, 2, `origin/docs/day-tier-zero-commission`
- 2026-10-05, 2, `origin/research/day-tier-strategy-audit`
- 2026-10-05, 3, `origin/research/day-tier-test3-result`
- 2026-10-12, 2, `origin/feat/f6-ownership-preflight`
- 2026-10-13, 2, `origin/feat/f6-residual-stop-coverage`
- 2026-10-14, 4, `origin/feat/f6-pending-buy-reservation`
- 2026-10-15, 1, `origin/claude/extended-hours-adds-wip`
- 2026-10-16, 1, `origin/claude/gai-ladder-live-models`
- 2026-10-16, 1, `origin/claude/qhm-ext-stop-watch`
- 2026-10-16, 2, `origin/feat/f6-typed-kill-verdict`
- 2026-10-17, 1, `origin/claude/handoff-20261017c`
- 2026-10-17, 5, `origin/feat/f6-bounded-entry`
- 2026-10-20, 1, `origin/claude/slack-0930-rq`
- 2026-10-23, 3, `origin/claude/track-a-edge-1023`
- 2026-10-27, 1, `origin/claude/bv5-replay-tool`
- 2026-10-30, 19, `origin/claude/day-search-r34`
- 2026-11-03, 1, `origin/claude/day-orb-design-1103`
- 2026-11-04, 1, `origin/claude/swing-capacity-1104`
- 2026-11-04, 1, `origin/fix/f6-crash-trigger-restoration`
- 2026-11-04, 10, `origin/claude/sharp-mendel-cjiil9`

# Cross-Account Changelog

One human-readable line per **meaningful** change, newest on top, so the other account (Claude or
ChatGPT) can `git pull` → read `handoff.md` → skim this → review quickly. Skip routine
auto-generated commits (report syncs, etc.). Format:

`[YYYY-MM-DD HH:MM PT] <plain-English what changed> — PR #<n> / <short-sha> — <files>`

---

- `[2026-09-19]` Core MTF replay evidence: reconstructed completed-bar VWAP, premarket high, and 12–1 momentum for all 11 parent short entries; AVGO’s losing Sep-04 short entered with +41.80% 12–1 momentum. Research only; no trading behavior changed. — docs/core-mtf-short-replay-evidence — handoff.md, logs/core_mtf_short_replay_intake_2026-09-19.md

- `[2026-09-19]` Core MTF short replay intake: reconciled 30-day short cohort and documented a P0 completed-bar/repainting defect in the live signal path. Research only; no trading behavior changed. — docs/core-mtf-short-replay-intake — handoff.md, logs/core_mtf_short_replay_intake_2026-09-19.md

- `[2026-09-13]` FINDING (open, held for Mon 09-15 open): RTH DAY-stop backstop not re-arming for intraday overnight positions — AAPL/EWY/RIVN had NO exchange stop during 09-12 RTH (software stop only). Bounded gap; root-cause + fix Monday. — logs/tb_audit_log.md
- `[2026-09-13]` Stop-protection (RISK-PATH, board 4-0): orphan_manager now checks the Alpaca calendar before the premarket GTC-stop cancel — a non-trading day is treated like the "closed" phase (validated stops retained, stale ones still cancel+resubmit), fail-open. Fixes weekend/holiday stop-stripping (observed 09-13). — fix/orphan-gtc-nontrading-day — execution/orphan_manager.py
- `[2026-09-13]` QHM Weekly Thesis Slack card: "Board read" now sent in readable per-pick parts (un-truncated; was one 700-char inline block) — fix/qhm-thesis-slack — scripts/qhm_thesis.py
- `[2026-09-13]` Added cross-account coordination prompt for ChatGPT + this changelog — fix/qhm-thesis-slack — logs/chatgpt_coordination_prompt.md, logs/CROSS_ACCOUNT_CHANGELOG.md
- `[2026-09-13]` Handoff ⏩ block synced (3 Slack ships + stop-protection & day-tier as next pick-ups) — PR #301 / `0e32185` — handoff.md
- `[2026-09-13]` Muted the autonomous-patch "No action needed" Slack ping (else branch logs instead of paging) — PR #300 / `1ef74bc` — autonomous_patch_generator.py
- `[2026-09-13]` Realized · Close card redesign: account-day P&L headline, compact per-tier rows, dated header — PR #299 / `6b7db31` — scripts/pnl_snapshot.py
- `[2026-09-13]` Weekly Post-Mortem card redesign: drop long/short arrow, "—" for empty exit-reason, sort worst→best, "left on table" — PR #298 / `ca63216` — weekly_postmortem.py

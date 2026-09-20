# Cross-Account Changelog

One human-readable line per **meaningful** change, newest on top, so the other account (Claude or
ChatGPT) can `git pull` → read `handoff.md` → skim this → review quickly. Skip routine
auto-generated commits (report syncs, etc.). Format:

`[YYYY-MM-DD HH:MM PT] <plain-English what changed> — PR #<n> / <short-sha> — <files>`

---

- `[2026-09-20 12:39 PT]` **ChatGPT/Codex:** canonical April-forward Core MTF intake merged and synced to OCI: 76 broker-exact entry parents (48 long, 28 short), 0/52 legacy ledger rows with exact ownership proof; no exit/P&L claims. Two cold-reject rounds fixed five integrity defects; Board+Groq+Google AI+NVIDIA+cold+CI passed. — PR #358 / `b3e7357` — research/core_mtf_canonical_entries.py, tests/test_core_mtf_canonical_entries.py

- `[2026-09-19 16:00 PT]` Core MTF fail-closed OHLCV replay merged: source-bound simulation quarantines both same-symbol overlap parents and forbids execution claims; Board, Groq, Google AI, NVIDIA, and CI passed. — PR #351 / `1dd30c4` — research/core_mtf_simulated_replay.py

- `[2026-09-19 15:35 PT]` Core MTF bounded broker-order correlation merged: 9 full and 2 partial uniquely time-correlated legacy sell orders, but zero confirmed short-parent mappings; intent remains explicitly unknown. BGGN and CI passed; research-only. — PR #349 / `3e4d63b` — research/core_mtf_broker_entry_correlation.py

- `[2026-09-19 15:20 PT]` Core MTF broker-order research foundation merged: a read-only, hashed Alpaca order-history snapshot records retrieval provenance and forbids ledger-event identity claims; Board, Groq, Google AI, NVIDIA, and CI passed. No OCI deployment or trading behavior change. — PR #347 / `76274aa` — research/core_mtf_alpaca_order_snapshot.py

- `[2026-09-19 15:05 PT]` Core MTF exit evidence merged: exact parent/order identity is now required for VERIFIED; historical text/quantity matches remain ledger-correlated only (5 full, 2 partial, 4 unmatched). Board, Groq, Google AI, NVIDIA, and preship passed. — PR #345 / `94bc402` — research/core_mtf_observed_exits.py

- `[2026-09-19 14:56 PT]` Core MTF replay research foundation merged: SHA-bound/UTC-normalized entry extraction plus a content-hashed Alpaca bar snapshot adapter; Board, Groq, Google AI, NVIDIA, and preship passed. Research-only, no OCI deployment or trading behavior change. — PRs #340–#343 / `7785895` — research/core_mtf_point_in_time_replay.py, research/core_mtf_replay_events.py, research/core_mtf_alpaca_bar_snapshot.py

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

# Cross-Account Changelog

One human-readable line per **meaningful** change, newest on top, so the other account (Claude or
ChatGPT) can `git pull` → read `handoff.md` → skim this → review quickly. Skip routine
auto-generated commits (report syncs, etc.). Format:

`[YYYY-MM-DD HH:MM PT] <plain-English what changed> — PR #<n> / <short-sha> — <files>`

---

- `[2026-10-08 late PT]` **OpenAI Codex:** extended Claude’s PR #526 tier-name foundation with canonical internal IDs and strict legacy-state adapters; ownership and reports now reject alias collisions and malformed attribution. Rebased over Claude PRs #522–#536, with no parallel registry or trading-logic replacement. — PR #535 / `cf0e190` — tier names, ownership snapshot, dashboard/monthly/shared HTML, tests, design record

- `[2026-10-07 late PT]` **OpenAI Codex:** shipped a signed cross-tier ownership snapshot and wired the day-tier no-co-hold route to it; ambiguous or inconsistent broker/ledger/lifecycle state now fails closed to foreign-held. BGGN, CI, OCI compile/import, and live read-only reconciliation passed; no signal, sizing, stop, exit, or order behavior changed. — PR #520 / `bf81288` — ownership snapshot, day logger, day runner, tests, design record

- `[2026-10-06, in review]` **ChatGPT/Codex:** restored the full day-tier lifecycle preflight to the shipped order-ID, journal, risk-sizing, and tier-kill contracts. Combined Track M/router/context/Swing/Track A/Track B/cap/kill simulation: 230 passed plus 5 parameterized cases; production code unchanged. — `test/day-tier-full-lifecycle-preflight` — two test modules and design record

- `[2026-10-06]` **ChatGPT/Codex:** shipped the admitted-family router into Track M only and added versioned family tags to the live swing-breakout lifecycle. Track A/B isolation simulations, malformed-allocation regressions, Board 2/2, Groq, Google AI Studio, mechanical review, CI, and OCI no-order probes passed. Failed swing-score features, rejected ORB, and blocked GEX remain excluded. — PR #493 / `086eafe5` — Track M, runner, swing manager, tests, design record

- `[2026-10-06]` **ChatGPT/Codex:** added direction-aware day-tier implementation-shortfall attribution and a fail-closed, evidence-versioned family router with timestamped score freshness. It consumes Claude's admission decisions and is not wired to order execution or account-tier budgets. Board 2/2, Groq, Google AI Studio, mechanical preship, and CI passed after three reject/fix rounds; no service restart because the modules are not execution-wired. — PR #491 / `9a826d6d` — cost reducer, admission registry/router, tests, design record

- `[2026-10-04 21:57 PT]` **ChatGPT/Codex:** built an additive day-tier mechanism evidence layer: ten independent evidence states, validated as-of provenance, explicit unknowns, versioned family/digest tags on decisions and lifecycle fills, and fail-safe restart recovery by trade ID. BGGN passed after two reject/fix rounds; no signal, sizing, allocation, entry/exit, or routing behavior changed. Pending Claude adversarial audit; not merged or deployed. — `feat/day-tier-mechanism-foundation` — mechanism context, logger, tests, design record

- `[2026-10-04 21:50 PT]` **ChatGPT/Codex:** defined the day tier's ten-mechanism foundation and build order, explicitly separating independent hypothesis families from static confluence scoring; OFI and shared mechanism tagging are first. No live behavior changed. — PR #478 / `65e2ba2` — design record and handoff

- `[2026-10-04 21:38 PT]` **ChatGPT/Codex:** corrected day-tier research language to distinguish Alpaca's zero equity commission from spread/slippage/regulatory friction and reran Test 3 at zero commission and zero modeled friction; it still failed with +0.017 bp/trade, SR .015, DSR .031, and bootstrap p=.477. No live behavior changed. — PR #477 / `d987d4b` — research test, design record, handoff

- `[2026-10-04 21:25 PT]` **ChatGPT/Codex:** executed the pre-registered JFE late-day hedging-demand momentum test once on 1,183 SPY/QQQ SIP dates; it failed all admission gates and is recorded as rejected, with the exact reproducible research script. No live behavior changed. — PR #476 / `23f0530` — research test, design record, handoff

- `[2026-10-04 21:07 PT]` **ChatGPT/Codex:** audited primary intraday-strategy research against Claude's completed SIP tests; rejected HFT/ORB scaling on the current stack and pre-registered the distinct JFE late-day hedging-demand momentum test with cost, leakage, correlation, DSR, and concentration gates. No live behavior changed. — PR #475 / `f64f9dd` — design record and handoff

- `[2026-09-29 08:27 PT]` **ChatGPT/Codex:** shipped and deployed the shared live tier-capital allocator with dynamic regime limits and fail-closed wire-time admission across Swing, Day Trade, QHM, and F6. Board/mechanical/GAI/CI passed; Groq received Rafael’s one-time waiver; services and live config verified. — PR #442 / `d1148a9` — allocator, four entry integrations, broker recovery, QHM safety, tests, design record

- `[2026-09-27 17:05 PT]` **ChatGPT/Codex:** revamped all five generated HTML operator pages around current tier truth; preserved weekly/0DTE recommendations, added one clear primary action per horizon, exact ownership labels, and account-vs-lifecycle reporting. BGGN/UX/adversarial/CI passed; live pages and three OCI services verified. — PR #432 / `9c8623f` — dashboard/scanner/options/weekly/monthly generators, shared HTML UI, tests

- `[2026-09-27 12:10 PT]` **ChatGPT/Codex:** rebuilt Strategy Edge as a fail-closed, broker-authoritative lifecycle report for Confluence 2.0 to consume; no trading behavior changed. Board/adversarial/BGG gates, CI, OCI tests, live ledger render, and service health passed. — PR #429 / `aebc8d1` — reporting/pnl_ledger.py, reporting/report_figures.py, monthly_review.py, tests, design record

- `[2026-09-26 17:58 PT]` **ChatGPT/Codex:** fixed Day Tier actual-fill exit geometry and fail-closed reducer recovery; removed the false weekly “dollars left on table” aggregate; full BGG + CI passed and OCI deploy verified. — PR #411 / `42a6246` — execution/day_trade_manager.py, weekly_postmortem.py, tests, design record

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

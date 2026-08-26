# alpaca-mtf-bot — PROJECT STATE (2026-08-25, cross-account handoff)

## Cross-account pickup (new Claude Gmail account)
`git pull` → main is at `8e3e59b`. Then read this state + `logs/design_records/agent_remediation_plan_2026-08-25.md`.
NOTE: `handoff.md`'s top pointer is STALE (its update is blocked by the claim gate false-rejecting a true summary — see bottom). Trust THIS Master Brain state + the git log commit messages, which carry the truth.

## Shipped tonight (both merged to main; tooling only — no OCI restart)
- **#175** (merge `54a620a`) — ROOT-CAUSE GAI FIX. The multi-day "GAI outage" was a DEAD MODEL ALIAS, not an outage: `gemini-flash-latest` (alias) = 0/3 dead pool, `gemini-2.5-flash` = 404 RETIRED, `gemini-3.5-flash` = 3/3. Pinned `gemini-3.5-flash` + `thinkingConfig.thinkingBudget:0` (thinking model was eating the token budget → blank verdict) in BOTH `.claude/preship/preship_audit.py` and `.github/scripts/ci_audit.py`.
- **#176** (merge `8e3e59b`) — Gate 4: `.claude/preship/gai_health.py` — canary (CI `preship` step blocks ONLY on deterministic CONFIG_BUG: retired pin / thinking-budget; a transient congestion OUTAGE passes) + `pick_working_gemini` self-healing selector. Wired into `.github/workflows/preship-verify.yml`. Substitute now engages only on a proven Gemini-wide outage.
- **#174** (merge `179898d`, earlier tonight) — council-resilience foundation (NVIDIA substitute + `GEMINI_ALLOW_PAID` default-deny paid lock), split from #169.

## GAI status
Intermittently recovering (near free daily quota). Canary passes; back-to-back calls still 429. The NVIDIA substitute legitimately carries any preship that 429s. Enable paid ONLY via `GEMINI_ALLOW_PAID=1` (Rafael-only).

## Ready to re-land (non-GAI)
- **Gate 1+2 — dynamic-derivation scanner** (`no_static_scan.py` + `_dynamic_ok`): BUILT + TESTED + preship-PASSED, STASHED on branch `feat/preship-option-c-resilience` as `gate-1+2-remediation-WIP` (stash@{0}). Re-land off fresh main.

## Open PRs
- #173 (MRI rate-driven bond leg) — superseded by the coming MRI dynamic rebuild.
- #170 (MRI word-boundary matcher) — PARKED, do not merge.
- #169 (option-C) — foundation was split to #174 (merged); #169 now carries ONLY its un-merged Self-QA gate suite (design-record + no-guess gates) — resubmit those as per-gate diffs.

## NEXT (Rafael #1 priority, in order)
1. Finish the 10-gate remediation (`logs/design_records/agent_remediation_plan_2026-08-25.md`): re-land Gate 1+2 (stashed), then Gates 3/5-10; re-land #169's design-record + no-guess gates as per-gate diffs. Deferred: wire `pick_working_gemini` into `preship_audit.py`'s live substitute path.
2. THEN the MRI dynamic-driver rebuild: SPY ±1% days since 2022, financial-news-CONFIRMED drivers (not guessed) BOTH directions, dynamically derived (through the DDP gate).

## Trading/product backlog (design-stage, need design gate + Rafael go)
Dip-add-into-earnings (Rafael's sequenced item; he did NVDA manually at ~$209 tonight), QHM stop noise-filter (9/9 sustained breach before stop, risk-path), 1.5% max-daily-loss circuit breaker (risk-path), Slack messaging overhaul (broken/cut-off rendering), QHM research→auto-execution pipeline (big), observability/ops diagnostic.

## Key learnings (memory saved: reference_gai_model_pin_and_canary.md)
- A Gemini 503/429 is usually a DEAD MODEL ID, not an outage — model-list 200 = service up; never use `-latest` aliases; pin a family version + thinkingBudget:0.
- The claim gate (GATED_CLAIM_FILES = handoff.md) false-rejects true summaries via the weak substitute during a GAI outage AND via Gro — a gate-improvement item (allow git-verifiable references, or skip substitute for claim files).

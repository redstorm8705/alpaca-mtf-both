# OpenAI Codex branch triage — 2026-10-11

**Author/signature:** OpenAI Codex (OpenAI Codex)

Compared every requested remote branch with `origin/main` after `git fetch --all --prune`.

## Already merged/closed

- `feat/canonical-tier-language` — PR #535.
- `docs/canonical-tier-handoff` — PR #538.
- `feat/cross-tier-ownership` — PR #520.
- `docs/codex-ownership-handoff` — PR #521.
- `feat/live-tier-capital-allocator` — PR #442; live allocator later disabled by #468,
  so the historical branch must not be replayed.
- `docs/chatgpt-allocator-handoff` — PR #444.
- `feat/html-ux-current-state` — PR #432.
- `research/day-tier-test3-result` — PR #476.
- `research/day-tier-strategy-audit` — PR #475.
- `docs/day-tier-zero-commission` — PR #477.
- `docs/day-tier-mechanism-foundation` — PR #478.
- `fix/killswitch-f6-exclusion-2026-08-06` — PR #99.
- `fix/killswitch-phantom-fills` — PRs #327 and #328.
- `fix/killswitch-qhm-intraday-pl-2026-08-06` — PR #98.
- `fix/gemini-model-pin` — PR #175.
- `fix/gemini-model-unbreak-2026-08-08` — PR #104.

Squash merges explain why some remote branch tips are not ancestors of main; GitHub PR state
and patch equivalence confirm closure.

## Superseded/closed

- `fix/mri-news-noise-wordboundary` — PR #170 was already closed and explicitly parked.
- `fix/mri-rate-driven-bond-leg` — PR #173 closed during this audit. It was an eight-week-old,
  fixed-threshold half of an abandoned MRI redesign, with no current replay or post-live
  contract. A new rate signal must be dynamic and pass the current gate.

## Still owed queue after this audit

- Weekly post-mortem tier/lifecycle split: implemented in the companion diff.
- Strategy Edge Report rebuild: already live in PR #429; no duplicate work.
- F6 shared-symbol go-live: still disabled and blocked by its safety plan plus Claude's
  in-progress bracket-add/shared-stop work; do not duplicate that work.
- Entry lab step 2: still owed after the report ship.
- Exit redesign: still owed after entry evidence, per the accepted sequencing record.

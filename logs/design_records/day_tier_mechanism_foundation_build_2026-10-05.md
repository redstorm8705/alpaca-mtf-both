# Day-tier mechanism evidence build — 2026-10-05

**Owner/signature:** ChatGPT/Codex

**Status:** Implemented and BGGN-approved on `feat/day-tier-mechanism-foundation`; pending Claude adversarial audit before merge/deploy.

**Behavioral scope:** Logging and attribution only. Claude's day-tier signals, thresholds, sizing, allocation, entry/exit decisions, and order routing are unchanged.

## Problem

The day tier already writes its raw decision, trigger, size, fill, and exit events, but those records do not have a stable hypothesis-family identity or a consistent statement of which professional market mechanisms were actually observed at decision time. That makes later walk-forward attribution ambiguous and encourages falling back to another static confluence score.

## Build

`strategy/day_tier_mechanism_context.py` converts the values already present in the decision/trigger/size payload into an evidence object. It creates one independent record for each of the ten declared mechanisms. Missing inputs are explicitly `UNKNOWN`; partial inputs are `PARTIAL`; a mechanism becomes `OBSERVED` only when its declared minimum fields and a parseable, timezone-aware upstream feature/bar as-of timestamp are present. The timestamp is normalized to UTC and must not be later than the immutable logger capture timestamp. Boolean, blank, malformed, timezone-naive, and future values fail closed to `PARTIAL`. Logger wall-clock time is labeled only as capture time and is never used as evidence source time. The adapter performs no network, broker, market-data, order, sizing, or configuration call.

Every context carries:

- a versioned `family_id` and `hypothesis_version`;
- ten independent mechanism states and their observed values;
- a stable SHA-256 `context_digest` over canonical JSON;
- an explicit risk delta of zero for size, frequency, and concurrency;
- a build status so malformed/non-finite input degrades to an all-unknown record instead of affecting trading.

`strategy/day_tier_logger.py` adds the full context to each decision and copies compact family/version/digest tags to entry, partial-exit, and exit fills. Tags are cached during a process lifetime. After a restart, the logger resolves them from the original durable decision record by `trade_id`. A lookup failure produces an `unclassified_v1` tag and never raises into the trading path.

The current family mapping describes the strategies already implemented by Claude:

| Existing path | Family ID |
|---|---|
| Track A, FADE | `gex_wall_fade_v1` |
| Track A, RIDE | `gex_wall_ride_v1` |
| Explicit Track B (including DRIVE/PULLBACK/ORB) | `orb_break_hold_v1` |
| Anything not provably classified | `unclassified_v1` |

Mode-name substrings never promote Track A into Track B; an unrecognized Track-A mode remains unclassified. These tags describe observations. They do not admit a new strategy or cause any family to trade.

## Failure direction

- Context construction failure: emit `build_status=ERROR`, all mechanisms `UNKNOWN`, zero risk delta.
- Entry/exit tag lookup or malformed stored context: emit `unclassified_v1` with an empty digest and still write the lifecycle event.
- Decision write failure: do not cache tags for a decision that was never durable.
- Durable log write failure: preserve the logger's existing alert and never-raise behavior.
- Process restart: recover tags from the fsync-backed JSONL decision event.
- Missing feature: record `UNKNOWN`; never substitute zero, false, or a guessed market state.

## Verification

`tests/test_day_tier_mechanism_context.py` covers family separation, missing-data honesty, deterministic digests, malformed input, zero risk delta, no cache entry for WAIT decisions, and durable tag recovery across a simulated restart.

Final focused result: `12 passed`; the legacy durable logger stamp regression also passed. Python compilation, Ruff correctness checks, `git diff --check`, and the repository preship gate pass. A broader pre-existing day-tier selection produced 72 passes and 23 failures; the failures are in unchanged sizing/order tests and reproduce signatures unrelated to this logging diff (including an existing mock that does not accept `client_order_id`). They are not represented as passing evidence for this build.

## BGGN result

The first two Board rounds rejected the build. The findings were concrete and fixed before external review:

1. Logger wall-clock time was mislabeled as decision time and session buckets were inferred without source provenance.
2. Track-A family matching could promote mode substrings into the Track-B ORB family.
3. Mechanism evidence could be marked observed without an upstream as-of timestamp.
4. Malformed durable context could suppress an exit log, and a failed decision write could leave non-durable cached tags.
5. A merely non-null as-of value could be blank, boolean, malformed, timezone-naive, or future-dated.

Exact code tree `adabcd7cd132ca033b6ef59bdb13bf467b6e9d4e` passed both available Board seats. Google AI Studio and Groq returned `APPROVE` for the final code. An earlier Groq pass required exact-diff chunking after a per-minute limit; the final one-line Ruff correction was reviewed directly by both providers. Nvidia was not used. The code-file audit markers are SHA-bound to the staged contents.

## BGGN questions

1. Does any changed line modify the probability, size, concurrency, side, timing, or routing of an order?
2. Can context/tag construction raise, block, or corrupt the existing durable lifecycle record?
3. Does any mechanism state claim observation without the required point-in-time input?
4. Are family tags stable and recoverable across restart without becoming a second source of trading truth?
5. Is the change suitable to hand to Claude for an independent adversarial audit before merge/deploy?

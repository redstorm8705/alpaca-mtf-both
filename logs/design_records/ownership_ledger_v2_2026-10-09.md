# Ownership ledger schema v2 — canonical durable tier identities

**Author:** OpenAI Codex (GPT-6)
**Date:** 2026-10-09
**Status:** DESIGN DRAFT — implementation blocked on BGGN review

## Problem

The bot has one canonical four-tier vocabulary: `day`, `swing`, `qhm`, and `forever_6`. The ownership ledger still persists historical keys (`daytrade`, `intraday`, `qhm`, `forever6`). That mismatch lets new code accidentally treat aliases as separate owners, complicates reconciliation, and makes durable state ambiguous to humans and agents.

## Scope and boundary

This increment changes the ledger serialization boundary only. It does not alter entry logic, exit logic, order prefixes, risk limits, capital targets, protected-floor rules, fill attribution, or Claude's Confluence 2.0 work. Existing `DT`, `IN`, `QH`, and `F6` broker order prefixes remain stable protocol identifiers.

## Proposed schema

On disk, schema v2 stores exactly these tier keys in every position: `day`, `swing`, `qhm`, `forever_6`. The root `version` becomes `2`.

`load_ledger()` accepts schema v1 or v2, validates it strictly, and returns the current legacy-shaped compatibility view used by existing execution code. A new `load_canonical_ledger()` returns canonical v2 keys for new consumers. `save_ledger()` accepts either a complete v1 compatibility object or a complete v2 canonical object and rejects mixed aliases and malformed values. Before explicit migration it preserves the current on-disk schema, so deploying dual-read code cannot silently migrate state through a routine reconciliation. After migration, the on-disk v2 schema is preserved by normal saves. A newly created or absent ledger defaults to v1; only the explicit migration command may select v2.

Root `version` is authoritative and must be an exact non-boolean integer 1 or 2. Every v1 position has exactly `{daytrade,intraday,qhm,forever6}`; every v2 position has exactly `{day,swing,qhm,forever_6}`. Unknown, extra, missing, mixed-family, cross-position mixed, and duplicate JSON object keys are rejected. Compatibility reads return a fresh deep v1 object with `version=1`; canonical reads return a fresh deep v2 object with `version=2`; saves never mutate their input. Full candidate validation completes before current state or the operational backup changes.

The strict field grammar is: root requires `version`, `last_reconciled_utc`, and `positions`; `last_reconciled_utc` is null or a string; position symbols are non-empty uppercase strings equal to their stripped form; every position requires finite non-boolean `alpaca_net_qty`, `tiers`, and finite non-boolean `drift`; every claim requires finite non-boolean `qty`, finite non-boolean `avg_cost`, and `last_fill_id` that is null or a string. Unknown extra fields at root, position, or claim level are preserved byte-value/type through conversion rather than interpreted. Tier maps alone reject extras because their key set is exact. JSON duplicate keys are rejected before object validation, including duplicates in nested objects.

## Locked invariants

1. The broker position remains ground truth; migration cannot change any quantity, cost, drift, timestamp, symbol, or fill identifier.
2. The protected floor before and after migration must be identical for every symbol.
3. The sum of all four ownership claims before and after migration must be identical for every symbol.
4. Mixed canonical/legacy aliases for the same owner fail closed; they are never added, merged, or chosen by precedence.
5. Unknown tier keys, missing tier keys, booleans, nonnumeric or nonfinite quantities, and malformed entries fail closed.
6. Existing execution consumers continue receiving the legacy compatibility view until each is separately migrated. No broad trading-path rename is part of this change.
7. The operational last-known-good backup may use v1 or v2 on disk, but it is always validated and converted to the legacy compatibility view before the guard reads it. A v2 backup must preserve the Forever 6 floor in fallback mode.
8. Routine writes remain atomic. Migration apply uses a separate mandatory, fail-closed exclusive lock held from the raw read through archive creation, write, reload, and postcondition checks. It never inherits the routine lock's timeout-and-proceed-unlocked behavior.
9. Migration creates an immutable timestamped archive of the exact pre-migration bytes. Before replacing the ledger, it persists and fsyncs a prepared manifest containing the preimage SHA-256 and deterministic intended postimage SHA-256. After verified replacement it marks the manifest applied. There is no migration-complete marker; the validated current ledger version is the sole schema selector. Operational backup rotation is separate and cannot replace this archive. Crash recovery accepts rollback only when current bytes equal the prepared postimage hash; any other hash refuses. Normal rollback follows the same compare-and-swap rule so a later sync or protected-floor update cannot be erased. No broker orders are sent by migration.
10. Every `(symbol, canonical tier)` quantity, `avg_cost`, and `last_fill_id` must match before and after conversion. Aggregate equality cannot substitute for per-owner equality.
11. Migration apply and rollback fsync the written file and containing state directory after each atomic replace, then reload and verify hashes and ownership invariants before reporting success.
12. Any failure after replacement but before final verification triggers automatic compare-and-swap restoration of the archived preimage under the same exclusive lock. The command verifies the restored hash and exits nonzero. It never releases an unverified v2 ledger. An already-valid v2 apply is a true no-op: no bytes, mtime, archive, manifest, or backup changes.
13. Migration uses a dedicated cross-process fence. Every `save_ledger()` holds a shared lock on that fence across validation, backup rotation, and replacement; migration apply/rollback holds the exclusive lock. This is separate from the legacy best-effort writer lock, so even a routine writer that times out and proceeds cannot write during migration. Direct save, reconcile, sync, and authorized-reduction race tests prove no routine write lands inside the transaction.
14. Dry-run and apply refuse an absent ledger. A valid present-but-empty v1 ledger may migrate; absence can never be interpreted as an empty account or used as a v2 preimage.

## Migration process

A dedicated command performs: read-only validation, canonical conversion in memory, invariant comparison, JSON preview, and SHA-256 reporting. Mutation requires an explicit `--apply` flag and a mandatory exclusive lock. While holding that lock it creates and fsyncs an immutable timestamped byte-for-byte archive, persists a prepared manifest with the preimage and deterministic intended-postimage hashes, writes atomically, fsyncs the state directory, reloads into exact canonical and compatibility views, compares the complete objects (including reconciliation timestamp and preserved extension fields), repeats every invariant, and marks/fsyncs the manifest applied. Any post-replace failure automatically CAS-restores and verifies the archive before releasing the lock. If exclusivity cannot be acquired or the archive/manifest cannot be made durable, it exits without modifying the ledger. Crash recovery and rollback require the manifest's intended postimage hash to match the current file before restoring and fsyncing the original bytes. It sends no broker orders and makes no network calls.

Deployment order:

1. Before deployment, prevalidate the exact live ledger and backup bytes with the new strict parser in a read-only command and record their hashes.
2. Pause every ledger writer and sync launcher. Ship dual-read/schema-preserving-write code and tests. Routine writers must prove they leave the v1 schema as v1 before migration.
3. On OCI, confirm the paused files still match the prevalidated hashes, then run dry-run and record hashes/invariants.
4. Apply once only if dry-run is clean.
5. Before any writer or service resumes, run both loader views, per-owner invariant checks, ownership snapshot, protected-floor checks, and file-hash verification. Raw byte rollback is permitted only in this locked pre-writer window and only when compare-and-swap succeeds.
6. Resume services, then run ledger sync and service-health verification. Once any writer changes the post-migration hash, raw byte rollback is permanently prohibited because it could erase fills or a newly raised floor.
7. A problem discovered after writers resume uses a forward-only, lock-held repair: stop writers, load the latest v2 ledger, validate it against broker truth and the immutable migration manifest, convert the latest state to the required schema without changing per-owner claims, write atomically, and re-run all checks. Any ownership mismatch or protected-floor ambiguity fails closed and requires the existing operator-confirmed heal procedure; it is never resolved by restoring stale bytes.

## Tests required

- v1 to v2 round trip preserves every field, all four complete tier keys, and every per-tier quantity/cost/fill identifier.
- parser matrix covers exact versions, duplicate JSON keys, mixed families within and across positions, unknown/extra/missing tier keys, required known fields, preserved unknown fields, symbol form, last-fill/timestamp types, booleans, and nonfinite values.
- v2 load to legacy compatibility view preserves execution behavior.
- v1 and v2 operational backups protect the same symbols and floors, including a direct Forever 6 fallback test.
- mixed aliases, unknown/missing tiers, booleans, NaN/Infinity, malformed claims, and unsupported versions are rejected.
- failed writes do not replace the current ledger or its valid backup.
- existing ownership, ledger-sync, broker-floor, orphan, allocator, snapshot, and dashboard suites remain green.
- migration dry-run changes no file or mtime and absent-ledger dry-run/apply refuse; pre-migration routine saves remain v1 and post-migration saves remain v2; already-v2 apply is a true no-op; shared/exclusive-fence races cover direct save, reconcile, sync, and authorized reduction; lock contention and archive/manifest/write/fsync/replace/reload/invariant failures leave either old-valid or new-verified state; post-replace failures automatically restore and verify the preimage; prepared-manifest crash recovery is hash-safe; apply→rollback→delete/recreate defaults safely to v1; successful pre-writer rollback restores the archived byte stream and matching SHA-256; an intervening sync/update makes raw rollback refuse and requires the forward-only repair path; apply and rollback directory durability calls are covered.

## Required mechanical gate

This is safety-critical execution state. The change requires full-read evidence for `execution/ownership_guard.py`; RC1–RC8 analysis; execution-risk, data-integrity, and masked-loss board seats; Groq and Google AI Studio pre-code and exact-final review (configured Nvidia backup only when a provider is unavailable); cold-second and adversarial review; design/provenance/preship/log-exemption markers on every gated file; Python 3.10 compilation; Ruff E/W/F/B; mypy; diff-check; provenance scan; and focused plus adjacent regression suites for ownership, snapshot, OCO attribution, ledger sync, broker stop replacement, orphan handling, Swing breakout, allocator, dashboard, reporting, and migration failure injection.

## Relationship to Claude's work

This is a storage-boundary extension of Claude's `tier_names.py` authority. It does not replace Claude's trading mechanisms, Confluence 2.0, entry work, post-live audit work, or allocator behavior. New code imports the existing canonical registry; it creates no second tier registry.

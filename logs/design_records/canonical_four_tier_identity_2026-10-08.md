# Canonical four-tier identity foundation — 2026-10-08

**Author/signature:** OpenAI Codex
**Scope:** vocabulary and compatibility foundation; complementary to Claude's shipped trading logic.

## Contract

The bot has exactly four strategy tiers:

| Canonical ID | Display label | Historical persisted key accepted on read |
|---|---|---|
| `day` | Day | `daytrade` |
| `swing` | Swing | `intraday` |
| `qhm` | QHM | `qhm` |
| `forever_6` | Forever 6 | `forever6` |

“Intraday” may describe a timeframe, bar, return, or market session. It is not a tier. Day Tracks A/B/M are mechanisms within Day. Swing breakout is a mechanism within Swing.

## This increment

1. Extends Claude’s shipped `tier_names.py` as the single canonical ID/label registry; no parallel registry remains.
2. Makes `OwnershipSnapshot` expose canonical claims and accept canonical or historical tier arguments.
3. Reads both historical and canonical ledger keys during migration, but rejects a row containing both aliases for the same tier so quantities can never double count.
4. Makes the Day runner query ownership with canonical `day`.
5. Changes shared HTML and monthly labels to Day, Swing, QHM, and Forever 6.

## Compatibility and boundaries

The live ownership ledger and broker client-order prefixes remain unchanged in this increment. Historical state is translated only at the ownership-snapshot boundary. Claude's entry, exit, stop, adoption, close-timing, Confluence, and order-tag implementations are untouched. A later separately gated migration will introduce canonical durable writes and atomically upgrade state before historical aliases are removed.

## Failure posture

Unknown tiers, duplicate canonical/legacy aliases, malformed claims, lifecycle uncertainty, or broker uncertainty keep exclusivity unprovable. They cannot make a position look exclusively owned.

## Validation

- 41 focused identity and ownership tests pass.
- Python compilation passes.
- Ruff E/W/F/B passes.
- `git diff --check` passes.
- BGGN verdicts are recorded in `logs/tb_audit_log.md` before shipping.

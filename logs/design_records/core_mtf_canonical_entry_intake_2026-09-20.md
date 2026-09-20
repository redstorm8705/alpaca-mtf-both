# Core MTF canonical entry intake — 2026-09-20

**Author:** ChatGPT / Codex (`research/core-mtf-canonical-intake`)

## Problem

The April-forward Alpaca order snapshot contains a broader Core MTF history than
`trade_events.jsonl`, but the existing research tools either start in July or correlate
records by symbol and time. Symbol/time proximity cannot prove trade ownership when tiers
share a ticker or when an order may be an entry, stop, or exit.

## Scope

Add one research-only intake that emits a source-bound cohort of broker-exact Core MTF
entry parents. It does not import execution code, call Alpaca, alter a signal, size an
order, or calculate P&L.

An Alpaca order is admitted only when all of these facts are present and consistent:

- unique immutable broker order ID;
- client order ID in the historical `mtf-` namespace;
- explicit `position_intent` of `buy_to_open` or `sell_to_open`;
- order side agrees with that intent;
- market or limit order type;
- timezone-aware submission and fill timestamps;
- positive filled quantity and average fill price.

Canceled or partially filled parents remain visible and are labelled partial; the intake
does not silently turn them into completed entries. Every rejected `mtf-` candidate is
counted by reason.

The event ledger contributes ownership only when a row explicitly names the Core MTF tier
and carries an exact entry broker order ID that matches an admitted broker parent. Existing
legacy rows that only say `trade_mode=intraday` do not qualify. Duplicate cross-source rows
are joined only by exact broker order ID.

## Output contract

The artifact records full-file SHA-256 hashes, source ranges, inclusion predicates, row
counts, exclusion reasons, and provenance per parent. It sets
`execution_claims_permitted=false`, `outcome_claims_permitted=false`, and
`selection_permitted=false`. Any duplicate broker ID, malformed timestamp, invalid numeric
field, contradictory intent/side pair, or source-hash mismatch fails closed.

## Ten-point audit

1. **Correctness:** exact identifiers and intent/side consistency replace proximity joins.
2. **Failure behavior:** malformed or contradictory source data raises; no partial artifact.
3. **Data provenance:** complete source files and canonical order arrays are hashed.
4. **Ownership:** broker ID is the only cross-source join key.
5. **Quantity:** positive finite filled/requested quantities are preserved; partials remain partial.
6. **Time:** all admitted timestamps parse as timezone-aware and normalize to UTC.
7. **Persistence:** output uses a same-directory temporary file then atomic replace.
8. **Trading impact:** none; module lives under `research/` and imports no execution module.
9. **Statistical claims:** prohibited; the artifact describes a cohort and no outcomes.
10. **Interconnection:** the output is intended for lifecycle replay, trial accounting, and future
    walk-forward datasets; it cannot authorize a score or threshold change by itself.

## RC scan

- **RC-1 calendar/time:** no market-calendar inference; timezone-aware timestamps required.
- **RC-2 paths:** caller-supplied paths only; no CWD-relative production state.
- **RC-3 exceptions:** no silent exception paths.
- **RC-4 Slack:** none.
- **RC-5 persistence:** atomic sidecar replace.
- **RC-6 joins:** exact broker order ID only.
- **RC-7 rounding:** no share rounding or sizing.
- **RC-8 mutable strategy state:** none.

## Board result

PASS for this research intake with the contract above. The board separately requires a
point-in-time universe of accepted and rejected candidates before any admission rule can be
evaluated; executed entries alone are not sufficient for walk-forward selection.

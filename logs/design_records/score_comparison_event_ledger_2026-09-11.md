# Score-Comparison Event Ledger — Design Record (2026-09-11)

## Problem and evidence

`strategy/signal_generator.py::run_scan()` calculates full 12-point and 16-point comparison rows on every
scan, then atomically overwrites a date-keyed snapshot. `scripts/score16_aggregator.py` archives only the
last snapshot for each `(date, symbol)`. OCI evidence on 2026-09-11: the history held 1,472 rows, but it
cannot recover per-scan divergence or create a counterfactual trade lifecycle. The DSR review therefore
correctly left DSR unavailable rather than treating IC predictor columns on the same 21 realized trades as
independent strategy returns.

## Bounded increment

After the existing snapshot replacement succeeds, append one complete scan event to
`logs/score_comparison_events.jsonl`. The event contains `schema_v`, the existing Phase-3 scan timestamp,
trade mode, universe size, and the already-computed comparison rows. The append flushes and fsyncs once.
Failure only emits a warning; `run_scan()` returns its existing signal list unchanged.

The increment adds no market-data or broker calls, execution imports, entry/exit/sizing/concurrency logic,
virtual trades, return calculations, or DSR output. It is an evidence capture step only.

## Integrity contract

There is one writer: the scan process. An event is a single JSON line; a crash can leave a torn final line,
which future readers must skip rather than repair or infer. Repeated scans are retained as distinct events;
a later evaluator will group explicit scan timestamps and define its own deduplication policy. The file is
append-only and is not read in the live scan path.

## Review

Groq and Google AI Studio both approved the bounded design on 2026-09-11. Their shared requirements folded
here are: schema versioning, scan timestamps, one write/fsync per completed scan, failure isolation, and
future reader tolerance of partial lines. Their suggested claim that regular-file appends are automatically
atomic was not adopted; the reader contract instead treats a torn tail as possible.

## Verification plan

Focused tests will verify schema and content, flush/fsync invocation, a write failure returning `False`,
and the live integration continuing unchanged on a failed append. Static checks run with the project lint and
type commands. OCI verification will call the helper with a temporary output path and confirm one parseable
line; production deployment is marked deployed-unexercised until the next completed scan appends an event.

## Forward pass

This ledger is necessary but insufficient for DSR. A later offline evaluator must define comparable virtual
entry, exit, cost, and return rules before it can create independent shadow strategy return streams. That
future evaluator, not this logging increment, decides any DSR input contract.

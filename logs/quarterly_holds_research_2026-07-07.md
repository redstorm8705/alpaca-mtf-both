# Quarterly Holds Research — Q3 2026
**Date:** 2026-07-07 | **Prepared:** CCR session (commit 3ce41ee, proxy-blocked) — recovered S54
**Status:** APPROVED — 4 picks confirmed by Rafael

---

## Executive Summary

4 picks selected for Q3 2026 quarterly long holds. First entry: Jul 7 (GS).
Picks span financial services, industrials, and healthcare/pharma.

---

## Selected Picks

| Tier | Symbol | Composite Score | Entry Window | Notes |
|------|--------|----------------|--------------|-------|
| T1 | **LLY** | 9.75/10 🟢 | Day 3 = Aug 7 | |
| T1 | **GE** | 8.25/10 🟢 | Day 3 = Jul 25 | Fallback: CEG if FMP staleness >30d at Jul 25 |
| T2 | **GS** | 9.25/10 🟢 | Jul 7–14 | First entry this quarter |
| T2 | **GEV** | 9.25/10 🟢 | Jul 22–29 | Fallback: FCX if parser confidence <80% at Jul 22 |

---

## Fallback Rules

- **GE → CEG:** Activate if FMP data staleness >30 days at Jul 25 pre-entry check
- **GEV → FCX:** Activate if parser confidence <80% at Jul 22 pre-entry check

Board splits on GE and GEV were resolved by composite score. Both have explicit pre-entry validation gates.

---

## Integration Status

- [ ] `_THESIS_CONFIG` in `execution/quarterly_hold_manager.py` updated with Q3 picks
- [ ] `orphan_manager.py` QHM stop exclusion: DONE (commit 436e1ad, S54)
- [ ] QHM internal fixes (risk.register_open, tracker.record_exit, NVDA thesis): PENDING
- [ ] run_cycle.py wiring: PENDING
- [ ] main.py QHM instantiation: PENDING

---

## Source

CCR session commit `3ce41ee` (24 commits ahead of origin, proxy 403 blocked push at time of recovery).
Recovered from session transcript into this repo S54 2026-06-07.

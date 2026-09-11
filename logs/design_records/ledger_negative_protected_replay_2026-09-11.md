# Negative protected replay balance repair — 2026-09-11

## Observed failure
OCI `ledger_sync` repeatedly refuses an NVDA/QHM floor reduction even after the floor was explicitly healed to zero. The broker has no NVDA position, the current QHM state records `qty_filled: 0`, and the persisted ledger records `qhm: 0`. A read-only exact replay of all 31 NVDA fills instead calculates `qhm: -2` because two historical exits are attributed to QHM while their earlier legacy entries remain untagged.

## Decision
A protected tier represents long-only ring-fenced shares. A negative protected replay quantity can never be a valid floor. Reclassify only that negative amount to the intraday tier, preserving total ledger quantity and broker-net drift. The protected quantity becomes zero. This does not reduce any positive protected floor, does not change broker orders, and leaves a real positive `old_q -> new_q` reduction subject to the existing confirmation gate.

## Verification
The regression test covers: (1) a flat broker net with a reconstructed negative QHM balance heals to zero floor without drift; (2) an actual QHM decrease from 2 to 1 remains refused. Production replay has been captured read-only before implementation and reproduces `NVDA/qhm was 0, would_be -2, net 0`.

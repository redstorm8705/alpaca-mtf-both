# Session Summary — 2026-09-29/30 (ends 08:40 PT)
**Project:** alpaca-mtf-bot | **Signed:** Claude (interactive) | **Duration:** multi-day session (QHM tranches → C2 research → builds)

## Bugs Fixed / Found
| ID | File | Fix / status |
|----|------|--------------|
| DT-ROOM | execution/day_trade_manager.py `_min_stop_room_ok` | Stop room measured from the live bid/ask, not the limit price; fail-closed on an unknown direction or a crossed stop. PR #443 merged `7cac5d9`, deployed. |
| QHM-TRANCHE | execution/quarterly_hold_manager.py | Tranche adds + the stop-safe add (cc97cc4, earlier in session). The 21-day earnings blackout was replaced by a trim-state guard (Rafael). Deployed. |
| ALLOC-ENUM (P0) | execution/tier_capital_allocator.py | `str(enum)` → `'OrderStatus.NEW'` on py3.10 → every entry denied. Patch saved (`logs/pending_patches/allocator_enum_fix_2026-09-29.patch`); NOT shipped. |
| ALLOC-LEDGER (P0) | execution/ownership_guard.py sync / day tier OCO legs | Untagged OCO exit legs are attributed to intraday → crossed net-zero rows (AMZN/META/MSFT) → the allocator denies every tier. Not yet fixed. |

## Decisions Made
- Earnings: add into earnings; the 21-day blackout was removed (Rafael).
- C2 swing tier = the megacap 55-day breakout (lab 8c); new 12-point swing entries go off (-$126 over 154 trades).
- Breakout lots use the "intraday" owner tag + the allocator's "swing" budget (a new ledger tier would break the allocator's reconciliation).
- Sizing: the 1-share floor, up to 25% of equity with the 2% stop-risk cap (board risk seat + Gro + GAI). Raising the allocator swing budget (option B) is deferred until the day tier's budget use can be measured.
- Allocator incident: Gro + GAI recommend Option B (disable the allocator, fix, re-enable). Pending Rafael.

## Corrections
- The lab 8 beta bug (rolling cov → NaN → beta=1) was corrected; the lab 9 t-stat was overstated and corrected.
- Stale model ids: Groq llama-3.3 and Gemini 2.5 are dead; use openai/gpt-oss-120b and gemini-3.1-flash-lite (the preship_audit ladder).

## Confirmed Patterns
- Cold reviewers + board seats caught ~10 real stop/P&L recovery defects that ruff/mypy/tests all passed.
- Read-only production probes (run the code on OCI) found the root cause in minutes where the logs alone did not.

## Open Items
- [ ] Rafael: decide allocator Option B.
- [ ] Gate + ship the allocator enum fix; fix the ledger OCO-leg attribution + heal the 3 rows; re-enable after an OCI probe.
- [ ] Breakout tier: cold-2nd on the last two edits → Gro+GAI preship → Rafael approval → ship.
- [ ] Follow-ups: dead MAX_OVERNIGHT_EXPOSURE_PCT; the allocator audit writes nothing when the snapshot fails; research labs 6-9 uncommitted.

## User Preferences Observed
- "Trade the market we have"; find an edge (Mag-7 is fine); 2-30 day swing horizon, not 10-year thinking.

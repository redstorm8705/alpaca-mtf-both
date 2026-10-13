# UBER incident — full audit (2026-10-11/12, Claude)

**What happened:** 72 UBER (~$5,120, ~1.9x equity) appeared in the paper account at the 2026-10-12 04:00 ET open.
No tier owned them, no stop protected them after the close, and no Slack page fired. Buying power was $0 all
session, so no tier could open a trade on 2026-10-12.

## Root cause (verified at source)
1. **Origin: my local test runs, not production.** On 2026-10-11 I ran `tests/test_day_tier_track_b_live.py` four
   times in the worktree. The four `DT-UBER-b-...` client order ids (18 shares each) were created at those run times.
2. **Why a test could trade:** the harness mocked `broker.submit_limit_order` but not `broker.submit_bracket_entry`.
   PR #590 set `DAYTRADE_NO_COHOLD=False`, which sent the co-hold test case down the bracket path. The worktree
   `.env` is a symlink to the real paper keys, and `run_day_tier.py` calls `load_dotenv` at import, so the unmocked
   call reached Alpaca.
3. **Why no tier owned it (Rafael: "how did something pass that wasn't assigned a tier?"):** nothing "passed" a tier
   gate. The orders came from outside the bot's process, so no tier's entry logic, gates or lifecycle log ever saw
   them. The `DT-` tag looked Day-tier, but the Day tier only manages trades it recorded itself, so no tier adopted
   the shares. The fill-based ownership ledger counted the DT-tagged fills as Day-owned, so the ledger's drift check
   stayed quiet.
4. **Why no Slack alert:** no running check compared Alpaca's positions against what the tiers manage.
   - `main.py` P0-STARTUP checks only the Swing tracker, only at startup, and only logs.
   - `run_ledger_sync.py` covers the protected (QHM/F6) floor, using the same fill ledger that mislabeled the shares.
   - The test process itself cannot page, by design.
5. **Why buying power was $0:** at ~1.9x equity on a margin account with existing holdings (cash -$6,084.78 on
   2026-10-12 after the close), the account had no buying power left for any entry.
   - Separately, Rule 1 premarket red lockout blocked longs (6/8 movers red).
   - DRAM was blocked in the Swing scorer, which needs more than 200 daily bars; DRAM has 133. That is a separate
     limited-history issue; see below.

## Fixes
| # | Fix | Status |
|---|-----|--------|
| 1 | Tests can never reach the real broker: `execution/broker.py` `_get_trading_client` refuses under test; `tests/conftest.py` blanks the Alpaca keys; the bracket mock was added to the harness (PR #619) | live |
| 2 | Unmanaged-position alarm: every main-loop cycle (including overnight), page when Alpaca holds shares no tier manages; page-only, an unreadable source is never counted as zero (`execution/unmanaged_positions.py`) | gating, branch claude/unmanaged-alarm |
| 3 | UBER disposition (Rafael 2026-10-12: not selling; convert to a QHM hold, trim, GTC stop at entry) — Rafael placed GTC sell 65 @ $71.40 and GTC stop 6 @ $70.15; after the fill, register the remaining shares in QHM with the stop pinned | waiting on the trim fill |
| 4 | Swing scorer limited-history (DRAM) | board + Gro + GAI design pass |

## Lesson
A test that can reach a real write API is a live action. A config flag that reroutes an execution path must be
checked against every test harness that touches that path (memory: tests-never-reach-broker).

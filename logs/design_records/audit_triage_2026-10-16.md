# Audit-finding triage — reports 2026-10-09 .. 2026-10-15 (Claude, 2026-10-16)

**Scope:** every finding, directive and "NEW BUGS" row in the synced reports for the last 7 days:
`logs/gemini_audit_2026-10-{09,12,13,14,15}.txt`, `logs/midday_gemini_2026-10-{09,12,13,14,15}.txt`,
`logs/midday_audit_2026-10-{09..15}.json`, `logs/meta_audit_latest.json`. No `ai_audit_meta_*.json` and no
`audit_directives.jsonl` exist in this checkout (they live on the server). The midday Gemini reports for 10-14 and
10-15 are empty (Gemini HTTP 503). The `midday_audit_*.json` files hold computed data (entries, fills, stop coverage),
not findings. They were used only as evidence.
**Method:** each finding was checked against the current code at source (file:line on `main` @ `02686ea`). Server
logs, fills and state files are not available in this cloud session. Anything that depends on them is marked
CANNOT_VERIFY_WITHOUT_LOGS.

## Plain-English table

| # | Report(s) | What the AI auditor said | Verdict | One-line why |
|---|-----------|--------------------------|---------|--------------|
| 1 | 10-09 midday + nightly | QHM order call passes an argument the broker adapter rejects (NVDA buy failed) | CONFIRMED — already FIXED | It was real. #564 added `client_order_id` to the adapter on 10-09. |
| 2 | 10-09 midday, 10-13 midday | Trades entered with "score 0/12", so the minimum-score gate is being bypassed | REFUTED (gate) / CONFIRMED (report bug) — **fixed in this PR** | Day-tier and promotion trades carry no 12-point score. The report printed a missing score as 0/12. |
| 3 | 10-09 nightly, 10-13 nightly | "FIFO orphan" warnings for AMZN/SNXX/PLTU mean P&L is being lost | REFUTED (P&L) / CONFIRMED (false warning) — proposal | The warning fires before the same-day check, so a Day-tier round trip logs "orphan". FIFO still prices it. |
| 4 | 10-09 nightly | The ledger wrongly moves NVDA shares from the protected tier to Swing | REFUTED | That is the designed correction for an impossible negative protected balance. |
| 5 | 10-09, 10-13 nightly | Ledger sync takes 15-16 s, so the bot misses its scan window | REFUTED (cause) / CANNOT_VERIFY (timing) | Ledger sync is its own cron (`run_ledger_sync.py`), not part of the trading loop. |
| 6 | 10-09 nightly, 10-12 midday, 10-13 nightly | DRAM "No daily data" is a data-feed failure | CONFIRMED (misleading wording) — tracked elsewhere | DRAM has 133 bars and the check needs more than 200. The real fix is the young-instrument design. |
| 7 | 10-09 midday | MRI VIX feed stale ("age=inf") | CANNOT_VERIFY_WITHOUT_LOGS | Needs `mtf_bot.log`. "inf" means the cache was empty at that moment. |
| 8 | 10-12 midday + nightly | QHM tried to SHORT NVDA ("shorting not enabled") | REFUTED (short) / CONFIRMED (wrong log text) — owner's file | Any Alpaca 40310000 rejection is logged as "shorting not enabled". On 10-12 buying power was $0, so a QHM *buy* was most likely rejected [hypothesis — needs the 10-12 order log]. |
| 9 | 10-12 midday | $0 buying power blocks all shorts | REFUTED as a code bug | The cause was 72 UBER shares from test-placed orders (handoff 2026-10-12). It was fixed by #619 (tests can never reach the broker). |
| 10 | 10-12 nightly | `earnings_preflight.py` crashes on a missing `symbols` argument | REFUTED | It was fixed 2026-06-30. Gemini quoted the fix's own comment. |
| 11 | 10-12 nightly | `quarterly_holds.json` corrupt, so QHM quantities fail closed | CANNOT_VERIFY_WITHOUT_LOGS | A state-file event on the server. The fail-closed path is correct. |
| 12 | 10-12 midday | Short intraday side has PF 0.25 over 55 trades | Not a code finding | A strategy-quality note. It goes to research, not to a bug fix. |
| 13 | 10-13 midday | PLTR shows $50.02 profit on a $25.01 move: P&L calculation bug | REFUTED | 2 shares × $25.01 = $50.02. The report math is `(exit − entry) × qty`. |
| 14 | 10-13 midday | NVDA and PLTR NAKED for 6 minutes | CANNOT_VERIFY_WITHOUT_LOGS | The ground truth shows a 06:30-06:36 PT window. That is the open hand-off, which is SOFTWARE-ONLY-BY-DESIGN on other days. It needs that day's order log. |
| 15 | 10-13 .. 10-15 | EOD P&L drift between Alpaca and the tracker | REFUTED as a bug | It is a pre-heal snapshot (the nightly prompt states this). The EOD reconcile heals it. |
| 16 | 10-13 nightly | Unmanaged-position alarm "fails to clear" for UBER | REFUTED | The alarm repeats while a residual exists. The 7 UBER shares were real. |
| 17 | 10-13 nightly | `_load_log` double-record means FIFO reconciliation is failing | REFUTED | Gemini inferred it from reading the code. No log line in the report shows that warning firing. |
| 18 | 10-14 nightly | Tracker vs broker position count mismatch blocks entries (ANOMALY-2, NFLX) | REFUTED (block) / CANNOT_VERIFY (counter) | The same report says the Rule 1 premarket lockout blocked NFLX. The counter gap needs logs. |
| 19 | 10-14 nightly | Fill notifier can't find the tier of an old bracket parent | REFUTED as a bug | By design it labels the tier "unavailable", never a wrong tier. The label is only shown on the fill card. |
| 20 | 10-14 nightly | Fill notifier could hit the 200-page limit | REFUTED | That needs more than 100,000 fills in a day. |
| 21 | 10-15 nightly | NVDA 1 residual share naked | CONFIRMED — already FIXED | The root cause was fixed in #662. The share was healed 10-16 (handoff). |
| 22 | 10-15 nightly | NFLX short "stop_hit" with +$2.27 P&L looks wrong | CANNOT_VERIFY_WITHOUT_LOGS | A positive short stop is normal after a trailing stop moves below entry. Needs that trade's events. |
| 23 | 10-15 nightly | `dip_zones.refresh()` could drop a bar if run at midnight | REFUTED | The cron runs 12:20/21:20 UTC. It filters `< today ET`, which is the intended "completed sessions only". |
| 24 | 10-15 nightly | `run_ledger_sync` owner-exit warnings may cause alert fatigue | CANNOT_VERIFY_WITHOUT_LOGS | Fail-safe by design (`since_utc` None means page). The frequency needs logs. |

**Fixed in this PR:** #2 (`midday_audit.py`).
**Proposals (not shipped):** #3 (portfolio_tracker hotspot, needs its own full-read gate). #8 (`execution/broker.py`
is owned by another session).

### For the owner's session — mark REFUTED in `audit_directives.jsonl`
#4, #5 (cause), #9, #10, #13, #15, #16, #17, #18 (block), #19, #20, #23. Also #2 and #8 as "gate bypass" / "QHM short"
claims (the underlying report/log defects are tracked separately).

## Evidence

**#1 QHM `client_order_id`:** `main.py:452-455`, which now reads
`def submit_limit_order(self, s, q, side, price, ext=False, tier="qhm", client_order_id=None)`. The fix is commit
`9fb9263` ("the QHM broker adapter accepts client_order_id — every QHM buy was failing (NVDA 10/09)"), merged as #564.

**#2 "score 0/12".**
- The Day tier logs its entry with no score: `execution/day_trade_manager.py:2736-2738`
  `trade_logger.log_event("entry", symbol=symbol, price=fill_px, size=filled_qty_i, data_source="daytrade", tier="daytrade", direction=direction, trade_id=entry_coid, stop=stop_px)`.
- Promotions (`execution/day_promotion.py:598-600`, `data_source="promotion"`, `promoted_from_day_tier=True`) and Swing
  breakouts (`execution/swing_breakout_manager.py:332-336`, `setup="c2_breakout_55d"`) are the same.
- `trade_logger.log_event` defaults `score: int = 0` (`trade_logger.py:93`) and always writes `"score": int(score)`
  (`:116`). So every one of those entries lands in `trade_events.jsonl` with `"score": 0`, and the report printed
  `score 0/12` (`midday_audit.py` old `:627`).
- On 10-09, AMZN/SNXX/PLTU were `owners=day` (gemini_audit_2026-10-09 BROKER GROUND TRUTH).
- The Swing MIN_SCORE gate sits in `strategy/run_cycle.py` (dynamic MIN_SCORE layers). Day entries never pass through it.
- **Fix:** `midday_audit._unscored_source()` reads the record's own markers (tier, data_source,
  promoted_from_day_tier, setup) and labels those entries "no 12-pt score (Day tier | Day-to-Swing promotion |
  Swing 55-day breakout)". A KNOWN BENIGN PATTERNS line explains this to the auditor. Every other entry keeps its
  logged score, including a real 0.
- Review round 1 (cold-2nd + adversarial) FAILED the first draft. It keyed on a missing `score` field, which never
  happens in production. Tests now build records with the real `trade_logger.log_event`
  (`tests/test_midday_audit_ground_truth.py::TestUnscoredEntries`).

**#3 FIFO orphan warning.**
- `execution/portfolio_tracker.py:715-724`: the warning `FIFO orphan: closing fill for {sym} has no prior lot and no tracker record` fires when the symbol has no Swing tracker record.
- That fires *before* the "Guard 2: intraday — opening fill exists today" check (`:735-739`).
- A Day-tier round trip (buy and sell the same day, never in the Swing tracker) therefore logs the warning. It is
  still skipped from seeding (sentinel `None`), so FIFO prices the round trip natively from today's fills.
- Result: a false warning, not lost P&L.
- **Proposed fix:** skip the warning when `_fsym in _long_opened_today | _short_opened_today`.
  `portfolio_tracker.py` is a CRITICAL hotspot (2212 lines), so it needs its own Explore full read and gate. It is not
  in this PR.

**#4 negative protected replay:** `execution/ownership_guard.py:1279-1301`. A negative QHM/F6 replay balance is moved
to Swing (`intraday`) and logged by design. The comment says it is "an attribution artifact, never a negative floor".

**#5 ledger sync:** it is a separate cron script (`run_ledger_sync.py`), not called from the 5-minute trading loop.

**#6 DRAM.**
- `strategy/confluence.py:89` requires `len(daily_df) > config.SMA_200` (200, `config.py:145`). Otherwise it logs
  "No daily data — long entry blocked (fail-closed)" (`:105`, `:348`).
- DRAM has 133 bars (`logs/design_records/young_instrument_swing_scoring_2026-10-13.md`). The wording is wrong
  ("short history", not "no data").
- The behaviour change is the board-aligned young-instrument design (renormalize). It is risk-path and tracked there.

**#8 "shorting not enabled".**
- `execution/broker.py:801-802` reads `if "40310000" in err or "not allowed to short" in err: logger.warning(f"[{symbol}] Limit order rejected — shorting not enabled")`.
- Alpaca uses code 40310000 for several rejections (held-for-orders, insufficient quantity, and per the 10-12 context
  buying power). The broker's own 40310000 handlers elsewhere treat it as held_for_orders (CLAUDE.md, broker partial-close path).
- QHM only calls `submit_limit` for tranche entries and dip-adds (`execution/quarterly_hold_manager.py:2835`, `:4180`).
- The 10-12 midday report records `buying_power=$0.00`.
- Proposal for the broker owner: log the raw Alpaca message instead of "shorting not enabled".

**#9 $0 buying power.** The trading-client refusal comment at `execution/broker.py:159-163` records it: an unmocked
test placed 4 real UBER bracket orders (72 sh, about 1.9x equity), and buying power went to $0 for a session.

**#10 earnings_preflight:** `earnings_preflight.py:118-128` now calls `preload_earnings_week(sorted(symbols), lookahead_days=35)`.
The "since 2026-06-07" text Gemini quoted is the fix's own comment.

**#13 PLTR:** `midday_audit.py:242` computes `raw_pnl = (xp - ep) * qty`. (206.73 − 181.72) × 2 = 50.02, which is
asserted in `TestUnscoredEntries.test_missing_score_is_none_not_zero`.

**#15 drift:** `nightly_audit.py:503` tells the auditor that a pre-heal `pnl_drift` is not corruption.

**#16 UBER:** `execution/unmanaged_positions.py:379` logs `"%s [%s] still unmanaged: residual %g (paged)"` every pass
while the residual exists. That is the intended re-page behaviour (design `unmanaged_position_alarm_2026-10-12.md`).

**#17 double-record:** `execution/portfolio_tracker.py:183-189`. It is a warning on load. The 10-13 nightly cites the
code, not a fired log line.

**#18 ANOMALY-2:** `strategy/run_cycle.py:1900-1909` fires for any symbol with ≥3 confirm scans and no entry,
whatever the reason. The 10-14 nightly itself states "The NFLX short (19:47:03) was blocked by the RULE 1 premarket red lockout".

**#19/#20 fill notifier:**
- `execution/fill_notifications.py:208-230`: the bounded search returns "unavailable", never a guessed tier. The
  comment says "An empty bounded search does not prove this was a manual/untagged order".
- Pages are capped at 200 × 500 = 100,000 fills, and the account trades tens of fills per day.

**#21 NVDA residual:** handoff 2026-10-16 (#662: QHM add fills are credited to the ledger at once; NVDA healed to 2
shares with stop `788c4696`).

**#23 dip zones:** `execution/dip_zones.py:156` (`today` = ET date) and `:169` (`c.index < pd.Timestamp(today)`).
The crons are at 12:20/21:20 UTC (`scripts/install_dip_zones_cron.sh`), never at ET midnight.

**#24 owner-exit:** `run_ledger_sync.py:130-140`. With no `since_utc` it returns False, which means page.

## Not reproduced (separate owner task, 2026-10-16)
`tests/test_broker_tier_safety.py::PartialCloseCoholdBound::test_unreadable_broker_uses_day_log` passes on unmodified
`main` (`c9d4b59`) in this container:
- the whole file: 75/75 under pytest and under `python -m unittest`;
- the single test alone;
- paired with every other test module that touches `ownership_snapshot` or the broker.

`git log -L` shows `_cohold_own_cap` is unchanged since #584 (`0fb92c8`); #651 (`0cfd791`) only added the
serialization decorator and the F6 reduction check. The failure needs the failing machine's environment to reproduce
(data/state files, or a stubbed `alpaca` package from another test in the same run). `execution/broker.py` was not
edited.

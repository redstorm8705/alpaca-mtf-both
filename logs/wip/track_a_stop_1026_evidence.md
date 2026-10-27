# TRACK-A-STOP-1026 WIP — evidence (signed: Claude, 2026-10-26)

## Code state
- Not committed as code: `git status` on branch claude/bv5-replay-tool shows config.py, execution/day_trade_manager.py,
  execution/earned_sizing.py modified in the working tree only; the commit carrying this file contains docs/research only.
- Not deployed: OCI HEAD 1296a44 (`git -C /home/ubuntu/mtf-bot rev-parse --short HEAD` -> 1296a44), which predates it.
- Patch: logs/wip/track_a_stop_1026_wip.patch (`git diff config.py execution/day_trade_manager.py execution/earned_sizing.py tests/test_track_a_noise_stop.py`, 498 lines).

## Tests (OCI scratch copy /tmp/ta1026 = `git archive 1296a44` + the patched files; Alpaca keys unset)
- `python3 -m unittest tests.test_track_a_noise_stop` -> `Ran 16 tests in 0.201s / OK`.
- Day-tier + sizing suites (tests/test_day*.py, test_earned_sizing.py, test_*bracket*.py) on the untouched archive and on
  the patched copy: 28 ERROR/FAIL lines in each; `diff` of the two sorted lists -> identical (SAME_FAILURES).
- Statics: py_compile ok (3 files); `ruff check --select E,W,F,B` -> All checks passed; mypy --warn-unreachable on the two
  execution files -> Success: no issues found; no_static_scan on added lines: 0 violations in the execution files, 5 in
  config.py = exactly the five owner numbers listed in the design record's `Owner numbers:` line.

## Reviews (verbatim verdict lines)
- Cold second agent: "PASS — The diff is correct for the six checks you asked for." (logic inversion, boundaries,
  missing conditions, branches, shares/dollar risk never above today, py3.10).
- Board risk seat (Thorp/Taleb lens): "APPROVE-WITH-CHANGES". Failing input: SNDK at 09:40 ET 2026-10-26 data,
  noise room $80.60 (atr5 13.434 x 6) > per-trade budget 1.5% x $2,650 = $39.75 -> risk_qty = floor(39.75/80.60) = 0 ->
  entry skipped. Required: cap the room at the one-share budget; correct record Q2/Q5/Q11; add a break-even hit-rate
  reversal (09:40 room/target: AAPL 3.41, TSLA 5.30, META 2.98).

## Real-data probe (research/ta_probe.py on OCI with the bot's .env, read-only, no orders)
- 09:40 TSLA: atr5 2.685, room 16.11, range 8.69, target 3.04; 13:15 TSLA: room 5.14, target 3.04.

## BV-5 delayed-feed validation (research/bv5_block_replay.py, BV5_FEED=sip, BV5_DELAY_MIN=15, mode validate, 2026-10-21)
- Output: `{"date": "2026-10-21", "comparisons": 164, "exact": 106, "within_1": 125, "exact_pct": 64.6}`; the same run
  with no delay was 50% (earlier run this session). Mismatches by condition: c6_vwap_sd_bands 35, c4_macd_dual_tf 24.
  Hypothesis only — not proven.

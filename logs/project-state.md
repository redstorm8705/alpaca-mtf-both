# Project State — alpaca-mtf-bot
**Updated:** 2026-05-25 S38 9:40 AM PT | **Overwrite every session — canonical state only**

## Bot Status
- **Running:** YES — OCI Phoenix `129.153.208.32` | all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32` (Ed25519 key)
- **Bot CWD on OCI:** `/home/ubuntu/mtf-bot/` — always rsync to this path
- **Account:** Paper | equity **$2,852.68** (confirmed Alpaca MCP, S37) | MIN_SCORE=10/12 | KELLY_FRACTION=0.25 | KELLY_MAX_RISK_PCT=6% | MAX_PORTFOLIO_RISK_PCT=4%
- **RAM:** 422MB used / 387MB available (S38 start, rising — peaked 750MB end of S37, restarted)
- **PDT:** 0/3 slots used

## Open Positions (confirmed Alpaca API, S37 ~11:50 PM PT 2026-05-24)
- AMZN long 1sh @ $264.81 | unrealized +$1.51
- INTC long 2sh @ $117.00 | unrealized +$5.69
- MSTR short -3sh @ $162.95 | unrealized +$9.18
- PANW long 1sh @ $247.26 | unrealized +$13.32
- TOST short -28sh @ $22.79 | unrealized -$10.36
- TQQQ long 1sh @ $76.22 | unrealized +$1.62

## S37+S38 Session Summary (overnight + wrap-up 2026-05-24/25)

**RC-3 x22 violations patched total (20 in S37, 2 in S36C-cont):**
- execution/fill_helpers.py — x2 (L55, L67)
- execution/fill_reconciler.py — x1 (L90)
- state/persistence.py — x2 (L71, L79)
- execution/lifecycle.py — x1 (L193)
- data/fmp_client.py — x7 (L98, L128, L195, L238, L280, L346, L505)
- execution/gtc_manager.py — x3 (L138, L274, L311)
- main.py — x4 (L522, L555, L690, L838)
- execution/kelly.py — x1 (L101, S36C-cont)
- alerts.py — x1 (L243, S36C-cont)

**RULE C-4 pre-existing fixes** in lifecycle.py, fmp_client.py, gtc_manager.py, fill_reconciler.py, persistence.py.

**S38 local cleanup:** pycache/pyc/DS_Store removed; OCI sync confirmed; all 9 OCI py_compile PASS.

## Open Items (next session priority)
- [ ] P1: execution/entry_logic.py RC-3 sweep — 1694L, Explore subagent required, DS/GAI gate. Deferred by DS.
- [ ] P1: scan_to_html.py 11 remaining RC-3 violations — L79, L241, L806, L917, L992, L1299, L1664, L1681, L1834, L2444, L2520. DS/GAI required (RTH chain).
- [ ] P2: RC-9 scan_to_html.py — yfinance for news (T4 violation). Board vote + migration.
- [ ] P2: BUG-C structural fix — write_scan_html background thread. Deadline 2026-06-30.
- [ ] P2: RAM trend — peaked 750MB. If >550MB within 1 session again, investigate leak.
- [ ] P3: options_scanner.py — BUG-0DTE-FALLBACK + RC-5. Pending DS+GAI.

## Hard Invariants
- paper=True hardcoded in execution/broker.py — never change without full board vote
- SPY 5-min bar-over-bar is the SOLE entry gate
- PDT hard cap: 3 slots / 5 rolling trading days
- All P&L sourced from Alpaca fills API only
- T1 (Alpaca) for all equity/ETF data — yfinance only for ^VIX, ^VIX3M, JPY=X
- DS/GAI prompts: plain text in-session ONLY — never save to .md files

## Key User Rules
- No grep/partial reads EVER — full read, declare line count
- Files >1000L → Explore subagent
- DS/GAI gate: RTH execution impact, not file name
- Board = independent subagents, not inline roleplay
- All patches logged to tb_audit_log.md + bug_counter.json IN SAME TURN as patch
- Autonomous RAM restart: if >550MB, restart services without waking user

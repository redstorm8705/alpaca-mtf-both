# Pending Approval Queue — 2026-06-16 S60

**Prepared by:** Claude (interactive session, remote cloud env)
**Status:** Ready for Rafael review at next Mac/OCI session

---

## SESSION SUMMARY

User directed work on deferred DS/GAI MODE 2 items #6 (TCA Execution Quality) and #7
(Bar-end adverse selection) from `logs/pending_approvals_2026-06-15.md`. Item #5 (Alpha
Decay/Walk-Forward) remains explicitly out of scope — still blocked by lack of OCI/SSH
access from this remote session.

| Item | Status | Notes |
|------|--------|-------|
| #6 TCA Execution Quality | IN PROGRESS — Steps 1-3 done, Step 4 blocked | `tca_logger.py` built, audited, board-approved. DS/GAI needs Mac/OCI `.env` access. |
| #7 Bar-end adverse selection | NOT STARTED | Design locked (post-hoc script, no runtime hook). Implementation not begun this session. |

---

## ITEM #6 — TCA LOGGER (`tca_logger.py`) — BLOCKED ON DS/GAI ACCESS

**User-approved design (this session):** "Build infra now, validate later" — observational
slippage logger built even though paper-trading slippage numbers are synthetic/illustrative,
so the infrastructure is ready and battle-tested before live trading.

**What's done:**
- File written: `tca_logger.py` (133 lines, repo root) — `log_entry_slippage()` /
  `log_exit_slippage()`, mirrors `trade_logger.py` pattern, writes `logs/tca_metrics.jsonl`.
- Step 1 (Full Read), Step 2 (10-point audit + RC-1–8 scan), Step 3 (board vote 2-1
  APPROVE) — all complete and clean. Sign/direction logic explicitly verified correct on
  all 8 long/short × entry/exit cases. Full detail in `logs/tb_audit_log.md` (Item #6 section).
- Static analysis already run during drafting: `py_compile` PASS, `mypy --warn-unreachable`
  PASS, `ruff --select E,W,F,B` PASS.

**What's blocking:**
- Step 4 (DS/GAI External Audit) is mandatory — RULE C-5 applies because `tca_logger.py`
  will be imported by RTH-running `execution/entry_logic.py` and `execution/exit_logic.py`.
  This remote session has no `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` (they live in
  `/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env` on the Mac/OCI side, not in this
  cloud sandbox). User explicitly chose to defer rather than paste keys into this session
  or skip the gate.

**Still needed before implement:**
- [ ] Step 4 — DS/GAI external audit (MODE 1 persona, same prompt to both) — run from a
      session with `.env` access
- [ ] Step 5a — re-confirm static analysis (already passed once; re-verify post any DS/GAI-driven edits)
- [ ] Step 5b — cold second-agent logic review on `tca_logger.py` itself
- [ ] Step 5c — code-review-graph impact radius
- [ ] Step 6 — propose exact diff (tca_logger.py is new/complete; the diffs needed are the
      call-site insertions into entry_logic.py and exit_logic.py — see below)
- [ ] Step 7 — Rafael approval
- [ ] Step 8 — apply, rsync, restart
- [ ] Step 9 — post-patch verification

**Planned integration (NOT YET WRITTEN — pending Step 4-7):**
- `execution/entry_logic.py`: insert `log_entry_slippage(...)` call after the existing
  fill-confirmation poll loop (~lines 1249-1282), using signal_price = pre-order
  `entry_price` (set via `get_latest_trade()` at lines 701-709), fill_price = confirmed
  Alpaca fill, fill_confirmed = new local bool tracked through the poll loop.
- `execution/exit_logic.py`: insert `log_exit_slippage(...)` at each of the 9
  `record_exit()` call sites, gated on `not trade.get("_fill_unverified")`, using the
  per-reason "intended price" mapping already documented: trail_stop→`trade.get("trail_stop")`,
  overnight_atr_buffer_exit→`_be_thresh`, thesis_invalidation→None (skip), hard_stop→
  `active_stop`, target→`trade.get("target")`, signal→None (skip), external_close→
  `trade["stop"]`, pm_exit→`current_price`.
- Per RULE C-6, each file (entry_logic.py, exit_logic.py) requires its own full Steps 1-9
  completed independently — `tca_logger.py` itself must finish all 9 steps first.

**RISK IF APPROVED:** Adds one extra function call per fill-confirmed entry/exit — negligible
CPU/latency, purely additive logging, cannot affect order routing or sizing.

**RISK IF REJECTED:** No execution-quality telemetry exists; cannot quantify real slippage
once the account goes live, cannot validate broker fill quality assumptions.

---

## ITEM #7 — BAR-END ADVERSE SELECTION — NOT STARTED

**User-approved mechanism (this session):** Post-hoc analysis script — no runtime hook
into `entry_logic.py`/`exit_logic.py`. Standalone script retroactively analyzes
already-fetched bars (via `data/fetcher.py`'s `fetch_bars()`) and `logs/trade_events.jsonl`
after the fact. Avoids the DS/GAI RTH-impact gate entirely per RULE C-5, since it will not
be imported by any RTH-running file.

**Still needed — nothing built yet:**
- [ ] Name the file (candidate: `adverse_selection_audit.py`, matching `midday_audit.py`
      naming convention)
- [ ] Design exact logic: read today's entries from `trade_events.jsonl`, retroactively
      fetch N bars post-entry via `fetch_bars()`, check for adverse price movement within
      that window (bar-end vs. intra-bar fill timing mismatch)
- [ ] Step 1 — full read (new file, ≤1000 lines expected, direct Read sufficient once drafted)
- [ ] Step 2 — 10-point audit + RC scan
- [ ] Step 3 — board vote (Harris/Kyle microstructure + McKinney data + Katsuyama TB,
      per domain mapping)
- [ ] Step 4 — DS/GAI: **not required** — not imported by any RTH-running file, per
      RULE C-5 and the user-approved post-hoc design
- [ ] Step 5 — static analysis + cold second-agent + impact radius
- [ ] Step 6-9 — propose/approve/apply/verify

---

## OCI VERIFICATION (REQUIRED AT NEXT INTERACTIVE SESSION)

Same outstanding item carried from 2026-06-15 — verify commits `da13ad7` and `3087360` are
pulled and the bot service is running:
```bash
ssh oci 'cd ~/alpaca-mtf-bot_FINAL && git pull && git log --oneline -5'
```

---

*Prepared: 2026-06-16 S60*

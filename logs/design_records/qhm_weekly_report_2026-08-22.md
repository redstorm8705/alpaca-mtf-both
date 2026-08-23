# QHM WEEKLY STATUS REPORT — design record (Rafael approved 2026-08-22)
**Why:** the QHM report Rafael reads (`logs/quarterly_holds_research_*.md`) was produced by his OTHER account's
Claude CCR routine and STOPPED after 2026-07-07 (~6-7 weeks). Depending on a fragile per-account routine that
can silently stop is the failure. Fix = a reliable OCI-cron report (like the weekly/monthly ones) that always
runs regardless of which account is active, committed to git (cross-account-readable) + posted to Slack.

## FEATURE DESIGN PROTOCOL
1. **Data source / tier / fallback:**
   - T1 Alpaca — `reporting.pnl_ledger.fetch_positions()` for live current price + unrealized P&L per symbol.
   - Local state — `data/state/quarterly_holds.json` (FLAT {symbol: {...}}): avg_entry_price (cost basis),
     qty_filled/qty_total (shares), stop_price/stop_order_id (current stop), target_equity_pct (target),
     earnings_gate_date, created_at/entry_day (days held), state, thesis_check_result/thesis_check_last.
   - T2 FMP (cached) — `data.fmp_client.get_cached_earnings_dates(symbol)` for earnings proximity. NEVER a live
     per-symbol FMP call (respects the 250/day limit); cached only.
   - Universe: `execution.quarterly_hold_manager.get_quarterly_hold_symbols()` (state ∪ configured picks).
   - FALLBACK: Alpaca fetch fails → report from state file only + flag "live P&L unavailable". State file
     missing → report configured picks + flag. FMP earnings unavailable → omit earnings column, don't fail.
2. **Output:** `logs/qhm_report_YYYY-MM-DD.md` (atomic tmp→replace; committed to git = cross-account-readable)
   + a Slack post via `alerts.send_slack` (or a local `_slack`, matching the other report scripts). Per-hold
   table: symbol · shares · cost basis · current price · unrealized P&L ($ / %) · current stop · target% · %-of-
   equity vs target · earnings date + days-to-earnings · days held · thesis status. Plus a one-line book summary.
3. **Integration point:** a NEW standalone script `scripts/qhm_report.py`, run by an OCI cron (weekly — Monday
   ~06:00 PT via cron_tz_wrapper, alongside the existing report crons). READ-ONLY on all trading state; imports
   reporting/data/execution modules read-only; NEVER touches execution/sizing/exits.
4. **Failure mode:** never crashes the report; degrades gracefully per the fallbacks above; never affects the
   bot (separate process, read-only). A Slack/commit failure logs a warning, doesn't raise.
5. **Board vote needed?** NOT risk-path (reporting-only, read-only on trading state, no sizing/execution/exit
   logic). So: lean BGG design pass (Self-QA #4) + the standard build gate (full-read of referenced modules +
   statics + cold-2nd + Gro/GAI preship). No mandatory cold-board/masked-loss seat (that's risk-path only).

## DYNAMIC (B10 is-it-static gate)
The report is fully DATA-DRIVEN (live Alpaca + state + cached FMP) — no static thresholds. Cadence (weekly) is a
schedule config, not a signal threshold, so it is not a static-regime violation.

## CADENCE / STYLE (Rafael 2026-08-22)
Weekly, position-status style (the table above) with a short thesis-check line; matches how Rafael consumes the
weekly report. (Research-memo style deferred — this is the ongoing monitoring report he was missing.)

## BGG DESIGN PASS (2026-08-22) — Gro + GAI, both REVISE-WITH-CONDITIONS (sound foundation, add guardrails)
Convergent. Fold ALL of these into the build before ship:
1. **Alpaca is the P&L GOLDEN source.** Use fetch_positions() `unrealized_pl`/`unrealized_plpc`/`market_value`/
   `avg_entry_price`/`qty` (incorporates fills, fees, corporate actions). quarterly_holds.json = the INTENT /
   METADATA store (target_equity_pct, stop rules, earnings_gate_date, thesis, created_at). Do NOT silently
   reconcile two cost bases — DISPLAY broker P&L, and FLAG divergence: `[DISCREPANCY]` if broker_qty != state
   qty_filled or |broker_basis - state avg_entry_price|/state > 0.5%.
2. **State/broker asymmetry:** ZOMBIE hold (state ACTIVE but symbol absent / qty 0 in Alpaca — stopped out
   externally) → classify CLOSED / LIQUIDATED_EXTERNALLY, own "Closed / exited holds" section, no phantom
   exposure. GHOST (a QHM-universe symbol in Alpaca positions but not in state) → "Unmanaged" flag row.
3. **Add risk-monitor fields** (quarterly horizon needs more than P&L): DISTANCE-TO-STOP % = (price-stop)/price;
   DIP-ADD HEADROOM $ = target$ - current$; EARNINGS-GATE STATUS categorical = SAFE(>14d)/APPROACHING(<=14d)/
   LOCKED(<=3d)/PAST. DRAWDOWN-FROM-PEAK (HWM) is valuable but needs bar history or tracked HWM → deferred to
   v1.1 (note it; don't block v1).
4. **Equity fallback hierarchy** for %-of-equity (use NAV/equity, NOT buying power/cash): Alpaca
   get_account().equity → sum(position market_value)+cash → last-known snapshot → omit column + `[EQUITY_UNAVAILABLE]`.
5. **Silent-death guardrail (the ROOT failure):** the predecessor died silently for 6 weeks. The report writes a
   last-run heartbeat (logs/qhm_report_heartbeat.json); a lightweight staleness check alerts if no QHM report in
   >8 days. (Full external dead-man's-snitch = follow-up; the staleness watchdog is the v1 minimum so it can't
   silently vanish again.)
6. **Slack-FIRST / decouple from git:** post to Slack BEFORE the git commit; a git/network failure logs a
   warning and never suppresses the Slack delivery.
7. **Read-only safety:** only get_*/fetch_* calls (get_account, fetch_positions) — NO submit/cancel/close_position
   anywhere in the script. Separate process; never affects the bot.
8. **Atomic write** (tmp→replace) on the .md; weekend/pre-market run notes the price_source (last close vs
   pre-market) since the Mon 06:00 PT run may be pre-open.
Directional: design is SOUND; ship v1 with guardrails 1-8 (drawdown-from-peak deferred to v1.1). Not risk-path.

# alpaca-mtf-bot — Project State (current, overwritten each session)
**As of 2026-07-06 06:09 PM PT**

## Bot
All 4 OCI services active, HEALTH_OK, HEAD 654d507. Paper equity $2,819.34. 9 open positions (AVGO, GOOGL[QHM], HOOD, MARA, MS, MSTR short, NVDA[QHM], RIVN, SNOW). RBLX short closed −$2.98 today.

## Shipped 2026-07-06
- 654d507: portfolio_tracker repeat-run FIFO attribution fix. 2nd+ same-day EOD run skipped all fills (processed_fill_ids) → empty per_trade/$0 → false `alpaca_fifo_unattributed` CATASTROPHIC + understated cumulative. Fix: persist alpaca_today_pnl/alpaca_per_trade atomically + accumulate each run's new fills onto the same-day baseline. Full board+Gro+GAI+cold-agent gate (unanimous after counter-prompt); final pre-ship GAI APPROVE + Gro WAIVED (Groq daily-token limit, Rafael-authorized). Live, healthy.

## Key decisions
- Bridge-then-decomp (Option 1): minimal persist+accumulate now (done); OPT-2 event-sourced replay lands inside the M1 fifo_pnl.py extraction.
- M1 sequencing = A (mechanical-extract-first, golden-eod-diff parity, then OPT-2 separately). Council 4-0.

## Open items (next session starts with M1)
1. M1 mechanical extract → fifo_pnl.py (spec: logs/M1_decomp_spec.md; watch _atomic_write/_BotEncoder circular import).
2. OPT-2 event-sourced replay (separate ship; guard mid-day-restart duplicate-lot hazard).
3. CLAUDE.md §OPEN QUESTION PROTOCOL — remove "cannot resolve from first principles" loophole (gated).
4. Groq UA fix in autonomous pipeline urllib callers (Cloudflare-1010, not TPD).
5. RBLX phantom short-lot cleanup + confirm reconcile_eod corrects cumulative.

## Way-of-working (hardened 2026-07-06)
- Board + Gro + GAI POV on EVERY fork/question before it reaches Rafael — no exemptions.
- No hedging about Claude's own context budget; wrap only when Rafael asks.

## Infra notes
- Groq 403 `error code:1010` from urllib = Cloudflare UA ban → add browser User-Agent (curl works). Distinct from real TPD daily limit.
- Preship gate: .claude/preship/preship_audit.py writes marker (curl-based); preship_gate blocks commit/push without it; --waive-gro needs Rafael auth.

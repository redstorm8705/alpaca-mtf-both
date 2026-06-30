# Handoff — Session 2026-06-30: Major session — ATH gate fix, Movers/QHM collision, Phase 2 redo, RTH Block removal

## LATEST CHANGES (this session)

| Commit | File | Fix |
|--------|------|-----|
| `8400ea7` | `config.py`, `strategy/run_cycle.py` | **ATH/market-top compound gate**: ATH_MIN_SCORE_RAISE_PCT 2.0→1.0; zone_tier >=2→>=3. Was blocking ALL entries all day (floor=12/12). Now floor=11. |
| `67e669c` | `strategy/movers/strategy.py` | **Movers/QHM cross-strategy collision**: added `get_quarterly_hold_symbols()` guard at both `close_position()` call sites. QHM tranche cycles were being reset every time Movers exited the same symbol. |
| `bf760c9` | `CLAUDE.md` | ATH stop-tightening + cross-strategy audit logged to Future Roadmap. |
| `c09291b` | `execution/risk_manager.py`, `execution/entry_logic.py`, `run_movers.py` | ATH entry scalar (0.90x when SPY <1% from 52w high, preserves R:R) + run_movers.py buffer hardcoded 15min→config 30min. |
| `2f5a13b` | `CLAUDE.md` | RTH Block policy removed (Rafael mandate 2026-06-30). |
| `182634d`–`f98e0c0` (14 commits) | 14 scripts | RTH Block runtime code removed from all 14 analysis/backtest scripts. Also found 2 real bugs in `earnings_preflight.py` during this pass. |
| `5afa539` | `execution/gtc_manager.py` | GTC partial-exit collateral-cancellation detection (dormant structural hardening). |
| `6353814`, `8460a19` | `events/calendar.py`, `config.py` | CAUTION tier 0.50→0.65x; validate_config() toggle hardening for VOLUME_CONFIRMATION_ENABLED. |
| `c4bcdf0` | `events/calendar.py` | NFP date error (2026-07-02 was Thursday) + RC-1 ET-anchor at 3 call sites. |
| `d11dc32` | `indicators/macd.py` | Missing len(df)<2 guard in macd_histogram_rising/falling. |
| `3f2ab0e` | `indicators/momentum.py` | Missing try/except + 0.0 boundary mislabel. |
| `5afa539` | `autonomous_patch_generator.py` | **P0**: Migrated dead DeepSeek API (402 all calls for weeks) to Gro/Groq. |
| Various | Phase 2 redo (10 files) | Full board (4 agents) + Gro/GAI redo: 13 bugs fixed across execution/, events/, strategy/, indicators/. |

## BOT STATUS RIGHT NOW
- **Running:** YES — systemctl active on OCI
- **Open positions:** None (NVDA/GOOGL QHM reset to PENDING_ENTRY tonight at 18:02 ET)
- **Dynamic MIN_SCORE floor:** 11 (MRI=ELEVATED only; ATH compound fixed)
- **ATH entry scalar:** 0.90x active when SPY <1% from 52w high
- **autonomous_patch_generator:** NOW FIXED — tonight's 11 PM ET run should be the first to produce real output in weeks

## OPEN ITEMS (require action next session)
- [ ] **OCI git cleanup**: 106 uncommitted files (stray PDFs, zips, `Users/` dir). User asked for categorization before any reset.
- [ ] **qhm_external_close pnl field**: trade_events.jsonl shows `price: null` for external_close exits — low priority since Alpaca fills are authoritative.
- [ ] **Cross-strategy Phase 3 audit**: logged to CLAUDE.md Future Roadmap. Does `safe_close_all()` or main bot exit paths protect QHM positions?
- [ ] **ATH trailing tightening** (-2.5x ATR from ATH, Taleb position): needs board session + 2-year SPY backtest.
- [ ] **GE/GEV/LLY QHM entries**: auto-triggers July 22+ when `not_before_date` passes.

## QHM PICKS (Q3 2026)
NVDA (20%, hold →Aug 19) | GOOGL (15%, hold →Jul 28) | GE (20%, enter Jul 25) | GEV (15%, enter Jul 22) | LLY (20%, enter Aug 7)

## HARD INVARIANTS
- `paper=True` hardcoded in `execution/broker.py`
- SPY 5-min bar-over-bar is the SOLE entry gate
- PDT abolished (S63 sweep)
- All P&L from Alpaca fills API only
- Gro/GAI sign-off = lean prompts only (no synthesized conclusions in prompt)

## SESSION START CHECKLIST
- [ ] Read this handoff
- [ ] Check autonomous pipeline logs (did tonight's patch_generator and review run?)
- [ ] Check `logs/nightly_audit_*.txt` for today's audit
- [ ] Bot is running: `systemctl is-active mtf-bot` on OCI

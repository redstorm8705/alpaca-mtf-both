# Paper → Live Trading Checklist
**Created:** 2026-05-16 | **Status:** Paper phase (aggressive)
**Trigger:** $10K cumulative profit on paper → deploy small live capital alongside paper

---

## The North Star
Paper phase = push limits, maximize daily profit, pressure test the bot.
Live phase = re-evaluate every item on this list before first live order.

---

## Section 1 — Hard Invariants to Flip

| Item | Paper Setting | Live Setting | Gate |
|------|---------------|--------------|------|
| `paper=True` in `execution/broker.py` | `True` (hardcoded) | `False` | Full board vote + cold audit |
| Alpaca API keys | Paper keys in `.env` | Live keys in `.env` | Manual swap — do NOT commit to git |
| `ACTIVE_PROFILE` | `"paper"` | `"live"` | Confirm profile constants below |
| Kill switch `MAX_DAILY_LOSS_PCT` | 7% | 3% | Board-approved live value |

---

## Section 2 — Risk Parameters to Tighten for Live

| Parameter | Paper (aggressive) | Live (conservative) | Why |
|-----------|-------------------|---------------------|-----|
| `KELLY_FRACTION` | TBD (board vote) | 0.25 (or recalibrate from N trades) | Real money — fractional Kelly mandatory |
| `MAX_PORTFOLIO_RISK_PCT` | 0.03 | 0.02 | Per-trade risk lower on real capital |
| `KELLY_MAX_RISK_PCT` | TBD (board vote) | 0.04 | Hard cap matters more with real money |
| `KELLY_MIN_RISK_PCT` | 0.0075 | 0.0075 | Keep — ensures minimum viability |
| `A2_DD_START` | 0.02 | 0.02 or lower | May want earlier protection on real money |
| `A2_MULT_FLOOR` | 0.33 | 0.20 | Tighter floor (Taleb) for real capital |
| `MAX_OPEN_POSITIONS` | 4 | 3–4 | Re-evaluate vs. account size |

---

## Section 3 — Score / Signal Thresholds

| Parameter | Paper | Live | Why |
|-----------|-------|------|-----|
| `MIN_LONG_SCORE` / `MIN_SHORT_SCORE` | TBD (board vote) | ≥10 | Real money = higher quality bar |
| Kelly warmup gate | N≥30/type | N≥50/type recommended | Larger sample = more stable edge estimate |
| `KELLY_MIN_SAMPLE_SIZE` | 30 | Consider 50 | LdP: more data = better estimate for real capital |

---

## Section 4 — Feature Gates (review before live)

| Feature | Gate | Status |
|---------|------|--------|
| Kelly full confidence (shorts) | N≥30 short_intraday | GATED — 3 trades away |
| TSMOM scoring (c8 slot) | 90-day log + CPCV backtest | GATED — July 22, 2026 |
| KNN classifier | 500+ closed trades | GATED |
| H9 trail stop rv_scalar | 200+ closed trades + inverted formula | DEFERRED |
| A2 Kelly adaptation | Board-approved 2026-05-16 | ✅ Implementing now |
| Score-weighted warmup sizing | Separate board vote | DEFERRED |

---

## Section 5 — Infrastructure / Operations

| Item | Status | Action Before Live |
|------|--------|--------------------|
| OCI server uptime | ✅ Running | Verify SLA / alerting cadence for real money |
| Slack alerts | Active | Confirm alerts reach you during market hours |
| EOD reconciliation | ✅ Alpaca FIFO verified | Verify P&L accuracy on 50+ trades before live |
| Dashboard P&L accuracy | Active | Cross-check against Alpaca fills API on 5 random trades |
| Bot crash recovery | Manual restart | Document restart SOP; consider watchdog |
| Backup `.env` | Local only | Secure backup (not git) |
| API rate limits | Paper tier | Verify live tier limits (Alpaca) |

---

## Section 6 — Legal / Compliance

| Item | Check |
|------|-------|
| Account equity ≥ $25K OR use cash account rules | PDT: 3 day trades / 5 days if under $25K applies to live too |
| Tax documentation | Ensure `trade_events.jsonl` captures all required fields for tax lots |
| Pattern Day Trader flag | If live account gets PDT-flagged, all day trading blocked for 90 days |
| Wash sale rules | Bot does not track wash sales — flag for accountant |

---

## Section 7 — Validation Before First Live Order

- [ ] Minimum 5 consecutive profitable trading days on paper (recent, not historical)
- [ ] Kelly stats stable: long_intraday N≥50, short_intraday N≥30
- [ ] All critical bugs resolved (handoff.md open items #7–#13 closed or explicitly deferred)
- [ ] Cold second-agent audit of `broker.py` after `paper=False` change
- [ ] Full board vote on live parameter set
- [ ] DS + GAI audit of `broker.py` change
- [ ] Manual test: place 1 live limit order far from market, verify it routes correctly, cancel it

---

## Section 8 — A2 / Drawdown Protection

- [ ] Reset `ath_equity` in `kelly_stats.json` if equity is injected (prevents false drawdown signal)
- [ ] Confirm A2 constants are appropriate for live capital (0.02 start, floor re-vote)
- [ ] Verify `kelly_stats.json` is not corrupted before first live trade

---

## Notes

- **Aggressive paper vs. conservative live**: All paper-phase parameter changes should be flagged here so they get re-evaluated before live. When in doubt, add the item.
- **$10K profit trigger**: When cumulative paper P&L hits $10K, pause and run through this checklist before deploying real capital.
- **Real capital amount**: TBD by user. Start small (e.g. 10–20% of paper account size).

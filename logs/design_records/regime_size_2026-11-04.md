# REGIME-SIZE-1104 — the core-tier size multipliers change no share counts (signed: Claude, 2026-11-04)

Approval-ID: REGIME-SIZE-1104
Status: rev 3.1 — BGC aligned (board 3/3, GAI, Codex); awaiting owner approval. No code written.
Covers (planned): config.py, strategy/volatility_regime.py, strategy/run_cycle.py, execution/entry_logic.py, CLAUDE.md
(Rule E text), tests/test_core_sizing_multiplier.py (new).

## Question that started it
`CLAUDE.md` Rule E (2026-08-02) says the regime layer's up-multiplier `REGIME_LOW_VOL_SIZE_MULT` is "pinned to 1.0 until
staged". `config.py:468` has 1.25 and has since the initial commit `0d951ce5` (2026-05-25); `git log -G` shows no value
change. No staging record exists.

## Findings (code-verified this session; how often each fires live is unmeasured — no server logs here)
1. **No multiplier in the stack changes share counts, except 0.0.** `execution/entry_logic.py`: `raw_shares =
   int(dollar_cap / stop_distance)` (L1365); `_sized = int(raw_shares * size_multiplier)` (L1371); `shares = max(1,
   _sized)` (L1380-1381); then `max_shares_by_value = int(dollar_cap / entry_price)` and `shares = min(shares,
   max_shares_by_value)` (L1397-1398). Because a stop distance is a small fraction of price, the value cap always wins
   unless the multiplier is below about stop/price (~0.02-0.10). Example (seat LdP): price $100, stop $2.40, cap $1,000 →
   10 shares at 0.5, 1.0 and 1.25. The trade count is also unchanged: `_can_afford_one` (L1372) ignores the multiplier.
   All 3 seats found this independently.
2. **So the 1.25 "size up" is inert — and so is every protective cut in the same product** (`strategy/run_cycle.py:1827`:
   event × regime × time-of-day × SPY × session P&L × overnight), including HIGH_VOL 0.5. Only the 0.0 blockade
   (L1374) acts. What really sizes positions acts on `dollar_cap` earlier: Kelly, TSMOM vol-scaling, FVG, VOTE-3,
   earnings, plus the Kelly share clamp (L1438-1446) and VOTE-5 after.
3. **The log lines overstate what happens.** "Low-vol grind … sizing up 1.25x" (`strategy/volatility_regime.py:151`)
   and "HIGH VOL — half size, wider stops" describe changes that never reach the order. `stop_mult` (incl.
   `REGIME_HIGH_VOL_STOP_MULT = 1.5`) is returned but read nowhere in `run_cycle.py` / `entry_logic.py`.
4. **Rule E names the wrong layer.** The August mean-reversion layer `execution/mr_regime.py` has no size multiplier;
   the constant belongs to the older `strategy/volatility_regime.py`.
5. **The same VIX number drives opposite rules.** `run_cycle.py:1675`: VIX < 14 raises the minimum score by 2 ("low-vol
   grind produces false breakouts") while the regime layer claims low vol means "size up". The size-up does nothing;
   the score gate really cuts trades.
6. **The open field-mixing bug reaches live gates.** `_realized_vol` holds VIX (`volatility_regime.py:119`) or, when the
   VIX fetch fails, SPY 10-day realized vol (`:128`) (`logs/tb_audit_log.md:10201`, open since 2026-09-14). It feeds
   `_main._last_vix` (`run_cycle.py:1156`), so on a fallback day the VIX >30 / >25 / <14 score gates (`:1671-1676`) and
   the VIX > 35 overnight zero (`:1812`) read SPY realized vol against VIX cut-offs. [hypothesis — unverified:
   realized vol usually sits below VIX, so fallback days trip the <14 cut more and the >30/>35 protections less.]
7. A previous design pass (`logs/design_records/regime_state_phase2_design_2026-09-14.md`, `research/regime_empirical.py`
   commit `00774fcf`) assumed the 1.25 had an effect.

## Board recommendation (3/3 unless marked)
1. **Fix the field-mixing bug first** (separate `vix_level` and `spy_rvol_10d`; each gate reads the series it was set
   on; a fallback is labelled). The only item here that changes live behaviour today. Risk-path by frequency (score
   gates) → full gate + owner approval.
2. **Make the logs honest:** correct the "sizing up" / "half size" notes, and log which term bound the final share count
   on every entry.
3. **The 1.25:** LdP sets it to 1.0 (no effect, honesty only); Thorp/Taleb delete the low-vol up-leg and its 12.0
   threshold; Shaw leaves it until a data-derived regime state replaces it. All agree it changes no shares today and
   should not be made to work (index-VIX up-sizing double-counts the per-name vol sizing already in TSMOM / VOTE-5).
4. **Owner decision (risk-path): should the protective cuts (event, high-vol, time of day, session P&L, overnight) start
   working?** That means applying the multiplier where it bites (to `dollar_cap` or the risk budget). It would cut sizes
   on stressed days and can cut trades through the 1-share floor. Needs a replay of logged share counts with and without
   the change first.
5. **Correct Rule E**: name `strategy/volatility_regime.py`, state that the core-tier stacked multiplier does not change
   shares, and drop "DOWN-sizing (protective)".
6. Remove or wire `stop_mult`; retire or ship `research/regime_empirical.py` (log-only, against the no-shadow rule).
7. Add a unit test that pins the share count as a function of the multiplier, so this cannot silently drift again.

## Replay required before item 4 (or any regime-based size)
About a year of Alpaca T1 bars and the logged decision stacks: recompute every core-tier entry's shares under (a) current
code and (b) the multiplier applied before the value cap. Wrong if any logged entry shows the multiplier was the binding
term today. For a regime-based edge claim (LdP): triple-barrier labels in R, purged CV, every variant counted and the
Sharpe deflated; detecting a 0.1R difference needs about 1,570 trades per arm, so live data alone cannot settle it.

## Rev 2 (2026-11-04 evening, Mac session, signed: Claude) — server facts + GAI / Codex round 1 folded

### Server facts (read-only, OCI `logs/mtf_bot.log`, which starts 2026-07-19)
- **Finding 1 confirmed on a real entry.** SOFI short 2026-09-22 16:53 UTC: final `dollar_cap` $74.83 at $17.24 →
  `int(74.83 / 17.24)` = 4; 4 shares filled. Stop distance $1.12 = 6.5% of price; stacked multiplier 0.75 (doldrums), far
  above 0.065, so the value cap bound.
- **A path the record missed: the "Leg2 margin min-2 rescue".** SPY 2026-09-25 (mult 0.32) and NFLX 2026-09-23 (mult
  0.495) both reached 0 shares because `dollar_cap` ($56, $58) was below one share's price — the value cap, not the
  multiplier — and the rescue then set 2 shares. The multiplier did not bind there either (stop/price 0.004 and 0.049).
- **No core-tier entry since 2026-09-25** (52 core entries 07-29 → 09-25 in `trade_events.jsonl`; none after).
- **VIX fallback: 1 day out of 101.** Regime source was VIX on every logged day except 2026-07-23, when SPY 10-day
  realized vol (11.5%) was used and labelled the day LOW_VOL ("sizing up 1.25x"). LOW_VOL fired only on that fallback
  day; HIGH_VOL never fired. On that day `_last_vix` = 11.5 also met the `< 14` score gate (+2 to the minimum score).
  So the field-mixing bug is real and has reached a live gate once in 3.5 months.

### What changed after GAI + Codex (round 1)
GAI: APPROVE-WITH-CHANGES. Codex: REJECT, on (a) its sandbox showing no repository (it could not check the cited lines —
the lines are now pasted verbatim in the counter-prompt), (b) findings 1-2 stated too broadly, (c) unspecified
missing-VIX behaviour, (d) the proposed test would freeze the defect. All four are accepted:
1. **Findings 1-2 bounded:** the multiplier product binds only when it is below stop_distance / entry_price. On every
   logged core entry checked it was not; the replay (below) counts the cases instead of asserting "never".
2. **VIX split, exact behaviour (Codex):** typed fields `vix_level / vix_asof / vix_source` and `spy_rvol_10d /
   spy_rvol_asof`. VIX gates read only a fresh VIX value. **Missing or stale VIX → the VIX-specific score gates and the
   VIX > 35 overnight zero are not applied, trading continues, and the decision is logged `not_applied:
   vix_unavailable`.** No cross-series substitution. (On 07-23 this would have removed the false +2 score gate.)
3. **Low-vol up-leg deleted** with its 12.0 threshold (Codex, Thorp/Taleb; GAI "defensive-only, multipliers in [0, 1]").
   No live behaviour change (it never changed shares).
4. **`stop_mult` removed, not wired** (Codex; GAI notes wiring wider stops without a size cut would raise $ risk).
5. **Structured sizing record** on every core entry in `trade_events.jsonl`: price, stop, stop distance, `dollar_cap`
   after each modifier, raw shares, each multiplier, stacked multiplier, shares after multiplier, value-cap shares,
   rescue applied y/n, Kelly clamp, final requested / submitted / filled, binding constraints[]. Log text corrected
   ("sizing up 1.25x" / "half size, wider stops" removed).
6. **Tests (Codex spec):** table cases around multiplier = stop/price, binding and non-binding positive multipliers,
   zero blockade, stacked product, cap < price → 0 then rescue, ordering against Kelly / VOTE-5 caps, final notional ≤
   cap property, missing-VIX cases; all broker calls mocked.
7. **Rule E text (Codex wording):** "Core-tier size modifiers are inputs; final shares come from the ordered sizing
   constraints in `execution/entry_logic.py`, and each entry logs whether each modifier changed the final share count.
   There is no low-vol up-sizing." Names the producer (`strategy/volatility_regime.py`), the assembly
   (`strategy/run_cycle.py`) and the enforcement point (`entry_logic.py`).
8. `research/regime_empirical.py` stays as offline research (Codex: not a shadow); its header gets a note that the
   1.25 never reached orders.

### Item 4 (make the protective cuts bite) — reviewers split, owner decides
- GAI: yes, apply the multiplier to `dollar_cap`, after a replay shows no conviction trade loses its 1 share.
- Codex: no, not as a bundled product; only factor-by-factor after a replay shows each one improves results; applying
  it to `dollar_cap` can drop 1-share trades (cap $120, price $100, ×0.5 → 0 shares).
- Board (rev 1): needs a replay first.
- **Common ground: nothing activates before a replay.** Recommendation: do not activate in this change; the replay runs
  on the structured sizing records once they exist, factor by factor.

### Scope of the first diff (what the owner approves)
Items 2 (VIX split + missing-VIX rule), 3, 4, 5, 6, 7, 8 above. Risk-path by frequency (the score gates) → full gate.
Expected live effect: none on a VIX day; on a VIX-outage day the VIX gates stop firing on a non-VIX number.
Reversal criterion: any logged entry whose binding constraint is "multiplier", or any VIX gate reading a non-VIX value.

## Rev 3 (2026-11-04 night, signed: Claude) — round 2 folded

1. **The min-2 rescue is not a cap violation; my test spec was wrong.** `execution/entry_logic.py:1448-1478` (Leg 2,
   owner mandate 2026-08-08, margin account): when the equity value cap leaves 0-1 shares, the lot is raised to exactly
   2 only if (a) not a blockade, (b) not a leveraged ETF, (c) the Kelly per-trade risk clamp permits ≥ 2
   (2 × stop distance ≤ `KELLY_MAX_RISK_PCT` × equity), (d) the VOTE-5 vol target permits ≥ 2, (e) live buying power
   affords 2. `dollar_cap` is the EQUITY notional budget, which the rescue is designed to exceed with margin; per-trade
   RISK is never raised. Leg 1 (L1480+) then skips anything below 2 shares. Corrected test property:
   - without the rescue: final notional ≤ `dollar_cap`;
   - with the rescue: shares == 2 AND 2 × stop distance ≤ `KELLY_MAX_RISK_PCT` × equity AND the vol-target count ≥ 2 AND
     buying power affords 2; the record logs `rescue_applied=true` with each condition;
   - the $56 cap / $600 price case: rescue fires only when (c)-(e) hold, else 0 shares and a skip.
   Changing the rescue itself is out of scope (owner mandate; risk-path).
2. **VIX freshness, numerically:** a VIX value is fresh when its fetch time is within 2 × `SCAN_INTERVAL_INTRADAY`
   minutes (paper profile 5 → 10 min) of the decision time, both tz-aware UTC. Boundary tests at exactly 10 min (fresh)
   and 10 min + 1 s (stale). Implausible values (non-finite, ≤ 0, > 150) are treated as unavailable.
   `check_partial_exits`' reader of `_last_vix` moves to the same typed, freshness-aware value.
3. **Reversal criterion corrected:** any VIX rule consuming a non-VIX or stale value. A binding multiplier is recorded,
   not treated as failure.
4. **Separate finding to the owner (not this diff):** no core-tier entry since 2026-09-25; diagnose why before any
   core-tier sizing work.

## Rev 3.1 — round 3 verdicts and final changes (2026-11-04 night, signed: Claude)

| Voice | Round 1 | Round 2 | Round 3 |
|---|---|---|---|
| Board (3 cold seats) | 3/3 approve-with-changes | — | — |
| GAI | approve-with-changes | — | approve-with-changes |
| Codex (reviewer #2) | reject (no repo in sandbox; claims too broad; missing-VIX rule; test spec) | reject (test spec vs min-2 rescue) | **approve** |

Folded from round 3 (GAI):
1. The min-2 rescue logs at WARNING (not INFO) and sets `rescue_applied=true` with each condition in the sizing record.
   Log level only; the rescue logic is unchanged.
2. Cold start: the typed VIX value starts as None, so no VIX gate applies until a fresh VIX is read (the current code's
   `0 < _last_vix` guard already skips 0.0; the typed field keeps that explicit).
3. The "no core-tier entry since 2026-09-25" diagnosis is a separate item for the owner, ahead of any core-tier sizing
   work (it does not block this diff, which is data correctness + logging).

**Alignment reached:** board, GAI and Codex on the Rev 3 / 3.1 scope. Item 4 (making protective cuts bite) stays out of
scope; all voices agree nothing activates before a factor-by-factor replay. Owner approval of the exact Rev 3.1 design
comes next; then the build and the full code gate.

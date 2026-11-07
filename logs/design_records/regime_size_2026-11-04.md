# REGIME-SIZE-1104 — the core-tier size multipliers change no share counts (signed: Claude, 2026-11-04)

Approval-ID: REGIME-SIZE-1104
Status: rev 3.14 FINAL DRAFT (Covers completed 2026-11-06 to every file the rev 3.10-3.14 sections name: consumer table, snapshot writer and cron, pages) for owner approval (2026-11-06: board, GAI, Codex rounds folded; owner chose maximum for the < 14 row 2026-11-06). Rev 3.7 DRAFT — owner direction 2026-11-05 (all VIX sources read and cross-checked; FMP client added to Covers). Approval voided by the Covers change; board + GAI + Codex review, then owner re-approval.
Covers: data/schwab_quotes.py, data/fmp_client.py, config.py, alerts.py, strategy/volatility_regime.py, strategy/run_cycle.py, strategy/regime_state.py, events/macro_risk_index.py, execution/entry_logic.py, execution/exit_logic.py, execution/param_engine.py, execution/risk_manager.py, execution/tier_capital_allocator.py, execution/overnight_capacity.py, research/regime_empirical.py, run_vix_snapshot.py (new), scripts/install_vix_snapshot_cron.sh (new), CLAUDE.md, tests/test_core_sizing_multiplier.py (new), tests/test_vix_source.py (new), tests/test_vix_snapshot.py (new), tests/test_schwab_quotes.py, tests/test_fmp_client.py (new), tests/test_overnight_capacity.py, tests/tests_tier_capital_allocator.py

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

## Rev 3.2 — the real VIX from Schwab (owner direction 2026-11-04 night)

Owner: "We should be able to get the actual box reading now, not some composite reading."

**Measured on the server 2026-11-04 night (read-only, one request):** the owner's Schwab Market Data app returns the
CBOE indexes directly: `$VIX` last 16.61, `$VIX3M` 18.8, `$SPX` 7688.62, each `assetMainType INDEX`, `realtime: true`,
with a `tradeTime` (epoch ms; the VIX print was 21:15 UTC = the 4:15 PM ET settlement). Index quotes carry no bid/ask,
so the existing `nbbo()` path (which validates bid/ask and rejects `$` symbols) cannot read them.

**Design change (adds `data/schwab_quotes.py` to Covers):**
1. New read-only `index_last(symbols, max_age_s)` in `data/schwab_quotes.py`: one quotes request for `$`-prefixed index
   symbols through the same token lock, request budget, kill switch, timeouts and redaction as `nbbo()`; returns
   `{symbol: (last, trade_time_utc)}` only for finite last > 0 whose own `tradeTime` is within the caller's bound; a
   reason per missing symbol; never raises.
2. VIX source order for the regime layer: **(a) Schwab `$VIX`** (live CBOE value, as-of = its own tradeTime);
   **(b) the existing T4 yfinance `^VIX`** if Schwab has no fresh value; **(c) neither → VIX rules not applied**
   (`not_applied: vix_unavailable`), trading continues. **SPY realized volatility is never used as VIX** — it stays a
   separate, labelled `spy_rvol_10d` field.
3. Freshness is measured from the index's own trade time during regular hours (VIX prints every 15 s), bound
   2 × `SCAN_INTERVAL_INTRADAY` (10 min) as in Rev 3.
4. Every regime record logs `vix_source` (schwab / yfinance / none), value, trade time and age.
5. Not in this diff (anti-silo follow-up, separate gated diff): the same `index_last` gives a live `$VIX3M`, which the MRI
   and the composite-regime term structure currently read from yfinance (T4) — CLAUDE.md roadmap "real-time VIX3M
   source investigation" is answered by this measurement.

## Rev 3.3 — VIX-source review round 1 folded (2026-11-04 night, signed: Claude)

Reviewer #2 this round was Gro (Codex at its usage limit until 11:02 PM PT). Points folded:
1. **Index path is separate from the stock-quote path (both reviewers).** `index_last()` never calls `_validate()`,
   `to_schwab()` or `_nbbo_impl()`; it has its own index check: `assetMainType == "INDEX"`, finite `lastPrice` > 0,
   integer `tradeTime`. Index symbols are validated by a local pattern (`$` + letters/digits) inside `index_last` —
   the global `_SYMBOL_RE` is NOT widened.
2. **Freshness from the index's own tradeTime (GAI 4):** age = fetch time − tradeTime. Stale when age > bound. A
   tradeTime more than the existing clock-skew tolerance in the future → stale with reason `future_trade_time` (Gro).
3. **Shared lock and budget, bounded wait (GAI):** `index_last` takes the same token lock and request budget as
   `nbbo()`, and the regime caller passes a deadline (as `nbbo_with_reasons` does) so the run_cycle thread never waits
   past it; on a lock timeout → reason `LOCK_TIMEOUT` → yfinance fallback.
4. **Source switches are visible (GAI):** one WARNING line when the VIX source changes (schwab → yfinance → none and
   back), not one per cycle; every regime record carries `vix_source`, value, trade time and age.
5. Not adopted: Gro's "fall back to a pure SPY realized-vol regime when both VIX sources are stale" — that is the
   substitution the owner ruled out on 11-04 ("not some composite reading"); with no fresh VIX the VIX rules are not
   applied and trading continues. Not adopted: raising the Schwab request budget — one extra request per 5-minute
   cycle; the budget logic already pages on 429s.
6. Tests (with the Rev 3 list): index payload with bid/ask None accepted; `realtime` false or last ≤ 0 rejected;
   stale and future tradeTime rejected; lock timeout → yfinance; yfinance missing → not applied; source-switch WARNING
   once; no equity `nbbo()` behaviour change (existing 37 Schwab tests stay green).

## Rev 3.4 — VIX-source review round 2 (2026-11-04 night, signed: Claude)

| Voice | VIX-source round 1 | Round 2 |
|---|---|---|
| GAI | reject (read index_last as going through nbbo/_validate; corrected: it is a separate path) | approve-with-changes |
| Gro (reviewer #2 fallback; Codex at usage limit until 11:02 PM PT) | approve-with-changes | approve-with-changes |

Folded from round 2:
1. **No stale carry-over (GAI):** when neither Schwab nor yfinance has a fresh VIX, `_refresh` sets the VIX value to
   None and the regime label to NORMAL with `vix_source = "none"`; the previous reading is never kept. The VIX score
   gates and the VIX>35 overnight zero read None as "not applied".
2. **`vix_source`, value, trade time and age are in `_build_result`** (GAI) so every consumer and every record carries
   them.
3. **`$` handling stays local to `index_last`** (Gro round 1); GAI's request to accept `$` is met by that local pattern,
   without widening the shared `_SYMBOL_RE` used by the stock-quote path.
4. Future tradeTime beyond the existing clock-skew tolerance and age over the bound are both rejected, measured
   against `datetime.now(timezone.utc)` (GAI, Gro).
5. The regime size multiplier is neutral (1.0) when `vix_source = "none"` (GAI nit; it changes no shares today).

## Rev 3.5 — data-source board seats folded (2026-11-04 night, signed: Claude)

Board (cold, McKinney + Katsuyama + Harris): 3/3 approve-with-changes. All agree the CBOE index value is the right VIX,
SPY realized vol must never stand in for it, and freshness is measured from the index's own time stamp. Folded:

1. **Read Schwab `$VIX` every cycle (M1).** The detector's 15-minute cache (`volatility_regime.py:45`
   `_refresh_mins = 15`) would make a healthy value look stale under a 10-minute bound. The Schwab read runs every
   cycle (one request per 5 min); the fallbacks keep their own caches. Freshness bound = 2 × `SCAN_INTERVAL_INTRADAY`.
2. **Source order with as-of times on every tier (M2, K1):** Schwab `$VIX` (tradeTime) → FMP `^VIX` (T2, which the MRI
   already reads at `macro_risk_index.py:624`, with its quote timestamp) → yfinance `^VIX` (T4, as-of from the chart
   `regularMarketTime`) → unavailable. A value with no as-of time is unavailable, never "fresh". One shared VIX
   provider serves the regime layer and the MRI, so the bot never holds two VIX numbers.
3. **Every consumer and its behaviour when VIX is unavailable (M3):**
   - Score gates (`run_cycle.py:1671-1676`): not applied (no +2/+3), trading continues.
   - VIX > 35 overnight zero (`run_cycle.py:1812`): sticky for the day — if any fresh reading today was > 35 it stays
     applied (tightens only, as Invariant #11's "stale → last good") (H3).
   - **Stop width and stop-confirm counts** (`execute_entries` vix → `entry_logic.py:1004` stop multiplier, `:704`
     confirm scans, `:1189` correlation cap; `check_exits` → `exit_logic.py:1465` breakeven buffer, `:1776`
     stop-confirm): use the **last good VIX of the session**, else the prior session's close from the highest tier
     with its as-of date, labelled `vix_basis=last_good|prior_close`. Never 0 — a missing feed must not narrow a stop.
   - `regime_state.py` ledger: records the value with its source and as-of, or `unavailable`.
4. **`index_last` has its own index path (M4, K2):** own shape check on `assetMainType == "INDEX"`, requested symbol
   must equal the returned key, finite last > 0, integer tradeTime, `realtime` true (else reason `not_realtime`),
   the existing `clock_ahead` correction applied to tradeTime; its own page kind; reasons returned per call only (never
   the shared `_last_reasons`); field names pinned from the raw 11-04 response in a test fixture.
5. **Plausibility (M5):** no fixed 150 cap. The VIX consumer rejects non-finite or ≤ 0 values only; anything else is
   accepted and the cross-check (item 6) pages an anomaly. No "2× prior close" price-sanity rule on VIX.
6. **Cross-check (K3):** the regime record logs Schwab VIX − FMP VIX (no extra fetch: the MRI already has FMP's);
   an anomaly is paged when the difference falls outside its own rolling distribution (anomaly rule, not a gate).
7. **Deadline and switch (H1, H2):** `index_last` runs through the `nbbo_with_reasons` worker-thread deadline
   pattern; the deadline is derived as the Schwab client's own connect + read timeouts (4 s + 4 s), so the trading
   thread waits at most that and then uses the fallback. A 401/403 kills the Schwab module for the day (same token);
   a 400 / `invalidSymbols` for `$VIX` does not. New flag `SCHWAB_INDEX_ENABLED` (per-feature kill, Rule E).
8. **Runtime probe (Q22/Q23 dispute):** before go-live, run the real `index_last` read-only on the server, including a
   forced token refresh, and one pre-open probe (~8:00 ET) to see whether Schwab carries the overnight VIX session.
9. **Anti-silo follow-ups (separate gated diffs):** MRI VIX + VIX3M term structure (`macro_risk_index.py:624, 692`,
   today mixing live FMP VIX with yfinance's prior-day VIX3M), composite regime (`volatility_regime.py:244-282`),
   F6 trigger (`f6_empirical_trigger.py:120`), overnight-capacity VIX scale (`overnight_capacity.py:268`),
   options/0DTE, scanner and dashboard.

Required-question answers corrected by the board: Q7 (full consumer list above), Q12 (stop width reads VIX —
handled by item 3, never narrowed by a missing feed), Q13 (every tier now has an as-of time), Q17 (every-cycle
Schwab read), Q26 (Schwab → FMP T2 → yfinance T4), Q28 (deadline = client timeouts, worker thread), Q5 (no new fixed
cap), Q22/Q23 (real-function probe incl. token refresh).

## Rev 3.6 — GAI round 3 on Rev 3.5 folded (2026-11-04 night, signed: Claude)

GAI: approve-with-changes. Reviewer #2: Codex at its usage limit until 11:02 PM PT and Gro refuses the prompt size
(HTTP 413); the Codex pass runs on the code diff at pre-ship. Folded:
1. **Which value is used:** for the live VIX rules, the single highest-tier value that is fresh (never an average of
   sources). When no tier is fresh, the stop-width basis is the value with the most recent as-of time across the
   session's last good reading and every tier, labelled with its source and as-of.
2. **The sticky VIX > 35 overnight zero is persisted** in the regime state file (atomic write) with its date, so a
   mid-day restart keeps it.
3. **`SCHWAB_INDEX_ENABLED` is checked before any Schwab call**; off or killed → straight to the FMP tier with no
   connection attempt.
4. Every entry's decision record carries `vix_value`, `vix_source`, `vix_as_of` and `vix_basis`.

## Rev 3.7 — owner direction 2026-11-05: read and cross-check every VIX source (DRAFT — review pending, signed: Claude)

Owner, 2026-11-05: Covers widened to the FMP client "yes and should all available sources should be checked and
cross referenced for accuracy." This replaces the fallback-only order of Rev 3.5 item 2 / Rev 3.6 item 1. Widening
Covers voids the 11-05 approval of this record; it returns to the owner after board + GAI + Codex review.

1. **Every source is read on every regular-hours cycle**, each with its own as-of time: Schwab `$VIX` (owner's feed),
   FMP `^VIX` (T2, new read-only quote function with the quote timestamp in the FMP client module, shared with the MRI
   so the bot makes one FMP VIX request per cycle and holds one VIX number), yfinance `^VIX` (T4, regularMarketTime).
   Request budgets: Schwab 1 per call (existing budget logic); FMP shared with the MRI's existing per-cycle read
   (no new daily calls); yfinance about 1 per cycle (rate and failure behaviour for reviewers to judge).
2. **Cross-reference:** each cycle compares every pair of fresh sources. The tolerance is each pair's own rolling
   difference distribution (no fixed number; cold start: no agreement verdict, labelled `uncorroborated`).
   - The value used for the VIX rules is the highest-tier fresh value that at least one other fresh source corroborates.
   - Only one fresh source → used, labelled `uncorroborated`.
   - The highest tier contradicted while the other two agree → the agreeing pair's higher tier is used, the anomaly is
     paged once per source per session, and the event goes to the anomaly rules (CLAUDE.md ANOMALY rule).
   - No two sources agree and three are fresh → the median of the three is used, paged.
3. Every regime record logs all three values, their as-of times and ages, each pairwise difference, the agreement verdict
   and which value was used.
4. Slice 1 (logs/wip/regime_size_1104_slice1_2026-11-05.patch, Schwab → yfinance fallback) is rebuilt on this design
   before it ships.

Covers (proposed): data/schwab_quotes.py, data/fmp_client.py, config.py, strategy/volatility_regime.py,
strategy/run_cycle.py, strategy/regime_state.py, events/macro_risk_index.py, execution/entry_logic.py,
execution/exit_logic.py, research/regime_empirical.py, CLAUDE.md, tests/test_core_sizing_multiplier.py (new),
tests/test_vix_source.py (new), tests/test_schwab_quotes.py, tests/test_fmp_client.py

## Rev 3.8 — board round on Rev 3.7 folded (2026-11-06, signed: Claude)

Board (cold; McKinney, Katsuyama, Taleb masked-loss seat): McKinney and Katsuyama approve-with-changes; Taleb rejects
Rev 3.7 as written. GAI (Rev 3.7) asked that a lower tier never outvote the top tier. Codex was at its usage limit and
Gro refused the prompt size (HTTP 413). Facts measured this turn (OCI, read-only, 2026-11-06 after the close):
- FMP `stable/quote?symbol=^VIX` returns `"timestamp": 1793999701` (21:15:01 UTC, the 4:15 PM ET settlement), the same
  as yfinance `regularMarketTime` 1793999701; both carry their own as-of time. The legacy `api/v3/quote` path is
  retired ("Legacy Endpoint"); the MRI already uses `stable/quote` (macro_risk_index.py:571).
- The MRI's FMP VIX read failed 344 times since 2026-07-20 (`MRI VIX: FMP None` / `FMP returned None` lines; e.g.
  20/day on 07-20..07-22, 17 on 11-03). Its HTTP status is logged at DEBUG only (macro_risk_index.py:577), so the cause
  is unknown — an anomaly under the ANOMALY rule, investigated in this diff.
- The MRI reads VIX every 10 minutes (macro_risk_index.py:108) after the regime read, keeps an FMP value up to 30 min
  (:638) and otherwise a yfinance close with no as-of time (:516-532); `_fmp_quote` keeps no timestamp (:564-586).
- Regular-hours as-of delay of FMP and yfinance ^VIX is NOT measured (only the settlement print was readable).

Changes (supersede Rev 3.7 items 1-2 where they differ):
1. **One VIX provider, three concurrent reads, one deadline (Katsuyama).** Schwab, FMP and yfinance are read at the
   regime read (the regime layer's `get_regime()` call in the cycle) concurrently in worker threads under one deadline
   derived from the Schwab client's own `_timeouts()`; a source that misses it is missing for that cycle (reason
   `deadline`). Worst case added to the trading thread = that one deadline, not the sum. The MRI consumes the
   provider's cycle value; its own FMP VIX read, 30-min cache and yfinance close for VIX are retired (one VIX number in
   the bot — Rev 3.5 item 2).
2. **FMP as-of from its own `timestamp` field** (pinned from the 11-06 raw response in a test fixture; never the fetch
   time). FMP HTTP status and error logged at WARNING. A per-day FMP call counter in the client module so the extra VIX
   reads (~78 per day plus the MRI's USDJPY) cannot push the account past its daily limit unseen.
3. **The value used is the HIGHEST fresh source (Taleb).** Every VIX rule (score gates, VIX > 35 overnight zero, stop
   width, stop-confirm, breakeven buffer) reads the maximum of the fresh values: a lower tier can raise the VIX used,
   never lower it. This replaces Rev 3.7's corroboration / outvote / median selection. Failing input it removes:
   Schwab 37.0 real-time, FMP 29.8 and yfinance 30.1 delayed and agreeing → Rev 3.7 used 29.8 (no VIX > 35 zero, no +3
   gate, narrower stop); Rev 3.8 uses 37.0. A false-high costs a wider stop bounded by the per-trade Kelly risk clamp;
   a false-low narrows stops and drops the overnight zero. With the maximum, the value cannot flap between sources.
   **3b. No source is ever dropped from the maximum; corroboration decides only whether a high is held for the day
   (Taleb re-check of an earlier 3b/3c draft, which could drop a real Schwab spike or a real lower-tier spike while
   Schwab was frozen, and so produce a false low).** Every cycle the value used is the plain maximum of the fresh
   sources, in the cold start as well. Each VIX rule acts on that cycle's value: a value above 35 applies the overnight
   zero for that cycle whatever its source. The sticky VIX > 35 flag (held for the rest of the day, persisted) is set
   only when Schwab's own fresh value is above 35, or when two fresh sources are above 35. A lone lower-tier value
   above 35 is paged and acts for its cycle only. Inputs checked against the record as written:
   - yfinance bad print 40.0, Schwab 20.0, FMP 20.1 → 40.0 for that cycle (wider stop bounded by the Kelly risk clamp,
     +3 gate, overnight zero for that cycle), paged, sticky NOT set; next cycle back to 20.x if the print clears.
   - Schwab 37.0 real-time, FMP 29.8 / yfinance 30.1 delayed → 37.0, sticky set (Schwab above 35).
   - Schwab 24 at 9:50 and 37 at 9:55 on its tape, FMP 28.0 / yfinance 28.2 as-of 9:52 → 37.0 (nothing dropped).
   - Schwab frozen 20.0 with a fresh tradeTime, FMP delayed 20.1, yfinance 34.0 → 34.0; Schwab and FMP page as
     disagreeing with yfinance once their as-of times pass; real VIX 38 seen by yfinance → 38.0 and the cycle's
     overnight zero applies (sticky only once a second source confirms).
   - Schwab missing, FMP 37.0, yfinance 30.1 → 37.0, overnight zero for that cycle, sticky not set.
   The cross-check (item 4) changes no value used; it pages and feeds the anomaly rules.
   Every disagreement page and record carries each source's as-of time and the as-of gap used in the alignment
   (network lag vs bad data is then visible).
4. **Cross-check = accuracy check and anomaly paging, not selection (GAI, McKinney).** Each delayed source is compared
   with Schwab's own tape around that source's as-of time (Schwab's per-cycle tape for the session, persisted atomically
   in the regime state file), not with Schwab's latest: the residual is how far the delayed value lies outside the
   range of the two tape samples that bracket its as-of time. No tape sample before the as-of (session open) → no
   verdict (`uncorroborated`). FMP and yfinance are compared with each other at their own as-of times the same way when
   both are delayed. A pair disagrees when its residual is outside the rolling quantile of its own past residuals
   (seeded from the item-5 probe and persisted; cold start → `uncorroborated`, no verdict). Flagged cycles are
   excluded from that window for the rest of the session only, so a lasting change in a source's delay re-enters the
   tolerance the next day. Disagreement is paged once per source per session and goes to the anomaly rules. A Schwab
   value frozen with a fresh tradeTime shows as a disagreement and, being lower, is overridden by the maximum.
   Machine check (Q40): no regime record has a value used below any fresh source value of the same cycle.
5. **Measure before the tolerance code ships (Katsuyama, Q22):** one regular-hours read-only server probe logging
   fetch time − as-of for each source every cycle (Mon 2026-11-09). A source delayed beyond the 10-min bound is stale
   for the VIX rules and is still cross-checked at its own as-of time.
6. Every regime record logs each source's value, as-of, age, reason if missing, each aligned residual, the verdict and
   the value used, as typed fields.

Required-question answers corrected for Rev 3.8: Q2 — the maximum can only raise VIX, so on disagreement days the
+2/+3 gates fire more (fewer trades for those minutes) and the VIX < 14 +2 gate fires less (more trades); measured in
the replay and for the owner to approve. Q5 — tolerance derived as above; the 14/25/30/35 cut-offs stay inherited
static (flagged; rolling percentiles are the Q6 forward pass). Q9 — add replays of a VIX-spike day (value used vs
Schwab's), a frozen-Schwab day, a spike between tape samples, a lone bad print and a cold start; the replay counts the
entries whose score gate changes under the maximum (Q2). Q12 — with item 3b no source is dropped, so a feed failure
or a bad low print can never narrow a stop or remove the overnight zero. Q17/Q22 — the item-5 probe also records each
source's delay against Schwab's tape during moves. Q13/Q26 — every source has its own as-of; the MRI retires its separate VIX. Q17/Q22 — FMP and
yfinance regular-hours latency probe (item 5). Q19 — the owner approves this exact Rev 3.8. Q28/Q36 — three concurrent
reads, one deadline (about the Schwab client timeouts). Q44 — Rev 3.8 review: board above, GAI + Codex next.

## Rev 3.9 — GAI + Codex round on Rev 3.8 folded (2026-11-06, signed: Claude)

Codex (reviewer #2): reject of Rev 3.8; GAI: changes requested. Folded:
1. **Each VIX consumer uses the value that makes IT more loss-protective (Codex 1).** Direction verified at source:
   - `exit_logic.py:1776` stop-confirm: VIX ≥ `VIX_STOP_WIDEN_THRESHOLD_2` → 1 breach instead of 3 (a higher VIX
     exits FASTER) → maximum.
   - `exit_logic.py:874` stressed T2 partial at VIX > 30 → profit taken earlier → maximum.
   - `param_engine.py:174` `get_be_buffer_mult` (overnight breakeven early-exit buffer): 0.25 / 0.40 / 0.50 ATR as VIX
     rises, capped 0.65 ATR — a higher VIX DELAYS this early exit → **minimum** of the fresh values. The hard stop
     (1.25 ATR) is untouched by VIX refreshes; a broker stop is never widened after entry because of a VIX refresh.
   - Entry side (score gates, entry stop width and confirm scans `entry_logic.py:1004, :704`, correlation cap `:1189`,
     VIX > 35 overnight-entry zero) → maximum.
   Each consumer's direction is pinned in a test so a later change to a consumer cannot silently flip it.
2. **The trading thread never waits on a VIX feed (Codex 2).** The provider refreshes in its own background worker
   (all three reads concurrent, each bounded by its client's own timeouts); the cycle reads the latest COMPLETED
   snapshot under a lock and never blocks on a fetch, so the added wait before `check_exits()` is the snapshot read
   only. Freshness still comes from each value's own as-of time, so a hung refresher shows up as stale values (VIX
   rules not applied; stop basis per Rev 3.5 item 3), plus a page when the snapshot itself is older than 2 cycles.
3. **Slices (Codex 3).** Slice 1 = shared provider, per-consumer policy above, sticky rule, typed records with the
   raw aligned residuals and the source of the maximum (GAI nit), MRI on the shared provider. The disagreement
   tolerance and paging ship as a later slice, after the item-5 probe data exists and the estimator is frozen
   (quantile level, lookback, minimum samples, bracketing rules) and replayed; they change no value used, so slice 1
   does not depend on them. A daily disagreement count goes in the session summary (not silenced by once-per-session
   paging).
4. **Not adopted (GAI):** a "max > 1.5 × median → use the median" cap and a "3 consecutive cycles → sticky" rule.
   Both are fixed numbers in risk-scope code (project rule: no unpaired static thresholds). The first would let the
   median outvote a real high — the false-low case the Taleb seat rejected. On oscillation: a lone source above 35
   applies the overnight-entry zero on every cycle it is above 35, so each cycle acts on that cycle's maximum; nothing
   toggles that the cycle's value does not.
5. **Answers corrected (Codex 4).** Q2: the maximum can mean fewer entries in minutes when sources disagree upward —
   an owner decision, counted by the replay before he decides. Q12: corrected — with ONE fresh source that is wrong
   low and the others missing, the value is wrong low; no design can see past its only source; this exposure exists
   today and the design narrows it. Q19: the owner has not yet approved this design (only the 11-05 direction to
   read and cross-check every source). Q28: no added wait on the trading thread beyond the snapshot read.

## Rev 3.10 — confirmation round on Rev 3.9 folded (2026-11-06, signed: Claude)

Codex, GAI and the Taleb masked-loss seat each asked for changes on Rev 3.9; none rejected it. This section is the
selection rule of record and supersedes Rev 3.8 items 3 / 3b and Rev 3.9 item 1 wherever they differ.

1. **Per-consumer selection table** (direction verified at source; each pinned in a test):
   | Consumer | Where | Value used |
   |---|---|---|
   | +2 / +3 score gates at VIX > 25 / > 30 | run_cycle score gates | maximum |
   | +2 score gate at VIX < 14 | run_cycle score gates (:1675) | **owner decision** (below) |
   | entry stop width (continuous scalar), entry confirm scans, correlation cap | risk_manager.py:1809-1828, entry_logic.py:1004, :704, :1189 | maximum |
   | VIX > 35 overnight-entry zero, this cycle | run_cycle (:1812) | maximum |
   | sticky VIX > 35 (rest of day) | regime state | Schwab's own value > 35, or two fresh sources > 35 |
   | exit stop-confirm (1 breach at VIX ≥ 30, else 3) | exit_logic.py:1776 | maximum |
   | stressed T2 partial at VIX > 30 | exit_logic.py:874 | maximum |
   | overnight breakeven early-exit buffer | param_engine.py:174 via exit_logic.py:1465 | minimum |
   | tier allocator "crash" regime (raises exposure) | tier_capital_allocator.py:1181-1212 | the sticky-qualified value (Schwab, or two sources agreeing) — never a lone maximum |
   | tier allocator "risk_on" (low_vol → larger gross multiplier) | tier_capital_allocator.py:553/568 | maximum |
   | volatility_regime HIGH_VOL (size ×0.5, stop ×1.5) / LOW_VOL labels | strategy/volatility_regime.py | maximum |
   | overnight-K VIX scale (tightener), MRI VIX component | overnight_capacity.py, macro_risk_index.py | maximum |
   F6's trigger reads its own yfinance daily closes (f6_empirical_trigger.py:114), outside this provider — unchanged
   here, listed for the anti-silo follow-up.
   Failing input removed by the allocator row: Schwab missing, yfinance bad print 40.0, FMP 20.1, SPY −8.5%, VIX3M 30 →
   a lone maximum (term ratio 1.33) would have switched on the crash allocation; with the sticky-qualified value it
   does not. (Verified at source 2026-11-06: the crash test is tier_capital_allocator.py:1197-1203; the allocator is
   globally off today, `config.TIER_CAPITAL_ALLOCATOR_ENABLED = False` (config.py:1030), so this row binds when it is
   re-enabled.)
2. **Stale snapshot errs to the fast exit (Taleb 3).** "Last good" is kept per consumer direction. Once the snapshot
   is older than the 2-cycle page, stop-confirm uses 1 breach and the breakeven buffer uses the minimum last-good
   value; entry consumers use the maximum last-good value (Rev 3.5 item 3 basis). Failing input removed: refresher
   hangs at 10:00 with VIX 22, real VIX 33 at 10:30, NVDA long breaches its stop → exit on the first breach, not the
   third.
3. **Sticky state on restart (GAI):** the sticky flag is reloaded from the regime state file before the first
   `check_exits()`; if the file is missing or corrupt it is rebuilt from the day's regime records; if both are
   unavailable the flag is treated as set until the first fresh Schwab reading of the day at or below 35, and paged.
4. **Records (Codex 2, GAI):** typed fields per cycle: every source's value, as-of, age and missing reason;
   `fresh_sources`; for each consumer row above the value used and its source; `overnight_zero_cycle`,
   `overnight_zero_sticky` and the sticky-setting evidence; snapshot age; fallback reason when no source is fresh.
5. **Machine checks (Q40, replaces the Rev 3.8 single check):** per cycle, every "maximum" consumer's value equals the
   maximum of the fresh values, the breakeven value equals the minimum, the allocator crash value is sticky-qualified,
   and the sticky flag is never set by a lone lower-tier value.
6. Not adopted: GAI's fixed 2-second ceiling — after Rev 3.9 item 2 the trading thread no longer waits on any fetch,
   so no ceiling applies on that thread; the background refresher keeps each client's own timeouts.

**Owner decision — VIX < 14 +2 gate:** with Schwab 13.2 real-time and FMP 14.3 delayed, the maximum (14.3) turns
the gate off (more entries, score-9 trades go through); the minimum (13.2) keeps it on (fewer entries). The
consistent loss-protective rule is the minimum; the data-collection goal favours the maximum. Both are counted in the
replay before the owner chooses.
**Owner choice, 2026-11-06: `maximum`** ("More trades for a vix under 14 until the vix moves above then more
conservative"). `VIX_LOW_GATE_POLICY = "maximum"`: the < 14 gate uses the highest fresh reading; above 14 the
existing maximum-based > 25 / > 30 gates apply unchanged.

Answers corrected: Q2 — two owner decisions: fewer entries in minutes when sources disagree upward (> 25 / > 30
gates), and the < 14 band choice above. Q11 — the allocator's crash and risk_on paths set gross exposure (risk-path);
handled by item 1. Q12/Q13 — a stale snapshot now errs to the fast exit (item 2). Q19 — not yet approved by the owner;
the package carries this table.

## Rev 3.11 — owner direction 2026-11-06: read VIX at the Day tier's 2-minute pace and alert every tier on a spike

Owner, 2026-11-06: "Why not just have the vix reading happen every 2 mins like the day tier? It could alert the
other tiers if the vix spikes." Measured: the Day tier runs as its own process every 2 minutes (OCI crontab
`*/2 * * * * … run_day_tier.py`; `config.DAYTRADE_SCAN_INTERVAL_MIN = 2`, config.py:826); the main cycle is 5 minutes.

1. **Cadence from existing settings, no new number:** the background refresher (Rev 3.9 item 2) reads Schwab `$VIX`
   every `DAYTRADE_SCAN_INTERVAL_MIN`; FMP and yfinance stay at `SCAN_INTERVAL_INTRADAY` (they are delayed feeds, and
   FMP's free tier is 250 calls/day: a 2-minute FMP read would need ~195 calls a regular-hours day on its own).
2. **One shared snapshot for every tier:** the refresher writes the snapshot atomically to `data/state/vix_snapshot.json`
   (values, as-of times, per-consumer values, sticky flag, spike state). The Day tier process, Swing, trend_night and
   QHM read that file instead of fetching VIX themselves; a missing or stale file → each reader treats VIX as
   unavailable and uses its own Rev 3.10 last-good direction. Freshness is still each value's own as-of time.
3. **Spike alert:** a spike is a 2-minute change in Schwab's own `$VIX` outside the rolling quantile of its own past
   2-minute changes (same estimator slice as the cross-check tolerance: ships after the item-5 probe data, which now
   also records the 2-minute Schwab tape). On a spike: one page, `spike` set in the snapshot with its time and size,
   and every tier's next read sees the new maximum within one refresh. The alert itself changes no value used; what a
   tier does is its consumer row in Rev 3.10's table (e.g. a spike above 30 makes the main cycle's stop-confirm 1
   breach on its next run; a spike above 35 applies the overnight-entry zero).
4. **Day tier wiring is a separate gated diff:** which Day-tier rules should read VIX (none today — to verify at
   build time) is a design question for that diff; slice 1 only publishes the snapshot.
5. Schwab budget: one `$VIX` request per 2 minutes (~195 per regular-hours day) through the existing request budget,
   which pages on 429s.

## Rev 3.12 — confirmation round on Rev 3.11 folded (2026-11-06, signed: Claude)

Codex rejected Rev 3.11 (refresher owner process, reader cold start, the < 14 row undecided); GAI asked for changes.
1. **One writer, named (Codex 2; Katsuyama K1 — own process, not a thread):** the refresher is its own short-lived
   cron process, `run_vix_snapshot.py`, scheduled every `DAYTRADE_SCAN_INTERVAL_MIN` during regular hours on OCI, under
   `flock` on `data/state/vix_snapshot.lock` (the Day tier's pattern) — a second copy exits without writing. A fresh
   process each run means an uncaught error kills only that run, and a main-service restart (e.g. an RTH deploy) does
   not stop the snapshot. It reads Schwab every run; FMP and yfinance only when that source's own last as-of is older
   than `SCAN_INTERVAL_INTRADAY`. Writes via the existing `_atomic_write_json` pattern (temp file, fsync, replace) with
   `written_at` and `schema_version`; readers must run as the same OCI user (file mode 600 — verified at build). A page
   fires when the snapshot is older than 2 refresh intervals. Runtime test: `written_at` advances every refresh during
   regular hours, and only the lock holder calls Schwab.
   **The main cycle keeps its own in-memory per-direction last-good (Taleb T3)** and reads the file once per cycle, so a
   dead writer never removes protection from the main cycle; it falls to the Rev 3.10 item 2 stale rules.
2. **Reader cold start (Codex 3):** the snapshot file carries the per-consumer last-good values with their as-of
   times, and the refresher writes the prior session's close (highest tier, with as-of date) into it after the close,
   so a newly started reader always has a basis. If the file is missing or unreadable: page; exits use the fast-exit
   side (stop-confirm 1 breach, breakeven buffer at its smallest VIX band); no new overnight exposure that cycle; entry
   stop width uses the prior-close basis if any reader-side copy exists, else the continuous scalar's floor is NOT used
   — the entry is skipped for that cycle (no invented neutral VIX); score gates are not applied.
3. **Sticky-qualified value defined (GAI):** `max(Schwab fresh value, second-highest fresh value)` — a lone lower-tier
   high never qualifies; any two sources agreeing on a high do. Used by the sticky VIX > 35 flag and the allocator crash
   row.
4. **Spike flag (GAI):** cleared when Schwab's next 2-minute change is back inside its own distribution; never carried
   into the next session.
5. **Policy mismatch (GAI Q40):** a cycle whose consumer values do not match the table pages and logs; it never aborts
   the cycle (aborting would skip `check_exits()`).
6. **Not adopted (GAI):** maximum last-good for the breakeven buffer when the snapshot is stale. A larger buffer delays
   the early exit (param_engine.py:174); the Taleb seat and Codex both require the fast-exit side; 2-1 with the board
   seat — the minimum stays.
7. Replay cases added (Codex Q9): the 13.2 / 14.3 case under both owner choices, and a second-writer start.
8. **The < 14 row is the owner's decision in the approval package**; no code before he chooses (Q19).
9. **Board (Katsuyama + Taleb) on Rev 3.11, both changes requested:** verified at source that no tier outside the main
   cycle reads VIX today (no "vix" in run_day_tier.py, day_trade_manager.py, swing_breakout_manager.py,
   swing_overnight.py, quarterly_hold_manager.py), so the shared file removes no protection today; reader fallbacks are
   fixed now so it cannot when readers are wired.
10. **A bad read is a missing read (K2):** a reader treats a parse failure, a missing field or an unknown
   `schema_version` as "file missing" (item 2 rules), never crashes and never uses part of the file.
11. **Rev 3.11 item 5 corrected (K3):** the Schwab request budget does NOT page on 429s today (data/schwab_quotes.py
   counts and backs off only, :657-671, :1102-1104); this diff adds one page per session on a `$VIX` 429.
12. **Spike field has three states — none / spike / blind (Taleb T1):** `blind` when there is no fresh Schwab reading
   (e.g. the index call refused and disabled for the day, schwab_quotes.py:1098) or two consecutive Schwab readings
   carry the same as-of time (frozen tape); one page naming the affected tiers when it turns blind. Pinned in a test.
13. **One owner for the sticky flag:** the snapshot file owns it; the regime state file copies it.
14. Q26 corrected: Schwab `$VIX` is the owner's approved read-only Schwab Market Data API (outside the T1-T4 table);
   FMP `^VIX` is T2; yfinance `^VIX` is the permitted T4 VIX source.

## Rev 3.13 — confirmation round on Rev 3.12 folded (2026-11-06, signed: Claude)

Codex and GAI each asked for changes on Rev 3.12; neither rejected it. Board seats were folded in Rev 3.12.
1. **No VIX basis at all → no new entry that cycle (Codex):** supersedes the "score gates are not applied" clause of
   Rev 3.12 item 2. When a reader has no snapshot, no readable file and no prior-close basis, every new entry whose
   VIX-dependent gate cannot be evaluated is skipped for that cycle and paged; exits still run on the fast-exit side.
   Input removed: file missing, real VIX 33, a candidate that passes the base score but fails the +3 gate → previously
   entered, now skipped until a basis exists. The main cycle keeps its in-memory last-good, so this applies to a cold
   reader process only.
2. **Write failure (GAI):** an `OSError` at write or fsync pages and leaves the previous file untouched (`written_at`
   does not advance); a partial file is never visible because the replace is atomic.
3. **Reads need no lock (GAI 2, answered):** the writer replaces the file with `os.replace`, so a reader opens either
   the old or the new complete file, never a partial one; the read is a plain non-blocking open.
4. **The < 14 row is a config setting (GAI 3):** `VIX_LOW_GATE_POLICY` in config.py holds the owner's choice
   (`minimum` or `maximum`); its value is the owner's decision in the approval package, and a test pins that the code
   reads it.

## Rev 3.14 — final changes from the Rev 3.13 confirmation (2026-11-06, signed: Claude)

Codex and GAI both returned approve-with-changes on Rev 3.13 (no reject from any voice in that round). Folded:
1. **Outside regular hours (Codex 2):** a post-close run of the writer records the day's settlement value of each
   source with its as-of date. A reader outside regular hours uses that same-date settlement as its basis (a defined
   value, not "stale"); with no same-date settlement it follows the no-basis rule (no new overnight exposure). Note:
   trend_night enters at ~12:40 PT (15:40 ET, inside regular hours, handoff 11-01), so it reads a live snapshot today.
2. **Main cycle reads the file twice (GAI 1):** at cycle start and again just before the entry block; exit consumers
   update their in-memory per-direction value at each read, so a sticky flag set mid-cycle reaches the entry gate.
3. **`VIX_LOW_GATE_POLICY` validation (GAI 2):** a value other than `minimum` / `maximum` → `minimum` and a page. Its
   configured value is the owner's choice in the approval package (Codex 1), set before any commit.
4. **Write durability and audit (GAI nits):** the writer also fsyncs the directory after the replace, and the snapshot
   carries a `sequence` number that every regime and entry record logs.

## Required questions
Q1: Data/edge quality: the score gates and the entry record stop being contaminated by a non-VIX number (07-23: SPY rvol 11.5 read as VIX added +2 to the minimum score), and every entry logs which limit set its share count — the input edge evaluation needs. Not conservative: it removes a false brake.
Q2: On normal days no change. On a VIX-outage day trades scoring between the base minimum and the false VIX-raised minimum now enter (more trades). With Schwab as primary VIX, outage days should become rarer. Deleting the inert 1.25 changes no count (it never changed shares).
Q3: Yes — the field-mixing is a live correctness bug (1 of 101 days so far) and the sizing logs overstate what happens. Simpler: typed VIX vs SPY-rvol fields and deleting inert settings; nothing new is activated.
Q4: logs/design_records/regime_state_phase2_design_2026-09-14.md and research/regime_empirical.py assumed the 1.25 acted (superseded on that point); logs/tb_audit_log.md:10201 field-mixing item (open since 09-14, this closes it); SCHWAB-QUOTES-1103 (#837/#838) provides the client; no open PR touches strategy/volatility_regime.py (checked origin/main 11-04).
Q5: VIX cut-offs 14/25/30/35 and the +2/+3 score steps are inherited and unchanged here (static, flagged); freshness bound is derived from the scan interval; the deleted 1.25 and 12.0 are removed, not replaced. Forward pass (Q6) is to make the cut-offs rolling percentiles.
Q6: Exposed assumption: one absolute VIX level means the same thing in every regime, and an intermediate multiplier equals real exposure. Next build: rolling-percentile VIX and VIX/VIX3M term-structure state from the live Schwab indexes, with per-factor outcome attribution from the new sizing records.
Q7: VIX feeds the score gates, the VIX>35 overnight zero, the entry stop multiplier / confirm scans / correlation cap (entry_logic.py:1004, :704, :1189), the exit breakeven buffer and stop-confirm (exit_logic.py:1465, :1776), check_partial_exits and regime_state.py; follow-ups: MRI, composite regime, F6 trigger, overnight capacity, options. Rev 3.5 item 3 gives each its unavailable behaviour. Each consumer reads the typed field with its own freshness; unavailable = rule not applied, logged. Testable per consumer.
Q8: Measured: 07-23 fallback in logs/mtf_bot.log ("Volatility regime: LOW_VOL (SPY realized vol (VIX fallback): 11.5)"); 1 of 101 days; SOFI 09-22 share count = int(74.83/17.24) = 4; Schwab $VIX 16.61 returned live 11-04. No performance claim is made.
Q9: Replay of the logged core entries 07-29 → 09-25 (52) recomputing share counts and binding limits; the 101 logged regime days re-evaluated with typed fields (expected: only 07-23 changes — its +2 gate disappears). Wrong if any VIX rule reads a non-VIX or stale value after ship.
Q10: No validated edge is held back; this is correctness and honest logging.
Q11: Frequency can rise on outage days (score gate no longer falsely applied) → risk-path by frequency; board seats reviewed (3/3) incl. Thorp + Taleb (masked-loss). Size: no change (inert settings removed; the min-2 rescue untouched).
Q12: Stop width reads VIX; Rev 3.5 keeps it on the last good / prior-close VIX when the live value is missing, so a feed failure can never narrow a stop. No change to the 7% kill switch, never-mask-a-loss or paper=True. Data tiers: VIX moves to the owner's Schwab feed (owner direction 11-04) with yfinance T4 kept as fallback; SPY rvol stays T1 Alpaca.
Q13: (Rev 3.7) Each of Schwab, FMP and yfinance is read every cycle; a source that is missing, stale, non-finite or ≤ 0 drops out of the cross-check. No fresh source → VIX rules not applied, trading continues, logged; stop width uses the last good / prior-close basis (Rev 3.5 item 3). Never a substitute series.
Q14: No shadow: the VIX split acts live; the sizing record logs what live code did. research/regime_empirical.py stays offline research (not in the trading process).
Q15: Yes — each entry's sizing record carries every modifier, dollar cap step, the value cap, the rescue conditions, Kelly clamp and binding limits; each regime record carries vix_source, value, trade time, age and the rule outcomes.
Q16: Proof lines: `regime` record with `vix_source` on every cycle; `sizing` record on every core entry. Post-live: replay the first full session's records against raw logs and Schwab/yfinance values.
Q17: Read-only server probe of index_last before ship; first session: every cycle has vix_source=schwab with age < 10 min during regular hours.
Q18: Yes, reopened: the "sizing up 1.25x" / "half size" logs were taken as true for months; the design was re-examined against real share counts.
Q19: Yes (score gate behaviour on outage days, regime source) — owner approves this exact Rev 3.2 design before commit.
Q20: Prompts neutralised (prior votes stripped; the bias gate refused two drafts until they were); logs checked on the server; board, GAI, Codex reviewed (3 rounds); the Schwab index source was measured, not assumed.
Q21: No broker calls added; tests mock Schwab and yfinance HTTP; no test reaches the broker.
Q22: Not a fail-closed control (fails open: rule not applied). The Schwab index read was run read-only on the server 11-04.
Q23: Full-path test: run_cycle with mocked Schwab fresh / stale / missing and yfinance fresh / missing, through the score gates and an entry's sizing record, before calling it ready.
Q24: The claim "Schwab returns the CBOE VIX" was checked at source in this turn (HTTP 200, $VIX 16.61, realtime true).
Q25: The VIX-vs-realized-vol relationship is not asserted (labelled hypothesis in the record); fallback frequency measured from 101 days of logs.
Q26: (Rev 3.7) VIX: Schwab Market Data `$VIX` (owner's account), T2 FMP `^VIX`, T4 yfinance `^VIX` — all read each cycle with their own as-of times and cross-checked (Rev 3.7 item 2); none fresh → unavailable. SPY rvol: T1 Alpaca bars, separate field.
Q27: Records append to logs/trade_events.jsonl (one JSON line per record via the existing trade logger); no new state file. Schwab token/budget files keep their existing atomic writes.
Q28: strategy/volatility_regime.py reads VIX on the run_cycle thread before the score gates (strategy/run_cycle.py ~L1150-1680); index_last is bounded by the Schwab client timeouts (4 s connect / 4 s read) and the caller passes a deadline so check_exits is never delayed beyond it; entry_logic writes the sizing record after the final share count.
Q29: High for a small diff: it fixes contaminated gate inputs and creates the per-entry sizing data every later sizing decision needs; below diagnosing why core-tier entries stopped after 09-25 (flagged separately).
Q30: Missing professional pieces: typed, time-stamped market features with provenance; explicit constraint precedence in sizing; per-entry binding-limit attribution. Underweighted risk: interacting multipliers that look active but never bind.
Q31: No edge claim; no variants tried for performance. Regime-based sizing stays inactive until a factor-by-factor replay with purged CV and deflated Sharpe.
Q32: Replay uses only values logged at decision time and closed bars; the 52 entries are the bot's own logged decisions (no survivorship of today's universe).
Q33: No cost claim; the sizing record adds requested / submitted / filled quantities so live cost can be measured.
Q34: No return claim; not applicable to this correctness diff.
Q35: HIGH_VOL never fired in 101 days; only one fallback day. No regime-performance claim is made.
Q36: Not applicable (no new trades sized differently); index reads are one request per cycle.
Q37: Not applicable to correctness; a 0.1R regime effect would need roughly 1,570 trades per arm (board LdP estimate, unverified derivation) vs 52 core entries now.
Q38: Not an edge feature; reversal criterion: any VIX rule consuming a non-VIX or stale value.
Q39: Size is unchanged and still comes from the Kelly path fed by Alpaca fills; inert fixed multipliers are removed.
Q40: Protects gate-input integrity. Machine check: no regime record with a VIX rule applied where vix_source is none or age > bound; every core entry has a sizing record whose final shares equal the submitted quantity.
Q41: Use the live index feed (done), add VIX3M term structure and rolling percentiles next, and attribute every entry to its binding limit.
Q42: Raw: the live CBOE index prints from Schwab with their trade times, Alpaca bars, and the logged decision stacks — not the composite regime label.
Q43: Point-in-time, provenance-tagged features; no silent substitution between different series; explicit precedence and invariants in sizing; replay against raw logs.
Q44: Independent: Codex and GAI reviews (3 rounds), the cold second review on the code diff, and the first-session replay comparing each logged VIX value to the Schwab/yfinance raw value.

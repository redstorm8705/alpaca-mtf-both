# REGIME-SIZE-1104 — the core-tier size multipliers change no share counts (signed: Claude, 2026-11-04)

Approval-ID: REGIME-SIZE-1104
Status: rev 3.7 DRAFT — owner direction 2026-11-05 (all VIX sources read and cross-checked; FMP client added to Covers). Approval voided by the Covers change; board + GAI + Codex review, then owner re-approval.
Covers: data/schwab_quotes.py, config.py, strategy/volatility_regime.py, strategy/run_cycle.py, strategy/regime_state.py, events/macro_risk_index.py, execution/entry_logic.py, execution/exit_logic.py, research/regime_empirical.py, CLAUDE.md, tests/test_core_sizing_multiplier.py (new), tests/test_vix_source.py (new), tests/test_schwab_quotes.py

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
Q13: Schwab missing/stale/killed → yfinance ^VIX; both missing/stale/implausible (non-finite, ≤0, >150) → VIX rules not applied, trading continues, logged; never a substitute series.
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
Q26: VIX: Schwab Market Data `$VIX` (owner's account) → T2 FMP `^VIX` → T4 yfinance `^VIX`, each with its own as-of time → unavailable. SPY rvol: T1 Alpaca bars, separate field.
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

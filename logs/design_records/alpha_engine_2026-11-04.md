# Alpha lifecycle engine: strategy registry, verifier, decay monitor — ALPHA-ENGINE-1104 (signed: Claude, 2026-11-04; rev 7) — DESIGN

Approval-ID: ALPHA-ENGINE-1104
Covers: data/fetcher.py, deploy/systemd unit files and scripts/install_*cron*.sh (MTF_RUNTIME only), execution/strategy_registry.py (new), research/verifier.py (new), run_decay_monitor.py (new), scripts/install_decay_monitor_cron.sh (new), execution/swing_breakout_manager.py, execution/swing_overnight.py, execution/day_trade_manager.py, tests/test_strategy_registry.py (new), tests/test_verifier.py (new), tests/test_decay_monitor.py (new)

Owner direction 2026-11-04: review an article on alpha lifecycles ("Discovery, Validation, Deployment, Decay,
Retirement"; a verifier with a sealed out-of-sample lockbox, a trial-count-adjusted bar and walk-forward windows; a
monitor that flags decay, regime divergence and cost drift and feeds research), identify the bot's gaps, then "Yes,
design it" for the recommended build: one strategy registry + a verifier in code + a nightly decay monitor + an action
rule. Extends LOSS-PM-1104 (its remedy registry becomes the verifier's trial log).

## 1. What exists today (verified at source)
- Trend night has a live edge check: `execution/swing_overnight.py:_edge_check` compares the live mean night return
  with the replay prior through a normal-mixture boundary (`mixture_halfwidth`), an anytime-valid sequential test, and
  pages the owner; turning it off stays manual.
- Swing breakout sizes from a replay prior (`research/swing_owner_prior_2026-10-25.json`) until 30 closed lots, then
  from its own fills (Kelly).
- Day-tier families have an admission file (`strategy/day_tier_family_admissions.json`, 4 families, status
  ADMITTED_PAPER, evidence record, independent audit).
- Nothing else checks a live strategy against what it was approved on; research replays keep no trial count and can
  reread the test years without limit (this session: 54 earnings variants and 27 Swing filters on one 2024-26 window).

## 2. The registry (`execution/strategy_registry.py`, state `data/state/strategy_registry.json`, atomic writes)
One record per strategy family: id, tier, stage (CANDIDATE → APPROVED → LIVE → FLAGGED → REDUCED → RETIRED), the
approval id, the **baseline** it was approved on (unit of return — R per trade, or return per night; mean, sd, n,
win rate, cost assumption, replay period, evidence file), the **regime at approval** (the distribution of SPY 20-day
realized volatility and VIX over the replay period, stored as quantiles), the sealed holdout date, and a **size
multiplier** (1.0 unless flagged). Initial entries are written from the existing evidence: swing_breakout_55d_v1,
trend_night, the 4 admitted Day families, QHM picks, and the legacy intraday bot. F6 (never sells) is registered with
no decay monitoring. Stage changes are appended to `logs/strategy_registry_events.jsonl` with the reason.

## 3. The verifier (`research/verifier.py`, used by every research replay)
- **Trial log:** every replay that evaluates a candidate rule calls `register_trial(family, hypothesis, params,
  windows)`; the log is append-only (`logs/research/trial_registry.jsonl`, LOSS-PM-1104's registry) and the count is
  cumulative per family, never per run.
- **Sealed holdout:** data on or after the family's seal date (2026-11-04 for everything that exists; each new family
  seals at its design date) can only be read through `verify_holdout(candidate_id)`, which records the read; a second
  read of the same holdout by any candidate of that family is refused unless a new seal date is set (new data).
- **Bar that rises with the search:** a candidate passes only when (a) its Deflated Sharpe Ratio (Bailey & López de
  Prado 2014), computed from the per-trade or per-period returns with the family's cumulative trial count, skewness
  and kurtosis, clears 0.95, and (b) it is positive in each of at least three non-overlapping walk-forward windows
  chosen before the run. A pass makes the candidate eligible for the normal board + GAI + Codex gate and owner
  approval — it never ships anything by itself.
- The verifier is a library plus a pre-commit check: a research result quoted in a design record must name its
  trial-log entry (`trial_id`), or the design-record check refuses it.

## 4. The decay monitor (`run_decay_monitor.py`, nightly after the close, separate process)
For each LIVE family, from the rawest data (Alpaca fills joined to each tier's ledger, per-lot R / per-night return):
1. **Edge decay:** the trend-night mixture boundary, generalized: live mean vs the registry baseline mean with the
   anytime-valid normal-mixture half-width (`mixture_halfwidth` moved to a shared module). Valid under continuous
   monitoring, so no "N bad windows in a row" rule; with few live trades the boundary is wide and does not fire.
2. **Regime divergence:** today's SPY 20-day realized volatility and VIX against the quantiles stored at approval;
   flagged when the current reading has been outside the approval range for longer than the family's own typical
   holding period (measured from its fills).
3. **Cost drift:** live slippage (fill vs decision price logged in the entry event) against the replay's cost
   assumption, with the same mixture boundary on the slippage series.
Output: a `decay_check` event per family, the registry stage update, and one Slack digest.

## 5. The action rule (the only trading change)
- **FLAGGED (edge decay crossed):** the family's size multiplier drops to its Kelly-consistent value: the fraction
  implied by re-estimating Kelly on the shrunk mean (live mean shrunk toward the baseline by their sample sizes),
  floored at one share per lot — trade count is unchanged; only size shrinks. A research task is written to the trial
  log's queue and the owner is paged with the evidence.
- **Regime divergence or cost drift alone:** flag and page only; no size change.
- **Recovery:** when the boundary is no longer crossed on a later evaluation, the multiplier returns to 1.0.
- **RETIRED:** only by owner approval (the monitor proposes it when the shrunk mean is below zero with the boundary
  crossed).
- The multiplier is applied in each tier's sizing as a final down-only factor (never above 1.0); it cannot raise size,
  frequency or exposure, so it is not a risk-path amplifier; it is still an owner-approved trading change.

## 6. Not in scope (phase 2, separate design)
Overlap check between strategies (factor loading of a new family vs live families) and automatic research hypotheses.

## 7. Static numbers (flagged)
DSR 0.95 and three walk-forward windows (verifier policy, owner-set), the mixture boundary's alpha (already used by
trend night), the holdout seal dates.

## 8. Rev 2 (2026-11-04) — changes from board seats Thorp and López de Prado, Codex round 1 (11 points) and GAI round 1. Where this section differs from sections 3-5, this section governs.

### 8a. Verifier
1. **One trial log** for the verifier and LOSS-PM-1104: `logs/research/trial_registry.jsonl` (LOSS-PM's
   `remedy_registry.jsonl` path is retired before it is created). Backfilled as counted trials: the 54 earnings-gap
   variants, the 27 Swing filter candidates and the LOSS-PM remedies tried on 2026-11-04 (scripts in
   `logs/research/gap_exit/` and `logs/research/swing_pm/`). Family names are fixed at registration; renaming a family
   carries its count.
2. **One trial per parameter vector** (a grid of k is k trials). Each trial stores its daily return series hash, Sharpe,
   T, skewness and kurtosis. SR0 is computed from the variance of Sharpe ratios across the family's trials (Bailey &
   López de Prado 2014); both the raw count and a clustered effective count (ONC) are reported; if clustering fails
   the raw count is used.
3. **Returns are daily family returns** (equity change of the family's lots per session), so overlapping lots are not
   counted as independent; periodicity is fixed per family.
4. **The seal is enforced at commit, not by convention:** research code loads bars only through
   `research/verifier.load_bars()`, which cuts the end date at the family's seal; a lint check (local hook and CI)
   refuses any file under `research/` or `logs/research/` that calls the bar fetchers or the Alpaca data endpoints
   directly. Holdout bars are fetched only by `verify_holdout()`, which writes a manifest (dates, hashes, timestamp)
   and the read to the trial log. Honest limit: this is enforced on committed code, not by operating-system access.
5. **Holdout release:** a family registers a frozen candidate set and its evaluation statistic (timestamp, data-cutoff
   hash) before one family-level release; the holdout is consumed once for that frozen set. A new seal covers only data
   after the last read and must be long enough, by a power calculation for the candidate's claimed effect, to detect it.
   A family whose holdout is consumed keeps researching on discovery data and confirms through live paired differences
   against the current rule.
6. **Walk-forward hygiene:** purged and embargoed windows (embargo = the family's maximum holding period), cut by equal
   trade count; the gate statistic is the DSR of the concatenated out-of-sample daily returns (pre-declared), not
   "positive in each window".

### 8b. Decay monitor
1. **The baseline is de-biased:** the deflated or holdout mean, not the selected replay mean; the registry records
   which. It is stored **per volatility bucket** (terciles of SPY 20-day realized volatility over the replay period),
   and the monitor compares live results with the baseline of the bucket each day was in.
2. **Two-sample test with the baseline's own uncertainty:** an anytime-valid confidence sequence on the difference
   between live and baseline daily family returns, including the baseline's sampling variance. Method: the betting /
   empirical-Bernstein confidence sequence (Waudby-Smith & Ramdas), which does not assume normal returns; trend night's
   normal-mixture boundary is kept only for trend night until migrated. Trend night's baseline is frozen at its
   registry entry instead of recomputed daily.
3. **Calibrated before it acts:** before the multiplier is enabled, the monitor is run on bootstrap resamples of each
   family's replay returns (no decay present) and on the same with an injected decay; the realized false-flag and
   detection rates are reported in this record. If the false-flag rate exceeds alpha, the method is not enabled.
4. **Family-wise alpha:** size-changing actions use alpha split across the live families; pages use 0.05.
5. **Separate flags:** `edge_flag`, `regime_flag`, `cost_flag` are stored separately with their own evidence; the
   stage is derived from them (REDUCED is removed: a flagged family with m < 1 is FLAGGED).
6. **Regime check from data:** a regime flag is raised when the family's baseline expectancy in the current volatility
   bucket is not positive (page only), not on a raw VIX band; VIX and realized volatility are treated as one dimension.
7. **Cost:** side-correct, quantity-weighted implementation shortfall (decision price to fill), plus the opportunity
   cost of missed and partial fills, against the replay's cost assumption.

### 8c. Action (the only trading change)
1. **Continuous multiplier, no on/off flag:** m = min(1, f_plausible / f_tier_now), computed nightly.
   - f_plausible is the Kelly fraction at the upper end of the live confidence sequence for the edge (the largest mean
     the evidence has not ruled out), with the larger of live and baseline variance.
   - f_tier_now is the fraction the tier actually uses that night.
   Because m follows the upper bound, it rises only as new live evidence accumulates; no flip back to 1.0 on a single
   night. A retirement proposal goes to the owner when the upper bound itself is below zero.
2. **No double count:** where a tier already sizes from its own live fills (Swing after 30 closed lots; trend night's
   pooled budget), m = 1 by construction. m bites only where a tier still sizes from a replay prior or a constant
   (Swing before 30 lots, the Day families, the legacy intraday bot, QHM). A unit test per tier.
3. **Order and trade count:** raw size → m → existing caps → integer rounding. A lot that sized to at least one share
   before m still gets at least one share after it, if the existing caps, the one-share risk cap, the stop and the kill
   switch allow it (the owner's data-collection policy: fewer shares, never fewer trades). Both the continuous and the
   effective m are logged on each entry. Tests include the $720-name Swing case.

### 8e. Rev 3 — Codex round 2 (7 of 11 closed; recovery, Kelly formula, seal scope, opportunity cost open). Governs where it differs.
1. **Change-point detection and recovery (replaces 8c item 1's monitoring basis).**
   - Detection: a sequential change detector (e-detector, Shin, Ramdas & Rinaldo 2023) on daily family returns against
     the de-biased baseline; it alarms when its e-value exceeds 1/α_k and also estimates where the change started.
   - Alpha accounting: the k-th alarm or recovery test of a family spends α_k = α·2^-k (α is the family's share of the
     family-wise alpha), so repeated restarts cannot inflate the error rate.
   - After an alarm, a fresh confidence sequence starts at the estimated change point; the multiplier is computed from
     post-change data only (formula in item 2). It can go down or up as post-change evidence arrives (the round-1 claim
     that it only rises was wrong and is withdrawn).
   - Recovery: the family returns to m = 1 when a second e-detector, run on post-change data, shows the post-change mean
     is at least the baseline's lower bound; this spends the next α_k. Until then m follows item 2.
2. **Executable Kelly (sizing families measured in R per lot; trend night is daily-return sized and keeps m = 1).**
   - Units: R = (exit − entry) / initial stop distance per lot, from Alpaca fills, net of costs; one observation per lot.
   - The tier's fraction (Swing's live formula, `swing_breakout_manager.kelly_risk`):
     f = KELLY_FRACTION × max(0, μ − se) / σ², the fraction of equity risked at the stop per lot; f_tier_now is the
     value the tier computed and logged for that entry (per lot; simultaneous lots each carry their own).
   - Plausible fraction: f_plausible = KELLY_FRACTION × max(0, μ_up) / max(σ²_live, σ²_base), with μ_up the upper bound
     of the post-change per-lot confidence sequence (width calibrated by block bootstrap over entry weeks, so
     overlapping lots are not treated as independent).
   - m = 1 if f_tier_now ≤ 0, else min(1, f_plausible / f_tier_now). Negative or zero plausible edge gives m = 0 in the
     continuous value; the one-share policy (8c item 3) then decides the effective size. Both values are logged.
   - Fixture (unit test): KELLY_FRACTION 0.25, μ_up 0.10 R, σ 1.5 R → f_plausible = 0.25 × 0.10 / 2.25 = 0.01111;
     f_tier_now 0.01484 → m = 0.749. Second fixture: μ_up −0.05 R → m = 0. Third: f_tier_now 0 → m = 1.
3. **Seal scope:** the lint runs repository-wide in CI and the local hook over every committed `.py`, `.ipynb` and
   `.sh`, refusing direct calls to Alpaca market-data clients, endpoints or the bar caches outside an allow-list of
   runtime modules (`data/fetcher.py`, `data/alpaca_data.py` and the live tier modules that already use them). Any
   research figure quoted in a design record must cite a trial-log entry whose holdout manifest (dates and hashes) shows
   no read past the seal, or the design-record check refuses it. Stated limit: analysis that is never committed is
   outside the guarantee.
4. **Opportunity cost of missed and partial fills:** for each entry order with unfilled quantity, cost per unfilled
   share = side × (price at the lot's planned horizon − decision price), horizon = the actual exit of the filled part,
   or, when nothing filled, the exit the replay rule would have taken; summed with the filled part's implementation
   shortfall and expressed in R and in dollars.
5. **Preconditions before the multiplier or the monitor's flags are enabled** (build deliverables, recorded here before
   enabling, not design-time inputs): the bootstrap calibration per family (false-flag rate under no decay, detection
   rate and delay under injected decay, family-wise), the read-only server run on current ledgers, and the full-path
   simulation. A family whose false-flag rate exceeds its alpha keeps m = 1 until re-estimated (GAI round 2).

### 8f. Rev 4 — Codex round 3 (recovery rule and seal boundary). Governs where it differs.
1. **No separate recovery rule.** The "recovery to the baseline's lower bound" test in 8e item 1 is withdrawn. After a
   downward alarm, m is always m = min(1, f_plausible / f_tier_now) on post-change data (8e item 2); full size returns
   only when the post-change upper bound itself supports the tier's full fraction (f_plausible ≥ f_tier_now). An upward
   change never restarts the confidence sequence; only a new downward alarm starts a new, later change point.
   Fixtures (unit tests):
   - f_tier_now 0.10, post-change f_plausible 0.04 → m 0.40, and it stays 0.40 while f_plausible stays 0.04.
   - A negative post-change mean (μ_up < 0) gives m = 0 (continuous) and never restores full size, whatever the
     baseline's interval.
   - Genuine recovery: as post-change evidence moves f_plausible 0.04 → 0.11, m moves 0.40 → 1.00 (capped), at the
     evaluation where f_plausible first reaches 0.10.
2. **Executable detector.** Statistic: x_t = the family's daily return minus its de-biased baseline daily mean, clipped
   to the family's replay-measured range [−c, c]. Detector: the e-detector maxing over start times of the betting
   product Π(1 − λ_s·x_s / c), with λ_s chosen by the predictable aGRAPA rule in [0, ½] (Waudby-Smith & Ramdas), testing
   the alternative "mean below the baseline". Alarm when it exceeds 1/α_k. The estimated change point is the start time
   that maximizes the product. After an alarm the detector restarts from the alarm date with α_{k+1} = α_k / 2.
3. **Seal at the shared data boundary, plus lint.** `data/fetcher.py`'s bar functions and the bar-cache readers gain a
   research mode: a process that imports `research/verifier.py` registers its family, and in that mode every
   historical bar request is cut at the family's seal (only `verify_holdout()` can lift it, once, with its manifest).
   Live processes never enter research mode. The CI lint (repository-wide, `.py`, `.ipynb`, `.sh`) refuses any
   committed file outside the runtime allow-list that imports `data.fetcher`, `data.alpaca_data` or an Alpaca data
   client, by name, alias, `importlib`/`__import__` string, subprocess call, or a read of the bar-cache paths, unless it
   imports `research/verifier.py` first. Negative fixtures in CI cover each of those. `data/fetcher.py` is added to
   Covers. Stated limit: uncommitted or deliberately obfuscated analysis is outside the guarantee.

### 8g. Rev 5 — Codex round 4 and GAI round 4. Governs where it differs.
1. **Valid detector (replaces the maximum in 8f item 2).** The detector is the Shiryaev-Roberts e-detector: the SUM over
   start times s ≤ t of the betting products Π_{u=s..t}(1 − λ_u·x_u/c), with the same clipped statistic and aGRAPA bets.
   Alarm when the sum reaches A. Guarantee (Shin, Ramdas & Rinaldo 2023): under no change, the average run length to a
   false alarm is at least A sessions. A is set per family from the owner's tolerated false-alarm rate: A = 5,040
   sessions (20 trading years) divided by the number of live families' share, i.e. family-wise about one false alarm
   per 20 years across all families. The estimated change point is the start time with the largest term of the sum.
   Null simulations over a 5-year monitoring horizon, per family, report the realized false-alarm rate before enabling
   (8e item 5).
2. **Post-change multiplier, fully specified.**
   - Lots whose entry date is on or after the estimated change date form the post-change sample; a lot spanning the
     change date counts in the pre-change sample.
   - μ_up is the upper end of a (1 − α_size) betting confidence sequence (Waudby-Smith & Ramdas, hedged capital) on the
     post-change per-lot R values, clipped to the family's replay range of R; α_size is the family's share of the
     size-action alpha (8b item 4).
   - Before any alarm, m = 1 (no post-change sample exists). After an alarm, m = min(1, f_plausible / f_tier_now) with
     f_plausible from 8e item 2 using μ_up; with no post-change lots yet, μ_up is the clipped range's upper end and m = 1
     until lots arrive. Baseline uncertainty enters only through the detector (de-biased baseline mean in x_t).
3. **Seal by capability, not by caller.** The bar functions in `data/fetcher.py` and the bar-cache readers check the
   process capability: live services and live cron jobs run with `MTF_RUNTIME=live` (set in the systemd units and in
   every crontab line the installers write); any process without it is in research mode and every historical request
   is cut at the global seal date, whatever module called the fetcher (transitive imports included). `verify_holdout()`
   lifts the cut once, with its manifest. A live job that somehow lacks the variable gets capped data, so each live
   entry point checks the capability at startup and pages if it is missing; a CI test checks every crontab and systemd
   line carries it. The repository-wide lint stays as a second layer and also refuses `sys.modules` injection of the
   data modules (GAI). Covers adds the systemd unit files and the cron installers.
4. **Registry locking (GAI):** `strategy_registry.json` writes use an fcntl lock plus temp-file and rename; readers take
   a shared lock and fall back to the last good copy if the file is unreadable.
5. **Calibration artifact (GAI):** the calibration results are committed as JSON under `logs/research/calibration/`
   before the monitor's flags or multiplier are enabled.

### 8h. Rev 6 — Codex round 5 and GAI round 5. Governs where it differs.
1. **The sizing confidence sequence starts at the alarm** (replaces 8g item 2's estimated-change-point sample): after
   an alarm, μ_up comes from a (1 − α_size) hedged-capital betting confidence sequence on per-lot R of lots entered on
   or after the alarm date only — no data used by the detector to choose a change point. Until the first post-alarm lot
   closes, f_plausible is computed from the pre-alarm per-lot sample's lower half: m = min(1, f(μ_lower,pre) / f_tier_now)
   [conservative interim]. The estimated change date is reported for diagnosis only.
2. **Threshold, algebraically:** for K live families sharing the family-wise budget equally, each family's
   Shiryaev-Roberts threshold is A_k = 5,040 × K sessions (5,040 = 20 trading years), so the family-wise average run
   length to a false alarm is at least 20 years. (Rev 5's "5,040 divided by" wording was wrong.)
3. **Capability checked at every call (GAI):** the `MTF_RUNTIME` capability is checked inside each bar and cache entry
   point on every call (a decorator), not only at process start.
4. **Calibration artifact contents (GAI):** the JSON reports, per family and family-wise, the realized false-alarm
   rate and average run length under no change over a 5-year simulated horizon, and the detection rate and delay under
   injected decays, using each family's own replay returns.
5. **Not adopted (GAI round 5 item 1):** skipping a lot when m × raw size is below one share. The owner's standing
   policy is fewer shares, never fewer trades; a one-share lot keeps its stop and remains inside the 7% kill switch.
   The continuous m and the effective m are both logged so the difference is visible.

### 8i. Rev 7 — Codex round 6 and GAI round 6. Governs where it differs.
1. **Sample by information set, not calendar date:** the sizing confidence sequence includes only lots ENTERED
   strictly after the alarm's emission timestamp (the alarm runs after the close, so normally from the next session),
   and excludes every lot or return component that entered the detector's computation. Fixture: a Day-tier lot entered
   and closed during session d, which feeds the alarm raised after session d's close, is excluded from the sizing
   sequence; a lot entered at session d+1's open is included.
2. **Interim size after an alarm is the floor, not pre-alarm statistics:** from the alarm until the first post-alarm
   lot closes, the continuous multiplier is m = 0, so every new lot of that family trades at the one-share minimum
   (8c item 3: fewer shares, never fewer trades). No pre-alarm statistic is used for post-alarm sizing (8h item 1's
   pre-alarm lower bound is withdrawn).
3. **Ordering:** the monitor writes the multiplier file (fcntl lock, temp file and rename) after the close; tiers read
   it at sizing time, so the first post-alarm session already trades at the floor. If the file is unreadable, a family
   with an active alarm in its last good copy stays at m = 0.

### 8j. Review status (2026-11-04)
Board seats Thorp and López de Prado: all requested changes folded (sections 8a-8c, 8e-8i). Reviewer #2 (Codex):
rounds 1-6 rejected on concrete inputs, each fixed; round 7 approve. GAI: round 7 approve. Not adopted with reasons
in 8d and 8h item 5.

### 8d. Not adopted, with reason
- GAI: a 5-day dry-run log before the tiers read the multiplier — that is a shadow period, which the owner's mandate
  prohibits before $25K; the bootstrap calibration (8b item 3) and the read-only server run replace it.
- GAI: "pause all" when more than 30% of capital is in flagged strategies — it cuts trade count on a fixed threshold;
  the 7% kill switch is the account-level brake; the digest reports the flagged share of capital instead.
- GAI: a fixed 5-day cooldown — replaced by the continuous, evidence-driven multiplier (8c item 1).

## Required questions (RQ-4)
Q1: Edge evaluation becomes automatic and continuous for every live strategy; data collection gains a trial log, so research results become trustworthy.
Q2: Trade count is unchanged: a lot that sized to one share or more keeps at least one share (8c item 3), tested on the $720-name Swing case.
Q3: Simpler: extend trend night's existing edge check to every family and add a trial log; that is exactly the core of this design.
Q4: LOSS-PM-1104 (remedy registry reused as the trial log), trend-night edge check, Swing replay prior, Day family admissions file, CO audit design (co_audit_and_anomaly_2026-10-09.md, build item 6 anomaly detection); no open PR on this.
Q5: Thresholds: DSR 0.95 and three windows are policy (flagged); the decay boundary is derived from each family's own variance and sample sizes; regime ranges come from the replay period.
Q6: Static assumption exposed: a strategy approved once is assumed to work forever; this builds the dynamic check.
Q7: Feeds each tier's sizing (down-only), the research queue, LOSS-PM proposals and Slack; reads fills and tier ledgers; fails safe (no reading → multiplier unchanged).
Q8: Section 1 facts are cited from code; the article's methods are labelled as its claims.
Q9: Replay the monitor over each family's live history to date and over the replay period split into pseudo-live windows; expect no flag on current data (few trades); reversal: a flag that fires on a family whose later results recover within its holding period.
Q10: No validated edge is held back; this measures edges.
Q11: Down-only multiplier: cannot increase size, frequency or exposure; the board risk seat checks that it cannot be raised above 1.0.
Q12: Safety envelope untouched; stops, kill switch, paper flag unchanged.
Q13: Missing fills, baseline or bars → no evaluation that night, multiplier unchanged, digest says why.
Q14: No shadow: the monitor acts (down-size) and the verifier gates research.
Q15: Each decay_check event logs inputs, boundary, decision and multiplier.
Q16: decay_check events and registry stage events; audited after the first full session.
Q17: Run read-only on the server against today's ledgers before enabling the multiplier.
Q18: Exists because a strategy's decay would otherwise be noticed only by the owner.
Q19: Yes (sizing multiplier); the owner approves this exact design.
Q20: Prompts neutral; the article's claims are not adopted without check.
Q21: Tests mock fills and broker; nothing places orders.
Q22: Read-only server run first.
Q23: Full-path simulation: registry build → nightly check → flag → multiplier read by each tier → recovery.
Q24: N/A — no existence claims made.
Q25: Volatility regimes measured from SIP bars and VIX history.
Q26: T1 Alpaca fills and bars; T4 VIX via the existing approved fallback; no new sources.
Q27: data/state/strategy_registry.json (atomic), logs/strategy_registry_events.jsonl, logs/research/trial_registry.jsonl, trade_events.jsonl.
Q28: Nightly cron after the close; tiers read the multiplier at sizing time (one file read, cached per cycle).
Q29: High: it makes every strategy self-correcting and makes research results credible.
Q30: The standard pieces the bot lacked: trial registry, deflated Sharpe, decay monitor, retirement rule.
Q31: The verifier is the answer to this question for all future work.
Q32: Holdout sealing prevents rereading; universe survivorship still applies to replays and is stated per family.
Q33: Cost drift is monitored; baselines carry their cost assumption.
Q34: Baselines and live returns are also reported net of SPY over the same holding periods.
Q35: Regime divergence is a monitored dimension.
Q36: Capacity not binding at $25K; ADV fraction logged for completeness.
Q37: The confidence sequence is sized by sample count and calibrated by bootstrap (8b item 3); Swing needs about 285 trades to separate its edge from zero at 2 standard errors.
Q38: This design is the decay-and-replacement definition: FLAGGED → down-size; RETIRED by owner.
Q39: m = min(1, Kelly at the upper live bound / the tier's current fraction), from Alpaca fills; m = 1 where the tier already sizes from its fills.
Q40: Protects against running a decayed edge at full size; check = replay of the monitor on history flags known decays.
Q41: Phase 2: overlap/orthogonality check and automatic hypothesis generation.
Q42: Alpaca fills joined to tier ledgers, SIP bars.
Q43: Lifecycle management with a deflated-Sharpe verifier and sequential decay tests; the design uses both.
Q44: Board, GAI, Codex; the replay of the monitor on history; the post-live audit.

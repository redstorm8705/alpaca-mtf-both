# F6 crash-only trigger restoration — 2026-10-28

**Author:** OpenAI Codex
**Status:** revised exact diff owner-approved 2026-11-04; F6 remains disabled
Approval-ID: F6-CRASH-1028
Owner numbers: execution/f6_empirical_trigger.py F6_HISTORY_FETCH_DAYS=90; execution/f6_empirical_trigger.py F6_HISTORY_SESSION_COUNT=21; execution/f6_empirical_trigger.py F6_PRIOR_CLOSE_OFFSET=2; execution/f6_empirical_trigger.py F6_RETURN_PERCENT_SCALE=100.0; execution/f6_empirical_trigger.py F6_THREE_SESSION_OFFSET=4; execution/f6_empirical_trigger.py F6_CLOSE_FLOOR_PCT=2.0; execution/f6_empirical_trigger.py F6_CLOSE_VIX_SLOPE=0.15; execution/f6_empirical_trigger.py F6_INTRADAY_LOW_PCT=-3.5; execution/f6_empirical_trigger.py F6_THREE_SESSION_PCT=-10.0; execution/f6_empirical_trigger.py F6_VIX_RELATIVE_CONFIRM=1.3; execution/f6_empirical_trigger.py F6_DEEP_CRASH_PCT=-7.0; execution/f6_empirical_trigger.py F6_DEEP_VIX_LEVEL=35.0; execution/f6_empirical_trigger.py F6_TIER_C_CASH_FRACTION=0.30; execution/f6_empirical_trigger.py F6_TIER_B_CASH_FRACTION=0.20; execution/f6_empirical_trigger.py F6_TIER_B_MAX_NAMES=3; execution/f6_empirical_trigger.py F6_TIER_B_DEPTH_PCT=-4.0; execution/f6_empirical_trigger.py F6_TIER_A_CASH_FRACTION=0.10; execution/f6_empirical_trigger.py F6_TIER_A_MAX_NAMES=2; execution/f6_empirical_trigger.py F6_TIER_A_DEPTH_PCT=-2.0; execution/f6_empirical_trigger.py F6_RECENT_PRIOR_SESSION_COUNT=5; execution/f6_empirical_trigger.py F6_EVIDENCE_FLOAT_TOLERANCE=0.000000001
Covers: execution/f6_empirical_trigger.py, execution/forever_hold_manager.py,
  execution/tier_capital_allocator.py, research/f6_empirical_starter_replay.py,
  research/f6_full_path_simulation.py, tests/test_f6_empirical_trigger.py,
  tests/test_f6_bounded_entry.py, tests/test_f6_pending_buy_reservation.py,
  tests/test_f6_instrument_policy.py,
  tests/tests_tier_capital_allocator.py, tests/test_f6_full_path_simulation.py,
  logs/research/f6_empirical_starter_replay.json,
  logs/f6_full_path_simulation_latest.json,
  logs/post_live/expect/f6-empirical-starter.json,
  logs/design_records/f6_crash_trigger_restoration_2026-10-28.md,
  handoff.md, logs/tb_audit_log.md

## Owner direction and measured defect

Rafael clarified on 2026-10-28 that F6 may trigger only during a broad-market crash confirmed by
heightened VIX. The current paired-return/volume rule does not implement that definition. Its replay
armed on 60 of 783 sessions (7.7%); the mildest armed SPY close was -0.53% and the median was -1.40%.
The cause is measured in `execution/f6_empirical_trigger.py:106-115`: the four-event monthly allowance
is divided by median monthly sessions, producing roughly a 19% lower-return tail and a mirrored volume
tail. Volume is not VIX confirmation.

## Prior work

- `logs/forever6_scenario_board_2026-07-09.md:33-45,80-86` locks broad-price depth as the trigger,
  rejects an orderly -5% SPY day below the deep ladder, and says VIX >40 alone is context only.
- `logs/f6_starter_bgg_2026-07-13.md` adopts a cash-only, close-confirmed, market-wide starter with a
  segregated budget; the later replay below supersedes its VIX-multiplied cutoff because that cutoff
  rose during crashes and missed March 2020.
- `logs/design_records/exit_and_reports_audit_2026-09-26.md:145-185` records the BGGN crash trigger
  and 2020-2025 replay: SPY price damage plus VIX confirmation originally produced 19 candidates, zero in calm years,
  and intentionally missed the grinding 2022 selloff because Rafael wanted crash-like conditions only.
- `logs/design_records/f6_instrument_and_kill_policy_2026-10-15.md:114-150` later replaced that rule
  with the currently deployed return/volume quantiles; its own replay explicitly says it was a
  frequency/plumbing test, not a return backtest.
- `logs/design_records/f6_scoped_allocator_2026-10-18.md:34-56` gives F6 a 10% non-crash and 30%
  verified-crash envelope. This correction removes the non-crash entry possibility at the allocator
  boundary rather than trusting the planner alone.
- `logs/design_records/f6_full_path_simulation_2026-10-23.md` is the launch-path proof and must be
  regenerated on the final trigger bytes before F6 can be reconsidered for enablement.
- `logs/design_records/f6_trim_resync_2026-10-17.md` owns F6 reduction reconciliation and is
  unchanged by this entry-only classifier correction.
- `logs/design_records/f6_etf_winddown_2026-10-21.md` owns the common-stock-only universe and ETF
  wind-down boundary that remains binding after a crash arms.
- `logs/design_records/ownership_ledger_v2_2026-10-09.md` and
  `logs/design_records/qhm_manual_add_adoption_2026-10-23.md` own cross-tier attribution and manual
  QHM adoption; neither ownership path is changed here.
- `handoff.md` records the shipped F6 prerequisites and keeps enablement as a separate owner decision.
- The latest 2026-11-04 refresh found open PRs #834 (Swing-capacity research), #804 (research/docs,
  overlapping only `handoff.md`) and #727 (Slack docs). None changes F6 execution, allocator,
  trigger, simulation or test files; the handoff overlap will be reconciled before push if #804 lands.

## Exact design

1. Restore the historically replayed two-part crash gate using completed daily observations only.
   A starter requires both (a) a negative SPY close with qualifying broad-market price damage and
   (b) heightened VIX confirmed by VIX at least 1.3 times its strictly-prior 20-session average or
   VIX/VIX3M term inversion above 1.0. A VIX spike without the SPY selloff sends no order.
2. Classify depth from the worse of SPY close-to-close return and the completed session low versus
   prior close, while requiring the close itself to be negative. Restore the replayed ladder:
   A is -2% through -4%, B is -4% through -7% with term inversion, and C is at least -7% in one
   session or -10% over three completed sessions with VIX above 35. These are existing replayed
   thresholds, not newly selected values.
3. Tier A may deploy 10% of settled cash to at most two names; B may deploy 20% to at most three;
   C may deploy 30% subject to affordability and the reviewed common-stock universe. Existing cash
   floor, cash-only, allocator, reservation, ownership, quote-liveness, catalyst and product-policy
   checks remain binding.
4. Keep the existing four-event monthly cap for A/B and the replayed five-session A cooldown. Tier C
   bypasses the event-count cap so the deepest day of a cascading crash cannot be suppressed, but it
   remains bounded by settled cash, the cash floor and the F6 capital envelope.
5. Require current-session, completed, date-aligned SPY/VIX/VIX3M evidence. SPY comes from T1 Alpaca;
   VIX and VIX3M use the already-permitted T4 yfinance source because those indices are unavailable
   from Alpaca. Because Cboe calculates spot VIX through 4:15 PM ET, starter evaluation begins at
   4:16 PM ET; trims retain their existing schedule. Missing, stale, malformed, partially aligned or
   forming data fails closed.
6. Replace volume-tail evidence fields with explicit price/VIX evidence: close return, low return,
   three-session return, VIX, prior VIX average, term ratio, classified tier, reason, source dates and
   a SHA256 of the exact completed current and prior inputs. The order boundary revalidates the exact
   durable row, independently recomputes both gates and both VIX ratios, and rejects inconsistent flags.
   The manager itself refuses evaluation before 4:16 PM ET and records both the evaluation and durable
   append time; the order boundary rejects evidence evaluated or recorded before final VIX.
7. At the allocator boundary, an F6 entry is refused unless its current trigger evidence is armed,
   classified A/B/C and cryptographically bound to the exact latest fsynced trigger-evidence row. A
   caller-supplied digest that does not match that durable row is rejected. The generic normal/stressed
   10% envelope cannot independently admit an F6 buy.
   The classified crash tier selects the already-bounded event budget; Day, Swing and QHM behavior is
   unchanged.
   The money-moving boundary also recomputes the event's maximum spend from the exact A/B/C cash
   fraction and fresh settled cash, so a caller cannot enlarge a 10% Tier-A plan into a 30% order.
8. Rebuild the historical replay over 2020 through current data, explicitly report every fire and
   verify the documented calm-year zero-fire behavior, March 2020, August 2024 and April 2025. The
   corrected replay found 25 eligible crash observations and 20 affordable reference plans across
   1,715 sessions, with zero fires in 2021, 2023 and 2026. The increase from the original 19 is
   explained by the recorded post-replay correction to classify from the worse completed close/low
   and let Tier C bypass the A/B monthly cap; every added date is disclosed in the artifact.
9. Regenerate the source-bound full-path simulation on the final bytes. F6 remains disabled throughout;
   `FOREVER6_ENABLED=False`, `paper=True`, and no test may reach Alpaca.

## Independent board finding and correction

The first independent board pass rejected the draft for two concrete reasons: it could
read a still-forming VIX row between 4:05 and 4:15 PM ET, and the money boundary trusted stored gate
booleans without recomputing them. The exact design now starts starter evaluation at 4:16 PM ET and
recomputes price damage, VIX confirmation, VIX/prior-average and VIX/VIX3M from durable numeric
evidence. Forged-row and 4:05-versus-4:16 regressions cover both findings.

The masked-loss seat then rejected two deeper caller-bypass paths: a direct manager caller could
evaluate before 4:16 and execute the cached row later, and a caller could pass a budget larger than
the classified tier fraction. The manager now refuses the early call before loading volatility,
persists and validates post-4:16 evaluation/append timestamps, and recomputes the tier fraction from
fresh settled cash at execution. Dedicated regressions show a 4:05 call performs no volatility read,
an early timestamp cannot execute later, and a $300 caller budget cannot make a Tier-A event spend
more than $100 from $1,000 settled cash.

The final execution-board pass found a cross-cycle liveness defect: the first cycle durably recorded
its evaluation time and seeded quote freshness, while the second cycle generated a later evaluation
time that no longer exactly matched the deduplicated durable row. The evidence writer now reuses the
durable evaluation timestamp only when every non-time market/classification field is identical. The
full-path simulation advances the injected clock by five minutes between the quote-seeding and order
cycles, so this production-shaped sequence must reach a bounded submission rather than silently
blocking every F6 order.

The first exact-diff BGGN pass then rejected the classifier because the Tier-C three-session
alternative was nested behind a one-session price gate. Three roughly -3.45% completed SPY sessions
could exceed the reviewed -10% cumulative crash definition while each individual day stayed just
above the intraday-low gate. The classifier and money-boundary recomputation now treat the completed
three-session return as an explicit price-damage path. A regression isolates that shape, and the
2020-current replay was regenerated; its disclosed counts remain 25 eligible events and 20 affordable
reference plans.

The fresh post-approval masked-loss pass found that the five-session Tier-A cooldown existed only
in planning, so a direct execution caller could bypass it, and that the Saturday test harness mixed
Friday trigger evidence with Saturday event state. The durable evidence now carries the exact five
prior completed SPY session dates. Execution validates those dates, checks the cooldown before any
order work, repeats it inside the symbol lock, and repeats it again under the atomic event-state lock.
All starter event dates and cap checks now use the manager's injected clock; the full-path proof
asserts that the durable event date and timestamp equal the trigger session. The new direct-caller
regression proves a prior-session Tier-A event produces zero submission at both the execution and
atomic-reservation boundaries.

The next adversarial pass found two more malformed-input paths. First, a data provider could omit a
completed SPY row and thereby omit a prior Tier-A event date from the exact five-session list. The
cooldown now also treats any counted Tier-A event inside the evidenced calendar interval as active,
so an omitted row fails closed. Second, an impossible OHLC bar could report a close above its high
while contributing a deep low to Tier C. The classifier now requires every open and close to lie
inside its low/high range before it computes price damage. Regressions cover both counterexamples.

The post-rebase masked-loss pass found that allocator admission still accepted a caller-supplied
digest without proving that it named the latest durable trigger row. It also found a time-of-check /
time-of-use window in which another process could append a newer unarmed row after allocator
validation but before broker submission. The allocator now requires the exact canonical digest of
the latest current-session row and validates its evaluation/record timestamps. A shared re-entrant,
cross-process evidence lock serializes trigger appends, allocator proof and the F6 broker submission
boundary. The production call-graph regression proves the manager is the only F6 allocator caller
and that it holds that lock across both admission and submission. Forged, stale and superseded rows
all fail closed.

The exact-diff preship review then isolated the ladder boundary at exactly -7.0%: when VIX was not
above the Tier-C requirement, the strict Tier-B lower comparison left the event unclassified. Tier B
now includes exactly -7.0% unless the higher-priority Tier-C price-and-VIX condition is satisfied.
The order-boundary recomputation mirrors that priority, and a dedicated exact-boundary regression
prevents the gap from returning.

The renewed masked-loss pass executed that same decimal boundary with VIX above 35 and exposed
binary floating-point drift (`93 / 100` computed as -6.999999999999995%). Classifier and execution
validator now use the existing evidence tolerance symmetrically at every ladder boundary. Separate
regressions prove exactly -7.0% maps to B below the deep-VIX requirement and takes C priority above
it.

The post-rebase exact-diff review found that the non-hex source-digest regression omitted the valid
durable-row digest and therefore failed at an earlier guard. The fixture now supplies a valid durable
digest while corrupting only the source digest, so the intended hexadecimal check is isolated.

## Plain-English example

If SPY closes down 0.7% while VIX rises, F6 does nothing. If SPY closes down 4.8%,
its completed low is down 5.4%, and VIX term structure is inverted, the event is B: F6 may consider a
cash-only 20% event budget, then every existing admission control still applies. NVDA's price is not
read as a purchase candidate until that market-wide crash gate has armed.

## Reversal criterion

Do not ship if the replay fires in the documented calm years, misses the known qualifying crash dates
because of data alignment, admits any price-only or VIX-only event, uses a forming bar, permits an F6
allocator lease without armed evidence, changes another tier, or allows any network access in tests.
After launch, any F6 order whose durable evidence does not show both qualifying SPY damage and VIX
confirmation is a P0 reversal and disables F6.

## Required questions

Q1. Yes — it prevents F6 cash from being spent on ordinary declines while preserving the historically replayed crash accumulation path toward $25K (`exit_and_reports_audit_2026-09-26.md:162-185`).
Q2. It reduces the erroneous 60/783 potential triggers to 25 completed crash observations (20 affordable reference plans) across 1,715 sessions; the original 19 became 25 only after applying the documented worse-of-close/low and Tier-C corrections.
Q3. Yes — restore the already-tested price-plus-VIX gate and enforce it twice; a new model is less direct and would duplicate prior work.
Q4. The six prior records above own the scenario map, BGGN decision, replay, allocator and launch proof; open PR #727 does not overlap this scope (`gh pr list`, 2026-10-29).
Q5. No new number is introduced: the design restores the previously replayed ladder and uses the dynamic VIX/20-session ratio plus term structure (`exit_and_reports_audit_2026-09-26.md:148-185`).
Q6. It exposes the static assumption that an event-frequency cap can define a crash; the forward pass is empirical re-estimation of VIX/price joint tails without weakening the locked structural crash ladder.
Q7. It connects trigger, durable evidence, event sizing, allocator admission and the full-path simulator; each connection refuses unknown inputs and does not modify other tiers.
Q8. Yes — the superseded artifact measured 60/783, -0.53% mildest and -1.40% median; the rebuilt artifact now measures 25/1,715 eligible crash observations and 20 affordable reference plans.
Q9. Replay covers 2020-current, including March 2020, 2022, August 2024 and April 2025; any price-only/VIX-only fire or calm-year fire disproves the result.
Q10. No — the existing trigger has no validated crash-only edge and F6 is disabled, so correcting the definition is a launch prerequisite rather than calibration delay.
Q11. Yes — it changes F6 entry frequency and event budget, so it is risk-path and requires the board including the masked-loss seat plus Groq and GAI.
Q12. It preserves paper mode, cash-only admission, the typed 7% exception, every ownership guard and `FOREVER6_ENABLED=False`; execution independently caps spend to the evidence tier's fraction of fresh settled cash.
Q13. Missing/stale/misaligned SPY, VIX or VIX3M evidence, any pre-4:16 evaluation or durable append, or internally inconsistent one-day/three-session evidence produces zero allocator lease/order.
Q14. No shadow path is added; after replay and approval the corrected gate replaces the wrong gate, while F6 enablement remains a separate decision.
Q15. Yes — the append-only evidence row records all price, volatility, tier and provenance fields needed to reconstruct the decision.
Q16. Runtime proof will be the F6 trigger evidence row plus the route tag and post-live expectation; repeated identical polls reuse that exact row, and any order without matching armed evidence fails the audit.
Q17. The exact source-bound full-path simulation runs under OCI Python 3.10, and the disabled production process receives a read-only evaluation before enablement.
Q18. Yes — the current 7.7% trigger frequency is treated as a design failure, despite the current implementation behaving as written.
Q19. Yes — this changes F6 entry/sizing/routing; no production commit occurs until Rafael approves `F6-CRASH-1028` after the final design/replay package.
Q20. Review prompts will ask for false positives, missed crashes, look-ahead, misalignment, cap bypass and cross-tier leakage without prescribing an approval verdict.
Q21. Yes — all fetchers and broker boundaries are injected/mocked, credentials are removed, and tests assert zero network/order access.
Q22. Before enablement, the final classifier will run read-only on OCI against completed live inputs and must produce no order or state mutation.
Q23. The source-bound F6 full-path simulation must pass after this change; the earlier artifact is invalidated by changed trigger bytes.
Q24. N/A — no claim is made about a stock, ETF or venue being absent; the existing common-stock product policy remains unchanged.
Q25. Yes — all SPY/VIX behavior and fire counts come from the raw 2020-current replay, not memory.
Q26. SPY is T1 Alpaca SIP daily; VIX/VIX3M are permitted T4 yfinance daily indices; any unavailable or stale source fails closed with no fallback guess.
Q27. Evidence appends to the existing JSONL with flush/fsync discipline; replay/full-path artifacts are atomically replaced JSON files.
Q28. `execution/f6_cycle.py` runs trims first, refuses starter evaluation before 4:16 PM ET, then fetches completed SPY data; the manager classifies before planning and allocator/order validation follows.
Q29. This is P0 for F6 launch because the existing gate can classify an ordinary -0.53% day as eligible; no F6 enablement package is valid until it is corrected.
Q30. A professional design requires joint market-price/volatility stress, completed-data alignment, layered admission, event-ladder state and crash-period replay; the current volume proxy underweights false-positive deployment risk.
Q31. The trigger is a structural crash classifier, not a Sharpe-optimized strategy; the original rule had one declared variant and the rebuilt replay will disclose every tested variant, with no in-sample retuning.
Q32. The replay uses only completed bars, strictly prior VIX averages and a fixed present-day F6 universe solely for trigger timing; no per-stock return claim is made, so universe survivorship cannot create a trigger fire.
Q33. Trigger classification makes no fill-return claim; the full path retains bounded extended-hours limits, quote freshness and reservations, while historical reference closes are labelled non-executable.
Q34. F6 intentionally buys broad crash beta; the design makes no alpha-separation claim and logs the market trigger explicitly.
Q35. It is designed for high-VIX broad selloffs; calm-year fire counts, worst subsequent drawdown and event clustering are reported in the replay before approval.
Q36. At $25K the common-stock universe is liquid and whole-share affordability improves; existing quote, spread/product and cash limits remain the capacity controls.
Q37. N/A — this change restores a crash classifier and does not claim a per-trade expectancy estimate; 25 eligible observations and 20 reference plans bound how strongly performance can be inferred.
Q38. Any order without both market-price and VIX confirmation, or material divergence from the documented crash replay, is the owner-visible non-working condition that disables F6 pending replacement.
Q39. Event size follows the pre-existing crash ladder and is recomputed from fresh settled cash at the money boundary, not win-rate Kelly; no caller budget can exceed the A/B/C fraction.
Q40. It owns “F6 buys only in a defined broad-market/VIX crash”; machine checks require both evidence families and reject allocator/order paths without them.
Q41. The best expert would preserve the locked structural breaker levels, test joint tails out of sample, report clustering and drawdown path, and enforce the classifier again at the money boundary; this design does all four.
Q42. It uses raw completed SPY/VIX/VIX3M daily bars, durable trigger evidence, allocator decisions and broker-order transcripts rather than summaries.
Q43. The professional method is a predeclared multi-factor stress classifier with strictly prior calibration, walk-forward replay, fail-closed data joins and independent execution bounds; this design follows that method.
Q44. Independent board/masked-loss review, Groq/GAI exact-diff counter-review, GitHub CI, OCI Python 3.10 replay and the source-bound full-path simulation provide evidence beyond the author's assessment; 226 focused tests, four subtests and the advancing-clock hermetic simulation pass on the revised bytes.

Signed: **OpenAI Codex**

## Final validation after owner approval

- Rafael approved the earlier `F6-CRASH-1028` revision on 2026-10-31; later exact-diff reviews found
  the durable-row and decimal-boundary defects documented above. Rafael renewed approval for the revised
  exact design on 2026-11-04 by naming `F6-CRASH-1028`. This authorizes shipping the prerequisite only;
  it does not authorize enabling F6 or placing an F6 order.
- The revised diff is rebased onto `origin/main` `8fcd50f3`. The earlier Saturday validation exposed a test-harness clock mismatch: production correctly requires a completed weekday market session while the simulator used the wall-clock Saturday. The simulator now selects the latest completed weekday and supplies that same clock to the allocator's local datetime import. Production clock logic is unchanged.
- Revised focused gate: 226 tests plus four subtests passed after the durable-row binding, cross-process locking and tolerance-safe -7.0% ladder-boundary corrections; py_compile, Ruff E/W/F/B and mypy `--warn-unreachable` also pass.
- Repository-wide suite after the `8fcd50f3` rebase: 3,235 passed, 1 skipped and 234 subtests passed. The sole failure is the unrelated Day-promotion fixture `test_qualifying_long_is_marked_pending`, already reproduced on unmodified main.
- Final hermetic full-path proof: all 20 scenarios passed with an advancing five-minute clock between quote cycles; cycle one placed zero and cycle two reached a bounded broker submission.
- The refreshed 2020-current replay still measures 1,715 completed sessions, 25 eligible crash observations, 20 affordable reference plans, A/B/C counts of 7/12/6, and zero fires in 2021, 2023 and 2026.
- Independent execution board final review: PASS on the exact durable-row lock and tolerance-safe Tier-B/Tier-C boundary after the `d30b4802` rebase.
- Masked-loss review rejected a caller-fabricated allocator digest that could obtain the crash envelope without matching the durable trigger row. The revised allocator compares a canonical SHA256 of the exact latest fsynced row, its tier and its source-input digest before issuing a lease; the fresh final review passes the lock, stale-row and exact-decimal boundary cases.
- GitHub CI correctly rejected inline owner-approved crash constants under the no-static-threshold gate. The revised code centralizes the unchanged historical crash map in one module, lists every deliberate owner number above, and makes the manager import the same constants for independent recomputation. This is a behavior-preserving compliance refactor, but the record changed and therefore requires renewed exact approval.
- Earlier external review rejected the blocked three-session Tier-C path and then approved that correction. On the final current-main bytes, Google AI Studio approved every remaining file; Rafael explicitly waived Groq for the quota-blocked remainder after Groq approved the classifier and exhausted its daily allowance on the larger manager diff. The evidence counter-review documents and refutes every earlier chunk-context objection.
- Pycompile, Ruff E/W/F/B and mypy `--warn-unreachable`: PASS on every touched Python file.

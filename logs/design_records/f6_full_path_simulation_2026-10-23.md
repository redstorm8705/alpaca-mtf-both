# F6 full-path pre-market simulation — 2026-10-23 (revision 23)

Approval-ID: F6-SIM-1023

Covers: research/f6_full_path_simulation.py,
tests/test_f6_full_path_simulation.py,
execution/f6_cycle.py,
strategy/run_cycle.py,
tests/test_f6_cycle.py,
tests/test_run_cycle_kill_switch_exits.py,
logs/design_records/f6_full_path_simulation_2026-10-23.md,
logs/f6_full_path_simulation_latest.json,
logs/post_live/expect/f6-sim-1023.json,
handoff.md,
logs/tb_audit_log.md

Signed: OpenAI Codex

## Decision

The first implementation failed the cold-second and adversarial reviews because it called the F6 manager
directly and replaced ownership, allocator and order-admission behavior with canned successes. That could
produce a green artifact while the production router or durable journals were broken. Revision 3 rejects
that proof and adds one production orchestration seam, `execution/f6_cycle.py`, used by both
`strategy/run_cycle.py` and the simulator. The helper preserves the current live order of operations:
when F6 is enabled after the close, trims run first under every kill cause; starter evaluation runs only in
the F6 entry session; control faults block starters; a typed genuine-loss trip may still admit a starter.
No threshold, size, product rule or enablement flag changes.

Revision 17 adds the repository-required post-live contract. The shared helper emits the unique
`change=f6-sim-1023` tag only when the production F6 after-close route actually runs. Because F6
remains disabled in this shipment, the expected rate is zero until a separate owner-approved activation;
after activation, the tag must appear on each after-close invocation. Absence after activation or any
invocation while F6 is disabled disproves the wiring claim.

The extraction also moves the F6-specific typed-kill resolution now at `strategy/run_cycle.py:263-279` into
a shared function. Its inputs are F6 enabled state, the current session's typed kill verdict and the risk
manager; its output is the starter-block decision plus a machine-readable reason. Production and the simulator
call the same function. The connected kill case does not inject a trusted verdict: it drives the real
`RiskManager.check_kill_switch()` from raw fake Day fills, position marks, ownership and start equity, then
feeds that exact -7% genuine-trading-loss verdict into the shared resolver. Unit and structural tests cover
the other resolver branches: no kill, control fault, unreadable verdict, F6 disabled and a verdict exception.
This extraction does not change the existing decision table or P&L calculation.

The simulator runs that shared router and the production F6 manager against raw offline boundaries. It uses
the real `TierCapitalAllocator`, real broker admission functions, real pending-buy/reducing-order journals,
real ownership writes, and a real `run_ledger_sync.sync_once` replay fed by fake Alpaca fills, orders and
positions. Only raw broker/network and market-data fetches are replaced. It proves startup reconciliation,
empirical trigger planning, cross-cycle quote liveness, cash and allocator admission, durable order
reservation, filled-order ownership replay, restart reconciliation, trim reservation and filled-trim
ownership reduction. It injects an ETF, cash-floor breach, startup-not-ready state, a control-fault route,
a genuine-loss route, unresolved-order restart, broker rejection/timeout, shared-stop collision, and deliberate
network and filesystem escape attempts. Each unsafe path must fail closed with zero unauthorized orders.

Production startup remains once per process in `main.py`; the after-close helper never calls startup
reconciliation. The simulator separately calls the same production `ForeverHoldManager.reconcile_on_startup()`
before its first router tick, then carries the resulting module readiness state through multiple cycle calls.
Those calls cover accepted, partially filled, filled, rejected and transport-ambiguous order states while the
same temporary durable journals remain mounted. A manager restart reuses those journals to prove duplicate
suppression and recovery. Fill quantity, timestamp, execution price, client order ID and broker order ID must
all reconcile before ownership changes.

The simulator also runs a shared-symbol collision: QHM owns two NVDA shares protected by a live QHM stop and
F6 owns four NVDA shares. An F6 trim receives Alpaca's held-for-orders rejection/403 on its retry path. The
path must never cancel or resize QHM's stop, never sell more than F6 owns, and must keep the trim reserved.

The harness sets `MTF_TEST_MODE=1`, removes broker credentials for its scope, patches
`execution.broker._get_trading_client` to a transport-free fake, blocks socket creation and SDK client
construction, and restores the environment and every touched process global in `finally`, including
`_STARTUP_READY`, `_F6_QUOTE_OBSERVATIONS`, allocator singletons and all path constants. A regression seeds
sentinel globals, forces an exception, and requires exact restoration. Another regression deliberately tries
to open a network connection and requires a recorded refusal.

The harness runs in an isolated child process from a complete temporary checkout. After network and filesystem
guards are installed, that child alone temporarily overrides `FOREVER6_ENABLED=True` so the real starter
boundary can execute; tracked `config.py` remains false and the child must prove the imported flag is restored
before exit. A filesystem guard refuses subprocess creation plus built-in, `io`, `os` and `pathlib.Path` write, create, rename, replace
and unlink operations outside the temporary state directory. Regressions attempt those escape forms directly.
Every production state path used by the connected manager is redirected into that directory. Revision 23 also activates a scoped Python audit hook for process and native-loader events, blocks `subprocess.Popen`, proves that a child-process `touch` attempt cannot bypass the in-process filesystem guards, and makes the committed-artifact verifier require the complete exact refusal map so a false or missing subprocess result cannot pass.

The simulation hashes every production Python file under the repository root, `data`, `events`, `execution`,
`reporting` and `strategy`, plus the runner and its three enforcement tests, before execution, rehashes afterward,
and fails on a change. This deliberately broad binding prevents a late or transitive import from escaping the
evidence manifest. It writes a source-bound JSON result containing an informational `source_commit`, both hash
sets, scenario verdicts, order transcript and final ownership. The JSON is written to a temp file, fsynced and
atomically replaced. File hashes are authoritative;
the commit field is not used as a self-referential gate. A `--check` mode reads the committed artifact with
`git show HEAD:logs/f6_full_path_simulation_latest.json` before any regeneration and validates it against
current source hashes and the required scenario schema. A launch
result is PASS only when every scenario passes, all entry and trim orders flow through production broker
admission, every terminal fill flows through canonical replay, the asserted leases and journals reach their expected state, the run
begins and ends with tracked `FOREVER6_ENABLED=False`, and hermeticity/state restoration are proven. The
isolated child may temporarily override the imported value only after all guards are active; the script never
changes the production flag or a live state file. F6 remains off; a passing artifact supports a separate one-page
owner decision.

The shared orchestration extraction touches production strategy routing even though its intended behavior is
unchanged. Therefore no production edit or commit may occur until Rafael approves this exact revision by
naming `F6-SIM-1023`. The eventual `FOREVER6_ENABLED=True` change remains a separate risk-path decision with
its own approval ID.

## Cold-review corrections

The 2026-10-26 cold review rejected revision 2 for three false-pass routes. Revision 3 makes the connected
starter and trim lifecycle call `run_f6_after_close` itself, patches `Path.open/mkdir/unlink/replace` in addition
to built-in/`io`/`os` writers, and replaces the hand-curated source list with a repository-wide production Python
manifest. The escape regression and source-manifest regression fail if any of those repairs is removed.

The follow-up cold review rejected revision 3 because `--check` compared the committed artifact with working-tree
source bytes. A dirty checkout could restore stale bytes and falsely validate evidence for a different HEAD.
Revision 4 enumerates and hashes the bound Python blobs directly from `HEAD` with Git, requires the working-tree
bytes to match those HEAD hashes, and verifies a canonical source-tree digest stored in the artifact. Regressions
prove that stale artifact bytes plus a restored dirty worktree are rejected and that exact clean HEAD bytes pass.

The next cold pass found that revision 4 checked HEAD and the working tree but had not actually read the Git index.
Revision 5 separately hashes each bound index blob with `git show :path`, requires index = HEAD = working bytes,
and adds the exact staged-B/working-A/HEAD-A false-pass regression. `--check` now refuses both unstaged and staged
bound-source drift before a commit or deployment can reuse the artifact.

The adversarial review then demonstrated that link creation could escape the filesystem guard even while the
five original escape probes passed. Revision 6 guards `os.symlink`, `os.link`, `Path.symlink_to` and
`Path.hardlink_to`, refuses links whose source/target or destination resolves outside temporary state, and adds
all four operations to the machine PASS predicate. The claim is limited to the explicit guarded primitives.

The next adversarial pass mutated production to combine the ordinary kill flag with the typed F6 verdict; the
direct-helper simulation still passed. Revision 7 strengthens the production-wiring structural assertion so the
`block_starter` keyword must be exactly `_f6_kill_block_entries`, rejecting Boolean combinations or substitutes.
The connected simulator proves helper behavior; this assertion proves production supplies the intended verdict.

The final adversarial pass rejected a status-only partial-order fixture. Revision 8 now creates a real one-share
partial trim fill, reduces the simulated broker position, appends a broker fill, runs canonical sync, restarts the
manager, and completes the second share. This intentionally proves the production contract rather than claiming
incremental ownership accounting that does not exist: while the order remains active, protected-floor replay
refuses the broker shrink, ownership stays at the pre-order floor, the rung remains reserved, startup remains
blocked, and no duplicate sell can be submitted. Once the broker order is terminal, the manager applies the exact
cumulative two-share reduction once, canonical replay heals to the broker position, and the rung becomes done.
Dropping the partial fill or allowing a restart duplicate makes the scenario fail.

The next exact-byte reviews rejected two more false passes. Revision 9 excludes macOS AppleDouble metadata
from both working-tree and Git source manifests, so the committed artifact now binds exactly to the 143 staged
Python sources instead of an isolated-copy sidecar set. The production-wiring assertion now requires exact AST
values for all four `resolve_f6_kill_route` arguments; wrapping or replacing the control-fault input fails the
test. The offline broker also rejects duplicate client-order IDs, the deterministic ID generator advances per
logical side/order, and the result requires every submitted client ID to be unique. This makes the second trim
exercise a broker-admissible order instead of silently accepting an ID already consumed by the first trim.

Revision 10 closes the two remaining orchestration-test gaps found by the exact-diff reviews. The production
assignment from `_f6_kill_route.block_starter` to `_f6_kill_block_entries` is now pinned as an exact AST shape,
so negating or wrapping the typed verdict fails. The helper's starter gate is also required to be the direct
positive `if block_starter` branch; inverted polarity no longer passes through a mere name-presence check. The
exception-restoration regression now saves and restores the test process's true pre-test globals rather than
leaving hard-coded defaults behind, preventing suite-order contamination.

Revision 11 binds the final outer production seam: the helper call must be enclosed by exactly
`config.FOREVER6_ENABLED and _is_ah`, and all five helper keyword values must match their intended AST
expressions. Reversing the after-close condition or changing any handoff value now fails. Artifact validation
also requires exactly one PASS row per required scenario, so a duplicate name cannot overwrite and conceal an
earlier failure. The ambiguous `production_flag_before_and_after` evidence key is renamed
`production_flag_remained_disabled`; a true value now states its meaning directly and is independently paired
with the unchanged tracked `config.FOREVER6_ENABLED=False` check.

Revision 12 covers the production-only manager-construction branch that connected simulations intentionally
bypass through dependency injection. The non-injected helper test now requires `ForeverHoldManager` to be
constructed exactly once with the imported production `execution.broker` module. Replacing that broker with
`None` or another object fails before launch evidence can be accepted.

Revision 13 prevents the outer runner from overwriting an inner failure. Both generation and committed-artifact
checking independently require a transport-free broker, the exact four-order transcript shape, four non-empty
unique client IDs, the disabled production flag, unique scenario rows, and valid source binding. Tamper tests
flip network capability and duplicate a trim ID and require rejection. The obsolete ambiguous flag field is
removed, leaving only `production_flag_remained_disabled: true`.

Revision 14 binds the serialized order transcript to the exact symbol, side, whole-share quantity, limit-price
and field shape of all four expected orders, and requires the restored final F6 quantity plus the entry, first
trim and partial-terminal ownership quantities recorded in scenario evidence. Tamper regressions change symbol,
side, quantity, limit and final holdings and require rejection, preventing a source-valid artifact from masking
an oversell or wrong-direction order.

Revision 15 binds the sole `resolve_f6_kill_route` call directly to the `_f6_kill_route` assignment whose
`block_starter` value controls production, preventing an unused real call plus a fabricated verdict. The
simulation now also snapshots, restores and verifies both risk-manager intraday attribution caches, so its
Day-loss fixture cannot contaminate later in-process risk checks while `process_state_restored` reports PASS.

Revision 16 prevents textual ordering from standing in for control-flow proof: the trim call may not appear in
either branch of any `if` whose test references `block_starter`. This rejects attempts to place trims behind
`if not block_starter` while leaving a later positive starter-return gate and the same source-line order.

Revision 18 applies the same control-flow proof to starter reachability. The starter call may not appear in
either branch of any additional `if` whose test references `block_starter`; this rejects an unreachable
`if block_starter: maybe_start_accumulation(...)` mutation even when textual call order remains unchanged.

Revision 19 closes the production-only manager false pass. The unblocked starter test no longer replaces the
manager constructor with a mock: it requires a real `ForeverHoldManager` constructed with the production
broker module, while patching only its external-effect methods. A branch that admits injected simulators but
returns early for the real production manager now fails the executable positive-path test.

Revision 20 exercises the complete activated-production cross-product in that positive test: the imported
production flag is temporarily true at the same time that the real manager holds the production broker. This
rejects a branch that permits either condition alone but disables starters only when both identify production.
The context restores the tracked disabled flag after the call and the test requires that restoration.

Revision 21 supplies that production-shaped positive path with a timezone-aware New York timestamp and requires
the same ET date to reach `maybe_start_accumulation`. A branch that disables only the aware-time plus production-
broker combination now fails instead of escaping behind the simulator's injected broker.

Revision 22 closes descriptor-relative filesystem escapes. Every guarded `os` writer resolves relative paths
through `dir_fd`, `src_dir_fd`, or `dst_dir_fd` before applying the sandbox boundary. The simulator opens real
directory descriptors and attempts `openat`, mkdir, unlink, rename, replace, symlink and hardlink escapes; every
one must raise before the system call or the filesystem scenario fails.

## Machine-owned result

`python3 research/f6_full_path_simulation.py --output /tmp/f6_full_path_simulation.json` must exit zero and
emit `overall: PASS`. The reviewed artifact is then copied into the tracked evidence path, committed with the
code, and `--check` in the following verification commit or CI run must validate the HEAD blob before any
regeneration. Tests independently require the
shared production router, every scenario name, pre/post source binding, exact entry and trim order identities,
real terminal allocator/journal state, zero unauthorized ETF/floor/fault orders, canonical fill ownership,
network and filesystem refusal, shared-symbol stop preservation, real typed-kill derivation, exact global
restoration and unchanged production `FOREVER6_ENABLED=False` after success and injected failure.

## Executed evidence

- OCI Python 3.10 focused command: `python -m pytest -q tests/test_f6_full_path_simulation.py tests/test_f6_cycle.py tests/test_run_cycle_kill_switch_exits.py` → `32 passed`.
- OCI Python 3.10 artifact command: `python research/f6_full_path_simulation.py --output /tmp/f6-final-artifact.json` → `overall: PASS`; tracked artifact contains 20 unique PASS scenarios, 143 bound sources, four unique submitted client IDs, and `production_flag_remained_disabled: true`.
- OCI Python 3.10 full command after rebasing revision 23 onto `origin/main` `2652888`: `python -m pytest -q` → `1 failed, 2883 passed, 1 skipped, 186 subtests passed`; the sole failure, `test_qualifying_long_is_marked_pending`, reproduces from a clean `origin/main` archive. An after-hours dashboard-date fixture also reproduced on clean main during the prior time window but passed in this final run.
- Static commands over all six changed Python files: `py_compile`, Ruff `E,W,F,B`, and mypy `--warn-unreachable --explicit-package-bases` → PASS.
- Independent staged-manifest check: artifact before/after maps, Git index and working tree are identical across 143 sources; tree digest matches.

## Reversal criterion

Any direct-manager shortcut around the shared router, mocked decision/admission success, missing scenario,
pre/post SHA mismatch, network escape, unexpected order, unresolved lease/journal, ownership drift, state
leak, restart failure, or production flag mutation makes the artifact FAIL and blocks the enablement package.
The simulator must be rerun on final `origin/main` bytes after every F6-path change.

## Prior work

- `logs/design_records/f6_golive_2026-09-26.md` declares the full-path simulation as the last prerequisite before enablement.
- `logs/design_records/f6_startup_ownership_preflight_2026-10-12.md` owns exact startup replay and the durable degraded interlock.
- `logs/design_records/f6_pending_buy_reservation_2026-10-12.md` owns the unresolved-buy reservation and exact-order recovery contract.
- `logs/design_records/f6_instrument_and_kill_policy_2026-10-15.md` owns product policy and the typed 7% brake exception.
- `logs/design_records/f6_bounded_entry_2026-10-17.md` owns quote liveness, cash, event, allocator, and limit-order boundaries.
- `logs/design_records/f6_trim_resync_2026-10-17.md` owns fill-confirmed trim replay.
- `logs/design_records/f6_scoped_allocator_2026-10-18.md` owns F6-only capital admission.
- `logs/design_records/f6_emergency_flatten_attribution_2026-10-18.md` owns stop-preserving explicit shutdown.
- `logs/design_records/f6_etf_winddown_2026-10-21.md` requires an injected ETF to create zero reservations and zero orders.
- `logs/design_records/sizing_replay_gate_2026-10-19.md` is the related source-bound replay pattern; this simulator applies provenance binding to F6 plumbing rather than Day sizing.
- `logs/design_records/owner_approval_gates_2026-10-17.md` defines the exact-design approval boundary; this non-production proof leaves the enablement approval separate.
- `logs/design_records/intraday_gex_interconnect_2026-09-03.md` and `logs/design_records/gex_full_week_coverage_2026-10-17.md` are interconnection examples but do not exercise F6 ownership or orders.
- `logs/design_records/earned_concentration_sizing_2026-10-18.md`, `logs/design_records/day_tier_v2_design_2026-08-29.md`, and `logs/design_records/day_tier_track_b_inc2_2026-09-22.md` own Day sizing/routing and are outside this F6-only simulator.
- `logs/design_records/dashboard_protection_truth_2026-10-22.md` owns read-only dashboard truth and does not provide launch-path execution evidence.
- `logs/design_records/startup_managed_position_truth_2026-10-26.md` owns startup managed-position truth and alerting; this simulation consumes the same fail-closed ownership boundary without changing it.
- `logs/design_records/qhm_manual_add_adoption_2026-10-23.md` owns QHM manual-add adoption and stop resync; this F6 proof verifies shared-symbol isolation without changing QHM adoption behavior.
- `handoff.md:33-34` names this simulation and the enablement package as the exact next action.
- `gh pr list --state open` on 2026-10-23 showed only unrelated PR #727; no parallel F6 simulator exists.
- `tests/test_run_cycle_kill_switch_exits.py:198-219` structurally inspects the former inline F6 route; the shared seam requires that regression to assert the same invariants through `execution/f6_cycle.py` instead.

## Required questions

Q1. Yes — it converts the final F6 launch unknown into executable evidence for the paper-account growth engine (`handoff.md:33-34`).
Q2. No live trade count changes; the simulator only determines whether the already-built F6 path is safe to enable.
Q3. Yes — one connected deterministic lifecycle is simpler and stronger than inferring readiness from isolated component tests.
Q4. Component coverage exists in `tests/test_f6_*.py`, but `rg` found no connected full-path simulator; the prior-work records above define its boundaries.
Q5. No trading number is added; scenario inputs are fixtures used to cross existing production boundaries, not live thresholds.
Q6. It exposes the static assumption that component passes imply integration readiness; the forward pass is rerunning this source-bound simulator after every F6-path change.
Q7. It connects trigger, allocator, broker admission, pending buys, ownership replay, restart, trims, product policy, and kill attribution in one proof.
Q8. The missing launch proof is explicit in `handoff.md:33-34` and nine prior design records; no performance cause is claimed.
Q9. N/A — this deterministic plumbing proof uses boundary scenarios rather than a historical return replay; any unauthorized order, wrong ownership transition or failed recovery disproves readiness.
Q10. N/A — this validates plumbing and invariants, not an estimated trading edge or calibration.
Q11. The orchestration extraction touches routing but is behavior-preserving; it does not change size, frequency, concurrency or exposure, and F6 stays disabled pending a separate decision.
Q12. It drives the real 7% calculation from raw trading-tier P&L, excludes held-tier F6 P&L, preserves cash-only admission and protected ownership, keeps trims reachable under every cause, and allows only the existing genuine-loss starter bypass; `paper=True` and the separate enablement decision remain locked.
Q13. Missing or contradictory trigger, ownership, broker, order, or cash evidence must produce a named refusal and zero unauthorized orders.
Q14. N/A — this is a pre-launch mechanical test explicitly required by Q23, not a live shadow feature or observation period.
Q15. The artifact records every scenario verdict and evidence payload, the broker order transcript, pre/post source hashes, final F6 quantity and the production flag state.
Q16. The proof is the committed `logs/f6_full_path_simulation_latest.json` validated from the HEAD blob before regeneration; `logs/post_live/expect/f6-sim-1023.json` then requires the unique route tag after enablement and treats any call while disabled as a reversal.
Q17. The exact script will also run in an OCI `/tmp` copy under Python 3.10, with the same production SHA binding and a broker adapter that cannot access the network.
Q18. No — “components already pass” is treated as an unproved integration hypothesis until the connected lifecycle passes.
Q19. Yes — revision 2 extracts the existing F6 after-close route into a shared production helper, so Rafael must approve `F6-SIM-1023` before any production edit or commit even though behavior is intended to remain identical.
Q20. Prompts will ask reviewers to find false passes, missing paths, network escape, source-binding gaps, and state leakage without stating an expected verdict.
Q21. Yes — an isolated child in a complete temp checkout sets `MTF_TEST_MODE`, removes credentials, blocks sockets/SDK clients and built-in/`io`/`os`/`Path` writes outside temporary state, and contains deliberate escape regressions; only raw external I/O is replaced.
Q22. N/A — the helper is a behavior-preserving seam rather than a new live control; its exact call order is compared with the existing route and F6 remains disabled.
Q23. This design is the full-path readiness proof; readiness is denied unless every scenario passes on final source bytes, including real kill arithmetic, shared-symbol stop collision and durable recovery.
Q24. N/A — it makes no external absence, tradability, or ETF-availability claim; product classes are injected to test existing policy.
Q25. N/A — no stock-return, volatility, or typical-move statistic is claimed.
Q26. Production F6 uses T1 Alpaca inputs; the simulator supplies typed equivalents locally and has no fallback to any network source.
Q27. The only output is atomically replaced JSON at the explicit `--output` path; all mutable manager journals live under a temporary directory.
Q28. `strategy/run_cycle.py` delegates typed-kill resolution and its after-close F6 call to `execution/f6_cycle.py`; `main.py` keeps its one-time startup reconcile, which the simulator runs before shared multi-tick routing, canonical replay and restart reconciliation.
Q29. It is first because `handoff.md:33-34` identifies it as the single remaining prerequisite before the F6 enablement decision.
Q30. Professional launch validation requires scenario-level integration, source binding, hermetic broker isolation, state-transition assertions, and negative-path order-count assertions; those are the design.
Q31. N/A — no return series, model selection, Sharpe, or parameter trial is evaluated.
Q32. N/A — synthetic boundary fixtures do not support an edge claim; the empirical trigger's historical honesty remains owned by its prior replay.
Q33. The simulation validates limit ceilings, reservations, partial/terminal order states, and no-order refusals; it makes no fill-quality or performance claim.
Q34. N/A — no expected return or factor alpha is estimated.
Q35. The connected case covers an exact -7% genuine Day loss plus a QHM/F6 shared-stop collision; resolver units cover control/no-kill/unreadable branches. Market-regime performance, worst-day expectancy and book correlation remain outside this plumbing proof.
Q36. The production whole-share cash and allocator boundaries are exercised; liquidity/ADV expectancy is unchanged and no $25K capacity claim is made.
Q37. N/A — deterministic invariants require all declared scenarios to pass rather than a statistical confidence interval.
Q38. A source-hash change invalidates the artifact and forces rerun; live strategy decay remains owned by Confluence 2.0 and weekly evaluation.
Q39. N/A — the simulator does not alter F6 sizing; it checks the existing cash and allocator decision with exact order amounts.
Q40. It owns “F6 cannot be called ready unless its connected lifecycle works and unsafe paths send no order”; the machine-evaluated overall verdict proves it.
Q41. A production launch engineer would add hermetic adapters, state-transition proofs, source provenance, negative fault injection, replayability, and CI enforcement; this design includes them.
Q42. It drives the exact production router, manager, allocator, broker-admission, ledger-sync and journal functions; only raw external I/O is replaced.
Q43. The professional method is deterministic end-to-end scenario testing through the production orchestration seam, with fault injection, hermetic I/O and provenance-bound artifacts before activation; revision 2 uses it.
Q44. Independent cold/adversarial reviewers, routing and masked-loss board seats, Groq, Google AI Studio, CI, and an OCI Python 3.10 `/tmp` run will evaluate the exact final bytes.

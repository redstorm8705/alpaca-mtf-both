# Before/after replay gate for sizing, entry and exit changes — 2026-10-19 (Claude)

Approval-ID: REPLAY-1019
Covers: execution/day_trade_manager.py, strategy/day_tier_logger.py, .claude/preship/decision_replay.py, .claude/preship/preship_gate.py, .github/workflows/preship-verify.yml, tests/test_decision_replay.py

## Why (owner, 2026-10-19)
"Why did you reapply the cap? Did anyone authorize you to do that? You cost us money today. What will you be doing
moving forward to ensure this never happens again?"

## What happened (measured)
- CONV-1018 (PR #709, commit 25582c2) replaced `_bounded_entry_qty` for Day entries. It copied the Track-B dollar cap
  but not its 10/10 (max_size) exemption (CEO 2026-10-10: "no caps if the conviction is high and the stops are being
  respected"). The approved design record never mentioned the Track-B cap.
- 2026-10-19 07:16 PT AMZN, Track B, ten_of_ten=true: conviction sizing wired 18 shares (per-share risk $5.51), the
  cap cut it to 1 (logs/day_tier_events.jsonl decision DT-AMZN-b-1792419387444-a26f0a63). Result +$3.29; at 18 shares
  about +$59.
- Every reviewer passed it. Nothing compared the new path's sizes with the old path's on real decisions.

## The control (a check that can block the ship, not a written rule)
1. **Log the full sizing input.** Each Day `decision` record gains `sizing_inputs`: equity, start-of-day equity, buying
   power, maintenance margin and rate, the order and stop prices, the size dict, open Day lots, open orders, and the
   state-only lots — everything the wired quantity depends on.
2. **One pure sizing function.** The wired-quantity calculation in `place_entry` moves, unchanged, into
   `wired_entry_qty(inputs) -> (qty, why)`. It makes no broker call, so it can be replayed. This ships first, as its own
   diff (tidy-first). Its test proves it returns the same quantity as today for every logged decision that has inputs.
3. **The replay gate** (`.claude/preship/decision_replay.py`). For any diff touching a Day sizing, entry or exit file, it
   runs every logged decision since `sizing_inputs` began through `wired_entry_qty` on origin/main and on the staged
   code. It lists each decision whose wired quantity, or skip/enter result, differs.
4. **Every difference must be named.** The design record of the change carries a section, "Expected replay
   differences": the symbols, dates and old→new quantities the owner approved. `preship_gate` blocks a commit or push
   when the replay shows a difference that section does not list, or when the replay cannot run. A diff with no
   differences passes.
5. **Approval packages show the replay table.** The owner sees, in plain English, which past trades would have been
   larger, smaller, added or skipped, before saying approve.

## Revised design after review (2026-10-19: board Beck + Kim, Gro, GAI — all APPROVE-WITH-CHANGES, aligned)
Each reviewer found the first version would NOT have caught CONV-1018 at PR #709. The cause was the same in every
review: no logged inputs existed yet, and the new path needed inputs the old one never logged. The revised build
is three separate diffs, in this order:

1. **Extraction + golden master (behaviour-neutral, risk-path refactor).**
   - The Day entry's wired result `(limit, stop, qty, why)` moves, unchanged, into one pure function that makes no
     broker call. All live reads (state, journal, live price, quote, ATR, spreads, account, positions, orders) happen
     first, in the caller, and are passed in as one snapshot.
   - It covers the stop and limit, not only the share count, because a stop change changes the quantity.
   - A hand-built grid of cases is pinned on origin/main. The grid crosses: track A/B/M, 10/10 or not, deep or thin
     name, long or short, Track-B reserve on or off, conviction-sized or today's sizing, and which room binds.
   - Any later diff that changes a grid result fails the check unless the owner-approved record lists that change.
   - This alone would have caught both CONV-1018 drops on day one, with no history needed.
   - The extraction diff itself must show zero differences.
2. **Log the inputs on every attempt.**
   - Each Day entry attempt that reaches sizing logs its raw input snapshot and the pre-wire size dict. This includes
     skips: room-stop abort, below minimum, risk budget, co-hold, short maintenance.
   - A logging failure never blocks the entry (warning only).
3. **The gate.**
   - `.claude/preship/decision_replay.py` runs the grid plus every logged production decision through the function
     on origin/main and on the staged code.
   - The decisions are read read-only from the server's log, and the corpus hash and count go in the marker.
   - The gate runs in the local preship hook AND as a required CI check on main.
   - It blocks on any difference not listed in the change's "Expected replay differences" section, and on any
     replay that cannot run. The one exception: decision classes the owner lists as "unreplayable: new input X",
     so new growth work is never blocked forever.
   - A permanent test plants the CONV-1018 mistake (drop the 10/10 exemption and the per-name rooms) and must be
     blocked.
   - The check must finish in under a minute.
4. **Exits** (stop/target moves, scale-out, force-close) follow on the same pattern as a fourth diff.
   - GAI asked for exits in the same diff. The board and Gro asked for a separate step after entries.
   - The aligned order is entries first, so the risk-path refactor stays small and reviewable; exits come
     immediately after.

Rejected, with reasons:
- Gro asked for a full-cycle P&L simulation matching live to ±0.1%. Rejected: a fill and slippage model is not
  needed to catch sizing drops, and it would make the gate slow and flaky.
- Gro asked for a separate "kill-switch" ticket system. Rejected: it duplicates the fail-closed block.
- Gro cited three things as evidence: an audit report `audit_2026-10-19.pdf`, a dashboard image, and
  `risk/kelley_sizing.py` / `compute_target_qty`. A search of the repo and the server found none of them.

## What it would have shown on CONV-1018
All five earlier 10/10 Track-B entries had max_size=True and skipped the cap under the old path. Under the copied cap,
floor(logged Track-B budget / fill price), with a minimum of 1 share, they would have been wired:
- PLTU 10/09: 19 shares → 1 ($130.53 budget at $67.32)
- UBER 10/14: 18 → 2 ($141.32 at $70.12)
- NFLX 10/14: 5 → 2 ($141.42 at $69.56)
- TSLA 10/15: 4 → 1 ($374.39 at $355.47)
- AVGO 10/16: 3 → 1 ($352.69 at $335.67)

Computed from the logged decision budgets and fills (logs/day_tier_events.jsonl). These are the cap's results only;
the conviction-sizing quantity before the cap was not logged for those dates.

## Limits (stated, not hidden)
- The replay covers decisions logged after item 1 ships. Until enough exist, the gate also requires the author to list
  every cap and exemption of any code path being replaced, and a test for each exemption.
- It replays sizing and the enter/skip decision. Exit behaviour replay (stop and target paths) is a second step on
  the same pattern, using the exit records already logged.

## Required questions
Q1: Protects data collection and the $25K goal: an approved edge is never silently shrunk or blocked by an unreviewed rule.
Q2: No change to trade count; the gate only makes changes to trade count or size visible and approved.
Q3: Yes. The simpler option (a written rule) is what failed; only a blocking check prevents a repeat.
Q4: Builds on the decision records (logs/day_tier_events.jsonl), the preship gate, and the owner-approval gate; no existing replay tool.
Q5: No new numbers; the gate compares quantities, it sets none.
Q6: Static assumption exposed: "reviewers reading code will catch a dropped exemption". Forward: extend the replay to exits and the Swing tier.
Q7: Feeds the approval package and the preship gate; fails closed (replay unavailable = blocked).
Q8: The AMZN facts come from the logged decision record and the commit history (25582c2).
Q9: Will be validated (nothing is built yet): the golden-master grid on origin/main, the extraction diff showing zero differences, and from 10/20 the production corpus matching live wired quantities. Proven wrong if the planted CONV-1018 diff passes.
Q10: Not an edge decision.
Q11: Item 2 is a behaviour-neutral refactor of the sizing path (risk-path code): board masked-loss seat reviews it and the replay must show zero differences.
Q12: Safety envelope untouched.
Q13: Missing inputs or a replay crash block the ship rather than pass it.
Q14: Not a shadow; the logged inputs are used by the gate on every relevant ship.
Q15: Adds the full sizing input to every decision record.
Q16: Proof: decision records carry `sizing_inputs`; the gate's marker lists the replayed count and differences.
Q17: The live entry path will call the extracted function (a test asserts it), and the first full session is replayed against live wired quantities.
Q18: The AMZN 1-share trade is treated as a process failure, not explained away.
Q19: Changes the Day entry path only by extraction (no behaviour change); the owner approves this design.
Q20: Facts only in review prompts.
Q21: The replay calls no broker; inputs come from logs.
Q22: The gate runs read-only against the server logs before it is relied on.
Q23: Not yet. Full-path test planned: grid + production corpus through the extracted function equals live wired quantities, plus a CI run on a deliberately broken branch.
Q24: The design makes no claim that a tool, file or instrument does not exist, except Gro's three cited items, which were searched for in the repo and on the server this session and not found.
Q25: The design quotes no stock or market statistic; its only figures are the bot's own logged quantities (AMZN 18, 6 and 1 shares; the five earlier 10/10 trades).
Q26: Inputs from the bot's own logs (from T1 Alpaca reads at decision time).
Q27: Decision records: append + fsync (existing). Gate marker: .claude/preship/markers (existing pattern).
Q28: Logging inside place_entry before the order; the gate inside preship_gate at commit/push.
Q29: Top: it protects every future sizing, entry and exit change.
Q30: Standard practice: regression replay of production decisions before a model change ships.
Q31: Not a fitted model.
Q32: The replay uses only inputs logged at decision time (no look-ahead).
Q33: The gate compares wired share quantities before an order is sent, so spread, slippage and fills are outside it; live fills stay the record of real cost.
Q34: The gate takes no market position and adds no exposure; it only blocks unapproved changes to how existing trades are sized.
Q35: The replay is deterministic in any market regime; the golden-master grid includes high-volatility stop widening and thin names so those paths are covered.
Q36: Unaffected by account size.
Q37: Not a statistical claim; deterministic comparison.
Q38: The gate is not an edge and cannot decay; if it ever passes a planted dropped-rule diff, its own test fails and it comes back to the owner.
Q39: Sizing unchanged by this design.
Q40: Outcome: no sizing, entry or exit change ships with a difference the owner did not approve. Check: the gate blocks an unlisted replay difference (tested by a planted dropped-exemption diff).
Q41: An expert would also replay exits and keep a standing regression set of hand-built edge cases (10/10, Track B, shorts).
Q42: Rawest data: the logged decision inputs and broker fills.
Q43: Professional method: shadow-free regression replay of production decisions (deterministic, pre-ship).
Q44: Independent evidence: the gate blocking a planted copy of the CONV-1018 mistake in its own test.

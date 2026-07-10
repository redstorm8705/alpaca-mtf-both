# Forever-6 — Expanded Crash-Scenario BUY-Rule Map — BOARD RESULT (2026-07-09)

## ★ RAFAEL DECISIONS — LOCKED 2026-07-09 (both forks)
- **FORK A → PER-RUNG MONOTONIC LATCH.** One buy per depth level per (symbol,date): once at −7,
  once at −13, once at −20; each level only ONCE (monotonic — can't re-fire a lower rung); max 3
  buys/name/day; still bounded by ≤3/day ceiling + geometric decay + CAP truncation. (Board 3/5;
  −20 fills next session by market mechanics, so not same-day piling-on.)
- **FORK B → SMALL FIRST SLICE FIRES IMMEDIATELY + SLACK PING + HUMAN-GATE THE DEEPER RUNGS**
  (Sosnoff option). On a −30% single-name EARNINGS gap (routed via the FMP earnings-calendar
  origin classifier): fire ONE first rung at 1.0× (smallest, ~1 share) immediately, Slack-notify
  concurrently, and require Rafael's OK before any 1.6×/2.5× follow-on rung fires (those arrive 1–3
  sessions later — human has time). Recorded caveat: the risk (Thorp/Taleb) + execution (Harris)
  seats preferred a PRE-fire gate (default-skip) because never-sell makes a wrong entry permanent;
  Rafael chose the immediate-small-first-rung path — bounded because rung-1 is the smallest tranche
  and all deeper conviction sizing stays human-gated.


**Item (b)** of the "still owed before API" list in `logs/build_f_decision_2026-07-08.md`.
Design/board pass ONLY — no code. Risk-path → cold board (Thorp/Taleb masked-loss seat + Sosnoff
aggression + Harris execution) + Gro + GAI, same task spec to all. Constants were LOCKED going in
(margin ≤30% MV, CAP 35% fixed reserve, convex ladder 1.0/1.6/2.5× @ −7/−13/−20, geometric-decay
base, exit 25%@10x/25%@20x, per-day latch ≤3/day, marketable limit, data-quality gate). This pass
mapped every sell-first/no-buyers scenario onto those constants and resolved the 2 open forks.

## VOICES
Gro, GAI, Thorp/Taleb (risk/ruin/masked-loss — mandatory seat), Sosnoff (aggression/pacing),
Harris (execution/microstructure). All 5 returned full scenario maps.

## CONSOLIDATED SCENARIO → RULE MAP (board-aligned unless noted)

| # | Scenario | Detection | Buy | Notes / dissent |
|---|----------|-----------|-----|-----------------|
| 1 | Flash crash, single name −7→−10% in 1–3 5m bars | per-name session-cum ≤ −7% confirmed on 2nd bar close (never bar-1 wick); data-quality gate first | rung by DEPTH (1.0× at −7, etc.), concentrate on the one name, marketable limit last×1.01–1.015 | abort if spread >3% at signal (Harris) |
| 2 | Halt (LULD/news) → RESUMPTION | Alpaca tradable False→True | NEVER buy the reopen auction/indicative cross — wait for 1st REAL continuous trade (30–60s cooldown), re-run data-quality on it, rung by that print's depth | Harris + Thorp/Taleb: halts are the #1 bad-print cluster point |
| 3 | MWCB −7/−13/−20 (L1/L2/L3) | session-cum SPY vs prior close at MWCB bands | spread across all 6 (systemic, no name-edge), rung = band. **L1/L2 halt 15m then resume same-day; L3 halts for the DAY → its rung fills NEXT session open** (Harris) | ≤3/day ceiling caps to 3 of 6 names/event → prioritize by largest CAP-headroom (Sosnoff) |
| 4 | Intraday SPY −5% orderly | session-cum SPY −5%, no halt | **NO buy — −5% is below the −7 ladder floor by design** (Sosnoff/Thorp) | (Gro/GAI had a 1.0× buy here — overruled; ladder starts at −7) |
| 5 | Weekly crash −10/−20% over a week | cumulative depth vs an ANCHORED reference (rolling high / drawdown-start), NOT naive daily re-base | rung by cumulative depth; ladder progresses across days | **NEW GUARD**: anchor the reference or the ladder mis-fires (Thorp/Taleb) |
| 6 | Bear market (SPY >20% below 90d high) | LOCKED price-depth pacing: buy per additional 5% SPY drawdown | rung by name's own depth; CAP becomes the binding constraint (correct) | most benign fill regime (Harris); hardest emotionally |
| 7 | Gap-down open −10%+ at 9:30 | opening print vs prior close, 2nd-bar/30–60s confirm | NEVER buy the 9:30 opening auction cross — wait for 1st continuous trade (Harris) | same auction-print risk as a halt reopen |
| 8 | Sector selloff (e.g. semis −8%, SPY flat) | per-name −7+ while SPY doesn't confirm | scope to AFFECTED names only — do NOT spread to unaffected names | (Gro/GAI overruled — no false diversification) |
| 9 | Single-name earnings gap −30% | gap + earnings-calendar proximity tag | **FORK B** (see below) | never buy the earnings print; info-driven, keeps sliding 10–30m |
| 10 | VIX spike (>40), modest price move | VIX>40 alone | **NO buy — VIX is not a price-depth trigger; context annotation only** | Thorp/Taleb + Sosnoff + Harris all overrule Gro/GAI's "buy" here |
| 11 | No-bid / illiquid, limit won't fill | NBBO spread ≥5% (or 3× trailing) / bid<$2K / no bid | no fill = no buy; NEVER widen past 1.02, never market | **NEW GUARD**: unfilled order must NOT consume the latch or ≤3/day ceiling |
| 12 | Fat-finger / bad print | close<0.5×prior OR zero-vol, BEFORE the detector | reject, no order constructed | **NEW GUARDS**: (a) 2nd-bar confirm as a 2nd independent gate; (b) 0.5×/2× check vs longer-window last-known-good, not tick-to-tick (chained-bad-print defense); (c) Slack-log every reject |
| 13 | **NEW — correlated tech-basket crash** | ≥3 of 6 names past −7% same session while SPY does NOT trip MWCB (F6 is tech-concentrated) | treat as a #3-style spread event across affected names; Slack-flag as basket-level | Sosnoff — the scenario most likely to be missed by SPY-only + single-name detection |

## FORK A — the −20 (L3) latch-upgrade exception
**Board: 3 ALLOW (Gro, Sosnoff, Harris) · 2 HOLD-strict (GAI, Thorp/Taleb).**
- **Reconciliation (Harris, decisive):** L3 halts trading for the rest of the day, so a −20% buy
  physically fills at the NEXT session's open, not same-day → it is not "compounding into a
  same-day cascade" at all; it's a separately-timed tranche on a fresh day/fresh latch.
- **Sosnoff reframe:** the latch should mean "one buy per RUNG per (symbol,date)," monotonic
  (can't re-fire a lower rung), max 3 rungs = 3 buys/name/day, still capped by ≤3/day + geometric
  decay + CAP. He EXPLICITLY rejects a 2nd buy on an already-maxed 2.5× rung — which is Thorp/Taleb's
  actual fear. So the two seats agree on substance (no same-rung double-buy, no unbounded averaging).
- **RECOMMENDATION → PER-RUNG MONOTONIC LATCH.** Lets the convex ladder function as designed
  (−7 and −13 can both fire same-day through the 15-min halts; −20 fills next session), while the
  monotonic + ≤3/day + geometric-decay + CAP truncation hard-caps it. Honors the masked-loss seat's
  invariant (no averaging into a deepening crash) via mechanics, not via a blunt 1/day rule.

## FORK B — −30% single-name earnings gap
**Board: all 5 PERMIT (not block) — but split on TIMING of the human check.**
- Gro/GAI: fire the buy, escalate after. Sosnoff: fire a small 1.0× first rung immediately, gate
  the deeper rungs. **Thorp/Taleb + Harris (decisive): gate the buy on a PRE-FIRE human thesis
  check, default-to-SKIP on timeout** — because under never-sell you cannot un-buy a broken-thesis
  name; the asymmetry (cheap to wait, catastrophic to be permanently long a broken name at depth)
  demands the human look BEFORE the fill, not after. Fill quality is a non-issue (these are the 6
  most liquid mega-caps); the only risk is thesis risk.
- **RECOMMENDATION → PERMIT, but PRE-FIRE Slack human-thesis gate + default-skip on timeout**, and
  add an **earnings-origin classifier** (FMP T2 earnings calendar) to route −30% earnings gaps to
  this path vs the normal ladder. Nothing is lost by waiting — the position isn't gone, only the
  entry timing; and never-sell makes a wrong entry permanent.

## NEW GUARDS TO FOLD INTO THE BUILD (surfaced by the board, none optional)
1. Per-rung monotonic latch (FORK A).
2. Latch + ≤3/day ceiling decrement ONLY on confirmed fill — never on submission (no-fill / data-
   quality reject must not burn the day's shots). Unanimous.
3. Never buy an auction/reopen/opening cross — wait for 1st real continuous trade + cooldown; L3
   fills next session. (Harris, scenarios 2/3/7/9)
4. Anchored reference price for multi-day/weekly depth — not naive daily re-base. (Thorp/Taleb)
5. Chained-bad-print defense: 0.5×/2× sanity vs a longer-window last-known-good, not tick-to-tick;
   + 2nd-bar confirm as an independent 2nd gate. (Thorp/Taleb + Harris)
6. Spread/liquidity gate: abort if NBBO spread ≥5% (or 3× trailing) / top bid < ~$2K / no bid. (Harris)
7. VIX>40 alone = context only, never a standalone trigger. (all 3 seats)
8. Sector selloff scoped to affected names only. (Thorp/Taleb + Sosnoff)
9. NEW scenario #13 detector: ≥3-of-6 names past −7% w/o MWCB = basket event. (Sosnoff)
10. Earnings-origin classifier (FMP calendar) for FORK B routing. (Thorp/Taleb + Sosnoff)

## STILL OWED AFTER THIS PASS (unchanged from decision doc)
(c) full-read gate on `execution/quarterly_hold_manager.py` (1954L) — forever-6 extends it;
(d) final board + Gro + GAI on the fully-mapped combined proposal → then API build.
Two forks above go to Rafael to LOCK first.

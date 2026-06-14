# OPEN QUESTION — BV-5 MRI Hard Block vs. Invariant #9
**Date:** 2026-06-12 (decisions locked 2026-06-13) | **Session:** remote (claude/vibrant-cannon-ppodcr)
**Status:** Board vote COMPLETE (3 cold parallel subagents: BoD/AB/TB lenses). DS + GAI **NOT RUN** — API keys unavailable in remote container (keys live in `/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env` and on OCI). Prepared same-prompt below.
**Authority:** Rafael is sole mandate authority. No code has been edited. Any patch requires a fresh full Steps 1–9 sequence (RULE C-2/C-7) including DS/GAI in the live patch session (RULE C-3).

---

## RAFAEL'S DECISIONS — LOCKED 2026-06-13

1. **Option C** — demote BV-5 AND fix the news_alerts component. ✅
2. **Residual hard block: HIGH + CRITICAL** (score ≥ 61). Claude recommendation accepted. Rationale: once the news component is capped (decision 3), the score reaches HIGH only on genuine multi-component price stress; the week-long dark period occurred at STRESSED (41–60), which Option C makes tradeable again at 0.70x size + MIN_SCORE +2.
3. **News cap: 15** (down from 35), graduated, **plus** a real price-confirmation gate (top tier unlocks only when price_score ≥ 10, not the current "any non-zero component"). Claude/board blend (AB 15 + TB/Dalio/Taleb gate-strengthening). Conservative alternative (cap 10) noted but not chosen.

**Deferred to patch session (needs Mac/keys):** DS/GAI external audit (RULE C-3/C-4), OCI deploy. Decisions above are inputs to the full Steps 1–9 sequence — they do NOT substitute for it.

**Patch order (RULE C-6, each file its own full sequence):**
- (a) `events/macro_risk_index.py` — news cap 15 + price_score ≥ 10 gate in `inject_news_state()`
- (b) `events/news_monitor.py` — word-boundary keyword matching (`\b`) + market-scope filter for ambiguous tokens
- (c) `strategy/run_cycle.py` — narrow BV-5 tuple `("STRESSED","HIGH","CRITICAL")` → `("HIGH","CRITICAL")`
- (d) `CLAUDE.md` invariant #9 wording update (reflect HIGH/CRITICAL hard block + size/score floors below)

Recommend (a)+(b) land before (c): fix the signal before relaxing the gate that reads it (Kyle/Schneier concern).

---

## Problem (code-verified)

- `strategy/run_cycle.py` L1385–1407 — **BV-5** hard-blocks ALL new entries when `mri.level()` ∈ {STRESSED, HIGH, CRITICAL}, returning from `run_cycle()` BEFORE `run_scan()` (L1410). Comment: *"Intentional: MRI blocks entries (not just adjusts size/score). STRESSED/HIGH/CRITICAL = 0% WR per live trade log (23 exits, 2026-05-05)."*
- Contradicts CLAUDE.md invariants #1 (SPY 5-min bar-over-bar is SOLE entry gate) and #9 (MRI background only — size floor + MIN_SCORE floor, never a direct gate).
- The 23-trade 0% WR justification predates S57 stop widening (1.75→2.0), S58 negative-Kelly floor fix, and PDT removal — and is below the project's own 30-sample Kelly threshold.
- Live impact: MRI STRESSED ~70% of cycles week of 6/8 (106/151 refresh samples); 0 entries since 2026-06-08.

## Factual corrections found by the board (vs. original framing)

1. **A market-reaction-first gate ALREADY exists** (`macro_risk_index.py` `inject_news_state()`, board fix Apr 15 2026): when `price_score == 0`, news bonus is capped at +10. The problem is the gate's threshold — **any single non-zero price component (e.g. VIX 19.5 → 9 pts) unlocks the FULL +35 news bonus** (5+ alerts → +35; 3–4 → +20; 1–2 → +10). That is how 35/54 pts came from headlines this week.
2. **STRESSED soft-handling already exists and is wired**: `_SIZE_FLOOR["STRESSED"]=0.70` (applied run_cycle.py ~L1104–1109) and `_SCORE_FLOOR_DELTA["STRESSED"]=+2` (applied ~L1155–1157). Demoting BV-5 requires no new mechanism — only removing/narrowing the early return.
3. **Keyword matching is substring, not word-boundary** (`news_monitor.py` L444–447: `if k in lower`): "ppi" matches inside "shipping"/"apple"; "congress" matches "congressional". No symbol/market-scope filter. Confirms the reported false positives mechanically.
4. **News alert persistence:** 30 min (`_alert_duration_mins = 30`); bonus clears when alerts subside (`inject_news_state(0)` path).
5. MRI level thresholds: NORMAL 0–20, ELEVATED 21–40, STRESSED 41–60, HIGH 61–80, CRITICAL 81+.

## Decision fork

- **A.** Restore invariant #9: demote BV-5 — STRESSED → MIN_SCORE floor (+2, already coded) + 0.70x size floor; hard block only at HIGH/CRITICAL.
- **B.** Keep BV-5 as-is; amend CLAUDE.md invariant #9 to match code.
- **C.** A **plus** fix the news_alerts MRI component (cap and/or stronger price-confirmation gate).
- **D.** Keep BV-5; fix only the news_alerts component.

## Decision table (3 cold parallel subagents, 17 member voices)

| Voice | Vote | Core argument |
|-------|------|---------------|
| Simons (BoD) | A | 23-trade 0% WR is stale evidence from a different parameter generation; price-driven regime detection over exogenous gating |
| Taleb (BoD) | C | News-driven gating is fragility — headlines held the bot dark 4 days with VIX 19.5; cap news / require price confirmation |
| Shaw (BoD) | A | Macro regime is a multiplier on signal quality, not a veto; sizing + quality bar, never paralysis |
| Kyle (BoD) | D | Fix the signal source before the gate that depends on it; hard block defensible only if MRI score is correct |
| Peterffy (BoD) | A | Hard block is a single point of failure; layered gates (MIN_SCORE + size) are resilient |
| Thorp (AB) | A | n=23 < the project's own 30-sample Kelly minimum, from a different stop/Kelly regime — statistically meaningless; RoR acceptable under 0.70x + 7% kill switch + 2.0x stops |
| Harris (AB) | A | Sizing, not gating, is the correct lever for degraded microstructure; VIX stop-widening already auto-activates |
| Dalio (AB) | C (lean A) | News at 65% of MRI = sentiment/price correlation collapse; require sustained or price-confirmed news |
| Tudor Jones (AB) | A | Regret asymmetry: sitting out two-sided volatility costs more than trading it at reduced size + raised bar |
| Douglas (AB) | A | The code/invariant contradiction is itself the discipline failure; restore the documented rule or rewrite it openly — never leave them in conflict |
| Brandt (AB) | A | High bar (11+/12) + reduced size + wide stops is exactly the right high-vol entry posture; absence is surrender, not management |
| McKinney (TB) | C | Substring keyword matching is a signal-integrity collapse; word-boundary regex + scope filter + permanent low cap |
| Derman (TB) | A | Risk should degrade gracefully, not cliff off; STRESSED (41–60) is a yellow light — hard block belongs at the tail only |
| Schneier (TB) | D | Keyword spoofing = poisoned adversarial input; fix input validation structurally (word-boundary + scope) before touching gate policy |
| Majors (TB) | C | Root issue includes observability — dashboard must show component breakdown ("news 35/54, gated: no") so STRESSED is explainable |
| Beck (TB) | A | Hard block destroys the feedback loop: with it in place you can never collect post-S57 STRESSED data to validate or retire it |
| Katsuyama (TB) | C+A | Gating on 30-min-lagged news while executing 5-min bars is latency arbitrage against yourself; fix news, demote block, shrink persistence window |

**Tally:** A = 10 | C = 5 | D = 2 | B = 0.
**Consensus: Option C** (C is a strict superset of A — combined A-or-C = 15/17). Both dissenters (Kyle, Schneier) demand the news-component fix that C includes; their dissent is only on keeping the hard block.

**Board recommendation: Option C — demote BV-5 AND fix the news_alerts component — as two separately-sequenced patches** (RULE C-6: run_cycle.py and macro_risk_index.py/news_monitor.py each get their own full Steps 1–9).

## Sub-decisions for Rafael (open parameters)

1. **Where does the hard block remain?** Fork as framed: HIGH+CRITICAL. Derman/Beck/Katsuyama argued CRITICAL-only (HIGH → 0.55x size + MIN_SCORE +3, already coded). **Needs Rafael's call or DS/GAI input.**
2. **News cap value:** TB proposed 10 pts flat; AB proposed 15 max (15/10/5 schedule); original framing said ~20. Alternative/additional: strengthen the existing gate from `price_score == 0` to `price_score < 10` (so one mild component can't unlock +35).
3. **Keyword matcher fix** (news_monitor.py): word-boundary regex (`\b`) + market-scope filter for ambiguous tokens ("ppi", "cpi", "congress"). Unanimously endorsed across boards; separate patch.
4. **Stop-gap option:** if Rafael wants the bot trading before the full sequence completes, the smallest reversible change is narrowing the BV-5 tuple `("STRESSED", "HIGH", "CRITICAL")` → `("HIGH", "CRITICAL")` — still requires full sequence + DS/GAI (RTH-impacting).

## New pre-existing bugs surfaced (queue items — not patched)

1. **HIGH — stale-level fail-closed:** `macro_risk_index.py` `level()` returns `_last_known_good_level` when refresh fails; BV-5 can then block entries for days on stale macro state (feed outage Monday → still dark Wednesday). Needs board decision: fail-open vs. staleness ceiling.
2. **MEDIUM — observability gap:** components dict has `"gated"` flag but no pre-cap/post-cap or "would-be level without news" surfaced to dashboard/logs.
3. **MEDIUM — stale comment:** BV-5 comment cites pre-S57/S58 sample as live justification.
4. **RC-3 candidate:** news_monitor.py ~L1373–1374 except block (logs warning, non-RTH path) — verify against RC-3 counter (currently shows 1 unlocalized; may be this).

## Caveats / honesty notes

- Some member positions are persona-synthesized: agents flagged several as "[inferred from general domain knowledge — not sourced to public record]". Two citations from the AB agent are unreliable and were discounted: "PTJ, *Diary of a Volatility Trader*" (not a real title) and specific SPY move figures for June 8–12 (+2.3% etc.) which the agent could not have observed — **the directional regret argument stands; the quantification does not.**
- Live log evidence (106/151 STRESSED samples, 35/54 news pts, 0 entries since 6/8) is from Rafael's report; this repo clone's logs were not re-verified against OCI.

## DS/GAI — prepared same-prompt (run from Mac or OCI where keys exist)

Send the EXACT same prompt to both (DS persona + GAI persona per CLAUDE.md):

```
P0 design review — Alpaca paper-trading bot (MTF confluence, $2.8K paper, 7% kill switch, MAX_OPEN_POSITIONS=4, 2.0x ATR stops on HIGH-vol tier, fractional Kelly 0.35).

FACTS (code-verified):
1. strategy/run_cycle.py L1385-1407 ("BV-5") hard-blocks ALL new entries when MacroRiskIndex level is STRESSED/HIGH/CRITICAL, returning before any symbol is scored. Comment justifies it with "0% WR per live trade log (23 exits, 2026-05-05)" — a sub-30 sample predating a stop-width change (1.75x→2.0x ATR), a Kelly floor fix, and PDT removal.
2. Project architecture invariants state: "SPY 5-min bar-over-bar is the SOLE entry gate" and "MRI is background only — sets size floor and MIN_SCORE floor. Does not gate entries directly." BV-5 contradicts both.
3. MRI levels: NORMAL 0-20, ELEVATED 21-40, STRESSED 41-60, HIGH 61-80, CRITICAL 81+. Soft handling already coded and wired: size floors 1.00/0.85/0.70/0.55/0.40 and MIN_SCORE deltas +0/+1/+2/+3/+3.
4. events/macro_risk_index.py inject_news_state(): news bonus +10/+20/+35 for 1-2/3-4/5+ active alerts. Existing gate caps bonus at +10 ONLY when price_score == 0; any single non-zero price component (e.g. VIX 19.5 scoring 9 pts) unlocks the full +35.
5. events/news_monitor.py keyword matching is substring ("if k in lower"): "ppi" matches inside "shipping", "congress" matches "congressional". No word-boundary, no market-scope filter. Alert persistence 30 min.
6. Live result week of 2026-06-08: MRI STRESSED ~70% of cycles (news component 35 of ~54 pts, Iran headlines; VIX 19.5); zero entries all week.

DECISION FORK — pick one and defend it technically:
A. Demote BV-5: STRESSED → MIN_SCORE floor +2 and 0.70x size floor (both already coded); hard block only at HIGH/CRITICAL.
B. Keep BV-5 hard block at STRESSED+; amend the documented invariant to match code.
C. Option A PLUS fix the news component (cap bonus at 10-20 pts and/or require price_score >= 10 before full bonus; word-boundary keyword matching).
D. Keep BV-5; fix only the news component.

ALSO ANSWER:
(i) If A/C: should the remaining hard block sit at HIGH+CRITICAL or CRITICAL only? Why?
(ii) Exact news cap you'd choose (10/15/20) and/or price_score gate threshold, with failure scenarios.
(iii) Risk: macro_risk_index.level() returns last-known-good on refresh failure — BV-5 then blocks on stale state indefinitely. Fail-open, staleness ceiling, or keep fail-closed?
(iv) Any failure mode the 17-member internal board missed (their vote: 10 A, 5 C, 2 D — consensus C).
Be concrete: exact lines, exact thresholds, reproducing conditions. No hedging.
```

## Next steps (pending Rafael)

1. Rafael picks the option + sub-decision parameters (or asks for DS/GAI first — prompt above is ready).
2. Patch session (with API keys available): fresh full Steps 1–9 per file — run_cycle.py (BV-5), then macro_risk_index.py (cap/gate), then news_monitor.py (word-boundary) — each independently (RULE C-6). All three are RTH-impacting → DS/GAI mandatory per file.
3. CLAUDE.md invariant #9 wording updated in the same approval if A/C chosen.
4. Queue the stale-level fail-closed bug (HIGH) as its own item.

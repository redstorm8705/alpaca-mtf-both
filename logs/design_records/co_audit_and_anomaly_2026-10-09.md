# CO audit + anomaly program + META conflict — BGGN scope (2026-10-09, Claude)

Voices (same brief: scratchpad bggn_2026-10-09.md): board seats Harris+Douglas (execution / exit discipline),
López de Prado+Thorp (validation / risk), Gene Kim+Charity Majors (process / observability); Gro (Groq gpt-oss-120b);
GAI (Gemini 3.1-flash-lite). Gro's quoted back-test numbers ("120k ticks", "+$12 over 90 days") were never run —
discarded; only its design points are used.

## 1. META 2026-10-08 — conflicting signals on an open Day lot (facts verified on the SIP tape + OCI logs)
- 09:38 ET LONG 1 @ 721.27 ("failed downside sweep of put_wall 720"). IEX quote bid 716.14 / ask 722.61 ($6.47).
  Structural stop 719.44 widened to 709.67 by the room-stop rule (2x spread = $12.94). Target 725.78.
- 09:40-09:44 and 12:20-12:24 ET: SHORT triggers on the SAME 720 strike (call wall = put wall = a pin); skipped by
  "one direction per stock". Best +2.59 (723.86 ~10:35), worst -9.59 (711.68 at 13:25), exit 720.67 at 15:58, -0.60.
- Only logged instance (rule is 2 days old). Exiting at either trigger would have lost more (-2.4 / -1.8).

Consensus (all five voices agree on the defects; 4 of 5 against reversing):
- **Defect A — spread-widened opening stop.** A $6.47 IEX top-of-book at the open is a thin-book quote, not the cost
  of trading [hypothesis — verify vs SIP quotes]. It turned 1.83 of risk into 11.60 for 4.51 of reward (b ≈ 0.39,
  breakeven win rate ~72%, Thorp). Fix: cap the spread term at the volatility floor / treat an IEX spread above it as
  unusable; skip the entry if reward:risk after widening < 1 (LdP/Thorp, Harris; GAI/Gro: spread gate).
- **Defect B — one-strike pin fires both directions.** call wall == put wall → classify PIN; suppress failed-sweep
  fades on the side the target geometry disagrees with (Harris) / both sides (LdP, Gro).
- **Defect C — nothing acts on an open lot whose setup is contradicted.** Keep the broker stop as the catastrophe
  stop; record the unwidened structural stop as `thesis_stop`. On an opposite trigger (counted per distinct 5-min
  bar, not per tick): if the last closed bar is beyond `thesis_stop` → exit; else tighten the broker stop to
  `thesis_stop` (Harris+Douglas exit arm; LdP+Thorp and Gro tighten arm — the two combine). No reverse (GAI's
  soft-reverse is the minority; reversing adds frequency/size and one case cannot justify it).
- **Replay before ship:** rebuild Track-A triggers over past sessions on SIP 5-min bars; arms = ignore / tighten /
  exit-on-(trigger+thesis) / exit-on-trigger / reverse, ± the spread cap; metric = paired mean R per lot vs hold with
  a day-clustered bootstrap + tail check; ≥30 independent events to choose among arms. Under 30: ship only the
  bounded arm (tighten-to-thesis, can only shrink a loss) live with a written reversal criterion. Open question to
  check first: whether historical GEX walls exist to rebuild Track-A triggers (wall snapshots in logs).
- Rule E: the spread cap can raise shares on a tighter stop → risk-path, board gate. Exit/tighten rules: size 0,
  concurrency ≤ 0, frequency small (frees the stock sooner) → board gate.

## 2. CO (connection-optimization) audit — weekly, automatic
- **Engine (Kim/Majors, Gro):** hybrid. A free deterministic pass over the whole repo every run (Python/ast + the
  event logs): orphan producers (written, never read by a decision path), orphan consumers, dual writers of one
  state key, cadence mismatches between a fact's writer and reader (the QHM-heal 30 min vs ledger-page 20 min case),
  dead flags (False > 30 days), entry-gate-without-exit-counterpart (the META case), action paths without a decision
  record. Then Gro + GAI (same prompt) triage only that run's slice; keep a finding only if both flag it or the
  deterministic pass confirms it. No headless Claude by default (fits the $20/month API budget at ~$0).
- **Slices:** time axis — every PR merged since the cursor, then one backlog block (~40 PRs, docs/tests dropped)
  backward toward April 2026 (~500 merges since April → ~8 weeks); signal axis — one family per run (GEX/walls,
  MRI/VIX, ledger/tiers, QHM/F6, Track A/B, exits/stops, sizing/Kelly, news) building a signal × path matrix (wired /
  no safe cross-use + reason / gap).
- **Schedule:** OCI crontab, Saturday ~06:00 PT, `git pull --ff-only` first; writes only `logs/co_audit/` (cursor
  file, `YYYY-MM-DD.md`, append-only `findings.jsonl`); never pushes to main. One Slack digest, top 3 findings.
- **Findings → fixes:** each finding carries producer file:line, consumer path, proposed link, evidence (a real log
  line), fail-safe, test, size/frequency/concurrency delta; accepted ones ship through the normal gate.
- **Noise:** ≤5 findings/run, ≤3 shown; fingerprint per finding; a rejected fingerprint stays quiet until its code
  changes; "no safe cross-use" recorded once with its reason.
- **Cadence change:** biweekly once the cursor reaches April, every family is covered once, and two runs in a row
  produce no accepted finding from new PRs; back to weekly automatically on a burst of merges or an anomaly.

## 3. Anomaly rules (auto-open an investigation record; [page] = also Slack)
Opposite trigger on an open lot; one-strike pin firing both ways; a symbol benched all day or a module UNKNOWN on
>90% of ticks; impossible ledger value [page]; the same warning >K times a day; a page before the owner tier's own
heal; a position without a resting stop >2 min [page]; a stop widened far beyond its structural level or an entry
spread >0.5% of mid; an action without a decision record [page]; a stale heartbeat [page]; an untagged fill on a
tiered symbol; direction flip-flop >2 in 30 min; a Day lot open past 15:58 [page]; tracker vs Alpaca P&L mismatch.
Thresholds K etc. set from the first month of logs (PROV).

## 4. DRAM — young instruments (owner rule: 6+ months of data = tradable)
Coverage measured against the APPLICABLE stack (MAs the history can support), not the full stack, with a floor of
≥120 daily closes and ≥5.0 voting weight; a failed fetch on an old name still counts against coverage; emit
`stack_truncated`, `history_days`. Validation: truncate mature names to ~130 days and compare to their full-stack
side. (Gro alone kept DRAM untradable — overridden by the owner rule.)

## 5. ETF map
Add now (verified on Alpaca 10/09, IEX volume 10/08): AMD inverse DAMD (2x, 322,212); SNDK bull SNXX (1,056,025) /
SNDG (80,728) / SNDU (14,297); SNDK inverse SNDQ (2x, 324,059). Then dynamic discovery as a CANDIDATE generator only:
parse Alpaca asset names, verify sign + leverage by regressing ETF vs stock returns (wrong sign = hard reject), ≥30
sessions since listing, rank by 20-day volume, freeze a pre-market map, alert on changes, fall back to the static map.

## 6. Cross-tier exit events + the NVDA ledger warning
Each tier's exit fill appends a record and sets a dirty flag; a 1-minute "sync if dirty" runs the ledger sync at once;
the 20-min cron stays as the backstop and the full replay stays the authority. The 8/24 untagged NVDA buys
(order 7ee261e7…, sold inside QHM's 8/28 3-share sell) get an explicit order-id → tier override
(`data/state/ledger_attribution_overrides.json`) instead of a silenced warning.

## Build order
1. ETF map additions (DAMD, SNXX/SNDG/SNDU, SNDQ) — small, owner-confirmed.  2. DRAM coverage fix.
3. META: spread cap + pin suppression + thesis-stop exit/tighten — replay first (check wall history), then gate.
4. CO audit cron (deterministic pass + Gro/GAI triage) on OCI, first run Saturday.  5. Exit-event ledger nudge +
attribution override.  6. Anomaly rules feed (into the CO deterministic pass + daily).

## Round 2 (2026-10-09, after the owner's follow-ups) — board Schneier+LdP, Harris+Kyle; Gro; GAI; FACT RULE in the brief
**Claim taxonomy (20 types, all checkable at a source):** liveness/deploy status; verified/fixed/tested; quantities,
prices, P&L; counts and zeros; time; attribution (which tier did it); universals (always/never/none); capability
exists; "by design"; predictions; third-party API/model claims; what a reviewer/board said; market facts (presence
and absence); anomaly dismissal; comparative/causal; capability absence; coverage ("read the whole file"); numbers
reported by reviewers/agents; scope/impact ("display only"); negations of state ("no open orders").
**Mechanisms, in order:** (1) in-turn claims ledger — every number / SHA / verdict count in a report must appear in a
tool result from the same turn or carry "[estimate — method]"; (2) "by design / as intended / expected" needs a
design source in the same sentence, else it is an anomaly to investigate; (3) scope the existing gates' evidence to
the claim's own sentence and require a matching tool call this turn (a bare file path no longer counts).
**Reviewer prompts:** closed-world facts; quote-or-omit for every number; never report a run that was not run;
required FACTS / ESTIMATES / UNKNOWNS sections; a mechanical post-check that rejects any number not found in the
supplied material and not labelled an estimate (counter-prompt naming the tokens, never a blind re-roll).
**Dynamic liquidity (replaces the static 50K / 100x / 0.5% floors):** L1 round-trip cost (entry spread + the ETF's
own 90th-percentile exit spread for that time bucket + stop slippage from its own minute bars) <= f x stop distance;
L2 spread <= m x its own median spread for that 5-min bucket (catches dislocations); L3 order <= p x max(its own
expected IEX volume over the next window, trailing 15-min volume) — no cumulative-since-open floor; L4 liveness vs
its own typical print gap; L5 displayed size >= order when sizes are available. Baselines from the same feed (IEX)
history; missing baseline -> current static gate with the reason logged. Rule E: frequency can rise -> board gate.
**Report language:** "thin"/"illiquid" only with the instrument, the measurement and its own baseline in the same
sentence; never from a static constant, a single snapshot, a missing-data block or a morning-only reading.

## Round 3 (2026-10-09) — CO audit RE-SCOPED by the owner: adversarial ACCURACY audit (supersedes the hygiene scope in section 2)
Owner: "optimize and make sure the bot is dynamic and connected, not siloed ... an adversarial audit and POV to
specifically shift gaps that need to be connected to other bots to ensure accuracy." Voices: board Simons+Shaw
(candidates, grep-verified file:line), Taleb+LdP (method), Gro, GAI — aligned on the method below.
**Method.** Unit = a decision instance (tier, type: entry/skip/size/stop/target/exit/hold-overnight/routing,
decision_id, timestamp, inputs read). A consumption map (script, free) lists for every decision type the signals it
reads and the signals the bot already computes that it does NOT read; that complement is the candidate list.
Adversarial roles: PROSECUTOR (Gro + GAI, same prompt) argues "decision X is less accurate because it ignores Y",
with a mechanism, a pre-registered falsifier and a logged instance where Y disagreed; DEFENDER (cold Claude seat)
argues no connection / redundancy / feedback risk; FRAGILITY seat argues what happens when Y is stale/wrong; the
REPLAY is the judge (point-in-time Y only; outcomes from logged fills/price path; purged train/test; permutation
null; a trial ledger counting every candidate ever tested; candidates sharing one Y counted as one family).
**Ship live** (after replay + the normal gate, no shadow) when: falsifier did not fire, beats the deflated null, holds
on the held-out block and without its best day, Y was logged point-in-time, fail-safe exercised (stale Y -> current
behaviour), read through one isolated call with a timeout, Rule-E delta traced (non-zero -> board), new input logged
in the decision record, and any trade-count reduction justified against data collection. Connections that ADD
entries or size face the strictest bar. Recorded "no safe cross-use" with a reason when: no mechanism / an existing
proxy, Y not available point-in-time (open a logging fix), no neutral fail-safe, fails the null ("not supported at N
trials"), underpowered ("undecidable", decisions needed stated), widens downside risk, or cascades.
**Engine/cadence.** Server (free): consumption map + Gro/GAI prosecutor candidates (model canary first), weekly
Saturday. Interactive Claude (flat rate): defender/fragility seats, replay build (written once, reused), owner
report — top findings in plain English with a stock + dollar example. API budget only for the final diff gate of a
connection that passed replay. Cap ~5 candidates tested per run [estimate]; "no connection warranted" is a normal
result. Weekly until the backlog to April 2026 is covered, then biweekly; connections ship one at a time.
**First candidates (grep-verified, Simons+Shaw):** (1) Swing-breakout entry ignores earnings dates
(swing_breakout_manager.py run_entries; Swing intraday checks at entry_logic.py:1027-1094); (2) Swing-breakout entry
ignores the negative-catalyst gate (entry_logic.py:493-507 has it); (3) Day tier reads no earnings data (grep of
run_day_tier.py, day_trade_manager.py, strategy/day_tier_*.py); (4) Day tier ignores the negative-catalyst cache;
(5) Day tier ignores MacroRiskIndex (logs/mri_state.json, macro_risk_index.py:932-949); (6) Day side module could
inherit the underlying's side for a young ETF; (7) an open Day lot ignores the tier's own opposite trigger (META);
(8) Swing targets ignore GEX call/put walls (risk_manager.py:695-760) that the options scanner and Day tier use;
(9) Swing exits ignore a NEW negative catalyst on a held name (gate is entry-only, entry_logic.py:497); (10) QHM adds
ignore the catalyst gate F6 has (forever_hold_manager.py:178-186); (11) QHM/F6 adds ignore MRI + breadth; (12)
Swing-breakout ignores MRI/breadth; (13) Track B ignores the premarket cache Swing uses (entry_logic.py:381-408);
(14) Day stops ignore the VIX/ATR curve Swing uses; (15) options scanner ignores earnings/catalysts/MRI; (16) Kelly
GEX multiplier uses SPY's regime, not the name's own (kelly.py:360-407) — risk-path; (17) capital allocator ignores
per-tier realized expectancy — risk-path; (18) Day ETF routing ignores the live asset list (DAMD/SNDK case).
#7 and #9 realize losses earlier -> masked-loss seat; #11/#16/#17 can raise size -> board.

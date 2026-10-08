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

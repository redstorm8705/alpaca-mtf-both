# Forever-6 "SPY −2% Starter-Initiation" Rule — BGG DELIBERATION (2026-07-13)

**Status:** BGG (Gro + GAI + 2 cold board seats) ALIGNED on an AMENDED design. **Awaiting Rafael's
adopt/reject decision.** This is a NEW shallow "starter-initiation" trigger, DISTINCT from and ON TOP OF
the already-LOCKED deep crash-buy ladder in `logs/build_f_decision_2026-07-08.md` (do not confuse).

## Rafael's proposal (2026-07-13)
"Open starter positions in the Forever-6 (TSLA/GOOGL/AMZN/CRWD/META/NVDA) on the next day SPY is down
≥2% intraday, REGARDLESS of where each stock trades. Anchor the LONGEST-horizon tier (Forever-6) FIRST,
then cascade up the tier chain (QHM next)."

## Account reality (the binding constraint)
Equity ~$2,745 · **settled cash $681** · RegT BP $3,063. Forever-6 tier is CASH-FUNDED-ONLY (locked).

## 4-voice verdict
| Q | Gro | GAI | Thorp/Taleb | Shaw/Dalio | Consensus |
|---|-----|-----|-------------|------------|-----------|
| 1 −2% intraday trigger | CHANGES →−2.5/−3% | REJECT →−3/4% close | REJECT →−3% close | CHANGES →−3/4% close | **Too shallow → ~−3% CLOSE-based** |
| 2 buy regardless of level | +screen | +screen | +screen | +screen | **Unanimous: mandatory catalyst screen** |
| 3 F6-first cascade | APPROVE | APPROVE | APPROVE | APPROVE | **✅ Unanimous APPROVE** |
| 4 funding priority | breadth | breadth+disc | breadth+disc | **corr-rank**+disc | **Breadth, ranked by correlation** |
| 5 static −2% | dynamic | dynamic | dynamic | dynamic | **Unanimous: dynamic** |

**Dynamic formula (Shaw/Dalio):** `trigger = −max(2.0, 0.15 × VIX)%` close-based (VIX13→−2% floor,
20→−3%, 27→−4%) — keeps it a ~2σ event across regimes.

## The two ruin-class catches the board found that BOTH Gro AND GAI missed
1. **AMMUNITION CANNIBALIZATION (Thorp/Taleb).** The −2% starter draws the SAME $681 settled cash the
   locked deep −7/−13/−20 ladder needs as dry powder. A −2% day is usually day-1 of a drawdown → the
   shallow rule fires first at the least-convex node and arrives at the deep high-edge rungs broke.
   Effective over-betting of the low-edge cell (Kelly violation) / inverted barbell (Antifragile).
   **FIX:** segregate ≥60–70% of settled cash for deep MWCB rungs ONLY; cap the starter to ≤15–20% of
   settled cash + a per-month event cap; auto-DISABLE the starter once cash < the −20% rung requirement.
2. **CONCENTRATION-AS-DIVERSIFICATION + NO ALPHA (Shaw/Dalio).** All 6 F6 names are high-beta mega-cap
   tech, pairwise corr ~0.6–0.85 → buying all 6 on one down-day ≈ 1.5–2 independent bets, not 6 (Dalio:
   15 uncorrelated streams cut risk ~80%; 6 correlated ones cut ~nothing). And −2% SPY has ~0 forward
   predictability — this is beta-timing/DCA with survivorship-biased name selection, not alpha.
   **FIX:** rank each event's add by MARGINAL correlation contribution (lowest corr to existing book)
   then discount; cap the AGGREGATE beta-adjusted add per event.

## CONSOLIDATED BGG RECOMMENDATION (Rafael decides — sole authority)
Cascade (F6-first) + concept (build the base on market-wide dips) = **unanimous APPROVE**. The raw
"−2%, buy all 6, regardless" form is REJECTED by the board on the two catches above. Safe amended form:
**−3% DYNAMIC close-based (`−max(2,0.15·VIX)%`) · catalyst-screened (excludes RIVN-type dilutive/idiosyncratic
fallers) · breadth-then-correlation-ranked funding · funded from a SEGREGATED starter budget that cannot
cannibalize the crash ladder · per-event + per-month caps.** Ties into the pending catalyst/news engine
(the Q2 screen) and is a RISK-PATH build → full patch sequence + cold masked-loss seat + Gro/GAI on the
diff before any live wiring.

**NEXT:** Rafael adopt/amend/reject → if adopt, this becomes a Feature-Design build alongside the catalyst
engine (which supplies the Q2 screen).

---

## RAFAEL DECISION (2026-07-13): ADOPTED cascade + concept ("fine with what everyone approves").
Build proceeds in the board-amended safe form (−3% dynamic close · catalyst screen · breadth/correlation-
ranked · segregated starter budget). ONE open parameter → the MARGIN fork below.

## MARGIN FORK — BGG (Rafael: "$681 cash barely funds starters — use some margin?")
Gro + GAI both in; cold board seat (Thorp/Taleb/Dalio) finalizing. STRONG early alignment:
- **Q1 shallow-starter margin: Gro REJECT · GAI REJECT.** Levering into day-1 of a −3% decline is the
  death-spiral cash-only was built to prevent. Starter stays 100% CASH.
- **Q2 where: both = deep rungs ONLY (−13/−20).** Gro tighter ≤20%-of-MV; GAI the locked ≤30%-of-MV/tranche.
- **Q3: margin re-opens BOTH ruin findings** (ammo cannibalization; margin-call forced-liquidation of the
  never-sell book) — mitigated (deep-only + cap + pre-call alerts), NOT eliminated.
- **Q4 funding architecture: both pick (B) cash-only STARTER + bounded margin ONLY at deep rungs.**
- **Q5 too small? both YES.** $681 cash + high-priced names = razor-thin margin buffer; a levered 6-name
  never-sell tier on this equity has outsized margin-call risk that VIOLATES the never-sell premise. Honest
  answer: stay cash-only / fund FEWER names (1–3) / accept slow accumulation until equity grows.
- Board seat (leverage-ruin lens) verdict + "what Gro/GAI missed" PENDING → consolidate then to Rafael.

## MARGIN FORK — FINAL BGG VERDICT (unanimous): NO MARGIN — Forever-6 stays CASH-ONLY.
Cold board seat (Thorp/Taleb/Dalio) delivered the decisive finding Gro+GAI both MISSED:
- **RegT margin is computed on TOTAL account equity, not per-tier.** Forever-6 shares the Alpaca
  account with the intraday bot. Worked case: F6 on 20% margin (~$550 borrowed) + an UNRELATED intraday
  loss of $400 (a normal bad day on $2,745) → equity $2,345 → maintenance breach → broker force-sells
  the highest-MV liquid names (TSLA/NVDA) to cure it. **The never-sell book is liquidated by a loss that
  had nothing to do with it.** => margin and the never-sell invariant are MUTUALLY EXCLUSIVE.
- **Kelly:** at ~$2,745 bankroll, 6 names ~0.6-corr, basket σ~40%: ruin term ∝ L²σ² dominates growth
  ∝ Lμ for ANY L>1 → no leverage is growth-optimal on this book until equity is large enough that a
  maintenance call can't reach it.
- Q-summary all 3 voices: Q1 REJECT starter-margin · Q2 (if ever) deep rungs only ≤20% MV · Q3 margin
  re-opens both ruin findings, (ii) forced-liquidation is FATAL · Q4 Architecture B (cash starters) ·
  Q5 YES too small for a levered 6-name never-sell.

**UNANIMOUS RECOMMENDATION → Rafael:** Forever-6 = **CASH-ONLY**, fund **1-3 highest-priority names**
(breadth/correlation-ranked) all-cash now, widen to 6 as equity compounds. RegT BP above settled cash is
a TRAP number — locked/unused for F6. Awaiting Rafael's adopt on the cash-only/fewer-names framing → then
Feature-Design build (F6 starter tier, cash-only) alongside the catalyst engine (Q2 screen).

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

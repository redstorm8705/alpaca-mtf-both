# BOT IMPROVEMENTS LOG

Tracks external ideas/articles digested for possible bot integration. Each entry: source →
scope (what's MISSING / PARTIAL / ACTIVE in the bot) → key integrations it surfaces → BGG status.
This is a SCOPE/triage log, not an approval. Items graduate to a build only via the normal
Feature-Design + BGG gate.

---

## 2026-07-12 — "How Quants Use Loop Engineering to Build Alpha" (Horizon / horizon.trade)

**Digest (the framework):** turn one-shot strategy guesses into an iterative SEARCH — generate a
hypothesis → backtest → SCORE it → read why it failed → feed that back → repeat, keeping only what
survives. Four load-bearing pieces:
1. **Scoring — Information Coefficient (IC):** `IC = corr(factorₜ, returnₜ₊₁)`. A single IC is noisy;
   optimize toward its consistency, **ICIR = mean(IC) / std(IC)**. A steady modest IC beats a flashy
   one-off.
2. **Decay check — half-life:** fit the signal's persistence as AR(1), `t½ = −ln(2)/ln(ρ)`. Long
   half-life (~50d) = slow tradeable edge; short (~2d) = paying costs to chase noise. Reject short.
3. **Out-of-sample (OOS) gate — the anti-overfit step:** every surviving candidate is re-tested on
   data it never saw, and ICIR must HOLD there. Raise the bar as the attempt count grows (multiple-
   testing). Skip this and "loop engineering is just automated curve-fitting."
4. **Pitfalls:** looping on in-sample data (overfits faster), no scoring function (drifts), chasing
   high IC while ignoring stability, mistaking a better model for the method.

**Why it matters to us:** this is the RIGOROUS formalization of Rafael's **Evolution Mandate**
(2026-07-03: "a trading bot that is constantly evolving") + the **Shadow Strategy Tracker**. Our
shadows already accumulate candidate signals with flip-triggers, but the "score → gate → feed back"
loop is currently manual/session-based and lacks the IC/ICIR metric and the OOS gate. The article
names exactly the missing machinery.

### SCOPE vs current bot

**ACTIVE (we have it):**
- 12-pt confluence scoring live + 16-pt shadow (log-only). Static weights (`config.SCORE_WEIGHTS`).
- Kelly sizing — the ONE parameter that already updates from realized trade outcomes.
- Shadow Strategy Tracker: delta-of-signal, volume-confirmation, 16-pt — logged-but-not-live with
  sample-count flip triggers (CLAUDE.md). GEX / TSMOM staged-active.
- Reactive dynamism: VIX continuous stop curve, MRI, 9-layer dynamic floor, session-P&L feedback.
- Single-pass backtests (`backtest_12pt`, T1-compliant).

**PARTIAL (proto, but not the article's rigor):**
- Shadow framework = a proto-loop: it collects candidates + has flip criteria, but the criteria are
  ad-hoc sample thresholds (e.g. "≥50 delta samples", "≥30 vol samples", "1.5× vol"), NOT IC/ICIR.
  It is "generate + log", missing "score → OOS-gate → feed back".
- 16-pt shadow scoring = raw material for factor analysis, but per the roadmap "never analyzed — no
  pipeline to detect stale signals."
- LdP CPCV / PBO are *referenced* in shadow flip language, but not implemented as an engine.

**MISSING (the article surfaces — none of this is built):**
- **IC/ICIR measurement per factor.** The bot has zero information-coefficient computation. Roadmap
  "Alpha Decay & Walk-Forward Validation" (S59) explicitly calls for "IC monitoring per factor" — not
  built.
- **Factor half-life / decay measurement** (AR(1) persistence). Roadmap estimates 20–40% edge
  evaporation over 3–6 months but has no measurement to detect it.
- **Out-of-sample validation gate.** No held-out ICIR gate for weight/factor changes. Roadmap's
  `research/walk_forward_optimizer.py` is a NEW (unbuilt) file. This is the article's single most
  important step.
- **Automated loop orchestrator** — generate variants → score in-sample → OOS-gate → surface board-
  approvable parameter-update proposals. Today this is manual per session.
- **Multiple-testing discipline** — no "raise the bar as attempts grow" (deflated Sharpe / PBO), so a
  shadow could be flipped live on noise from many comparisons.

### KEY INTEGRATIONS this surfaces (prioritized by value ÷ effort — for BGG triage)
1. **IC/ICIR engine (HIGH value / MED effort).** Compute IC of each confluence component (the 12/16
   score factors) vs forward returns, from `trade_events.jsonl` + the shadow logs; rank by ICIR.
   Replaces ad-hoc shadow flip thresholds with a rigorous, comparable metric. New: `research/ic_engine.py`.
   → Directly upgrades the Shadow Strategy Tracker's flip decisions.
2. **OOS / walk-forward gate (HIGH value / MED-HIGH effort)** — the roadmap's `walk_forward_optimizer.py`.
   Every proposed weight change gated on held-out ICIR + PBO. THE anti-overfit step. Board vote required
   before any weight change (Architecture Invariant #1).
3. **Factor half-life monitor (MED value / LOW-MED effort).** AR(1) half-life per factor; flag/ downweight
   short-half-life signals. Cheap add-on to (1); feeds the "static weights go stale" gap.
4. **Loop orchestrator as a standing weekly job (HIGH value / HIGH effort).** Ties 1–3 into a cadence:
   propose → score → OOS-gate → emit board-approvable parameter-update proposals. This IS the
   "walk-forward/IC recalibration engine" already on the S59 roadmap + the Evolution Mandate.
5. **Multiple-testing discipline (MED value / LOW effort as a guard on 1–2).** Deflated-Sharpe / PBO so
   shadows never flip live on noise.

### NON-GOALS / caveats (be honest)
- We are a small paper account; the loop must respect our data volume (few hundred trades) — IC on
  ~158 entry-level trades is itself noisy, so bootstrap/CI + PBO matter MORE for us, not less.
- Article is vendor content (Horizon waitlist). The *framework* (IC/ICIR/half-life/OOS) is standard
  quant canon (Grinold-Kahn IC, LdP CPCV/PBO, Jegadeesh-Titman) — adopt the canon, ignore the pitch.
- Architecture Invariant #1 stands: SPY 5-min bar-over-bar remains the sole entry gate; any factor the
  loop validates only ever adjusts the quality bar / weights, via a board vote.

**BGG STATUS:** scope drafted 2026-07-12; SUBMITTED to BGG for prioritization read (not yet a build).
Recommended first build = (1) IC/ICIR engine (unlocks 2–5). Ranks against the existing roadmap
whitespace items (TCA, correlation aggregator, adaptive MIN_SCORE) — this is the "learning loop" the
Evolution Mandate specifically called out as the bot's biggest structural gap.

**BGG READ (2026-07-12):**
- **GAI:** build **IC/ICIR measurement FIRST** — "without it you cannot validate if your confluence
  scoring or shadow strategy are generating any signal, making all other components potentially
  useless." #1 risk = **overfitting / noise-masking**: ~158 trades are insufficient to statistically
  separate genuine alpha from chance, especially across a 12-factor score.
- **Gro:** call errored (shell-escaping bug in the harness prompt, not a real reject) — deferred per
  Gro-skip; a prioritization read, not a ship gate.
- **Board framework grounding (already in scope):** Grinold-Kahn (IC/breadth), LdP (CPCV/PBO — the
  small-sample discipline GAI flags), Jegadeesh-Titman (momentum factor). Consensus with GAI: **(1)
  IC/ICIR first, and it MUST ship with bootstrap-CI + PBO from day one** because our sample is tiny —
  the deflated-Sharpe/multiple-testing guard (#5) is not optional here, it is co-required with (1).
- **NEXT:** Rafael to decide whether to graduate item (1) into a Feature-Design + full-board build,
  ranked against the S59 roadmap whitespace. No build started — scope only, as requested.

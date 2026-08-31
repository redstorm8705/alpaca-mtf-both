# GEX Regime Artifact + Weekly Levels — BGGN design alignment (2026-08-30)

**Status:** DESIGN ALIGNMENT (no code shipped). Surfaced from Rafael's question: "are these the GEX
levels we have, can we pull them Sunday night, should it be an automated Sunday-8pm report?" Recorded
per the DURABLE SYNC RULE (a BGG alignment triggers the sync even with zero code). Build runs the full
gate and depends on shipping the held `get_gex_levels` (day-tier Track-A prerequisite).

## Verified facts (measured live 2026-08-30, not assumed)
- **Weekend OI IS available** via Alpaca: near-money Mag-7 contracts return `open_interest` tagged
  `open_interest_date` = prior trading day, static until Monday open. So a weekend/pre-market GEX
  compute is time-valid (GEX for Monday is inherently a Friday-close computation). Verify:
  `get_option_contracts(underlying_symbols="AAPL", ...)` → every contract carries `open_interest`.
- **What we produce today:** `data/gex.py get_gex_regime()` computes regime (POSITIVE/NEGATIVE/
  NEAR-FLIP/UNKNOWN) + net GEX ($M) + flip strike for **SPY and QQQ only** (see any
  `logs/gex_daily_audit_*.json` `label_distribution`). Per-name pin/call-wall/put-wall **levels**
  (`get_gex_levels`) are BUILT but HELD (`scratchpad/gex_levels.diff`), unshipped. Mag-7 GEX universe
  (#203 `fb8b42f`) is deployed-but-unexercised (first compute Mon RTH).
- **Accuracy spot-check** of an external Mag-7 GEX post for 8/31: Fri-close spots AAPL 319.92 / NVDA
  217.55 / TSLA 348.18; AAPL heaviest near-term call OI at 320 (12,408 @ 9/4) = pin, 325 (7,233) = next
  wall — brackets the post's "320 pivot, 325/330 resistance" correctly. Post is a competent real GEX read.

## BGGN convergence (Gro + GAI + 2 cold board seats — NO split)
| Voice | Verdict |
|---|---|
| Gro (HFT eng) | Build the FEED not the report; wire into Track-A; Monday pre-market; fail-safe gates; human view = 1-line Slack only |
| GAI (quant eng) | Kill static report; expose as dynamic regime signal into Track-A; relabel walls (anti-anchor); stale-OI hash gate |
| Board — Sosnoff/Sinclair/Nathan + Harris | Build the compute, kill the report; **edge is in the gamma REGIME + flip, not the precise pin**; SPY/QQQ first-class, Mag-7 on a shorter leash; Monday pre-market; fail-CLOSED |
| Board — Taleb/Douglas | Build a DIFFERENT form; keep the regime (convex, growth-relevant), kill naked "support/resistance" levels — anchoring trap that imports the capital-preservation reflex the charter warns against |

**Agreed substance:**
1. The compute is worth it AND is already on the day-tier Track-A path (anti-silo; not net-new scope).
2. A static "support/resistance" PDF is the WRONG primary artifact (latency + anchoring + fragility).
3. **Regime sign gates everything:** in NEGATIVE gamma the walls ACCELERATE the move, they don't hold —
   treating a put wall as deterministic support and buying it at size is the single biggest failure mode.
4. Bad/illiquid/garbage data **fails CLOSED (no trade)**, never defaults to a level (matches the bot's
   UNKNOWN→neutral doctrine). Fail-safes named: stale-OI hash check (Hash(OI_t)==Hash(OI_t-1)→INVALID),
   liquidity floor, concentration-ratio pin-risk gate, pin-sanity bounds vs near-money strip.
5. Timing: **Monday pre-market** authoritative (OCC settles Saturday; gamma re-strikes vs fresher
   Monday spot/DTE). Sunday-8pm PT is fine for a *human preview* (OI already settled by then).

## Aligned direction (the recorded recommendation — proceed on this at build time)
- **(A) THE EDGE — wire GEX regime→Track-A** (fade in positive gamma / ride in negative; size-down near
  walls; fail-closed). Already on the day-tier critical path; requires shipping the held `get_gex_levels`
  + Monday's Mag-7 run. This is where the P&L is.
- **(B) RAFAEL'S VISIBILITY — a lean Slack GEX-REGIME summary** (Sunday-night preview + Monday
  pre-market authoritative), **regime-first**, labeled *probabilistic bias, not support/resistance*
  (anti-anchoring: "probabilistic magnet / higher-friction zone", never "wall/floor"; attach a
  confidence/hit-rate; self-invalidating after a big move or scheduled event). Small, the one net-new
  piece. This is the element Rafael may confirm or kill at build time (the board leaned against any
  human-read levels; Rafael's CEO-visibility need is the counterweight — his call).
- **NOT** a heavy static Sunday PDF of tradeable levels.

## Build sequencing (all gated)
1. Ship held `get_gex_levels` (also unblocks day-tier Track-A) — full gate.
2. Let Monday's Mag-7 universe run populate real per-name levels; validate against the external post as a sanity check.
3. Wire regime→Track-A direction+sizing with fail-closed guard (risk-path → BGGN + masked-loss seat).
4. (B) the regime summary emitter → Slack (folds into the Slack overhaul WS2/WS3 formatting).
Cross-refs: `day_tier_v2_design_2026-08-29.md` (Track-A GEX core), `slack_messaging_overhaul_2026-08-19.md` (WS2/WS3).

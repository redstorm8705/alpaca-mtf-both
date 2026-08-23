# QUEUE (major architectural build) — Catalyst/Fundamental Research Swarm (Rafael 2026-08-21)
**Source:** industry article "Build an AI Hedge Fund Desk … That Hunts Alpha While You Sleep" (6-bot Grok Bot
research desk). Rafael: add to the queue as a major architectural build; wants summary + what's missing + where
it optimizes + BGG consensus. **This is a QUEUE/scoping record — NOT a decision to build now.**

## SUMMARY OF THE CONCEPT
A swarm of 6 named bots on a shared persistent filesystem, each owning one RESEARCH vertical, handing off
overnight, synthesized into a 6 AM morning brief: (1) Filings Analyst (SEC EDGAR 10K/8K/13F/Form4, material
changes), (2) Earnings Analyst (transcripts, EPS/rev-vs-consensus, guidance, Loughran-McDonald tone), (3)
Sector Research, (4) Sentiment Analyst (X/Reddit/StockTwits mention-volume 3σ), (5) Insider Tracker (Form-4
cluster buys; Cohen-Malloy-Pomorski 5.3% alpha; 13F), (6) Coordinator (maker-checker: grades the 5, HIGH
CONVICTION needs 2+ bot confirmation → brief). Self-improving (false positives rewrite each bot's prompt);
verifiable stopping conditions ("filing exists at URL", not "the bot says it ran"). It is a **RESEARCH /
signal-generation layer** — the article itself frames it as the research HALF that pairs with a separate
execution system.

## WHAT'S MISSING FROM OUR BOT (gap — our bot is purely technical/price-driven)
Our `alpaca-mtf-bot` EXECUTES on 12-pt MTF confluence (SPY 5-min gate) + MRI + GEX + QHM. It has news_monitor
(RSS headlines) but has **none** of: SEC-filings reader; earnings-transcript/EPS-surprise; insider/Form-4/13F
tracking; social-sentiment mention-volume; per-sector research; a forward-looking research-synthesis morning
brief. It has zero FUNDAMENTAL/CATALYST signal — an entire orthogonal alpha source it is blind to. (It DOES
already have a maker-checker: the nightly/midday Gemini audits + the Gro+GAI meta-audit.)

## BGG CONSENSUS
**Gro + GAI (both returned, strongly convergent):**
- **#1, ship-first: SEC EDGAR Form-4 OPPORTUNISTIC insider CLUSTER buys** (Cohen-Malloy-Pomorski 2012, 5.3–8.2%
  4-factor alpha on opportunistic-vs-routine; cluster = 3+ insiders/30d). **FREE** (EDGAR JSON/RSS),
  deterministic parse (10b5-1 flag to exclude routine), LOW effort, LOW collinearity with our price signal
  (r≈0.12–0.25) → genuinely additive. File: `signals/insider_catalyst.py`.
- **#2: 8-K material-event drift** (Griffin-Hirschey-Tang; 1.5–3.2% 5-day CAR, under-reaction; distinct from
  earnings). FREE EDGAR; filter Item 1.01/1.02/4.02/5.02; micro LLM cost on item text. File:
  `data/edgar_feed.py` + `signals/event_drift.py`.
- **#3: PEAD / earnings-surprise (SUE)** — FMP free-tier calendar/surprise; pure quantitative, medium effort.
- **DEPRIORITIZE / negative-ROI for us:** social-sentiment (X native = Grok-only, no access; Reddit noisy,
  bot-spam) and full sector research (low signal/noise on our caps). Transcript LM-tone = paywalled, high cost.
- **HARD REQUIREMENT (both):** keep the research swarm **OUT of the execution risk-path** — separate process,
  file-based read-only contract (`high_conviction.json`), execution reads a SNAPSHOT at bar-top and ignores any
  stale/older-than-bar file; on missing file → fall back to the existing MTF score. No blocking research call in
  the run_cycle loop. This maps directly to our anti-silo rule (adds-signal / fails-safe / stays-testable).
**Board (framework-grounded; full cold vote owed when the build is undertaken):** Simons (BoD) — a
catalyst/fundamental layer is ORTHOGONAL to our technical edge → additive, not collinear. Thorp/LdP (AB) — the
cited signals are academically validated but require OUR-OWN out-of-sample (CPCV) validation before they size
anything → start LOG-ONLY/SHADOW, never straight into sizing. Taleb (BoD) — a FULL 6-bot desk is over-scoped
for a $2.5K account + our current execution/reporting-stability priorities → phase the free high-ROI subset,
isolate from the risk envelope.

## OPTIMIZATION / PHASED ROADMAP (for a $2.5K aggressive-growth paper account)
Build on OUR infra (OCI + Claude/Gro/GAI), NOT SuperGrok. Ship as a research layer feeding the WATCHLIST /
confluence as an ADDITIVE bonus (shadow-first), never a straight-to-sizing gate:
- **Phase 1 (ship first):** `signals/insider_catalyst.py` — nightly EDGAR Form-4 opportunistic-cluster detector
  → writes `data/insider_catalyst.json` (read-only for execution); shadow-log its would-be confluence bonus.
- **Phase 2:** 8-K material-event classifier (`edgar_feed.py`).
- **Phase 3:** PEAD/SUE from FMP.
- **Phase 4 (optional):** a Coordinator/brief synthesizer that cross-confirms these + emits a morning brief +
  a HIGH-CONVICTION watchlist (2+ source confirmation). Isolation + fail-safe + shadow-validation gates on each.
**Prerequisite gate:** Feature Design Protocol + full board + Gro + GAI before any of it sizes a real trade.

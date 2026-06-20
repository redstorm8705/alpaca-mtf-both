# Quarterly Holds Research — Q3 2026
**Prepared:** 2026-06-20 CCR session | **Requested:** S47f (carries forward per schedule)
**Status:** COMPLETE — board vote done, ready for Rafael review
**DS/GAI:** NOT available this session (no .env in CCR). Integration code changes require DS/GAI in next in-person session.

---

## Executive Summary

3 confirmed S&P 500 quarterly holds for Q3 2026. Entry window opens in 17 days (GS July 7, revised to July 15 post-earnings per board; GEV July 22; LLY August 7). Board unanimous top 2 if account constrained: **GE + LLY**.

| Rank | Ticker | Company | Last Earnings | Next Earnings | Entry Window | Current Price | Board Score |
|------|--------|---------|--------------|--------------|-------------|---------------|-------------|
| 1 | **GE** | GE Aerospace | Apr 21, 2026 | **Jul 16, 2026** | Day 3 = Jul 25 | ~$360 (ATH $364) | 8.0/10 |
| 2 | **LLY** | Eli Lilly | Apr 30, 2026 | **Aug 5, 2026** | Day 3 = Aug 7 | ~$1,098 | 8.25/10 |
| 3 | **GEV** | GE Vernova | Apr 22, 2026 | **Jul 21, 2026** | Day 1+ = Jul 22 | ~$1,113 | 7.75/10 |
| ⚠️ | **GS** | Goldman Sachs | Apr 13, 2026 | **Jul 14, 2026** | ~~Jul 7–14~~ → **Jul 15+** | ~$1,097 (52-wk high) | 7.25/10 |

**CRITICAL FLAG: GS entry window revised.** Original window (Jul 7–14) ends ON earnings day. 4-board unanimous consensus: do not enter pre-earnings at 52-wk high. Enter July 15+ post-earnings only.

---

## Status vs. S47f Research

The June 4 S47f research identified AVBO/NVDA/ANET as Q2 2026 picks. This memo is the Q3 successor.
Q3 picks (LLY/GE/GEV/GS) were board-approved in a prior CCR session and are reflected in `quarterly_holds_research_2026-07-07.md` (sparse stub). This memo provides full thesis, updated earnings data, and board vote.

---

## Pending Approvals Summary

| File | Status | Action Needed |
|------|--------|---------------|
| `logs/pending_approvals_2026-06-07.md` — all 4 items | CLOSED STALE (confirmed S58c/S59) | None |
| `logs/queued_for_review_2026-06-16.md` — exit_logic.py T1 tranche | BLOCKED — 3 Rafael decisions | See §Pending Decisions for Rafael |
| `logs/queued_for_review_2026-06-02.md` — trade_engine.py risk.open_positions | BLOCKED — board fail (double-increment) | Needs redesign, then board vote |
| `logs/queued_for_review_2026-06-01.md` — reconcile_eod.py RC-3 | BLOCKED — board fail (detection aid, not fix) | Needs redesign |
| `logs/queued_for_review_2026-05-28.md` — scan_to_html.py T4 violation | BLOCKED — DS/GAI required | Next in-person session |

**3 decisions needed from Rafael on exit_logic.py T1 tranche** (from queued_for_review_2026-06-16.md):
1. Is enabling trail ACTIVATION at new T1 a "forbidden stop-loss calculation change" or allowed activation logic?
2. T3 silent skip for qty_orig=3 — acceptable (check_exits handles close) or fix required?
3. DS/GAI sign-off pending per RULE C-2 — authorize in next in-person session?

---

## Candidate Analysis

### 1. GE Aerospace (GE) — HIGHEST CONVICTION

**Sector:** Aerospace & Defense | **Entry Window:** Day 3 after Jul 16 earnings = **Jul 21–25**
(Note: Jul 4 holiday. Day 1 = Jul 17, Day 2 = Jul 18, Day 3 = Jul 21)
**Current Price:** ~$360 | **All-Time High:** $364.70 (June 18, 2026)

#### Q1 2026 Earnings (April 21, 2026)
- **Revenue:** $12.4B GAAP (+25% YoY), $11.6B adj (+29%)
- **EPS:** $1.86 vs. $1.60 estimate — **beat +16.3%**
- **Orders:** $23.0B (+87% YoY) — exceptional
- **Services:** +39% YoY | Spare parts revenue +25%+ | Shop visits +35%
- **FY2026 guidance:** Maintained (not raised). Adj. rev growth ~21%, op. profit $9.85B–$10.25B, EPS $7.10–$7.40

**Key quote (earnings call):** "Our order momentum reflects multi-year fleet expansion and engine service cycle demand that we believe is durable through the decade."

#### Structural Tailwind
GE Aerospace's LEAP engine series powers the Boeing 737 MAX and Airbus A320neo — collectively 80%+ of narrow-body deliveries. Services revenue (maintenance, repair, parts) is contracted 10–15 years forward. The +87% order growth locks in 3-5 years of revenue visibility:
- Defense spending globally accelerating (NATO 2% GDP commitments, U.S. defense budget growth)
- Commercial aviation recovery: airline fleets aging, replacement cycle underway
- Sole-source on LEAP engine: no competitive threat to services revenue

#### 13F / Institutional Data
- Institutional ownership: 74.77%
- GQG Partners: Added $1.6B worth (8M shares) in Q1 2025 — major conviction bet
- Major holders: Vanguard, T. Rowe Price, Geode, Norges Bank — all long-term institutional grade
- Net: 1,311 institutions adding vs. 987 reducing over prior 8 quarters

#### Risk Factors
- FY2026 guidance not raised (maintained) — signals management caution despite beat
- Boeing production risk: GE is sole-source on LEAP; any Boeing halt = near-term parts/service revenue drag
- "Dynamic geopolitical landscape" cited by management — U.S./China export control risk on engine technology

#### Board R:R
- Entry: ~$360 (post-earnings, Day 3)
- Target: $420–440 (12-week, Thorp/Asness)
- Stop: $325 (Thorp: -9.7%)
- **R:R: 1.72:1 — best of the 4 picks** (Thorp)

---

### 2. Eli Lilly (LLY) — SECOND HIGHEST CONVICTION

**Sector:** Healthcare / Pharmaceuticals | **Entry Window:** Day 3 after Aug 5 earnings = **Aug 7, 2026**
**Current Price:** ~$1,098 | **52-week range:** $623–$1,183

#### Q1 2026 Earnings (April 30, 2026)
- **Revenue:** $19.8B (+56% YoY) — massive beat
- **EPS:** $8.55 adjusted vs. $6.66 estimate — **beat +28.4%**
- **Mounjaro (diabetes):** $8.66B worldwide (+125% YoY)
- **Zepbound (obesity):** $4.16B U.S. (+80% YoY)
- **Combined GLP-1:** $12.8B — added $6.7B YoY growth in a single quarter
- **FY2026 guidance RAISED:** $82B–$85B revenue (from $80B–$83B), EPS $35.50–$37.00

**Key quote (earnings call):** "Lilly holds 60.1% of the U.S. obesity and diabetes market. With Foundayo now approved and shipping, we are entering the oral GLP-1 era from a position of market leadership."

#### Structural Tailwind
GLP-1 drugs are the most significant pharmaceutical franchise since statins:
- 70% of Americans are overweight/obese — addressable market is enormous
- Mounjaro+Zepbound: injectable GLP-1 dominance → growing market, growing share
- **Foundayo (orforglipron):** FDA-approved April 1, 2026 — first GLP-1 pill without food/water restriction. Launches Q2 2026. Analyst estimates: $146M Q2, $1.6B FY2026, $40B+ peak sales
- Market share holding at 60% vs. Novo Nordisk 39.4%

#### Competitive Risk Note (Foundayo Launch)
- Foundayo tracking below Novo Nordisk's Wegovy pill (oral semaglutide) in week 7 of launch
- Novo's oral GLP-1 described as "more effective" in some clinical trial comparisons
- Foundayo FY2026 estimates: $1.5B–$2.8B range (Guggenheim to Citi) — wide uncertainty
- **BUT:** Injectable franchise (Mounjaro+Zepbound) is still dominant. Oral is additive, not the core thesis.
- Risk is narrative risk if Foundayo underperforms — not fundamental risk to the core business

#### 13F / Institutional Data
- 4,436 institutional owners, ~791.7M shares held
- Top holders: Lilly Endowment (~11%), Vanguard, BlackRock, PNC, State Street, FMR (Fidelity)
- Analyst consensus: 24 BUY, 2 SELL. Average PT: $1,215 (vs. current $1,098 = +10.6% upside)

#### Risk Factors
- Foundayo oral launch underperforming Novo's oral GLP-1 (week 7 data below Wegovy pace)
- Novo Nordisk competition intensifying in the oral GLP-1 segment
- Regulatory: CMS drug pricing negotiations under IRA could cap Mounjaro/Zepbound pricing
- Stock below prior high ($1,183) — suggests some unresolved selling pressure

#### Board R:R
- Entry: ~$1,098 (Day 3 post-Aug 5 earnings)
- Target: $1,250 (Thorp, 12-week); $1,215 analyst consensus
- Stop: $980 (Thorp: -10.7%)
- **R:R: 1.29:1** (Thorp) — lowest of four, but macro defensiveness and secular duration compensate

---

### 3. GE Vernova (GEV) — THIRD PICK (conditional on GE+GEV correlation acknowledgment)

**Sector:** Power Infrastructure / Energy Transition | **Entry Window:** Day 1+ after Jul 21 earnings = **Jul 22–29**
**Current Price:** ~$1,113 | **52-week high:** ~$1,118 (June 19, 2026)
**Bernstein PT:** $1,206 (Outperform, explicit AI data center rationale)

#### Q1 2026 Earnings (April 22, 2026)
- **Revenue:** $9.34B (beat)
- **EPS:** $17.44 vs. $1.67 estimate — **massive beat** (note: large non-recurring items likely; verify normalized EPS)
- **Orders:** $18.3B (+71% YoY) | Book-to-bill ~2x
- **Backlog:** $163B (from $116B) — +$13B sequential, +40% YoY
- **Equipment backlog:** +80% at better margins
- **EBITDA:** $896M (+87% YoY)
- **2026 guidance RAISED:** Rev $44.5B–$45.5B, EBITDA 12%–14%, FCF $6.5B–$7.5B
- **Wind segment:** EBITDA loss $382M (ongoing drag)

**Key quote (earnings call):** "AI data centers are the fastest-growing source of power demand we have ever seen. Our gas turbine, transformer, and grid solutions backlogs reflect utilities planning for 10+ years of accelerating load growth."

#### Structural Tailwind
GEV sits at the intersection of two mega-themes: AI data center power consumption and the energy transition:
- AI data centers require massive, reliable power → gas turbines + grid infrastructure = GEV's core
- U.S. grid is aging and requires $4.5T+ in upgrades through 2035 (DOE estimate)
- Utilities are ordering years in advance (equipment backlog +80%)
- Electrification segment: nearly tripled North America/Asia orders in Q1 2026
- GEV tariff impact: $250–350M in 2026 (managed, manageable)

#### CRITICAL CORRELATION NOTE (BoD board unanimous)
**GE Aerospace (GE) + GE Vernova (GEV) must be treated as ONE POSITION for risk purposes.**
- Shared institutional holders: Vanguard, BlackRock, State Street all held both from GE spinoff
- Shared ETF inclusion (XLI — industrial ETFs)
- Overlapping sell-side analyst coverage
- In a tail event (sector selloff, GE heritage news), both fall simultaneously
- BoD board: "Treat GE + GEV as one position. Effective portfolio is 3 uncorrelated bets, not 4."

If Rafael holds both GE and GEV, the portfolio has: GS + LLY + {GE/GEV as one} = 3 positions, effectively.

#### 13F / Institutional Data
- 2,911 institutional owners, 209M shares held
- Q1 2026: 1,569 institutions adding vs. 995 reducing — net buyer trend
- Capital World Investors +95.4% addition; Amundi +256.6% — significant conviction buying
- Coatue Management: -23.7% reduction (one of the few major sellers)

#### Risk Factors
- Wind segment ongoing losses (-$382M EBITDA Q1, guided -$200M to -$300M Q2)
- Tariff impact $250M–$350M 2026
- Infrastructure regulatory risk (FERC approvals, state PUC timelines)
- Interest rate sensitivity: power infrastructure = long-duration asset; 10Y +50bps compresses NPV
- Correlation risk with GE Aerospace (treat as one position)

#### Board R:R
- Entry: ~$1,113 (Day 1 post-Jul 21 earnings)
- Target: $1,300–$1,350 (Thorp/Asness; Bernstein PT $1,206)
- Stop: $980 (Thorp: -11.9%)
- **R:R: 1.41:1** (Thorp); Asness widens target to $1,350 → 1.86:1

---

### 4. Goldman Sachs (GS) — ENTRY TIMING REVISED (post-July 14 only)

**Sector:** Financial Services | **REVISED Entry Window:** **July 15, 2026** (one day after Q2 earnings)
**Current Price:** ~$1,097 | **52-week high:** $1,098.36 (June 5, 2026)
**YTD Performance:** +16.3% | **YoY Performance:** +73.72%

#### Q1 2026 Earnings (April 13, 2026)
- **Revenue:** $17.23B (+21% YoY)
- **EPS:** $17.55 vs. $16.49 estimate — **beat +6.3%** — second highest in GS history
- **Global Banking & Markets:** Record $12.7B | Advisory +89% YoY
- **Equities:** $5.3B — record quarterly revenue
- **AUM:** $3.7T — record high | LT fee-based inflows: $62B
- **ROE:** 19.8% | **ROTE:** 21.3%
- **Q2 2026 guidance:** $13.75 EPS / $15.71B revenue (significant step-down from Q1)

#### Structural Tailwind
- **SpaceX IPO catalyst (Q2 2026):** GS was lead underwriter on SpaceX $75B IPO — largest in U.S. history. Advisory fee revenue should substantially boost Q2 actuals vs. $13.75 guidance.
- **M&A renaissance:** CEO David Solomon predicted "dealmaking renaissance" driven by deregulation, PE dry powder, stabilizing rates. Advisory +89% YoY in Q1 confirms thesis.
- **Deregulation tailwind:** Banking deregulation = lower compliance costs, wider trading books, more M&A activity
- **AUM momentum:** $3.7T AUM with $62B inflows — asset-light recurring fee revenue growing

#### Why Q2 Guidance Step-Down Is Misleading
Q1 EPS of $17.55 included Equities at record $5.3B (markets-sensitive). Q2 guidance of $13.75 reflects:
- Seasonal trading volume reduction
- Q1 record was partially driven by January volatility spike
BUT: SpaceX advisory fees were NOT in Q1 — they are a Q2 event. If SpaceX alone generates $300–500M in advisory fees, Q2 EPS likely beats $13.75 meaningfully.

#### PRE-EARNINGS ENTRY REJECTED — 4-BOARD CONSENSUS
- **Analytics Board (Thorp/Dalio/Asness/Brandt): 4-0 WAIT.** SpaceX thesis is already priced at $1,097. Even on a beat, stock may sell-the-news from highs. Wait for July 15 confirmation.
- **BoD (Simons/Taleb/Kyle/Shaw): 4-0 REJECT.** Kyle: "Informed trading pre-announcement means PEAD mechanism is weakest here — price discovery already advanced." Taleb: "Concave payoff at 52-wk high = do not enter pre-earnings."
- **Execution (Harris/Levitt): 2-0 WAIT.** Harris: "At 52-wk highs near earnings, order book is adversely selected — informed traders already positioned."

**Revised action: Enter July 15 if Q2 earnings beat. Do NOT enter July 7–13.**

#### 13F / Institutional Data
- GS is widely held by all major institutional owners
- GS is a lead manager for SpaceX IPO (public, Q2 event)
- Druckenmiller: SOLD GS shares and rotated to AVBO in Q1 2026 — slight negative signal
- Q1 2026 guidance step-down to $13.75 EPS caused some sell-side PT revisions
- Analysts: Some flagging "17% overvalued" at current levels

#### Risk Factors
- Stock at 52-week high with Q2 EPS guidance DOWN (-21% from Q1)
- Binary event risk: if GS misses $13.75 guidance or gives weak Q3 guide, stock gaps down from ATH
- M&A pipeline can stall: tariffs, geopolitical uncertainty, delayed Fed rate cuts
- Credit cycle: commercial real estate, leveraged loans could weigh on advisory pipeline
- Druckenmiller rotation OUT of GS is a soft negative signal

#### Board R:R (post-earnings entry July 15)
- Entry: ~$1,097 (post-earnings, Day 1)
- Target: $1,200 (Thorp, 12-week)
- Stop: $1,020 (Thorp: -7.0%)
- **R:R: 1.34:1** (Thorp)

---

## Board Vote Summary

### 4-Board Consensus Table

| Question | Analytics Board | Board of Directors | Execution Board | Technical Board |
|----------|----------------|-------------------|-----------------|-----------------|
| Top 2 picks | **GE, LLY** (3-1; Brandt: GE/GEV) | **LLY, GE** (4-0) | Not asked | See below |
| GS pre-earnings entry | 4-0 WAIT (post Jul 15) | 4-0 REJECT | 2-0 WAIT | N/A |
| GE + GEV correlation | Treat with caution | **One position** (4-0) | Exclusion list | Architecture concern |
| Stop design | — | — | B+D: 2x ATR trail, cancel pre-earnings | GTC + cancel/resubmit |
| Sizing | Max 2 holds, 15% each | Cap at 40-50% equity | Max 2 holds, 15% each | See below |
| Bot conflict resolution | — | — | Exclusion list in config | See below |

---

### Analytics Board (Thorp, Dalio, Asness, Brandt)

**Fundamental scores:** LLY 8.25 > GE 8.0 > GEV 7.75 > GS 7.25

**R:R ranking:** GE (1.72:1) > GEV (1.41:1) > GS (1.34:1) > LLY (1.29:1)
- GE is the clearest technical breakout (ATH) with the best risk/reward
- LLY scores highest fundamentally but lowest R:R — secular growth compensates
- GEV has good R:R but needs pullback to $1,050 for cleaner entry (Brandt)
- GS entry timing complication reduces near-term edge

**Kelly note (Thorp):** Theoretical Kelly = 15-16% per position (half-Kelly). At $2,500 with stocks priced $360–$1,113, sub-share sizing makes formal Kelly inapplicable. Accept the constraint; pick 2 names maximum.

**Top 2 if constrained:** GE (#1 unanimous), LLY (#2 by 3-1; Brandt prefers GEV)

---

### Board of Directors (Simons, Taleb, Kyle, Shaw)

**Alpha confirmation:** PEAD is real but compressed at mega-cap scale. All 4 picks clear the threshold (≥15% earnings surprise, raised guidance, sector momentum alignment). Real alpha, not data mining.

**Tail risk ranking:** GEV > LLY > GS > GE Aerospace
- GEV: Wind segment losses + interest rate sensitivity + policy/regulatory tail
- LLY: GLP-1 market concentration risk (60% share invites regulatory scrutiny and competitive response)
- GS: Credit cycle exposure; SpaceX fee is one-time
- GE: Lowest tail risk — multi-year services contracts are counter-cyclical

**GS pre-earnings verdict (Taleb):** "Concave payoff. Stock at 52-wk high with limited upside (already priced) and uncapped downside (miss + Q3 guide cut). This is the exact structure Fooled by Randomness warns against."

**Overnight budget:** Spirit of 100% rule is triggered. Cap quarterly anchors at 40-50% of $2,500 account = max 2 positions at $375-500 each (paper margin permitted for more, but not recommended).

**Top 2:** LLY + GE Aerospace (4-0 unanimous)

---

### Execution Board (Harris, Levitt)

**GS timing:** 2-0 WAIT. Harris: "At 52-wk highs near earnings, order book is adversely selected. Paying the spread is paying informed traders to leave."

**Day 3 entry rule:** CONFIRMED. Count trading days only. GE: Jul 16 report → Day 3 = Jul 21 (accounting for Jul 17, 18, 21 — note: Jul 4 independence day is Jul 4, GE reports Jul 16 so no holiday conflict). GEV: Jul 21 report → Jul 22 entry. LLY: Aug 5 report → Aug 7 entry.

**Stop design recommendation:** Option B + D combined:
- No stop for first 48 hours (price discovery)
- Then: 2x ATR trailing stop, activated when position is +10% profitable
- Cancel GTC stop 30 minutes before earnings announcement
- Re-submit stop after post-earnings open stabilizes (30-60 min post-open)
- Hard -15% stop as absolute floor regardless of trail status

**Position sizing:** Max 2 quarterly holds at $2,500. 15% each = ~$375. At current prices, this means 1 share of GE ($360) and 1 share of LLY ($1,098) — LLY exceeds 15% at one share. Harris: "Capital allocation conflicts between strategies in the same account destroy the edge of both."

**Bot conflict:** Add quarterly anchor symbols to intraday bot exclusion list in config. Do NOT run concurrent intraday short + quarterly long in same symbol. Implement as config flag, not runtime logic.

---

### Technical Board (McKinney, Beck, Derman, Minsky) — COMPLETE

**Concern 1: Same-symbol conflict (intraday bot fires on QHM-held symbol)**
4-0: `get_quarterly_hold_symbols()` frozenset already exists in QHM. Wire it into `entry_logic.py` scan loop as hard exclusion. Add Beck Test 3 to verify exclusion. McKinney: frozenset is the correct type for cross-module shared state. Derman: block is a Kelly independence requirement, not optional. **APPROVE the existing design; wire the import.**

**Concern 2: risk.register_open() — CYCLE-SYNC-GUARD**
4-0: P0-STARTUP block in `main.py` auto-handles `risk.open_positions` via Alpaca count — no new QHM-specific registration code needed. **BUT: CYCLE-SYNC-GUARD must be patched** to exclude QHM symbols from the tracker comparison count (otherwise CRITICAL log fires every cycle for QHM positions, training operators to ignore alerts). Beck: "This is a safety property degradation." Fix: `_tracker_open_count = sum(1 for t ... if t.get("status") != "closed" and symbol not in get_quarterly_hold_symbols())`. This is a **blocking prerequisite before first QHM entry.**

**Concern 3: tracker.record_exit() — should QHM exits feed Kelly stats?**
4-0: QHM already exits via `broker.close_position()` directly — NOT through `tracker.record_exit()`. This is intentional (documented in QHM as RC-4: N/A). Derman: "Mixing quarterly R-multiples (15% wide stop) with intraday R-multiples (1.25×ATR stop) shifts `avg_loss_r` and destroys Kelly calibration." **QHM must NOT feed `kelly.record_trade()`. Add `trade_events.jsonl` write on QHM external close for observability (Minsky).**

**Concern 4: Stop loss design — GTC + earnings gap risk**
4-0: Add `PENDING_EARNINGS` state to `HoldState` enum. Cancel GTC stop the day before earnings (not 5 min pre-announcement — cancel at close-1-day). Resubmit next morning at post-earnings price. The FMP calendar is already injected at QHM construction — use it. Beck: "Current state machine is incomplete — all four Q3 picks have earnings during their hold windows." **This is a P0 gap. `PENDING_EARNINGS` state is required before first earnings-window entry.**

**Concern 5: PDT/Kelly interaction**
4-0: No code change needed. QHM exits are isolated from `kelly.record_trade()`. Total equity denominator (from Alpaca) is correct — includes QHM unrealized P&L. `_get_quarterly_notional_excl()` already handles QHM-internal tranche sizing. No contamination path.

**Concern 6: Integration priority vs. 17-day window**
4-0: 17 days is sufficient for a disciplined sequence. Priority order:

| Priority | Item | File | Blocking For |
|----------|------|------|-------------|
| P0-1 | CYCLE-SYNC-GUARD: exclude QHM symbols from tracker comparison | `execution/entry_logic.py` ~L353 | All wiring |
| P0-2 | Wire `get_quarterly_hold_symbols()` into scan loop exclusion | `execution/entry_logic.py` scan loop | Safe entry |
| P0-3 | Create `quarterly_holds_config.json` with Q3 picks | `data/state/` | `add_candidate()` |
| P0-4 | `qhm.reconcile_on_startup()` in `main.py` | `main.py` | Automated entry |
| P0-5 | `qhm.maybe_enter_positions()` in `run_cycle.py` | `strategy/run_cycle.py` | Automated entry |
| P1 | Add `PENDING_EARNINGS` state + cancel/resubmit logic | `execution/quarterly_hold_manager.py` | Earnings-safe stops |
| P2 | Add `trade_events.jsonl` write on QHM external close | `execution/quarterly_hold_manager.py` | P&L observability |
| P2 | Add Beck Test 3 (same-symbol exclusion) | `execution/quarterly_hold_manager.py` | Test coverage |
| P3 | QHM registry startup log | `main.py` | Operational verification |

**Manual entry safety:** ONLY safe after P0-1 and P0-2 land. Before those: intraday bot will fire on the manually-held symbol with no guard — two concurrent positions in the same name. Derman: "Sequence matters: wire exclusion → create config → seed state → place manual order → verify startup log."

**IMPORTANT QHM DOCSTRING FINDING (Technical Board):**
The QHM docstring at `line 4` currently reads: `"Q3 2026 board-approved picks: LLY/GE/GEV (GS window closed)"`. The parenthetical `"(GS window closed)"` was written by the S61 autonomous session when updating from Q2 picks. This is consistent with tonight's 4-board consensus that the GS pre-earnings entry window (Jul 7-14) should be rejected. The docstring already reflects the board's conclusion. **GS is NOT in the active QHM target list** — tonight's research confirms this is the correct decision, with the modification that a GS post-earnings entry on July 15 remains valid if Q2 beats.

---

## Integration Status (QHM Module)

`execution/quarterly_hold_manager.py` — **1305 lines, EXISTS, NOT wired to RTH chain**

Current state (as of S61, June 19, 2026):
- Module exists (commit 93cd5fb — docstrings updated to Q3 picks)
- Q3 picks (LLY/GE/GEV/GS) now in module docstrings
- `ds_gai_complete` status in pending_ds_gai JSON (S49 session)
- Board-approved from S49 with extensive modifications
- `orphan_manager.py`: QHM stop exclusion present (L125-148, L288-295) ✅
- `risk.register_open()`: NOT updated for QHM startup registration ❌
- `tracker.record_exit()`: NOT wired for quarterly closes ❌
- `run_cycle.py`: NOT wired ❌
- `main.py`: NOT instantiated ❌

**Minimum viable integration for July 15 GS entry window:**
Not achievable within the 17-day window without DS/GAI (RTH-chain impact). Recommendation: **Manual entry for Q3 holds.** Rafael places the orders manually. Bot continues intraday trading on symbols NOT in the quarterly hold list. Add quarterly anchor symbols to `config.py` INTRADAY_EXCLUSIONS list (non-RTH-chain change — does not require DS/GAI).

**Full QHM integration:** Reserve for next in-person session. Requires:
1. Steps 1-9 on run_cycle.py (RTH-chain) + DS/GAI audit
2. Steps 1-9 on main.py + DS/GAI audit
3. Steps 1-9 on risk_manager.py (register_open startup) + DS/GAI audit
4. Cold second-agent review of the integrated sequence

---

## Open Questions for Rafael

Before any quarterly hold entry, Rafael needs to decide:

**Q1: Manual vs. automated?**
- A. **Manual (recommended for July 15 window):** Rafael places GS order directly in Alpaca paper account. Bot continues intraday trading, excluding GS. No code changes needed except adding GS to exclusion list.
- B. **Bot-automated:** Requires QHM integration (full Steps 1-9 × 3 files + DS/GAI). Not feasible in 17-day window without skipping mandatory sequence.

**Q2: Which picks to enter?**
- Board unanimous top 2 if constrained: **GE + LLY**
- If GS enters post-earnings (July 15): GS is valid third pick
- GEV is fourth (correlated with GE — treat as one position)
- Recommendation: Start with GE (July 25) and LLY (August 7). Add GS July 15 if Q2 earnings beat.

**Q3: Stop design**
- Board recommendation: Option B+D (2x ATR trail + cancel/resubmit around earnings)
- Absolute floor: -15% hard stop regardless of trail
- Rafael must decide: Automate the cancel/resubmit (requires code) or manage manually?

**Q4: Bot exclusion list**
- Which symbols to exclude from intraday bot while quarterly holds are active?
- Minimum: GS, GE, GEV, LLY
- Implementation: `config.py` addition (non-RTH-chain, no DS/GAI needed)

**Q5: How do quarterly hold P&L entries work in Kelly stats?**
- Quarterly holds have different win rate / payoff distribution than intraday entries
- Should they feed the same Kelly stats engine, or be tracked separately?
- Board recommendation: Separate tracking (Thorp: "different variance profile from intraday R")

---

## Entry Calendar (Q3 2026)

```
Today:    June 20, 2026
─────────────────────────────────────────────────────────
GS Q2 earnings:    July 14, 2026 (board confirmed: do NOT enter before this)
GS entry:          July 15, 2026 (post-earnings, only if Q2 beats)
GE Q2 earnings:    July 16, 2026
GE entry:          July 21, 2026 (Day 3: Jul 17, 18, 21 are trading days)
GEV Q2 earnings:   July 21, 2026
GEV entry:         July 22, 2026 (Day 1 — immediately post-earnings)
LLY Q2 earnings:   August 5, 2026
LLY entry:         August 7, 2026 (Day 2)

NOTE: GEV entry (Jul 22) and GE entry (Jul 21) are on consecutive days.
  If both are entering simultaneously, this triggers the GE+GEV correlation concern.
  Board recommendation: Enter GE on Jul 21 and defer GEV decision to Jul 23+
  after observing GE Q2 earnings reaction.
─────────────────────────────────────────────────────────
```

---

## Next Steps

1. **Rafael reviews this memo** — confirms or adjusts picks and entry approach
2. **Decide: Manual or automated?** Board recommends manual for the July 15 window
3. **Add symbols to exclusion list** — add GS/GE/GEV/LLY to intraday bot exclusion config (non-RTH-chain change, no DS/GAI needed, can be done in CCR)
4. **GS entry: July 15** — if Q2 earnings beat on July 14, enter Day 1 post-earnings
5. **GE entry: July 21** — Day 3 after July 16 Q2 earnings (assuming beat, which is likely given +87% orders)
6. **GEV entry: July 23+** — defer until after GE reaction is assessed (correlation management)
7. **LLY entry: August 7** — Day 2 after August 5 Q2 earnings
8. **Full QHM integration:** Schedule for next in-person session with DS/GAI audit

---

## Data Sources

- GS Q1 2026: Goldman Sachs IR press release, CNBC Q1 2026 earnings, Yahoo Finance Q1 highlights
- LLY Q1 2026: TIKR.com, Motley Fool transcript, PR Newswire (GLP-1 +56% YoY), prnewswire Foundayo approval
- GE Q1 2026: GE Aerospace press release (geaerospace.com), TIKR.com, Value The Markets
- GEV Q1 2026: Investing.com transcript, GE Vernova IR press release, Yahoo Finance highlights
- Earnings dates: Nasdaq, MarketChameleon, coincodex
- Prices: Google Finance, CoinCodex, MacroTrends (June 17-20 data)
- 13F data: Fintel.io, HedgeFollow, WhaleWisdom, MarketBeat
- Foundayo competitive analysis: BioPharma Dive, Fierce Pharma, CNBC
- GS catalyst (SpaceX IPO): Yahoo Finance, StockStory, iTiger

*DS/GAI audit on integration code deferred — not available in CCR session (no .env). Required before any QHM RTH-chain changes. This memo is research-only; no RTH-chain file edits were made.*

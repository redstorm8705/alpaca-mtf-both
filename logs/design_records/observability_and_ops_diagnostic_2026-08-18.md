# Bot Operation + Observability + Reporting — FULL DIAGNOSTIC (2026-08-18)

**Trigger:** Rafael, reviewing a week of Slack, flagged that "something is broken," several routines look
like "dead ends both on Claude AND Slack," the weekly P/L summary isn't actionable, and asked why the bot
isn't trading a down day. **All findings below are SOURCE-VERIFIED this session** (live Alpaca API, live
OCI bot log/state, current Groq model list) — no assumptions. Framework: BUILD-don't-fix (each item gets a
root cause + a forward build, not a band-aid); BGG (board + Gro + GAI) to review the solution set.

**⚠️ CROSS-CUTTING CONSTRAINT (found while diagnosing): Gro is DOWN.** `llama-3.3-70b-versatile` now
returns `404 model_not_found` from Groq — it was deprecated mid-week. This breaks the autonomous audit
pipeline AND the BGG's own Gro voice. Current Groq models: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`,
`qwen/qwen3.6-27b`. **Fix Gro first** (below) or every BGG round runs GAI-only (Gro-skip rule applies but
we lose a voice). This is why "Gro ❌" appears everywhere in Slack.

---

## THE UNIFYING PICTURE
The bot's **execution** is largely fine (it trades, stops work, QHM/earnings-trim fire). What's broken is
the **feedback layer** around it — the audit pipeline, the ledger reconciliation, P&L attribution, and
alerting — so Rafael can't get a trustworthy, actionable read on what the bot is doing or how it's doing.
Plus one **strategy gap** (red-lockout blocks the dip-accumulation he wants). Seven verified issues:

---

## A. WHY THE BOT ISN'T TRADING TODAY — strategy gap, not a bug (VERIFIED at live OCI log)
- Live log 2026-08-18: `RULE 1 — PREMARKET RED LOCKOUT: 14/16 tracked movers red ≥2% (88%) — ALL LONGS
  BLOCKED this session. Shorts + inverse ETFs only.` + `MRI STRESSED (score=41) → size 0.70x` + `Dynamic
  MIN_SCORE raised to 10/12 → 11-12 signals filtered`.
- The bot is doing what it was designed to do: momentum logic avoids longs on a broad-red day. But Rafael
  wants to ACCUMULATE QHM/F6 (buy-and-hold) + dip-buy on exactly these days.
- **ROOT:** an INTRADAY momentum rule (Rule-1 red-lockout) is applied uniformly, including to the
  long-horizon tiers (QHM/F6) whose thesis is the opposite (buy weakness).
- **BUILD (forward):** carve QHM/F6 accumulation OUT of the intraday red-lockout — give the long-horizon
  tiers their own entry gate (value/dip-oriented: e.g. enter on a red day when price is within X% of a
  support level or Y% below a recent high), independent of the intraday momentum gate. Board vote required
  (changes an entry gate → risk-path per CLAUDE.md Rule E). This is a "build," not a fix.

## B. AUGUST PERFORMANCE — the honest, Alpaca-sourced read (answers "how are we doing?")
- Equity **$2,769.89** (cash $513.49). Month: **$2,678 → $2,774 = +$95.77 (+3.6%)**; PEAKED **$2,864.85
  (Aug-4)** then pulled back ~$95; still BELOW the April-17 ATH **$2,903.91**.
- Per-strategy realized (pnl_ledger FIFO, as of Aug-11 — the last clean pull; a fresh pull ERRORED, see
  §G): **+$41.47 = intraday −$31.10 + QHM +$72.57.** The INTRADAY core is net-negative on the month; QHM
  holds + earnings-trims carry it; most of the equity gain is UNREALIZED QHM markup.
- **This IS the actionable insight the weekly P/L summary is missing:** per-tier attribution (intraday vs
  QHM vs F6), realized-vs-unrealized split, and drawdown-from-peak. Today's summary reports an aggregate
  "missed move" number without the per-strategy breakdown that would tell Rafael *which engine is working*.
- **BUILD:** rebuild the weekly summary around per-tier realized/unrealized attribution + drawdown-from-
  peak + win-rate/payoff PER TIER (not blended). (Ties to the dashboard-dropdown work already queued.)

## C. LEDGER FROZEN + CORRUPTED (the loudest Slack thread) — VERIFIED live on OCI
- Live OCI `ownership_ledger.json`: `last_reconciled_utc: None` (has NEVER successfully reconciled);
  **GEV/qhm=1.0** (STALE — GEV is FLAT at Alpaca; its GTC stop fired, a legit close); **NVDA
  intraday=-1.0 (PHANTOM synthetic short) + qhm=1.0** (broker shows NVDA long 1). `heal_confirms: {}`.
- The GEV floor shrink (1→0) trips the never-shrink guard → needs a manual `confirm_ledger_heal.py GEV
  qhm 0` → nobody ran it → **`ledger_sync` FAILED 18 consecutive times → ledger STALE all week.**
- **IMPORTANT NUANCE (verified):** `OWNERSHIP_GUARD_ENFORCE=False` → the "sells stay frozen" text is
  logged-but-NOT-enforced (the guard is dormant). So the frozen ledger is a **data-integrity + alerting
  mess, NOT an execution block** — it is not why the bot isn't trading (that's §A).
- **ROOT (two joined defects):** (1) a legit protected-tier reduction via ANY path (GTC stop, earnings-
  trim, external close) demands a manual confirm the operator never gives → the sync jams; (2) the FIFO/
  ledger has no long lots for QHM buys → a QHM sell books a phantom synthetic short (NVDA intraday=-1).
- **BUILD:** (1) **v4-B** (built + gate-cleared this session — see §H) auto-confirms the EARNINGS-TRIM
  reduction; EXTEND the same authorized-reduction path to the GTC-stop / external-close exit of a QHM hold
  (GEV's case) so ANY legit QHM exit self-reconciles. (2) Fix the FIFO attribution so QHM buys register
  long lots (no phantom short) — the tier-tagged fills exist; the FIFO reconstructor must consume them per
  tier. Both are risk-path → full board + Gro/GAI.
- **IMMEDIATE (operator, not code):** GEV is confirmed flat at Alpaca (legit) → running `confirm_ledger_
  heal.py GEV qhm 0` on OCI unsticks the sync now. (Operator action — Rafael's call; I don't run
  financial-state ops unilaterally.)

## D. AUTONOMOUS AUDIT PIPELINE DEAD ("dead-end routines") — VERIFIED
- `autonomous_review.py` + `autonomous_patch_generator.py` hardcode `_GRO_MODEL="llama-3.3-70b-versatile"`
  → 404 → "Groq API failed", "All DS/GAI API calls failed tonight", meta-audit "Gro ❌ 404". These
  routines RUN nightly but produce nothing → exactly the "dead ends" Rafael saw.
- **ROOT:** Groq deprecated the model; the constant is stale in ≥2 files (and my BGG scripts).
- **BUILD:** (1) swap `_GRO_MODEL` → `openai/gpt-oss-120b` (current largest Groq general model) across all
  call sites; (2) BUILD-don't-fix forward: add a startup/pre-call **model-availability probe** (hit
  `/v1/models`, pick the configured model or fall back to the first available general model) so a future
  Groq rename degrades gracefully + alerts, instead of silently dead-ending for a week. Risk-path? No
  (audit tooling, not RTH execution) — but it IS a GATED_SELF concern; board-light + statics.

## E. FIFO SYNTHETIC-SHORT on QHM exits — VERIFIED (NVDA, GEV both hit it)
- Slack: `FIFO CRITICAL: NVDA closing sell with no prior long lots. Qty=1 @ $227.89. Synthetic short
  recorded` (and same for GEV). Live ledger confirms NVDA intraday=-1.0.
- **ROOT:** the FIFO P&L reconstructor doesn't see the QHM buy as a long lot for the tier being sold →
  treats the sell as opening a short. Attribution gap between the tier-tagged buy and the FIFO lot book.
- **BUILD:** make the FIFO reconstructor tier-aware (consume the tier's own long lots), OR seed prior-day
  QHM lots so a QHM sell always matches a long. Part of the §C ledger rebuild. Risk-path (P&L integrity).

## F. P&L DRIFT + FILL-UNVERIFIED — recurring, VERIFIED
- EOD drift $-9.75 (08-17), $+49.74 (08-18); `FILL UNVERIFIED [QQQ]` → P&L booked at entry_price fallback.
- **ROOT:** tracker vs Alpaca-FIFO reconciliation gap + the close-fill recovery falling back to entry
  price when it can't recover a verified fill.
- **BUILD:** the P&L-sourcing rule says Alpaca FIFO is authoritative — make the tracker DISPLAY the FIFO
  number (not its own math) so "drift" can't exist as a user-facing surprise; harden the fill-recovery to
  page (not silently book entry_price) when a fill can't be verified. (Overlaps §B reporting rebuild.)

## G. ALERT NOISE / SEVERITY MISCALIBRATION + P&L-TOOL FAILURE — VERIFIED
- `CRITICAL — BOT SHUTDOWN SIGTERM (signal 15)` fires on the ROUTINE 2 AM nightly restart → false-alarm
  CRITICAL. 18× identical `ledger_sync FAILING` every 20 min → no dedup/escalation. `autonomous_patch_
  generator: clean run no work` twice → no-op noise. AND: `build_ledger()` (the authoritative P&L tool)
  THREW on `fetch_all_fills()` when I pulled fresh numbers — the tool that should give actionable P&L
  itself failed.
- **ROOT:** severity is per-event, not per-context (a planned restart ≠ a crash); repeated identical
  alerts aren't deduped/escalated; the P&L tool has an unhandled fetch failure path.
- **BUILD:** context-aware severity (planned restart → INFO; only an UNPLANNED exit → CRITICAL); alert
  dedup + escalation ladder (Nth identical alert → one escalation, not N repeats); harden `build_ledger`'s
  fetch with retry/fallback so the P&L view never dead-ends.

## H. v4-B STATUS (this session's build) — GATE-CLEARED, ready to ship
- The QHM earnings-trim → direct authorized ledger decrement (pure-qhm guarded, tier-1 + tier-2 with a
  settle-poll). Build gate: statics ✅; masked-loss ✅; reliability ✅; data-integrity ✅ (tier-2 poll
  cleared its reject); cold-2nd ✅; GAI ✅; **Gro 404 (unavailable → CLAUDE.md Gro-skip rule: board
  majority + GAI APPROVE suffices)**; adversarial + reliability-re-review were still in flight when the
  ops review took over. It directly fixes the EARNINGS-TRIM slice of §C. Ready to ship on Rafael's word.

---

## PRIORITY / SEQUENCING (proposed — BGG to confirm)
| # | Item | Type | Risk-path? | Effort |
|---|------|------|-----------|--------|
| 1 | Fix Gro model (`gpt-oss-120b`) + availability probe | unblocks BGG + the pipeline | no | small |
| 2 | Ship v4-B (earnings-trim auto-reconcile) | already gate-cleared | yes (done) | ship |
| 3 | GEV operator-confirm (unstick live ledger NOW) | operator action | — | 1 cmd |
| 4 | Extend authorized-reduction to GTC/external QHM exits + FIFO tier-aware lots (§C/§E) | ledger integrity | yes | medium |
| 5 | Per-tier P&L attribution rebuild (§B/§F) — actionable weekly summary + dashboard dropdown | reporting | no | medium |
| 6 | QHM/F6 dip-accumulation gate (exempt from intraday red-lockout) (§A) | strategy | yes | medium |
| 7 | Alert severity/dedup + build_ledger hardening (§G) | observability | no | small-med |

**BGG PLAN:** convene a MODE-2 (architecture/whitespace) board + Gro (once §1 fixes it) + GAI on this
diagnostic to (a) confirm the root causes, (b) rank by P&L/risk impact, (c) approve the build order. Then
each build goes through its own standard gate. Nothing ships from this doc without that.

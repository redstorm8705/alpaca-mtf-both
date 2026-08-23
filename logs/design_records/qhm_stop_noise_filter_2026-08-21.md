# QUEUE (risk-path, awaiting Rafael approval) — QHM protective-stop noise filter (2026-08-21)
**Rafael's question:** can QHM stops require the 9/9 sustained-breach confirmation before the stop initiates?
**Answer + BGG recommendation below. NOT built — risk-path, needs the full masked-loss board gate + Rafael's go.**

## THE MECHANISM (verified at source)
- A **hard GTC stop** (what protects QHM holds today) is a real SELL-STOP order resting at Alpaca; it fills the
  instant price touches it, with NO bot involvement — so it protects even when the bot is offline, but it fires
  on any single touch/wick. It CANNOT be gated by the 9-scan confirmation (an exchange order can't wait for the
  bot's scans; that instant fill is the feature). `broker.submit_gtc_stop_order`.
- The **9-scan sustained-breach exit** (`overnight_atr_buffer_exit`, exit_logic.py L1270-1287) is a BOT-SIDE
  soft exit — needs the breach to persist 9 consecutive 5-min scans, runs only while the bot scans. It lives on
  the INTRADAY book only; QHM does NOT have it.
- So "9/9 before a QHM stop" is not a toggle — it requires changing how QHM protection works.

## BGG CONSENSUS (Gro + GAI, strongly aligned; board framework POV) — Hybrid (c), REJECT pure-soft
- **The catastrophic risk is an unprotected offline gap, NOT a noise stop-out.** GAI: LLY ≈ 46% of NAV in one
  name; a −20% overnight gap while the bot is down = **−$240 (−9.2% of the account)** with no resting order.
  A noise stop-out just locks a gain — **LLY's +$40.88 was the system WORKING, not a bug.** Removing the hard
  stop (option b) is "strictly unacceptable" (GAI); Taleb/Thorp: never remove the ruin-avoidance floor for a
  nuisance.
- **Recommendation: HYBRID (c)** — keep a WIDE hard GTC as a disaster-only backstop + add the 9-scan soft exit
  for profit-lock / noise-filtered exits (reuse of the tested intraday two-layer scheme, not greenfield).
- **The calibration that makes it work:** the hard GTC must sit FAR below the soft level (GAI: ≥
  `max(2.5×ATR14, 8%)`); if it's tight (as LLY's raised profit-lock stop was), a noisy wick trips the hard stop
  before the soft exit completes its 9 scans and the filter does nothing. The real change: **stop using the
  hard stop to lock profits; let the 9-scan soft exit do that, and demote the hard stop to a wide catastrophe
  rail.** Tail risk is NOT widened (exchange backstop stays); ~0.2–0.5×ATR local execution variance traded for
  holding power, well-aligned with a 60–90-day hold.
- **Mandatory guards (GAI):** (1) hard GTC ≥ 2.5×ATR14 / 8% below the soft level; (2) update the hard GTC ≤
  once daily (avoid order churn / Alpaca rate thrash); (3) if the bot fails to scan ≥30 min during RTH, the
  hard GTC is the sole defender — NEVER cancel it before confirming soft-exit readiness.

## RAFAEL'S DYNAMIC REFINEMENTS (2026-08-21) — the stop must be STATE-AWARE, not static
The BGG Hybrid (c) is correct but must be made DYNAMIC along two axes (Rafael: "we got stopped out and were
fine with just taking +$40 and calling it a day. That's static and not dynamic"):

1. **PROFIT-STATE-AWARE ("in profit" → let winners run; underwater → harder stops).** When the position is IN
   PROFIT, lean toward letting the winner run: wait for the 9-scan confirmation even if +50% gives back to +40%
   — accepting the give-back to avoid a weak-wick stop-sweep is CORRECT. The "in profit" branch favors the soft
   9-scan exit + a WIDE hard rail. When the position is NOT in profit, that is when HARDER (tighter) stops
   apply — protect capital when there's no cushion. So the hard/soft geometry FLIPS on the sign of open P&L.
2. **NAV-CONCENTRATION-AWARE.** The higher the % of NAV a single name occupies, the CLOSER the soft stops move
   toward the take-profit targets (tighter give-back tolerance) — a name that is 46% of NAV cannot be given the
   same loose leash as a 5% name. Soft-stop distance scales INVERSELY with position-NAV-weight.

These MODIFY the Hybrid-c geometry: the "wide hard rail + tighter soft exit" spacing is not a fixed
`max(2.5×ATR,8%)` — it is a FUNCTION of (open-P&L sign/magnitude, NAV concentration). BGG refinement owed on
the exact curves before build. Reconciles with backlog #2 (ATR buffer Tier_adj 0.5→0.75 overnight).

## STATUS
Queued behind P2 (ledger auto-confirm). On Rafael's approval → full masked-loss board + Gro + GAI gate on the
design + diff (risk-path: changes when/whether a protected hold exits). Reuses `overnight_atr_buffer_exit`.
Dynamic curves (profit-state + NAV-concentration) need a BGG refinement pass before implementation.

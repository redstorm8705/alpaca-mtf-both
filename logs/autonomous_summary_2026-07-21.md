# Autonomous session summary — 2026-07-21 (Rafael away)

Written for Rafael's return. Plain English first, technical detail after.

---

## 🚨 READ THIS FIRST — your bot is not running, and it hasn't been since Sunday

**What I found.** I logged into the Oracle server to deploy a change and discovered that all three
bot services are **stopped and switched off**. Not crashed — deliberately shut down. All three went
down at the exact same second: **Sunday July 19, 1:50 PM PT**. They have been off for about two days.

There is also **no cron schedule installed** for the bot user, so nothing is waking it up.

**How I know it wasn't a crash.** A crash leaves an error in the system log. These three logged
"Deactivated successfully" — a clean, orderly stop — and then someone set them to `disabled`, which
is the switch that stops them from coming back on their own after a reboot. That is a person or a
script doing it on purpose. I could not find any note anywhere explaining why.

**I did not turn it back on, on purpose.** Restarting a trading system that someone deliberately
switched off, while you're away and I can't reach you, is your call and not mine. The moment it comes
back up it starts managing your 11 open positions again. **This is the first thing to decide when
you're back: was Sunday's shutdown intentional, or was it maintenance nobody reversed?**

**Your money is not sitting exposed.** I checked the account read-only. You have **11 open positions
and 10 protective stop orders sitting live at Alpaca**. Those stops are GTC ("good till cancelled") —
they live on Alpaca's servers, not in the bot, so they keep protecting you whether the bot is running
or not. I checked every one against its position and the share counts match exactly:

| Symbol | Position | Stop order | Covered? |
|--------|----------|-----------|----------|
| PANW | 1 long | sell stop 1 @ $317.13 | ✅ |
| NET | 1 long | sell stop 1 @ $256.28 | ✅ |
| MARA | 11 long | sell stop 11 @ $10.86 | ✅ |
| SOFI | 4 short | buy stop 4 @ $18.83 | ✅ |
| NFLX | 2 short | buy stop 2 @ $71.21 | ✅ |
| HOOD | 2 long | sell stop 2 @ $92.60 | ✅ |
| XOM | 3 long | sell stop 3 @ $147.72 | ✅ |
| TQQQ | 1 long | sell stop 1 @ $43.23 | ✅ |
| RIVN | 10 long | sell stop 10 @ $14.35 | ✅ |
| NVDA | 2 long | sell stop 2 @ $169.21 | ✅ |
| **GOOGL** | **2 long, $697.66** | **none** | **by design** |

GOOGL is the only one without a stop, and that is correct, not a hole: `protected_symbols.json` on
the server lists `["GOOGL","NVDA"]` — your quarterly long-term holds, which are deliberately exempt
from intraday stops.

Account as of 2026-07-21 5:10 PM PT: **equity $2,701.80**, cash $130.42, previous close $2,678.45
(so roughly +$23 on the day).

**What you ARE losing while it's off:** no exits, no partial profit-taking, no trailing-stop
ratcheting, no MRI or kill-switch monitoring, and no dashboard / scanner / GEX updates. The hard
stops hold the floor, but nothing is taking profits or tightening anything.

---

## ⚠️ A correction to the record — the handoff file was telling you something false

The previous handoff block, dated **July 20**, said: *"OCI DEPLOY_OK, HEAD=4c657f7, 4/4 services
active, _compute_pin runs on the box."*

**That was wrong**, and it was dated a day *after* the services had already been shut down.

The evidence: the system journal shows no start event after July 19 20:50 UTC, and the GEX snapshot
file on the server was last written **July 17**. So the GEX pin numbers that were "verified in
production" that session (SPY centroid 747.15 / wall 747.00 / confidence 0.28) came from a **manual
one-off command run by hand on the server**, not from a live service on a schedule.

To be clear about what this does and doesn't mean: the GEX pin code itself is correct and it *is*
deployed on the box. It just isn't being called on a schedule, because nothing is running. Deploying
code and having code run are two different things, and the earlier session conflated them.

I've corrected `handoff.md` and added a note that any "N/N services active" claim must be re-verified
with `systemctl show` rather than believed.

---

## ✅ What I actually shipped — monthly timeframe support (commit `1df57f5`)

This is step 1 of 6 of the scanner tiering you approved (splitting the scanner into
intraday / weekly / monthly, each bullish or bearish, one stock in exactly one bucket).

**The problem in plain English.** You asked for a monthly view of each stock. The bot literally could
not ask Alpaca for monthly price bars — it knew about 15-minute, hourly, daily and weekly bars, and
nothing longer. Any request for a monthly bar would have thrown an error.

**The fix.** Taught it the monthly timeframe: what to call it, how many bars to keep (36 ≈ three
years, enough for a 10-month moving average with room to spare), and how far back to reach to get
them. That's it — four small additions across two files.

**Why it's safe.** This is purely additive. It adds a new option; it changes nothing about how any
existing timeframe behaves. The riskiest way a change like this could go wrong is if some piece of
code loops over the list of timeframes — then adding one would silently make the live bot fetch extra
data every cycle. A cold reviewer swept the entire repository and confirmed **nothing loops over
those lists**; every single use is a direct lookup by name. The two places that do loop over
timeframes use separate lists that I didn't touch.

**Proof it works.** I ran it live against the real Alpaca API: SPY returned 36 monthly bars, and the
10-month average came out to 704.59.

**One catastrophic failure mode, caught and closed.** The cold reviewer pointed out that if the
Oracle server's copy of the Alpaca library didn't support monthly bars, the bot would crash *on
startup* — a "purely additive" change would have taken the whole system down. So I checked the
server's library **before** deploying: `alpaca-py 0.43.3`, monthly supported. Verified, then shipped.

**Gate record:** full read of both files (703 + 326 lines) → py_compile / mypy / ruff all clean →
cold second-agent PASS → preship GAI **APPROVE**. Gro was skipped — it's out of daily quota, and you
authorized "if groq isn't responsive, skip it." Deployed to Oracle, `DEPLOY_OK`, server HEAD is
`1df57f5`. No restart needed (nothing live uses it yet, and the services are off anyway).

**Two traps noted for the next step**, both flagged by the cold reviewer:
1. The scanner must call `fetch_bars(symbol, TF_MONTHLY)` directly. Going through the older
   `get_bars(days_back=...)` helper would translate "3 years" into a request for ~96 years of data.
2. The most recent monthly bar is **always the current, unfinished month**. The tiering logic must
   drop it — which matches the "completed bars only" rule already in the design doc.

---

## 📋 Where the rest of the queue stands

| # | Item | Status |
|---|------|--------|
| A1 | Scanner tiering — monthly timeframe plumbing | ✅ **shipped `1df57f5`** |
| A2 | Three horizon-state functions (completed bars only) | ⏭️ next |
| A3 | Tier engine (pure function, one stock → one bucket) | queued |
| A4 | Direction × horizon UI in `scan_to_html.py` | queued |
| A5 | Relative strength vs SPY | queued |
| A6 | Universe expansion (+XLE/XLF/XLV/XOM/JPM/LLY/UNH/IWM/GLD) | queued |
| B | $75 cap on 0DTE recommendations — put the number to BGG | queued |
| C | BGG evaluation queue (financial-datasets MCP, 10 libraries, ICT model, research prompts) | queued |
| D | Why the dashboard scans every 5 min with the market closed | queued |

Design decisions for A are all locked in `logs/scanner_tiering_design_2026-07-20.md` — your seven
answers plus the BGG rulings on Q2/Q4/Q6.

---

## ❓ The one thing I need from you

**Was the July 19 shutdown intentional?**

- If **yes** — tell me the reason so it goes in the handoff, and I'll stop treating "services down"
  as an incident.
- If **no** — say the word and I'll re-enable and start all three, verify they come up clean, and
  confirm the first scan cycle and dashboard write.

Everything else in the queue I can keep driving without you.

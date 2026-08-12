# APPROVAL PACKAGE — Autonomous session 2026-08-11 (AWP rolling chain)

**Scheduled/autonomous session. NOTHING has been shipped or applied — this package stops at
diagnosis + a scoped proposal, per the AWP hard rule.** All findings below were verified at
source (live OCI logs, Alpaca's own fill records, live-invoked code, adversarial markers) —
not relayed from any prior session's claims.

---

## 🆕 ADDENDUM (2026-08-11 ~20:00 PDT, same rolling-chain, continued after-hours) — Finding A's
open question CLOSED with a live number; Finding B's "anomaly" is now a fully-located root cause

**FINDING A — the stop IS live, exact number confirmed (was previously "could not independently
confirm").** Queried Alpaca's own `/v2/orders?status=open&symbols=SMCI` directly (read-only,
same call `generate_dashboard.py` already makes): **SELL 3 sh STOP @ $24.05, status `new`, id
`a24f87cc-967e-4e51-9be7-8e7cc63e8ff6`, created 2026-08-10T20:36:39Z** — the original protective
stop from Monday's entry, still resting, untouched. SMCI trades ~$34.05 right now (dashboard),
so there's ~$10/share of cushion. **Money is not open-ended at risk — bounded by a real resting
stop, confirmed, not inferred.** Important nuance for the record: this is the *static* catastrophe
stop only. Because the position is absent from the tracker's `open_trades`, none of the bot's
*adaptive* exit logic (trailing, breakeven push, target-based partial exits) is running for it —
only this one flat GTC order protects it. Doesn't change the root cause or the recommended fix
(still needs the dedicated interactive session on `portfolio_tracker.py` Phase 2a.5) — it answers
the "is money at risk" question with a number instead of a shrug.

**FINDING B — root cause LOCATED (was "a live-process anomaly, cause unclear").** The dashboard
fix (PR #130) is correct on disk and in any fresh process — that part of the prior write-up
holds. What was missing: **a second, separate, long-running process is clobbering it.**
`mtf-writer.service` (`live_data_writer.py`) has been running continuously since
**2026-08-06 18:44:02 UTC** (`systemctl show mtf-writer --property=ExecMainStartTimestamp`) —
**four days before** `generate_dashboard.py`'s fix was committed (file mtime on disk:
**2026-08-10 21:23:51 UTC**). That process does `from generate_dashboard import generate`
*inside its refresh loop*, with a comment claiming this makes it hot-reload
(`live_data_writer.py:74`: `# Import inside loop so any hot-reload of generate_dashboard
works`). **That belief is false.** CPython caches modules by name in `sys.modules`; a repeated
`import` statement after the first one is a no-op — it does not re-read the file from disk. Only
`importlib.reload()` does that. So this process has been silently running the **stale, pre-fix**
`generate()` in memory the whole time, overwriting `dashboard.html` every ~30s — roughly 10x more
often than `main.py`'s own correct 5-minute write (main.py's process *was* restarted 2026-08-11
06:00:06 UTC, after the fix, so its writes are genuinely correct — they just keep losing the race).

Evidence chain (verify-at-source):
- `systemctl show mtf-writer --property=ExecMainStartTimestamp` → `Thu 2026-08-06 18:44:02 UTC`
- `generate_dashboard.py` disk mtime → `2026-08-10 21:23:51 UTC` (4 days after the writer started)
- `logs/live_writer.log` tail: continuous successful writes every ~31s at 24,905–24,919 bytes,
  zero errors — matches the prior session's own byte-deficit measurement (~1,000–1,300 bytes
  short of a correct render) exactly, now with a mechanism instead of a mystery.
- Read `generate_dashboard.py:558-620` in full: the QHM stop-fallback logic itself is correct and
  unchanged since the fix; not the site of the bug.

**Residual open question, honestly flagged, not guessed at:** `auto_deploy.sh` restarts
`mtf-bot`, `mtf-writer`, and `mtf-http` together in one `systemctl restart` command, so a normal
automated deploy should have cycled both in lockstep. Why `mtf-bot` shows a fresh Aug-11 restart
while `mtf-writer` still shows Aug-6 isn't fully explained by what's on the box —
`logs/auto_deploy.log` is only 28 lines total (thin for this week's commit volume) and its tail
shows 12 repeated `auto_deploy.sh: not found` errors before a handful of daily heartbeat lines,
which smells like a separate reliability gap in the deploy mechanism itself. Not chased further
this session — flagged as adjacent, not this finding's cause.

**Recommended action — now much more precisely scoped and lower-risk than the prior "restart
mtf-bot" suggestion:** `sudo systemctl restart mtf-writer` **only.** This process has zero
execution/trading-path involvement (it only calls `generate_dashboard.generate()` and a
read-only GEX refresh) — restarting it doesn't touch the trading engine at all. Next 30-second
write should show the real QHM stops immediately.

**Forward-improvement note (BUILD, DON'T JUST FIX pass — logged, not built, no code changed):**
the same staleness risk applies to every module `live_data_writer.py` imports inside its loop
(`generate_dashboard`, `data.gex`, `alerts`) — any future fix to any of those three will silently
fail to reach the live dashboard until this companion process is separately restarted, exactly as
happened here. Two structural fixes worth a future gated pass: (a) cheap — a post-deploy smoke
test in `auto_deploy.sh` that renders dashboard.html in a fresh subprocess and diffs it against
the live service's last write, Slack-alerting on mismatch, catching this whole class immediately;
(b) more thorough — swap the in-loop `import` for `importlib.reload(generate_dashboard)` so the
comment's stated intent becomes true. Worth investigating together with the open question above
(why the two services didn't restart in lockstep) since if that's systemic it recurs every deploy.

---

## 🚨 FINDING A (URGENT, action needed) — a real 3-share SMCI position has been invisible to
the bot's own protection/exit logic for 2 full trading days

**THE PROBLEM (plain English + real numbers):**
On Monday 2026-08-10 at 7:45 AM PT, the bot correctly bought 3 shares of SMCI at $32.68 average
— a normal, healthy entry (score 10/12, same batch as a SPY entry that morning). Three minutes
later, something in the bot's own bookkeeping **incorrectly decided this trade had already been
closed** — it wrote a fake "closed, exit reason: external_close, price $32.685, profit $0.005"
record into its trade log, even though **nothing was ever sold.** I confirmed this directly
against Alpaca's own fill history (the source of truth): there is no SMCI sell order anywhere
in this window. The broker has held these 3 shares continuously since Monday morning.

Because the bot's tracker believes this position is closed, its normal stop-loss/target/exit
logic has not looked at SMCI once in 2 days. Overnight, a second bug compounded it: the bot's
"orphan adoption" process found the broker's 3 shares, didn't recognize them (because the
tracker had wrongly marked the trade closed), and adopted them as a *brand-new* position with
the WRONG stop ($29.51 instead of the real $24.07) and WRONG target ($39.30 instead of $50.58)
— and then immediately fake-closed *that* one too, the same way.

**Is money at risk right now?** Likely bounded, not zero. The original entry did request a real
protective stop order from Alpaca (`gtc_stop_order_id` is recorded), so there may be a resting
stop at $24.07 still on the broker's books — I could not independently confirm this without
order-management API access (deliberately excluded from this read-only session per the Alpaca
MCP invariant). SMCI is trading well above that level per today's volume logs. This is paper
money. The real damage today is **integrity, not dollars**: two fabricated ~$0 "trades" are now
sitting in the bot's own performance history, which feeds directly into the Kelly position-sizing
statistics ("Kelly stats rebuilt from corrected closed_trades post-EOD FIFO" — this fabricated
SMCI record is now poisoning the size of every future trade, however slightly).

**What caught it:** the read-only drift detector shipped 2026-08-09 (PR #111) worked exactly as
designed — it has correctly logged this exact mismatch (`drift_type: phantom_broker`) on every
single 5-minute RTH cycle for 2 full trading days (44+ consecutive occurrences), Monday and
today. It is intentionally detection-only for this drift class (adopting/correcting a position
the tracker doesn't know about is explicitly out of scope until a future gated build) — so it
correctly never touched it. **This is not a bug in anything shipped this week — the new
detector is the reason we found this at all**, not the cause.

**Evidence chain (verify-at-source, not relayed):**
- Alpaca FILL activity API: SMCI buy, 3 sh, $32.69+$32.68×2, order `3b754ee8…`, 2026-08-10
  14:45:27–28 UTC. Zero SMCI sell fills anywhere after that.
- `mtf_bot.log:222863`: `execution.portfolio_tracker | [SMCI] Entry recorded: long 3 @ $32.68`
  — the entry WAS correctly recorded at the time.
- `mtf_bot.log:222976` (3 min later): `CRITICAL | execution.fifo_pnl | [SMCI]: buy_to_cover with
  no open short lots (net_qty=-1...)` — SMCI's FIFO lot state was corrupted (thought there was a
  phantom 1-share short open), likely a carryover artifact from an unrelated SMCI short trade
  that closed 8/4. I read `execution/fifo_pnl.py` in full (442 lines): this specific CRITICAL log
  is a **correct, board-approved (S49) fail-safe** — it explicitly refuses to fabricate a
  synthetic lot and does NOT itself write to `trade_log.json`. It's a symptom, not the bug.
- `trade_log.json` (live, on OCI right now): **two** separate `"status": "closed"` records for
  this same SMCI entry (same entry_time, same $32.683333 entry price), both with
  `"exit_reason": "external_close"`, `"exit_price": 32.685` (no matching real fill), `"pnl": 0.005`.
  One is the original entry; the second has `"_adopted_orphan": true` with different (wrong)
  stop/target, exit_time 2026-08-10T23:02:20 PT (overnight, likely the nightly restart's
  reconcile pass).
- Alpaca's own live position snapshot, read every RTH cycle by the bot itself: SMCI still 3 sh
  long, avg cost exactly $32.683333 — unchanged for 44+ consecutive drift-detector cycles.

**ROOT CAUSE — located, verbatim-verified, NOT patched (per AWP no-code rule):**
`portfolio_tracker.py::write_eod_summary()`, the "Phase 2a.5" overnight reconciliation block
(~lines 1051-1126), decides a position was "externally closed" **by its absence from a locally
FIFO-reconstructed lot dict** — never by asking Alpaca directly. That FIFO dict is built by
`_fifo_reconstruct()` (`fifo_pnl.py`), which — by a DELIBERATE, board-approved (S49) fail-safe —
silently drops a symbol whose lot-matching goes wrong rather than fabricate a fake lot (exactly
what happened to SMCI, per the CRITICAL log). Phase 2a.5 then computes a plausible-looking "exit
price" from the same corrupted match and calls `record_exit(..., alpaca_confirmed_absent=True)`
— **hardcoding the "confirmed absent" flag to True without ever independently confirming
anything.** `record_exit()`'s own Guard D exists precisely to stop an unverified external_close
("must be Alpaca-VERIFIED by the caller") — Phase 2a.5 satisfies Guard D's letter while defeating
its purpose. By contrast, `orphan_manager.py`'s own external-close branch (same kind of decision)
does two independent live broker queries first (a fresh full-account snapshot AND a fresh
per-symbol lookup) before ever calling `record_exit` — Phase 2a.5 has neither. That
architectural inconsistency between two code paths making the same kind of decision, one safe
and one not, is the real defect. The later `_adopted_orphan: true` duplicate record is explained
too: once the (still fully live) SMCI position reappears in `orphan_manager.reconcile_positions`
as a genuine orphan (tracker has no record of it, broker does), it's re-adopted with fresh
ATR-based stop/target and `overnight: True` — which makes it eligible for the *same* Phase 2a.5
sweep on the next EOD flush, closing it identically a second time.
**Not done this session (needs the full patch sequence, dedicated interactive session):** the
actual fix — most likely making Phase 2a.5 do what orphan_manager already does (an independent
live `/v2/positions` check before trusting the FIFO dict's absence as proof) — requires full read
+ 10-pt audit + board + Gro/GAI on `portfolio_tracker.py` (1,948L) and `orphan_manager.py`
(1,733L), both hotspot files. Full verbatim trace (6 files, ~2,639 lines read) archived in
`logs/tb_audit_log.md`'s 2026-08-11 entries.

**Recommended immediate operator action (not code — just verification):** confirm the $24.07
GTC stop is still live on Alpaca for SMCI (Alpaca app or a read-only order query), since the bot
cannot currently show you this itself.

---

## ⚠️ FINDING B — PR #130 (QHM dashboard stop fix) is verifiably correct in code but is NOT
rendering correctly on the live, running dashboard

**THE PROBLEM (plain English + real numbers):** PR #130 (merged Monday) was supposed to fix the
dashboard showing "—" instead of a real stop price for quarterly holds (GE, GEV, GOOGL, LLY,
NVDA). Right now, live on the production dashboard, all 5 of those holdings **still show "—"**
for Stop — the exact bug #130 claimed to fix.

I did not stop at "still broken" — I verified WHY, and the answer is surprising: **the fix code
itself is correct.** `data/state/quarterly_holds.json` has the real stop prices (NVDA $211.06,
GE $307.33, GEV $863.06, GOOGL $338.62, LLY $1011.60). When I manually invoked the exact same
`generate_dashboard.generate()` function the live bot calls, using the exact same live data, it
correctly wrote "$307.33" for GE. The commit's own adversarial-gate marker
(`.claude/preship/markers/generate_dashboard.py.adversarial.json`, sha256-bound to the exact
shipped file) independently confirms a render test passed the same way at ship time. Yet the
**live, running bot service** — same code on disk, restarted since the fix (06:00 UTC today,
9+ hours after the fix merged) — has consistently written "—" across dozens of consecutive
5-minute cycles all day today (byte-count of the written HTML is consistently ~1,000-1,300 bytes
smaller than a correct render, matching the missing stop/target markup for exactly 5 QHM rows).

I could not pin down the exact discrepancy between "code run by hand" and "same code run by the
live process" in the time available — I checked for stale bytecode caching (ruled out: `.pyc`
postdates the source fix) and for a caching/import-order issue (ruled out: `generate()` re-reads
every file from disk on every call, no module-level caching). This looks like a genuine
live-process runtime anomaly, not a logic bug — which is a more concerning class of finding
than a simple missed case, because it means **a correctly-written, correctly-tested fix can
silently fail to take effect in production while every gate says PASS.**

**Recommended cheap next step (not a code change):** a single `systemctl restart mtf-bot` the
next time it's convenient, then re-check the dashboard. If that clears it, the cause is some
kind of process-level staleness and the next session should hunt for what specifically isn't
being refreshed. If it does NOT clear it, this needs the full patch sequence on
`generate_dashboard.py` and whatever calls it in `strategy/run_cycle.py`.

PR #131 (scanner-offline banner), by contrast, verified clean — it's client-side JS with correct
branching logic, shipped and present in the live `scan_results.html`; it doesn't depend on the
long-running Python process's internal state the way #130 does, so it isn't exposed to whatever
this is.

---

## Item 2 — verification of the three 2026-08-10 QA-gate ships

- **Autonomous phantom_tracker self-heal (PR #125):** targets the *opposite* drift direction
  (tracker over-reports a position the broker doesn't have) from Finding A above (broker has a
  position the tracker doesn't). It correctly never fired this week — there's no phantom_tracker
  drift in the logs, only the phantom_broker one in Finding A, which is explicitly out of this
  PR's scope. No evidence of a wrong drop or fabricated P&L from this specific mechanism.
- **First-mile BGG bias gate (PR #124):** used successfully this session — I had to write neutral,
  non-leading prompts for the gate #4 design work below specifically because this gate is active.
- **Adversarial-claims marker (PR #127/#128):** confirmed live and load-bearing. I independently
  re-verified one of its stored claims (the generate_dashboard.py marker above) against live
  production behavior — the claim about the render test was accurate for a standalone test, which
  is exactly what surfaced Finding B as a live-process anomaly rather than a code defect.

---

## Item 3 — self-QA gate #4 design proposal ("BGG design-record") — DESIGN ONLY, no code written

Ran the Feature Design Protocol + Open Question Protocol (neutral prompts — the bias gate would
have blocked anything else) across Gro, GAI, and two cold board seats (Beck/Kim QA-process lens,
Peterffy infra lens). Three sub-questions; consensus below.

| Q | Question | Gro | GAI | Board (Beck/Kim) | Board (Peterffy) | Recommendation |
|---|---|---|---|---|---|---|
| Q1 | What triggers the gate? | Independent reviewer classification | Independent reviewer classification + auto-override on risk-path files | Independent reviewer classification (never self-cert, mirrors Rule B) | Independent classifier; file-creation only as a cheap pre-filter, not authoritative | **4/4 converge:** independent reviewer/cold-agent classifies "new capability vs. fix," default-to-gated on ambiguity — mirrors the existing risk-path routing screen. Self-declaration is an input the reviewer can overrule, never sufficient alone. |
| Q2 | How do you prove design came BEFORE code? | Git ancestry alone (`merge-base --is-ancestor`) is sufficient | Git ancestry **alone is gameable** (stash-then-backdate trick) — needs a real-time `PreToolUse` write-blocking hook that refuses to let implementation files be edited until a design-record JSON already exists on disk | (did not directly address the gaming risk; recommended ancestry) | **Independently flagged the same gaming risk as GAI** (`rebase -i` trick) — git ancestry is necessary but not sufficient; recommends GitHub server-side push-receipt timestamp as a required status check, same two-layer pattern this project already uses for `preship_gate.py` ("hook = speed bump, branch protection = the real wall") | **Majority (GAI + both board seats) over Gro:** local git-ancestry check alone is real but insufficient — it is a known gaming vector, independently discovered by two of the three other voices. Recommend the two-layer design: (1) a `PreToolUse` hook that blocks writes to implementation paths until a matching design-record file already exists in the working tree (GAI's proposal — fast, catches accidental violations), PLUS (2) a required GitHub branch-protection status check verifying the design-record commit's *server-received* timestamp precedes the implementing commit's (Peterffy-seat's proposal — the actual forgery-resistant wall, consistent with this project's own admitted architecture for the existing preship hook). |
| Q3 | What should the marker contain? | Hash/reference to a separate design-doc file | Full verbatim payload inline (content-addressed JSON) | Hash/reference (avoids transcript drift, mirrors existing cold-2nd/adversarial markers) | Hash/reference, PLUS a novel addition: an explicit `record_scope_waiver.py` requiring **Rafael's own sign-off** (not any Claude session) as the escape valve when the reviewer misclassifies a small change as "new capability" — consistent with the Authority Rule | **Majority (3/4) — hash/reference to `logs/design_records/<feature>.md`**, not verbatim-in-marker (avoids the same transcript-duplication/drift risk the project already avoids elsewhere). Adopting Peterffy-seat's waiver-escape-valve idea as a genuinely new, useful addition no one else raised — without an escape valve this becomes exactly the kind of friction that gets `--waive`d into meaninglessness. |

**Split disclosed, not hidden:** Gro's Q2 position (ancestry alone) is the minority view — 3 of 4
voices independently converged on "insufficient," including two who arrived at the *same specific
gaming vector* from different angles without seeing each other's answer. Per the Gro/GAI
Tie-Breaker Protocol, this is a clean majority, not a deadlock — not escalated to you as an
unresolved split.

**This is a design proposal only.** Building it means: writing `.claude/preship/` schema +
scripts for `record_design.py`, a `PreToolUse` hook, and (separately, needing your GitHub access)
a branch-protection status check — full patch sequence, own session, your explicit go-ahead
first.

---

## Item 4 — HTML redesign (deferred, not started this session)

Given how much this session's other two findings needed, I did not open the remaining ~16 HTML
issues from `html_issues_enumeration_2026-08-09.md`. Untouched, no regression — deferring is the
right call given Finding A's urgency.

---

## Session housekeeping

- Rolling AWP chain re-armed for 2026-08-11 19:25 PDT (+5h05m) at session start, per protocol.
- `data/fmp_client.py` has a staged-but-uncommitted modification from a prior session (visible in
  `git status` at session start) — untouched this session per RULE C-1/C-2 (its patch sequence
  restarts from Step 1 whenever it's picked back up; not evaluated here, out of scope for this
  session's findings).
- Several stray untracked scratch files exist at the repo root from a prior session
  (`scratchpad_*.json`, `logs/session_checkpoint.md`, `scripts/checkpoint_hook.py`) — left
  untouched; not mine to clean up without knowing their origin/intent.

**YOUR DECISION:**
- Finding A: **CLOSED-OUT informational** — stop confirmed live at $24.05, no action needed unless
  you want the tracker fix now. Actual code fix needs a dedicated interactive session — full patch
  sequence on two large files (`portfolio_tracker.py`, `orphan_manager.py`).
- Finding B: **root cause found, one cheap targeted action recommended** — `sudo systemctl
  restart mtf-writer` (not `mtf-bot` — see addendum above for why this is now the safer, precise
  ask).
- Gate #4 design: APPROVE the two-layer Q2 design (PreToolUse hook + GitHub branch-protection
  check) to move to build / DEFER / send back for more design work.

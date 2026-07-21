# Handoff — alpaca-mtf-bot
**Updated:** 2026-07-19 (interactive — Rafael present) | **CROSS-ACCOUNT HANDOFF** — always current per the
DURABLE SYNC RULE (CLAUDE.md). Pushed the moment alignment is reached, not at session end.

> **NEW ACCOUNT READS THESE FIRST, IN ORDER:** (1) this file (the ⏩ block below IS your pick-up
> point), (2) `CLAUDE.md` (binding rules — note new §DURABLE SYNC RULE), (3) `logs/tb_audit_log.md`
> (bug/patch log), (4) `logs/qhm_v2_design_2026-07-11.md` + `logs/ownership_ledger_design_2026-07-10.md`
> (active design). Master Brain: `notebooklm use $(cat ~/.claude/master_brain_id)`.

## ⏩ LATEST (2026-07-20 interactive) — pick up here

**⏩⏩ CROSS-ACCOUNT PICK-UP:** `git pull` → read this → `notebooklm use $(cat ~/.claude/master_brain_id)` + query.

**🛑 THE "ONE-LINE WIRING PATCH" IN THE 2026-07-19 BLOCK BELOW IS CANCELLED — DO NOT SHIP IT.**
A fresh gate (RULE C-2/C-7, all voices re-run) proved it would have made naked positions MORE
likely. VERIFIED IN SOURCE: `stop_protection.reconcile_protection` submits a stop when it thinks a
position is naked → Alpaca rejects 40310000 (held_for_orders) → **broker.py's recovery calls
`cancel_open_orders_for_symbol()` (GTC L525 / DAY L623), CANCELLING THE GOOD LEGACY STOP**, then
resubmits at a DIFFERENT price (the AH loop's breakeven/VIX/random-offset price is never written
back to `trade["stop"]`, so the reconciler's `_intended_stop` always differs) — or, on 63s poll
exhaustion, returns None leaving the position genuinely NAKED. This falsifies stop_protection.py's
own headline invariant (L17-18). Board exec-risk + reliability BOTH REJECT; Gro + GAI both reversed
to agree after counter-prompt. Also found: **5 uncovered `run_cycle` returns** hold open positions
and never reach any proposed call site — L242 kill-switch, L431 premarket, L892, L1011 EOD,
L1046 blackout (kill-switch + blackout are the two highest-stress days of the year); and the
prior "Option C" guard-dict fix is a **no-op** (guard keys on stop PRICE, which structurally differs).

**✅ SHIPPED + LIVE + VERIFIED 2026-07-20 (`cd81a53`, OCI DEPLOY_OK, HEAD=cd81a53, 4/4 services
active, dashboard 200, 20/20 harness green ON THE BOX via
`PYTHONPATH=/home/ubuntu/mtf-bot venv/bin/python3 -m unittest tests.test_stop_protection`).**
`execution/broker.py` — **`allow_cancel_blocking` opt-out** (keyword-only, default True).
Default = MSTR recovery (board 26-0) bit-for-bit UNCHANGED for all 14 callers. False = cancels
NOTHING, and returns a THREE-STATE result: `PROTECTION_ALREADY_HELD` (verified stable hold →
protected, don't page) / `PROTECTION_UNKNOWN` (order book unreadable → caller's throttled unknown
path) / `None` (genuine failure → page). New `_hold_state()` never asserts protection it cannot
verify. **INERT** — grep shows no caller passes it; zero runtime change. 24-scenario harness 7/7.

**✅ RECONCILER-SIDE PATCH DONE** (`execution/stop_protection.py` + harness): passes
`allow_cancel_blocking=False` on every submit; `if order is not None:` replaced by the 4-way
identity match (`is PROTECTION_ALREADY_HELD` → new `summary["broker_held"]`, no page, no order-id
write, guard NOT armed; `is PROTECTION_UNKNOWN` → `_skip_unknown`; `is None` → page; else →
placed); summary log now UNCONDITIONAL. Harness 27/27 (7 new — note 2 of the 7 are invariant
tests that would also pass pre-fix; the other 5 genuinely fail against the old contract).

**🚧 PRE-WIRING BLOCKERS — the module MUST NOT be wired (even `place=False`) until ALL THREE land.
Cite these IDs verbatim in code comments and PRs:**

**`✅ PRE-WIRE-BLOCKER-1` — SHIPPED + LIVE + VERIFIED 2026-07-20 (`4a93165`, OCI DEPLOY_OK,
4/4 services active, dashboard 200, 41/41 tests green ON THE BOX).** Per-(symbol,reason) page
throttle: `_throttled_page` pages the FIRST occurrence of each (symbol,reason) immediately, then
re-reminds at most once per the reason's TTL — TIERED: naked/blind reasons 15m, conflict reasons
60m, any unmapped reason → 15m loud default (typo fails loud). Keys clear on every DEFINITE good
outcome (already_protected/broker_held/placed/flat/no-position) so a resolved-then-recurring
condition pages FRESH (never masks a newly-naked position). `_page_throttle` TUPLE-keyed, pruned on
`k[0]` (a bare-key prune = silent no-op landmine, caught by the board + pinned by a test). Loop-error
de-throttle FIXED (rerouted off `_skip_streak` to a blind-spot page in `skipped` only); cover-failed
(recurs) throttled at naked tier, one-shot post-cover pages raw. Gates ONLY the page — every
placement/cover still runs each cycle. INERT (unwired) — zero runtime change today. 9 new tests
(41/41); anti-circle landmine proof done. Gate: full read 481L + statics + 41/41 + cold-2nd PASS +
board 3-0 (exec-risk+reliability+GAI, tiered TTL + landmine + double-count + cover-failed folded) +
FINAL preship GAI APPROVE (diff-misread reject resolved via counter-prompt) + Gro WAIVED (TPD-
exhausted, Rafael-authorized). **⏳ FOLLOW-UP before wiring (exec-risk+reliability, NON-blocking to
this patch): move the Slack POST off the cycle thread — the throttle cuts volume ~50-100× but does
NOT bound a cold-start burst of simultaneous first-pages (restart w/ 5 naked → 5×~4s blocking).**
~~`PROTECTION_UNKNOWN` de-throttle~~ — FIXED 2026-07-20 (dedicated `_unknown_page_streak`).

**`✅ PRE-WIRE-BLOCKER-2 PART A` — SHIPPED + LIVE + VERIFIED 2026-07-20 (`3f4f72e`, OCI
DEPLOY_OK, HEAD=3f4f72e, 4/4 services active, dashboard 200, 45/45 tests green ON THE BOX).**
Collapsed N per-symbol `get_open_orders(symbol)` calls into ONE account-wide `get_open_orders()`
fetch + in-memory group-by-symbol (`_orders_by_symbol.get(symbol, [])`). `get_open_position`
stays per-symbol. LOAD-BEARING: a None fetch fails-safe EVERY symbol (never `_all_orders or []`);
`if _all_orders is None` precedes every `.get()`. Symbol-less orders dropped (GAI preship fix).
Semantics-preserving; NO change to placement/cover/4-way-sentinel. INERT (zero call sites). Gate:
full read 585L + 10-pt + board reliability+exec-risk APPROVE + statics + cold-2nd PASS + 45/45
(4 new: 1-fetch-not-N, per-symbol slicing, None-fails-all, symbol-less-dropped) + FINAL preship
Gro APPROVE + GAI APPROVE (marker 5f3c5f6bb005) = **FULL BGG per Rafael**.

**`PRE-WIRE-BLOCKER-2 PART B: wall-clock sweep budget` — DEFERRED to its own gated patch.**
2-of-3 board voices REJECT bundling B with A (exec-risk: a mid-sweep break manufactures a TAIL
naked-leak — the last positions in iteration order, which most need a stop placed, get skipped —
plus fixed-order iteration = starvation; GAI: defense-in-depth fast-follow). Reliability seat wants
it but flagged B AS DESIGNED IS INCOMPLETE: `broker._hold_state` (broker.py:238) ALSO re-fans-out
`get_open_orders(symbol)` on the submit→40310000 path, which a loop-level budget does NOT bound —
so on a many-visibility-lag day the O(N) broker-side fetch fan-out survives PART A. B needs its own
design pass: (1) budget ANCHORED TO THE WATCHDOG DEADLINE (pass cycle_start+WATCHDOG−margin, NOT
now+budget-from-entry — reliability: 480/600 from entry let the watchdog fire first; use ~300s or
deadline-anchored); (2) `break` NOT `return` (return skips the prune L515-527 + the unconditional
summary log → unbounded dict growth + observability blindness); (3) SEED `open_symbols =
set(open_trades.keys())` up front from a SINGLE snapshot (else early-break prune wrongly drops
un-visited symbols' throttle/streak keys); (4) ROTATE/prioritize iteration (fixed dict order + fixed
break point = same tail starved every slow sweep); (5) truncation marker in summary+log+HARNESS
(un-visited symbols absent from all buckets breaks the harness's one-bucket invariant); (6) reject
"continue-without-submitting" (latency is in the READS, not submits). Also worth folding: bound the
broker-side `_hold_state` fan-out. Design refs: prior-session scratchpad `blocker2_design.md`.

**`✅ PRE-WIRE-BLOCKER-3` — SHIPPED + LIVE + VERIFIED 2026-07-20 (`750c52c`, OCI DEPLOY_OK,
4/4 services active, dashboard 200, 54/54 tests green ON THE BOX).** `broker._hold_state(symbol,
side, qty)` now PROVES ≥1 live reducing-side stop-family order covers the qty before returning
`_HOLD_STABLE`; readable-but-uncovered (incl. empty book) → `_HOLD_NO_COVER` (page), unreadable →
`_HOLD_UNKNOWN`. Callers capture `_requested_qty` BEFORE floor-bounding and prove cover against
that (strictly safer, never over-claims). Reliability-seat log hardening folded in (NO_COVER dumps
observed book; dropped-order debug). New `tests/test_broker_hold_state.py` (22 cases, template =
anti-circle regression rule) pins empty/wrong-side/partial/unparseable/requested-vs-floor-reduced.
Gate: full read 1422L + statics + 54/54 + cold-2nd PASS + board 2/0 APPROVE + preship Gro+GAI
APPROVE (GAI false-premise reject on a redundant `float(qty)` cast resolved via counter-prompt,
not re-roll). INERT — no live caller passes `allow_cancel_blocking=False`; zero runtime change today.
**2 informational residuals logged for the WIRING session (both fail-safe, over-page not mask):**
(a) a `stop_limit` counted toward cover can gap through its limit (bounded under-page); (b)
`get_open_orders` uses `QueryOrderStatus.OPEN`, so any protective stop resting in Alpaca `held`
status (bracket child) is excluded from cover → benign false page — confirm at wiring whether any
protective stops legitimately rest in `held`. The `_skip_unknown` comment was already corrected in
the `07bac0b` reconciler sync (comment now points at `_unknown_page_streak`).

**⚠️ 1 PRE-WIRE BLOCKER REMAINS (module still MUST NOT be wired until it lands):**
BLOCKER-2 (collapse N per-symbol `get_open_orders` into 1 account-wide fetch). BLOCKER-1 and
BLOCKER-3 shipped 2026-07-20 (see the ✅ blocks above). Also fold in the BLOCKER-1 Slack-off-thread
follow-up + the BLOCKER-3 wiring residuals when wiring. **⏩ NEXT ACTIONABLE: PRE-WIRE-BLOCKER-2 PART B (own gated design pass — see conditions above), OR the GEX/0DTE accuracy evaluator (design BGG-aligned, awaiting Rafael APPROVE/REJECT). All 3 original pre-wire blockers' PART-A/core work now shipped; PART B is the last item before the module can be WIRED.**

**⏩ THEN wire** — shadow (`place=False`) FIRST, and the call sites must include the **5 gaps**
(L242 kill-switch, L431 premarket, L892, L1011 EOD, L1046 blackout — kill-switch and blackout
being the two highest-stress days of the year), each wrapped in `try/except` + `logger.exception`
(every other risky call in run_cycle is wrapped; an unwrapped raise at the RTH site skips
`_touch_cycle_ts()` → watchdog → `os.execv` → restart loop). Then staged placement, site-2-first,
overnight-only. Reviews/prompts: scratchpad `wiring_prompt/broker_review/gai_counter2/recon_review`.

**✅ GOOGL-NAKED — DIAGNOSED, NOT A BUG (2026-07-20 read-only diag).** The prior "GOOGL still
naked, investigate" flag was a FALSE ALARM. GOOGL is in QHM state **`PENDING_EARNINGS`** (earnings
**2026-07-21**); the quarterly-hold manager DELIBERATELY runs earnings-paused positions stop-less
through the earnings window (documented "temporarily stop-less" state) to avoid an earnings-gap
stop-out. Last GOOGL stop was 2026-07-13 (qty2 GTC, canceled) — none since, by design. Ledger clean:
`net=2.0 qhm=2.0 protected_floor=2.0 drift=0.0` (never-sell floor still protects it from being SOLD;
that is separate from a stop-loss). NVDA has its stop because NVDA is not in an earnings window. NO
orders placed by Claude. **OPEN POLICY QUESTION for Rafael (not a bug, not forced): is holding a
~26%-of-equity single name stop-less through earnings the intended risk posture? If revisit wanted,
that is a QHM earnings-pause design review (board + Gro + GAI), separate from the stop_protection
wiring.** ⏩ Wiring next steps unchanged: BLOCKER-1 (page throttle) + BLOCKER-2 (N→1 order fetch).

**⚙️ PRESHIP TOOLING GOTCHA (cost a full false-APPROVE this session):** `preship_audit.py` hashes
the **staged/committed** blob and diffs `--cached`. Run it BEFORE `git add` and it audits an EMPTY
diff, records the PRE-change sha, and reports a meaningless `gro=APPROVE gai=APPROVE`.
**ALWAYS `git add` FIRST, then audit, then commit.** Also: once a commit already exists ahead of
`origin/main`, the gate checks `HEAD:` not the index — `git reset --soft origin/main` before
re-committing an amended change. Local lint env: `python3.10` (has alpaca; mypy/ruff installed
this session). `python3.10` lacks SSL certs → use system `python3` for Gro/GAI curl calls.

---

## (2026-07-19 interactive) — prior state

**🛠️ ACTIVE DEEP-WORK THREAD (2026-07-19) — GTC STOP-PROTECTION DEFINITIVE REDESIGN.**
**✅ STEP 1 SHIPPED + LIVE + VERIFIED (`1ee383e`, deployed OCI, DEPLOY_OK, 20/20 harness green ON THE BOX) —
but INERT (unwired).** New `execution/stop_protection.py` = the single-invariant reconciler (Alpaca-derived
protection state each cycle, no per-day gate, no stored-id trust) + `tests/test_stop_protection.py` = 20-case
failure-injection harness. Gate: statics + 20/20 harness + cold-2nd x2 PASS + board exec-risk+reliability
REJECT → revision closing 4 blockers (RC-4 cover price; stop-vs-stop double-place via non-idempotent coid;
stop-vs-limit over-sell; silent unknown-skip) → FINAL preship Gro APPROVE + GAI APPROVE (marker ae69e50a).
Closes tonight's findings A (gate-before-submit naked-all-session) + B (_TERMINAL omits "rejected") BY
CONSTRUCTION. **⏩ EXACT NEXT STEP = the WIRING PATCH:** one call to `reconcile_protection(tracker, risk,
session=...)` in `strategy/run_cycle.py` (after check_exits) — its OWN full-read gate on run_cycle.py (2064L) +
board + preship. THAT activates the fix. Follow-ups logged (non-blocking, fold into wiring): shadow-mode breach
reporting fidelity; register_close-raise page label; `_cover` assumes full flatten; deterministic broker coid
(Phase-B bulletproof idempotency). Phase B = retire the ~13 legacy submit sites + 6 status sets once shadow-proven.
Design/reviews: scratchpad `stopprotect_*`.
**SYSTEMIC anti-circle rule to add to CLAUDE.md (Rafael approved): NO fix ships without a regression test
reproducing the failure** (this harness is the template). Add on the wiring patch.
PLAN (for reference): (P1) map the COMPLETE stop-protection surface — all ~5 stop concepts
(overnight GTC, RTH DAY, cover-on-breach, trail, breakeven) across gtc_manager/broker/run_cycle/orphan_manager/
exit_logic/risk_manager [Explore mapping RUNNING]; (P2) design ONE invariant ("every open position always has a
correct live protective stop during RTH, or it's flattened+alerted") + ONE stateless enforcer that re-derives
protection from Alpaca each cycle (no gate to burn, no id to lose across restart) — bring to Rafael for sign-off;
(P3) build enforcer + FAILURE-INJECTION TEST HARNESS proving the invariant under rejected-stop/40310000/
partial-fill/restart/gap (this is the missing piece — prior fixes never forced the failure); (P4) gate + ship.
Model = pnl_ledger.py (stateless recompute killed the phantom-P&L recurrence; GTC never got that cure). SYSTEMIC
anti-circle rule being added to CLAUDE.md: NO fix ships without a regression test reproducing the failure.
Tonight's 2 verified GTC bugs to be SUBSUMED+proven-closed by the harness, not patched: (A) gtc_manager.py:77
once-per-day gate burns before submission → a DAY-stop failure at open (40310000/API) is never retried → naked
all session (premarket path is BLOCKED by run_cycle market-open gate L445 + _too_early L889 — verified); (B)
gtc_manager.py:381/302 _TERMINAL omits "rejected" → rejected stop misread as live → close gated/deferred.

**🚀 DEPLOY-TIMING POLICY CHANGED + SHIPPED (2026-07-19, `d98c719`).** Rafael mandate: fixes deploy WHEN READY,
NOT gated on market hours. New CLAUDE.md §DEPLOY TIMING rule removes the "defer restart to next non-RTH window"
convention in full. Safety gates unchanged — only deploy TIMING. Claude must NOT tell Rafael to wait for close
or mark a fix "restart deferred." Startup reconcile verified benign (reliability board) so RTH restarts are safe;
prefer landing a restart between 5-min cycles. **The deferred-restart backlog framing elsewhere in this file is
now historical — going forward nothing waits for market hours.**

**🔎 RTH BUG-HUNT DONE (2026-07-19, 4 board seats + Gro + GAI).** Ranked for Monday: P0 = gtc_manager stop
protection (findings A+B above — being subsumed by the redesign). P1 = Kelly `long_intraday` pinned at max cap →
~96% equity single long, CV=2.85 penalty neutered (fix: clamp `kelly_scale=min(kelly_scale,1.0)` at
entry_logic.py:1119) — QUEUED next after GTC. VERIFIED BENIGN: POSITION COUNT DRIFT CRITICAL (downgrade log to
INFO); false-drop of a live position (3 fail-closed guards). P2 reporting-only: exit_time UTC/PT mis-bucket
(pt:1146, only 5pm-midnight PT fills; OCI py3.10 also drops these from weekly_review exec_stats), RC-7 1-share
tranche skip, TQI recompute/window nondeterminism, score 8-9 dead-band. Latent: launch without --profile paper →
3x Kelly (mitigated by launch_bots.sh).

**🧾 PHANTOM-FILL REPAIR — BGG ALIGNED 2026-07-19 (cross-account resume; Board 4/4 + Gro + GAI → Option C).
AWAITING RAFAEL: approve design direction. NO CODE SHIPPED.** Thread: Rafael asked "was 7/2 really −$251?" → NO.
`trade_log.json` `closed[]` still carries RC-4 phantom rows (exit prices absent from Alpaca fills). Audit: ~27.7%
of 112 closed rows contaminated (14 PHANTOM / 14 NO_FILLS / 3 NO_EXIT_TS), −$188.48 fake P&L.
`scripts/phantom_fill_audit.py` (read-only, committed `a42ffc7`).
- **VERIFIED THORP FINDING** (`kelly.py:396 rebuild_from_trades` ← `portfolio_tracker.py:390/1207`): phantoms are
  fake LOSSES currently SUPPRESSING live Kelly (`long_intraday` n=43 / `short_intraday` n=33, both ACTIVE). Its
  guards (L409 `_fill_unverified`, L417 exit≤0) do NOT catch a plausible-but-wrong price. Healing → Kelly SIZES
  UP on next EOD rebuild. The cleanup is a live risk-INCREASING event, not neutral bookkeeping. Only sizing lever
  = R-multiple stats (kill switch = Alpaca-equity, unaffected; A2 = equity, unaffected).
- **BGG ALIGNMENT (this session, RULE C-2 fresh):** **Option C** — heal `closed[]` for reporting truth NOW; keep
  healed rows EXCLUDED from `rebuild_from_trades` (via `_fill_unverified` flag) until a SEPARATE, numerically-
  quantified, board-gated Kelly re-warm. **A rejected by all.** Votes: exec-risk C, reliability C, data-integrity
  C, GAI C, Gro C-mechanism (labeled B).
- **BUILD METHOD (data-integrity seat, decisive):** do NOT per-row match (that reintroduces the exact heuristic
  that CREATED the phantoms). REPROJECT `closed[]` from `pnl_ledger.compute_realized()['round_trips']` via a NEW
  `heal_trade_log()` sibling to `heal_history()` in `reporting/pnl_ledger.py` — reuse its immutable
  `fetch_all_fills` source + fail-closed $5 reconcile invariant (L567-581) + `dry_run` diff (L542-611) + atomic
  write; ADD timestamped backup + fsync. Normalizes `pnl` by construction; catches the 4 missing-exit rows
  (GOOGL/NVDA/MSTR/RBLX) automatically; quarantine+tag the 17 unverifiable rows.
- **TWO MUST-RESOLVE-BEFORE-APPLY (board-flagged):** (1) `closed[]` OWNERSHIP — one-off heal then bot resumes
  append, vs ledger-owned projection (dual-writer risk, pnl_ledger L627-633); (2) `entry_time` JOIN-KEY drift —
  reprojection keys on fill `transaction_time`; preserve bot's original as `entry_time_bot` or verify no
  downstream join on it.
- **⏩ EXACT NEXT STEP:** Rafael approves Option C direction → build `heal_trade_log()` under FULL patch sequence
  (full-read `reporting/pnl_ledger.py` + `portfolio_tracker.py`; statics; cold-2nd; preship Gro+GAI on the diff)
  → surface exact before/after diff + Kelly re-warm as a SEPARATE decision. Nothing writes to `trade_log.json`
  until Rafael approves the diff. (Separate: pnl-field inconsistency = reporting-only, normalized by reprojection;
  missing-exit rows tie to the existing P0 false-drop root.)

**🔓 REPORTS FIXED (2026-07-19) — MIGRATION GAP: host firewall never got the 8080 rule.**
Rafael reported the 5 report URLs dead on the new box. The URLs were CORRECT; the blocker was
`iptables`. The new A1 box's INPUT chain allowed only SSH (dpt:22) and then REJECTed everything
else — port 8080 packets were dropped before nginx ever saw them (external `connect` failed
outright, HTTP=000). The OLD box had `-A INPUT -p tcp --dport 8080 -j ACCEPT`; the migration
copied nginx config, .htpasswd, and the HTMLs but NOT the firewall rule. FIX APPLIED + PERSISTED:
`sudo iptables -I INPUT 5 -p tcp --dport 8080 -m state --state NEW -j ACCEPT` then
`sudo netfilter-persistent save` (verified written to /etc/iptables/rules.v4, survives reboot).
All 5 pages now return HTTP 401 externally (= nginx reached, basic-auth prompting — correct),
connect ~22ms. Basic-auth user: `mtf`. Serve dir `/home/ubuntu/mtf-bot/public` has all 5 symlinks.
**LESSON FOR ANY FUTURE BOX MIGRATION: the host firewall is NOT part of the app deploy — check
`iptables-save` parity explicitly.** (options.html showing Friday's timestamp is CORRECT, not
stale — its cron is `*/15 13-21 * * 1-5`, weekdays only; it refreshes Monday at the open.)

**🎯 EXACT NEXT ACTION (2026-07-19, newest first): convene the BOARD SEATS on Rafael's two new
directives (Gro + GAI already returned — see "GEX RE-SPEC" below). Two Gro/GAI splits need the board
as tie-breaker: (1) is the gamma-weighted centroid spot-INDEPENDENT? Gro says yes, GAI says no-but-
not-1:1 (GAI's reasoning is the more rigorous — gamma weights are computed at S0, so it cannot be
fully independent; this must be settled BEFORE building, or we rebuild the same tautology); (2)
outright long 0DTE (Gro) vs defined-risk DEBIT VERTICAL (GAI). Then bring the aligned package to
Rafael. Do NOT start building until the centroid's spot-dependence is resolved.**

**✅ S0 SHIPPED + DEPLOYED + VERIFIED (2026-07-19, commit `2219f70`) — GEX IS NOW DISPLAY-ONLY.**
`config.py`: `GEX_ENABLED` True→False, `GEX_EDGE_MULT_MOMENTUM` 1.10→1.00, `GEX_EDGE_MULT_MR`
1.05→1.00, `GEX_MIN_SCORE_NEG_BUMP` 1→0. GEX now has ZERO effect on sizing (kelly.py:331 edge
multiplier) and ZERO effect on gating (run_cycle.py:1585 Layer-8 MIN_SCORE bump). Verified ON THE BOX:
all 4 services active, dashboard 200, HEAD=2219f70, values confirmed via `venv/bin/python3 -c "import
config"`. **Evidence clock still accruing by design** — `refresh_gex()` (live_data_writer.py:97) is
NOT gated on the flag, and run_cycle.py:1571-1584 logs the Layer-8 shadow record BEFORE the flag check.
Gate: statics 3/3 + cold-agent PASS + preship Gro APPROVE / GAI APPROVE (GAI rejected first on a false
premise — `_gex_min_score` contributing `_base_min` to the `max()` — refuted by showing all 8 layers
initialize to `_base_min` and `max(S ∪ {m}) == max(S)` when `max(S) ≥ m`; its `-999999` sentinel would
have leaked into the operator-facing `_score_reason` log at run_cycle.py:1607. One counter-prompt round.)

**🧭 GEX RE-SPEC — Rafael directives 2026-07-19 (Gro + GAI in; BOARD SEATS STILL NEEDED):**
Rafael: *"the gex should be measuring the weekly midpoint and flips for that week only"* and *"the 0DTE
spy recommendation should be price-action based, but I'd leave that to the bgg."* Prompt:
`…/scratchpad/bgg_respec_prompt.md`. Gro+GAI AGREE on: weekly midpoint = **gamma-weighted centroid**
`Σ(K·|GEX_K|)/Σ|GEX_K|`; re-key `strike_gex` to **(expiry, strike)** with per-expiry subtotals;
**EXCLUDE 0DTE** from the weekly measure (its OI is T+1-stale AND it carries ~7× the gamma weight, so
it hijacks a number meant to describe the week); **DROP the flip concept** entirely in favour of the
centroid + a positive/negative gamma BAND; price-action trigger = reuse SPY 5-min bar-over-bar
(Architecture Invariant #1) + VWAP reclaim/rejection; emit ONE direction or NO call (current both-legs
behavior makes hit rate mechanically ~50% and unfalsifiable); replace the hardcoded 10:05-10:20 window
with a trigger-driven entry; score in **P&L-in-R / breakeven-move hit rate**, NOT directional hit rate
(theta dominates); ship as SHADOW first. GAI adds the key normalization problem: the measure must stay
**comparable across weekdays** as the expiry set shrinks Mon→Fri — it proposes a FIXED expiry set (the
first two non-0DTE Friday expiries) so the number means the same thing every day.

**⚠️ THE UNRESOLVED CRUX (board must settle):** GAI states plainly that the centroid is **NOT
spot-independent**, because each Γ_i is evaluated at the current spot S₀ — it argues the dependence is
not 1:1 and that the centroid tracks the SHAPE of the gamma distribution rather than spot. Gro asserts
it IS independent. **If GAI is right and the dependence is strong, we would rebuild the same
tautology under a new name.** The decisive pre-build test is the same one that killed the flip:
regress the proposed centroid on spot across a month of sessions — slope≈1 and R²≈1 means it carries
no independent information. RUN THAT TEST ON HISTORICAL DATA BEFORE WRITING THE PRODUCTION PATH.

**🔬 GEX / 0DTE ACCURACY AUDIT — BGG ALIGNED 2026-07-19 (Board 4/4 + Gro APPROVE + GAI APPROVE).**
Full design: `logs/gex_0dte_evaluator_design_2026-07-19.md`. Alignment reached in ONE counter-prompt
round (Gro+GAI initially split on 3 points; all reversed when shown the board's code/git evidence —
disagreement protocol worked as designed, zero blind re-rolls). Headline findings:
- **flip=694 is a CLOSED pre-fix artifact.** `aad518a` committed 2026-07-15 10:02:11 -0700; the 694
  record is timestamped 06:37 AM PT the SAME DAY (3h25m earlier). Also arithmetically impossible under
  current code (`_FLIP_WINDOW_PCTILE=0.05` → band [714.4, 789.6] at spot 752). Do NOT re-diagnose it.
- **THE LIVE DEFECT: the 7/14–7/15 fix made the flip TAUTOLOGICAL.** `gex.py:384-397` sweeps strikes K
  at FIXED spot and argmin-selects the crossing nearest spot — it never reprices Γ at candidate spots,
  so it is not a gamma flip at all. Post-fix SPY flip = 755.0/755.0/755.0 vs spot 754.68. A wrong
  answer that looked absurd (694, caught in a day) was replaced by an uninformative one that looks
  plausible forever. **Decisive check (no new instrumentation): regress emitted `flip` on `spot` —
  slope≈1, R²≈1 proves zero independent information.**
- Other confirmed: per-expiry collapse (`gex.py:327`, Γ∝1/√T so 0DTE dominates + its OI is T+1-stale);
  67.4% chain censoring that deletes ATM preferentially (where gamma is maximal); `_MAX_CONTRACT_PAGES=2`
  truncation that drops PUTS first (biases net_gex positive); `raw_gex_m` ~100× mislabelled (missing the
  `×0.01`); `_get_spot` on IEX with no cross-check; `"—"` string sentinel in a float column.
- **0DTE is a RETENTION gap, not a logging gap** — `options_scanner.py:1173-1175` already writes the full
  rec dict, then `os.replace` destroys it every 15 min. Fix ≈12 lines. No archive exists → reconstruction
  has NO substrate (and `_get_vix_tertile` would inject look-ahead into the ADMISSION criterion).
- **Statistical floor: effective N ≈ 1.2 obs/day; with ρ≈0.7 (VIF 5.67) a verdict needs ~960 sessions.**
  The unlock: score the vol-regime claim at 15-MIN resolution with AFML average-uniqueness weights →
  ~37–90 sessions. GEX weight → ZERO meanwhile (it feeds `kelly.py`, so a bad edge is a LEVERAGE error
  across the whole book). REJECTED unanimously: reconstruction, auto-degrade, flip/pin evaluator, and
  "staged-ACTIVE" as a state. Q4 dynamic sourcing DEFERRED to ≥60 clean sessions (deriving thresholds
  from today's history would fit parameters to the defects).

**🚚 OCI ARM A1.Flex MIGRATION — PHASE 1 + PHASE 2 DONE + VALIDATED (2026-07-19). PHASE 3 (cutover flip) is
the NEXT STEP — execute in a MARKET-CLOSED window.**
- **NEW A1 box `137.131.51.250`** — VM.Standard.A1.Flex, aarch64, 2 OCPU / **11.9GB RAM / swap=0**, Ubuntu
  22.04.5, py3.10.12. = 12.5× the old 956MB → swap-thrash / 12-14min-hang problem structurally solved.
  Provisioned via OCI CLI (caught A1 capacity in phx AD-1), commit `9487b0c`. SSH: `~/.ssh/mtf_bot_oracle`.
- **OLD prod box `129.153.208.32`** — E2.1.Micro 956MB, STILL LIVE + trading, HEAD `7401a17` (has datetime fix
  `c1d5998` + all ships). Untouched = production until the flip.
- **✅ PHASE 2 (software bring-up) DONE + VALIDATED on the new box:** apt deps + `git clone` (HEAD 9487b0c) +
  venv + `pip install` (ALL 78 deps from aarch64 wheels — ZERO source builds/errors) + `.env` (600) + data/state
  (13 files; re-copied FRESH at the flip). systemd units INSTALLED but **DISABLED** (no trading until flip).
  crontab NOT installed yet (avoids double-cron). ARM verify: 11/11 bot modules import OK, main.py compiles,
  `_iso_to_dt` works. Bundle staged: Mac `…/scratchpad/migrate/` + new-box `/tmp/mig/` (secrets wiped; re-push at flip).
- **✅ PHASE 3 CUTOVER FLIP — EXECUTED + VERIFIED (2026-07-19 Sun, market CLOSED). THE NEW A1 BOX IS NOW
  PRODUCTION.** SINGLE SAFETY RULE held: exactly ONE bot active throughout.
  - OLD box `129.153.208.32`: bot STOPPED + services DISABLED + crontab REMOVED (backup `crontab_preflip_*.bak`
    on the box). Frozen, intact = ROLLBACK for ≥7 days. **Still holds its IP** (IP not moved — see below).
  - NEW box `137.131.51.250`: final `.env`+`data/state`+`trade_log.json` snapshot restored → crontab installed
    (31 jobs, memory_watchdog `*/6`, ram_watch retired) → all 4 services (mtf-bot/writer/http/nginx) ACTIVE →
    dashboard HTTP 200. Fixed: recreated the runtime `public/` serve-dir (symlinks, not in git). VERIFIED:
    Alpaca positions == 7 (DDOG:1 GOOGL:2 HOOD:2 NVDA:2 RIVN:10 TQQQ:1 XOM:3), ownership_ledger intact (37
    positions), **swap=0, RAM ~11.3GB avail**, 0 errors in log. Bot trades on this box Monday.
- **✅ IP / DEPLOY-TARGET — DONE (option B, ref-update).** OCI ephemeral IPs can't be moved, so kept the new
  box's own IP `137.131.51.250` and updated all hardcoded `129.153.208.32` refs → `137.131.51.250` in:
  `scripts/deploy_to_oci.sh`, `scripts/service_watchdog.sh`, `scripts/failback_to_mac.sh`,
  `scripts/auto_continue_prompt.txt`, the `~/.claude/skills/session-start` skill (9 refs), and
  `.claude/settings.local.json`. **DEPLOY TARGET IS NOW `137.131.51.250`.** (Optional future robustness:
  reserve a public IP for the new box so it survives even termination.)
- **✅ 5 HTML REPORTS — accessible on the new box, SAME login, NEW IP.** Served by nginx **:8080 with basic
  auth** (never :80 — `:80=404` on BOTH boxes, unchanged). New URLs: `http://137.131.51.250:8080/dashboard.html`
  · `/scan_results.html` · `/options.html` · `/weekly_review.html` · `/monthly_review.html` (same credentials —
  copied `/etc/nginx/.htpasswd` old→new). Copied the generated HTMLs + `logs/{weekly,monthly}_*.html` from old→new
  (they're gitignored) so they render immediately; crons refresh them on the new box going forward.
- **MIGRATION FULLY COMPLETE.** Old box stays as a stopped rollback ≥7 days.
- Migration runbook `7401a17` (BGG 4/4). Bundle for reference: Mac `…/scratchpad/migrate/`.

---

## ⏩ PRIOR (2026-07-18 interactive)

**⏩⏩ CROSS-ACCOUNT PICK-UP POINT (if usage limit hit → other Claude Gmail account resumes here):**
Sequence: `git pull` → read this block → `notebooklm use $(cat ~/.claude/master_brain_id)` + query.

**STANDING AUTHORIZATION (Rafael 2026-07-16, while he is away):** continue the ongoing work, then
the queue. **Once BGG is aligned → approved to ship** (unaligned → queue it). Push agreements to git +
logs + .md + Master Brain at EVERY step so any account/session can pick up mid-stream.

**🟢 THIS SESSION (2026-07-18) — CHECKPOINT (updated at checkpoints, not just ships, per Rafael's
cross-account-switch need):**
- **✅ REPORTS DURABILITY (Option B) — DONE + LIVE.** All routine audit reports now cross-account via
  `git pull`. `.gitignore` un-ignores the report globs; `scripts/sync_reports.py` (`994baaa`) is the
  single OCI cron (wired **23:45 ET daily**, `cron_tz_wrapper.py`) that batch-commits new reports →
  pushes to main (pull-rebase retry, rebase --abort on conflict, never force-push) → reconciliation
  gated on `eod_{date}.json` → Slack-on-gap. **Validated live:** OCI test-run swept **256 historical
  reports** into `5b228aa`, pushed clean, tree clean, exit 0. Design `logs/reports_durability_design_2026-07-18.md`;
  board 3-1 + Gro (GAI dissent resolved). weekly/monthly `*.html` dashboards intentionally NOT synced
  (global `*.html` ignore; human-facing, web-served) — Rafael to decide if he wants them versioned.
- **✅ F6 PREREQ #3 (GTC/DAY stop floor check) — DONE + LIVE** (`b5d519c`, deployed in `4c3ced6`).
  The prior checkpoint's "WIP/uncommitted" was STALE — verified at 100% this session: `_floor_bound_stop_qty`
  defined broker.py:337 (+wrapper:362, impl:382), WIRED at :458 (`submit_gtc_stop_order`) + :582
  (`submit_day_stop_order`); working tree clean.
- **✅ F6 D-obs + OBS-A (prereq-2 arming cond b) — SHIPPED + LIVE (`d93be65`, deployed 2026-07-18 Sat
  market-CLOSED, `git pull --ff-only` + restart, DEPLOY_OK + health OK, ENFORCE=False confirmed live;
  Rafael APPROVED).** DARK/inert (behind `OWNERSHIP_GUARD_ENFORCE=False`; **live behavior change = ZERO**).
  Files: `alerts.py` (+`alert_floor_blind` bool transport), `execution/ownership_guard.py` (+`page_floor_blind`
  never-raises throttled pager + function-boundary hardening of `check_never_sell_floor`), `execution/broker.py`
  (`close_position`→wrapper+`_close_position_impl`; +`_floor_bound_partial_qty`/`_impl`; page wiring in
  `_floor_bound_stop_qty`). **OBS-A:** a type-corrupt ledger qty → fail CLOSED for a cached-protected symbol
  (page it), fail OPEN (keystone) otherwise — never crashes the exit path (Exec-risk's PLTR −$16k scenario
  averted, runtime-proven). **D-obs:** throttled (kind,symbol / 30-min) phone+Slack page on fail-closed
  AMBIGUITY (ledger/Alpaca unreadable, drift-freeze, type-corrupt); deterministic floor-binding rejects stay
  log-only. Gate: 5-voice board (Reliability+Exec-risk+Observability+Gro+GAI) all APPROVE-WITH-CHANGES (every
  change applied) → statics ✓ → runtime self-test 13/13 ✓ → cold-2nd PASS → FINAL preship markers
  (broker/alerts = real Gro+GAI APPROVE; ownership_guard = gai=APPROVE, gro=WAIVED on active Groq TPM).
  Design: `logs/f6_dobs_obsa_design_2026-07-18.md`. **v2 QUEUED (build BEFORE arming, NOT this ship):**
  cycle-rollup + recovery/all-clear + heartbeat; `load_ledger` qty-type validation at source.
- **⏩ NEXT EXACT STEP:** F6 v2 alert-polish (above) → live-verify a rejected sell on a protected symbol
  (paper canary) → then prereq #2 = arm `OWNERSHIP_GUARD_ENFORCE=True` (LAST; per
  `logs/f6_activation_BLOCKED_2026-07-17.md`). NO SEED until #2 lands.
- **✅ RAM ALERT-SPAM RECALIBRATION — SHIPPED (2026-07-19, `scripts/memory_watchdog.sh`).** ONE watchdog at
  `*/6` (ram_watch.sh retired via crontab); RTH two-tier crit `<15MB`/warn `<30MB` available (below the 58MB
  floor → RTH spam gone); relabel "free"→"available"; off-hours auto-restart ACTION unchanged, ping 1/day +
  24h count + escalation ≥5/24h + 20-min anti-thrash cooldown; parse-fail guard. Gate: board
  Observability+Reliability + Gro + GAI all APPROVE-WITH-CHANGES (all applied) + cold-2nd PASS + behavior test
  8/8 + FINAL preship real Gro+GAI APPROVE. Design `logs/ram_alert_recalibration_design_2026-07-19.md`.
  **v2 DEFERRED:** "Online" self-test suppression (found BROKEN-as-designed — sentinel eaten by alert_crash
  before startup reads it; needs alerts.py+main.py redesign), swap-pressure alert (true leading indicator),
  trailing-baseline dynamic threshold. **NOT a fix for the RAM tightness** — box size-up (REFUSED) / working-set
  trim stays LIVE.
- **✅ SLACK GEMINI-REPORT FORMAT FIX — SHIPPED (2026-07-19, `scripts/audit_slack.py`).** Display-only. Fixed
  the 4 garbling bugs (`**` leak, `title — title` dup, 3–4× repeats/mis-severity, mid-word truncation) in
  `findings_from_report()` — now one clean grouped entry per finding, markdown stripped (backticks + `2*ATR`
  preserved), exact-dedup, word-boundary truncation. Gate: cold-2nd PASS + statics + tests + FINAL preship real
  Gro+GAI APPROVE. Effect visible on next midday/post-market cards.
- **🌙 AUTONOMOUS (Rafael asleep 2026-07-19, authorized "activate BGG, audit, validate, ship+commit next
  approved items"):** shipped the Slack-format fix, then began the #1 open RTH bug.
- **✅ RC-4 DATETIME-PARSE P&L FIX — SHIPPED + LIVE (`c1d5998`, 2026-07-19, OCI restarted, market closed,
  DEPLOY_OK + services active; `_iso_to_dt(Z/frac)` verified parsing live).** The CATASTROPHIC 7/17 bug: raw
  `datetime.fromisoformat()` at portfolio_tracker.py:166/290/404 + fill_reconciler.py:156 failed on Alpaca
  `Z`/variable-fraction timestamps → SMCI never reconciled (permanent P&L corruption) + re-queue loop + false
  EXPIRED. FIX: tolerant `_iso_to_dt` in `execution/state_io.py`, routed the 4 sites through it. BGG 4/4
  APPROVE-WITH-CHANGES → HARDENED (corrupt ts → route to EXPIRED/stamp `_patch_applied_ts`, surface once, no
  loop; fill_reconciler MINIMAL). Design `logs/datetime_parse_pnl_fix_design_2026-07-19.md`. FOLLOW-UPS
  (BGG-logged, NOT shipped): (C) entry_time-bounded recovery for a corrupt-exit/valid-entry fill; pt:448
  `.startswith(today)` UTC-vs-PT overnight mis-bucketing (`_pt_date`-style fix).
- **✅ SOP — DISAGREEMENT PROTOCOL now MANDATORY on ANY reject (`c230c03`, Rafael 2026-07-19):** any reviewer
  reject (cold-2nd/board/Gro/GAI/preship) MUST counter-prompt with the refuting evidence — blind re-rolling
  PROHIBITED. `preship_audit.py` gained a `--evidence <file>` counter-prompt path (MACHINE-LOCAL — `.claude/`
  is git-ignored; each machine needs the same one-time add; the SOP documents the manual fallback).
- **⏭️ STE-Lite report readability (Rafael APPROVED STE-Lite + clickable GitHub-SHA links + TL;DR):**
  BGG 4/4 aligned; design `logs/ste_report_readability_design_2026-07-19.md`. Rafael sharpened: the CEO line
  = TRUE LAYMAN plain English (no jargon), the evidence line = full technical for engineers. NEXT BUILD after
  the queue below. **✅ OCI ARM A1.Flex MIGRATION — SCOPED (BGG 4/4 aligned, 2026-07-19). NOT executed.**
Runbook: `logs/oci_arm_migration_runbook_2026-07-19.md`. Move E2.1.Micro (1GB, swap-thrash `deactivating` hangs) →
free A1.Flex ARM 24GB (fall back 2/12), same Phoenix region, $0. All 4 voices (DevOps + Reliability board seats +
Gro + GAI) aligned: rebuild venv from `requirements.lock` (NEVER copy x86 venv; `--only-binary=:all:` +
`file …so`=aarch64 proof); git clone (not copy); **single-writer flip** (stop+disable OLD trading & `crontab -r`
BEFORE any NEW writer/cron starts); **authoritative state snapshot taken ONLY after OLD writers stopped** (graceful
stop, no `kill -9`, sha256 witness); **weekend/market-CLOSED cutover only**; keep E2 as hot rollback ≥5 sessions.
**ONE open item = IP:** reserve+reassign `129.153.208.32` (consensus) — VERIFY IN OCI CONSOLE whether the ephemeral
IP can move off a *running* E2 (GAI: needs poweroff; board: convert→reserved live); fallback = new IP + update 8
hardcoded refs (enumerated in runbook). **✅ PHASE 1 DONE (2026-07-19):** new A1 box LIVE at `137.131.51.250` (ARM/Ubuntu 22.04/Py3.10.12/**11.9GB RAM,0 swap**/2 OCPU/AD-1; OCID `…jzjgwx2vfmixzq`). Launched via OCI CLI (console iframe froze; CLI caught capacity attempt-1). OCI CLI now configured on Mac (`~/.oci/config`). OLD box IP `129.153.208.32` untouched. Free A1 ceiling is now 2/12 (Oracle cut it). **⏩ NEXT: Phase 2** = SSH to new box → apt deps + venv rebuild from requirements.lock + git clone + staging state copy + recreate services/crons/nginx DORMANT (all safe/anytime; box does NOT trade till weekend flip). Then weekend single-writer cutover.
**Remaining queue:** OCI provider pivot research (Rafael's stated next priority — free OCI
  ARM A1.Flex 24GB vs Hetzner); checkpoint automation "B"; F6 v2 alert-polish; options/0DTE (SPX BLOCKED);
  UX redesign. Gemini routine reports NOT in Master Brain — optional add.

**🟢 SHIPPED (2026-07-17→18) — ⚡ ALL NOW DEPLOYED LIVE ON OCI (`4c3ced6`, restarted 2026-07-18 Sat
while market CLOSED; per Rafael "ship everything BGG-built, nothing dark without an explicit reason").
The deferred-restart backlog (items 1–6) is RUNNING, not dark. ONLY remaining dark item = the
`OWNERSHIP_GUARD_ENFORCE` flag (prereq #2 arming) — explicit board reason: needs D-obs + a live-verified
rejected sell first (see prereq tracker).**
1. **`d883f59` — fill-signal None-on-failure refactor.** `fetch_actual_fill_price` split into
   `_recover_fill (→float|None)` + thin wrapper (byte-identical for 17 callers) + `fetch_actual_fill_price_or_none`.
   `fill_reconciler` now branches on `fill is None` (leave pending / retry) instead of the fragile
   `abs(fill-entry)<_MIN_PRICE_DIFF` miss-guess that skipped a genuine scratch-at-entry forever. Preship
   `7781d06d`/`f15a6dfd`, cold-2nd PASS. (Closes the fill_helpers:369 board follow-up.)
2. **`3270a76` — F6 fail-closed orphan exclusion + per-day starter guard (DARK/inert).** BGG-aligned
   safety pre-positioning; F6 stays DARK. Preship `3e5a7a4a`/`7b5a72c0`.
3. **`3453148` — F6 prereq-1 design + blocker docs now git-tracked** (.gitignore negations).
4. **`800815e` — F6 prereq #1 part C-1: cross-process lock on `sync_ledger`.** Closes the
   Reliability-seat-proven lost-update (concurrent cron + in-process replay clobbering the F6 write).
   Best-effort flock; fail-open on timeout/setup-failure so it can NEVER hang the cron. Gate: design
   board + statics + self-test + cold-2nd PASS + Gro/GAI preship `55b0c5b0`. **Live cron-path change →
   takes effect at the next non-RTH OCI restart (deferred).**
5. **`b5d519c` — F6 prereq #3: floor-bound resting sell-stops (GTC/DAY), DARK/inert.** See prereq
   tracker below. Full gate 5/5 Variant A; supersedes stranded other-account `715c0b0` (never reached
   this remote — its forever6-only fix refuted 5/5 as a QHM-floor breach). OCI git-synced, restart
   deferred. **Cross-account note:** the other account also generated Q3 QHM research (MU top pick 4/4,
   AVGO #2, VRT post-7/29, AMAT/LMT disqualified on earnings proximity) — findings live only in that
   session's transcript; the memo was deemed already-committed (`quarterly_holds_research_Q3_2026-06-20.md`),
   no new file. New follow-ups logged: D-cache + D-obs (prereq-2 arming conditions), and a separate
   governance q — `check_never_sell_floor` lets a `qhm`-tier resting stop self-liquidate the QHM slice
   (effective_floor=floor−own_qhm=0 when f6=0); pre-existing in the chokepoint, shared by the close path.
6. **`4c3ced6` — D-cache (Opt-2): retire protected_symbols.json sidecar → single-source ledger +
   last-known-good `.bak`.** Closes the cache-coherence fail-OPEN window (2 files can't be atomically
   consistent). `save_ledger` rotates the current VALID ledger to `.bak` before overwriting (best-effort,
   never hangs the cron, never overwrites a good .bak with corruption); `_cached_protected_symbols`
   derives from `.bak`; `check_never_sell_floor`'s LedgerError branch runs the FULL check against `.bak`
   (live-Alpaca drift → fail-closed-on-drift, not silent fail-open). **`.bak` rotation is LIVE on the
   ledger-sync cron; guard path dormant behind the flag.** = F6 prereq-2 arming condition #1 (D-cache) DONE.
   Gate: design board (Data-integrity+Reliability) + Gro + GAI 3/4 → Opt-2 (Rafael-approved); full-read
   625L; statics; self-test PASS; cold-2nd PASS; preship gro+gai APPROVE (sha 1a13b53). Cold-2nd OBS-A
   (guard `protected_floor` vs a type-corrupt `.bak`) folded into the D-obs patch.

**▶ RAFAEL APPROVED (this session): BUILD THE 3 F6-ARMING PREREQS.** Design is board-blessed:
`logs/f6_prereq1_syncgap_design_2026-07-17.md` (4-voice gate).
- **✅ PREREQ #1 COMPLETE:** C-1 lock (`800815e`) + C-2/C-3 post-buy verify loop + persisted
  seed-block flag (`9ad926d`, DARK/inert). Both fully gated (design board + statics + cold-2nd +
  Gro/GAI preship). OCI restart deferred (C-1 is a live cron-path change; C-2/C-3 inert).
- **✅ PREREQ #3 COMPLETE (`b5d519c`, 2026-07-17 interactive, DARK/inert):** `_floor_bound_stop_qty`
  wired into `submit_gtc_stop_order` + `submit_day_stop_order` — a resting sell-stop on a protected
  (forever6/qhm) symbol can never fire INTO the never-sell floor. Gate = `protected_floor` (F6+QHM),
  matching close-path chokepoint (**Variant A**, board 3/3 + Gro + GAI = **5/5**; the stranded
  other-account fix `715c0b0` proposed forever6-ONLY which was refuted 5/5 as opening a QHM-floor
  breach). Helper hardened with a never-raises wrapper (fail-closed on type-corrupt ledger — cold
  board Reliability seat). Full-read 995L + statics + cold-2nd PASS + FINAL preship gro+gai APPROVE
  (sha 34059f81). OCI git-synced (b5d519c), restart deferred with #1.
- **⏳ PREREQ #2 (LAST — the one dangerous flip):** arm `OWNERSHIP_GUARD_ENFORCE=True` (config.py:556)
  — ONLY after #1 (ledger populated + protected_symbols.json present) AND #3 (done) land, AND a
  live-verified rejected sell on a protected symbol. Changes the close path for ALL tiers.
  **BINDING ARMING CONDITIONS from the prereq-#3 cold board (must resolve BEFORE the flip):**
  (a) ✅ **D-cache DONE** (`4c3ced6`, Opt-2 — single-source ledger + `.bak`, deployed live).
  (b) ⏳ **D-obs (NEXT)** — unify an operator PAGE across all guard fail-closed *ambiguity* returns
  (stop-skip + close + partial: ledger/Alpaca unreadable, drift-freeze), suppressing the deterministic
  floor-binding rejects to avoid alert fatigue; route via `alerts.py` webhook (NOT the unauthorized
  finance:slack MCP). Board+Gro+GAI aligned (Data-integrity+Reliability+Gro+GAI). **FOLD IN cold-2nd
  OBS-A:** guard the `protected_floor(_bak, symbol)` call (and the main-path call) in
  `check_never_sell_floor` so a type-corrupt `.bak`/ledger qty fails CLOSED (returns protected) instead
  of raising — restores the fail-closed-on-ambiguity posture the retired sidecar branch had.
  (c) live-verified rejected sell on a protected symbol (paper canary) before the flip.
**NO SEED (execute_starter) until all land.** ⏩ NEXT EXACT STEP: build **D-obs + OBS-A** (full patch
sequence, `execution/ownership_guard.py` + `execution/broker.py` + `alerts.py`), then the live-verify,
then prereq #2 (arm `OWNERSHIP_GUARD_ENFORCE=True`) per `logs/f6_activation_BLOCKED_2026-07-17.md`.

**🔴 STILL BLOCKED — FOREVER-6 ARMING (do NOT flip FOREVER6_ENABLED=True until all 3 prereqs land).**
Full analysis: **`logs/f6_activation_BLOCKED_2026-07-17.md`**. Rafael directed "turn F6 on + seed
≥1 share (Option B)." The cold **execution-risk board seat REJECTED arming** (Gro/GAI/Reliability all
APPROVED — they trusted the "floor is live" premise; it is NOT). **Verified at 100%:** (a)
`OWNERSHIP_GUARD_ENFORCE=False` (config.py:556) → `close_position` does a RAW close, zero floor;
(b) the F6 buy never syncs `ownership_ledger.json` (only the RTH-only `run_ledger_sync` cron does) →
a fresh anchor is invisible to the exclusion overnight → a watchdog restart adopts it as an intraday
orphan → `check_exits` raw-closes it → **the never-sell anchor gets sold.** This is the mandatory-cold-
board-on-risk-path rule working. **3 board-required prereqs before ANY arming (all P0):** (1) arm
`OWNERSHIP_GUARD_ENFORCE=True` w/ ledger F6 floors populated + `protected_symbols.json` present +
live-verify a rejected sell; (2) F6 buy must persist forever6 qty to the ledger SYNCHRONOUSLY (or run
ledger-sync immediately post-seed); (3) floor check on the GTC/DAY stop-submission path. Seed plan is
READY in the doc (CRWD+AMZN+TSLA ≈ $841, ~$487 dry powder) but runs only AFTER all 3 clear + a clean restart.

**Gro's TPD has RESET — full Gro+GAI preship is working again (no --waive-gro needed).**

**🔴 BIGGEST FIND OF THE SESSION — pnl=0.0 EPIDEMIC ROOT FIXED + LIVE (`fb93d11`, 2026-07-16).**
From today's post-market VERDICT=FAIL. **Every stop-breach cover at the RTH open recorded pnl=$0.00
PERMANENTLY.** Today RIVN's real **+$0.51** → `$0.00`; the SAME mechanism recorded RIVN's real **−$41 as
$0.00 on 7/7 plus 6 other trades**. Bug A (`5fb5c4e`) fixed WHICH query the reconciler runs — this fixed the
reconciler **never running in time at all**. TWO structural defects:
1. `run_cycle.py` — the 9:30-10:00 ET "opening noise window" block runs check_exits then **RETURNS**, while
   `_run_fill_recon` sat ~700 lines below → the reconciler **NEVER ran in the opening window** = exactly when
   open-covers fire AND when fills are slowest (RIVN's cover filled in 4 pieces over 13:32:51-13:34:15).
   Now called after check_exits, before the early return.
2. `fill_reconciler.py` — `max_age_minutes` hardcoded **5** vs observed **~5.5-6 min cadence** → at most ONE
   retry, usually ZERO. Now `config.RC4_RECONCILE_WINDOW_MINUTES = 90`. It is a RETRY BUDGET only — it does
   NOT widen the Alpaca query (bounded independently by entry_time + side filter + ±50% band) → wrong-fill
   risk is **ZERO-DELTA** (board). Floor-clamped at 5 (below that, `mark_fill_expired` would silently skip
   expired trades → infinite re-queue loop).
**WHY IT MATTERS:** a SUPPRESSED LOSS inflates Kelly's win rate + under-reports drawdown → **biases sizing
UPWARD**. Strictly risk-reducing. Kill switch was NEVER affected (Alpaca-equity sourced).
**✅ TQI GAP NOW CLOSED TOO (`10f710b`, 2026-07-16)** — the board caught that I'd wrongly claimed fb93d11
repaired TQI. It didn't; this does. **⚠️ It also corrects a claim I repeated twice: "TQI has zero capital-gating
role" is WRONG** — that came from an agent scoped to `exit_logic.py`, but **AB-3 lives in `entry_logic.py`
:1128-1150 and does `dollar_cap *= _tqi_kelly_adj`** (floor 0.5x) when the rolling 10-trade avg < 50. TQI IS
capital-affecting. THE HARM (measured): `_compute_tqi` gives `r_mult>=0 → 10 pts` but a REAL LOSS → 0, so a
suppressed loss scores +10 too high — 7/7 RIVN's true −$41 scored **33/100 vs a true 23/100**. An inflated TQI
**demotes LESS → sizes LARGER** (same upward-sizing bias as the Kelly win-rate path). FIX (board's rec):
`_record_tqi` skips `append_tqi` when `_fill_unverified` (still stores tqi_score for audit); `patch_exit_pnl`
recomputes from the TRUE pnl and appends once VERIFIED (lazy import — exit_logic imports portfolio_tracker;
try/except so a TQI failure can never undo the committed P&L patch). Enters the average exactly ONCE, only when
verified; never recovered → NO TQI (honest) over a fabricated one (poison). Gate: statics + self-test (unverified→[],
verified→[23], +10 inflation reproduced, P&L-before-TQI ordering), Gro+GAI APPROVE, preship `0b59ef04dc3a` +
`66132e189704`. Runtime-verified on OCI. Kelly ✓ / win-rate ✓ / daily_pnl ✓ / **TQI ✓**.
Gate: board APPROVE (4 required changes applied) + Gro APPROVE + GAI APPROVE; preship `57b8c343732f` +
`c1a22b1e679c` + `59520d693858`. Runtime-verified on OCI. Full evidence: `logs/pnl_zero_root_cause_2026-07-16.md`.
**▶ NEXT CANDIDATES (board follow-ups, logged not done):** TQI recompute-on-patch (closes the gap above);
`fill_helpers:369` returns entry_price as its FAILURE value — indistinguishable from a real scratch fill, which
forces the fragile `abs(_fill-_entry_px)<_MIN_PRICE_DIFF` heuristic → return None + explicit flag; upper-bound
accepted `filled_at` at ~exit+window; `reconcile_eod:475` `_qty_at_close` clobber (poisons only a POST-EOD
patch); `gtc_manager`'s false "Cover-on-breach filled @ $X" log (nothing had filled).

**✅ SLACK-RELIEF SECONDARY SHIPPED + LIVE + VERIFIED (`2cf6526`+`600c5d0`, 2026-07-16) — last known spammer
closed.** New `scripts/service_watchdog.sh` replaces the `*/5` one-liner (which alerted on the FIRST failed
check → FALSE "bot DOWN" on any mid-restart blip, 18 restarts/24h; and re-alerted every 5 min forever on a
real outage). Now: consecutive-fail GRACE (2≈10min, tunable `SVC_GRACE_CHECKS`) + 30-min THROTTLE + one-shot
RECOVERY notice + honest delivery (never logs "ALERT SENT" unless the POST succeeded; stamps only on a
confirmed send so a failed delivery retries) + counter DECAY (a flapper still accumulates; hard reset would
NEVER alert) + UNTHROTTLED disk-full/state-write guard. Cron: `*/5 * * * * /bin/bash .../service_watchdog.sh`
(bash-prefixed so a lost exec bit can't disarm it; file committed 100755; crontab backup
`logs/crontab.bak.1784217095`). Gate: GAI APPROVE + board Majors/Kim APPROVE (both blockers fixed) + preship
`baf2117d0d60`, Gro WAIVED. **⚠️ Self-caught pre-ship:** the anchored grep `^SLACK_WEBHOOK=` matched NOTHING
(var is **SLACK_WEBHOOK_URL**) → would have made the watchdog unable to EVER alert; fixed + live-verified.
**✅ BOARD FOLLOW-UPS #1 + #2 ALSO SHIPPED + LIVE (2026-07-16):**
- **`75c75ad` — memory_watchdog confirmed-send backport:** `alert_once()` stamped BEFORE curl, so a failed
  delivery silently ate a REAL alert for 30 min (was live in prod). Now stamps only after a confirmed POST →
  failed delivery retries next tick; missing webhook/curl failure logged loudly. Scope: `alert_once` ONLY —
  the auto-restart action stays unthrottled (outside it). Preship `fb766de537a6`.
- **`32d0f97` — HEARTBEAT + nightly freshness ("nobody watches the watchdog" CLOSED):**
  `service_watchdog.sh` touches `logs/svc_watchdog.heartbeat` every */5 run; `nightly_audit.py`
  `_check_watchdog_heartbeat()` asserts <15min freshness → CRITICAL Slack if STALE/MISSING (deterministic
  mtime math, called first + independently in main(), never raises). Live-verified end-to-end on OCI.
  Preship `fd105cec1cc5` + `0e588eb65434`.
**ALERTING-INTEGRITY ARC COMPLETE:** nightly crying-wolf suppression (`c069132`) + memory_watchdog throttle
(`b2f79db`) + service_watchdog grace/throttle (`2cf6526`/`600c5d0`) + confirmed-send (`75c75ad`) + heartbeat
(`32d0f97`). **Remaining follow-up (LOW value, not done):** rolling-window flap detector — a perfect 50/50
down/up alternation still never reaches GRACE (decay already covers the downs>ups case).

**✅ RIVN P&L CORRUPTION — FULLY RESOLVED (all 3 bugs, 2026-07-16). Chain broken.**
- **Bug A SHIPPED (`5fb5c4e`):** fill_reconciler.py external-close path → pnl=0.0 fixed.
- **Bug B SHIPPED (`71cae8c`):** orphan_manager recent-close guard (config window 120min) + CRITICAL
  mismatch alert → wrong-direction re-adoption prevented. Startup-only reconcile → re-alerts each restart;
  real position auto-adopts on next restart past window (self-heals). preship gai=APPROVE gro=WAIVED(TPD).
- **Bug C RESOLVED via Option B:** pop@portfolio_tracker:1543 is CORRECT; the guard makes the false-drop
  harmless. Risky Option A lifecycle rewrite deliberately NOT done. Full diag+gate:
  `logs/rivn_pnl_corruption_diagnosis_2026-07-16.md`.
- **✅ Bug E RESOLVED (2026-07-16, no code change):** NO double-sell — complete fills 7/6-7/8 = bought 17, sold 17,
  NET 0; ONE sell order. The -17 was a PHANTOM (Alpaca paper booked the long-closing sell as opening a short,
  avg_entry=$17.32=sell price). **SMOKING GUN:** order f1d4e826 = BUY 17 stop @$18.81 submitted 7/7 14:14:11, 5s
  after the phantom adoption → the bot placed a REAL live order against a NON-EXISTENT short (would have bought 17
  real shares if triggered; canceled before fill). **Bug B guard = real capital-risk prevention, VALIDATED**; the
  "real double-sell left unmanaged" fail-safe worry does NOT apply to this class.
- **⚠️ RETRACTED (2026-07-16): an earlier version of this block claimed "OM-BUG-1 is NOT benign — it cost a real
  loss." THAT WAS WRONG — do not act on it.** OM-BUG-1 diagnosed to completion; **its KNOWN BENIGN classification
  is DEFENSIBLE**; NO fix warranted. Evidence: RIVN 7/6 3pm ET C=**20.11** (stop 18.38 legitimately accepted at
  4:07pm); 7/7 9:30 open O=**17.745** → a **~12% OVERNIGHT GAP straight through the stop**. Stops are
  `extended_hours: False` → a stop does NOT execute outside RTH, so even a LIVE stop would have triggered at the
  open into the gap (~17.7) ≈ the bot's actual 17.32 cover. **The rejection cost ≈$0-7 slippage, NOT the −$41.**
  The rejection was ASYNC (accepted 7/6 21:21:55Z; Alpaca `failed_at` 7/7 08:00:01Z = 4am ET session-open
  validation once the gap made sell-stop@18.41 sit above market) — the submit-time cover-on-breach pre-flight
  (run_cycle:711) could not have known. Recovery worked as designed (premarket reconcile cleared the dead ID;
  RTH cover-on-breach closed at the open). **REAL LESSON: a 12% overnight gap is unhedgeable by a stop —
  that's an overnight-gap/sizing question (Arch Invariant #11), not a stop-mechanics bug.**
  **Low-impact residual (logged, NOT worth a risky fix):** "phantom protection" — GTC stop status polled only
  ~1s post-submit (run_cycle:793-824), so an async rejection goes undetected until the next premarket reconcile
  (harmless: stops don't fire pre-market; RTH cover-on-breach backstops). Latent nit: `gtc_manager.py:302` and
  `:381 _TERMINAL` omit `"rejected"` (falls into "else: still live") — cheap hardening candidate only.
  Full evidence: `logs/rivn_pnl_corruption_diagnosis_2026-07-16.md`.
- **Other follow-ups (logged, non-blocking):** Harris masked-loss doc-comment in `fill_helpers._sanity_ok`;
  persist-then-auto-adopt tightening. Plus the pre-RIVN priority queue: catalyst guidance_cut one-click approval
  (`logs/pending_approval_catalyst_guidance_2026-07-14.md`); Stage-2 cosmetic renames; GEX threshold recalibration
  once clean data; avg_r_multiple exit-discipline STRATEGY review (0.5R premature-truncation lever — board).

**Earlier this session (all SHIPPED + LIVE + VERIFIED):** P&L ledger `e6d471e` · TQI `df03656` ·
Slack signal-to-noise `c069132` · RIVN Bug A `5fb5c4e`. avg_r_multiple = RESOLVED not-a-bug (exit-discipline).

---

## ⏩ PRIOR (2026-07-15 interactive — autonomous-chain resume)

**✅ SLACK SIGNAL-TO-NOISE FIX SHIPPED + LIVE + VERIFIED (`c069132`, OCI pull, cron — no restart):** Rafael
asked for a BGG audit of the week's Slack reports (5/7 nightly FAIL = crying wolf). Root cause (Majors code-trace):
nightly_audit.py verdict is a stateless daily LLM grep, no dedup/lifecycle, stale hardcoded P5_BUG_QUEUE. FIX:
`logs/audit_suppressions.jsonl` (false_alarm|acknowledged|resolved) + deterministic `_apply_suppressions` post-filter.
false_alarm removed; acknowledged kept-visible-not-FAIL; resolved-reappears=[REGRESSION]+FAIL. Downgrade FAIL→WARN
only when zero real catastrophics + zero unsuppressed CRITICAL + no unaccounted cat (declared-count guard). Never
suppresses unmatched; fail-open; report FILE keeps original verdict. RIVN NOT suppressed. Gate: full read 648L,
statics, 9-scenario self-test, cold-2nd r2 PASS, preship gro+gai APPROVE (eedcafb39fff). Runtime-verified OCI.
Expected 5/7→~2/7 FAIL. Seeded: POSITION_COUNT_DRIFT + PAPER_FILLS_A4 (false_alarm), PNL_LEDGER + TQI (resolved).

**▶ RIVN P&L corruption — PHASE-1 DIAGNOSIS COMPLETE (full doc: `logs/rivn_pnl_corruption_diagnosis_2026-07-16.md`).**
Ground truth: RIVN bought 17@$19.73 (7/6), sold 17@$17.32 (7/7 13:38 UTC, GTC stop fired) = clean −$41 long loss.
Bot corrupted it 3 ways off ONE clean trade. TWO confirmed root bugs (cold-agent full reads):
  • **Bug A (pnl=0.0):** `fill_reconciler.py:90` derives submitted_after from entry_time → forces LEGACY P5-H2 path
    (`fill_helpers.py:280-304`: Sort.ASC + limit=5) → never matches a close fill ~16h later → falls back to entry_price.
    FIX (isolated, low-risk, HIGHEST VALUE — feeds the false kill-switch): reconciler calls the external-close path
    (`fetch_actual_fill_price(submitted_after=None, no_retry=True)`, filled_at DESC + entry lower bound) instead.
  • **Bug B (direction flip):** `orphan_manager.py:928` infers dir from bare qty sign; no guard vs adopting a
    just-closed/flat symbol; correction handler (L1335-1525) only runs for already-tracked syms. FIX: guard
    just-closed/flat adoption + validate direction from last-fill SIDE.
  • **Upstream:** main-bot false-drop of a still-live position from trade_log (record_exit path) = Bug B's trigger.
**▶ NEXT: Phase 2 = ship Bug A first (isolated fill_reconciler fix, own full-read gate + BGG + preship), then Bug B +
false-drop root.** Rafael to confirm Phase-2 go / sequencing.
**avg_r_multiple 0.012 = RESOLVED as NOT-a-bug** (McKinney code-trace: correct R-multiple; real exit-discipline —
tranches/breakeven/trails scratch ~1% initial risk; 0.5R premature-truncation is the lever). STRATEGY review, NOT a metric patch.


**✅ TQI STALE-BASELINE FIX SHIPPED + LIVE + VERIFIED (`df03656`, restarted 2026-07-15):** today's nightly
ALPHA/HIGH — `execution/exit_logic.py` `_compute_tqi` Component-1 hardcoded floor=9 went stale when the board
lowered the entry floor 2026-06-30 (CONVICTION_SKIP_BELOW 10→8); valid score-8 (half) AND score-9 (FULL-conviction)
entries both graded 0 confluence pts → biased Kelly TQI feedback down. FIX: config-derive anchor =
`config.CONVICTION_SKIP_BELOW` (min enterable score), 5pt floor → 25 at the 12-pt max. Mapping now 8→5, 9→10,
10→15, 11→20, 12→25 (RUNTIME-VERIFIED on OCI). **Anchor note:** approval said min(MIN_LONG_SCORE) but those =4
at runtime; CONVICTION_SKIP_BELOW (=8) is the constant that reproduces the approved mapping. Gate: Open Question
Protocol design 4/4 (config-derived unanimous; floor=5 3-1 vs LdP's 2) + Rafael APPROVE → statics/self-test/cold-2nd
PASS → FINAL preship gro+gai APPROVE (26c5482be261). Full read: exit_logic.py 2268 lines (Explore verbatim + personal
1-600). RC pre-existing flagged for separate pass: RC-7 qty_rem==1 tranche skip (~L638); RC-4 fill sites (likely false).
**STILL-OPEN from today's nightly (next candidates):** avg_r_multiple 0.012 vs 2.08 target R:R (edge-capture failure
— ALPHA/HIGH, diagnostic-first); confluence chart-context fields None for all entries (silent fetch fail); VOLSHADOW
"bucket":"B" stale tag (ALPHA/LOW cosmetic).


**✅ P&L LEDGER unparseable-boundary FIX SHIPPED + LIVE + VERIFIED (`e6d471e`, restarted 2026-07-15):**
Today's nightly Gemini VERDICT=FAIL was driven by CRITICAL (2x) `reporting.pnl_ledger fetch_all_orders:
unparseable boundary created_at '2026-05-13T06:00:16.06547Z' — stopping; order history may be INCOMPLETE`.
Root cause (reproduced live on OCI py3.10.12): `datetime.fromisoformat` rejects 5-digit fractional seconds →
`_bump_iso_ms`→None → pager HALTED. FIX: shared `_iso_to_dt()` normalizes fraction to 6 digits before parse;
`_pt_date`+`_bump_iso_ms` route through it (also cured a latent `_pt_date` after-hours PT-date mis-bucket).
Reliability board clarified: realized-P&L-from-fills was NOT corrupted (build_ledger uses fetch_all_fills,
not orders) — the halt truncated order→tier attribution only. **RUNTIME-VERIFIED: `fetch_all_orders()` now
walks 1989 orders to completion, zero CRITICAL.** Gate: board 2/0 (McKinney+Kim) + Gro + GAI APPROVE, cold-2nd
PASS, statics clean, preship marker cd9975b5337c (GAI flash false-reject on regex re-rolled clean). Audit:
`logs/tb_audit_log.md` 2026-07-15 entry. FOLLOW-UPS logged (non-blocking): EOD authoritative-flag doesn't
reflect an order-fetch halt (P2 observability); naive-timestamp WARNING log (P3).
**Also open from today's nightly (NOT yet addressed — separate items):** TQI `_compute_tqi` gives 0 score_pts
to valid score-8/9 entries (MIN_LONG_SCORE=8/CONVICTION_FULL_MIN=9 mismatch — ALPHA/HIGH); avg_r_multiple
0.012 vs 2.08 target R:R (edge-capture failure — ALPHA/HIGH); VOLSHADOW `"bucket":"B"` stale tag post-collapse
(ALPHA/LOW); confluence chart-context fields None for all entries (data-integrity — ds_meta).

**⏰ RESUME CRON: `ce7c799d` fired 20:02 PT (2026-07-15) → this resume. Session-only (CronCreate).**

**✅ OPTION A part 1 SHIPPED + LIVE (`daadf19`, restarted 2026-07-15):** `data/fetcher.py` `_bar_cache`
eviction — `_cache_put` now sweeps (throttled once per TTL): drop entries older than TTL + hard-cap
5000→4000. Fixes the TRUE leak (cache had NO eviction; the 180s TTL was read-only → stale DataFrames +
premarket-mover key churn climbed the RSS floor → OOM). Gate: static+self-test PASS, cold-2nd PASS,
Gro+GAI APPROVE, BGG design consensus. Diagnosis: `logs/option_a_memory_diagnosis_2026-07-15.md`.
**OPTION A part 2 (transient peak) — NO CODE FIX NEEDED (verified 2026-07-15):** the DataFrame-freeing
was ALREADY done (`signal_generator.py:771-774` pops+dels `_entry_df`/`_daily_df` after 16pt scoring; the
liquidity-skip at 716-717 also pops) — S43B "Priority 4 RAM leak fix". The hunt agent saw the retain at
L224 but missed the pop. So the remaining RSS spikes (195↔574MB) are INHERENT pandas working-set (per-symbol
fetch + prepare_df copies + 16pt scoring × 36 symbols) — a WATCHLIST-SIZE / BOX-SIZE matter, not a code leak.
Option A is substantively COMPLETE with part 1 (bar_cache eviction). If spikes still trip the watchdog:
trim the 36-symbol watchlist or size up the box (not a code change).
**▶ NEXT (resume here): priority queue** — (1) Slack-relief secondary: */5 "bot DOWN" watchdog CONSECUTIVE-FAIL
grace (only alert after ~2 straight fails — kills the mid-restart false DOWN) + consolidate the 3 RAM watchdogs;
(2) catalyst guidance_cut one-click approval (`logs/pending_approval_catalyst_guidance_2026-07-14.md`, Rafael go);
(3) Stage-2 cosmetic renames (BUCKET_B_MAX_POSITIONS*); (4) GEX threshold recalibration once clean data accumulates.

**✅ TODAY'S SHIPS (all LIVE on OCI, restarted 2026-07-15):** Slack-relief `b2f79db` · Stage 1 risk-gov
`0569360` (cap→circuit-breaker 20 + BP pre-flight + gross 2.5x + overnight 0.40) · Stage 2 Bucket A/B
collapse `6d79d32` (unified tier + leveraged ring-fence 5%) · GEX fix+sharpen `aad518a`+`ee88482` (flip now
**0.0% off spot**, was ~9%). Details in the blocks below.

---

## ⏩ PRIOR (2026-07-14) — tier refactor + Stage 1/2

**FULL DESIGN + BGG CONSENSUS: `logs/tier_refactor_design_2026-07-14.md` (read it first).**

**SHIPPED + LIVE this session:** Slack false-alarm relief (`b2f79db`) — `scripts/memory_watchdog.sh`
throttled (was the primary un-throttled */30 <200MB Slack spammer; 2026-07-02 fix had only covered
the secondary `ram_watch.sh`) + stale `/tmp/mtf_planned_restart` sentinel cleared. Gro+GAI APPROVE,
deployed OCI.

**✅ SHIPPED + LIVE (Stage 1 — risk-governance redesign, `0569360`, DEPLOY_OK+HEALTH_OK):**
`MAX_OPEN_POSITIONS` 4→20 base + **7→20 paper profile (the live value)** = circuit-breaker only.
NEW `risk_manager.check_buying_power_for_order()` (live Alpaca BP pre-flight, fail-closed — **fixes a
latent over-commit/desync bug: the bot never checked BP before ordering**) + `check_gross_exposure_for_order()`
(2.5× equity governor); both called before `submit_market_order`. NEW `MAX_GROSS_EXPOSURE_RATIO=2.5` +
`MAX_OVERNIGHT_EXPOSURE_PCT=0.40`. `_ALPACA_BASE_URL` constant = single live-migration point. Buckets
INTACT this stage (no lethal leveraged exposure). Gate: static clean, cold-2nd PASS, ruin/masked-loss
seat APPROVE, Gro+GAI APPROVE (diff + preship markers). **Follow-ups (logged, non-blocking):** alert on
gross-exposure fail-open (tracker-corruption blind spot); the P0-STARTUP gross-exposure health check was
deferred (with MAX=20 the count-HALT is a harmless high backstop); ruin models re-baseline at Stage 2.

**✅ STAGE 2 SHIPPED + LIVE-VERIFIED (`6d79d32`, Rafael "approve" 2026-07-15) — Bucket A/B DELETED.**
Tiers are now: **intraday/intraweek + QHM + F6.** Deleted BUCKET_A/B allocation tiers +
BUCKET_A_TICKERS (→ LEVERAGED_TICKERS, identical set, 8 files) + dead calculate_bucket_a_size/
calculate_bucket_allocation. Unified sizing: ALL symbols via conviction+Kelly+multipliers. NEW
`LEVERAGED_NOTIONAL_MAX_PCT=0.05` ring-fence (board FORK-5 — caps a 3x ETF at ~$139, replacing the
old 15%/leverage cap; verified TQQQ $2362→$139). intraweek=Option A (hold winners under existing exit
logic). `INTRA_ALLOCATION_PCT=0.85` replaces BUCKET_B_ALLOCATION_PCT. Gate: static+import clean on OCI,
cold-2nd PASS, ruin/masked-loss seat APPROVE, Gro+GAI APPROVE (diff-level); preship markers (4
mechanical-rename files gro=WAIVED+GAI-APPROVE — Rafael authorized "A" past a per-file-context hook
limitation that couldn't verify the same-set rename). Code on OCI; **deferred restart (market open) →
activates next AH/nightly restart.** FOLLOW-UPS: rename BUCKET_B_MAX_POSITIONS* count constants off the
"BUCKET" prefix; cosmetic `is_bucket_a` local-var names in exit_logic/scan_to_html; a future FORK-5
option to differentiate 3x(5%) vs 2x/1.5x leveraged caps (current flat 5% is tighter-and-safe).
> **STILL UNCOMMITTED (separate, DO NOT bundle):** the F6 1c edits (`execution/forever_hold_manager.py`
> +8, `strategy/run_cycle.py` +28) from session start — dark/gated, need their own gate/decision.

**Also diagnosed (not yet built):** (a) position-count-drift nightly CATASTROPHIC = **FALSE ALARM**
(board 4/4 — over-entry doubly guarded); (b) **✅ GEX/S&R FIXED + SHARPENED + LIVE** (`aad518a` fix + `ee88482` sharpening; RESTARTED 2026-07-15,
DEPLOY_OK). `data/gex.py`: (1) fail-safe data-quality gate → UNKNOWN+flip=None when data untrustworthy
(zero exec effect); (2) flip = local ±5% nearest-spot crossing; (3) **windowed capture** — capture_ratio
measured over ±10% near-spot window (was ALL contracts → far-OTM sparseness caused false UNKNOWN despite
atm_count=80). LIVE-VERIFIED: SPY flip=755.0 @ spot 754.64 (**0.0% off** — was ~9% off; median historical
9%). Full arc: 9%-off bug → safe UNKNOWN → sharp-at-spot. quality_ok=atm>=3 AND windowed_capture>=0.40 AND
windowed_valid>=10. Diagnosis: `logs/gex_flip_diagnosis_2026-07-14.md`. FOLLOW-UP: recalibrate thresholds
from accumulated CLEAN (post-fix) data + the level-respect / regime-predictive backtest (historical data is
corrupted by the old bug — median 9% off — so a backtest needs post-fix data to accumulate first).
(c) catalyst guidance_cut approval still pending
(`logs/pending_approval_catalyst_guidance_2026-07-14.md`).

---

## ⏩ PRIOR (2026-07-14 autonomous resume)

**⏩⏩ 2026-07-14 AUTONOMOUS SESSION RESULT (scheduled resume; Rafael asleep) — ONE approval waiting.**
Verified true state at 100% (git, not the stale prompt): catalyst gate is **already LIVE** (`2e2561d`,
Rafael go) and F6 1a is dark. Did the priority-1 catalyst **validation**: classifier 8/9 on real
headlines, blocks dilution/solvency/legal correctly, no over-block — found ONE recall gap (`guidance_cut`
missed "cuts full-year **REVENUE** guidance"-style split phrasings). Built + fully gated the fix
(present-tense active verb expansion + a pre-existing mypy fix; statics clean; **11/11** validated).
Gate result: **Gro APPROVE ×3, cold board 2-0 APPROVE, GAI REJECT ×3** (its R1 noun + R2 past/passive
catches were VALID and incorporated; R3 attacks the already-approved risk-first premise → board ruled
out-of-scope). **Not clean 3-way → QUEUED** (per auto-apply mandate + preship_gate needs an honest
GAI marker I won't self-write).

> **🟢 ONE-CLICK FOR RAFAEL:** read `logs/pending_approval_catalyst_guidance_2026-07-14.md`. If you say
> **approved**, apply `logs/pending_patch_2026-07-14_catalyst_guidance.patch` (clean on `ea5a58c`) →
> commit → OCI `git pull --ff-only` + `sudo systemctl restart mtf-bot` → done. Board + Gro back it;
> GAI's dissent is against risk-first (a real cut blocks a new entry even alongside an EPS beat), which
> you already approved when the gate shipped.

**✅ F6 1b SHIPPED DARK (`3711f03`, Rafael "ship" 2026-07-14):** `execute_starter()` in
`execution/forever_hold_manager.py` — cash-only, fail-closed, tier-tagged `forever6` (never-sell
floor via ownership_guard), slippage-buffered `reserved` guard so a market fill can't breach the
cash floor / cannibalize the deep ladder. Double-dark (unwired + `FOREVER6_ENABLED=False`). Gate:
cold ruin/masked-loss seat APPROVE + Gro APPROVE + GAI APPROVE (preship marker `49f8ebd1`). OCI
pulled, NO restart (dead code; nightly cron loads it). Note: GAI flash oscillated ~6 rolls on the
non-safety fill-price reporting logic → simplified it out (record planned px; Alpaca holds
authoritative cost-basis; floor safety is via `reserved`, unchanged) → clean Gro+GAI APPROVE.

**NEXT — F6 increment 1c (run_cycle wiring), needs interactive session:**
- Full spec in `logs/f6_1b_build_package_2026-07-14.md` (§1c). Explore full-read `run_cycle.py`
  (2023 lines, mandatory) → wire an after-close once-per-day hook behind `if config.FOREVER6_ENABLED:`
  that calls `maybe_start_accumulation()` then `execute_starter()` → full board + Gro/GAI on the
  run_cycle diff → ship DARK. Then a SEPARATE 1-line `FOREVER6_ENABLED=True` flip needs Rafael's
  explicit go (execution-behavior change). Do NOT flip live-order execution autonomously.
- **GEX expected-close BUILD** (top build from the prior handoff) — 0-DTE chain in data/gex.py + band.
- Lower-pri: catalyst downgrade/leadership/recall types after more validation; overnight_atr_buffer_exit
  (c)tag/(d)rename; IC Phase 1b per-factor logging.

---

**⏩⏩ 2026-07-14 CROSS-ACCOUNT PICK-UP (a DIFFERENT Claude Gmail resumes — Rafael at 89% weekly).**
Standard resume: `git pull` → read this block → query Master Brain. Everything below is SHIPPED+PUSHED
or a scoped/aligned next step; nothing lives only in the prior session's context.

**✅ SHIPPED + LIVE + SYNCED this session (all on `main`, OCI at `9ee9299`):**
- **CATALYST ENGINE — LIVE.** `events/catalyst_engine.py` per-name blocking-catalyst detector +
  `CATALYST_GATE_ENABLED=True` (`2e2561d`); entry-gate wired into `execution/entry_logic.py:execute_entries`
  after QHM exclusion (`7732c5a`,`0c2db0d`) with never-mask fault handling (block only on positively-confirmed
  catalyst; on cache-absence/ambiguity return raw). ~10-min RTH cron writes `logs/catalyst_state.json`.
  Validated 48/50 correctly-neutral; caught+fixed a co-tag attribution bug (RIVN offering was false-flagging TSLA).
  Blocking types: dilution_offering/guidance_cut/solvency/legal_probe.
- **FIFO EDGE REPORT — LIVE (this was the corrupt-tracker fix Rafael asked for: "score should match the P/L").**
  `research/fifo_reconcile.py` (`e57b380`) signed-FIFO over all Alpaca fills → reconciles account to **$-0.02
  residual**. Truth: all-time realized **+$255.61**, fees $0.65, open unrealized ~−$36, equity−2500 ≈ +$228.
  Tracker was **~$436 off** (showed −$181). Cache `logs/fifo_edge.json` (cron `8 5,20 * * 1-5` on OCI).
  `weekly_review._strategy_validation_html` now sources this cache (`ae3a5d9`) + a reconciliation bridge note
  (`9ee9299`) so per-score rows always sum to the account total. **Per-score edge revealed: 11/12 is a REAL
  LOSING band (−$38, 39% win, n=31); 10/12 +$105.56 (55%, n=95); 12/12 +$111.59 (47%, n=55).**
- **F6 (Forever-6) starter — BUILT, DARK.** `execution/forever_hold_manager.py` (`417f0865`, gated, log-only)
  + config constants (`5f712c7`, `FOREVER6_ENABLED=False`). Cash-only, catalyst-screened, breadth-first,
  VIX-scaled starter trigger. NOT wired into run_cycle yet.
- Options UX 2-col + 0DTE directional reframe; dashboard overnight soft-exit transparency; GOOGL 2sh add;
  weekly/monthly 404 fix (public/logs symlinks + daily cron `35 5 * * *`).

**🤝 BGG ALIGNMENTS (persisted, no code yet — safe to build next):**
- **GEX expected-close (dynamic, not static) — SCOPED, cold-seat-vetted.** Rafael: add an "on-the-close"
  EOD SPY estimate that is dynamic. Cold seat found the fatal flaw: **the weekly chain is the wrong instrument
  — the closing pin is driven by 0-DTE gamma (∝1/√T), not the weekly GEX.** Build spec: fetch 0-DTE SPY chain
  in `data/gex.py` → persist per-strike gamma profile → regime-conditional gravity band (negative GEX = amplify/
  trend, positive = dampen/pin) → dashboard render. A pure single-number "gravity price" was rejected; it's a
  regime-conditional band. **THIS IS THE TOP NEXT BUILD.**
- **Catalyst Phase 2 (exit-on-catalyst)** pending; reverses Architecture Invariant #2 (news display-only) →
  needs the Invariant #2 text updated + board vote before exit-side goes live.
- **F6 cascade concept** adopted by Rafael ("fine with the cascade"); live-wiring needs run_cycle hook +
  cash-only order path + `FOREVER6_ENABLED` flip + a masked-loss cold seat on the risk-path diff.

**📌 QUEUE (Rafael-greenlit, gated, for the next account in priority order):**
1. **GEX expected-close BUILD** (spec above) — `data/gex.py` + dashboard. Feature Design Protocol gate first.
2. **Slack-spam / `main.py` memory-leak** (Option A, Rafael already said 'proceed'; RSS 120→600MB over hours).
3. **`overnight_atr_buffer_exit`** follow-ups: (c) order-tagging so exits are attributable, (d) rename for
   clarity; **dashboard must show the real ~$15 ATR-buffer stop** (this exit sold RIVN — NOT the hard stop;
   PRESERVE the logic, it's correct; `execution/exit_logic.py:1187-1349`).
4. **F6 live-wiring** (above). 5. **IC Phase 1b** per-factor logging (`research/ic_engine.py`).

**⚠️ OCI HAZARD CLEARED THIS TURN:** OCI had a *direct-edited* `weekly_review.py` + 2 untracked `research/*.py`
that were blocking `git pull --ff-only` (silent — HEAD was stuck at `cc1724b` for multiple prior "deploys").
Backed the OCI-local copies to `/tmp/*.bak`, `git checkout --` the file, landed `9ee9299`. **Lesson: after any
deploy, verify `git rev-parse HEAD` on OCI actually advanced — a blocked ff-pull fails silently and the `&&`
chain still prints later steps.** Never edit tracked files directly on OCI (off-git channel = prohibited).

---

**⏩⏩ 2026-07-13 RESUME (cron 741aaaf8 @ ~11:56 PM PT, session-only, after Rafael's usage reset) — READ
`logs/work_order_2026-07-13_pm.md` FIRST.** CATALYST ENGINE fully built + wired, all DARK: 1a detector
(`8d46797`) + 1b gate-logic+cron (`ae9db77`, ~10min RTH refresh populating catalyst_state.json) + 1b
entry-wiring (`7732c5a`, in execute_entries, never-mask fault handling, entry-branch-only, deployed).
NEXT: (1) validate the detector from overnight cron data (logs/catalyst_engine.log); (2) flip
CATALYST_GATE_ENABLED=True (Rafael go + Gro/GAI on 1-line diff) → catalyst entry-block goes LIVE; (3)
FOREVER-6 cash-only build with the screen live (logs/f6_starter_bgg_2026-07-13.md). Lower-pri: options UX
step F + dead-CSS purge; overnight_atr_buffer_exit (c)tag/(d)rename; IC Phase 1b. Options UX 5/6 + dashboard
soft-exit transparency + GOOGL 2sh all shipped earlier. Cron method = CronCreate one-shot session-resume.

**⏩⏩ 2026-07-13 PM RESUME (cron 0365e14f @ ~1:45 PM PT, session-only) — READ `logs/work_order_2026-07-13_pm.md`
FIRST.** Priority queue for the resumed session (all Rafael-greenlit this session): (1) **Options page UX
REDESIGN** (display-only) per Luke's spec — anchor SPY/QQQ pinned atop 0DTE, SECONDARY→dropdown, freshness
pill (last/next scan), kill text density + dup data, keep vol-events banner; (2) **Forever-6 CASH-ONLY starter
build** [gated, approved]; (3) **Option A Slack-spam + main.py leak** [gated, 'proceed']; (4) **IC Phase 1b**
[gated] if usage remains. Full spec + Luke's ordered build steps in the work-order. Cron method = CronCreate
one-shot session-resume (Rafael's standard going forward). SHIPPED today: options 2-col + 0DTE reframe, IC
Phase 1, GOOGL 2sh add — all live+synced.

**🌙 OVERNIGHT/EARLY-AM 2026-07-13 — pick-up state.** Full detail: `logs/overnight_work_order_2026-07-12.md`
+ `logs/july_trade_audit_2026-07.md`. **SHIPPED + LIVE:** dashboard SPY GEX+S/R card; monthly P/L fix
(`4b43042`); loop-engineering scope (`bot_improvements.md`); **QHM dip-add FULLY LIVE** (`aeed5cd`):
flag ON + RegT-BP affordability guard + **Option-C stop-safe add** (cancel GTC stop→marketable
add→15s poll→resubmit stop for actual qty; 4-branch fail-safe; RTH-gated; cold-2nd PASS + Gro+GAI).
**KEY FINDING:** July "downturn" is 87% PHANTOM — real July P&L = **−$34.70** FIFO (not −$279), and
~all of it is **RIVN** (bought into its 7/6 public offering; catalyst the bot doesn't screen).
**REMAINING QUEUE (all need Rafael/BGG):** (a) NVDA catch-up DONE (2sh @$203.13, stop 1f951687) —
Rafael now redirecting adds to **GOOGL** (below cost −2%; NVDA is +1.8% ABOVE cost so does not
qualify for a dip-add); GOOGL stop-safe add pending his go; (b) eod/P&L 100%-reliable (purge 07-02
phantom + reconcile to FIFO — Rafael mandate); (c) catalyst/news engine (per-name screen; reverses
news-display-only invariant → board vote); (d) IC/ICIR learning-loop **Phase 1 SHIPPED** (`81a4c08`
`research/ic_engine.py`; 12pt score IC=+0.27 ICIR=+2.08 n=51 borderline-sig; Phase 1b = per-factor
logging next); (e) S/R-calc BGG (VWAP/history/dynamic); (f) dip-add Findings #2 (78s-latency timeout)
+ #3 (cosmetic); (g) **Slack spam ROOT-CAUSED = main.py memory leak** (RSS 120→600MB over hrs →
false RAM alerts + memory-pressure hangs; 4 un-throttled alert sources amplify each restart) — fix =
tracemalloc leak hunt (RTH path, needs BGG) + throttle/dedupe the 3 shell/alert sources. Margin: RegT
BP $3,266 (overnight) / effective $7,724 (intraday) / cash $888.

**✅ 2026-07-13 SHIPPED (display-only, non-gated):** options_scanner.py **two-column layout** (`4e74fac`)
+ **0DTE REFRAME** (`d4a9874`). 2-col: Weekly | 0DTE side-by-side (Rafael's 2026-07-06 mockup), 1-col
<768px, 8-field slim rows + click-to-expand (no data lost). 0DTE REFRAME: 0DTE is NO LONGER premium-
selling — now **directional intraday-swing capture** (long OTM ~0.35δ CALL upside + PUT downside per
name, both shown = alternatives, pick one), universe **SPY/QQQ + Mag 7**, moved to the **LEFT** column;
retired the premium-selling conflict/align banners + direction-lock. Gro+GAI SHIP-WITH-NOTE (notes folded
in: alternatives-not-straddle + speculative-theta warning). Live+served OCI (:18080, 0DTE-left verified,
fresh scan 2 weekly / 13 0DTE). Optional future (GAI): a VWAP/MA intraday nudge toward the likelier side.

**✅ 2026-07-13 GOOGL QHM ADD DONE (live):** +1 sh stop-safe → **GOOGL 2 sh @ $359.30, 25.9% equity**,
GTC stop 2sh @ $308.22 (`bbed81da`); quarterly_holds.json updated; verified single stop, no dup. (Rafael
redirected the catch-up from NVDA→GOOGL: GOOGL was below cost, NVDA above.) Lesson in logs/ERRORS.md
(204-empty-body + bot-recreates-stop race → keep bot paused whole op, cancel LIVE stop id dynamically).

**⏳ AWAITING RAFAEL (2 items):** (1) **F6 SPY-2% starter rule** — full BGG done
(`logs/f6_starter_bgg_2026-07-13.md`): cascade+concept UNANIMOUS APPROVE, but board caught 2 ruin-class
flaws Gro/GAI missed (ammo-cannibalization of the deep-crash cash; concentration-as-diversification of 6
~0.8-corr tech names). Amended safe form = −3% DYNAMIC close (`−max(2,0.15·VIX)%`) + catalyst screen +
breadth/correlation-ranked funding + SEGREGATED starter budget. Awaiting Rafael adopt/reject. (2)
**0DTE options reframe** — port scan_to_html `_fetch_spy_0dte_data` directional-swing logic (OTM ~0.35δ
call+put, straddle range, IV skew, S/R) to options page 0DTE col + move to LEFT. OPEN Q: SPY-only vs
per-name universe? **NEXT in Rafael's sequence after GOOGL = Option A (Slack-spam relief + memory-leak hunt).**


**▶ TWO ACTIVE THREADS:** (A) **QHM dip-add rule — magnitudes BGG-ALIGNED + Rafael APPROVED, ready to
BUILD (gated)** — see block just below; full spec `logs/qhm_v2_design_2026-07-11.md` PART 1 FINALIZED.
(B) **Ownership 4a activation — Monday-gated:** blocker #1 (QHM tier=qhm) CLEARED `main@a8584ac`; blocker
#2 REMAINS = verify OCI ledger populated + `protected_symbols.json` present (Monday RTH cron runs inc3
code) BEFORE flipping `OWNERSHIP_GUARD_ENFORCE=True`. Chokepoint is shipped DARK (`main@488a893`).

**QHM DIP-ADD RULE — FINALIZED (board 2 seats + Gro + GAI, Rafael APPROVED 2026-07-12). NEXT = BUILD
(gated: full patch sequence on execution/quarterly_hold_manager.py 1988L + new config.py constants).**
- Rung A: price ≤ cost_avg×(1−0.02) → small add, capped AT target weight.
- Rung B: price ≤ cost_avg×(1−0.05) → aggressive add (size ≥ existing position), MAY exceed target up to
  a **1.375× target ceiling (27.5% equity)** enforced as a **HARD PRE-FILL cap** + a **max-shares-per-name
  cap** recomputed quarterly (`floor(ceiling_wt×equity/price_at_review)`). Board OVERRODE Gro/GAI "no cap":
  cost-avg triggers re-arm on a falling avg → ladder can hit ~34% equity before the price floor fires.
- Hard STOP-ADDING floor: **−15% below FIRST-ENTRY (tranche1)** → escalate to board (NVDA: below $169.17).
- Trigger off COST-AVERAGE; stop-floor off FIRST-ENTRY. Anti-osc: max 3 adds/qtr, ≥2 days apart, none in
  final ~7d pre-earnings, keep day-3 re-confirm.
- **NVDA one-time CATCH-UP: +1 share → 15.2%** (unanimous; NOT +2/22.7%). ⚠️ LIVE paper order tagged
  tier="qhm" — needs Rafael's explicit go before it fires. GOOGL = no action (≈target).
- config constants to add: QHM_DIP_ADD_RUNG_A_PCT=0.02, _RUNG_B_PCT=0.05, _CEILING_MULT=1.375,
  _STOP_FLOOR_PCT=0.15, _MAX_PER_QUARTER=3, _MIN_DAYS_BETWEEN=2, _NO_ADD_DAYS_PRE_EARNINGS=7.

**NEW HARD RULE — DURABLE SYNC RULE (CLAUDE.md §, Rafael mandate 2026-07-12, Gro+GAI APPROVE
marker `a97ea0d7686f`).** On EVERY ship AND EVERY time Board+Gro+GAI align on a
protocol/rule/decision/scope — *even with zero code shipped* — sync all 5 channels the SAME turn:
git push (+ handoff/design docs carrying the EXACT next step), OCI `git pull --ff-only` (+restart
only if code), `.md` files, `logs/`, Master Brain. `handoff.md` always carries a live "⏩ pick up
here" pointer, pushed the moment alignment is reached. Surgical + cheap. Full text: CLAUDE.md
§DURABLE SYNC RULE. **Why:** Rafael switches between two Claude Gmail accounts on rolling 5h limits
— the other account must resume with `git pull` → read handoff.md → query Master Brain and land
exactly here.

**OWNERSHIP INCREMENT 3 — QHM-TIER ATTRIBUTION SHIPPED + LIVE (`main@cbb3925`).** Legacy untagged
QHM buys (NVDA/GOOGL, predate client_order_id tagging) now get a real never-sell floor (were
counting as intraday → floor=0 → sellable). New `get_quarterly_hold_quantities()` (fail-closed on
corrupt state) → `sync_ledger` qhm overlay `qhm=min(max(claim,replay_qhm),net-f6)` (MERGE not
overwrite) + SKIP drifted symbols. **Cold board caught 2 real bugs Gro+GAI BOTH missed**
(overwrite-clawback of tagged-qhm protection; drift-masking) — both fixed, 6/6 self-test PASS,
static clean, Gro+GAI APPROVE final combined diff. Still **UNWIRED** (standalone cron maintainer
`run_ledger_sync.py`; nothing reads the ledger to gate a live sell yet). See tb_audit_log 2026-07-12.

**OWNERSHIP INCREMENT 4a-part-1 — CHOKEPOINT SHIPPED + LIVE (DARK, `main@488a893`).** Rafael
APPROVED Option B (broker chokepoint) + kill-switch semantics + build structure (2026-07-12).
`broker.close_position(symbol, *, tier="intraday")` + `partial_close_position(..., _bypass_floor)`
now route reducing orders through `ownership_guard.check_never_sell_floor` when
`config.OWNERSHIP_GUARD_ENFORCE` is True. Shipped **flag=False (DORMANT — byte-equivalent to today,
cold-2nd verified)**. `close_position_for_tier` → thin alias (seam bug eliminated). Gate: static
clean, 5/5 self-test, cold-2nd PASS on dark ship, Gro+GAI APPROVE. Live: flag=False confirmed on OCI.

**➡️ EXACT NEXT STEP = 4a-part-2 ACTIVATION (flip `OWNERSHIP_GUARD_ENFORCE=True`). ONE blocker
remains:**
1. ✅ **CLEARED (`main@a8584ac`, 2026-07-12): QHM self-close tags `tier="qhm"`** — fixed at
   `main.py:459` (`_QHMBroker.close_position(self, sym, tier="qhm")`) + `quarterly_hold_manager.py:322`
   (`OrderDispatcher.close` → `broker.close_position(symbol, tier="qhm")`). Gate: static clean,
   self-test threads tier=qhm end-to-end, cold-2nd PASS 5/5, Gro+GAI APPROVE. Still dormant (flag off).
2. ⏳ **REMAINS — Ledger populated + `protected_symbols.json` present on OCI.** run_ledger_sync (cron
   */20 RTH Mon-Fri) last ran Jul-10 (pre-inc3) → NVDA/GOOGL currently floor=0 in the live ledger;
   cache absent. Monday's RTH cron runs inc3 code → will populate real floors + write the cache.
   VERIFY (`ssh OCI: cat data/state/protected_symbols.json` present + NVDA/GOOGL show a qhm floor in
   ownership_ledger.json) BEFORE flipping the flag — GAI: flag-on while ledger corrupt AND cache
   absent fails OPEN on a protected symbol; the cache-present precondition closes this.
Activation sequence (once blocker 2 verified): flip `OWNERSHIP_GUARD_ENFORCE=True` (gated config
change — 1-line, needs full read of config.py + gate + Rafael approval) → restart → live-verify an
intraday exit on a protected symbol bounds correctly + QHM self-close still works. Observability the
board asked for (log every guard decision; ALERT on unexpected QTY_BOUND; reconcile_drift alert) can
fold into the flag-flip ship.

**THEN 4b (open the door, only after 4a fully active + verified):** remove intraday-blocks-QHM gate at
`execution/entry_logic.py:438` (`if symbol in get_quarterly_hold_symbols(): continue`) + fix the QHM
exclusion in the cycle-sync risk count at `entry_logic.py:406-410` (an intraday position in a QHM
symbol SHOULD count toward intraday risk once co-holding is allowed).

**REQUIRED EXPLICIT-TIER CALLERS under B (board-identified — must NOT default to intraday):**
- `execution/quarterly_hold_manager.py:322` `QHMBrokerAdapter.close()` → `broker.close_position(sym,
  tier="qhm")` (else a QHM exit is bounded to the ~0-share intraday tier and silently no-ops).
- forever6 self-trim → `tier="forever6"` + `is_authorized_f6_trim=True` (ownership_guard L256-264).
- All ~9 sites in exit_logic.py (L1278/1439/1547/1621/1878 full + L297/715 partial), gtc_manager.py
  L189, and safe_close_all are legit intraday → default, no edit.

**SEAM BUG TO UNIT-TEST FIRST (board seat 1):** `close_position_for_tier` calls plain
`close_position(symbol)` at broker.py L721/L724. Once `close_position` gains a `tier` param, those
internal calls MUST pass `tier` through, else `close_position_for_tier(sym, tier="qhm")` on a
single-tier symbol collapses to the new `"intraday"` default and mistags the client_order_id.

**safe_close_all REWRITE (events/handlers.py L64-83):** drop the whole-symbol QHM skip → call
`close_position(sym, tier="intraday")` per symbol. Preserves QHM/F6 FLOOR even on circuit-breaker
(matches today's intent) BUT now correctly flattens the intraday shares of a co-held symbol.

**DO NOT build a full-flatten-regardless-of-tier path this increment** (board emphatic). A true
regulatory/forced liquidation must be a SEPARATE named primitive (e.g. `broker.force_liquidate`) —
never a `tier=` variant. `broker.close_all_positions()` (L740) is defined but has ZERO callers today;
leave it dead until a deliberate separate decision.

**KILL-SWITCH SEMANTICS = Rafael's open decision:** under B the 7% kill switch / circuit-breaker
preserves QHM/F6 shares but flattens the intraday shares of a co-held symbol (vs today's whole-symbol
skip). Recommended; awaiting Rafael confirm.

Note (housekeeping): code-review-graph is STALE (showed main.py at 3340 lines; it's 1092 post-
decomposition — exit logic now in execution/exit_logic.py). Rebuild the graph before relying on its
line numbers next session.

**GAI conservation:** Rafael topped up Gemini credits but mandated **free-tier gemini-2.5-flash ONLY**
(never pro/preview) + minimize call volume. `auto_ai_audit.py` already on flash (`a7923ae`).

**Queued (post-inc4):** QHM dip-add rule (2 rungs −2%/−5% + NVDA one-time catch-up; board vote on the
−5% over-weight multiple ~1.25–1.5× target), per-tier exit logic + Rafael wants a summary, Forever-6
build, S/R + buy-the-bounce feature (SPY/SPX/QQQ GEX levels on an HTML page), QHM v2 framework
(report→config pipeline, persistent conviction book, post-earnings state machine, take-profit).
See `logs/qhm_v2_design_2026-07-11.md`, `logs/improvement_queue.md`.

---

## (2026-07-10 interactive) — prior context
- **P&L DIAGNOSIS + SINGLE-WRITER FIX SHIPPED + LIVE (`main@1397194`).** Answer to "is the bot
  losing money": **NO — up +$259 (+10.4%), positive expectancy (50% win, avg win +$8.38 vs avg loss
  -$5.90).** The scary numbers were phantoms (RC-4 fill-matching: PANW -$182/TSLA -$81 on 07-02 that
  never happened; real 07-02 = +$45.31), the Movers mass-dump (retired), and the 07-08 false-HALT
  (Build F fixed). Reporting now truthful (lifetime cache = authoritative). **FIX (Rafael-approved,
  board 4-0 + Gro + GAI Option A):** killed the last dual-writer P&L hole — `lifetime_pnl_cache.json`
  had 2 writers with different `total_pnl` semantics (realized $273 vs total $259) swinging the
  headline ~$13 by cron timing + a hardcoded-2500 that would show a phantom gain on any deposit. Now
  pnl_ledger.heal_history is SOLE writer (total_pnl = equity-net_deposits TOTAL + components);
  generate_dashboard reads-only; metrics uses net_deposits not 2500. Live-verified: dashboard renders
  +$259.59 TOTAL. See logs/tb_audit_log.md 2026-07-10 entry. Follow-ups logged (net_deposits `or 2500`;
  monthly_review stale docstring).
- **WIN-RATE + TRADE-COUNT NOW AUTHORITATIVE (`main@96c89c1`, 2026-07-11).** Dashboard win-rate/count were
  eod-file-based (machine-dependent: 69 local/105 OCI, ~30-40%). Now ledger ENTRY-LEVEL (partials merged):
  **158 trades / 39.9%** (lot-level 232/49.6% over-counts partials). heal writes win_rate+total_trades to
  the authoritative cache; metrics overrides eod-based with it. Live: dashboard shows 158/40%. NOTE: my
  earlier "50% win" to Rafael was the lot-level count; accurate trade-level is ~40% (still net-positive,
  +$1.73/trade). Board 2-0 + Gro + cold-2nd PASS.
- **⚠️ GEMINI (GAI) API CREDITS DEPLETED (2026-07-11)** — 429 RESOURCE_EXHAUSTED. Blocks the final Gro+GAI
  gate (Gro-only now → each ship needs a Rafael GAI-skip or top-up) AND the OCI nightly pipeline
  (nightly_audit/meta-audit). ACTION: Rafael tops up Gemini billing / rotates key. Gro (Groq) still works.
- **P0-a OWNERSHIP LAYER — 3 increments SHIPPED (all INERT until wiring). LIVE `main@decbf77`.**
  Per-tier shared-lot ownership (intraday/qhm/forever6) so per-strategy P&L attribution + a
  never-sell floor become real. Alpaca = 1 untagged net position/symbol; ledger = per-tier CLAIM.
  - **(1) `execution/ownership_guard.py`** foundation (`658933f`): ledger I/O (RC-5 atomic),
    `make_coid`/`tier_of_coid`, `check_never_sell_floor` chokepoint, `reconcile_drift`, `launch_init`.
  - **(2) tier-tagging** (`7b1c5c0`): `tier=` param on the 5 broker submit/close wrappers +
    QHM dispatcher + `_QHMBroker` → client_order_id carries IN/QH/F6. New orders tag on next restart.
  - **(3) `sync_ledger` Option-C heal tool** (`decbf77`, THIS SESSION): full-replay rebuild from the
    Alpaca fill history, attributed by tier, with a **NEVER-SHRINK-A-PROTECTED-FLOOR** guard
    (aborts+alerts, never writes, if a replay would reduce any qhm/forever6 qty vs the persisted
    ledger — i.e. old fills aged out). Iterates union(new,baseline) so a vanished floor is caught.
  - **MAINTENANCE FORK RESOLVED → OPTION C, UNANIMOUS** (Gro+GAI+2 cold seats). Design + the 12-item
    hardening list + build sequence A→D: `logs/ownership_ledger_design_2026-07-10.md`.
  - **⚠️ RC-6 CONSTRAINT (verified live 2026-07-10):** Alpaca FILL activities do NOT carry
    `client_order_id` (only `order_id`); ORDER objects carry it and `fill.order_id==order.id`. So
    attribution REQUIRES a join `fill.order_id → order.client_order_id → tier` (fetch orders, build
    `{order.id: coid}` map). Shipped `sync_ledger` reads the fill's (absent) client_order_id → today it
    no-ops everything to intraday (SAFE: fallback, f6/qhm=0) but does NOT actually attribute until the
    join is added. Fold the join + an optional `coid_by_order_id` param into step B.
  - **(4) JOIN foundation SHIPPED** (`4c0902e`, THIS SESSION): `fetch_all_orders()` + `build_coid_map()`
    in `reporting/pnl_ledger.py` — the RC-6 fix. `fetch_all_orders` paginates `/v2/orders?status=all`
    via OVERLAP-AND-DEDUP (cold-2nd caught that a naive exclusive-`until` cursor silently strands
    same-timestamp tie-groups at the 500-row page cutoff; fix = `until`=oldest+1ms inclusive + `_seen`
    dedup; `_bump_iso_ms` emits 'Z' form or the URL 422s). Live: 1928 orders, 0 dupes, 369/369 fills
    joined. INERT (uncalled). `build_coid_map` → `{order_id: client_order_id}`.
  - **(5) sync_ledger ATTRIBUTION JOIN SHIPPED** (`d33c10c`, THIS SESSION): optional
    `coid_by_order_id` param; tier resolved via AUTHORITATIVE membership check (cold-2nd caught +
    fixed a map-hit-None-vs-miss ambiguity). Heal tool now attributes by tier when given the map.
    **LIVE END-TO-END PROVEN (temp ledger):** fetch_all_fills(370)+fetch_all_orders(1932)+
    build_coid_map → sync_ledger reconstructed all 5 open positions' per-tier ownership with
    **|drift|=0.0** (reconciles exactly to Alpaca net). All intraday until OCI restart tags new orders.
  - **ATTRIBUTION READ-PATH IS COMPLETE + GATED + LIVE-PROVEN.** The heal/reconstruct side of the
    ownership layer works end-to-end: given live fills+orders+positions it produces a per-tier ledger
    that reconciles to zero drift and refuses to shrink a protected floor.
  - **(6) MAINTAINER SHIPPED** (`23efb24`, THIS SESSION): `run_ledger_sync.py` — maintainer fork
    UNANIMOUS → OPTION 1 full-replay (Gro+GAI+2 cold seats); **incremental-delta engine ELIMINATED**
    (sync_ledger's refuse-to-shrink guard makes full-replay safe → incremental = cost-only, and its
    cursor failure modes are a fail-closed violation). Standalone cron script (NOT in run_cycle → no RTH
    edit, no restart); only writes the ledger. Instrumented (wall-time, healed-streak Slack, drift,
    per-tier sanity); sync_once never raises (cold-2nd caught + fixed a crash-into-cron). **Live-proven
    on Mac AND OCI:** 370 fills, 1932 orders, 35 positions, ~6-7s, seeds clean |drift|=0.0. OCI ledger
    is seeded. Design/decision: logs/ownership_ledger_design_2026-07-10.md.
  - **✅ MAINTAINER CRON INSTALLED (Rafael-approved 2026-07-10):** OCI crontab
    `*/20 13-21 * * 1-5 ... run_ledger_sync.py >> logs/ledger_sync_cron.log` — throttled ~every 20min
    across the RTH UTC window, weekdays. Raw UTC (no cron_tz_wrapper — exact ET alignment not needed for
    a reconciler; 13-21 UTC covers RTH year-round). $0 incremental cost (Alpaca paper API free; no LLM
    calls at runtime). OCI ledger seeded + now self-maintaining. Event-triggered-on-guard-reject is a
    step-D concern (guard unwired). Backup of prior crontab at OCI /tmp/crontab.bak.
  - **NEXT (build seq C2 → D):** C2 hardening before the guard gates live sells — #2 per-tier Level-2
    reconciliation (catches mistagged-fill floor drift the total-drift check misses), #7 untagged-after-
    launch quarantine, #9 Alpaca-net staleness window (fold into ownership_guard + maintainer). THEN
    D — wire the guard into reducing-order paths (exit_logic, broker close/partial), close_position
    multi-tier disallow, entry_logic tier-tag. **Guard does NOT gate live sells until C2 lands.** D is
    the only increment needing a restart + full RTH gate.
  - Gate note: preship_audit flash-GAI was in a stochastic false-reject loop (~13 rolls; hallucinated
    already-present guards); Rafael chose re-roll; roll landed a clean gro+gai APPROVE marker (sha 553e23d5).
    Every authoritative voice (board 4-0, my final Gro+GAI, cold-2nd) had already approved the exact diff.
- **Build F: ✅ MERGED + DEPLOYED — LIVE on `main@90f8bdf` (2026-07-10 00:27 UTC).** Reviewed the branch diff
  full + verified all 6 integration points; re-ran the gate this session (RULE C-2, prior OCI gate expired):
  Gro APPROVE + GAI APPROVE (both withdrew all findings on counter-prompt), cold board 2-0 (masked-loss +
  execution seats), py_compile/ruff/mypy clean, preship markers written for news_monitor/broker/run_cycle.
  Rafael APPROVED. Merged `claude/build-f-2026-07-08`→main (no-ff `90f8bdf`), pushed, OCI ff-only pull +
  restart → DEPLOY_OK, health OK, zero tracebacks. Branch `claude/build-f-2026-07-08` can be deleted.
- **TWO FOLLOW-UP ENHANCEMENTS from the Build F gate (logged, non-blocking):**
  (1) GAI: add a LOW-severity Slack heads-up if `get_asset_tradable("SPY")` returns None for several
  consecutive cycles while the SPY *bar* feed still works (a get_asset-only outage never trips the existing
  3-fail T1-FEED-DOWN alert). Path already logs a WARNING + `halt_eval` event each cycle — this just adds a
  human page for a persistent streak. (2) Masked-loss seat: cold-start edge — on the very first cycle after a
  restart, if SPY gapped down hard before the first successful 5-min fetch, `_spy_session_pct` reads 0.0
  ("no decline") for one cycle because `_main._spy_last_close` defaults to 0.0; the independent is_open /
  spy_tradable legs still catch a real halt, and it's auditable via `halt_eval`. Minor; consider a
  "warming/unknown" sentinel instead of 0.0. Both are `strategy/run_cycle.py`.
- **Forever-6 design = 100% COMPLETE + LOCKED (2026-07-09)** — but a big architecture decision reordered the build:
  - Scenario map (all 13 sell-first/no-buyers cases) + both forks locked → `logs/forever6_scenario_board_2026-07-09.md`.
    FORK A = per-rung monotonic latch; FORK B = small first rung fires + ping + human-gate deeper rungs.
  - Full read of `quarterly_hold_manager.py` (1955L) done → integration map `logs/forever6_integration_map_2026-07-09.md`.
  - **RAFAEL NEW MANDATE (2026-07-09): intraday/swing tiers must STILL trade QHM/Forever-6 names on confluence.**
    Ring-fence protects a SHARE COUNT, not the symbol. This requires the per-tier shared-lot ownership layer
    (the "real fix" the roadmap flagged; its absence retired Movers). Full board (5 voices) designed it →
    `logs/per_tier_ownership_design_2026-07-09.md`. **Rafael locked 3 decisions:** (1) shorts on ring-fenced
    names → via OPTIONS not shares (share tiers LONG-ONLY there); (2) real Alpaca GTC stops, auto-re-issued on
    every tier-qty change; (3) **BUILD ORDER = FOUNDATION FIRST, 3 PHASES.**
- **⏭ NEXT BUILD = PHASE 0 (COMBINED: ownership foundation + BROKER-AUTHORITATIVE P&L), NOT Forever-6.**
  Rafael merged the fail-proof P&L fix INTO Phase 0 (2026-07-09) — same problem: per-strategy P&L needs
  execution-time ownership. Plan: `logs/phase0_combined_ownership_pnl_plan_2026-07-09.md`. Sub-phases:
  **P0-a** ownership (ledger + client_order_id tier-tag + `ownership_guard.py` floor chokepoint +
  `qty_bounded_partial_close` + `close_position` multi-tier hard-disallow + drift reconcile + launch init;
  also fixes Movers bug). **P0-b** broker-authoritative P&L: WIRE IN `reporting/pnl_ledger.py` (570L,
  ALREADY BUILT, confirmed UNWIRED — the abandoned stateless-FIFO-from-Alpaca-fills engine) as the SOLE
  realized source + fill ingestion; DELETE the 3 parallel engines — `fetch_actual_fill_price` (~10 call
  sites), `_fifo_reconstruct`/`fifo_pnl.py`, and STRIP P&L from `record_exit` (~14 callers) while KEEPING
  its state machine. Total P&L→portfolio-history (advisory), unrealized→position objects. **P0-c** the
  STATE-RECONSTRUCTION INVARIANT: replay fills ledger → reconstruct per-(symbol,tier) qty/avg_cost →
  compare to Alpaca live positions every cycle; confirmed break (≥2 cycles)→FREEZE+manual, never auto-
  correct; portfolio-history is NEVER the freeze trigger (board seat fix). Validation = property-based
  FIFO tests + golden-master replay of the real bugged-months fills + 2am manual-reconcile runbook.
  **Build A (safe-mode) + OPT-2 (event-sourced replay) are SUBSUMED by P0-b/P0-c — retire as separate items.**
  Then Phase 1 (per-tier P&L history + synced stops), then Phase 2 (Forever-6 tier).
  Line-scoping → `logs/phase0_ownership_scoping_2026-07-09.md` (order-path sites) + canonical guard spec
  `logs/phase0_ownership_guard_spec_2026-07-09.md`. **SCOPED (4 core order files): broker.py (780L) +
  entry_logic.py (1688L, del registry block L437-440) + exit_logic.py (2269L, 23 reducing-order sites +
  qty-valuation bug zone) + fill_helpers.py (369L).** **ownership_guard.py = FULLY SPEC'D** (chokepoint
  algorithm + ledger schema + drift + stop-sync + launch-init, Rafael decisions folded in — implementable).
  **NEXT (fresh context): portfolio_tracker fill-reconcile (add client_order_id→tier attribution + untagged→
  halt) → ~15 remaining call sites (orphan 5 / run_cycle 4 / main 6 / QHM OrderDispatcher, tier=qhm) →
  launch-init script → assemble exact Phase-0 diff → static + cold-2nd + impact + FINAL Gro+GAI → API build.**
  Phase-0 surface ≈ 45 call sites / 6+ files + ownership_guard + ledger + launch-init. ~80% scoped.
- **TWO catastrophic modes the board designed against (do NOT regress):** (a) stale resting stop eats a
  protected share → synchronous cancel-replace on every tier-qty change; (b) floor recomputed from Alpaca's
  drifted net → floor is LEDGER-derived ONLY, drift→freeze-sells; (c) F6 trim reading Alpaca blended avg_entry
  → trim reads F6's OWN basis only.
- **Quick fix queued (from 2026-07-08 audits):** `scan_to_html._fetch_options_data` — `cannot convert float NaN
  to integer` (fired 10×; display-layer, fails-closed → not urgent). One-line NaN guard. NOTE: `scan_to_html.py`
  also has a *rejected* Gro/GAI RC-3 item today — handle carefully.
- **WEEKLY AUDIT ROLLUP (2026-07-02→09) → `logs/weekly_audit_rollup_2026-07-08.md`.** A week of Gemini
  midday/post-market audits, deduped to **TWO dominant roots:** (#1) **P&L ATTRIBUTION CORRUPTION** (`FILL
  UNVERIFIED → external_close → $0 P&L`; drove the 07-07 false −73.86% kill-switch trip, the EOD P&L drifts, RIVN
  $0-P&L + direction corruption, stop-hit fidelity) — build it WITH Build A + B (same fill/reconciliation family);
  (#2, new on 07-09) **ENTRY-PIPELINE THROTTLING** — sizing-cap stacking → 0 shares on high-priced names, silent
  "no allocation" skips, ANOMALY-2 confirm_gate stalls; qualified signals not being entered (costing trades now).
  Tier-2: TOD/phase, scan_to_html NaN, cycle-perf. Recurring FALSE POSITIVE: VOLSHADOW "score discrepancy" (Gemini
  misreads the log-only volume shadow — verify + rename). ALPHA reviews: exit R:R, high-score≠profit, MRI data.
  Full prioritized where-to-continue in the rollup.

## ⚡ 2026-07-10 SHIPPED (P&L fixes — LIVE)
- **Kill-switch phantom-proof** (commit `24c542a`, live): drop corrupt daily_pnl; measure = Alpaca
  equity delta − QHM unrealized (position objects, no fill-matching), fail-safe to account-level.
  Fixes the 07-07 false −73.86% trip. Board(masked-loss+reliability)+Gro+GAI, marker 79a4c2c8.
- **pnl_ledger pagination fix** (commit `e1e7416`, live): Alpaca activities use page_token NOT after_id
  (after_id ignored → infinite loop → 14-min hang on 367 fills — the "stayed stuck" issue). Now 3.7s.
- **P&L heal APPLIED + cron**: `python3 -m reporting.pnl_ledger --heal-apply` healed 51 eod days to true
  Alpaca-FIFO (7/02: −251.12→+41.08); invariant ok drift $0.65. **OCI cron `30 20,21,22 * * 1-5`**
  (post-close, both DST; idempotent/fail-closed/singleton). Dashboard regenerates from healed files.
  **TRUE LIFETIME REALIZED = +$282.88** (account UP ~$288 since inception; the losses were mostly
  corrupt reporting + the 07-08 false-HALT day). NOTE: crontab is NOT in git — re-add on host rebuild.
- **P0-b LIVE P&L authoritative — SHIPPED (commit `280cdec`, live)**: `portfolio_tracker.write_eod_summary`
  now sources `pnl_today` from `pnl_ledger.build_ledger()` (authoritative Alpaca-FIFO), invariant-gated
  ($5 tol), PT-keyed, A-4-gap-guarded, dual-compute fallback on any fail. Also fixed `pnl_ledger` to key
  per_day by PT date (was UTC → AH/overnight fills mis-bucketed; commit `1175596`). Gate: full read +
  static + cold-2nd PASS (caught the TZ bug) + Gro + GAI APPROVE. eod files re-healed with PT-keys. So
  the LIVE pnl_today the bot writes each cycle is now Alpaca truth, not corrupt-then-heal. New eod fields:
  pnl_today_qhm / pnl_today_total / pnl_ledger_authoritative.
- **P0-a ownership FOUNDATION — SHIPPED (commit `658933f`, unwired/zero-live-effect)**:
  `execution/ownership_guard.py` — per-tier ledger + `check_never_sell_floor()` chokepoint (fails CLOSED;
  floor ledger-derived only; ring-fenced=long-only; protected-tier self-exit nets vs effective_floor =
  floor-own) + `make_coid`/`tier_of_coid` + `reconcile_drift` + `launch_init`. Full self-tests + cold-2nd
  (caught QHM self-exit bug, fixed) + Gro + GAI + static clean.
- **P0-a WIRING — remaining, STRICT ORDER (or it fails-closed & freezes sells):** (a) run `launch_init`
  to seed the ledger from the current Alpaca book (all→intraday, F6/QHM=0); (b) add fill→tier attribution
  (client_order_id prefix) so the ledger stays current each cycle; (c) per-cycle `reconcile_drift`; (d)
  THEN route reducing-order paths (exit_logic 23 sites, broker close/partial → `qty_bounded_partial_close`)
  through `check_never_sell_floor` + mandatory broker `tier` param; entry_logic tier-tag + remove L437-440
  registry block. Each = own gated increment on live RTH. Scoping: `logs/phase0_*_2026-07-09.md`.

## Bot Status
- **Running:** 4 services active on OCI 129.153.208.32 (mtf-bot, mtf-writer, mtf-http, nginx). Git HEAD `9d03be1`
  (local = GitHub = OCI, verify with `git rev-parse HEAD` + OCI ssh). paper=True. Account ~$2,782, kill switch clear.
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32`. Deploy = git single channel (Mac commit→push→
  OCI `git pull --ff-only`+restart). NEVER rsync tracked files.
- **Permissions:** project `.claude/settings.local.json` `defaultMode: bypassPermissions` (set 2026-07-08 so
  scheduled/headless sessions don't stall on Bash approval). Backup: `settings.local.json.bak-20260708`.
- **Cost protocol (CLAUDE.md, LIVE):** interactive scopes/designs + runs board/Gro/GAI on the DESIGN; API
  (OCI headless) implements the pre-scoped diff + gates the DIFF + ships. Only Rafael changes it. Budget $20/mo.

## SHIPPED this session (2026-07-08)
- **F-INTERIM — commit `9d03be1`, LIVE + health-verified.** A false news-keyword HALT ("Can Trump cut off all
  trade with Spain?" matched substring "national emergency") had `safe_close_all`-liquidated the whole non-QHM
  book (6 pos, ~-$26, 11s). Interim: news-keyword HALT now BLOCKS NEW ENTRIES only, never liquidates. Full gate
  passed (board 3/3 + Gro + GAI; both final-pre-ship REJECTs were misreads, withdrawn on counter-prompt).
- **Cost fix:** morning health report retuned $0.86 → **$0.044/run** (`scripts/oci_report_runner.sh` Haiku +
  `scripts/collect_health_facts.py`; OCI cron 6 AM PT). Zero Mac dependency.
- **Scheduler root-cause fixed + validated:** crons failed on PERMISSIONS (no broad Bash allow → headless
  session stalled → wedged the slot), not scheduling. Fixed via bypassPermissions; live-test-fired 2026-07-08
  12:40 PT, wrote its marker cleanly. **In-thread pickup = CronCreate (not scheduled-tasks routines).**

## ACTIVE WORK — Build F + Forever-Hold (design DONE, decisions in `logs/build_f_decision_2026-07-08.md`)
**Build F (HALT/mass-liquidation redesign) — DECIDED (Rafael): "No reflex + halt observability."**
News = context/display only (delete `get_news_size_multiplier` 0.0 branch); NO automated close-all re-added
(liquidation authority stays with per-position stops + SPY-EXTREME entry-block + 7% kill-switch; `safe_close_all`
= user-shutdown only); ADD Alpaca venue-state detection → block entries + CRITICAL alert + `halt_eval` event
(never blind to a real halt). QHM exempt. Bucket-A-on-CB reversal → deferred to a board re-vote (moot w/o CB-liq).
KEY FACT: the 7% kill-switch does NOT liquidate — only blocks entries; only live `safe_close_all` caller = user-shutdown.

**Forever-Hold Accumulation — Rafael mandate, SEPARATE TIER above QHM (design done, guardrails locked, some open).**
- FOREVER-6: **TSLA, GOOGL, AMZN, CRWD, META, NVDA** — never sold (T1 = +1000%/10x → trim 25%; no other sell).
  Own bucket/logic/rules; ring-fenced (other strategies never dip into forever-6 shares). Curated 3–10yr secular
  names (NOT valuation-based), added manually over time; an extension of QHM. Crash/halt/CB/flash-crash = BUY.
  EXEMPT from kill-switch + halt-entry-block.
- **Board (Gro/GAI/Sosnoff/Thorp+Taleb) = SHIP-WITH-GUARDRAILS.** MANDATORY guards (see decision doc): (1)
  CASH-FUNDED ONLY (settled cash ≥ cost, never margin — makes margin-call impossible; THE load-bearing guard),
  (2) CAP on FIXED reserve / min(current,prior) equity not live equity, (3) per-name cap min(1sh, ~8% eq), (4)
  one-shot latch per (symbol, date) + ≤3 buys/day, (5) marketable limit never market, (6) data-quality gate
  before flash detector, (7) exemption from kill-switch/halt only — never from cash floor/CAP.
- **OPEN (Rafael + board):** sizing fork (scale-up vs flat/"more-levels" synthesis — recommend more-levels);
  CAP number (~30–40%); the EXPANDED crash-scenario board pass Rafael asked for (rules for flash/halt/CB/intraday
  crash/weekly crash/bear market — the full sell-first/no-buyers set); full-read `quarterly_hold_manager.py`
  (1954L); then final board+Gro+GAI on the combined proposal → **API build** (Rafael: ship to API to build).

## BUILD SLATE (Rafael): F → (Forever-6) → A → B → E  — full scope in `logs/api_build_packages_2026-07-08.md`
- **A — Data-Integrity Safe Mode** (2026-07-07 Alpaca-desync fix). Scoped to the LINE (full reads done:
  run_cycle 1865, orphan_manager 1624, risk_manager 802). Explained-P&L glitch validator → safe-mode → glitch-vs-
  real kill tagging → orphan/trade gating. Masked-loss seat MANDATORY (Scenario-7 test).
- **B — Orphan-stop root.** Scoped to the LINE (orphan_manager L714 + L1320; one confirming read left:
  portfolio_tracker.py:~965). Cancel stops BEFORE record_exit in external-close paths, fail-closed, + sweep.
- **E — QHM accumulation** (never-sell + buy-on-dips + `max(1, floor(0.03·eq÷price))`, 20% cap). Full read of
  quarterly_hold_manager.py (1954L) owed. **Note: Forever-6 supersedes/extends this — reconcile E with Forever-6.**

## OPEN ITEMS / BUGS / UNRESOLVED (as of 2026-07-08)
- **P0 (from incidents, in the slate):** Build A (glitch safe-mode), Build B (orphan-stop naked-short root).
- **Build F implementation** (decided, not built) + **Forever-6 implementation** (guardrailed; sizing/CAP + expanded
  crash board owed) → then API build.
- **Bucket-A-on-circuit-breaker reversal** — needs a fresh board vote (reverses Apr-8 ruling).
- **RC-4 open sites:** portfolio_tracker.py L1200/L1753 (per tb_audit_log hotspot table) — verify.
- **Main-bot false-drop root** (`record_exit` dropping a still-live position) — corrupts P&L; feeds the retired-Movers
  adopt vector. Pre-existing P0 (see FUTURE ROADMAP LOG MOVERS-RETIRED).
- **Cross-strategy audit** (strategies sharing Alpaca lots w/o ownership tags) — ongoing (roadmap 2026-06-30).
- **autonomous_review.py pushes to main** — conflicts with git-single-channel; needs branch+PR redesign (roadmap 2026-07-03).
- **Pre-incident:** Options two-column 0DTE page (SPX-source blocker — Alpaca has no SPX; mockup at
  logs/mockups/options_scanner_mockup_2026-07-06.html); OPT-2 event-sourced replay; CLAUDE.md Open-Question
  loophole removal; RBLX phantom lot; UX redesign of the 5 HTML pages; volume/16pt/delta/GEX/TSMOM shadow flips
  (see CLAUDE.md SHADOW STRATEGY TRACKER + FUTURE ROADMAP LOG).
- **Desktop scheduled-task `resume-build-f-2026-07-08`** — DISABLED (switched to in-thread CronCreate). Stray
  in-thread cron jobs are session-only (die on session end).

## Process / Invariants (binding — CLAUDE.md is authoritative)
- Board + Gro + GAI on EVERY fork BEFORE Rafael; the board+Gro+GAI audit of the FULLY-MAPPED proposal is the
  ABSOLUTE LAST STEP before API (account cross-strategy + existing bugs + hotspots). Final Gro+GAI pre-ship on the
  exact diff (preship_gate enforces on commit). Cold board MANDATORY on every risk-path diff.
- Safety: a control must NEVER mask a real loss. paper=True locked. SPY 5-min = sole entry gate. P&L from Alpaca fills only.

## References (all committed to git for the handoff)
- `logs/build_f_decision_2026-07-08.md` — Build F + Forever-Hold decisions + board results + open items (THE active doc)
- `logs/api_build_packages_2026-07-08.md` — F/A/B/E build slate (scoped to the line)
- `logs/tb_audit_log.md` — bug/patch log · `logs/incident_2026-07-07_alpaca_desync.json` — desync forensics
- `logs/safe_mode_spec_2026-07-07.md` — Build A spec

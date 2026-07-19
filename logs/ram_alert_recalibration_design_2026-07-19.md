# RAM alert-spam recalibration — DESIGN (2026-07-19)

**Status:** DESIGN for a lean cold board (Observability + Reliability) + Gro + GAI. Rafael's explicit
ask ("raise the threshold so the RAM spam stops; how is it affecting the bot"). Touches the OFF-HOURS
auto-restart (bot lifecycle) → Gro/GAI gate applies. Full reads (this session): `scripts/memory_watchdog.sh`
(97L), `scripts/ram_watch.sh` (26L).

## Problem (empirical)
Two watchdogs both alert on low **available** memory (their `free -m` `$7` is *available*, not *free* —
the Slack text mislabels it "MB free"):
- `memory_watchdog.sh` `*/30`: RTH alert `<200MB`; off-hours auto-restart `<150MB`, warn `<200MB`.
- `ram_watch.sh` `*/6`: RTH alert `<80MB`.

Observed RTH available = **58–95MB is the NORMAL steady state** (bot RSS 550–600MB on a 956MB box; ram_watch.log
min 60MB). So `<200` and `<80` both fire on the normal RTH floor → chronic Slack spam on a condition that is
true but **not operator-actionable** (the box is small; the real fix — box size-up or working-set trim — is
deferred/refused). Off-hours available recovers to ~200–350MB. The off-hours auto-restart + its "🚨 CRITICAL"
ping + the follow-on "MTF Bot Online" self-test ping also fire routinely (3:30PM, 1AM in the 7/17 log).

## How it hurts the bot (Rafael's "how/when/where")
Not the Slack noise itself — the mechanism: available memory this tight → kernel churns **swap** (4GB swap,
268–480MB in use) → 5-min scan cycles slow/thrash → the separate **cycle-hang watchdog** trips ("no completed
cycle in 12–14m") and **restarts the bot mid-RTH** (twice on 7/17) → missed scan cycles + the STATE DESYNC
criticals. Recalibrating alerts does NOT fix this; it only stops the noise. (Root fix = working-set trim /
box size-up, both out of scope this change — box size-up REFUSED, no spend.)

## Proposed fix (v1 — de-spam only)
1. **Retire `ram_watch.sh`** — remove its `*/6` crontab line (redundant with memory_watchdog). Leave the
   file in place (disarmed) for history; one watchdog owns RAM.
2. **Raise the RTH alert threshold** in `memory_watchdog.sh`: `<200MB` → **`<45MB` available**. Derivation
   (NOT a guessed static — No-Static-Regimes 1-in-10 data-derived exception, flagged): the observed RTH
   available floor is ~58MB; 45MB sits ~13MB (≈22%) below it, so it fires ONLY on a genuine dive below any
   normal RTH tick, never on the steady state. Keep the 30-min per-category throttle.
3. **Relabel** the message "MB free" → "MB available" (it is available memory; the current label misleads).
4. **Off-hours:** KEEP the auto-restart action (proactive memory reclaim before the next open is beneficial),
   but **downgrade its Slack notification to log-only** (a routine off-hours reclaim is not 🚨 CRITICAL). Drop
   the off-hours `<200MB` warn ping entirely (log-only). This kills the 3:30PM/1AM CRITICAL pings.
5. **Suppress the "MTF Bot Online" self-test ping after a PLANNED restart**: `memory_watchdog.sh` already
   `touch /tmp/mtf_planned_restart` before restarting; gate `alerts.alert_startup_test` (or its caller) on
   that sentinel like `alert_crash` does, so a watchdog-triggered restart doesn't also fire the "Online" ping.

## FORKS for the board
- **F1 — threshold 45MB vs a stateful trailing-baseline.** v1 = data-derived static 45MB (simple, shell).
  Dynamic (alert when available drops >X% below a trailing median) is better but needs state in the script.
  Recommend: static 45MB now + log the trailing-baseline as a roadmap item (No-Static 1-in-10 carve-out).
- **F2 — off-hours restart ping: log-only vs once-per-day throttle vs keep.** Recommend log-only (routine).
- **F3 — scope of #5 (self-test suppression):** include now (touches alerts.py) or defer? Recommend include
  (it's a real part of the spam and low-risk — sentinel pattern already exists).

## Invariants preserved
- Off-hours auto-restart SAFETY is unchanged (only its Slack chattiness drops). RTH still NEVER auto-restarts.
- The confirmed-send + 30-min throttle discipline in `alert_once` is untouched.
- No trading-logic change; entry/exit/sizing/scoring all untouched.

## Board vote required? YES — touches the auto-restart (bot lifecycle). Gro + GAI required.

---

## REVIEW — Gro + GAI (2026-07-19), both APPROVE-WITH-CHANGES
Convergent required changes (both voices):
1. **Don't retire ram_watch.sh — differentiate the tiers.** The defect is redundancy (both fire on the
   normal floor), not two scripts. Keep ram_watch.sh `*/6` as a CRITICAL imminent-OOM tier (very low
   threshold, NO throttle); memory_watchdog `*/30` becomes the throttled EARLY-WARN tier. A */30-only
   design risks missing a fast OOM dive between ticks (Gro + GAI).
2. **45MB is too aggressive** (too close to the 58MB floor for lead time). GAI: early-warn ~25–30MB;
   critical tier ~10–15MB. Both fire essentially never under normal 58–95MB operation → spam gone, OOM
   still caught.
3. **Sentinel #5 is BROKEN** (both): alert_crash consumes+DELETES /tmp/mtf_planned_restart on SIGTERM
   before the NEW process's alert_startup_test can read it → "Online" ping fires anyway. FIX: alert_crash
   must NOT delete the sentinel; alert_startup_test consumes it (read → suppress → delete) after use.
   (Prefer a timestamp in the file so both readers can age-check it.)
4. Off-hours auto-restart SAFETY confirmed preserved — notification-only change (both).
5. **Real leading indicator = SWAP PRESSURE** (sustained high swap during RTH), not raw available-MB — the
   direct signal for the thrash→cycle-hang→restart harm. GAI: add it. → v2 (needs vmstat/proc + sustain).

## REVISED SPEC (pending the 2 cold seats)
- **v1 (shell-only, kills the RTH spam):** memory_watchdog RTH <200→**<30MB** (early-warn, throttled 30m);
  ram_watch RTH <80→**<15MB** (critical imminent-OOM, keep */6, NO throttle); relabel "free"→"available"
  in both; off-hours: keep the auto-restart ACTION, downgrade its Slack ping to log-only + drop the
  off-hours <200 warn ping.
- **v1b (alerts.py, small):** fix the sentinel so the "Online" ping is actually suppressed on a planned
  restart — alert_crash stops deleting /tmp/mtf_planned_restart (writes/keeps a timestamp); alert_startup_test
  age-checks + consumes it. Include or defer per the board.
- **v2 (logged):** sustained-swap-pressure RTH alert (the true leading indicator); trailing-baseline dynamic
  threshold (replaces the data-derived static — No-Static roadmap item).

---

## COLD SEAT VERDICTS (2026-07-19) — both APPROVE-WITH-CHANGES
- **Observability (Majors):** biggest miss = **cadence ≠ throttle**. Detection cadence controls latency; the
  30-min `alert_once` throttle controls Slack rate — different knobs. Retiring the `*/6` and running detection
  `*/30` is a latency regression for ZERO spam benefit (throttle caps Slack regardless of cron). Bump survivor
  to `*/6`. Off-hours restart log-only fails silent on an ACCELERATING leak → add rate-escalation (page if
  >N restarts/24h). 45MB is granularity-close to the 58 floor with near-zero lead time; the real signal is
  swap-rate, not available-MB (lagging, already saturated at baseline). Track box size-up as a LIVE SLO breach,
  not "refused."
- **Reliability:** confirmed #1 restart-safety notification-only (traced :87-92); retiring ram_watch SAFE
  (grep: nothing reads its log/stamp). **#5 self-test suppression is BROKEN AS SPECIFIED** — `alert_crash`
  (alerts.py:334) `os.remove`s `/tmp/mtf_planned_restart` on SIGTERM before the new process's
  `alert_startup_test`, and only conditionally (main.py:638 = open positions) → fails in the common
  overnight-holds case; and returning False from `alert_startup_test` arms the main.py:386-398 5-min retry
  loop. Redesign (separate startup sentinel + gate at the main.py caller on the SUCCESS path) OR **defer #5
  (acceptable)**. Document the RTH alert-latency note; keep the trailing-baseline live.

## FINAL v1 (4-voice consensus — SHIP THIS; shell-only, no alerts.py, no sentinel risk)
- **One watchdog at `*/6`.** Retire `ram_watch.sh` (remove its `*/6` crontab line — cadence moves to
  memory_watchdog); bump `memory_watchdog.sh` cron `*/30 → */6`. Throttle still caps Slack rate.
- **RTH two-tier:** critical **<15MB** available (throttle 15m) + warn **<30MB** (throttle 30m). Both far
  below the 58MB floor → RTH spam eliminated; a genuine dive is still caught at `*/6`.
- **Relabel "MB free" → "MB available"** (the value IS available; the mislabel caused misdiagnosis).
- **Off-hours:** keep the auto-restart ACTION unchanged; ping throttled to **1/day** with a **rolling-24h
  restart count**; **escalate** (1h throttle, CRITICAL) if ≥5 restarts/24h (accelerating leak). Drop the
  off-hours <200 warn ping (log-only).
- **Documented:** RTH paging moves to <30MB @ `*/6` (latency note); v1 static thresholds are coupled to
  today's RSS regime.

## DEFERRED (logged — v1b/v2)
- **#5 "Online" self-test suppression** — redesign per Reliability seat (separate startup sentinel +
  main.py caller-gate on success path). Touches alerts.py + main.py; the risky part. Deferred.
- **Swap-pressure RTH alert** (swap-in / swap-used delta) — the true leading indicator of the
  thrash→cycle-hang→mid-RTH-restart harm. v2.
- **Trailing-baseline dynamic threshold** (No-Static roadmap; static 30/15 is coupled to the current leak).
- **The bot's actual RAM problem is NOT fixed by this** — box size-up (REFUSED, no spend) / working-set trim
  stays a LIVE open item; recalibration silences the smoke alarm, it doesn't fix the wiring.

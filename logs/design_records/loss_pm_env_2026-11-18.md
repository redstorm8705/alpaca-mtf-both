# LOSS-PM-ENV-1118 — the nightly loss post-mortem loads its keys under cron

Approval-ID: LOSS-PM-ENV-1118
Covers: scripts/loss_postmortem.py, tests/test_loss_postmortem_env_1118.py
Not changed: alerts.py, weekly_postmortem.py
Status: rev 2 (signed: Claude, 2026-11-18). Rev 2: step 2 names the helper and `.get` reads (GAI, Gro round 1). Design only. Reporting script; no trading path.

## Problem (verified at source 2026-11-18; GitHub #992, post-live audit of PRs #963/#966)
- `scripts/loss_postmortem.py` never loads `.env` (no `load_dotenv` call; `weekly_postmortem.py` has one at its top).
  Under cron there are no Alpaca keys and no Slack webhook.
- Server `logs/loss_postmortem_cron.log`: `[ALERT no-op] Loss review Fri Nov 13: 3 losing trades, $7.51 lost ...` and
  `[ALERT no-op] Loss review Mon Nov 16: 2 losing trades, $14.76 lost ...`; bar reads fail with "You must supply a
  method of authentication".
- Effect: no loss-review Slack card (so no loss-review issue) for 11-13 (MUD) or 11-16 (GOOGL, PLTR); the
  stop-vs-daily-range and after-exit fields are empty; the text prints "Stop Nonex daily range".

## Change
1. Load `.env` from the repo root at the top of the script, as `weekly_postmortem.py` does.
2. A missing value prints as "unknown" in the Slack/text line (no "Nonex"): a helper renders `None` or an absent key
   as "unknown", and every field in that line is read with `.get(...)` through it, so a missing key cannot raise.
3. Re-run the post-mortem for 11-13 and 11-16 once after deploy so the two missed reviews reach Slack.
Tests: the script loads `.env` before any data call (env var set from a temp `.env`); a None stop/after-exit value
and an absent key both print "unknown" without raising. No network, no broker.

## Required questions
Q1: Every losing trade gets its review again; the loss reviews feed the fixes that grow the account.
Q2: No trade-count change.
Q3: Simplest form: one line, matching the sibling script.
Q4: Follow-up to LOSS-PM-1112 (PRs #963/#966); nothing open touches this script.
Q5: Not applicable because no number is introduced.
Q6: Forward: a cron-environment check for every report script (a script that cannot read its keys pages once).
Q7: The reviews feed the loss-review issues; no trading path.
Q8: Measured: the cron log lines and the missing `load_dotenv` above.
Q9: Re-run on 11-13 and 11-16 shows the stop and after-exit fields filled and the card sent.
Q10: Not applicable because it is reporting.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change.
Q13: Keys missing: the script still runs and prints "unknown", as now, and the cron log says why.
Q14: Not a shadow: it restores a live report that has run every night without sending.
Q15: Not applicable because it makes no trading decision.
Q16: Proof: the next losing day's card arrives in Slack, and the cron log has no "ALERT no-op".
Q17: The next losing day after deploy.
Q18: The anomaly closed: a report that ran nightly while sending nothing.
Q19: Not trading: owner approval of this record.
Q20: Neutral prompts; reviewed by GAI, Gro and a cold board seat (Majors), whose nits (exit code on a failed send,
one helper for card and text, log whether .env loaded) are taken into the build.
Q21: Tests make no network or broker call.
Q22: Not applicable because it is not a control.
Q23: The re-run for 11-13 and 11-16.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because it uses no market statistic.
Q26: T1 Alpaca bars via data.fetcher (unchanged).
Q27: logs/loss_postmortem/<date>.json, atomic (unchanged).
Q28: OCI cron after the close (unchanged).
Q29: Small; restores a daily review the owner asked for.
Q30: Blind spot: cron scripts that silently lack their environment.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no replay.
Q33: Not applicable because it places no trades.
Q34: Not applicable because it has no return.
Q35: Not applicable because it is not market-dependent.
Q36: Not applicable because it trades nothing.
Q37: Not applicable because it is not a trading edge.
Q38: Not applicable because it is not a trading edge.
Q39: Not applicable because nothing is sized.
Q40: Protects the daily loss review; check: the re-run's JSON has non-null stop and after-exit fields.
Q41: Check every cron report for the same missing environment (the forward item in Q6).
Q42: Alpaca bars and the logged trade records, unchanged.
Q43: Load configuration at the process entry point, as the sibling script does.
Q44: The Slack card itself and the filled JSON fields.

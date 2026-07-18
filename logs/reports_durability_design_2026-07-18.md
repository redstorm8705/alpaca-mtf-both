# Routine-Report Durability — cross-account persistence (Option B) — 2026-07-18

**Rafael request (2026-07-18):** "100% of the routines' outputs (all reports) must be pushed to
durable, cross-account-readable storage (git / OCI / Master Brain / logs / .md) so other Claude
accounts can view them. Start with all the latest routine reports."

**Problem (verified):** ~9 OCI cron routines write report files ONLY to OCI-local `logs/`, ALL
gitignored, NONE pushed. A second operator account's `git pull` sees zero routine reports. Today
they're reachable only via the session-start SSH digest, which needs *this* account's OCI key.

## Decision: Option B (board 3–1 + Gro; GAI dissent resolved by design)
| Voice | Verdict |
|---|---|
| Board — deploy-infra (Peterffy/Kim) | **B** |
| Board — observability (Majors) | **B** |
| Gro | **B** |
| GAI | C (zero main-collision) — resolved: B's single serialized push in a no-deploy window + pull-rebase retry makes C's branch overhead unjustified, and C breaks the "just `git pull` and see everything" goal |
| A (per-routine self-push) | rejected by all — 9+ OCI→main pushes/day widen the deploy race |

**Option B =** ONE dedicated post-routine `scripts/sync_reports.py` that:
1. batches every new/changed routine AUDIT report into a SINGLE daily commit;
2. pushes once to `main` with `pull --rebase` retry on non-ff, in a FIXED late window (after the
   last daily routine, before the 2am restart) → collision with operator deploys ≈ negligible;
3. runs an **expected-vs-present reconciliation** (enumerate the routines that should have produced
   a report today; diff vs what's actually committed/in-remote) and **Slack-alerts on any gap** —
   the single instrumented chokepoint for the "did 100% land?" invariant;
4. (board-infra bonus) folds `autonomous_review.py`'s existing OCI→main push into the SAME
   serialized step → one writer, one window, one retry path (also closes that flagged race);
5. Master Brain digest: the `notebooklm` CLI is authed on the Mac, not OCI → the git channel is
   the guaranteed cross-account path; a Master Brain digest is a Mac-side add (session-start can
   push a rolled digest from the now-git-synced reports). Do NOT put a broken headless notebooklm
   call in the OCI cron.

## Scope
**In (machine-readable audit reports, un-ignored + synced):** `gemini_audit_*.txt`,
`midday_audit_*.json`, `midday_gemini_*.txt`, `meta_audit_latest.json`, `ai_audit_meta_*.json`,
`gex_daily_audit_*.json`, `wtp_*.md`, `weekly_audit_rollup_*.md`, `score16_report.json`.
**Flagged separate:** `weekly_*.html` / `monthly_*.html` dashboards stay under the global `*.html`
ignore — human-facing, served by the OCI web server, regenerate daily. Await Rafael's call on
whether to version them (churn/size tradeoff).

## Build plan
- **Part 1 (this commit):** gitignore negations + backfill the latest of each audit report
  (scp OCI→Mac→commit — normal git channel). ← DONE in this commit.
- **Part 2 (gated):** `scripts/sync_reports.py` + OCI cron wiring + fold autonomous_review push.
  Full gate (statics + cold-2nd + board/Gro/GAI preship) — it pushes to main, deploy-critical.

# Inventory of `logs/pending_*` files (2026-11-04, Claude signed)

23 files from 2026-06-05 to 2026-08-11. Each was checked against `origin/main` at `8fcd50f`: the JSON `status` field,
`git apply --check` (forward and reverse) for the patches, and whether the shipped code now contains the change.
Nothing was moved or deleted.

## Summary

| Bucket | Count | Action |
|---|---|---|
| Rejected by Gro/GAI at the time (13 `rejected_gro_gai` JSONs + the 08-04 patch) | 14 | Archive |
| Shipped later in another form | 4 | Archive |
| Historical packages and review notes, superseded by later work | 3 | Archive |
| **Still applicable, never decided** | **2** (one item: approval doc + patch) | **Keep; owner decision** |

## The one live item

**Catalyst `guidance_cut` recall fix (2026-07-14)** — `pending_approval_catalyst_guidance_2026-07-14.md` +
`pending_patch_2026-07-14_catalyst_guidance.patch`.
- The patch adds 19 present-tense phrasings ("lowers outlook", "cuts full-year guidance", …) so the live catalyst gate
  blocks a new entry after a guidance cut worded that way. Today it misses "Acme cuts full-year revenue guidance".
- `git apply --check` still passes on today's `events/catalyst_engine.py`: it was never applied. The gate is live
  (`execution/entry_logic.py:35` imports it).
- In July it had Gro APPROVE ×3, cold board 2-0, GAI REJECT ×3 (the last on the risk-first premise). It was queued for
  Rafael and never decided (`logs/tb_audit_log.md:8092`).
- It changes which entries are blocked, so it needs a fresh full gate on today's file before any approval.

## Per-file

| File | Target | Status | Evidence |
|---|---|---|---|
| pending_ds_gai_2026-06-05_quarterly_hold_manager.json | QHM module | Shipped later | `ds_gai_complete`; `execution/quarterly_hold_manager.py` exists and has been rebuilt since |
| pending_patch_2026-06-05_quarterly_hold_manager.patch | same | Shipped later | No longer applies either way (file rewritten) |
| pending_ds_gai_2026-07-01_alpha_issue_low_calendar.json | events/calendar.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-01_rc3_runcycle.json | strategy/run_cycle.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-01_rc4_portfoliotracker.json | portfolio_tracker.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-01_rc4_runcycle.json | strategy/run_cycle.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-02_3._category_risk_management_severi_orphanmanager.json | orphan_manager.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-02_rc2_runcycle.json | strategy/run_cycle.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-02_rc3_runcycle.json | strategy/run_cycle.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-06_rc3_runcycle.json | strategy/run_cycle.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-06_reporting_low_generatedashboard.json | generate_dashboard.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-08_rc3_scantohtml.json | scan_to_html.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-07-09_rc3_scantohtml.json | scan_to_html.py | Rejected | `rejected_gro_gai` (duplicate of 07-08) |
| pending_ds_gai_2026-07-15_infrastructure_medium_main.json | main.py | Rejected | `rejected_gro_gai` |
| pending_ds_gai_2026-08-04_rc2_runcycle.json | strategy/run_cycle.py | Rejected | `rejected_gro_gai`; decision-stack logging now covers its "log rejected entries" ask (CLAUDE.md Rule D) |
| pending_patch_2026-08-04_rc2_runcycle.patch | same | Rejected | Patch header targets the OCI absolute path; does not apply |
| pending_approvals_2026-07-01.md | strategy/run_cycle.py | Historical | Review notes for the 07-01 run_cycle items above, all rejected |
| pending_claude_session_2026-07-01.md | AWP queue | Historical | Session package; superseded by later handoff blocks |
| pending_claude_session_2026-08-08.md | ledger-heal tool | Shipped | `confirm_ledger_heal.py` exists at repo root |
| pending_claude_session_2026-08-11.md | SMCI visibility, PR #130 | Historical | Superseded by later ownership-ledger work [not verified against OCI from here] |
| pending_patch_2026-07-24_sync_reports.patch | scripts/sync_reports.py | Shipped | Malformed paths (`bb/a/…`); its additions (`urllib`, `_time_mod`) are in `scripts/sync_reports.py:25-27` |
| pending_approval_catalyst_guidance_2026-07-14.md | events/catalyst_engine.py | **Open** | See above |
| pending_patch_2026-07-14_catalyst_guidance.patch | same | **Open** | Applies forward cleanly today |

## Suggested move (not done)

Move the 21 archive rows into `logs/archive/pending_2026/` with `git mv`. Leave the two catalyst files in place:
the handoff and `tb_audit_log.md` link to them by path. Other docs may also link to the archived paths. The move
is a reversible commit and is ungated (`logs/` is outside the preship scope). It waits on the owner's OK.

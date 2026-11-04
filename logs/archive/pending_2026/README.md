# Archived `logs/pending_*` files (moved 2026-11-04, owner-approved)

21 stale approval-queue files from 2026-06-05 to 2026-08-11. Per-file status and evidence:
`logs/research/pending_files_inventory_2026-11-04.md`. Older links in `handoff.md` and `logs/tb_audit_log.md`
still point at `logs/<name>`; the same file now lives in this folder.

None had status `awaiting_ds_gai`/`awaiting_gro_gai`, so `autonomous_review.py` (which only picks those up from
`logs/`) is unaffected. `scripts/qhm_thesis.py` counts `logs/pending_approvals_*.md` in the weekly QHM report;
moving the stale 2026-07-01 file drops that count by one.

The catalyst guidance-cut pair (`pending_approval_catalyst_guidance_2026-07-14.md`,
`pending_patch_2026-07-14_catalyst_guidance.patch`) stays in `logs/` — still undecided.

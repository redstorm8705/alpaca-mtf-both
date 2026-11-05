# Mac-session runbook — items this cloud session could not finish (2026-11-04, Claude signed)

Start: `git fetch origin claude/sharp-mendel-cjiil9 && git checkout claude/sharp-mendel-cjiil9` (or merge it), then work
top to bottom. All files named below are on that branch.

1. **BGC design reviews (GAI + Codex; Gro only if Codex is unavailable).** Neutral prompts are pre-built (prior votes
   stripped, paper frame included):
   - `python3 .claude/preship/bgg_call.py logs/research/bgc_prompts/regime_size_2026-11-04.txt --add-questions`
   - `python3 .claude/preship/bgg_call.py logs/research/bgc_prompts/catalyst_dynamic_2026-11-04.txt --add-questions`
   - `python3 .claude/preship/bgg_call.py logs/research/bgc_prompts/vix_field_2026-11-05.txt --add-questions` (VIX-FIELD-1105, the live bug: run this one first)
   Verify each load-bearing claim at source; counter-prompt any reject with evidence (no blind re-rolls). Record the
   verdicts in the two design records (`logs/design_records/regime_size_2026-11-04.md`,
   `logs/design_records/catalyst_dynamic_2026-11-04.md`), then bring the owner one-page approvals.
2. **Server facts the designs need (read-only on OCI):**
   - the crontab line for `events/catalyst_engine.py` — does it pass symbols? (CATALYST defect 4);
   - `grep -c "CATALYST BLOCK" logs/mtf_bot.log*` and the blocked headlines (live block rate / false blocks);
   - `grep "Volatility regime" logs/mtf_bot.log* | grep -c "source: SPY"` style count of VIX-fallback days, and how often
     LOW_VOL / HIGH_VOL fired (REGIME-SIZE findings 2 and 6);
   - one logged core-tier entry's sizing: confirm the final shares equal `int(dollar_cap / entry_price)` (REGIME-SIZE
     finding 1).
3. **11-02 pre-approval scope:** read the 11-02 night session transcript (`~/.claude/projects/*alpaca-mtf-bot*/`) for
   the owner's exact pre-approval wording; add it to `logs/research/md_contradictions_2026-11-04.md` B.2 and bring the
   owner one question: standing rule or one night.
4. **Master Brain:** `notebooklm source --help`; delete superseded project-state / handoff sources; add
   `logs/project-state.md` and `logs/research/open_queue_2026-11-04.md`; confirm a query mentions 11-04.
5. **Gated doc fixes** (`CLAUDE.md`, `handoff.md`, `rules/*.md`) from `logs/research/md_contradictions_2026-11-04.md`
   section A and `logs/research/session_start_master_brain_audit_2026-11-04.md` §4 — one diff, through the preship gate.
   Also update the user-level reminder hook text from "board + Gro + GAI" to BGC.
6. **Post-live audits overdue: 86 changes since 10-09**, ordered in `logs/research/post_live_backlog_2026-11-04.md`
   (start with #825, #823, #822, #817, #753, #760, #583; `record_post_live.py`).
7. **Branch clean-up (owner-approved 2026-11-04 for branches certainly unused only).**
   - Delete the 428 branches listed in `logs/research/branches_deleted_2026-11-04.md`: each is already in main, has no
     open PR, no commit since before 2026-10-28, and no script names it. Re-check each before deleting
     (`git branch -r --merged origin/main` or `git cherry origin/main <b>` empty; not an open PR head), then
     `git push origin --delete <name>`. The file keeps every branch's last SHA for restore.
   - Do NOT delete the 63 branches with unmerged work (`logs/research/branch_inventory_2026-11-04.md`). Review each:
     superseded by a merged PR, still wanted, or abandoned. The 6 `feat/f6-*` / allocator branches belong to the
     Codex lane. Bring the owner a short list before removing any of them.

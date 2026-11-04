# Session-start and Master Brain audit (2026-11-04, Claude signed)

Checked from a cloud session at `origin/main` `8fcd50f`. This session has no NotebookLM access, no Mac files and no
OCI access, so the notebook itself and the `session-start` skill were **not** read. Everything below is from the repo.

## 1. What the session start depends on, and where it lives

| Piece | Where | Status |
|---|---|---|
| `/session-start` skill | The Mac's `~/.claude/skills/` (not in the repo; `.claude/` in the repo holds only `preship/` and `settings.json`) | **Not checked from here.** It isn't installed in cloud sessions. |
| Session-start hook | none in `.claude/settings.json` | Nothing starts automatically from the repo |
| Notebook pointer | `~/.claude/master_brain_id` on each Mac account | **Not checked from here** |
| Notebook ID in the repo | `CLAUDE.md:423` `0203f312-f285-4f20-8b8d-ca6fde65acf7` | The only copy in the repo; nothing cross-checks it against `master_brain_id` |
| Instructions | `CLAUDE.md:409-423` (Session Memory Protocol), `rules/OPERATING.md` G08, `handoff.md:5-9` | Consistent: read `handoff.md`, then query the notebook |
| What gets pushed to the notebook | `logs/project-state.md` | **Stale: last updated 2026-11-01** (main at `04213e7`); misses 11-02 to 11-04 |

## 2. Known Master Brain problems already logged

- 2026-10-17: "Master Brain login expired (owner runs `notebooklm login`)" (`handoff.md:413`).
- 2026-10-25: "Master Brain is at its 50-source cap — the owner frees old project-state sources, then push current
  state" (`handoff.md:279-280`). No later block records that it was fixed.
- `handoff.md:2019`: "notebooklm still needs a sticky reauth."

[hypothesis — unverified] If the cap was never cleared, Master Brain pushes since 10-25 may have failed or been
skipped, so a query could return state from October. Check on the Mac with `notebooklm source list`.

## 3. Stale or contradictory session-start text in the repo

1. **Wrap-up contradiction.** `CLAUDE.md:421` says "At the end of every session: Run `/wrap-up` automatically — do not
   wait for the user to ask." `CLAUDE.md:74-78` and `rules/OPERATING.md` G09 say never auto-trigger it. G09 is the
   current rule; line 421 is stale.
2. **Handoff header points at July designs.** `handoff.md:5-8` tells a new account to read
   `logs/qhm_v2_design_2026-07-11.md` and `logs/ownership_ledger_design_2026-07-10.md` as "(active design)". Current
   designs are in `logs/design_records/` (GAP-EXIT-1029, EARN-GAP-REVIEW-1104, LOSS-PM-1104, ALPHA-ENGINE-1104).
3. **Several "⏩ LATEST" headers.** `handoff.md` (4,755 lines) has blocks still titled LATEST from 10-29, 11-01 and
   10-28 below the real latest block. A new account can pick up the wrong one.
4. **Today's pick-up block is already behind main.** See `logs/research/open_queue_2026-11-04.md` section A
   (#825, #836, #837/#838 shown as unshipped).
5. **Project context in `CLAUDE.md`** ("12-point confluence system (live) … MIN_SCORE=9/12", "Account ~$2.5K", RC
   counts "as of 2026-06-15") predates the tier rebuild (Day / Swing / QHM / F6 / trend_night).

## 4. Fixes

These edits touch `CLAUDE.md` and `handoff.md`, which need a Gro + GAI marker before commit (`preship_gate.py:63`,
`GATED_SPECIFIC`), so they go through the Mac session.

1. `CLAUDE.md:421`: replace with "At the end of a session, offer `/wrap-up` once; run it only when Rafael asks (see
   WRAP-UP — NEVER AUTO-TRIGGER)."
2. `handoff.md:5-8`: point (4) at `logs/design_records/` and `logs/research/open_queue_2026-11-04.md`, and drop the
   July files.
3. `handoff.md`: rename every block below the latest to PREVIOUS / EARLIER. Move blocks older than about 2 weeks to
   `docs/archived/handoff_archive_2026.md`, keeping a pointer.
4. `logs/project-state.md`: rewrite to the current state, then push it to the notebook.
5. **Master Brain clean-up (on the Mac, owner):**
   - `notebooklm login`, then `notebooklm use $(cat ~/.claude/master_brain_id)` and confirm the ID matches `CLAUDE.md:423`;
   - `notebooklm source list`, and delete superseded `project-state` / handoff copies older than the latest;
   - add the current `project-state.md`, `handoff.md` and `open_queue_2026-11-04.md`;
   - run the `CLAUDE.md` session query and confirm the answer mentions 11-04 items (GAP-EXIT, EARN-GAP-REVIEW, LOSS-PM).
6. **Make it fail loudly.** Have the session-start skill print the notebook's newest source date and warn when it
   is older than the newest `handoff.md` block. That makes a stale or broken Master Brain visible instead of silent.

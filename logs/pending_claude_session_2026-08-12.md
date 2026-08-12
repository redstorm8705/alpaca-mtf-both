# APPROVAL PACKAGE — Autonomous session 2026-08-12 (AWP rolling chain)

**Scheduled/autonomous session. NOTHING has been shipped or applied — this package stops at
diagnosis + a scoped proposal, per the AWP hard rule.** All findings below were verified at
source (live code, board 3/3 vote, cold second-agent, static analysis).

---

## 🔧 ITEM 1 (RTH-CHAIN DRAFT READY) — Phase 2a.5 broker re-verify guard
### `execution/portfolio_tracker.py` — Finding A from 2026-08-11 AWP, root cause now patched

**THE FIX (plain English):**
The bot's nightly reconciliation (`write_eod_summary()` Phase 2a.5) was deciding that an
overnight position was "closed" purely because it disappeared from a FIFO lot reconstruction —
but the FIFO reconstructor has a designed fail-safe (S49) that deliberately drops symbols when
lot-matching fails. That meant a FIFO-reconstruction failure and a genuine broker close looked
identical to Phase 2a.5, so it was fake-closing live positions. This is what happened to SMCI
on 2026-08-10 (3 real shares, two fabricated $0.005 "close" records).

The fix: before any of the three code paths that can mark a position closed in Phase 2a.5,
add a live broker query (`get_open_position(symbol)`) — the same pattern orphan_manager.py
already uses safely. If the broker says the position is still open: log CRITICAL + Slack,
keep the position, do nothing. If the broker confirms the position is gone (returns None):
proceed with reconciliation as before. If the broker query itself errors: fail-closed
(also log CRITICAL + Slack, keep the position).

**FILES CHANGED:** `execution/portfolio_tracker.py` only (no other files touched)

**WHAT'S READY:**
- `logs/pending_patch_2026-08-12_portfolio_tracker.patch` — exact unified diff, git-apply-ready
- `logs/pending_gro_gai_2026-08-12_portfolio_tracker.json` — exact Gro+GAI prompts (Step 4 + Step 8)

**GATES CLEARED IN THIS AUTONOMOUS SESSION:**
- Board 3/3 PASS (3 independent cold agents, 3 rounds to converge on RC-3 fix)
- Cold second-agent: PASS (6 checks: no logic inversion, no boundary, complete branches, no unbound refs)
- Static analysis: py_compile PASS, mypy PASS, ruff PASS

**GATES THAT REQUIRE INTERACTIVE SESSION (before apply):**
- [ ] Step 4: Gro + GAI audit of the diff (prompts in pending_gro_gai JSON, ready to run)
- [ ] Step 7: Rafael's explicit APPROVE
- [ ] Step 8: FINAL PRE-SHIP Gro + GAI audit of exact diff (same JSON)
- [ ] preship_gate markers: record_adversarial.py + record_cold2.py
- [ ] Apply → commit → push → OCI git pull + restart

**TO PICK UP (interactive session):**
```bash
# 1. Verify source hasn't drifted since draft:
sha256sum execution/portfolio_tracker.py
# Must be: 49081cf8b8c98b7045fb298951beb52bfb862cb9f4c41c3e5ebf04b7d584b845

# 2. Read the pending files:
cat logs/pending_patch_2026-08-12_portfolio_tracker.patch
# 3. Run Gro/GAI from pending_gro_gai JSON (see gro_prompt + gai_prompt fields)
# 4. Apply with: git apply logs/pending_patch_2026-08-12_portfolio_tracker.patch
```

**CONSENSUS:**
Board: 3/3 PASS | Gro: PENDING (prompt ready) | GAI: PENDING (prompt ready)
All board blockers resolved: Yes

**RISK IF APPROVED:** Minimal — adds 70 lines of guard logic at EOD, no RTH impact.
The guard only adds a broker call before actions that were already happening; it cannot
*cause* a false close. Fail-closed means worst case is deferred reconciliation (manual).

**RISK IF REJECTED:** SMCI class of fake-close can recur on any future overnight position
whose FIFO lot-matching fails. Two fabricated trades per incident in Kelly stats.

---

## ⚠️ ITEM 2 (Carry-forward from 2026-08-11) — Dashboard live-process anomaly (Finding B)

NOT re-investigated this session. Status unchanged from 2026-08-11 package:
PR #130 (QHM stop fix) is correct in code but still showing "—" on live dashboard.
Cheap next step: `systemctl restart mtf-bot`, then re-check dashboard.
If restart fixes it: investigate what isn't being refreshed in the live process.
If restart doesn't fix it: full patch sequence on `generate_dashboard.py`.

---

## ⚠️ ITEM 3 (Carry-forward from 2026-08-11) — Gate #4 design proposal (BGG design-record)

NOT re-investigated this session. Status from 2026-08-11: design proposal ready, awaiting
Rafael's go-ahead to build. See 2026-08-11 pending_claude_session for full design table.

**YOUR DECISIONS:**
- Item 1 (Phase 2a.5 patch): ready to proceed. Next step: run Gro/GAI from JSON → approve → apply.
- Item 2 (dashboard): check `systemctl restart mtf-bot` when convenient.
- Item 3 (Gate #4 design): APPROVE to build / DEFER / send back for more design work.

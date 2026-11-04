# APPROVAL PACKAGE — Human-confirmed ledger-heal (2026-08-08)

**For Rafael's review + one-click approval. NOTHING has been shipped.** Branch:
`fix/ledger-heal-human-confirmed-2026-08-08` (3 files staged, +277/-11).

## What it fixes (your items #1 + #4)
The never-sell ownership ledger froze **stale forever** after any manual close of a QHM/Forever-6 hold
(the never-shrink guard refused to heal down, with no way to distinguish a legit manual close from a
breach). That caused the 17× `ledger_sync STALE` spam, the naked-stop cascade, and manual closes
breaking the tracker/aggregation. Two fully-automatic designs were board-REJECTED first (v1 laundered a
breach; v2's "owner claim" was itself broker-net-derived). The board proved: **no automated signal can
tell your manual reduction from a breach — only a human can.**

## The design (human-confirmed, still dynamic)
- **Default: REFUSE every protected-tier shrink** (stale-but-safe — the bounded, reversible error) and
  **page you** with the exact confirm command. A breach you didn't confirm stays frozen — never laundered.
- **Heal down ONLY** when BOTH hold: (1) the caller verified a **settled/stable/complete** Alpaca snapshot
  (`positions_settled`), and (2) **you explicitly confirmed** this exact reduction via `confirm_ledger_heal.py`
  — a **one-shot, 2h-expiring** confirmation matched on the **live snapshot** (net unchanged since confirm,
  `0 ≤ target ≤ net`). On a valid confirmation it **overrides** the tier to your confirmed target (clamped
  to net; handles aged-out replays and full closes to 0). Consumed one-shot after a successful write.

## How you use it (replaces the manual JSON poke)
When you manually close part/all of a quarterly/forever hold, on the box run:
```
cd /home/ubuntu/mtf-bot && python3 confirm_ledger_heal.py SYMBOL TIER TARGET
# e.g. sold 1 of 3 NVDA QHM shares → real qty now 2:   python3 confirm_ledger_heal.py NVDA qhm 2
```
The next `ledger_sync` (RTH cron) verifies the live snapshot still matches and heals it. If a reduction
appears that you did NOT confirm, Slack pages you to confirm-or-investigate; sells stay frozen until then.
(NVDA/GOOGL are already manually corrected — this makes future closes self-serviceable, not manual pokes.)

## Gate results
- **Statics:** py_compile / ruff / mypy — CLEAN (all 3 files).
- **Functional safety suite:** 10/10 PASS (added: multi-tier over-confirm -> fail-safe REFUSE) — (1) no-confirm→REFUSE+page; (2) aged-out+confirm→HEAL(override);
  (3) not-settled→REFUSE; (4) net-moved→REFUSE; (5) expired→REFUSE; (6) breach 5→3 no-confirm→REFUSE
  (not laundered); (7) target>net→REFUSE; (8) full-close confirmed→HEAL to 0; (9) full-close no-confirm→REFUSE.
- **Gro:** no concrete failing input (nits all non-issues on inspection).
- **GAI (free key):** ACCEPT — clean/sound/complete; no path unprotects a hold or launders a breach.
- **Board masked-loss (Taleb seat):** ✅ APPROVE — 'v2 net-derived laundering genuinely CLOSED; never-unprotect asymmetry holds.' Its 2 required changes (joint prot_sum<=net clamp + _entry rename) APPLIED + verified.
- **Cold-2nd:** ✅ PASS — 'no unconfirmed protected reduction can be written; no confirmed reduction can exceed net or leave sum!=net; unsettled reads block all overrides.'

## To SHIP (when you approve — interactive session only; scheduled sessions never ship)
1. FINAL preship Gro+GAI on the exact staged diff (`.claude/preship/preship_audit.py execution/ownership_guard.py run_ledger_sync.py confirm_ledger_heal.py --context <facts>`), record cold-2nd marker.
2. `git commit` → `git push` → `gh pr create` → **normal CI** (Actions healthy — no bypass) → merge.
3. OCI: `git pull origin main --ff-only`. **No bot restart needed** — `sync_ledger` is the standalone RTH
   cron tool and `check_never_sell_floor` (the wired live gate) is UNCHANGED. Monday's first `ledger_sync`
   runs the new code.
4. Update handoff.md + tb_audit_log; push Master Brain (once notebooklm reauth sticks).

## Forward-build (logged, separate gated diff)
QHM stop-rearm ↔ `cancel_stray_sell_orders` naked-window audit (the NVDA stop_order_id clear was a
band-aid; the ledger heal removes the drift trigger, but the guard/stale-id interaction deserves its own
audit). See `logs/ledger_heal_v2_design_2026-08-08.md`.

# alpaca-mtf-bot — Project State (current, overwritten each session)
**As of 2026-07-28 (resume session, Rafael present) — canonical state for Master Brain.**

## Bot
- OCI (137.131.51.250): mtf-bot, mtf-writer, mtf-http, nginx all **active** (verified via health check). HTTP dashboard OK. main = OCI in sync (`9eec0c2`).
- Paper account, aggressive growth ($2.5K → $25K). Safety invariants intact (paper=True hardcoded, 7% kill switch, P&L from Alpaca fills only).

## Shipped & deployed this session (3 gated ships)
1. **Phase 1** — 4 Gemini-report noise fixes (audit_slack, nightly_audit, midday_audit, pnl_ledger). PR #33 → main. Stops the false pre-heal-drift CATASTROPHIC alarms; adds a live naked-stop check to the midday report.
2. **CI majority-vote** — `.github/scripts/ci_audit.py` runs the server-side Gemini preship audit 3× and ships iff ≥2/3 APPROVE (the reviewer was stochastic — approved/rejected the same diff run-to-run). PR #35 → main. Self-validated 3/3 in prod.
3. **Phase 2 / D1 root fix** — `trade_logger.py`: a `numpy.bool_` in the entry `conditions` payload made json.dumps raise, and a broad except swallowed it → EVERY `entry` event silently dropped for 7 days (07-20..27) while exits wrote → all 3 daily audits re-derived a phantom "0 trades / accounting failure". Fixed at the serialization boundary (`default=_json_default`). PR #36 → main, OCI restart, verified live. Root of ~50–60% of the report noise.

4. **Observability follow-up / D1 hardening — SHIPPED 2026-07-28 (Rafael present).** `trade_logger.py` write-failure `except` escalated: silent `logger.warning` → `logger.error` + ONE throttled operator Slack (≤1/hour). Throttle stamped BEFORE the synchronous ~4s send → bounds run_cycle-thread blocking to ≤4s/hour; `alerts` imported lazily inside the failure path (top-level import could trip portfolio_tracker's `_log_event: pass` fallback and silently kill ALL logging — the D1 class); in-memory throttle; init `0.0` so the FIRST failure alerts immediately. Board 2/2 + Gro+GAI APPROVE + cold-2nd PASS + statics clean + failure path runtime-verified. PR #37 → main `9eec0c2` (patch c01e852), OCI restart + DEPLOY_OK, verified loaded on live venv. Closes the observability gap that let D1 hide 7 days.

## Open items (next session)
- D2 / Phase 3: meta Groq prompt dead 6/6 days (400 prompt-too-big); per-report tune-ups; trim 11k-row delta_shadow bloat; repo-manifest + dead-voice alert.
- PARKED: options_scanner.py `zdte_close_times()` wiring (helper added, unused, un-shipped).

## Key decisions
- CI fix = majority-vote (root fix for a stochastic reviewer; no fragile parsing, no fail-open).
- Phase 2 fixed at the serializer, not per-field — "make the wrong thing impossible."
- Blocked-but-verified-correct deploys: admin-merge-now + fix-the-gate-after (Rafael).

## Corrections
- A "push-wash gap" claim was FALSE (preship already diffs full PR-vs-base) — caught via verify-at-source and corrected before building.

## Infra
- Resume cron `mtf-bot-autonomous-resume` re-armed → fires 3:12 AM PT 2026-07-28.
- `main` protection: enforce_admins=true, strict=true, only gate = green `preship` (now majority-vote). Merge a strict-blocked PR by merging origin/main into the branch first, then normal `gh pr merge`.

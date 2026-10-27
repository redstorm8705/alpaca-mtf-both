# Session Summary — 2026-10-26 evening PT (signed: Claude)
**Project:** alpaca-mtf-bot
**Duration:** long session across 10/25–10/26 (continued after compaction)

## Bugs Fixed / Shipped
| ID | File | Fix |
|----|------|-----|
| REVIEWER-SWAP-1025 (#768) | preship_audit.py, bgg_call.py, gai_client.py, ci_audit.py | BGC gate: Codex reviewer #2, Gro fallback; Gemini model ladder fix |

## Built, not shipped
- TRACK-A-STOP-1026: Track A stop >= 6 x time-of-day 5-min noise, whole-lot target 0.35 x 20-day median range,
  existing sizing at the wider stop. Patch: `logs/wip/track_a_stop_1026_wip.patch`. Board risk seat found SNDK sizes
  to 0 at the open (room $80.60 > $39.75 budget) — needs the one-share room cap before ship.

## Decisions Made
- BGC (Board + GAI + Codex) is the approval gate; Groq only as Codex backup. Bake-off retired; approval-parser re-land dropped.
- Owner: Track A stays the larger-size tier; do not cut its size, only the existing per-trade budget trims shares.

## Corrections (Claude was wrong)
- Proposed "fewer shares so dollar risk never above today" for Track A -> owner: Track A is the larger tier; keep its size rule.
- Replay v1 too generous (Harris) and had range look-ahead (Thorp) -> v2/v3 realistic fills, prior sessions only.
- Replay v3 forced a one-share floor the code lacks -> the risk seat caught the SNDK zero-share skip.

## Findings
- TSLA 10/26: 2R target ($409.91) unreachable on a $13.54-range day; peak +0.5R; flattened -$6.91.
- Track A 30 trades: target touched 8/30; half of stops inside normal 5-min noise.
- Replay v3 (today's size kept, $40 budget): -$219.57 -> -$73.01 over 30; test half -$177.21 -> -$48.26.
- Real-data probe: at 09:40 the k=6 room is 3-5x the 0.35-range target; about 1x by early afternoon.
- BV-5: 15-minute data delay raises live-score match 50% -> 64.6% (hypothesis — unverified).

## Open Items
- [ ] TRACK-A-STOP-1026: one-share room cap + test, record Q2/Q5/Q11 fixes, break-even hit-rate reversal, GAI+Codex preship, owner capture (`approve track a stop 1026`), ship, pre-market simulation.
- [ ] BV-5: test 5/10/20-min delays and live bar timestamps, then owner package.
- [ ] #736 FIFO phantom (TSLA net -2) false CRITICAL pages.
- [ ] Post-live audits overdue (#768, #764, #760, #758, #753 and older); issues #749/#750/#751/#746/#777.
- [ ] Bake-off stash `bakeoff-wip-claude-1025-b8b86eba` — rebuild on main, do not pop.

## User Preferences Observed
- Wants queued work durable and pick-up-able by other Claude sessions and Codex.
- Pushes back on any proposal that shrinks the larger-size tier; asks "why" in plain terms.

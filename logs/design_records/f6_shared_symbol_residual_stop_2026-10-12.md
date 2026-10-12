# F6 shared-symbol residual stop coverage — 2026-10-12 (OpenAI Codex)

## Problem

`execution/stop_protection.py` excludes a tracker-open symbol whenever F6 owns any shares. That protects the never-sell F6 core from a full-net stop, but it also leaves a co-held Swing lot outside the reconciler. The broker already supports tier-tagged, floor-bounded stops and preserves another tier's orders; the reconciler must request only the verified Swing residual.

## Design

This increment remains dormant while `OWNERSHIP_GUARD_ENFORCE=False`. Once enforcement is enabled, the reconciler builds one account-wide canonical ownership snapshot from the same live position snapshot used for stop decisions. On a symbol with a positive QHM or F6 claim it requires all of the following before recognizing or placing a Swing stop:

1. every ownership source is readable;
2. the snapshot broker quantity equals the live position quantity and direction;
3. all tier claims fully explain the broker net with zero residual;
4. QHM and F6 claims are non-negative whole shares;
5. the Swing claim is same-direction, whole-share, and exactly equals the tracker trade's own quantity; and
6. every reducing order used as Swing protection carries an exact `IN-` owner tag. A reducing order with unknown ownership makes the state ambiguous and blocks placement.

The target stop quantity becomes the verified Swing claim, not the broker net. Known QHM, F6, and Day stops do not count as Swing protection and are never cancelled. The existing broker call remains `tier="intraday"` and `allow_cancel_blocking=False`, so its ownership floor and foreign-stop recovery remain the final submission chokepoint.

Any mismatch pages and places nothing. An unrelated non-protected Swing symbol retains the existing behavior. When ownership enforcement is off, the existing QHM/F6 whole-symbol exclusions remain unchanged.

## Test matrix

- F6 + Swing and QHM + Swing: place only the Swing residual.
- Existing exact Swing stop protects only the Swing claim; a foreign stop does not.
- QHM/F6/Day known foreign stops are preserved.
- untagged reducing order, unreadable snapshot, nonzero residual, tracker mismatch, opposite direction, fractional protected claim, and broker-net mismatch all block and page.
- enforcement off preserves the legacy exclusions.
- ordinary unprotected symbols keep the existing full-net behavior.

## Launch boundary

This diff does not enable F6 or the ownership guard. Pending F6-buy reservation, bounded F6 entry, trim resync, allocator and kill-switch attribution, emergency flatten, ETF eligibility, simulations, and the final flag flip remain required.

## Signature

Built by **OpenAI Codex**. Every commit and PR carries `(OpenAI Codex)`.

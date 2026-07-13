# ERRORS.md — Failure Log

Tracks approaches that took more than 2 attempts. Check this before suggesting approaches to similar tasks.

**Format per entry:**
- **What didn't work:** description of failed approach(es)
- **What worked instead:** the successful approach
- **Note for next time:** actionable guidance

---

<!-- Entries added below as failures are encountered -->

## 2026-07-03 — Market-status claim fabricated from assumption
**What didn't work:** Stated "market traded today with a 1 PM ET early close" from memory of NYSE early-close patterns. July 4, 2026 = Saturday → holiday OBSERVED Friday 7/3 → full closure, no trading. Claim was false; user caught it.
**What worked instead:** Alpaca `GET /v2/calendar` (date absent = full holiday) + `GET /v2/clock` (is_open, next_open) — authoritative in one call.
**Note for next time:** Never infer market schedule from the calendar date. Verify date/time via `TZ=America/Los_Angeles date` and market status via Alpaca clock/calendar before any schedule claim. Saturday holidays observe backward to Friday; Sunday holidays forward to Monday — easy to invert, so don't rely on the rule, query the API.

## 2026-07-03 — Editing live-state files (trade_log.json / kelly_stats.json) under a running bot
**What didn't work:** Ran a correction script to mark phantom trades + rebuild kelly_stats while mtf-bot was still active. The live process held closed_trades in memory and flushed it back to disk (periodic _save_log / shutdown), clobbering the on-disk markers. Post-restart reload showed the OLD 80-trade state, 0 markers.
**What worked:** `systemctl stop mtf-bot mtf-writer` → run correction → `systemctl start`. Reload then showed the corrected 76-trade state with markers intact.
**Note for next time:** Any one-time mutation of a JSON state file that a live service also writes (trade_log.json, kelly_stats.json, hybrid_state.json, quarterly_holds.json) MUST stop the writing service first. Editing under a live writer is a lost-update race.

## 2026-07-13 — Manual QHM stop-safe add (GOOGL) took 3 attempts
**What didn't work:**
1. Alpaca DELETE /v2/orders/{id} returns HTTP 204 with EMPTY body → `json.load(resp)` threw JSONDecodeError, crashing the script AFTER the stop was already cancelled (left GOOGL momentarily unprotected).
2. Restarting the bot between attempts: the QHM manager resubmits a fresh GTC stop on resume (new order id), so a hardcoded/stored stop id is stale → the new stop blocks the add buy as a wash trade (40310000 "opposite side market/stop order exists").
**What worked:** (a) REST helper must tolerate empty bodies: `raw=resp.read().decode().strip(); json.loads(raw) if raw else {}`. (b) Keep the bot PAUSED for the ENTIRE operation — do not start/stop between steps. (c) Cancel the CURRENT live stop id fetched dynamically (GET open orders, cancel all GOOGL sell-stops), never a stored id; verify zero open sell orders before buying.
**Next time:** use the one-shot script pattern (pause → fetch+cancel live stops → buy marketable-limit → poll → resubmit stop for new qty → atomic json update → resume). GOOGL result: 2sh @ $359.30, stop bbed81da.

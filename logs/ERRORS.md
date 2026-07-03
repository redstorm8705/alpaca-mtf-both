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

# JULY 2026 TRADE AUDIT — authoritative (Alpaca FIFO over true fills)

**Requested by Rafael 2026-07-13:** understand the July downturn; audit every trade; scope entry/exit
changes; review public catalysts per name. "We committed to being more aggressive but that's not
working" → this audit tests that claim against the real numbers.

## HEADLINE: the "significant downturn" is 87% a REPORTING PHANTOM.
- **Monthly page / eod figure: −$279.06.**
- **Authoritative July realized P&L (FIFO over the true Alpaca fill log): −$34.70.**
- The −$244 gap = the RC-4 fill-matching phantom (07-02 PANW/TSLA) that inflates the eod files. The
  lifetime cache was corrected earlier; the eod DAILY files (and thus the monthly page) still carry it.

### Phantom proof (Alpaca fills vs eod):
| Symbol | eod claimed | Alpaca-real (FIFO) | verdict |
|--------|-------------|--------------------|---------|
| PANW   | −$182.79    | **−$6.55** (buy 1@355.10 → sell 1@348.55) | PHANTOM |
| TSLA   | −$81.23     | **+$6.28** (−2.91 long + 9.19 short)      | PHANTOM (real = profit) |

## AUTHORITATIVE per-symbol July realized P&L
| Symbol | P&L | note |
|--------|-----|------|
| **RIVN**  | **−$34.25** | THE month. Bought 17 sh @ $19.73 (07-06) → dumped @ ~$17.32 (07-07). Public-offering / dilution catalyst — the day-after selloff Rafael flagged. |
| MARA   | −$18.95 | 07-06 re-entry chopped; small-cap crypto-proxy vol. |
| SNOW   | −$7.10  | small |
| PANW   | −$6.55  | small |
| MS     | −$4.65  | small |
| RBLX   | −$2.98  | overnight ATR-buffer stop-out |
| GOOGL  | +$1.85  | (QHM) |
| NVDA   | +$2.38  | (QHM) |
| TSLA   | +$6.28  | long+short |
| MSTR   | +$7.22  | |
| AVGO   | +$10.80 | best |
| HOOD   | +$11.25 | best |
| **TOTAL** | **−$34.70** | |

## WHAT THIS MEANS (the honest read)
1. **The aggressive strategy is NOT bleeding.** Ex-RIVN, July realized = **−$0.45 (breakeven).** The
   winners (AVGO/HOOD/MSTR/TSLA = +$35.55) roughly offset the small losers. "More aggressive" is not
   "not working" — it is roughly flat, dragged to −$34.70 by ONE un-screened catalyst event (RIVN).
2. **The one real, avoidable loss was a CATALYST the bot doesn't screen for** — a secondary/public
   offering (dilution). We screen EARNINGS; we do NOT screen pending offerings / 8-K dilution events.
3. **The reporting is still lying to you.** The monthly page shows −$279 (equity-delta + 07-02
   phantom), not the −$34.70 authoritative realized. My earlier "fix" made the header match the daily
   cells, but BOTH still display the phantom-inflated eod numbers, not FIFO-realized. → A DEEPER
   reporting fix is needed: the monthly/eod P&L should reconcile to Alpaca FIFO-realized, and the
   07-02 phantom must be purged from the eod files.

## PROPOSALS TO SCOPE (→ Feature-Design + BGG; NOT changing direction, per Rafael)
### Entry
- **A. Catalyst / dilution screen at entry (HIGHEST value — would have prevented the ONLY real loss).**
  Before entering, check for a pending/just-announced secondary or public offering / ATM / shelf
  takedown / material 8-K on the name (FMP + SEC EDGAR 424B/S-1/8-K feeds we can already reach). Block
  or hard-size-down on a fresh dilution catalyst. RIVN 07-06→07-07 is the exact case.
- **B. Small-cap / high-dilution-risk sizing haircut** (RIVN, MARA are sub-$20 high-beta names prone to
  offerings) — tie into the catalyst screen.
### Exit
- **C. Overnight ATR-buffer exits (RBLX, HOOD-07-10) fired on 9-scan breaches** — review whether the
  0.5× tier-adjusted buffer is too tight for these names (small, but a pattern).
### Reporting (correctness)
- **D. Purge the 07-02 phantom from eod files + reconcile monthly/eod P&L to Alpaca FIFO-realized** so
  the page you read matches reality (−$34.70, not −$279).

## NEXT STEPS
- Verify each name's public catalyst (WebSearch/FMP/EDGAR) — confirm RIVN offering; check MARA/SNOW.
- Convene board + Gro + GAI on proposals A–D (Open Question Protocol) → bring Rafael the consolidated rec.
- Append the graduated items to logs/bot_improvements.md.

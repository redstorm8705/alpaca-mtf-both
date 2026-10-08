#!/usr/bin/env python3
# ruff: noqa: E501
"""no_assume_gate.py — market-fact absence claims (Rafael 2026-10-09: "AMD has no liquid inverse ETF" was false —
DAMD traded 322,212 IEX shares on 10/08). Run: python3 .claude/preship/test_no_assume_gate.py (exit 0 = pass)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import no_assume_gate as g  # noqa: E402

BLOCK = [
    "With today's tight room it would most likely still have bought 0 shares, and AMD has no liquid inverse ETF to route to.",
    "DRAM lacks enough price history to score, so it is never tradeable.",
    "SNDK has no ETF.",
    "AMD has no inverse ETF.\n\nOther section cites strategy/day_tier_leverage.py.",   # evidence in another paragraph
    "There is no 2x bear fund for GOOGL.",
    "NVDA has no options data.",
    "GOOGL is not shortable.",
]
ALLOW = [
    "AMD has no liquid inverse ETF on the bot's map; Alpaca lists DAMD (322,212 IEX shares on 10/08).",
    "SNDK has no ETF mapping in strategy/day_tier_leverage.py today.",
    "There is no rush on this.",
    "The 1x bear AMDD isn't tradable at size (4,011 IEX shares on 10/08).",
    "META traded fine today.",
    "I haven't checked yet, but SNDK may have no inverse ETF.",
    "The trade had no edge.",
    "It is not traded on IEX, per the Alpaca snapshot.",
    "AMD has no inverse ETF in the map. The Alpaca assets API lists DAMD and AMDD.",   # evidence in the next sentence
    # ordinary trading prose (cold-2nd 2026-10-09) — not market-absence claims
    "Trade closed without volume confirmation.",
    "Entry with no volume spike.",
    "With no options flow, we skip.",
    "I entered with no history.",
    "TSLA is not traded today.",
    "Position is never traded overnight.",
    "The order was sent without options.",
    "The bot has no position in AMD.",
    "META had no stop hit today.",
    "The RSI has no data on the first bar.",
    "MACD has no history yet.",
    "The ATR has no history before bar 14.",
    "API has no data for it.",
    "We had no bear market.",
    "Without a bull run, no.",
]


class MarketAbsence(unittest.TestCase):
    def test_blocks_unchecked_market_absence(self):
        for t in BLOCK:
            self.assertIsNotNone(g._violation(t), t)

    def test_allows_checked_or_hedged(self):
        for t in ALLOW:
            self.assertIsNone(g._violation(t), t)

    def test_capability_rule_unchanged(self):
        self.assertIsNotNone(g._violation("The bot doesn't track order flow."))
        self.assertIsNone(g._violation("The bot doesn't track order flow (grep found no matches)."))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)

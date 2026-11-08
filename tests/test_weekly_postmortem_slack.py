# ruff: noqa: E501
"""WTP-SLACK-1108: the phone-first weekly Slack message (logs/design_records/wtp_slack_layout_2026-11-08.md).

The fixture is the real week of 2026-11-02 from logs/wtp_2026-11-06.md on OCI. No test reaches Alpaca or Slack."""
from __future__ import annotations

import re
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import weekly_postmortem as wtp

MON, FRI = date(2026, 11, 2), date(2026, 11, 6)
# (tier, symbol, direction, lifecycle P&L) — the 27 completed lifecycles of the 11-02 week.
ROWS = [
    ("day", "TSLA", "long", -8.15), ("day", "COIN", "short", -8.81), ("qhm", "GE", "long", -6.55),
    ("unattributed", "GE", "long", 14.51), ("day", "SNDQ", "long", 22.68), ("day", "AMZN", "short", 8.70),
    ("swing", "AAPL", "long", -17.81), ("swing", "XOM", "long", -34.98), ("swing", "QQQM", "long", 3.68),
    ("day", "AMZN", "long", 9.64), ("unattributed", "GE", "long", 43.76), ("day", "AAPL", "long", -8.98),
    ("day", "MSFT", "long", -2.59), ("qhm", "NVDA", "long", -33.97), ("qhm", "NVDA", "long", -23.40),
    ("day", "NVDA", "short", 7.76), ("day", "NVD", "long", 6.79), ("day", "AVS", "long", 2.96),
    ("day", "NVDA", "short", 3.88), ("day", "PLTD", "long", 0.14), ("day", "TSLA", "short", 4.45),
    ("day", "MU", "short", 41.13), ("day", "AVS", "long", -9.40), ("day", "META", "short", 0.18),
    ("day", "NVDA", "short", 32.62), ("day", "SNDK", "short", 24.34), ("day", "AVGO", "short", 6.78),
]


def _trades():
    return [{"tier": t, "symbol": s, "direction": d, "pnl": p} for t, s, d, p in ROWS]


def _stats(**over):
    st = {
        "total_pnl": 101.98, "pnl_check": "OK", "count": 25, "winners": 15, "losers": 10,
        "weekly_by_tier": {"day": 156.74, "swing": -49.11, "qhm": -63.92, "forever_6": 0.0},
        "manual": {"pnl": 58.27, "count": 2, "symbols": ["GE"]},
        "partial_open_lifecycles": 1, "partial_open_weekly_pnl": 2.20,
        "tier_goals": [{"tier": "day", "status": "beat"}, {"tier": "swing", "status": "within range"},
                       {"tier": "qhm", "status": "within range"}, {"tier": "forever_6", "status": "insufficient trades"}],
    }
    st.update(over)
    return st


def _msg(stats=None, trades=None, equity=2731.40, note=""):
    return wtp._compose_slack(stats or _stats(), _trades() if trades is None else trades,
                              Path("logs/wtp_2026-11-06.md"), MON, FRI, note, equity)


def _dollars(text: str) -> list:
    return [float(m.group(1) + m.group(2).replace(",", ""))
            for m in re.finditer(r"([+−-])\$([\d,]+\.\d\d)", text.replace("−", "-"))]


class ComposeRealWeek(unittest.TestCase):
    def test_headline_and_check_line(self):
        m = _msg()
        self.assertIn("📊 *Week of Nov 2–6: +$101.98* ✅", m)
        self.assertIn("Checked against Alpaca twice; both counts match.", m)
        self.assertNotIn("don't add up", m)

    def test_tier_lines_match_the_report_and_sum_to_the_total(self):
        m = _msg()
        self.assertIn("✅ *Day* +$156.74 · won 14 of 19 · beat its last 4 weeks", m)
        self.assertIn("🔻 *Swing* −$49.11 · won 1 of 3", m)
        self.assertIn("🔻 *QHM* −$63.92 · won 0 of 3", m)
        self.assertIn("➖ *F6* no trades", m)
        self.assertIn("✋ *Your GE trades* +$58.27", m)
        st = _stats()
        self.assertAlmostEqual(sum(st["weekly_by_tier"].values()) + st["manual"]["pnl"], st["total_pnl"], places=2)

    def test_best_and_worst_are_bot_trades_only(self):
        m = _msg()
        best = m.split("🏆 *Best*")[1].split("💸")[0]
        worst = m.split("💸 *Worst*")[1].split("👀")[0]
        self.assertEqual([ln for ln in best.strip().splitlines()],
                         ["• MU short +$41.13", "• NVDA short +$32.62", "• SNDK short +$24.34"])
        self.assertEqual([ln for ln in worst.strip().splitlines()],
                         ["• XOM long (Swing) −$34.98", "• NVDA long (QHM) −$33.97", "• NVDA long (QHM) −$23.40"])
        self.assertNotIn("+$43.76", m)   # the owner's GE trade is not the bot's best trade

    def test_watch_names_the_worst_tier_and_its_symbol(self):
        self.assertIn("👀 *Watch:* QHM lost $63.92 this week; its biggest loser was NVDA at −$57.37.", _msg())

    def test_partial_goal_and_footer(self):
        m = _msg()
        self.assertIn("Includes +$2.20 from 1 still-open trade(s) partly sold.", m)
        self.assertIn("Account $2,731.40 · goal $25,000", m)
        self.assertTrue(m.rstrip().endswith("_Full table and analysis: `logs/wtp_2026-11-06.md`_"))

    def test_every_dollar_shown_comes_from_the_figures(self):
        st, allowed = _stats(), set()
        allowed |= {round(v, 2) for v in st["weekly_by_tier"].values()}
        allowed |= {st["total_pnl"], st["manual"]["pnl"], st["partial_open_weekly_pnl"], 57.37, -57.37, 63.92}
        allowed |= {round(p, 2) for *_x, p in ROWS}
        for v in _dollars(_msg()):
            self.assertIn(round(v, 2), allowed)

    def test_short_enough_for_a_phone(self):
        self.assertLessEqual(len(_msg().splitlines()), 25)
        self.assertNotIn("```", _msg())


class ComposeEdgeCases(unittest.TestCase):
    def test_losing_week_shows_red_headline(self):
        st = _stats(total_pnl=-40.0, weekly_by_tier={"day": -40.0, "swing": 0.0, "qhm": 0.0, "forever_6": 0.0},
                    manual={"pnl": 0.0, "count": 0, "symbols": []}, partial_open_lifecycles=0)
        tr = [{"tier": "day", "symbol": "AMD", "direction": "long", "pnl": -40.0}]
        m = _msg(st, tr)
        self.assertIn("📊 *Week of Nov 2–6: −$40.00* 🔻", m)
        self.assertIn("🔻 *Day* −$40.00 · won 0 of 1", m)
        self.assertNotIn("🏆", m)
        self.assertNotIn("✋", m)

    def test_all_tiers_green_says_nothing_stood_out(self):
        st = _stats(total_pnl=10.0, weekly_by_tier={"day": 10.0, "swing": 0.0, "qhm": 0.0, "forever_6": 0.0},
                    manual={"pnl": 0.0, "count": 0, "symbols": []}, partial_open_lifecycles=0)
        m = _msg(st, [{"tier": "day", "symbol": "AMD", "direction": "long", "pnl": 10.0}])
        self.assertIn("👀 Nothing stood out: no tier lost money.", m)
        self.assertNotIn("💸", m)

    def test_missing_tier_value_is_unknown_not_green(self):
        st = _stats(weekly_by_tier={"day": 156.74, "swing": None, "qhm": -63.92, "forever_6": 0.0})
        m = _msg(st)
        self.assertIn("? *Swing* unknown", m)
        self.assertNotIn("✅ *Swing*", m)
        self.assertNotIn("🔻 *Swing*", m)

    def test_unknown_tier_never_says_nothing_stood_out(self):
        st = _stats(total_pnl=10.0, weekly_by_tier={"day": 10.0, "swing": None, "qhm": 0.0, "forever_6": 0.0},
                    manual={"pnl": 0.0, "count": 0, "symbols": []}, partial_open_lifecycles=0)
        tr = [{"tier": "day", "symbol": "AMD", "direction": "long", "pnl": 50.0},
              {"tier": "swing", "symbol": "X", "direction": "long", "pnl": -40.0}]
        m = _msg(st, tr)
        self.assertIn("? *Swing* unknown", m)
        self.assertNotIn("Nothing stood out", m)

    def test_break_even_tier_is_neither_green_nor_red(self):
        st = _stats(total_pnl=0.0, weekly_by_tier={"day": 0.0, "swing": 0.0, "qhm": 0.0, "forever_6": 0.0},
                    manual={"pnl": 0.0, "count": 0, "symbols": []}, partial_open_lifecycles=0)
        tr = [{"tier": "day", "symbol": "A", "direction": "long", "pnl": 5.0},
              {"tier": "day", "symbol": "B", "direction": "long", "pnl": -5.0}]
        m = _msg(st, tr)
        self.assertIn("➖ *Day* +$0.00 · won 1 of 2", m)
        self.assertIn("+$0.00* ➖", m)

    def test_missing_tier_with_no_trades_is_unknown(self):
        st = _stats(weekly_by_tier={"day": 156.74, "swing": -49.11, "qhm": -63.92, "forever_6": None})
        self.assertIn("? *F6* unknown", _msg(st))

    def test_sub_cent_loss_is_not_listed(self):
        tr = [{"tier": "day", "symbol": "B", "direction": "long", "pnl": -0.004}]
        st = _stats(total_pnl=0.0, weekly_by_tier={"day": 0.0, "swing": 0.0, "qhm": 0.0, "forever_6": 0.0},
                    manual={"pnl": 0.0, "count": 0, "symbols": []}, partial_open_lifecycles=0)
        self.assertNotIn("−$0.00", _msg(st, tr))

    def test_tier_lines_not_adding_up_are_flagged(self):
        m = _msg(_stats(total_pnl=110.00))
        self.assertIn("Tier lines add up to +$101.98, not the total", m)

    def test_pnl_check_words(self):
        self.assertIn("disagree", _msg(_stats(pnl_check="MISMATCH")))
        self.assertIn("not double-checked", _msg(_stats(pnl_check="SOURCE MISSING")))

    def test_fallback_warning_leads(self):
        m = _msg(note="fills fetch failed")
        self.assertTrue(m.startswith(":warning: *Fills fetch was incomplete"))

    def test_no_equity_drops_the_goal_line(self):
        self.assertNotIn("goal $25,000", _msg(equity=None))

    def test_unknown_trade_pnl_is_skipped_not_zero(self):
        tr = _trades() + [{"tier": "day", "symbol": "ZZZ", "direction": "long", "pnl": None}]
        m = _msg(trades=tr)
        self.assertNotIn("ZZZ", m)
        self.assertIn("won 14 of 19", m)

    def test_month_end_label(self):
        self.assertEqual(wtp._week_label(date(2026, 10, 26), date(2026, 10, 30)), "Oct 26–30")
        self.assertEqual(wtp._week_label(date(2026, 11, 30), date(2026, 12, 4)), "Nov 30–Dec 4")


class PostSlack(unittest.TestCase):
    def test_post_uses_new_layout_and_never_reaches_alpaca(self):
        sent = []
        with mock.patch.object(wtp, "SLACK_WEBHOOK", "test"), \
                mock.patch.object(wtp, "_slack_raw", side_effect=sent.append), \
                mock.patch.object(wtp, "_account_equity", return_value=2731.40), \
                self.assertLogs(wtp.logger, level="INFO") as logs:
            wtp._post_slack("old table rows", _stats(), Path("logs/wtp_2026-11-06.md"), "2026-11-02 → 2026-11-06",
                            trades=_trades(), monday=MON, friday=FRI)
        self.assertEqual(len(sent), 1)
        self.assertIn("📊 *Week of Nov 2–6: +$101.98* ✅", sent[0])
        self.assertNotIn("old table rows", sent[0])
        self.assertTrue(any("change=wtp-slack-1108 Slack layout v2 posted" in ln for ln in logs.output))

    def test_account_equity_failure_returns_none(self):
        with mock.patch("reporting.pnl_ledger.fetch_account", side_effect=RuntimeError("down")):
            self.assertIsNone(wtp._account_equity())
        with mock.patch("reporting.pnl_ledger.fetch_account", return_value={"equity": "2731.4"}):
            self.assertEqual(wtp._account_equity(), 2731.4)


if __name__ == "__main__":
    unittest.main()

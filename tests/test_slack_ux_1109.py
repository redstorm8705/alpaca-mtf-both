# ruff: noqa: E501
"""SLACK-UX-1109 part 1: the phone-first renderer, the scrubber and the send log in alerts.py.

No network: every test patches the Slack/ntfy POST functions; the send log is pointed at a temp file."""
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import alerts

# The 2026-11-09 07:05 PT NVDA burst, as the broker and QHM paths rendered it (broker.py:1358, QHM :3046).
_NVDA_GTC_BODY = ('The bot could not place the SELL protective stop @ $163.07 for NVDA.\n'
                  'Reason: {"code":40310000,"existing_order_id":"0b9a5e50-1c2d-4e5f-8a9b-0c1d2e3f4a5b",'
                  '"message":"potential wash trade detected. use complex orders","reject_reason":"opposite side '
                  'market/stop order exists"}\nUntil this is fixed, NVDA has no stop at the broker.')
_NVDA_QHM = ("QHM NVDA dip-add ambiguous-add-current-qty — PENDING_STOP_REPLACE coid=QH-NVDA-s-1762700000-51afa094 "
             "stop pending resubmit")


class ScrubTests(unittest.TestCase):
    def test_broker_json_removed(self):
        out = alerts._scrub_slack(_NVDA_GTC_BODY)
        self.assertNotIn("{", out)
        self.assertNotIn("40310000", out)
        self.assertNotIn("0b9a5e50", out)
        self.assertIn("(details in log)", out)
        self.assertIn("$163.07", out)
        self.assertIn("NVDA has no stop at the broker.", out)

    def test_internal_names_and_ids_removed(self):
        out = alerts._scrub_slack(_NVDA_QHM)
        for bad in ("ambiguous-add-current-qty", "PENDING_STOP_REPLACE", "QH-NVDA-s-", "51afa094", "coid"):
            self.assertNotIn(bad, out)
        self.assertIn("QHM NVDA dip-add", out)
        self.assertNotIn("  ", out)

    def test_bare_order_id_and_uuid_removed(self):
        out = alerts._scrub_slack("stop IN-QQQM-b-20261110-tnecls1 order 51afa094-1c2d-4e5f-8a9b-0c1d2e3f4a5b ok")
        self.assertEqual(out, "stop order ok")

    def test_exception_repr_removed(self):
        out = alerts._scrub_slack("close failed: APIError('insufficient qty available for order (requested 3)')")
        self.assertEqual(out, "close failed: (error in log)")
        self.assertIn("(error in log)", alerts._scrub_slack("Traceback (most recent call last): x"))

    def test_nested_json_removed(self):
        out = alerts._scrub_slack('reason {"a": {"b": {"c": 1}}} end')
        self.assertNotIn("{", out)
        self.assertTrue(out.startswith("reason ") and out.endswith(" end"))

    def test_tickers_and_public_words_survive(self):
        text = "🚨 ACTION NVDA · BUY 1 SPY 751C GTC stop HALT QHM SOXL BRK.B $1.07 (+2.4%) 10/10 Track B"
        self.assertEqual(alerts._scrub_slack(text), text)

    def test_coid_label_and_value_removed(self):
        self.assertEqual(alerts._scrub_slack("coid=IN-QQQM-b-1762-tnecls1, qty 3"), ", qty 3")

    def test_coid_with_uuid_value(self):
        uid = "4f1c2a3b-1111-2222-3333-444455556666"
        self.assertEqual(alerts._scrub_slack(f"coid={uid} NVDA stop placed at $180"), "NVDA stop placed at $180")
        self.assertEqual(alerts._scrub_slack(f"coid={uid} SOLD 4 NVDA"), "SOLD 4 NVDA")
        self.assertEqual(alerts._scrub_slack(f"NVDA coid: {uid}, qty 3 filled"), "NVDA , qty 3 filled")
        self.assertEqual(alerts._scrub_slack(f"NO action this tick. coid={uid}"), "NO action this tick.")

    def test_cubic_backtracking_inputs_are_fast(self):
        for hostile in ('{' + '":' * 1749, ('{' + '":' * 400) * 5, ('{' + '"' * 400 + ':' * 399) * 5,
                        '{"k": {"j": 1}} ' + ('{' + '":' * 300) * 5):
            t0 = time.monotonic()
            alerts._scrub_slack(hostile[: alerts._SLACK_CHUNK_LIMIT])
            self.assertLess(time.monotonic() - t0, 0.2)

    def test_hostile_input_is_fast_and_safe(self):
        hostile = ('{"' + "a" * 3000) + ("{" * 500) + ("_A" * 500) + ("Error(" * 300)
        t0 = time.monotonic()
        out = alerts._scrub_slack(hostile[: alerts._SLACK_CHUNK_LIMIT])
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIsInstance(out, str)

    def test_non_string_never_raises(self):
        self.assertEqual(alerts._scrub_slack(None), "None")
        self.assertEqual(alerts._scrub_slack(12.5), "12.5")

    def test_blocks_every_text_field_scrubbed(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "PENDING_STOP_REPLACE on NVDA"}},
                  {"type": "context", "elements": [{"type": "mrkdwn", "text": 'r={"code":1,"x":"y"}'}]}]
        out = alerts._scrub_blocks(blocks)
        self.assertEqual(out[0]["text"]["text"], "on NVDA")
        self.assertNotIn("{", out[1]["elements"][0]["text"])
        self.assertEqual(blocks[0]["text"]["text"], "PENDING_STOP_REPLACE on NVDA")  # input untouched


class RenderTests(unittest.TestCase):
    def test_action_with_you(self):
        out = alerts.render_notify(alerts.SEVERITY_ACTION, "nvda", "1 share has no stop",
                                   risk="1 share, $185 (no stop)", bot_is="retrying the stop each cycle",
                                   you="place a sell stop for 1 share at $163.07", tier="QHM")
        self.assertEqual(out.split("\n"), ["🚨 ACTION NVDA · 1 share has no stop",
                                           "Money at risk: 1 share, $185 (no stop)",
                                           "Bot is: retrying the stop each cycle",
                                           "You: place a sell stop for 1 share at $163.07",
                                           "QHM"])

    def test_action_on_line_one_needs_no_you(self):
        out = alerts.render_notify(alerts.SEVERITY_ACTION, "SPY", "buy 1 751C, limit $1.07", verdict_is_action=True)
        self.assertEqual(out, "🚨 ACTION SPY · buy 1 751C, limit $1.07")

    def test_action_never_renders_without_an_action(self):
        with self.assertLogs("alerts", level="ERROR"):
            out = alerts.render_notify(alerts.SEVERITY_ACTION, "NVDA", "1 share has no stop")
        self.assertIn("You: check the bot log.", out)

    def test_info_and_heads_up_omit_nothing(self):
        out = alerts.render_notify(alerts.SEVERITY_INFO, "AMD", "bought 2 @ $535.60", you="Nothing.", tier="Day")
        self.assertEqual(out, "AMD · bought 2 @ $535.60\nDay")
        out = alerts.render_notify(alerts.SEVERITY_HEADS_UP, "NVDA", "stop being re-placed", bot_is="retrying")
        self.assertEqual(out, "⚠️ NVDA · stop being re-placed\nBot is: retrying")

    def test_newlines_collapsed_and_no_bold(self):
        out = alerts.render_notify(alerts.SEVERITY_INFO, "AMD\n", "line\nbreak")
        self.assertEqual(out, "AMD · line break")
        self.assertNotIn("*", out.split("\n")[0])

    def test_unknown_severity_is_info(self):
        self.assertEqual(alerts.render_notify("weird", "", "report ready"), "report ready")


class SendLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "slack_sent.jsonl"
        self.patches = [mock.patch.object(alerts, "_SENT_LOG", self.log),
                        mock.patch.object(alerts, "_SLACK_WEBHOOK", "https://example.invalid/hook"),
                        mock.patch.object(alerts, "_NTFY_TOPIC", ""),
                        mock.patch.object(alerts.time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()
        self.sent: list = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _rows(self):
        return [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_notify_delivered_row_and_scrubbed_post(self):
        with mock.patch.object(alerts, "_post_slack_text", lambda t: self.sent.append(t) or True):
            ok = alerts.notify(alerts.SEVERITY_ACTION, "NVDA", "1 share has no stop",
                               you="place a sell stop for 1 share at $163.07", tier="QHM",
                               kind="stop_missing", risk='raw {"code":40310000,"x":"y"}')
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("40310000", self.sent[0])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertTrue(r["delivered"])
        self.assertEqual((r["kind"], r["severity"], r["symbol"], r["parts"]), ("stop_missing", "action", "NVDA", 1))
        self.assertEqual(r["first_line"], "🚨 ACTION NVDA · 1 share has no stop")
        self.assertTrue(r["incident"].startswith("NVDA-stop_missing-"))

    def test_failed_send_logs_delivered_false(self):
        with mock.patch.object(alerts, "_post_slack_text", lambda t: False):
            ok = alerts.notify(alerts.SEVERITY_HEADS_UP, "NVDA", "stop being re-placed", kind="stop_retry")
        self.assertFalse(ok)
        self.assertEqual([r["delivered"] for r in self._rows()], [False])

    def test_split_message_is_one_row(self):
        long = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
        with mock.patch.object(alerts, "_post_slack_text", lambda t: self.sent.append(t) or True):
            self.assertTrue(alerts._send_slack_chunked(long))
        self.assertGreater(len(self.sent), 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parts"], len(self.sent))
        self.assertEqual(rows[0]["kind"], "legacy")

    def test_legacy_send_slack_is_scrubbed_and_logged(self):
        with mock.patch.object(alerts, "_post_slack_text", lambda t: self.sent.append(t) or True):
            alerts.send_slack(_NVDA_QHM)
        self.assertNotIn("PENDING_STOP_REPLACE", self.sent[0])
        self.assertEqual(len(self._rows()), 1)

    def test_blocks_send_logs_one_row(self):
        with mock.patch.object(alerts, "_post_slack_payload", lambda p: self.sent.append(p) or True):
            ok = alerts.send_slack_blocks([{"type": "section", "text": {"type": "mrkdwn", "text": "PENDING_X ok"}}],
                                          "NVDA report")
        self.assertTrue(ok)
        self.assertEqual(self.sent[0]["blocks"][0]["text"]["text"], "ok")
        self.assertEqual([r["kind"] for r in self._rows()], ["blocks"])

    def test_lock_contention_drops_row_without_waiting(self):
        import fcntl
        self.log.touch()
        fd = os.open(str(self.log), os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            t0 = time.monotonic()
            with self.assertLogs("alerts", level="WARNING"):
                alerts._log_sent("x", True)
            self.assertLess(time.monotonic() - t0, 0.5)
        finally:
            os.close(fd)
        self.assertEqual(self._rows(), [])

    def test_two_processes_append_whole_lines(self):
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_append_many, args=(str(self.log), tag)) for tag in ("a", "b")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        rows = self._rows()   # every line parses: no torn or interleaved rows
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), 400)

    def test_default_path_not_written_in_tests(self):
        with mock.patch.object(alerts, "_SENT_LOG", alerts._SENT_LOG_DEFAULT), \
             mock.patch.object(alerts.os, "open", side_effect=AssertionError("wrote the real log")):
            alerts._log_sent("x", True)


def _append_many(path: str, tag: str) -> None:
    alerts._SENT_LOG = Path(path)
    for i in range(200):
        alerts._log_sent(f"{tag} {i} " + "y" * 150, True)


if __name__ == "__main__":
    unittest.main()

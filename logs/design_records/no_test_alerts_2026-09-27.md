# Test runs can never page the operator (2026-09-27)

**Incident.**
- On 2026-09-26 at 17:58 PT (Saturday, market closed), a test run sent five fake "[UBER] day-tier fill INVALIDATED the setup" pages to Rafael's Slack.
- **Path:** `execution/day_trade_manager._page` → `alerts.send_slack`, driven unmocked by `tests/test_day_tier_track_b_live.py` and `tests/test_day_tier_bracket_exit.py`, with the webhook configured.
- **Reproduced on the production box:** 12 send attempts on main.
- The standing rule "tests must never post to real Slack" had been violated before, so a mechanism was required (DOCUMENTATION IS NOT ENFORCEMENT).

**Design (two layers):**
1. **`alerts.py` sender layer.**
   - `_under_test()` is True when:
     - MTF_TEST_MODE=1, OR
     - pytest is imported, OR
     - the basename of argv[0] contains unittest/pytest or starts with `test_`.
   - The check sits at the only three network chokepoints: `_ntfy`, `_post_slack_text`, `_post_slack_payload`.
   - Suppressed sends return False and log at INFO.
   - No production process matches: `main.py`, `live_data_writer.py`, `http.server`, cron scripts. Tests assert this.
2. **Test-package layer.** `tests/__init__.py` and `tests/conftest.py` set MTF_TEST_MODE=1 and blank SLACK_WEBHOOK_URL / SLACK_WEBHOOK / NTFY_TOPIC. This covers scripts that post to the webhook directly: reconcile_eod, weekly_postmortem, auto_ai_audit.
   - **Residual:** under `discover -s tests` the package init does not run. `alerts.py` still suppresses (argv check); the tests that touch direct-post scripts mock their senders.

**Verification:**
- **Production box:** 0 Slack send attempts from those tests, under `unittest tests.x` and under `discover -s tests`.
- **Full suite:** 593 tests with 0 attempts. The same 34 pre-existing failures as main (identical set).

**Reviews:**
- Cold second-agent: round 1 FAIL (a self-test assertion broke under `discover -s tests`; fixed), round 2 PASS.
- Final Gro+GAI preship: APPROVE.

**Approval:** Rafael asked for the fix ("Fix this spam alert") and approved the package ("Ok let's continue").

**Follow-up (separate item):** 34 tests fail on main under `discover`, including day-tier OCO geometry tests after commit 2b8e3e5.

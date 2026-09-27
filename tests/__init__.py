"""Test-run guard: no test may page the operator.

Imported before any test module (as the `tests` package init under
`python -m unittest tests.x`, and as conftest under pytest). Marks the process as a
test run and blanks the alert transports, so code under test that calls Slack/ntfy
senders (alerts.py and the scripts that post to the webhook directly) cannot reach
the real channel. alerts.py also refuses to send whenever MTF_TEST_MODE=1, and
independently whenever the process is a test runner (so it still holds under
`python -m unittest discover -s tests`, where this package init is not executed).
"""

import os

os.environ["MTF_TEST_MODE"] = "1"
for _k in ("SLACK_WEBHOOK_URL", "SLACK_WEBHOOK", "NTFY_TOPIC"):
    os.environ[_k] = ""

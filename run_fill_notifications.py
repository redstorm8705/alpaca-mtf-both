#!/usr/bin/env python3
"""Independent one-shot broker-fill Slack notifier.

OCI cron runs this once per minute. It is deliberately outside every trading
loop: Alpaca activity history, order attribution, filesystem locks, and Slack
latency cannot delay entries, exits, stops, or reconciliation.
"""

from __future__ import annotations

import logging
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from execution.fill_notifications import poll_fill_notifications_safe


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    poll_fill_notifications_safe()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
cron_tz_wrapper.py
DST-aware cron wrapper. Each cron entry fires at both the EDT and EST UTC
equivalents of its target ET time. This wrapper checks the current ET clock
at runtime and only executes the command when it matches the target window.
The non-matching UTC firing exits silently — no log entry, no side effects.

Usage:
    python3 cron_tz_wrapper.py HH:MM COMMAND [ARGS...]
    HH:MM is the target time in ET (24-hour format).

Crontab pattern (5:30 AM ET example):
    30 9,10 * * 1-5 cd /home/ubuntu/mtf-bot && WRAPPER 05:30 VENV/python3 script.py
    # 9:30 UTC = 5:30 AM EDT  |  10:30 UTC = 5:30 AM EST
"""
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo

_WINDOW_MINUTES = 3


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(1)

    target_str = sys.argv[1]
    command    = sys.argv[2:]

    try:
        target_h, target_m = map(int, target_str.split(":"))
    except ValueError:
        sys.exit(1)

    now_et     = datetime.now(ZoneInfo("America/New_York"))
    now_mins   = now_et.hour * 60 + now_et.minute
    target_mins = target_h * 60 + target_m

    if abs(now_mins - target_mins) > _WINDOW_MINUTES:
        sys.exit(0)

    os.execvp(command[0], command)


if __name__ == "__main__":
    main()

"""SPY daily closes (SIP, split-adjusted) 2020-09 .. now -> spy_daily.json (date -> [open, close]). Read-only, T1."""
import json, os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config  # noqa: E402
from data.fetcher import fetch_bars_window  # noqa: E402
d = fetch_bars_window("SPY", config.TF_DAILY, datetime(2020, 9, 1, tzinfo=timezone.utc),
                      datetime.now(timezone.utc) - timedelta(minutes=20), feed="sip", adjustment="split")
out = {str(ix.tz_convert(ZoneInfo("America/New_York")).date()): [float(o), float(c)] for ix, o, c in zip(d.index, d["open"], d["close"])}
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "spy_daily.json"), "w"))
print(len(out))

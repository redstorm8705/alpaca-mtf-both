"""Read-only probe: TRACK-A-STOP-1026 noise room and range target on real Alpaca bars for the Day-tier universe at a
few times of day. No order, no broker call. Run from the scratch copy with the bot's .env loaded."""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
import config  # noqa: E402
from execution import day_trade_manager as dtm  # noqa: E402

ET = ZoneInfo("America/New_York")
syms = list(dict.fromkeys(list(getattr(config, "DAYTRADE_DEEP_LIQUIDITY_SYMBOLS", [])) + ["MU", "SNDK", "DRAM", "EWY"]))
for hh, mm in ((9, 40), (11, 0), (13, 15), (15, 0)):
    now = datetime(2026, 10, 26, hh, mm, tzinfo=ET)
    for s in syms:
        g = dtm._track_a_noise_geometry(s, now)
        if g is None:
            print(f"{hh:02d}:{mm:02d} {s:5s} None (today's stop and target)")
            continue
        print(f"{hh:02d}:{mm:02d} {s:5s} atr5 {g['atr5']:.3f} ({g['atr5_sessions']} sess) room {g['room']:.2f} "
              f"range {g['range_median']:.2f} target {g['target_dist']:.2f} room/target {g['room'] / g['target_dist']:.2f}")

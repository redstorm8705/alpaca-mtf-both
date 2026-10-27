"""Extract every Day-tier entry (track, side, fill, stop, target) from the Day tier's own log. Read-only."""
import json
import re

LOG = "logs/day_tier_runner_cron.log"
size_re = re.compile(r"^(\S+ \S+) .*\[(\w+)\] day-tier SIZE: track ([AB])")
prot_re = re.compile(r"^(\S+ \S+) .*\[(\w+)\] day-tier PROTECTED(?: \(OCO\))?: (\d+) sh @ fill ([\d.]+), stop ([\d.]+)(?:, target ([\d.]+))?")
tgt_re = re.compile(r"^(\S+ \S+) .*\[(\w+)\] .*(?:target|TP) \$?([\d.]+)")
last_track: dict = {}
trades = []
with open(LOG, encoding="utf-8", errors="replace") as f:
    for line in f:
        m = size_re.match(line)
        if m:
            last_track[m.group(2)] = m.group(3)
            continue
        m = prot_re.match(line)
        if m:
            ts, sym, qty, fill, stop, tgt = m.groups()
            fill, stop = float(fill), float(stop)
            trades.append({"ts": ts, "symbol": sym, "track": last_track.get(sym, "?"), "qty": int(qty),
                           "fill": fill, "stop": stop, "target": float(tgt) if tgt else None,
                           "side": "long" if stop < fill else "short"})
# de-duplicate re-protects of the same lot (same symbol/day/fill)
seen = set()
out = []
for t in trades:
    k = (t["symbol"], t["ts"][:10], t["fill"])
    if k in seen:
        continue
    seen.add(k)
    out.append(t)
print(json.dumps(out))

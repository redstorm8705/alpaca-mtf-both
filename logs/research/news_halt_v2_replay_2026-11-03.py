"""Read-only replay: every logged news headline through the v1 (substring) and v2 matchers; the
HALT-blocked regular-hours cycles (halt_eval keyword_hit) and their status under v2."""
import ast, collections, json, re, sys
from datetime import datetime, timedelta
from unittest import mock
sys.path.insert(0, "/tmp/nv1103")
import events.news_monitor as nm
with mock.patch.object(nm.NewsMonitor, "_load_seen_hashes", return_value={}), \
        mock.patch.object(nm.NewsMonitor, "_restore_macro_risk_window", return_value=None):
    M = nm.NewsMonitor()
LOG = "/home/ubuntu/mtf-bot/logs/mtf_bot.log"
pw = re.compile(r"^(\S+ \S+),\d+ \| \w+\s+\| news_monitor \|\s+\[([^\]]+)\] (HALT|CAUTION) \| (.*?) \| keywords: (\[.*?\]) \|")
pm = re.compile(r"^(\S+ \S+),\d+ \| \w+\s+\| news_monitor \| \[([^\]]+)\] MONITOR: (.*?) \| keywords: (\[.*?\])")
seen = {}
for line in open(LOG, errors="replace"):
    if "news_monitor" not in line or "keywords:" not in line:
        continue
    m = pw.match(line)
    if m:
        ts, src, lvl, hl, kws = m.groups()
    else:
        m = pm.match(line)
        if not m:
            continue
        ts, src, hl, kws = m.groups(); lvl = "MONITOR"
    seen.setdefault((src, hl), (ts, lvl, ast.literal_eval(kws)))
def v1(t):
    with mock.patch.object(nm, "NEWS_MATCHER_V2_ENABLED", False):
        return M._classify(t)
def v2(t, src):
    fed = src == "FederalReserve"
    title = t if not fed else t
    return M._classify(("federal reserve " + t) if fed else t, title=title, fed=fed)
first = min(v[0] for v in seen.values()); last = max(v[0] for v in seen.values())
print(f"unique logged headlines {len(seen)}  {first} .. {last}  logged levels {dict(collections.Counter(v[1] for v in seen.values()))}")
trans = collections.Counter(); halt_rows = []
for (src, hl), (ts, lvl, kws) in sorted(seen.items(), key=lambda x: x[1][0]):
    o = v1(hl)[0]; n, nk, _ = v2(hl, src)
    trans[(lvl, n)] += 1
    if lvl == "HALT" or n == "HALT" or o == "HALT":
        halt_rows.append(dict(ts=ts, src=src, logged=lvl, logged_kws=kws, v1_on_title=o, v2=n, v2_kws=nk, hl=hl[:110]))
print("logged level -> v2 level:", {f"{a}->{b}": c for (a, b), c in sorted(trans.items(), key=str)})
print("\n=== every headline that was or would be HALT ===")
for r in halt_rows:
    print(json.dumps(r))
# blocked cycles: halt_eval keyword_hit True
ev = []
for line in open("/home/ubuntu/mtf-bot/logs/trade_events.jsonl", errors="replace"):
    if '"halt_eval"' not in line or '"keyword_hit": true' not in line:
        continue
    try:
        ev.append(json.loads(line)["ts"])
    except Exception:
        pass
print(f"\nkeyword-blocked cycles in trade_events: {len(ev)}")
halts = [(datetime.fromisoformat(r["ts"].replace(" ", "T")), r) for r in halt_rows if r["logged"] == "HALT"]
for t in ev:
    tt = datetime.fromisoformat(t).replace(tzinfo=None)
    # log timestamps are PT wall time; trade_events ts carry their own offset — compare in PT wall time
    tp = datetime.fromisoformat(t).astimezone(__import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None)   # mtf_bot.log stamps UTC
    cause = [r for h, r in halts if timedelta(0) <= tp - h <= timedelta(minutes=30)]
    still = any(r["v2"] == "HALT" for r in cause)
    print(datetime.fromisoformat(t).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M PT"), "| v2 still blocks:", still, "| cause:", "; ".join(f"{r['src']}: {r['hl'][:70]} {r['logged_kws']}" for r in cause) or "UNMATCHED")

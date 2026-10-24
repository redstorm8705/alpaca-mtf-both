"""READ-ONLY replay of two Day-tier fixes on every closed Day trade (2026-09-15 .. 2026-10-23). Writes /tmp/dt_fix_replay.json.

FIX 1 (TRACK-A-EDGE): a Track A entry is sized at the Track B probe budget (equity x DAYTIER_ALLOC x TRACK_B_SHARE x
conviction, min 1 share, never above today's qty) while Track A's own walk-forward record (mean R of its trades that
closed before this week's Monday) is <= 0. No prior Track A record -> today's size. Trade count unchanged.
FIX 2 (STOP-BEYOND-LEVEL): the protective stop is never between the entry and the level that defines the trade
(wall_ref): stop = max-protective of (placed stop, level +/- DAYTRADE_STOP_BUFFER_PCT). Same dollar risk -> fewer
shares (min 1). FADE target = the pin (unchanged); other modes target = DAYTRADE_RIDE_TARGET_R x the new distance.

Fidelity check: the simulator re-runs every trade with its ACTUAL stop/target/qty and is compared to the real P&L."""
import json
import math
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, '/home/ubuntu/mtf-bot')
import config  # noqa: E402
from strategy import day_tier_sizing as dts  # noqa: E402

ET = ZoneInfo('America/New_York')
env = {}
for ln in open('/home/ubuntu/mtf-bot/.env'):
    if '=' in ln and not ln.strip().startswith('#'):
        k, v = ln.strip().split('=', 1)
        env[k] = v.strip().strip('"').strip("'")
H = {'APCA-API-KEY-ID': env['ALPACA_API_KEY'], 'APCA-API-SECRET-KEY': env['ALPACA_SECRET_KEY']}
BUF = float(config.DAYTRADE_STOP_BUFFER_PCT)
RT = float(config.DAYTRADE_RIDE_TARGET_R)
ALLOC, BSH = float(dts._DAYTIER_ALLOC_PCT), float(dts._TRACK_B_SHARE)

ev = [json.loads(l) for l in open('/home/ubuntu/mtf-bot/logs/day_tier_events.jsonl') if l.strip()]
dec = {e.get('decision_id'): e for e in ev if e.get('event') == 'decision'}
by = {}
for e in ev:
    if e.get('trade_id'):
        by.setdefault(e['trade_id'], []).append(e)


def bars_1m(sym, t0, day):
    end = datetime.fromisoformat(day + 'T16:00:00').replace(tzinfo=ET)
    u = (f"https://data.alpaca.markets/v2/stocks/{sym}/bars?timeframe=1Min&start={t0.isoformat().replace('+', '%2B')}"
         f"&end={end.isoformat().replace('+', '%2B')}&feed=iex&limit=10000")
    r = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=30).read())
    return [(datetime.fromisoformat(b['t'].replace('Z', '+00:00')).astimezone(ET), b) for b in (r.get('bars') or [])]


def sim(side, entry, stop, target, qty, bars, t_entry):
    """Exit at stop (gap -> open), target, or the 15:58 ET close. Stop checked before target in the same bar (worst case)."""
    s = 1 if side == 'long' else -1
    last = None
    for ts, b in bars:
        if ts < t_entry.replace(second=0, microsecond=0):
            continue
        first = ts == t_entry.replace(second=0, microsecond=0)
        o, h, lo, c = b['o'], b['h'], b['l'], b['c']
        if (s > 0 and lo <= stop) or (s < 0 and h >= stop):
            px = stop if first else (min(o, stop) if s > 0 else max(o, stop))
            return s * (px - entry) * qty, 'stop', ts
        if target is not None and ((s > 0 and h >= target) or (s < 0 and lo <= target)):
            return s * (target - entry) * qty, 'target', ts
        last = (ts, c)
        if (ts.hour, ts.minute) >= (15, 58):
            break
    return (s * (last[1] - entry) * qty, 'eod', last[0]) if last else (0.0, 'none', None)


rows = []
for tid, es in by.items():
    ent = next((x for x in es if x['event'] == 'entry_fill'), None)
    exits = [x for x in es if x['event'] in ('exit_fill', 'partial_exit_fill')]
    stops = [x for x in es if x['event'] == 'stop_placed']
    tps = [x for x in es if x['event'] == 'target_placed']
    if not (ent and exits and stops):
        continue
    if any(x.get('exit_reason') == 'transferred_to_swing_tier' for x in exits):
        continue   # handed to Swing: no comparable Day exit
    d = dec.get(ent.get('decision_id')) or {}
    trg, sz = d.get('trigger') or {}, d.get('size') or {}
    side, sym, day = ent['side'], ent['symbol'], ent['ts'][:10]
    t_entry = datetime.fromisoformat(ent['ts']).astimezone(ET)
    entry, qty = float(ent['fill_price']), int(float(ent['fill_qty']))
    stop = float(stops[0]['stop_price'])
    tgt = float(tps[0]['target_price']) if tps else None
    track = ent.get('track') or 'A'
    try:
        bars = bars_1m(sym, t_entry, day)
    except Exception as e:  # noqa: BLE001
        print('bars fail', sym, day, e)
        continue
    pnl_actual = round(sum(float(x['realized_pnl']) for x in exits), 2)
    sim_actual = sim(side, entry, stop, tgt, qty, bars, t_entry)
    lvl = trg.get('wall_ref')
    mode = trg.get('mode')
    new_stop, inside = stop, False
    if lvl is not None:
        beyond = round(lvl * (1 + BUF), 2) if side == 'short' else round(lvl * (1 - BUF), 2)
        inside = (stop < beyond) if side == 'short' else (stop > beyond)
        if inside and ((side == 'short' and beyond > entry) or (side == 'long' and beyond < entry)):
            new_stop = beyond
        else:
            inside = False if not inside else inside
    if new_stop != stop:
        new_qty = max(1, math.floor(qty * abs(entry - stop) / abs(entry - new_stop)))
        if mode == 'FADE' and trg.get('target') is not None:
            new_tgt = tgt
        else:
            new_tgt = round(entry + RT * abs(entry - new_stop), 2) if side == 'long' else round(entry - RT * abs(entry - new_stop), 2)
        sim_fix2 = sim(side, entry, new_stop, new_tgt, new_qty, bars, t_entry)
    else:
        new_qty, new_tgt, sim_fix2 = qty, tgt, sim_actual
    rows.append(dict(tid=tid, day=day, sym=sym, side=side, track=track, mode=mode, conviction=sz.get('conviction'),
                     equity=ent.get('equity_at_entry'), entry=entry, qty=qty, stop=stop, target=tgt, level=lvl,
                     inside=inside, new_stop=new_stop, new_qty=new_qty, new_target=new_tgt,
                     pnl_actual=pnl_actual, sim_actual=round(sim_actual[0], 2), sim_actual_how=sim_actual[1],
                     sim_fix2=round(sim_fix2[0], 2), sim_fix2_how=sim_fix2[1],
                     risk_actual=round(abs(entry - stop) * qty, 2), risk_fix2=round(abs(entry - new_stop) * new_qty, 2),
                     exit_ts=max(x['ts'] for x in exits)))
json.dump(rows, open('/tmp/dt_fix_replay.json', 'w'), default=str)
print('rows', len(rows), 'ALLOC', ALLOC, 'BSH', BSH, 'BUF', BUF, 'RT', RT)

import json, math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ET=ZoneInfo('America/New_York')
rows=json.load(open('dt_fix_replay.json'))
rows.sort(key=lambda r:r['exit_ts'])
ALLOC,BSH=0.15,0.35
# fidelity
dev=[abs(r['sim_actual']-r['pnl_actual']) for r in rows]
match=sum(d<=1.0 for d in dev)
print(f"FIDELITY: sim vs real within $1 on {match}/{len(rows)}; sum sim {sum(r['sim_actual'] for r in rows):+.2f} vs real {sum(r['pnl_actual'] for r in rows):+.2f}")
for r in rows:
    if abs(r['sim_actual']-r['pnl_actual'])>1: print('   miss',r['day'],r['sym'],r['pnl_actual'],r['sim_actual'],r['sim_actual_how'])
# FIX 2
ins=[r for r in rows if r['new_stop']!=r['stop']]
print(f"\nFIX2 trades changed: {len(ins)}")
for r in ins:
    print(f"  {r['day']} {r['sym']:5} {r['side']:5} {r['mode']:8} stop {r['stop']}->{r['new_stop']} qty {r['qty']}->{r['new_qty']} risk ${r['risk_actual']}->${r['risk_fix2']} | sim old {r['sim_actual']:+.2f} ({r['sim_actual_how']}) new {r['sim_fix2']:+.2f} ({r['sim_fix2_how']})")
print(f"  changed trades: sim old {sum(r['sim_actual'] for r in ins):+.2f} -> new {sum(r['sim_fix2'] for r in ins):+.2f}; stop-outs {sum(r['sim_actual_how']=='stop' for r in ins)} -> {sum(r['sim_fix2_how']=='stop' for r in ins)}; risk ${sum(r['risk_actual'] for r in ins):.2f} -> ${sum(r['risk_fix2'] for r in ins):.2f}")
# FIX 1 walk-forward
def wk(ts):
    t=datetime.fromisoformat(ts).astimezone(ET); d=t.date()-timedelta(days=t.weekday()); return datetime(d.year,d.month,d.day,tzinfo=ET)
def R(r): 
    dist=abs(r['entry']-r['stop']); return (r['pnl_actual']/r['qty'])/dist if dist>0 else 0
res=[]
for r in sorted(rows,key=lambda r:r['exit_ts']):
    if r['track']!='A': continue
    cut=wk(r['exit_ts'])  # approx entry week = exit week (day trades)
    prior=[R(x) for x in rows if x['track']=='A' and datetime.fromisoformat(x['exit_ts']).astimezone(ET)<cut]
    probe = len(prior)>=2 and sum(prior)/len(prior)<=0
    eq=r['equity'] or 2700.0; conv=r['conviction'] or 0.6
    pq=min(r['qty'],max(1,math.floor(eq*ALLOC*BSH*conv/r['entry']))) if probe else r['qty']
    res.append((r, probe, pq, len(prior), (sum(prior)/len(prior) if prior else None)))
oldA=sum(r['pnl_actual'] for r,*_ in res); newA=sum(r['pnl_actual']*pq/r['qty'] for r,p,pq,*_ in res)
print(f"\nFIX1 Track A trades {len(res)}; probe-sized {sum(p for _,p,*_ in res)}; P&L {oldA:+.2f} -> {newA:+.2f}; trade count unchanged")
for r,p,pq,n,m in res:
    print(f"  {r['day']} {r['sym']:5} {r['mode']:6} qty {r['qty']:>3}->{pq:<3} {'PROBE' if p else 'today'} priorA n={n} meanR={'' if m is None else round(m,2)} pnl {r['pnl_actual']:+.2f}->{r['pnl_actual']*pq/r['qty']:+.2f}")
# combined (fix2 then fix1 qty scaling on Track A)
tot_old=sum(r['pnl_actual'] for r in rows)
comb=0
for r in rows:
    base=r['sim_fix2'] - r['sim_actual'] + r['pnl_actual']  # apply fix2 delta to real P&L
    scale=1.0
    for rr,p,pq,*_ in res:
        if rr['tid']==r['tid'] and p: scale=min(1.0, pq/ r['new_qty']) if r['new_qty'] else 1.0
    comb+=base*scale
print(f"\nCOMBINED all Day trades: {tot_old:+.2f} -> {comb:+.2f} ({len(rows)} trades, count unchanged)")

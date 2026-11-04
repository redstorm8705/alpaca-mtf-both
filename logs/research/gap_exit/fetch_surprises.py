"""FMP /stable/earnings history (eps/revenue actual vs estimate) for the panel names -> surprises.json. Read-only, T2."""
import json, os, sys
import requests
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config  # noqa: E402
NAMES = list(config.DAY_TIER_UNIVERSE) + ["NOW", "GE", "XOM", "JPM", "LLY", "CRWD", "UNH", "ORCL"]
out = {}
for s in NAMES:
    r = requests.get("https://financialmodelingprep.com/stable/earnings", params={"symbol": s, "apikey": os.environ["FMP_API_KEY"]}, timeout=20)
    out[s] = r.json() if r.status_code == 200 else {"status": r.status_code}
    print(s, r.status_code, len(out[s]) if isinstance(out[s], list) else "", flush=True)
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "surprises.json"), "w"))

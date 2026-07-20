#!/usr/bin/env python3
"""Synthetic-chain probe: is the GEX 'flip' a market measurement or an artifact of spot?

THE EXPERIMENT
--------------
Hold the option chain COMPLETELY FIXED (same strikes, same call/put open interest,
same implied vol) and sweep the underlying spot price. Dealer positioning is, by
construction, IDENTICAL at every step -- only the underlying price moves.

    If the emitted flip TRACKS SPOT   -> it is an artifact of the algorithm. A level
                                         that moves when positioning did not move is
                                         not measuring positioning.
    If the emitted flip STAYS ANCHORED-> it is measuring the chain, which is what a
                                         gamma flip is supposed to do.

This is sample-size independent: it is a controlled experiment, not an observational
regression, so it cannot be defeated by restricted range (which is what made the
observational flip-on-spot regression inconclusive at n=24 on 2026-07-19).

The same sweep also tests the CANDIDATE replacement measure that Gro and GAI split on
-- the gamma-weighted centroid  sum(K*|GEX_K|)/sum(|GEX_K|).  Gro asserted it is
spot-independent; GAI argued it cannot be, since every gamma is evaluated at spot.
This settles it with numbers instead of opinion, BEFORE anything is built on it.

A KEY FACT THAT SHAPES THE CHAINS
---------------------------------
Black-Scholes gamma is IDENTICAL for a call and a put at the same strike/expiry/vol.
Since _compute_gex uses  sign * gamma * oi  with sign=+1 for calls and -1 for puts,
calls and puts at a strike CANCEL EXACTLY when their OI is equal. Net GEX is therefore
driven entirely by the call-vs-put OI IMBALANCE per strike -- never by gamma alone.
Consequences: a "symmetric OI" chain nets to zero everywhere, and a uniformly put-heavy
chain never changes sign. Neither produces a crossing. So the chains below place the
imbalance CROSSOVER at a KNOWN strike, deliberately away from spot.

Read-only. Imports data/gex.py for inspection only (no execution path, no orders).
Writes one JSON to logs/.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import data.gex as gexmod  # noqa: E402  (read-only inspection of the real implementation)

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

SIGMA = 0.20          # flat implied vol across the synthetic surface
STRIKE_LO = 600.0     # wide grid so the +/-5% window always has strikes as spot moves
STRIKE_HI = 900.0
STRIKE_STEP = 2.50
BASE_OI = 5000.0


def _expiry_dt() -> datetime:
    """Coming Friday 16:00 ET (>= 2 days out so T is comfortably above the 1h floor)."""
    now = datetime.now(ET)
    d = now + timedelta(days=(4 - now.weekday()) % 7)
    if (d.date() - now.date()).days < 2:
        d = d + timedelta(days=7)
    return d.replace(hour=16, minute=0, second=0, microsecond=0)


def _occ(underlying: str, expiry: datetime, is_call: bool, strike: float) -> str:
    return (f"{underlying}{expiry:%y%m%d}{'C' if is_call else 'P'}"
            f"{int(round(strike * 1000)):08d}")


def strikes() -> list[float]:
    out, k = [], STRIKE_LO
    while k <= STRIKE_HI + 1e-9:
        out.append(round(k, 2))
        k += STRIKE_STEP
    return out


def build_chain(scenario: str, crossover: float | None = None):
    """Return (oi_map, meta). OI structure is FIXED -- it never depends on spot."""
    exp = _expiry_dt()
    oi_map: dict[str, float] = {}
    for k in strikes():
        if scenario == "calls_only":
            call_oi, put_oi = BASE_OI, 0.0
        elif scenario == "symmetric":
            call_oi, put_oi = BASE_OI, BASE_OI
        elif scenario == "crossover":
            # Puts dominate BELOW the crossover, calls dominate ABOVE it.
            # The imbalance sign flips at a strike we choose -- not at spot.
            if k < crossover:
                call_oi, put_oi = BASE_OI * 0.25, BASE_OI
            else:
                call_oi, put_oi = BASE_OI, BASE_OI * 0.25
        else:
            raise ValueError(scenario)
        if call_oi > 0:
            oi_map[_occ("SPY", exp, True, k)] = call_oi
        if put_oi > 0:
            oi_map[_occ("SPY", exp, False, k)] = put_oi
    return oi_map, {"scenario": scenario, "crossover": crossover,
                    "expiry": exp.isoformat(), "n_contracts": len(oi_map)}


def build_snapshots(oi_map: dict, spot: float) -> dict:
    """Quotes priced by BS at THIS spot with a flat SIGMA.

    This is the correct counterfactual: the same positioning observed with the
    underlying at a different price. Quotes are tight (+/-1%) so they clear the
    _MAX_SPREAD_RATIO hygiene gate and _implied_vol recovers SIGMA.
    """
    now_et = datetime.now(ET)
    snaps: dict = {}
    for sym in oi_map:
        parsed = gexmod._parse_occ(sym)
        if parsed is None:
            continue
        expiry, is_call, strike = parsed
        T = (expiry - now_et).total_seconds() / (365.0 * 24 * 3600)
        if T < gexmod._MIN_T_YEARS:
            continue
        px = gexmod._bs_price(spot, strike, T, SIGMA, is_call)
        if px <= 0.05:          # unquotably cheap; a real feed would show zero bid
            continue
        snaps[sym] = {"latestQuote": {"bp": round(px * 0.99, 2),
                                      "ap": round(px * 1.01, 2)}}
    return snaps


def centroid(snapshots: dict, oi_map: dict, spot: float):
    """Candidate replacement measure: gamma-weighted centroid sum(K|G|)/sum(|G|).

    Recomputed with the SAME inputs and the SAME gamma model _compute_gex uses, so
    the comparison is apples to apples.
    """
    now_et = datetime.now(ET)
    num = den = 0.0
    for sym, snap in snapshots.items():
        oi = oi_map.get(sym)
        if not oi:
            continue
        q = snap.get("latestQuote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        if bid is None or ask is None or bid <= 0 or ask < bid:
            continue
        mid = 0.5 * (float(bid) + float(ask))
        parsed = gexmod._parse_occ(sym)
        if parsed is None:
            continue
        expiry, is_call, strike = parsed
        T = (expiry - now_et).total_seconds() / (365.0 * 24 * 3600)
        if T < gexmod._MIN_T_YEARS:
            continue
        iv = gexmod._implied_vol(mid, spot, strike, T, is_call)
        if iv is None:
            continue
        g = gexmod._bs_gamma(spot, strike, T, iv)
        sign = 1.0 if is_call else -1.0
        w = abs(sign * g * oi * 100.0 * spot ** 2)
        num += strike * w
        den += w
    return (num / den) if den > 0 else None


def oi_centroid(oi_map: dict):
    """CONTROL measure: open-interest-weighted centroid, sum(K*OI)/sum(OI).

    Deliberately uses NO gamma, so nothing in it is evaluated at spot. If the
    gamma-weighted centroid tracks spot while this one does not, the spot-tracking
    is caused by the gamma weighting -- which localises weight around spot -- and
    not by the strike/OI structure. This is the diagnostic that says WHAT to fix.
    """
    num = den = 0.0
    for sym, oi in oi_map.items():
        parsed = gexmod._parse_occ(sym)
        if parsed is None:
            continue
        num += parsed[2] * oi
        den += oi
    return (num / den) if den > 0 else None


def oi_imbalance_crossover(oi_map: dict):
    """CONTROL measure: strike where cumulative (callOI - putOI) crosses zero.

    Also gamma-free and therefore spot-free by construction. This is the level a
    'where does dealer positioning flip' measure should be anchored to.
    """
    per_strike: dict[float, float] = {}
    for sym, oi in oi_map.items():
        parsed = gexmod._parse_occ(sym)
        if parsed is None:
            continue
        _, is_call, strike = parsed
        per_strike[strike] = per_strike.get(strike, 0.0) + (oi if is_call else -oi)
    cum = 0.0
    prev = None
    for k in sorted(per_strike):
        cum += per_strike[k]
        cur = 1 if cum >= 0 else -1
        if prev is not None and cur != prev:
            return round(k, 2)
        prev = cur
    return None


def ols(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return slope, intercept, r2, n


def run_scenario(name, scenario, crossover, spots, out):
    oi_map, meta = build_chain(scenario, crossover)
    print(f"\n{'=' * 76}\nSCENARIO: {name}")
    if crossover:
        print(f"  chain FIXED. call/put OI imbalance flips at strike {crossover:.1f}")
    print(f"  {meta['n_contracts']} contracts, expiry {meta['expiry'][:10]}")
    print(f"{'=' * 76}")
    print(f"  {'spot':>8} {'flip':>10} {'flip-spot':>10} {'label':>10} "
          f"{'centroid':>10} {'cent-spot':>10} {'qual':>5}")
    # Gamma-free controls — computed ONCE, since the chain never changes.
    oi_c = oi_centroid(oi_map)
    oi_x = oi_imbalance_crossover(oi_map)
    print("  gamma-free controls (chain is FIXED, so these are CONSTANT by design):")
    print(f"    OI-weighted centroid      = {oi_c if oi_c is None else round(oi_c, 2)}")
    print(f"    OI-imbalance crossover    = {oi_x}")

    rows = []
    for s in spots:
        snaps = build_snapshots(oi_map, s)
        g = gexmod._compute_gex(snaps, oi_map, s)
        c = centroid(snaps, oi_map, s)
        flip = g.get("flip_strike")
        rows.append({"spot": s, "flip": flip, "label": g.get("label"),
                     "centroid": round(c, 2) if c else None,
                     "oi_centroid": round(oi_c, 2) if oi_c else None,
                     "oi_imbalance_crossover": oi_x,
                     "quality_ok": g.get("quality_ok"),
                     "contract_count": g.get("contract_count")})
        fs = f"{flip - s:+.1f}" if flip is not None else "--"
        cs = f"{c - s:+.1f}" if c is not None else "--"
        print(f"  {s:8.1f} {str(flip):>10} {fs:>10} {str(g.get('label')):>10} "
              f"{(round(c, 1) if c else '--')!s:>10} {cs:>10} "
              f"{str(g.get('quality_ok')):>5}")

    res = {"meta": meta, "rows": rows}
    fl = [(r["spot"], r["flip"]) for r in rows if r["flip"] is not None]
    ce = [(r["spot"], r["centroid"]) for r in rows if r["centroid"] is not None]
    for tag, pairs in (("flip", fl), ("centroid", ce)):
        if len(pairs) >= 3:
            o = ols([p[0] for p in pairs], [p[1] for p in pairs])
            if o:
                slope, intercept, r2, n = o
                res[f"{tag}_vs_spot"] = {"n": n, "slope": round(slope, 4),
                                         "r2": round(r2, 4)}
                tracks = abs(slope - 1.0) < 0.25 and r2 > 0.9
                if tracks:
                    verdict = "TRACKS SPOT (artifact)"
                elif abs(slope) < 0.25:
                    verdict = "anchored / independent"
                else:
                    verdict = "partial"
                print(f"  -> {tag:>8} ~ spot:  slope={slope:+.3f}  R^2={r2:.3f}"
                      f"   [{verdict}]")
        else:
            res[f"{tag}_vs_spot"] = {"n": len(pairs), "note": "too few points"}
            print(f"  -> {tag:>8} ~ spot:  only {len(pairs)} usable point(s)")
    out[name] = res


def main():
    spots = [float(x) for x in range(700, 801, 10)]
    out: dict = {
        "generated": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
        "experiment": ("chain held FIXED, spot swept; a level that moves with spot "
                       "while positioning is unchanged is an artifact"),
        "sigma": SIGMA, "spots": spots, "scenarios": {},
    }
    print("SYNTHETIC-CHAIN PROBE — chain FIXED, spot swept")
    print(f"spots: {spots[0]:.0f} .. {spots[-1]:.0f}   flat IV = {SIGMA}")

    run_scenario("crossover@730 (below spot-center)", "crossover", 730.0, spots,
                 out["scenarios"])
    run_scenario("crossover@770 (above spot-center)", "crossover", 770.0, spots,
                 out["scenarios"])
    run_scenario("calls_only (no true flip exists)", "calls_only", None, spots,
                 out["scenarios"])
    run_scenario("symmetric OI (nets to zero)", "symmetric", None, spots,
                 out["scenarios"])

    outp = os.path.join(_ROOT, "logs",
                        f"gex_synthetic_probe_{datetime.now(PT):%Y-%m-%d}.json")
    with open(outp, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {outp}")

    print("\n" + "=" * 76)
    print("HOW TO READ THIS")
    print("=" * 76)
    print("The chain never changed. Only spot moved.")
    print("  flip slope ~ +1.0, R^2 ~ 1.0  =>  the flip is spot in disguise.")
    print("  flip slope ~  0.0             =>  the flip is anchored to positioning.")
    print("Same reading applies to the candidate centroid (the Gro/GAI split).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

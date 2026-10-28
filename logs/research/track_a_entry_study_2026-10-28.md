# Track A entry study — 2026-10-28 (signed: Claude)

Owner request 2026-10-28: "Start Track A study". Read-only research; no code changed.

## Data and method
- **Signals.** Rebuilt every Track A FADE/RIDE trigger by calling the live code (`strategy/day_tier_entry_trigger.compute_entry_trigger`) at each completed 5-min bar.
  - Inputs: stored option walls (`logs/gex_history.jsonl`) and IEX 5-min bars, the same feed the bot reads live.
  - Wall history by symbol: SPY/QQQ from 07-27; AAPL/AMZN/GOOGL/META/MSFT/NVDA/TSLA from 08-31; EWY/MU/SNDK/DRAM from 09-06/08.
  - The other day names (AMD, AVGO, NFLX, COIN, PLTR, SMCI, UBER) have only had walls stored since 10-28, so they are excluded.
- **Fidelity.** After the 10-10 volume-rule change, the rebuild reproduces 68 of 79 live Track A triggers (86%) with the same wall and direction. Before 10-10 the live volume rule was looser, so the live set differs there.
- **Pricing.**
  - Fill and price path: next 5-min bar open; SIP bars for the path; stop checked before target; flat at the 15:55 close; 5 bps cost.
  - "Live geometry" means TRACK-A-STOP-1026: stop room = max(structural, 6 × the 5-session time-of-day 5-min ATR), target = 0.35 × the 20-day median range.
- **Split.** Discovery: before 2026-10-01. Test: 10-01..10-28.
- **Not modeled:** the one-share cap, account rooms, Layer A side bias and conviction.

## Result 1 — why the live trades lost (math check)
| Same rebuilt signals (first per name-day, n=154) | mean R | win % | t |
|---|---|---|---|
| Old geometry (structural stop ≈1.5 noise-ATR, pin/2R target), live until 10-26 | −0.37 | 34% | −3.4 |
| Live geometry since 10-27 (TRACK-A-STOP-1026) | −0.02 | 56% | −0.4 |

The old geometry reproduces the live record of 33 Track A trades (−0.37 to −0.55R).

The loss came from stops inside normal noise. The 10-27 change already moves the same entries to roughly breakeven.

## Result 2 — no entry condition separates winners
437 pattern signals (first per name-day-side, including volume-failed ones), live geometry. 27 buckets tested; none has t > 2 in discovery.

| Bucket | Discovery R | Test R |
|---|---|---|
| Volume-confirmed (live rule) | +0.03 | +0.05 |
| Volume-failed | −0.04 | −0.04 |
| FADE long | +0.00 | +0.08 |
| FADE short | +0.02 | −0.03 |
| RIDE long | −0.08 | −0.09 |
| RIDE short | −0.24 (t −2.5) | −0.07 |
| Entry far from wall (top third) | −0.05 | −0.11 |

Time of day, trend/VWAP alignment, gap, index vs single name, strength, near-expiry, pin side and sign reliability all flip sign between the two halves.

## Result 3 — alternatives tried (none wins)
| Family | Variants | Best discovery t | Both halves t > 1 |
|---|---|---|---|
| Exit grid (stop 3–8 noise-ATR; range/R/hold targets) × 5 subsets incl. RIDE reversed | 240 | 1.84 (test −0.04R) | 0 |
| Pull-to-pin (positive gamma, far from centroid, PM, ≤2 DTE) | 36 | 0.18 | 0 |
| **6-year** live trigger on proxy walls (round strikes ≈ live walls, 84% on $5 multiples); 58,383 signals | 384 | 1.78 (test −0.005R) | 0 |
| Intraday momentum (first / 12th half-hour → last half-hour, Gao et al. 2018), 6y | 18 | 2.61 on 49 trades (test −37 bps) | 0 |
| Gamma regime: follow the open drive in negative gamma, fade it in positive | 36 | 0.33 | 0 |
| **Total** | **741** | — | **0** |

- **The bot's current rule over 6 years (proxy walls):**
  - FADE: −0.025R (t −2.1) in discovery, +0.011R in test.
  - RIDE: −0.028R (t −3.0) in discovery, −0.019R (t −1.8) in test.
- **The positive-gamma premise does not hold on these names in this window.** Fading the morning move on positive-gamma days lost −13 bps in discovery and −22 bps in test (t −2.2).
- **Opening-range breakout and the last half-hour** were already tested in the Track B lab (`lab.py` `s_orb`, `s_lasthalf`) and did not win.

## Diagnosis
- The wall-sweep and wall-break triggers carry about zero gross directional edge. The price move after a trigger is a coin flip; mean favorable/adverse moves are equal in noise-ATR units.
- The 5 bps cost is the loss. Tight stops multiplied that cost into −0.37R per trade.
- **What would have been profitable:** a setup whose mean move after the trigger is at least the cost. Nothing tested here reaches that with significance.
  - The only consistent positive is the live volume confirmation under the new stop: about +0.04R in both halves, t < 1, so not proven.
- **RIDE is the consistently weaker leg.** It is negative in both halves on the 3-month rebuild and on the 6-year proxy.

## Limits
- Three months of real walls, 13 names. The 6-year test uses proxy walls and cannot reproduce the gamma sign.
- Test-year looks: each family was viewed once on test, after picking on discovery. With 741 variants and no discovery pick at t > 2, no multiple-testing adjustment is needed to reject.

Scripts (scratchpad, not tracked): `ta_rebuild_iex.py`, `ta_lab.py`, `ta_geom.py`, `ta_feat.py`, `ta_alt.py`, `ta_pin.py`, `ta6y.py`, `ta_imom.py`, `ta_regime.py`.

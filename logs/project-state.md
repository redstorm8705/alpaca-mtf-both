# Project state — alpaca-mtf-bot (2026-11-05 evening PT)

Shipped and live today: KILL-PARTIAL-1104 (PR 848, verified live: 6 multi-fill orders validated, attribution exact),
Schwab CBOE index reader (PR 846, inert until wired), dashboard held legs + Swing trend-exit label (PR 845), design
records (PRs 843, 847, 850). Owner approvals on record: REGIME-SIZE-1104, CATALYST-DYNAMIC-1104, KILL-PARTIAL-1104.

Exact next steps, in order:
1. **REGIME-SIZE-1104 slice 1 (the real CBOE VIX):** `git apply logs/wip/regime_size_1104_slice1_2026-11-05.patch`
   on a fresh branch (strategy/volatility_regime.py + tests/test_vix_source.py + post-live expectation). 211 tests pass,
   statics clean. Cold-2nd FAILED rounds 1-4 (F1 clock lag, F2 yfinance retry storm, F3 freshness gap, F4 clock read
   before the slow call, F5 allocator low_vol label) — all fixed in the patch; the round-5 review was cut off by the
   weekly limit, so run a FRESH cold-2nd first, then markers, preship with
   `--context logs/wip/regime_size_1104_slice1_preship_context.txt`, ship, deploy, verify the
   `Volatility regime: ... schwab ... [change=regime-size-1104]` line during RTH. Then slice 2 (run_cycle score gates on
   the fresh-only `vix_value`, stop width on the basis/prior close, sticky VIX>35, sizing records).
2. **SWING-RATCHET-1105 rev 9** (`logs/design_records/swing_ratchet_2026-11-05.md`): board + masked-loss seat + GAI
   approve; Codex rejected rev 8 (client-order-id length, fixed in rev 9) — Codex must confirm rev 9 (Gro may not stand
   in after a Codex reject), then bring the owner the one-page approval, then build.
3. **Kill switch after the close:** failed closed 3x (4:04/4:36/5:09 PM ET) with "exact tier attribution is
   unresolved" after "reattributed impossible negative protected replay NVDA/qhm=-2" (NVDA QHM stop-out + Day short).
   No RTH impact. Root-cause with the NVDA post-mortem (post-live #851).
4. CATALYST-DYNAMIC-1104 Phase A build (approved).
5. Owner questions: (a) 11-02 "ship on full BGC alignment" — standing rule or one night? (b) widen REGIME-SIZE-1104
   Covers to data/fmp_client.py for the FMP VIX tier? (c) fork_reminder.py text change (gate file).

Live state: BRK.B 2 sh protected by the OWNER's stop at $561.85 (untagged; the Swing manager logs "re-protecting"
each cycle and the broker refuses a duplicate — expected until the ratchet ships). No NVDA position. GE sold by the
owner, ledger healed. F6 off.


# REPORT SINGLE-SOURCE-OF-TRUTH — full audit + 10-point reconciliation + optimization plan
**Rafael mandate 2026-08-20:** "The html pages STILL do not show accurate and coherent data. This has
plagued me since I started back in April. Why hasn't this still been fixed yet?"
**Decision locked (Rafael):** *ledger is sole truth.* **Doctrine:** BUILD-DON'T-JUST-FIX + Beck tidy-first —
this ships as ONE reporting diff; the upstream tracker-pollution root fix is a SEPARATE sequenced build.

---

## WHY IT HAS PERSISTED SINCE APRIL (root cause, verified at source)
**No single source of truth. Each report tile reads a DIFFERENT dataset, and every past fix repointed ONE
tile to the correct data while leaving the tile next to it on the old data — so fixing one number silently
breaks its neighbor's reconciliation.** Whack-a-mole across sources. Five sources render adjacent on the
pages:
1. **Live Alpaca-FIFO ledger** — `reporting/pnl_ledger.build_ledger()` (authoritative per the P&L rule).
2. **Frozen `eod_YYYY-MM-DD.json` snapshots** — per-day values frozen at each close.
3. **Polluted tracker per-trade** — `trade_log.json` closed[] (the `$0.00` fill-unverified exits + synthetic
   shorts from the Slack dump live here).
4. **`lifetime_pnl_cache.json`** — written ONLY by `pnl_ledger.heal_history`, which **the live runtime never
   calls** (last heal 2026-07-10 on the Mac clone; refreshed only out-of-band by an OCI job/CLI).
5. **The 16pt/edge "validation universe"** — `_strategy_validation_html` mixes `fifo_edge.json` n_legs with
   `trade_log len(closed)`.

### The concrete incoherences (from Rafael's 2026-08-20 screenshots)
- Monthly: day-tiles sum **−$74.11** vs "Monthly P&L" **+$49.67**; 100%-WR loss day (Aug10 −$11.82); 0%-WR
  green day (Aug18 +$19.78); "228 vs 152 vs 319" trade counts on one page.
- Weekly: days sum **−$35.95** vs "Week P&L" **+$25.70**; Thursday tile **+$28.60** vs its trades summing
  **−$6.00**; "WR 0%" while MARA (+$2.56) is the listed biggest winner.
- Lifetime P&L **+$199.86 (weekly)** vs **+$196.80 (monthly)** — same "all-time" number, two values.

### The exact mechanisms (per-page tile→source, verified)
- **Monthly** (`monthly_review.py`, read in full): headline `$` = `compute_period_stats.total_pnl` = Σ
  **ledger.per_day** (correct); **day-cell `$`** = each `eod.alpaca_pnl` (frozen snapshot); **day-cell WR** =
  `eod.trades[].pnl` (tracker). → `$` and WR from different datasets in one tile = the impossible rows.
  Edge summary line = `trade_log` (152, PF 0.59); edge body = `_strategy_validation_html` (319, PF 1.47).
- **Weekly** (`weekly_review.py`, agent full read): "this week" computed **3 ways on one page** — headline =
  ledger; stats-row Trades/WR/Score/TQI = `eod.trades[]`; Detail-stats Edge/MRI = `trade_log.closed[]`. Per-day
  tiles are a **4th** universe: frozen scalars `pnl_today`/`trades_today`/`win_rate_today` (the +$28.60-vs-−$6.00
  bug). Biggest-winners set (`pnl is not None`) ≠ WR set (`status=="closed"`) → "0% WR + a listed winner."
  `_strategy_validation_html` (defined here, rendered on MONTHLY): summary/score-table = `fifo_edge.n_legs`
  (152) vs exit-table/drawdown/hold = `len(trade_log.closed)` (319) — two universes, one card.
- **Dashboard** (`generate_dashboard.py`, agent full read): All-Time `$` = LIVE `equity − net_deposits`;
  All-Time WR/count = **FROZEN cache** (39.9%/158 on the stale clone). Monthly reads the frozen cache
  `total_pnl`; weekly/dashboard read live equity → **that IS the +199.86/+196.80 split, and it grows unbounded
  between heals** because `heal_history` isn't in the runtime. Two live writers (`live_data_writer` 30s +
  `run_cycle` 5-min); `from generate_dashboard import generate` inside the writer loop does NOT hot-reload
  (module cached) → a code change needs a writer restart.

---

## THE HARDENED DESIGN (adversarial teardown baked in — Taleb/Derman POV found the design FALSE as written)
A new **`reporting/report_figures.py`** builds ONE canonical figures object per render from `build_ledger()`.
Every `$`/count/WR tile on dashboard/weekly/monthly reads ONLY that object. `eod`/`trade_log` contribute ONLY
non-P&L metadata (score/TQI/exit_reason/MRI). **The adversarial pass proved the naive version would false-page
constantly — these fixes are mandatory:**

| # | Hole (adversarial) | Fix baked into the design |
|---|---|---|
| 1 CRIT | `(symbol, entry_time)` metadata join misses ~100% (Alpaca fill UTC/`Z` ≠ bot-clock PT/`-07:00`, different instants) → every score/TQI/exit-reason cell blanks | **Join on `client_order_id`** (already half-built: `build_coid_map()` — fill.order_id→order.client_order_id) |
| 2 CRIT | Equity-based headline (realized+unrealized) never == Σ realized `per_day` → invariant false-fires on EVERY open position | **Headline ≡ Σ realized `per_day`.** Unrealized is a SEPARATE line, excluded from the day-cell invariant |
| 3 CRIT | Day-WR (round_trip level) ≠ headline WR (entry-level, partials merged); a cross-day partial = 1 header trade but 2 day-tiles | Pin ONE attribution rule (entry's final-exit date), drive day-WR AND day-`$` off the SAME set; or label them distinct metrics — never assert "one set" |
| 5 HIGH | round-of-sum (`lifetime`) vs sum-of-rounds (cells) → invariant false-trips as history grows | **Headline = Σ(rounded per_day cells)** (tautologically == grid) |
| 6 HIGH | `build_ledger` = 4 sequential live fetches (non-atomic); a fill mid-render → two tiles disagree | Build the object ONCE, thread to every tile (kill secondary `build_ledger`/`_fetch_alpaca_equity` in render path); treat equity-invariant as **soft telemetry**, not a page-blocking gate |
| 7 MED | `unmatched_closes` = real cash, $0 in `per_day`, no round_trip → "1 closed trade / $0.00 / 0 WR" row + breaks equity-invariant | Surface an "unattributed" badge on the affected day + in the invariant reason; exclude its magnitude from the equity-drift compare |
| 8 MED | Degraded "DATA UNAVAILABLE" overwrites last-good page → self-inflicted outage + alert storm | Keep last-good render with a stale badge ("as of HH:MM PT — ledger unreachable, retrying"); bare "DATA UNAVAILABLE" only when no last-good; "unreachable" = info/retry (no Slack), "reconciled-but-drifted" = critical |
| 9 MED | 319-vs-152 validation universe next to ledger numbers re-introduces adjacency incoherence | Mandate a universe tag on every count ("executed" vs "evaluated"); route validation counts to a labeled block |

**Concern that did NOT hold (adversary confirmed):** a day with nonzero `per_day` but zero round_trips is
IMPOSSIBLE by construction (`per_day` incremented only inside `_close`, same iteration that appends a
round_trip). The `_pt_date` PT-keying is DST-correct; any day-boundary bug would live only in `report_figures`
if it iterates civil dates in a non-PT tz — pin PT `.date()` with tz-aware coercion.

### Reconciliation gate (the mechanism, per "documentation-is-not-enforcement")
At render: assert `|headline − Σ rounded day-cells| < $0.01` AND day-WR/`$` share one set. On breach → visible
**RECONCILIATION ERROR** banner + Slack, never ship silent numbers. This is the Beck test-as-gate that stops
the whack-a-mole for good.

---

## 10-POINT PER-FILE AUDIT (runs on each, before its patch — full-read gate first)
Files: `monthly_review.py` (done), `weekly_review.py` (done), `generate_dashboard.py` (done),
`reporting/metrics.py` (done), `reporting/pnl_ledger.py` (done), `weekly_perf_audit.py`, `scan_to_html.py`,
`options_scanner.py`. RC findings already surfaced: RC-3 silent excepts (weekly L416-417/L1011-1016; dashboard
L554); RC-1 naive-datetime `.astimezone()` on naive = host-tz-dependent (weekly L976/1014/1135; dashboard
L306). RC-2/RC-5 PASS on all three generators (paths anchored, writes atomic).

## OPTIMIZATION PLAN
1. **One live fetch per render**, cached with a short TTL (build_ledger paginates Alpaca — never per-tile).
2. **Kill the eod-snapshot + trade_log `$` paths from the render** (metadata only).
3. **Collapse the 3 trade-count definitions to ONE** (entry-level from the ledger), universe-label the rest.
4. **Wire `heal_history` into the runtime OR have `report_figures` read live** so lifetime stops drifting
   between out-of-band heals (root of the +199.86/+196.80 split).

## GRO EXTERNAL DESIGN REVIEW — additional gaps folded in (2026-08-20, gpt-oss-120b)
Gro (MODE-2 architecture lens) confirmed the design is "a huge step forward" but named gaps the adversarial
pass did not, now REQUIRED in the build:
- **HIGHEST RISK — one immutable, VERSION-STAMPED snapshot handed to EVERY tile INCLUDING the headline.** The
  reconciliation invariant is checked ONCE after the snapshot; but if the dashboard headline is recomputed from
  a SEPARATE live-equity fetch (as today), a fill arriving between the two re-drifts headline vs grid and the
  soft-telemetry gate misses it. `report_figures` returns a `LedgerSnapshot(version, ts)`; the headline derives
  from `Σ snapshot.per_day`, NOT a second `_fetch_alpaca_equity()`. Every tile + the invariant compare the SAME
  version id. Atomicity is the keystone — if it breaks, every other safeguard collapses.
- **The invariant must ALSO cover the lifetime "All-Time $" line**, not just headline-vs-day-cells — else the
  `equity−net_deposits` line drifts from Σ realized (the +199.86/+196.80 split) UNCAUGHT.
- **Canonical PT-day function, called once per FILL (not per tile)** — fixes the RC-1 naive `.astimezone()`
  day-bucketing (weekly L976, dashboard L306) that can put one trade on two day-tiles.
- **Metadata-only block carries ZERO `$`/WR** (physically separate section, not merely labeled) — the 319
  "evaluated" universe never shares a card with ledger `$` numbers.
- **Last-good render cache is version-tagged**; degrade serves last-good HTML + "stale as of HH:MM" banner,
  never a blank page.
- **Automated consistency test harness** `tests/test_reporting_consistency.py` — inject a late fill mid-render
  (thread+barrier), clock-skew, and a truncated snapshot JSON; assert the banner fires / last-good serves. This
  is the regression mechanism so the fixes survive future edits (documentation-is-not-enforcement).

## GATE + SEQUENCE (this is a BUILD, not a patch)
Full reads (done) → **BGG design pass on THIS record** (board + Gro + GAI, Open Question / Self-QA Gate #4) →
implement `report_figures.py` + migrate the 3 pages → statics + cold-2nd + adversarial-claims + impact →
Gro+GAI preship on the diff → ship (branch→PR→CI→merge→OCI, restart mtf-writer since generate_dashboard is
module-cached) → verify pages reconcile. **Reporting-layer only → NOT risk-path.** Tracker-pollution root
(the `$0.00` fill-unverified exits / synthetic shorts) = separate sequenced build.

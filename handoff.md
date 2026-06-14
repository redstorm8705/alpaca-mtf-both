# handoff.md — MTF Bot Authoritative State

> Session memory protocol: read this FIRST at every session start.
> Last updated: 2026-06-14 (S60, overnight autonomous session)

---

## 🔴 CRITICAL — BOT IS STILL DARK (as of 2026-06-14 ~07:15 UTC)

The BV-5 fix is **committed and pushed to the branch `claude/vibrant-cannon-ppodcr`**
but is **NOT deployed to OCI and NOT merged to main**. The bot running on OCI is on
the OLD code with the BV-5 hard block (STRESSED/HIGH/CRITICAL → block all entries).

**A restart alone will NOT fix this** — `launch_bots.sh` does not git-pull; it restarts
whatever code is already on OCI's disk. The new code must be rsync'd to OCI first
(`deploy.sh`), THEN the bot restarted.

**Why Claude could not do it this session:** the remote Claude Code container cannot
reach OCI — TCP/22 to `129.153.208.32` is blocked by the environment egress policy,
there is no SSH key in the container (`~/.ssh/` empty), and `ssh` is not installed.
Same egress policy blocks `api.deepseek.com` (DS audits).

### Deploy steps Rafael must run (from Mac or any machine with the OCI key)
```bash
cd <repo>
git fetch origin claude/vibrant-cannon-ppodcr
git checkout claude/vibrant-cannon-ppodcr   # or merge into the branch OCI tracks

# Deploy the 3 changed files (rsync via deploy.sh — needs ~/.ssh/mtf_bot_oracle)
./deploy.sh strategy/run_cycle.py
./deploy.sh events/macro_risk_index.py
./deploy.sh events/news_monitor.py
./deploy.sh pyproject.toml      # optional (lint config only, not runtime)

# Restart on OCI
ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32 \
  'cd /home/ubuntu/mtf-bot && pkill -9 -f "main\.py"; nohup ./launch_bots.sh >/dev/null 2>&1 &'
```
### ⚠️ POST-DEPLOY VERIFICATION — confirm the first MRI refresh SUCCEEDS
There is a startup interaction that can keep the bot dark even with the fix deployed:

- `MacroRiskIndex.__init__` calls `_restore()`. If `logs/mri_state.json` on OCI is
  missing or **>20h stale** (very likely after 6 days dark), `_restore()` initializes
  level = **CRITICAL** (macro_risk_index.py L807-808, L826-828).
- main.py instantiates MRI with **no startup `force=True` refresh** (main.py L437). The
  first real refresh runs on the first `run_cycle()` (run_cycle.py L862).
- BV-5 hard-blocks at CRITICAL. So until the first refresh SUCCEEDS, the bot is blocked.
- If that first `refresh()` **fails** (any feed hiccup → `_compute()` raises ConnectionError
  at L709), `level()` returns the CRITICAL restore-default and **the bot stays dark despite the fix.**

**After deploy+restart, grep `logs/mtf_bot.log` for the first MRI line:**
```bash
ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32 \
  'tail -n 200 /home/ubuntu/mtf-bot/logs/mtf_bot.log | grep -E "MRI refreshed|MRI refresh failed"'
```
- `MRI refreshed: score=… level=NORMAL` → ✅ fixed, bot will trade.
- `MRI refresh failed: …` → ❌ stuck at CRITICAL default; investigate feeds (FMP_API_KEY,
  Alpaca Data) before expecting entries. This is the most likely reason the bot stays dark
  post-deploy. (See deferred item D1/D5 — the fail-closed-to-CRITICAL default is the design fork.)

Then confirm the scan loop is live and MRI is no longer hard-blocking at STRESSED.

---

## BV-5 FIX — what shipped this session (branch `claude/vibrant-cannon-ppodcr`)

Board-voted Option C (2026-06-12): demote STRESSED from hard-block to soft handling;
keep HIGH/CRITICAL hard block; cut the news-bonus inflation that was pushing MRI to
STRESSED ~70% of cycles on a calm tape (VIX ~19.5).

| Commit | File | Change |
|--------|------|--------|
| `56a2575` | `strategy/run_cycle.py` | BV-5 gate: block tuple `("STRESSED","HIGH","CRITICAL")` → `("HIGH","CRITICAL")`. STRESSED now uses existing soft handling (0.70x size floor L1104-1109, MIN_SCORE +2 L1157). |
| `a44e2cc` | `events/macro_risk_index.py` | `inject_news_state()` news bonus cap 35/20/10 → 15/10/5; market-reaction gate tightened to `_ps < 10` (was `_ps == 0`); `gated` flag = `(raw_bonus > bonus)`. |
| `0bd0b8f` | `events/news_monitor.py` | `_classify()` substring → pre-compiled word-boundary regex (kills "congress"/"congressional", "ppi"/"mississippi" false positives). Added "trading halts" to KEYWORDS_HALT. Type-annotation fixes. New `pyproject.toml` (ruff line-length=130). |

All three: full-read gate ✓, 10-pt + RC audit ✓, board vote ✓, GAI audit ✓
(DS blocked by egress — GAI authorized as stand-in by Rafael), static analysis
(py_compile/mypy/ruff) all PASS, cold second-agent PASS. Rafael approved each.

**Effective behavior post-deploy:** VIX 19.5 + 5 alerts → 9 price pts + 10 news pts
(gated, _ps<10) = 19 → NORMAL (below 21 ELEVATED). Bot scans and can enter again.

---

## OPEN ITEMS / DEFERRED (need Rafael's decision — see logs/pending_claude_session_2026-06-14.md)

1. **DEPLOY + RESTART the BV-5 fix to OCI** — bot is dark until this happens (above).
2. **Merge `claude/vibrant-cannon-ppodcr` → main** — or confirm which branch OCI tracks.
3. **DS egress block** — resolution options in pending package (new env with allowlist,
   or keep GAI-as-DS-standin). Both DS+GAI API keys appeared in transcript; rotate when convenient.
4. **D1 — macro_risk_index.py stale-level, NO staleness ceiling** (CONFIRMED via full read).
   `level()` (L178-183) returns `_last_known_good_level` whenever `_refresh_failed` is True,
   with NO time bound. The restart-time decay (`_restore`, -10pts/hr, fresh after 20h) does
   NOT apply to a live process whose feed keeps failing — it stays on the last good level forever.
   `_last_known_good_at` is tracked but never consulted by `level()`. **Design fork (Rafael's call):**
   on sustained outage, fail to NORMAL (permissive, trades through a blackout) or to a conservative
   level (blocks)? GAI earlier suggested a 12h ceiling. RTH-affecting hotspot → full board + DS/GAI + approval.
5. **D5 — MRI startup defaults to CRITICAL + no force-refresh** (NEW, found this session).
   `_restore()` sets level=CRITICAL on missing/>20h-stale state file; main.py does no startup
   `force=True` refresh. With BV-5 hard-blocking at CRITICAL, a failed first refresh = bot dark.
   See POST-DEPLOY VERIFICATION above. Candidate fixes (design fork, Rafael's call): add
   `mri.refresh(force=True)` at main.py startup; and/or change restore default CRITICAL→NORMAL;
   and/or staleness ceiling (overlaps D1). Same hotspot gate applies.
6. **D2 — news_monitor.py RC-3** — L329-330 `except Exception: pass` in `_load_seen_hashes()`
   inner loop (per-entry malformed-ISO skip, no log). Low severity. Reopens RC-3 (was CLOSED).
7. **D3 — Derman keyword plural-expansion** (board minority) — word-boundary now misses "rate cuts"
   vs "rate cut" etc. Accepted as trade-off under market-reaction-first; logged for a future pass.
8. **D4 — CLAUDE.md invariant #9** — says "MRI does not gate entries directly"; code DOES hard-block
   at HIGH/CRITICAL. Doc catch-up to the board-approved BV-5 behavior. Proposed text in pending pkg.
9. **D6 — macro_risk_index.py stale docstrings** (doc-only, found this session). After the prior
   a44e2cc patch: `inject_news_state` docstring L250-256 still says bonus 10/20/35 (code: 15/10/5)
   and L239-249 gate says `price_score == 0` (code: `_ps < 10`). Module docstring L37-38 says ">20h
   → VIX-only estimate" (code: → CRITICAL). Safe cleanup, no logic change.

## STATE I DO NOT HAVE AUTHORITATIVE DATA ON (verify on OCI)
- Current open positions (audit preview 06-12 referenced MSTR short stopped out 06-08,
  UBER short open in profit — not confirmed current)
- Exact account equity (~$2.8K paper per project context)
- Live MRI level / current alert count on OCI

---

## INFRA QUICK REFERENCE
- OCI host: `ubuntu@129.153.208.32`, key `~/.ssh/mtf_bot_oracle`, bot dir `/home/ubuntu/mtf-bot`
- Deploy one file: `./deploy.sh <path>` (rsync to `mtf-bot:` SSH alias)
- Launch/restart: `./launch_bots.sh` (kills stale main.py, clears __pycache__, restart loop)
- No CI/CD, no GitHub Actions, no auto-deploy — deployment is manual rsync.
- Branch for this work: `claude/vibrant-cannon-ppodcr`. `origin/main` tip: `89ee635` (far behind).

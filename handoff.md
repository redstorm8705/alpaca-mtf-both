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
Then confirm in `logs/launcher.log` / `logs/bot.log` on OCI that the scan loop is live
and MRI is no longer hard-blocking at STRESSED.

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
4. **macro_risk_index.py stale-level fail-closed** (GAI flag, prior session) — `level()`
   can return stale data on feed failure; GAI proposed a 12h staleness ceiling. Needs board + patch.
5. **news_monitor.py RC-3** — L329-330 `except Exception: pass` in `_load_seen_hashes()`
   inner loop (per-entry malformed-ISO skip, no log). Low severity. Reopens RC-3 (was CLOSED).
6. **Derman keyword plural-expansion** (board minority) — word-boundary now misses "rate cuts"
   vs "rate cut" etc. Accepted as trade-off under market-reaction-first; logged for a future pass.
7. **CLAUDE.md invariant #9** — says "MRI does not gate entries directly"; code DOES hard-block
   at HIGH/CRITICAL. Doc catch-up to the board-approved BV-5 behavior. Proposed text in pending pkg.

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

# Queued for Review — 2026-08-14

---

## Item: live_data_writer.py — writer hot-reload root (importlib.reload fix)

**Finding:** Line 74-75 comment says "Import inside loop so any hot-reload of generate_dashboard works" but the `from generate_dashboard import generate` inside the loop does NOT hot-reload. Python's sys.modules cache means the first import is cached; subsequent loop iterations use the stale pre-fix version. This caused the dashboard bug fix (generate_dashboard.py updated 2026-08-10) to be invisible until service restart (2026-08-12 — 2 trading days stale). Documented in handoff.md 2026-08-12 as "writer hot-reload root."

**Classification:** NON-RTH (not in import chain of RTH entrypoints)

**Proposed fix:** Add `import importlib` at top; pre-import `generate_dashboard as _gd_mod` before the while loop; inside loop replace `from generate_dashboard import generate` + `generate()` with `importlib.reload(_gd_mod)` + `_gd_mod.generate()`.

**Board vote (autonomous):**
- Agent A (Strict Protocol Parser): **FAIL** — Process concerns: (a) 10-point audit not pre-documented in board prompt, (b) cold second-agent not yet run at time of board vote, (c) Gro/GAI audit not shown (though NON-RTH). Technical concern: `importlib.reload()` is not thread-safe per CPython docs — must confirm live_data_writer.py is truly single-threaded before apply.
- Agent B (Malicious Red Teamer): **PASS** — No exploit paths; process is display-only, OS-isolated from trading engine.
- Agent C (Quant Risk Manager): **PASS** — No sizing/P&L/scoring/stop-target paths involved.

**Why queued:** Board rule = 3/3 PASS required; Agent A FAIL → queue.

**Technical notes for Rafael's session:**
- Agent A's process concerns are procedural; the fix direction (importlib.reload) is technically correct.
- Thread-safety concern: `live_data_writer.py` runs a single-threaded while loop, no concurrent threads — `importlib.reload()` is safe in this context. However, Agent A's point should be explicitly verified in the cold-2nd prompt.
- Alternative fix: update `auto_deploy.sh` to restart mtf-writer when generate_dashboard.py changes — avoids importlib.reload entirely and matches Rafael's own suggestion.
- Static analysis: py_compile PASS, mypy PASS, ruff PASS (all clean on existing file).
- Recommend resolving Agent A's concerns in an interactive session: run full 10-point audit formally, cold-2nd with threading context, then resubmit.

**Priority:** P2 (display-only, no trading impact)

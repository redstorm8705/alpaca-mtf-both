# Queued for Review — 2026-08-17

---

## live_data_writer.py — importlib.reload() fix — BOARD-B FAIL

**Date:** 2026-08-17 (autonomous nightly run)
**Item:** Writer hot-reload root fix — `live_data_writer.py` uses `from generate_dashboard import generate` inside a loop with the intent of hot-reloading on deploy, but Python's module cache means it only imports once at startup.
**Proposed fix:** `importlib.reload()` on every 30s cycle.
**Board vote:** A=PASS | B=FAIL | C=PASS → QUEUED

**Agent B (Red Teamer) FAIL — exact attack vector:**
`importlib.reload(_gd_mod)` on every 30-second cycle converts `live_data_writer.py` from a static-import daemon (immune to post-start file modifications) into an automatic code-execution proxy. Any change to `generate_dashboard.py` on disk — including via `autonomous_review.py` pushing to `main` without a `mtf-writer` restart — executes every 30 seconds with full Alpaca API credentials (`load_dotenv` at module level) and write access to all state files. This expands the attack surface beyond what the normal `auto_deploy.sh` + restart path provides.

**Recommended alternative (safer, no auto-execution risk):**
Use the `auto_deploy.sh` approach instead: detect `generate_dashboard.py` changes in the deployed commit and restart `mtf-writer` via systemd. This achieves the same hot-reload goal (deploy takes effect without manual restart) without any `importlib.reload()` loop. The `auto_deploy.sh` file needs a full read + board review + Gro/GAI before modifying.

**Files involved:**
- `live_data_writer.py` (the queued fix — DO NOT apply importlib.reload approach without B re-review)
- `deploy.sh` / `auto_deploy.sh` (recommended alternative target — needs its own full gate)

**Action for Rafael session:** Design the `auto_deploy.sh` restart approach; bring through full gate. The `importlib.reload()` approach should NOT ship as-is per Board B's verdict.

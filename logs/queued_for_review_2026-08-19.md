
## live_data_writer.py — Board FAIL (Agent B Red Teamer) — 2026-08-19 ~10:00 PM ET

**Finding:** L74-76 `from generate_dashboard import generate` inside while True loop does NOT hot-reload — Python caches modules. Every deploy to generate_dashboard.py is invisible until mtf-writer is manually restarted. Comment claiming "hot-reload" is factually incorrect.

**Proposed fix:** Add `import importlib` at line 11; replace `from generate_dashboard import generate` + `generate()` with `import generate_dashboard as _gd_mod; importlib.reload(_gd_mod); _gd_mod.generate()`.

**Board votes:**
- Agent A (Strict Parser): PASS — all 4 protocol checks clean, real finding, no forbidden categories
- Agent B (Red Teamer): **FAIL** — see technical argument below
- Agent C (Quant Risk): PASS — zero sizing/P&L/scoring risk; display-only; antifragile design

**Agent B FAIL — exact argument:**
`importlib.reload(_gd_mod)` converts `generate_dashboard.py` from a static startup artifact (loaded once, then cache-frozen) into a persistent code-execution trigger firing every 30 seconds. The old code's module-cache behavior was accidentally a security property: write access to `generate_dashboard.py` post-startup had zero runtime impact. With the new code, write access at ANY time → code execution within 30s with live_data_writer process privileges (same OS user, same .env with API keys). Attack surface: anyone who can write to generate_dashboard.py (compromised CI, misconfigured file permission, writable package cache) achieves recurring code execution.

**Rafael review required.** Counter-argument for consideration: (1) generate_dashboard.py is in the same git repo as all other code; any actor with write access to that file also has write access to main.py/broker.py via the same deploy pipeline; (2) auto_deploy.sh already does git pull + restart, meaning the effective old attack window was "at the next service restart" not "never"; (3) this is paper money, not live capital. Rafael decides whether the attack-surface expansion is acceptable given the single-channel git deploy model.

**Static analysis on proposed version:** py_compile PASS | mypy PASS | ruff PASS


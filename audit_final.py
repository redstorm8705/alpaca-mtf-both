"""
audit_final.py
Verifies that scan_to_html.py and dashboard.html are correctly wired
for the 5:45 AM PST run. Run from the bot root directory.

Usage:
    python3 audit_final.py
"""
import sys, os, ast, re
from datetime import datetime
from zoneinfo import ZoneInfo

# ── P5-M1: RTH block — CLAUDE.md Guardrail 5 ─────────────────────────────────
_ET = ZoneInfo("America/New_York")
_rth_now = datetime.now(_ET)
if _rth_now.weekday() < 5:
    _rth_mins = _rth_now.hour * 60 + _rth_now.minute
    if (9 * 60 + 30) <= _rth_mins < (16 * 60):
        print("BLOCKED: Cannot run during RTH hours (9:30 AM–4:00 PM ET / 6:30 AM–1:00 PM PT weekdays).")
        sys.exit(1)
# ─────────────────────────────────────────────────────────────────────────────

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"

errors = 0

def ok(msg):   print(f"  {PASS} {msg}")
def fail(msg): print(f"  {FAIL} {msg}"); global errors; errors += 1
def warn(msg): print(f"  {WARN} {msg}")

# ── Locate files ──────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.join(ROOT, "scan_to_html.py")
DASH = os.path.join(ROOT, "dashboard.html")

print("\n" + "="*60)
print("  audit_final.py — bot wiring check")
print("="*60)

# ── 1. scan_to_html.py checks ─────────────────────────────────────────────────
print("\n[1] scan_to_html.py")

if not os.path.exists(SCAN):
    fail(f"File not found: {SCAN}")
else:
    src = open(SCAN, encoding="utf-8").read()

    # Parse AST to check function scopes
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        fail(f"Syntax error: {e}")
        tree = None

    if tree:
        top_level_fns = {n.name for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and
                         any(isinstance(p, ast.Module) for p in ast.walk(tree)
                             if hasattr(p, 'body') and n in getattr(p, 'body', []))}

        # More reliable: check module-level names directly
        module_fns = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]

        if "conviction_score" in module_fns:
            ok("conviction_score() is at module level (not nested)")
        else:
            fail("conviction_score() is NOT at module level — still nested inside write_html()")

        # Check it's NOT nested inside write_html
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "write_html":
                nested = [n.name for n in ast.walk(node)
                          if isinstance(n, ast.FunctionDef) and n.name == "conviction_score"]
                if nested:
                    fail("conviction_score() is still nested inside write_html() — move it out")
                else:
                    ok("conviction_score() not nested inside write_html() ✓")

        # Check build_rows is also module-level
        if "build_rows" in module_fns:
            ok("build_rows() is at module level")
        else:
            warn("build_rows() not found at module level")

        # Check conviction_score is called inside build_rows
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build_rows":
                calls = [n.func.id for n in ast.walk(node)
                         if isinstance(n, ast.Call) and hasattr(n.func, 'id')]
                if "conviction_score" in calls:
                    ok("conviction_score() is called inside build_rows()")
                else:
                    warn("conviction_score() not called inside build_rows() — check template string")

        # Check write_html calls build_rows
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "write_html":
                calls = [n.func.id for n in ast.walk(node)
                         if isinstance(n, ast.Call) and hasattr(n.func, 'id')]
                if "build_rows" in calls:
                    ok("write_html() calls build_rows()")
                else:
                    fail("write_html() does NOT call build_rows()")

    # Quick text checks
    if "fetch_vix" in src:
        ok("fetch_vix() present")
    else:
        warn("fetch_vix() not found")

    if "vix_regime" in src:
        ok("vix_regime() present")
    else:
        warn("vix_regime() not found")

    if "--watch" in src:
        ok("--watch flag present")
    else:
        warn("--watch argument not found")

# ── 2. dashboard.html checks ──────────────────────────────────────────────────
print("\n[2] dashboard.html")

if not os.path.exists(DASH):
    fail(f"File not found: {DASH}")
else:
    html = open(DASH, encoding="utf-8").read()

    # The bad pattern: ternary that produces CAUTION for non-today events
    bad_pattern = r"level\s*:\s*isToday\s*\?\s*['\"]HIGH_RISK['\"]\s*:\s*['\"]CAUTION['\"]"
    if re.search(bad_pattern, html):
        fail("Bad level ternary found: `level: isToday ? 'HIGH_RISK' : 'CAUTION'` — change to `level: 'HIGH_RISK'`")
    else:
        ok("No isToday→CAUTION ternary (level fix applied)")

    # Check HIGH_RISK is used for calendar events
    if "'HIGH_RISK'" in html or '"HIGH_RISK"' in html:
        ok("HIGH_RISK level present in file")
    else:
        warn("HIGH_RISK not found in dashboard.html")

    # Check news terminal JS present
    if "KW_HIGH" in html and "KW_CAUTION" in html:
        ok("News terminal keyword arrays present (KW_HIGH, KW_CAUTION)")
    else:
        warn("KW_HIGH / KW_CAUTION not found — news terminal JS may be missing")

    # Check sort order
    if "HALT:0" in html or "HALT: 0" in html:
        ok("News terminal sort order present (HALT→HIGH_RISK→CAUTION)")
    else:
        warn("Sort order block not found")

# ── 3. Bot root sanity checks ─────────────────────────────────────────────────
print("\n[3] Bot root")

expected_files = ["config.py", "scan_to_html.py", "dashboard.html"]
for f in expected_files:
    p = os.path.join(ROOT, f)
    if os.path.exists(p):
        ok(f"{f} present")
    else:
        warn(f"{f} NOT found in {ROOT}")

expected_dirs = ["data", "strategy", "indicators", "execution"]
for d in expected_dirs:
    p = os.path.join(ROOT, d)
    if os.path.isdir(p):
        ok(f"{d}/ directory present")
    else:
        warn(f"{d}/ directory NOT found — imports may fail")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*60)
if errors == 0:
    print("  \033[32mAll checks passed. Bot is ready for 5:45 AM PT.\033[0m")
    print("  Run: python3 scan_to_html.py")
else:
    print(f"  \033[31m{errors} issue(s) found. Fix above before running.\033[0m")
print("="*60 + "\n")
sys.exit(0 if errors == 0 else 1)

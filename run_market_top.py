"""
run_market_top.py
Pre-market wrapper for the market-top-detector skill.
Calls the skill script, writes composite score + zone to logs/market_top_latest.json.
main.py reads that cache at startup — Layer 5 uses it to supplement the 52w ATH gate.

Schedule: 5:30 AM PT Mon-Fri (pre-market window, before 8:45 AM ET open scan).
  crontab: 30 5 * * 1-5 cd /path/to/bot && /usr/local/bin/python3.10 run_market_top.py >> logs/market_top_cron.log 2>&1

Output: logs/market_top_latest.json (overwritten each run)
        logs/market_top/<timestamped reports> (full detail, kept for history)
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── RTH block ────────────────────────────────────────────────────────────────
PT = ZoneInfo("America/Los_Angeles")
_now = datetime.now(PT)
if _now.weekday() < 5:
    _mins = _now.hour * 60 + _now.minute
    if (6 * 60 + 30) <= _mins < (13 * 60):
        print("BLOCKED: Cannot run during RTH hours (6:30 AM–1:00 PM PT weekdays).")
        sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_market_top")

BASE_DIR    = Path(__file__).parent
LOGS_DIR    = BASE_DIR / "logs"
OUTPUT_DIR  = LOGS_DIR / "market_top"
LATEST_PATH = LOGS_DIR / "market_top_latest.json"
SKILL_SCRIPT = BASE_DIR / "claude-trading-skills-main" / "skills" / "market-top-detector" / "scripts" / "market_top_detector.py"

FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()


def run_skill() -> int:
    """Run the market-top-detector skill script. Returns process exit code."""
    if not FMP_API_KEY:
        logger.error("FMP_API_KEY not set — cannot run market-top-detector.")
        return 1
    if not SKILL_SCRIPT.exists():
        logger.error(f"Skill script not found: {SKILL_SCRIPT}")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(SKILL_SCRIPT),
        "--api-key", FMP_API_KEY,
        "--output-dir", str(OUTPUT_DIR),
    ]
    logger.info(f"Running market-top-detector skill...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            logger.info(f"  [skill] {line}")
    if result.returncode != 0 and result.stderr:
        logger.warning(f"  [skill stderr] {result.stderr[:500]}")
    return result.returncode


def write_latest_cache() -> bool:
    """Find the most recent market_top_*.json and extract key fields to latest cache."""
    reports = sorted(OUTPUT_DIR.glob("market_top_*.json"), reverse=True)
    if not reports:
        logger.error("No market_top_*.json found in output dir after skill run.")
        return False

    latest = reports[0]
    try:
        with open(latest) as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {latest}: {e}")
        return False

    composite = data.get("composite", {})
    score = composite.get("composite_score", 0)
    zone  = composite.get("zone", "Unknown")

    # Map zone string to numeric risk tier for easy comparison in main.py
    # Green=0, Yellow=1, Orange=2, Red=3, Critical=4
    zone_lower = zone.lower()
    if "critical" in zone_lower:
        tier = 4
    elif "red" in zone_lower:
        tier = 3
    elif "orange" in zone_lower:
        tier = 2
    elif "yellow" in zone_lower:
        tier = 1
    else:
        tier = 0

    cache = {
        "generated_at":   _now.isoformat(),
        "source_report":  str(latest),
        "composite_score": score,
        "zone":            zone,
        "zone_tier":       tier,    # 0=green … 4=critical
        "risk_budget":     composite.get("risk_budget", "N/A"),
    }

    _tmp = LATEST_PATH.with_suffix(".json.tmp")
    with open(_tmp, "w") as f:
        json.dump(cache, f, indent=2)
    _tmp.replace(LATEST_PATH)
    logger.info(
        f"market_top_latest.json written: score={score}/100 zone={zone} tier={tier}"
    )
    return True


def main():
    logger.info(f"run_market_top.py starting — {_now.strftime('%Y-%m-%d %H:%M PT')}")
    rc = run_skill()
    if rc != 0:
        logger.warning(f"Skill exited with code {rc} — attempting cache update anyway.")
    ok = write_latest_cache()
    if not ok:
        logger.error("Cache update failed — market_top_latest.json not updated.")
        sys.exit(1)
    logger.info("run_market_top.py complete.")


if __name__ == "__main__":
    main()

"""
run_ftd.py
Pre-market wrapper for the ftd-detector skill (O'Neil Follow-Through Day).
Calls the skill script, writes combined_state + quality_signal to
data/cache/ftd_state.json.  main.py reads the cache at startup — Layer 7
uses it to raise the MIN_SCORE floor when the market is in confirmed correction.

Schedule: weekdays 6:00 AM PT (pre-market, before run_movers.py at 6:30).
  crontab: 0 6 * * 1-5 cd /path/to/bot && /usr/local/bin/python3.10 run_ftd.py >> logs/ftd_cron.log 2>&1

Output: data/cache/ftd_state.json (overwritten each run)
        logs/ftd/<timestamped reports> (full detail from skill)
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# RTH block — pre-market only (4:00–6:30 AM PT on weekdays is the target window)
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
logger = logging.getLogger("run_ftd")

BASE_DIR     = Path(__file__).parent
LOGS_DIR     = BASE_DIR / "logs"
OUTPUT_DIR   = LOGS_DIR / "ftd"
CACHE_DIR    = BASE_DIR / "data" / "cache"
CACHE_PATH   = CACHE_DIR / "ftd_state.json"
SKILL_SCRIPT = BASE_DIR / "claude-trading-skills-main" / "skills" / "ftd-detector" / "scripts" / "ftd_detector.py"

FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()

# FTD combined_state values that trigger MIN_SCORE raise
CORRECTION_STATES = {"MARKET_IN_CORRECTION"}


def run_skill() -> int:
    if not FMP_API_KEY:
        logger.error("FMP_API_KEY not set — cannot run ftd-detector.")
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
    logger.info("Running ftd-detector skill...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            logger.info(f"  [skill] {line}")
    if result.returncode != 0 and result.stderr:
        logger.warning(f"  [skill stderr] {result.stderr[:500]}")
    return result.returncode


def write_cache() -> bool:
    """Parse latest ftd_detector_*.json report and write ftd_state.json."""
    reports = sorted(OUTPUT_DIR.glob("ftd_detector_*.json"), reverse=True)
    if not reports:
        logger.error("No ftd_detector_*.json found after skill run.")
        return False

    latest = reports[0]
    try:
        with open(latest) as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {latest}: {e}")
        return False

    ms          = data.get("market_state", {})
    qs          = data.get("quality_score", {})
    combined    = ms.get("combined_state", "")
    dual_conf   = ms.get("dual_confirmation", False)
    quality_sig = qs.get("signal", "")
    quality_num = qs.get("total_score", 0)

    # Numeric floor contribution for Layer 7 (matches logic in main.py)
    min_score_delta = 1 if combined in CORRECTION_STATES else 0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = {
        "generated_at":       _now.isoformat(),
        "source_report":      str(latest),
        "combined_state":     combined,
        "dual_confirmation":  dual_conf,
        "quality_signal":     quality_sig,
        "quality_score":      quality_num,
        "min_score_delta":    min_score_delta,   # Layer 7 addend (0 or 1)
    }

    _tmp = CACHE_PATH.with_suffix(".json.tmp")
    with open(_tmp, "w") as f:
        json.dump(cache, f, indent=2)
    _tmp.replace(CACHE_PATH)
    logger.info(
        f"ftd_state.json written: state={combined} quality={quality_sig}({quality_num}) "
        f"dual_confirmation={dual_conf} min_score_delta={min_score_delta}"
    )
    return True


def main():
    logger.info(f"run_ftd.py starting — {_now.strftime('%Y-%m-%d %H:%M PT')}")
    rc = run_skill()
    if rc != 0:
        logger.warning(f"Skill exited {rc} — attempting cache update anyway.")
    ok = write_cache()
    if not ok:
        logger.error("Cache update failed.")
        sys.exit(1)
    logger.info("run_ftd.py complete.")


if __name__ == "__main__":
    main()

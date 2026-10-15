# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""run_dip_zones.py — refresh the data-derived QHM dip-add zones once per trading day before the open (Claude 2026-10-15).

Cron (OCI, scripts/install_dip_zones_cron.sh). Runs execution/dip_zones.refresh() and exits; QHM reads the result
through dip_zones.zones_for(). A failed refresh pages Slack and leaves the last good zones in force.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")

from execution import dip_zones  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return 0 if dip_zones.refresh() is not None else 1


if __name__ == "__main__":
    sys.exit(main())

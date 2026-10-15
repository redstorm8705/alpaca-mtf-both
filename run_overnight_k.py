# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""run_overnight_k.py — refresh the dynamic overnight cap K once per trading day before the open (Claude 2026-10-14).

Cron (OCI, scripts/install_overnight_k_cron.sh). Computes execution/overnight_capacity.refresh() and exits; the
tiers read the result through overnight_capacity.overnight_k(). A failed refresh leaves the last good K in force.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")

from execution import overnight_capacity  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state = overnight_capacity.refresh()
    if state is None:
        logging.getLogger("run_overnight_k").warning("overnight K not refreshed — last good K stays in force")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

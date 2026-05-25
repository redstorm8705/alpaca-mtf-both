"""
OCI A1.Flex upgrade retry script.
Runs from OCI cron every 2 hours between 1–6 AM PT (08:00–13:00 UTC) Mon–Sun.
Uses Instance Principal auth — no API keys or config files required.

Setup required (one-time, in OCI console):
  1. Identity → Dynamic Groups → Create:
       Name: mtf-bot-instance-group
       Rule: instance.id = 'ocid1.instance.oc1.phx.anyhqljti3ebloycm7lidtw3erjpfnbrmf54ljm7x2jeavzuqel7igjqqfza'
  2. Identity → Policies → Create (in root compartment):
       Name: mtf-bot-flex-retry-policy
       Statement: Allow dynamic-group mtf-bot-instance-group to manage orm-stacks in tenancy
       Statement: Allow dynamic-group mtf-bot-instance-group to manage orm-jobs in tenancy

Once those two steps are done, run this script manually once to verify, then the cron handles it.
"""

import os
import sys
import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "logs" / "flex_retry.log"
FLAG_FILE = ROOT / "logs" / "flex_upgrade_success.flag"

STACK_OCID = "ocid1.ormstack.oc1.phx.amaaaaaai3ebloyaw73yf3ibme5ldugobra53vwttklhritd2zy65rl5eoeq"
COMPARTMENT_OCID = "ocid1.tenancy.oc1..aaaaaaaa5avjrm3v6niwkveuv4reszzlomtf54ew4md5bvpsntrg7eo7dpnq"

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def _slack(msg: str) -> None:
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    try:
        requests.post(webhook, json={"text": msg}, timeout=10)
    except Exception:
        pass


def _already_upgraded() -> bool:
    """Return True if we already succeeded — skip all work."""
    return FLAG_FILE.exists()


def _mark_success() -> None:
    FLAG_FILE.write_text(datetime.now(PT).isoformat())


def _trigger_apply() -> dict:
    """
    Trigger an OCI Resource Manager apply job using Instance Principal auth.
    Returns the job response dict on success, raises on failure.
    """
    try:
        import oci
        from oci.auth.signers import InstancePrincipalsSecurityTokenSigner
    except ImportError:
        log.error("oci SDK not installed — run: pip install oci")
        sys.exit(1)

    signer = InstancePrincipalsSecurityTokenSigner()
    client = oci.resource_manager.ResourceManagerClient(config={}, signer=signer)

    job_details = oci.resource_manager.models.CreateApplyJobOperationDetails(
        execution_plan_strategy="AUTO_APPROVED",
    )
    create_job_details = oci.resource_manager.models.CreateJobDetails(
        stack_id=STACK_OCID,
        job_operation_details=job_details,
        freeform_tags={"trigger": "auto-retry", "script": "oci_flex_retry.py"},
    )

    response = client.create_job(create_job_details)
    return response.data


def _wait_for_job(job_id: str, timeout_s: int = 600) -> str:
    """
    Poll until job reaches a terminal state. Returns 'SUCCEEDED' or 'FAILED'.
    """
    try:
        import oci
        from oci.auth.signers import InstancePrincipalsSecurityTokenSigner
    except ImportError:
        return "FAILED"

    signer = InstancePrincipalsSecurityTokenSigner()
    client = oci.resource_manager.ResourceManagerClient(config={}, signer=signer)

    terminal = {"SUCCEEDED", "FAILED", "CANCELED"}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get_job(job_id)
        state = resp.data.lifecycle_state
        log.info(f"Job {job_id} state: {state}")
        if state in terminal:
            return state
        time.sleep(30)
    return "FAILED"


def main() -> None:
    now_pt = datetime.now(PT)
    ts = now_pt.strftime("%Y-%m-%d %H:%M PT")

    if _already_upgraded():
        log.info("A1.Flex upgrade already succeeded — flag file present. Exiting.")
        sys.exit(0)

    log.info(f"=== OCI A1.Flex retry attempt at {ts} ===")

    try:
        job = _trigger_apply()
    except Exception as e:
        err = str(e)
        log.error(f"Failed to trigger apply job: {err}")
        if "host capacity" in err.lower() or "out of capacity" in err.lower() or "500" in err:
            _slack(f":hourglass: *OCI A1.Flex retry* — capacity still unavailable at {ts}. Will retry in 2h.")
        else:
            _slack(f":x: *OCI A1.Flex retry* — unexpected error at {ts}:\n```{err[:300]}```")
        sys.exit(0)

    job_id = job.id
    log.info(f"Apply job created: {job_id}")
    _slack(f":arrows_counterclockwise: *OCI A1.Flex retry* — apply job started at {ts}\nJob: `{job_id}`")

    final_state = _wait_for_job(job_id)
    log.info(f"Job {job_id} finished with state: {final_state}")

    if final_state == "SUCCEEDED":
        _mark_success()
        msg = (
            f":white_check_mark: *OCI A1.Flex UPGRADE SUCCEEDED* at {ts}!\n"
            f"Job: `{job_id}`\n"
            f"Bot is now running on A1.Flex (4 OCPU / 24 GB RAM). Auto-retry cron is now inactive."
        )
        log.info("SUCCESS — flag file written. Cron will self-skip on all future runs.")
    else:
        msg = (
            f":x: *OCI A1.Flex retry FAILED* at {ts}\n"
            f"Job: `{job_id}` → state: {final_state}\n"
            f"Likely still 'Out of host capacity'. Will retry in 2h."
        )
        log.warning(f"Apply job failed: {final_state}")

    _slack(msg)


if __name__ == "__main__":
    main()

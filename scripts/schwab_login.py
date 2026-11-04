#!/usr/bin/env python3
# ruff: noqa: E501, E402
"""
scripts/schwab_login.py — Schwab Market Data OAuth login (run on the server over ssh, weekly).

Approval-ID: SCHWAB-QUOTES-1103. Design: logs/design_records/schwab_quotes_2026-11-03.md.

    ssh mtf-bot "cd mtf-bot && venv/bin/python3 scripts/schwab_login.py"

1. Prints a Schwab login link (random OAuth `state`).
2. You log in; the browser lands on a https://127.0.0.1/... page that does not load.
3. Paste that page's full address when asked (typed input — never a command-line argument).
4. The script rejects the address unless `state` matches, exchanges the code, writes the token
   (data/state/schwab_token.json, mode 600) and runs the scope proof: Schwab must REFUSE the
   accounts endpoint (401/403). If it does not, the token is deleted, Schwab quotes are disabled
   and Slack is paged.

    venv/bin/python3 scripts/schwab_login.py --revoke

Deletes the token, the kill state, the request-budget state and the page-dedupe state, and posts
to Slack. Runbook: regenerate the app secret or delete the app at developer.schwab.com; run
--revoke; remove the app's access in Schwab account security; change the Schwab password if the
server itself is suspected.

Read-only: this script never places an order and imports nothing from execution/.
"""

import argparse
import logging
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_ROOT / ".env")
except Exception as _e:
    logging.getLogger("schwab_login").debug("dotenv load skipped: %s", _e)

from data import schwab_quotes as sq

logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(message)s")


def authorize_url(client_id: str, state: str) -> str:
    return f"{sq.AUTHORIZE_URL}?" + urlencode({"client_id": client_id, "redirect_uri": sq.REDIRECT_URI,
                                              "state": state})


def parse_redirect(pasted: str, expected_state: str):
    """Return the authorization code from the pasted redirect address, or raise ValueError.
    parse_qs URL-decodes the code (Schwab codes contain '%40' [third-party docs, unverified])."""
    u = urlparse((pasted or "").strip())
    if f"{u.scheme}://{u.netloc}" != sq.REDIRECT_URI:
        raise ValueError("address is not the https://127.0.0.1 redirect")
    qs = parse_qs(u.query)
    got_state = (qs.get("state") or [""])[0]
    if not got_state or not secrets.compare_digest(got_state, expected_state):
        raise ValueError("state mismatch — rejected (possible forged or stale redirect)")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise ValueError("no code in the address")
    return code


def exchange_code(code: str) -> dict:
    """POST /v1/oauth/token grant_type=authorization_code (HTTP basic auth app key/secret)."""
    key, sec = sq._app_creds()
    connect, read = sq._timeouts("token")
    resp = sq.requests.post(sq.TOKEN_URL, auth=(key, sec),
                            data={"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": sq.REDIRECT_URI},
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                            timeout=(connect, read))
    if int(resp.status_code) != 200:
        raise RuntimeError(f"token exchange refused: HTTP {resp.status_code}")
    return resp.json()


def login() -> int:
    key, sec = sq._app_creds()
    if not key or not sec:
        print("SCHWAB LOGIN FAILED: SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing from .env")
        return 1
    state = secrets.token_urlsafe(32)
    print("Open this link, log in to Schwab, then copy the address of the page it lands on:")
    print(authorize_url(key, state))
    try:
        pasted = input("Paste the https://127.0.0.1/... address here: ")
    except EOFError:
        print("SCHWAB LOGIN FAILED: no address entered")
        return 1
    try:
        code = parse_redirect(pasted, state)
        sq._remember_secret(code)
        body = exchange_code(code)
        tok = sq.token_from_response(body)
        with sq._FileLock():
            sq.write_token(tok)
            proof = sq.scope_proof(tok["access_token"])
            if proof == "refused":
                tok = dict(tok)
                tok["scope_ok"] = True
                tok["scope_proven_at"] = tok["written_at"]
                sq.write_token(tok)
                sq._unlink(sq.KILL_FILE)
                sq._unlink(sq.REVOKED_FILE)   # only a proven login clears a revoke
    except Exception as e:
        print(f"SCHWAB LOGIN FAILED: {sq.redact(f'{type(e).__name__}: {e}')}")
        return 1
    if proof == "refused":
        print("SCHWAB LOGIN OK: token saved (mode 600); scope proof passed — Schwab refused the accounts endpoint.")
        return 0
    if proof == "ALLOWED":
        print("SCHWAB LOGIN FAILED: scope proof FAILED — the accounts endpoint answered 200. Token deleted, "
              "Schwab quotes disabled, Slack paged. Follow the revoke runbook.")
        return 2
    print(f"SCHWAB LOGIN INCOMPLETE: token saved, scope proof inconclusive ({proof}); the bot re-runs it before use.")
    return 1


def revoke() -> int:
    removed = []
    # The revoked marker is written FIRST and outside any lock, so a refresh already in flight in another
    # process sees it and discards its result; then the state is deleted under the lock (waiting out
    # that in-flight cycle) so nothing can write the token back afterwards.
    sq._atomic_write_json(sq.REVOKED_FILE, {"revoked_at": sq.time.time()})
    try:
        with sq._FileLock(deadline_s=3 * sq._lock_deadline_s()):
            for p in (sq.TOKEN_FILE, sq.KILL_FILE, sq.BUDGET_FILE, sq.PAGE_DEDUPE_FILE):
                if p.exists():
                    removed.append(p.name)
                sq._unlink(p)
    except sq._LockTimeout:
        for p in (sq.TOKEN_FILE, sq.KILL_FILE, sq.BUDGET_FILE, sq.PAGE_DEDUPE_FILE):
            if p.exists():
                removed.append(p.name)
            sq._unlink(p)
        print("lock busy — state deleted without the lock; the revoked marker still blocks any write-back")
    try:
        sq._send_slack("Schwab quotes: token REVOKED on the server (token, kill, budget and page state cleared). "
                       "The bot uses its existing quotes until the next login.")
    except Exception as e:
        print(f"Slack post failed: {type(e).__name__}")
    print(f"SCHWAB REVOKE OK: removed {', '.join(removed) or 'nothing (no state present)'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Schwab Market Data login (read-only quotes).")
    ap.add_argument("--revoke", action="store_true", help="delete the token and all Schwab state, post to Slack")
    args = ap.parse_args(argv)
    return revoke() if args.revoke else login()


if __name__ == "__main__":
    sys.exit(main())

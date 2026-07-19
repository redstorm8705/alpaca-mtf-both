# OCI ARM A1.Flex Migration Runbook — MTF Bot
**Scoped:** 2026-07-19 | **BGG:** 2 cold board seats (DevOps/Gene Kim+Peterffy · Reliability/Majors+Taleb) + Gro + GAI — **all 4 aligned** on the core plan.
**Status:** SCOPED — NOT executed. The cutover flip needs Rafael's explicit "go" and is weekend/market-closed-gated.

## Goal
Move the bot from OCI **VM.Standard.E2.1.Micro** (AMD x86_64, 1GB RAM — swap-thrashes → mid-day `deactivating` hangs) to a **free OCI VM.Standard.A1.Flex** (Ampere ARM64, 4 OCPU / 24GB; fall back to 2/12), same Phoenix region, at **$0**. Zero data loss, weekend cutover, E2 kept as hot rollback.

## Verified current state (live, this session)
- Ubuntu 22.04.5 LTS x86_64, Python 3.10.12 (system + venv). ARM 22.04 ships Python 3.10.x → clean rebuild.
- RAM 956MB; ~350MB used + ~200–320MB in swap (the thrash). `mtf-bot` intermittently `deactivating`.
- 3 systemd units (mtf-bot→`main.py --profile paper`, mtf-writer→`live_data_writer.py`, mtf-http→`http.server 18080`), all `User=ubuntu`, WD `/home/ubuntu/mtf-bot`, `EnvironmentFile=.env`, identical paths → units copy **verbatim**.
- nginx: `sites-available/mtf-bot` symlinked; listens :80 + :8080; htpasswd (`/etc/nginx/.htpasswd`); proxy → `127.0.0.1:18080`; static `/var/www/mtf-bot` for `meta_audit_latest.json`.
- ~30 crontab lines, absolute venv python, many via `scripts/cron_tz_wrapper.py` (DST dual-UTC). Includes `auto_deploy.sh`, `autonomous_review.py` (pushes main), watchdogs, audits, report-sync.
- git origin = `github.com/redstorm8705/alpaca-mtf-both.git` with **embedded oauth token**; branch main. Deploy = git-single-channel (`git pull --ff-only` + restart).
- `.env` 17 lines / mode 600. `data/state` 316KB — critical: `open_lots_prior_day.json` (600), `ownership_ledger.json`, `quarterly_holds.json`, `hybrid_state.json`, `kill_switch_state.json`, `catalyst_state.json`, `spy_52w_high.json`. Stray `.DS_Store`.
- 78 pip pkgs (numpy/pandas/scipy/cryptography/lxml/grpcio/cffi/curl_cffi + Alpaca SDK) — all have arm64 wheels.
- Free tier: A1 allotment (≤4 OCPU/24GB) is SEPARATE from the 2× E2.1.Micro allotment → **both boxes can run at once for staging** (but NOT both trading — see cutover).
- **Hardcoded IP `129.153.208.32`** in: repo `scripts/failback_to_mac.sh`, `scripts/service_watchdog.sh`, `scripts/deploy_to_oci.sh`, `docs/mac_failback.md`, `handoff.md`, `logs/tb_audit_log.md`; off-box `~/.claude/skills/session-start/SKILL.md` (9×), `~/.ssh/config`. SSH key `~/.ssh/mtf_bot_oracle`.

---

## THE PLAN (ordered)

### Phase 0 — Prep (anytime, no live impact)
1. On OLD box, freeze a reproducible manifest: `venv/bin/python3 -m pip freeze > requirements.lock.txt` (the venv's own pip → exact 78 pins). Commit it to git so it travels with the clone.
2. Back up off-box: `crontab -l > e2_crontab.bak`; copy the 3 unit files, nginx site, `.htpasswd` to a safe staging path. (These are recreation source, not the live path.)
3. Confirm OCI free-tier A1 headroom (Console → Limits/Quotas): A1 cores available ≥ target.

### Phase 1 — Provision A1 (anytime; capacity-gated)
4. Launch VM.Standard.A1.Flex, **Ubuntu 22.04 aarch64 image** (the #1 footgun — do not pick x86), **4 OCPU / 24GB**, same Phoenix AD, **same VCN/subnet/security-list** as E2 (so :22/:80/:8080 already open), same SSH public key (matches `mtf_bot_oracle`).
5. **"Out of capacity" (429) handling:** try each Phoenix AD; manual retry every ~15–30 min for ~2h; if still blocked, **script it** — OCI CLI `oci compute instance launch` in a loop with **60–90s sleep + jitter**, retrying on `Out of host capacity`, logging each attempt (idempotent; a success stops the loop). **Hard rule: accept 2 OCPU / 12GB the moment 4/24 blocks for >1 business day** — 12GB fully kills the swap root cause, and A1.Flex resizes up later without a rebuild.
6. Do **not** add swap initially — run swapless so validation measures true headroom (add a small cushion later if desired).
7. SSH in; confirm `uname -m` = `aarch64` and `python3 --version` = 3.10.x (flag if patch differs — harmless for wheels).

### Phase 2 — Software bring-up (ARM box)
8. `sudo apt update && sudo apt upgrade -y`; `sudo apt install -y python3-venv python3-dev build-essential libffi-dev libssl-dev libxml2-dev libxslt1-dev zlib1g-dev nginx git curl ca-certificates` (the `-dev` libs are insurance if any wheel falls back to sdist).
9. **venv: REBUILD, never copy the x86 venv.** Copied `.so` files are x86 ELF → fail at *import mid-trade* on aarch64 (worst-case failure). Procedure:
   - `cd /home/ubuntu/mtf-bot && python3 -m venv venv && venv/bin/python3 -m pip install --upgrade pip`
   - `venv/bin/python3 -m pip install --only-binary=:all: -r requirements.lock.txt` — `--only-binary=:all:` **fails loudly if any pkg would sdist-compile**, verifying the "all have arm64 wheels" claim at install time. If it rejects a legit pure-`py3-none-any` pkg, relax the flag for that one named pkg only (with the apt `-dev` libs present) and flag it.
   - **Prove ARM:** `venv/bin/pip debug --verbose | grep -i aarch64` (manylinux_*_aarch64 tags); `file venv/lib/python3.10/site-packages/numpy/core/_multiarray_umath*.so` must say `ARM aarch64`. If `file` says x86-64 anywhere → STOP.

### Phase 3 — Code + secrets + state (staging copy)
10. **Code = git clone** (not file-copy): clone origin into `/home/ubuntu/mtf-bot`, `git checkout main`. Clean tree by construction — no venv, `.pyc`, `.DS_Store`.
    - Git token: replicate exactly what `auto_deploy.sh` needs (it pulls non-interactively). Move `.git/config`/URL over SSH only — never into shell history/logs. **Flag to Rafael:** consider rotating the GitHub token post-migration (it will have lived on two boxes) — his call, not a blocker.
11. `.env`: transfer over SSH; `chmod 600`, `chown ubuntu:ubuntu`; `sha256sum` must match old↔new. Never echo contents.
12. **data/state STAGING copy (throwaway):** copy once now to verify paths/perms/format parse on ARM. This copy is NOT authoritative — it goes stale (the real one is Phase 5). Exclude `.DS_Store`/`*.pyc`; preserve modes (`open_lots_prior_day.json` stays 600).
13. Systemd units, nginx site, `.htpasswd`, `/var/www/mtf-bot`: recreate now (`daemon-reload`, `nginx -t` must pass) but **do NOT enable/start services and do NOT install the crontab** — those wait for cutover.

### Phase 4 — Pre-flight validation (before the flip; new box NOT trading)
14. Read-only dry-run on ARM: run a scan/audit-only script (NOT `mtf-bot.service` — that would place orders on the shared paper account) to confirm imports + Alpaca/GROQ/GEMINI/Slack auth + data fetch work on ARM. Observe RAM footprint (should be far under 12/24GB, zero swap).
15. Pre-validate the non-trading gates so only trading + state remain for the weekend: dashboard reachable via htpasswd (temp IP); a git-deploy round-trip dry-run; confirm box UTC/TZ matches E2 so `cron_tz_wrapper.py` fires correctly.

### Phase 5 — CUTOVER (⚠️ weekend/market-CLOSED only; needs Rafael's "go")
**Single-writer invariant: at no instant may both boxes run trading services, crontabs, OR auto_deploy.** Execute as one tight sequence:
16. Confirm market closed (Alpaca clock), no in-flight orders.
17. OLD box: `systemctl stop mtf-bot mtf-writer` then `systemctl disable` them (writers stop first — they mutate state). **Graceful stop (SIGTERM) — wait for confirmed process exit; never `kill -9`** (a SIGKILL mid tmp→replace can strand a `.tmp`).
18. OLD box: `crontab -l > e2_crontab.bak` then `crontab -r` (kills auto_deploy, autonomous_review, watchdogs — all-or-nothing, no "just the safe crons").
19. **AUTHORITATIVE state snapshot — taken NOW, after writers stopped + crons cleared** (the zero-data-loss crux): `sha256sum data/state/*.json > state.sha` on OLD; copy `data/state/` (exclude `.DS_Store`) old→new; verify every sha matches on NEW. Any mismatch → STOP, re-copy.
20. **IP:** reassign `129.153.208.32` to the NEW box's VNIC (see IP DECISION below — verify feasibility first).
21. NEW box: `systemctl enable --now mtf-bot mtf-writer mtf-http`; `systemctl reload nginx`.
22. NEW box: `crontab e2_crontab.bak` — now, and only now, crons run, on exactly one box.
23. Confirm NEW `mtf-bot` scanning cleanly (journalctl) and NO duplicate orders appeared in the window.

### Phase 6 — Validation gates (OBSERVE, not "is-active")
Nothing is "done" until ALL are observed under real conditions:
- **RAM under real RTH load** (numpy/pandas/scipy loaded, bars fetched): `Swap: used` = 0, >50% headroom. (The actual fix — measure under load, not idle.)
- **≥2 consecutive full 5-min scan cycles** during RTH with `mtf-bot` staying `active` (falsifies the hang).
- **GTC stops present at the BROKER** (Alpaca orders, open/stop/gtc) for every open lot, **no duplicates**.
- **git-deploy round-trip:** trivial commit → `auto_deploy.sh` on NEW does `git pull --ff-only` + `DEPLOY_OK` + restart; OLD box does NOT also deploy.
- **cron_tz_wrapper.py fires at the right ET time** on the new host (observe one wrapper-gated cron land its output).
- **Dashboard** :80/:8080 → htpasswd → proxy → `meta_audit_latest.json`, authenticated 200 from off-box.

### Phase 7 — Rollback (keep the cheap optionality — E2 is free)
- Keep E2 **provisioned + stopped** (services stopped, crons cleared, instance NOT terminated) for **≥5 clean RTH sessions**. Terminating E2 is irreversible → needs Rafael's explicit go.
- **Emergency (A1 misbehaves mid-RTH):** step 1 = `systemctl stop mtf-bot mtf-writer` on A1 (account is safe — GTC stops hold at broker). Then flip back market-closed: snapshot state A1→E2 (checksum-verified; use pre-flip snapshot if A1 state is suspect), reassign IP back to E2, `crontab e2_crontab.bak` + `systemctl enable --now` on E2, verify gates.

---

## IP DECISION — recommendation: RESERVE + REASSIGN `129.153.208.32` to the A1 box
**Consensus (both board seats + Gro + GAI):** reserve-and-reassign beats accept-new-IP, because the alternative is editing 8 files / ~15+ occurrences of a hardcoded IP across repo + the session-start skill + `~/.ssh/config` under cutover pressure — one missed reference = a silently broken deploy found mid-incident.

**⚠️ ONE THING TO VERIFY IN THE OCI CONSOLE FIRST (divergence flagged):** The current IP is **ephemeral**. GAI asserts the E2 must be **powered off** to release/reassign it (which would weaken rollback); the DevOps board seat says convert the ephemeral IP → **reserved**, then unassign from E2's VNIC and assign to A1 (no poweroff). **This is a real OCI-mechanics question I am NOT certain of — verify in the console before committing to the IP strategy.** Decision tree:
- If the existing IP can be converted to reserved + moved off a *running* E2 → do that (zero churn, rollback intact).
- If moving it requires powering off E2 → either (a) accept that (E2 gets a fresh ephemeral IP on next boot — script the rollback-IP path), or (b) fall back to **accept a new IP on A1 + update the 8 hardcoded references** (bounded, enumerated above).

Either way: the IP move happens **during the weekend flip, not at provisioning** (staging uses the A1's temp IP so the two boxes stay isolated).

---

## RANKED TOP RISKS
1. **x86 venv/`.pyc` leak onto ARM → import crash mid-trade.** Guard: git clone + venv rebuild + `--only-binary=:all:` + `file …so` = aarch64.
2. **Double-writer / double-cron collision** (both boxes on one paper account: duplicate entries, duplicate/naked GTC stops, split-brain kill switch, autonomous_review push race). Guard: strict single-writer flip — stop+disable OLD trading & `crontab -r` BEFORE any NEW writer/cron starts; weekend-only.
3. **Stale / lost state snapshot.** Guard: authoritative snapshot ONLY after OLD writers stopped + crons cleared; graceful stop + wait for exit (no `kill -9`); sha256 witness per file.
4. **A1 capacity blocks provisioning** (prolongs the hangs). Guard: scripted retry + "accept 2/12 after >1 business day."
5. **cron_tz_wrapper fires at wrong ET on new host** (missed overnight-stop/deploy). Guard: confirm host UTC/TZ matches E2; observe one wrapper-gated cron fire before declaring done.
6. **Secret mishandling** (.env, git token, .htpasswd across two hosts). Guard: SSH-only transfer, chmod 600, sha256-verify, never echo; optional token rotation post-migration.

## Steps needing Rafael's explicit "go" (irreversible)
- The **weekend cutover flip** (Phase 5 — account/cron/deploy ownership changes hands).
- **Reserved-IP reassign** off E2 (Phase 5.20 — after console feasibility check).
- **Terminating the E2 box** (only after ≥5 clean sessions).

## Effort
~1 focused staging session (Phases 0–4, anytime) + a short **weekend** cutover window (Phases 5–6) + a 5-session E2 hot-standby watch before terminating E2.

---

## ✅ PROVISIONED — 2026-07-19 (Phase 1 DONE)
New A1 box launched via OCI CLI (browser console iframe kept freezing; CLI retry loop caught capacity on attempt 1, AD-1). Rafael approved + gave "go".
- **New box public IP:** `137.131.51.250` (ephemeral; the OLD box `129.153.208.32` is untouched)
- **Instance OCID:** `ocid1.instance.oc1.phx.anyhqljri3ebloycjkowomvdyewrmzqwnaqthix2jwjcmbjzjgwx2vfmixzq`
- Verified live: aarch64 · Ubuntu 22.04.5 LTS · Python 3.10.12 · 2 OCPU / **11.9 GB RAM, 0 swap** · AD-1
- Free-tier note: Oracle dropped the Always-Free A1 ceiling to **2 OCPU / 12 GB** (was 4/24) — this box is at the max.
- **OCI CLI now configured on the Mac** (`~/.oci/config`, API key `oci_api_key.pem`; fingerprint `aa:e0:dc:…:d3`). Retry loop: `scratchpad/a1_retry_loop.sh`. Reusable for the weekend IP move.
- Launch facts (for rebuild/reference): compartment=`redstorm87(root)` tenancy=`…ntrg7eo7dpnq`; subnet=`ocid1.subnet.oc1.phx.aaaaaaaadx24xvas4zsf2xg4lzuv7s57homx7ip6d7geag6stnc74lqnmhka` (10.0.0.0/24); image=`Canonical-Ubuntu-22.04-aarch64-2026.04.30-1` (`ocid1.image.oc1.phx.aaaaaaaaufk34i3h6pc66mifse3yqpstkdbbohmlgpry7gu2ew6fq7faf5pa`).
- **⏩ NEXT: Phase 2** — SSH in (`ssh -i ~/.ssh/mtf_bot_oracle ubuntu@137.131.51.250`), apt deps, rebuild venv from `requirements.lock` (`--only-binary=:all:`), git clone, staging state copy, recreate services/crons/nginx DORMANT. All safe/anytime (box does NOT trade until the weekend single-writer flip).

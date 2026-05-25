# Infrastructure Proposal: Dual-Cloud Architecture (OCI + GCP)
**Date:** 2026-04-30 | Source: Claude in Chrome recommendation

---

## Problem Summary

- **Current:** Oracle Cloud Free Tier, tenancy `redstorm87`, region US West (Phoenix / PHX)
- **Instance:** ARM Ampere A1, 1GB RAM
- **Symptom:** Bot RSS grows ~1.2MB/min during RTH, hits 1GB ceiling → OOM → watchdog restarts
- **Root cause:** main.py memory growth (unbounded `_atr_cache`, zombie tracker entries)
- **Swap:** 4GB swapfile added 2026-04-29 — masks problem, does not solve it

## What Has Been Ruled Out

| Option | Status | Reason |
|--------|--------|--------|
| OCI second free account (new Gmail) | REJECTED | Oracle detected similar user |
| OCI multi-region provisioning | HARD BLOCKED | Free Tier hard-capped at 1 region |
| OCI service limit increase | BLOCKED | Requires paid account |
| OCI A1.Flex (4 OCPU / 24GB) via same account | CAPACITY LOCKED | Phoenix region out of A1 capacity |
| RAM swap | NOT A SOLUTION | Does not reduce RSS pressure |

## Proposed Solution: Dual-Cloud Architecture

### Components
- **Oracle Cloud (existing):** 1GB RAM VM, Phoenix — primary bot workload (main.py, live_data_writer.py)
- **GCP Free Tier (new):** e2-micro, 1GB RAM, us-west1 or us-east1, 30GB disk — offload workers
- **Connection:** WireGuard VPN tunnel between both VMs → private network

### Workload Distribution
| Service | Location | Rationale |
|---------|----------|-----------|
| `main.py` (trading bot) | OCI | Stays on OCI — all credentials + state here |
| `live_data_writer.py` | OCI | Tightly coupled to main.py state |
| `nightly_audit.py` / `midday_audit.py` | GCP | Offload Gemini API calls — reduces OCI RAM pressure |
| `run_movers.py` / `options_scanner.py` | GCP | Pre-market scan workloads |
| Redis (session cache, if added) | GCP | Stateless relative to main bot |
| Dashboard generation (`generate_dashboard.py`) | GCP | Not time-critical during RTH |

### Key Constraint
RAM cannot be pooled across VMs. Each VM limited to its own 1GB. Goal is workload distribution, not unified memory. A single process is still capped at ~1GB per host.

### Effective RAM After Split
- OCI: ~1GB (main.py only — target <400MB RSS after decomposition)
- GCP: ~1GB (audit/scan workloads)
- Total system: 2GB across 2 VMs

## WireGuard Setup (high-level)
1. Install WireGuard on both VMs
2. Generate keypairs on each
3. Configure `/etc/wireguard/wg0.conf` with peer public keys and allowed IPs
4. Assign private IPs: OCI → `10.0.0.1`, GCP → `10.0.0.2`
5. Enable `wg-quick@wg0` on both
6. Test connectivity: `ping 10.0.0.2` from OCI

## Next Steps Required
1. User creates GCP account and provisions e2-micro VM (us-west1, 30GB disk)
2. Set up WireGuard on both VMs
3. Migrate scan workloads to GCP VM
4. Monitor OCI RAM after offload — target <300MB RSS during RTH

## Alternative: OCI PAYG Upgrade
- Cost: ~$0.076/hr for A1.Flex (4 OCPU + 24GB RAM)
- Monthly: ~$55/month
- Pros: Single-VM, no WireGuard complexity, 24GB eliminates RAM issue permanently
- Cons: Costs money; requires Oracle billing setup
- Verdict: Cleaner long-term fix if monthly cost is acceptable

## Status
- Dual-cloud: PROPOSED — pending GCP account creation by user
- OCI PAYG: DEFERRED — user preference for free tier
- Code-level RAM fix: PARTIAL — `_atr_cache` pruning deployed 2026-04-30 (Patch 3)
- Full RAM fix: Pending main.py decomposition (reduces per-process RSS)

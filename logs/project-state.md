# alpaca-mtf-bot — project state (2026-09-27 18:35 PT)
Bot: OCI mtf-bot 137.131.51.250, paper account (~$2.5K equity, on margin). Goal $2.5K -> $25K; data collection first.
Shipped 2026-09-27: E0 closed-bar gate (#424), no-test-Slack guard (#426), point-in-time universe (#427), lab bars (#431), full QHM report in Slack (#433).
In flight (Rafael approved): QHM report plain-English takeaways (code in logs/wip patch, needs round-2 reviews + preship); QHM top-3 queued buys at support (design logs/design_records/qhm_top3_action_2026-09-27.md, risk-path, not built).
Key facts: Alpaca rejects limit BUY while a sell STOP rests (wash-trade rule); QHM ladder never reaches chunk 2; Claude cannot place trades (Anthropic rule) — build bot-side mechanisms instead.
GEV: Rafael sells manually Monday. Next: ship report patch, then build top-3 buys, then operator exit command.

#!/usr/bin/env python3
"""
generate_migration_pdf.py
Cloud Migration Audit & Status Report -- MTF Bot
Generated: 2026-04-27
For submission to AI agents for review.
"""
from fpdf import FPDF
from datetime import datetime

class PDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 11)
        self.set_fill_color(30, 30, 60)
        self.set_text_color(255, 255, 255)
        self.cell(0, 10, "MTF Confluence Bot -- Cloud Migration Audit Report", fill=True, ln=True, align="C")
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "", 8)
        self.cell(0, 5, f"Generated: 2026-04-27 | Server: 129.153.208.32 | OCI Phoenix AD-2 VM.Standard.E2.1.Micro", ln=True, align="C")
        self.ln(3)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"MTF Bot Cloud Migration Report | Page {self.page_no()} | CONFIDENTIAL -- paper trading only", align="C")

    def section_title(self, title, color=(30, 30, 100)):
        self.set_fill_color(*color)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 10)
        self.cell(0, 8, f"  {title}", fill=True, ln=True)
        self.set_text_color(0, 0, 0)
        self.ln(1)

    def subsection(self, title):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(30, 30, 100)
        self.cell(0, 6, title, ln=True)
        self.set_text_color(0, 0, 0)

    def body(self, text, indent=4):
        self.set_font("Helvetica", "", 8.5)
        self.set_x(self.get_x() + indent)
        self.multi_cell(0, 5, text)
        self.ln(1)

    def bullet(self, items, indent=8, color=(0,0,0)):
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*color)
        for item in items:
            self.set_x(indent)
            self.multi_cell(0, 5, f"  *  {item}")
        self.set_text_color(0, 0, 0)
        self.ln(1)

    def status_row(self, label, value, status="ok"):
        colors = {"ok": (0, 140, 0), "warn": (200, 120, 0), "crit": (180, 0, 0), "info": (30, 30, 120)}
        self.set_font("Helvetica", "B", 8.5)
        self.cell(70, 5, f"  {label}", border="B")
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*colors.get(status, (0,0,0)))
        self.cell(0, 5, value, border="B", ln=True)
        self.set_text_color(0, 0, 0)

    def two_col_table(self, rows, h1="Component", h2="Status/Notes"):
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(200, 210, 240)
        self.cell(90, 6, h1, fill=True, border=1)
        self.cell(0, 6, h2, fill=True, border=1, ln=True)
        self.set_font("Helvetica", "", 8)
        for i, (col1, col2, st) in enumerate(rows):
            fill_colors = {"ok": (240,255,240), "warn": (255,250,220), "crit": (255,235,235), "info": (240,240,255)}
            self.set_fill_color(*fill_colors.get(st, (255,255,255)))
            self.cell(90, 5, f"  {col1}", border=1, fill=True)
            self.cell(0, 5, f"  {col2}", border=1, fill=True, ln=True)
        self.ln(3)


pdf = PDF()
pdf.set_auto_page_break(auto=True, margin=15)
pdf.add_page()

# ── SECTION 1: EXECUTIVE SUMMARY ─────────────────────────────────────────────
pdf.section_title("1. EXECUTIVE SUMMARY -- Cloud Migration Status (2026-04-27)")
pdf.body(
    "On April 27, 2026, the MTF Confluence Bot was successfully migrated from a Mac-local process "
    "to an Oracle Cloud Infrastructure (OCI) Always Free compute instance. The bot is now fully "
    "autonomous -- it does not require the Mac to be on, connected, or awake. All trading decisions, "
    "position management, and scheduled analysis now run on the cloud server."
)
pdf.status_row("Cloud Server", "OCI Phoenix AD-2 | 129.153.208.32 | VM.Standard.E2.1.Micro", "ok")
pdf.status_row("OS / Python", "Ubuntu 22.04 LTS | Python 3.10.12 | venv isolated", "ok")
pdf.status_row("Bot Status", "main.py RUNNING (systemd, autostart) | PID active", "ok")
pdf.status_row("Writer Status", "live_data_writer.py RUNNING (systemd, autostart)", "ok")
pdf.status_row("Dashboard Access", "http://129.153.208.32:8080/dashboard.html (public HTTP)", "ok")
pdf.status_row("Mac Bot Processes", "KILLED -- all 5 Mac PIDs terminated 2026-04-27", "ok")
pdf.status_row("Mac Crontab", "DISABLED -- all jobs migrated to OCI crontab", "ok")
pdf.status_row("Alpaca API", "Paper account connected | paper=True hardcoded", "ok")
pdf.status_row("RAM Usage at Start", "92 MB / 1 GB limit | Headroom: ~900 MB", "warn")
pdf.status_row("A1.Flex (12 GB)", "NOT provisioned -- Phoenix capacity unavailable. Stack saved, NOT auto-retrying.", "warn")
pdf.ln(2)

# ── SECTION 2: ARCHITECTURE -- LOCAL vs CLOUD ─────────────────────────────────
pdf.section_title("2. ARCHITECTURE AUDIT -- Local vs Cloud Component Map")

pdf.subsection("2A. CLOUD (OCI 129.153.208.32) -- Running 24/7 independent of Mac:")
pdf.two_col_table([
    ("main.py --profile paper",        "CLOUD | systemd mtf-bot.service | autostart + auto-restart", "ok"),
    ("live_data_writer.py",            "CLOUD | systemd mtf-writer.service | autostart + auto-restart", "ok"),
    ("dashboard.html generation",      "CLOUD | generated by main.py every 5-min cycle", "ok"),
    ("scan_results.html generation",   "CLOUD | generated by main.py every 5-min cycle", "ok"),
    ("weekly_review.html generation",  "CLOUD | spawned by main.py post-RTH (1:05 PM PT)", "ok"),
    ("Alpaca order execution",         "CLOUD | broker.py -> paper-api.alpaca.markets", "ok"),
    ("Slack alerts",                   "CLOUD | alerts.py -> webhook in .env on OCI", "ok"),
    ("run_market_top.py (cron)",       "CLOUD | cron 5:30 AM PT Mon-Fri | output: logs/market_top_latest.json", "ok"),
    ("run_macro_regime.py (cron)",     "CLOUD | cron 5 PM PT Sunday | output: logs/macro_regime_latest.json", "ok"),
    ("midday_audit.py (cron)",         "CLOUD | cron 1:15 PM PT Mon-Fri | Gemini review -> Slack", "ok"),
    ("nightly_audit.py (cron)",        "CLOUD | cron 1:30 PM PT Mon-Fri | Gemini code review -> Slack", "ok"),
    ("options_scanner.py (cron)",      "CLOUD | cron every 15 min 9:30-4 PM PT | generates options.html", "ok"),
    ("run_movers.py (cron)",           "CLOUD | cron 5:35 AM PT Mon-Fri | pre-market mover scan -> HTML", "ok"),
    ("HTTP server port 8080",          "CLOUD | systemd mtf-http.service | serves all HTML dashboards", "ok"),
], "Process / Script", "Location | Schedule | Output")

pdf.subsection("2B. LOCAL (Mac) -- Can stay local, does NOT affect live trading:")
pdf.two_col_table([
    ("Claude Code / Claude sessions",  "LOCAL | AI dev sessions, code review, board audits", "info"),
    ("backtest_12pt.py",               "LOCAL | RTH-blocked, runs AH/weekend, no cloud dependency", "info"),
    ("run_backtest.sh",                "LOCAL | cron disabled, run manually on Mac when needed", "info"),
    ("Master Brain (NotebookLM)",      "GOOGLE CLOUD | separate from OCI, no bot dependency", "info"),
    ("preflight_simulation.py",        "LOCAL | dev/testing only", "info"),
    ("HTML file viewing (browser)",    "ANY DEVICE | http://129.153.208.32:8080/ from any browser", "ok"),
], "Process / Script", "Location | Notes")

# ── SECTION 3: CRITICAL FINDINGS ─────────────────────────────────────────────
pdf.add_page()
pdf.section_title("3. BOARD AUDIT -- CRITICAL FINDINGS & RISKS", color=(150, 30, 30))

findings = [
    ("CRITICAL-1", "market_top_latest.json MISSING on OCI at launch",
     "main.py loads market_top_latest.json at startup for position sizing (Layer 5). File did not transfer "
     "with rsync (empty logs/market_top/ dir on Mac too). Bot defaulted to zone_tier=0 (Green) -- most "
     "permissive sizing. run_market_top.py has been triggered manually on OCI to fix. Will run via cron "
     "5:30 AM PT daily going forward.",
     "crit"),
    ("CRITICAL-2", "macro_regime_latest.json MISSING on OCI at launch",
     "main.py loads macro_regime_latest.json at startup for MIN_SCORE floor (Layer 6). File missing. "
     "Bot defaulted to regime_tier=0 (Neutral) -- base MIN_SCORE=9 not adjusted. run_macro_regime.py "
     "scheduled for Sunday 5 PM PT cron. Current default is acceptable but stale.",
     "warn"),
    ("WARN-1", "1 GB RAM hard ceiling on VM.Standard.E2.1.Micro",
     "Bot started at 92 MB RSS. Normal RTH operation with 3-5 open positions stays under 250 MB. "
     "However: weekly_review.py spawned as subprocess adds ~200 MB. Gemini audit scripts add ~150 MB. "
     "Simultaneous execution of main.py + options_scanner + midday_audit could reach 700+ MB. "
     "OOM kill risk during peak load. MITIGATION: cron jobs are scheduled post-RTH (1:15-1:30 PM PT) "
     "when main.py is in its low-activity AH cycle. Monitor with: ssh to server and run 'free -h'.",
     "warn"),
    ("WARN-2", "A1.Flex (ARM 2 OCPU / 12 GB) NOT auto-retrying",
     "Phoenix capacity exhausted across all 3 ADs. Ashburn blocked by Free Tier 1-region limit. "
     "Terraform stack saved (OCID: ocid1.ormstack.oc1.phx.amaaaaaai3ebloyaw73yf3ibme5ldugobra53vwttklhritd2zy65rl5eoeq) "
     "but requires MANUAL Apply clicks in OCI console. Capacity typically opens at off-peak hours "
     "(2-5 AM PT). User must manually click Apply -> retry until provisioned. No auto-retry implemented.",
     "warn"),
    ("WARN-3", "No dead-man's switch / health monitor",
     "If both mtf-bot.service and mtf-writer.service crash and systemd fails to restart them, "
     "there is no external alerting. Bot could be down for hours without notification. "
     "RECOMMENDATION: Add a cron watchdog that checks service status every 5 min and posts to Slack if down.",
     "warn"),
    ("WARN-4", "Dashboard on public HTTP (port 8080, no auth)",
     "dashboard.html, weekly_review.html, scan_results.html, options.html are served publicly "
     "on http://129.153.208.32:8080/. This is paper account data. No passwords, no HTTPS. "
     "Acceptable for paper trading. Before live migration: add nginx + HTTP basic auth.",
     "info"),
    ("INFO-1", "PANW / COIN / TOST orphaned shorts adopted at cloud launch",
     "3 positions were open in Alpaca paper account when Mac bot was killed. Cloud bot adopted them "
     "correctly at 02:31 UTC with ATR-based stops. PANW short 3sh stop $180.25, COIN short 3sh "
     "stop $209.69, TOST short 21sh stop $29.68. All GTC stops are live.",
     "info"),
]

for fid, title, detail, sev in findings:
    sev_colors = {"crit": (180,0,0), "warn": (160,100,0), "info": (30,30,120)}
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*sev_colors.get(sev, (0,0,0)))
    pdf.cell(0, 6, f"  [{fid}] {title}", ln=True)
    pdf.set_text_color(0,0,0)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_x(12)
    pdf.multi_cell(0, 4.5, detail)
    pdf.ln(2)

# ── SECTION 4: PROACTIVE ISSUES -- TOMORROW (2026-04-28) ──────────────────────
pdf.section_title("4. PROACTIVE RISK FORECAST -- Tue 2026-04-28 (Tomorrow)")

tomorrow_risks = [
    ("5:30 AM PT", "MEDIUM", "run_market_top.py first cloud run -- verify logs/market_top_latest.json exists before 9:30 AM. "
     "If it fails, bot enters RTH with zone_tier=0 (Green/permissive). Check: "
     "ssh server 'cat logs/market_top_cron.log | tail -20'"),
    ("5:35 AM PT", "LOW", "run_movers.py first cloud run -- pre-market mover HTML. If FMP API calls hit 250/day limit, "
     "movers scan degrades gracefully. Check: ssh server 'cat logs/movers_cron.log | tail -10'"),
    ("9:30 AM PT", "HIGH", "RTH OPEN with PANW/COIN/TOST overnight shorts. All 3 have GTC stops but market open "
     "gap risk is real. PANW earnings on 5/20, COIN earnings on 5/7 -- thesis could shift. "
     "Bot will score each position at open and may exit if score drops below threshold."),
    ("All Day", "MEDIUM", "GDP Q1 2026 Advance release -- flagged in calendar as HIGH_RISK (50% size event). "
     "Bot will auto-throttle sizing. Watch for VIX spike which triggers ATR stop widening."),
    ("1:15 PM PT", "LOW", "First cloud midday_audit.py run. Gemini API key in .env on OCI. "
     "Verify Slack receives audit post. If silent: check logs/midday_audit_cron.log"),
    ("All Day", "MEDIUM", "RAM headroom watch. If options_scanner.py overlaps with main.py heavy cycle, "
     "monitor with: ssh server 'while true; do free -h; sleep 30; done'"),
]

pdf.set_font("Helvetica", "B", 8)
pdf.set_fill_color(200, 210, 240)
pdf.cell(30, 6, "Time (PT)", border=1, fill=True)
pdf.cell(22, 6, "Risk Level", border=1, fill=True)
pdf.cell(0, 6, "Issue / Action Required", border=1, fill=True, ln=True)
pdf.set_font("Helvetica", "", 8)
risk_colors = {"HIGH": (255,235,235), "MEDIUM": (255,250,220), "LOW": (240,255,240)}
for time, risk, desc in tomorrow_risks:
    h = max(6, int(len(desc) / 90) * 5 + 5)
    pdf.set_fill_color(*risk_colors.get(risk, (255,255,255)))
    y_start = pdf.get_y()
    pdf.cell(30, h, f"  {time}", border=1, fill=True)
    pdf.cell(22, h, f"  {risk}", border=1, fill=True)
    pdf.multi_cell(0, h, f"  {desc}", border=1, fill=True)
    pdf.ln(1)

# ── SECTION 5: RECOMMENDATIONS ────────────────────────────────────────────────
pdf.add_page()
pdf.section_title("5. BOARD RECOMMENDATIONS -- Prioritized Action List")

recs = [
    ("P0 -- IMMEDIATE", [
        "Verify market_top_latest.json created on OCI before tomorrow open (5:30 AM PT)",
        "Monitor PANW/COIN/TOST short positions at open -- GTC stops in place but gap risk real",
        "Test dashboard access: open http://129.153.208.32:8080/dashboard.html in browser NOW",
        "Confirm Slack is receiving alerts from OCI (not Mac) -- check channel for next bot message",
    ]),
    ("P1 -- THIS WEEK", [
        "Add systemd watchdog cron: every 5 min check mtf-bot + mtf-writer status, Slack alert if down",
        "Manually retry A1.Flex Terraform stack (try 2-4 AM PT when capacity opens) -- OCI console -> Stacks",
        "Add run_macro_regime.py immediate run before Sunday cron (regime data stale)",
        "Test all 4 HTML pages accessible: dashboard, weekly_review, scan_results, options",
        "Verify Gemini midday audit posts to Slack at 1:15 PM PT Tue",
    ]),
    ("P2 -- BEFORE LIVE TRADING", [
        "Migrate from VM.Standard.E2.1.Micro (1 GB) to VM.Standard.A1.Flex (2 OCPU / 12 GB)",
        "Add nginx + HTTP basic auth before exposing any live account data on port 8080",
        "Consider Tailscale VPN for private dashboard access from any device without public exposure",
        "Set up OCI monitoring alerts (CPU > 90%, RAM > 85%) -> Slack",
        "Implement automated rsync from Mac to OCI on code changes (vs manual deploy)",
        "Fix GTC-RACE bug in execution/broker.py (open item from 2026-04-22 session)",
    ]),
]

for priority, items in recs:
    pdf.subsection(priority)
    pdf.bullet(items)

# ── SECTION 6: SERVICE STATUS SNAPSHOT ───────────────────────────────────────
pdf.section_title("6. SERVICE STATUS SNAPSHOT -- 2026-04-27 02:31 UTC")
pdf.two_col_table([
    ("mtf-bot.service",    "active (running) | main.py --profile paper | PID 3997 | 92 MB RAM", "ok"),
    ("mtf-writer.service", "active (running) | live_data_writer.py | PID 3941 | 6.9 MB RAM", "ok"),
    ("mtf-http.service",   "active (running) | python3 -m http.server 8080 | port open", "ok"),
    ("OCI Security List",  "Port 8080 TCP ingress 0.0.0.0/0 | ADDED 2026-04-27", "ok"),
    ("VCN",                "vcn-20260427-0829 | subnet-20260427-0829 (regional) | 10.0.0.0/24", "ok"),
    ("Public IP",          "129.153.208.32 | AD-2 PHX | VM.Standard.E2.1.Micro Always Free", "ok"),
    ("SSH Key",            "~/.ssh/mtf_bot_oracle (ed25519) | ubuntu@129.153.208.32", "ok"),
    ("OCI Crontab",        "6 jobs: market_top, macro_regime, midday_audit, nightly_audit, options, movers", "ok"),
    ("Mac Crontab",        "DISABLED -- all 6 jobs commented out with DISABLED-OCI-MIGRATION tag", "ok"),
    ("Mac Bot PIDs",       "ALL KILLED -- PIDs 25515, 5030, 61392, 61393, 89122 terminated", "ok"),
], "Service / Resource", "Status")

# ── SECTION 7: QUICK REFERENCE ────────────────────────────────────────────────
pdf.section_title("7. QUICK REFERENCE -- Commands & URLs")
pdf.set_font("Courier", "", 8)
cmds = [
    ("SSH to server",             "ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32"),
    ("View live bot log",         "ssh server 'tail -f /home/ubuntu/mtf-bot/logs/mtf_bot_stdout.log'"),
    ("Check service status",      "ssh server 'sudo systemctl status mtf-bot mtf-writer mtf-http'"),
    ("Restart bot",               "ssh server 'sudo systemctl restart mtf-bot'"),
    ("Dashboard HTML",            "http://129.153.208.32:8080/dashboard.html"),
    ("Weekly Review HTML",        "http://129.153.208.32:8080/weekly_review.html"),
    ("Scan Results HTML",         "http://129.153.208.32:8080/scan_results.html"),
    ("Options Scanner HTML",      "http://129.153.208.32:8080/options.html"),
    ("Check RAM",                 "ssh server 'free -h && ps aux --sort=-%mem | head -8'"),
    ("A1.Flex Stack (OCI)",       "cloud.oracle.com -> Resource Manager -> Stacks -> mtf-bot -> Apply"),
    ("Master Brain",              "NotebookLM ID: 0203f312-f285-4f20-8b8d-ca6fde65acf7"),
]
for label, cmd in cmds:
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(55, 5, f"  {label}:", border="B")
    pdf.set_font("Courier", "", 7.5)
    pdf.cell(0, 5, cmd, border="B", ln=True)
pdf.ln(3)

pdf.set_font("Helvetica", "I", 7.5)
pdf.set_text_color(100, 100, 100)
pdf.multi_cell(0, 4,
    "DISCLAIMER: This is a PAPER TRADING bot. paper=True is hardcoded in execution/broker.py. "
    "No real money is at risk. All Alpaca transactions go to the paper account only. "
    "This report is generated for internal AI agent review and developer reference.")

out = "/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/logs/cloud_migration_audit_2026-04-27.pdf"
pdf.output(out)
print(f"PDF saved: {out}")

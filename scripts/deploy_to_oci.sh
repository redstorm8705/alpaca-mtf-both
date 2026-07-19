#!/bin/bash
# deploy_to_oci.sh — Sync Mac code changes to OCI and restart services
#
# SAFE: excludes .env (OCI has its own), logs/, venv/, __pycache__
# Restarts only the services that depend on changed code.
#
# Usage:
#   bash scripts/deploy_to_oci.sh           # sync + restart all 3 services
#   bash scripts/deploy_to_oci.sh --no-restart  # sync only, no restart

set -e

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OCI_USER="ubuntu"
OCI_IP="137.131.51.250"
OCI_PATH="/home/ubuntu/mtf-bot"
SSH_KEY="$HOME/.ssh/mtf_bot_oracle"
RESTART=true

if [ "$1" = "--no-restart" ]; then
    RESTART=false
fi

echo "=== MTF Bot Deploy to OCI ==="
echo "Source: $BOT_DIR"
echo "Target: $OCI_USER@$OCI_IP:$OCI_PATH"
echo ""

# ── Sync ─────────────────────────────────────────────────────────────────────
echo "Syncing files (excluding .env, logs/, venv/, __pycache__)..."
rsync -az --checksum \
    --exclude '.env' \
    --exclude 'logs/' \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'data/cache/' \
    --exclude 'data/state/' \
    --exclude '.DS_Store' \
    -e "ssh -i $SSH_KEY" \
    --itemize-changes \
    "$BOT_DIR/" \
    "$OCI_USER@$OCI_IP:$OCI_PATH/" \
    | grep -E '^[<>ch]' | head -30

echo ""
echo "Sync complete."

# ── Restart ───────────────────────────────────────────────────────────────────
if [ "$RESTART" = true ]; then
    echo "Restarting OCI services..."
    ssh -i "$SSH_KEY" "$OCI_USER@$OCI_IP" "
        sudo systemctl restart mtf-bot mtf-writer mtf-http
        sleep 3
        echo 'Service status:'
        systemctl is-active mtf-bot mtf-writer mtf-http
    "
    echo ""
    echo "=== Deploy complete. All 3 services restarted. ==="
else
    echo "=== Sync complete. Services NOT restarted (--no-restart). ==="
    echo "To restart manually: ssh -i $SSH_KEY $OCI_USER@$OCI_IP 'sudo systemctl restart mtf-bot mtf-writer mtf-http'"
fi

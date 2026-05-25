#!/bin/bash
# Deploy a single file to OCI mtf-bot instance.
# Usage: ./deploy.sh <file_relative_to_project_root>
# Example: ./deploy.sh config.py
# Example: ./deploy.sh execution/kelly.py
set -e

FILE="$1"
if [ -z "$FILE" ]; then
  echo "Usage: $0 <file>"
  exit 1
fi

DEST_DIR="/home/ubuntu/mtf-bot/$(dirname "$FILE")"
rsync -avz "$FILE" "mtf-bot:$DEST_DIR/"
echo "Deployed $FILE → OCI:$DEST_DIR/"

#!/bin/bash
# install_managed_gates.sh — OWNER-ONLY install of the bot's Claude Code gates as system-level managed hooks.
# Run by the owner from the repo root:   sudo bash scripts/install_managed_gates.sh
# Design record: logs/design_records/owner_approval_gates_2026-10-17.md (Approval-ID GATES-1017).
#
# What it does:
#   1. copies scripts/managed_gates/run_gate.py to /Library/Application Support/ClaudeCode/mtf-gates/ (root-owned);
#   2. writes manifest.json there with the sha256 of every .claude/preship/*.py and *.json (incl. the required
#      questions) in THIS checkout (the reviewed version);
#   3. writes /Library/Application Support/ClaudeCode/managed-settings.json registering every gate hook through the
#      launcher, with allowManagedHooksOnly=true so project or user settings can neither add nor remove hooks.
# Claude Code's agent runs as the normal user and cannot change root-owned files, so it can no longer edit, skip or
# unregister the gates. Re-run this script after the owner approves any change to a gate file.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/Library/Application Support/ClaudeCode"
GATES="$DEST/mtf-gates"
mkdir -p "$GATES"
install -o root -g wheel -m 0755 "$REPO/scripts/managed_gates/run_gate.py" "$GATES/run_gate.py"
PY_BIN=/usr/bin/python3   # system Python: root-owned, so the agent cannot swap the interpreter
/usr/bin/python3 - "$REPO" "$GATES" "$DEST" "$PY_BIN" <<'PY'
import hashlib, json, os, sys
repo, gates, dest, py = sys.argv[1:5]
pre = os.path.join(repo, ".claude", "preship")
files = {f: hashlib.sha256(open(os.path.join(pre, f), "rb").read()).hexdigest()
         for f in sorted(os.listdir(pre)) if f.endswith((".py", ".json")) and not f.startswith("test_")}
json.dump({"files": files}, open(os.path.join(gates, "manifest.json"), "w"), indent=2)
L = py + ' "' + os.path.join(gates, "run_gate.py") + '" '
h = lambda *names: [{"type": "command", "command": L + n} for n in names]
settings = {
    "allowManagedHooksOnly": True,
    "hooks": {
        "PreToolUse": [
            {"matcher": ".*", "hooks": h("owner_approval_gate.py")},
            {"matcher": "Bash", "hooks": h("bgg_prompt_bias_gate.py", "preship_gate.py")},
            {"matcher": "Agent", "hooks": h("bgg_prompt_bias_gate.py")},
            {"matcher": "Write|Edit", "hooks": h("design_record_gate.py")},
        ],
        "PostToolUse": [{"matcher": ".*", "hooks": h("behaviour_integrity.py")}],
        "UserPromptSubmit": [{"hooks": h("owner_approval_capture.py", "fork_reminder.py", "post_live_due.py --hook")}],
        "Stop": [{"hooks": h("execute_dont_ask_gate.py", "no_guess_gate.py", "no_assume_gate.py")}],
    },
}
json.dump(settings, open(os.path.join(dest, "managed-settings.json"), "w"), indent=2)
print(f"manifest: {len(files)} gate files; managed-settings.json written")
PY
chown -R root:wheel "$DEST"
chmod 0644 "$DEST/managed-settings.json" "$GATES/manifest.json"
echo "Installed. Restart Claude Code for the managed hooks to load."

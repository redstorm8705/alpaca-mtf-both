#!/usr/bin/env python3
# ruff: noqa: E501
"""run_gate.py — ROOT-OWNED launcher for the bot's Claude Code gates (installed by scripts/install_managed_gates.sh).

Lives in /Library/Application Support/ClaudeCode/mtf-gates/ next to manifest.json (sha256 of every
.claude/preship/*.py at install time). For a session inside the alpaca-mtf-bot repo it refuses (exit 2) when any gate
file differs from the manifest — so an edited or deleted gate blocks every gated action until the owner reinstalls with
his password — and otherwise runs the requested gate with the hook's stdin. Outside this repo it does nothing.
"""
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    proj = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    remote = subprocess.run(["git", "-C", proj, "remote", "get-url", "origin"], capture_output=True, text=True).stdout
    if "alpaca-mtf-both" not in remote and "alpaca-mtf-bot" not in proj:
        return 0
    data = sys.stdin.buffer.read()
    try:
        manifest = json.load(open(os.path.join(HERE, "manifest.json")))
    except Exception as exc:
        sys.stderr.write(f"MANAGED GATE: manifest unreadable ({exc}) — blocked; owner must reinstall.\n")
        return 2
    gate_dir = os.path.join(proj, ".claude", "preship")
    for fname, want in manifest.get("files", {}).items():
        try:
            got = hashlib.sha256(open(os.path.join(gate_dir, fname), "rb").read()).hexdigest()
        except OSError:
            got = ""
        if got != want:
            sys.stderr.write(f"MANAGED GATE: .claude/preship/{fname} differs from the owner-installed version — every "
                             "gated action is blocked until the owner reviews it and reinstalls "
                             "(sudo bash scripts/install_managed_gates.sh).\n")
            return 2
    if name not in manifest.get("files", {}):
        sys.stderr.write(f"MANAGED GATE: unknown gate {name!r} — blocked.\n")
        return 2
    env = dict(os.environ, CLAUDE_PROJECT_DIR=proj)
    r = subprocess.run([sys.executable, os.path.join(gate_dir, name)] + sys.argv[2:], input=data, env=env,
                       capture_output=True)
    sys.stdout.buffer.write(r.stdout)
    sys.stderr.buffer.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())

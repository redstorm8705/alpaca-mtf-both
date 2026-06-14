#!/bin/bash
set -euo pipefail

# Only run in Claude Code on the web (remote containers).
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Modern setuptools needed first: sgmllib3k (feedparser dep) fails to build
# under the container's Debian-patched setuptools 68.x (install_layout bug).
# Upgrade setuptools ONLY — pip/wheel are Debian-managed and cannot be uninstalled.
python3 -m pip install --quiet --disable-pip-version-check --upgrade setuptools

# Runtime deps (derived from imports — no requirements.txt in repo)
# + tooling for the mandatory static-analysis gate (py_compile/mypy/ruff) and pytest.
python3 -m pip install --quiet --disable-pip-version-check \
  alpaca-py \
  numpy \
  pandas \
  requests \
  python-dotenv \
  yfinance \
  feedparser \
  scipy \
  google-genai \
  lxml \
  pytest \
  mypy \
  ruff

echo "session-start: dependencies installed"

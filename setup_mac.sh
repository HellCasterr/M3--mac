#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "Trade M3 - macOS setup"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "Python 3.11+ is required. Install it from python.org or Homebrew and rerun:"
  echo "  brew install python@3.12"
  exit 1
fi
echo "Using $("$PYTHON" --version)"
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi
echo
echo "Setup complete. Open .env and configure Upstox and (optionally) OpenAI."
echo "Launch with: bash start_mac.command"

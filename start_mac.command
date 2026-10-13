#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Trade M3 is not installed. Run: bash setup_mac.sh"
  exit 1
fi
exec .venv/bin/python run.py

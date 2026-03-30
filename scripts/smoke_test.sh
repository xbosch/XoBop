#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv python. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

echo "NOTE: smoke_test.py (bot-self-send) is deprecated for owner-flow checks."
echo "Launching interactive verifier (smoke_watch.py)..."
exec .venv/bin/python scripts/smoke_watch.py

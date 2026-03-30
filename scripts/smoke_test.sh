#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Missing .env"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv python. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

exec .venv/bin/python scripts/smoke_test.py

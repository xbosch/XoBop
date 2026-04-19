#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv python. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

MODE="interactive"
if [[ "${1:-}" == "--non-interactive" ]] || [[ "${SMOKE_NON_INTERACTIVE:-0}" == "1" ]]; then
  MODE="non-interactive"
fi

if [[ "$MODE" == "non-interactive" ]]; then
  echo "Running non-interactive smoke checks..."
  .venv/bin/python -m py_compile XoBop.py xobop_core.py xobop_core_base.py xobop_core_tail.py xobop/*.py
  .venv/bin/python -m pytest -q
  .venv/bin/python scripts/eval_suite.py
  echo "SMOKE TEST RESULT: PASS (non-interactive)"
  exit 0
fi

echo "Launching interactive verifier (smoke_watch.py)..."
exec .venv/bin/python scripts/smoke_watch.py

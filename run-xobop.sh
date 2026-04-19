#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run ./install-xobop.sh first."
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install-xobop.sh first."
  exit 1
fi

source .venv/bin/activate
set -a
source .env
set +a

exec python XoBop.py

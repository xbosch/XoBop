#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and fill secrets."
  exit 1
fi

required=(TELEGRAM_TOKEN OPENAI_API_KEY OWNER_ID)
missing=0
for k in "${required[@]}"; do
  if ! grep -qE "^${k}=.+" .env; then
    echo "Missing required key in .env: $k"
    missing=1
  fi
done
if [[ $missing -ne 0 ]]; then
  exit 1
fi

source .venv/bin/activate
set -a
source .env
set +a

echo "Starting XoBop test run..."
exec python XoBop.py

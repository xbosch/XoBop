#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "== XoBop installer =="
echo "Repo: $REPO_DIR"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v git >/dev/null || { echo "git is required"; exit 1; }

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

python -m pip install -U pip setuptools wheel >/dev/null
python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

echo
read -r -p "Telegram bot token (required): " TELEGRAM_TOKEN
while [[ -z "${TELEGRAM_TOKEN}" ]]; do
  read -r -p "Telegram bot token cannot be empty. Enter token: " TELEGRAM_TOKEN
done

read -r -p "OpenAI API key (required): " OPENAI_API_KEY
while [[ -z "${OPENAI_API_KEY}" ]]; do
  read -r -p "OpenAI API key cannot be empty. Enter key: " OPENAI_API_KEY
done

read -r -p "Telegram OWNER_ID (your numeric chat/user id) [default: 5410368885]: " OWNER_ID
OWNER_ID="${OWNER_ID:-5410368885}"

read -r -p "Gmail address (optional): " GMAIL_EMAIL
read -r -s -p "Gmail app password (optional, hidden input): " GMAIL_APP_PASSWORD
echo

ABS="$REPO_DIR"

cat > .env <<EOF
TELEGRAM_TOKEN=${TELEGRAM_TOKEN}
OPENAI_API_KEY=${OPENAI_API_KEY}
OWNER_ID=${OWNER_ID}
GMAIL_EMAIL=${GMAIL_EMAIL}
GMAIL_APP_PASSWORD=${GMAIL_APP_PASSWORD}
MODEL=gpt-4.1-mini
EMBEDDING_MODEL=text-embedding-3-small
DB_FILE=memory.db
DEFAULT_MODE=strict
ARTIFACT_DIR=artifacts
ALLOWED_WRITE_ROOT=${ABS}
READ_ROOT=${ABS}
ALLOW_RAW_SHELL=1
USE_CAPABILITY_FACADE_SHELL=0
USE_SHELL_REPLAY_CONTROLLER=0
PANIC_UNLOCK_CODE=replace_with_high_entropy_secret
RUNTIME_SNAPSHOT_FILE=${ABS}/state/runtime_settings_snapshot.json
EMBEDDING_FALLBACK=1
FILE_MEMORY_ROOT=${ABS}
LONG_MEMORY_FILE=${ABS}/MEMORY.md
DAILY_MEMORY_DIR=${ABS}/memory
EOF

chmod 600 .env

echo
python - <<'PY'
import os, requests
from pathlib import Path
p=Path('.env')
vals={}
for line in p.read_text().splitlines():
    if '=' in line:
        k,v=line.split('=',1)
        vals[k]=v
tok=vals.get('TELEGRAM_TOKEN','')
if tok:
    try:
        r=requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
        if r.get('ok'):
            u=r['result'].get('username','?')
            print(f"Telegram token OK -> @{u}")
        else:
            print("Warning: Telegram token check failed:", r)
    except Exception as e:
        print("Warning: Telegram token check error:", e)
PY

echo
echo "Install complete."
echo "Start bot with: ./run-xobop.sh"

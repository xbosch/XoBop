#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

clear_runtime_lock() {
  source .venv/bin/activate
  python - <<'PY'
import sqlite3
con=sqlite3.connect('memory.db')
con.execute("INSERT OR REPLACE INTO settings(key,value,timestamp) VALUES('runtime_lock_json','{}',CURRENT_TIMESTAMP)")
con.commit()
print('runtime_lock_json cleared')
PY
}

is_running() {
  ./status_dev.sh | grep -q "^running pid="
}

./stop_dev.sh || true
./start_dev.sh || true

if ! is_running; then
  echo "First start failed; clearing stale runtime lock and retrying once..."
  clear_runtime_lock
  ./start_dev.sh || true
fi

if ! is_running; then
  echo "ERROR: bot is not running after restart attempts"
  ./status_dev.sh || true
  exit 1
fi

./status_dev.sh
./scripts/smoke_test.sh --non-interactive

echo "Restart + smoke verification: PASS"

#!/usr/bin/env bash
# Verify the most recent backup is actually restorable: open it read-only
# and sanity-check row counts on a few key tables against the live DB.
# Does NOT modify anything -- this is a check, not the restore itself.
#
# To actually restore from a backup (only after stopping the service):
#   systemctl --user stop xobop.service
#   cp workspace/backups/<chosen-backup>.db workspace/memory.db
#   systemctl --user start xobop.service
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="workspace/backups"
LIVE_DB="workspace/memory.db"

latest="$(ls -t "$BACKUP_DIR"/memory_*.db 2>/dev/null | head -1)"
if [[ -z "$latest" ]]; then
  echo "No backups found in $BACKUP_DIR"
  exit 1
fi

echo "Checking: $latest"

python3 - "$latest" "$LIVE_DB" <<'PY'
import sqlite3
import sys

backup_path, live_path = sys.argv[1], sys.argv[2]

tables = ["memory_items", "improvement_proposals", "reminders", "cron_jobs", "op_events"]

def counts(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        except sqlite3.OperationalError as exc:
            out[t] = f"ERROR: {exc}"
    conn.close()
    return out

backup_counts = counts(backup_path)
live_counts = counts(live_path)

print(f"{'table':<24} {'backup':>10} {'live':>10}")
ok = True
for t in tables:
    b, l = backup_counts[t], live_counts[t]
    print(f"{t:<24} {b!s:>10} {l!s:>10}")
    if isinstance(b, str):
        ok = False

if not ok:
    print("\nFAIL: backup is not readable / missing expected tables.")
    sys.exit(1)

print("\nPASS: backup opens read-only and all key tables are queryable.")
PY

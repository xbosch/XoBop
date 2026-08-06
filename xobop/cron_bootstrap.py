from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3


def ensure_default_cron_jobs(conn: sqlite3.Connection, repo_cwd: str) -> None:
    _backup_dir = os.getenv("BACKUP_DIR", str(Path(repo_cwd) / "workspace" / "backups"))
    jobs = [
        (
            "db-backup-6h",
            "Online SQLite backup every 6 hours",
            "0 */6 * * *",
            "db_backup",
            json.dumps({"backup_dir": str(Path(_backup_dir).resolve()), "repo_dir": repo_cwd}),
        ),
        (
            "prompt-review-weekly",
            "Weekly prompt proposal from usage/failure stats",
            "0 9 * * 1",
            "prompt_review",
            "{}",
        ),
        (
            "checkup-monthly",
            "Monthly environment/dependency check",
            "0 10 1 * *",
            "checkup",
            "{}",
        ),
        (
            "system-map-weekly",
            "Weekly self-documentation refresh",
            "30 9 * * 1",
            "system_map_refresh",
            "{}",
        ),
        (
            "improve-pipeline-sweep-nightly",
            "Nightly compile+simulate+auto-promote sweep over pending proposals",
            "0 3 * * *",
            "improve_pipeline_sweep",
            json.dumps({"max_proposals": 10}),
        ),
    ]
    jobs_no_alert = [
        (
            "reminders-check-5m",
            "Notify owner of due reminders and mark them sent",
            "*/5 * * * *",
            "reminders_check",
            "{}",
        ),
        (
            "daily-digest-8am",
            "Proactive morning digest: due reminders, stale important memories, pipeline health",
            "0 8 * * *",
            "daily_digest",
            "{}",
        ),
        (
            "op-events-cleanup-daily",
            "Delete op_events rows older than the retention window (unbounded table growth otherwise)",
            "0 4 * * *",
            "op_events_cleanup",
            "{}",
        ),
    ]

    for name, description, cron_expr, skill_type, skill_args in jobs:
        conn.execute(
            """
            INSERT OR IGNORE INTO cron_jobs (name,description,cron_expr,skill_type,skill_args,enabled,alert_owner,updated_ts)
            VALUES (?,?,?,?,?,1,1,CURRENT_TIMESTAMP)
            """,
            (name, description, cron_expr, skill_type, skill_args),
        )

    for name, description, cron_expr, skill_type, skill_args in jobs_no_alert:
        conn.execute(
            """
            INSERT OR IGNORE INTO cron_jobs (name,description,cron_expr,skill_type,skill_args,enabled,alert_owner,updated_ts)
            VALUES (?,?,?,?,?,1,0,CURRENT_TIMESTAMP)
            """,
            (name, description, cron_expr, skill_type, skill_args),
        )

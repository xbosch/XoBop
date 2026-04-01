from __future__ import annotations

import sqlite3
from typing import Callable


MigrationFn = Callable[[sqlite3.Connection], None]


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _migration_1_skill_audit_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(skill_audit)").fetchall()}
    for col, typ in (("owner_id", "INTEGER"), ("cmd_input", "TEXT"), ("exit_code", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE skill_audit ADD COLUMN {col} {typ}")


def _migration_2_improvement_runs_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(improvement_runs)").fetchall()}
    for col, typ in (("promotion_mode", "TEXT"), ("eval_phase", "TEXT"), ("reason", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE improvement_runs ADD COLUMN {col} {typ}")


def _migration_3_change_artifacts_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(change_artifacts)").fetchall()}
    for col, typ in (("promotion_mode", "TEXT"), ("eval_phase", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE change_artifacts ADD COLUMN {col} {typ}")


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations in deterministic order."""
    _ensure_migrations_table(conn)

    plan: list[tuple[int, str, MigrationFn]] = [
        (1, "skill_audit_add_owner_cmd_exit", _migration_1_skill_audit_columns),
        (2, "improvement_runs_add_promotion_eval_reason", _migration_2_improvement_runs_columns),
        (3, "change_artifacts_add_promotion_eval", _migration_3_change_artifacts_columns),
    ]

    existing = {
        int(r["id"])
        for r in conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
    }

    for mid, name, fn in plan:
        if mid in existing:
            continue
        fn(conn)
        conn.execute("INSERT INTO schema_migrations (id, name) VALUES (?, ?)", (mid, name))

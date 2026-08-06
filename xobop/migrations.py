from __future__ import annotations

import re
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


def _migration_4_runs_turn_id(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "runs" not in tables:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "turn_id" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN turn_id TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_turn_id ON runs(turn_id, id)")


def _migration_5_workflow_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_state (
            chat_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id, key)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_state_key ON workflow_state(key, chat_id)")


def _migration_6_memory_promotion_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_promotion_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            decision_reason TEXT,
            created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            picked_ts DATETIME,
            done_ts DATETIME
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_promotion_queue_status ON memory_promotion_queue(status,id)")


def _migration_7_memory_promotion_decision_reason(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_promotion_queue)").fetchall()}
    if "decision_reason" not in cols:
        conn.execute("ALTER TABLE memory_promotion_queue ADD COLUMN decision_reason TEXT")


def _migration_8_memory_promotion_status_normalize(conn: sqlite3.Connection) -> None:
    # Normalize legacy terminal status naming.
    conn.execute("UPDATE memory_promotion_queue SET status='done_promoted' WHERE status='done'")


def _migration_9_evolution_memory_v2(conn: sqlite3.Connection) -> None:
    """Add semantic fields to evolution_memory and failure_class to improvement_proposals."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "evolution_memory" in tables:
        em_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
        for col, typ in (
            ("artifact_family", "TEXT NOT NULL DEFAULT ''"),
            ("target_route", "TEXT NOT NULL DEFAULT ''"),
            ("metric_delta", "REAL"),
            ("failure_class", "TEXT NOT NULL DEFAULT ''"),
            ("signal_fingerprint", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if col not in em_cols:
                conn.execute(f"ALTER TABLE evolution_memory ADD COLUMN {col} {typ}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evolution_memory_v2 ON evolution_memory(failure_class, target_route, outcome)"
        )

    if "improvement_proposals" in tables:
        ip_cols = {r["name"] for r in conn.execute("PRAGMA table_info(improvement_proposals)").fetchall()}
        if "failure_class" not in ip_cols:
            conn.execute("ALTER TABLE improvement_proposals ADD COLUMN failure_class TEXT NOT NULL DEFAULT ''")


def _migration_10_evolve_memory_v2_backfill(conn: sqlite3.Connection) -> None:
    """Backfill v2 semantic fields on existing evolution_memory rows that still have empty failure_class."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "evolution_memory" not in tables:
        return
    em_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
    if "failure_class" not in em_cols:
        return

    ip_cols = {r["name"] for r in conn.execute("PRAGMA table_info(improvement_proposals)").fetchall()}
    select_fc = ", ip.failure_class" if "failure_class" in ip_cols else ""

    rows = conn.execute(
        f"""
        SELECT em.id, ip.title, ip.problem_statement{select_fc}, ca.artifact_type, ca.target
        FROM evolution_memory em
        LEFT JOIN improvement_proposals ip ON ip.id = em.proposal_id
        LEFT JOIN (
            SELECT proposal_id, artifact_type, target
            FROM change_artifacts
            WHERE id IN (SELECT MAX(id) FROM change_artifacts GROUP BY proposal_id)
        ) ca ON ca.proposal_id = em.proposal_id
        WHERE em.failure_class = ''
        """
    ).fetchall()

    _family_map = {
        "routing_rule": "routing",
        "config_change": "config",
        "prompt_change": "prompt",
        "skill_policy_change": "skill_policy",
        "state_lineage": "state",
    }

    for row in rows:
        # Prefer stored failure_class over keyword derivation
        stored_fc = str(row["failure_class"] if "failure_class" in ip_cols and row["failure_class"] else "").strip()
        if stored_fc and stored_fc != "general":
            fc = stored_fc
        else:
            title = (row["title"] or "").lower()
            problem = (row["problem_statement"] or "").lower()
            if "unknown command" in title or "unknown command" in problem or "discoverability" in title:
                fc = "unknown_command"
            elif "latency" in title or "latency" in problem or "p95" in title:
                fc = "latency"
            elif "stabilize" in title or "instability" in problem or "failing route" in title or ("route" in title and "fail" in title):
                fc = "route_failure"
            elif "retry" in title or "retry" in problem:
                fc = "retry_exhaustion"
            else:
                fc = stored_fc or "general"
        title = (row["title"] or "").lower()  # needed for route regex below

        af = _family_map.get(row["artifact_type"] or "", row["artifact_type"] or "") or ""
        tr = str(row["target"] or "")
        m = re.search(r"(?:route[:\s]+|for route[:\s]+|failing route[:\s]+)([a-z_]+)", title)
        if m:
            tr = m.group(1)

        conn.execute(
            "UPDATE evolution_memory SET failure_class=?, artifact_family=?, target_route=? WHERE id=?",
            (fc, af, tr, row["id"]),
        )


def _migration_11_evolution_memory_embedding(conn: sqlite3.Connection) -> None:
    """Add embedding_json column to evolution_memory for diagnosis-conditioned retrieval."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "evolution_memory" not in tables:
        return
    em_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
    if "embedding_json" not in em_cols:
        conn.execute("ALTER TABLE evolution_memory ADD COLUMN embedding_json TEXT")


def _migration_12_evolution_query_vectors(conn: sqlite3.Connection) -> None:
    """Per-failure-class learned query vectors for AttnRes-style conditioned retrieval.

    Each row stores a unit-norm embedding vector updated via EMA after every
    /evolve_capture call. Update rule:
        q_{t+1} = normalize( (1-α)*q_t + α*sign*d_t )
    where d_t is the lesson embedding and sign encodes outcome quality.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evolution_query_vectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            failure_class TEXT NOT NULL UNIQUE,
            query_vector_json TEXT NOT NULL,
            update_count INTEGER DEFAULT 0,
            ema_alpha REAL DEFAULT 0.15,
            last_updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eqv_failure_class ON evolution_query_vectors(failure_class)"
    )


def _migration_13_evolution_memory_blocks(conn: sqlite3.Connection) -> None:
    """Temporal block summaries over evolution memory (AttnRes Block variant).

    Partitions evolution entries into N contiguous blocks. Each block stores the
    mean L2-normalised embedding of its entries, dominant failure class, and
    aggregate statistics. Effective rank of G lies between N and N+S.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evolution_memory_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_index INTEGER NOT NULL,
            entry_ids_json TEXT NOT NULL,
            block_embedding_json TEXT,
            dominant_failure_class TEXT,
            entry_count INTEGER DEFAULT 0,
            avg_metric_delta REAL,
            date_from TEXT,
            date_to TEXT,
            recomputed_ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_emb_block_index ON evolution_memory_blocks(block_index)"
    )


def _migration_14_evolve_blocks_cron(conn: sqlite3.Connection) -> None:
    """Register the nightly block recomputation cron job."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "cron_jobs" not in tables:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO cron_jobs(name, description, cron_expr, skill_type, skill_args, enabled, alert_owner)
        VALUES(
            'evolve_blocks_nightly',
            'Recompute evolution memory block compression (N=4 temporal blocks)',
            '0 2 * * *',
            'evolve_blocks_recompute',
            '{"n_blocks": 4}',
            1,
            0
        )
        """
    )


def _migration_15_reminders_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            due_ts DATETIME NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            notified_ts DATETIME
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_ts)")


def _migration_16_reminders_retry_tracking(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reminders)").fetchall()}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if "last_error" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN last_error TEXT")


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations in deterministic order."""
    _ensure_migrations_table(conn)

    plan: list[tuple[int, str, MigrationFn]] = [
        (1, "skill_audit_add_owner_cmd_exit", _migration_1_skill_audit_columns),
        (2, "improvement_runs_add_promotion_eval_reason", _migration_2_improvement_runs_columns),
        (3, "change_artifacts_add_promotion_eval", _migration_3_change_artifacts_columns),
        (4, "runs_add_turn_id", _migration_4_runs_turn_id),
        (5, "workflow_state_table", _migration_5_workflow_state_table),
        (6, "memory_promotion_queue", _migration_6_memory_promotion_queue),
        (7, "memory_promotion_decision_reason", _migration_7_memory_promotion_decision_reason),
        (8, "memory_promotion_status_normalize", _migration_8_memory_promotion_status_normalize),
        (9, "evolution_memory_v2_semantic_fields", _migration_9_evolution_memory_v2),
        (10, "evolve_memory_v2_backfill", _migration_10_evolve_memory_v2_backfill),
        (11, "evolution_memory_embedding_json", _migration_11_evolution_memory_embedding),
        (12, "evolution_query_vectors", _migration_12_evolution_query_vectors),
        (13, "evolution_memory_blocks", _migration_13_evolution_memory_blocks),
        (14, "evolve_blocks_cron", _migration_14_evolve_blocks_cron),
        (15, "reminders_table", _migration_15_reminders_table),
        (16, "reminders_retry_tracking", _migration_16_reminders_retry_tracking),
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

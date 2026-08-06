import sqlite3

from xobop.migrations import apply_migrations


def test_apply_migrations_records_versions_and_adds_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE skill_audit (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE improvement_runs (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE change_artifacts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")

    apply_migrations(conn)

    rows = conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

    skill_cols = {r["name"] for r in conn.execute("PRAGMA table_info(skill_audit)").fetchall()}
    assert {"owner_id", "cmd_input", "exit_code"}.issubset(skill_cols)

    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "turn_id" in run_cols

    wf_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_state)").fetchall()}
    assert {"chat_id", "key", "value_json", "updated_ts"}.issubset(wf_cols)

    mp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_promotion_queue)").fetchall()}
    assert {"id", "text", "source", "status", "error", "decision_reason", "created_ts", "picked_ts", "done_ts"}.issubset(mp_cols)

    qv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_query_vectors)").fetchall()}
    assert {"failure_class", "query_vector_json", "update_count", "last_updated_ts"}.issubset(qv_cols)

    blk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory_blocks)").fetchall()}
    assert {"block_index", "entry_ids_json", "block_embedding_json", "dominant_failure_class"}.issubset(blk_cols)

import sqlite3

from xobop.migrations import apply_migrations


def test_migration_normalizes_done_status_to_done_promoted():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE skill_audit (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE improvement_runs (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE change_artifacts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")

    # apply through migration 7 first
    apply_migrations(conn)

    conn.execute("INSERT INTO memory_promotion_queue(text,source,status) VALUES ('x','user','done')")
    conn.execute("DELETE FROM schema_migrations WHERE id=8")
    apply_migrations(conn)

    status = conn.execute("SELECT status FROM memory_promotion_queue ORDER BY id DESC LIMIT 1").fetchone()["status"]
    assert status == "done_promoted"

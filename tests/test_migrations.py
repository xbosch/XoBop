import sqlite3

from xobop.migrations import apply_migrations


def test_apply_migrations_records_versions_and_adds_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE skill_audit (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE improvement_runs (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE change_artifacts (id INTEGER PRIMARY KEY)")

    apply_migrations(conn)

    rows = conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2, 3]

    skill_cols = {r["name"] for r in conn.execute("PRAGMA table_info(skill_audit)").fetchall()}
    assert {"owner_id", "cmd_input", "exit_code"}.issubset(skill_cols)

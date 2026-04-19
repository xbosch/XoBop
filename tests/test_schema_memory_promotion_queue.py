import sqlite3

from xobop.schema import ensure_core_schema


def test_schema_includes_memory_promotion_queue():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_core_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_promotion_queue)").fetchall()}
    assert {"id", "text", "source", "status", "decision_reason", "created_ts"}.issubset(cols)

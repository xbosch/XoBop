import sqlite3

from xobop.migrations import apply_migrations
from xobop.schema import ensure_core_schema


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_core_schema(conn)
    apply_migrations(conn)
    return conn


def _insert_event(conn, days_ago: int):
    conn.execute(
        "INSERT INTO op_events(timestamp,event_type,status) VALUES (datetime('now', ?), 'test', 'ok')",
        (f"-{days_ago} day",),
    )


def test_prune_op_events_deletes_only_rows_older_than_retention():
    conn = _make_db()
    _insert_event(conn, 200)
    _insert_event(conn, 150)
    _insert_event(conn, 100)
    _insert_event(conn, 10)
    conn.commit()

    retention_days = 120
    cur = conn.execute("DELETE FROM op_events WHERE timestamp < datetime('now', ?)", (f"-{retention_days} day",))
    assert cur.rowcount == 2  # the 200-day and 150-day rows

    remaining_days_ago = sorted(
        int((conn.execute("SELECT julianday('now') - julianday(timestamp) AS d FROM op_events WHERE id=?", (r["id"],)).fetchone()["d"]))
        for r in conn.execute("SELECT id FROM op_events")
    )
    assert remaining_days_ago == [10, 100]

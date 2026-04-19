import sqlite3

from xobop.improve_generate import diagnose_report


def _ctx(conn):
    class C:
        def __enter__(self):
            return conn

        def __exit__(self, exc_type, exc, tb):
            return False

    return lambda: C()


def test_diagnose_backfills_signals_from_op_events():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE op_events (id INTEGER PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, event_type TEXT, route TEXT, command TEXT, status TEXT, latency_ms INTEGER, retries INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE op_signals_daily (day TEXT PRIMARY KEY, events_total INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, command_unknown INTEGER NOT NULL DEFAULT 0, retries_total INTEGER NOT NULL DEFAULT 0, median_latency_ms REAL NOT NULL DEFAULT 0, p95_latency_ms REAL NOT NULL DEFAULT 0, updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO op_events(event_type,status,latency_ms,retries,route,command) VALUES ('unknown_command','error',1200,1,'cmd_exec','/statu')")
    conn.execute("INSERT INTO op_events(event_type,status,latency_ms,retries,route,command) VALUES ('route_selected','ok',300,0,'cmd_status','/status')")
    # tester noise — should NOT count toward command_unknown
    conn.execute("INSERT INTO op_events(event_type,status,latency_ms,retries,route,command) VALUES ('unknown_command','error',100,0,'cmd_exec','/unknowncommand')")
    conn.execute("INSERT INTO op_events(event_type,status,latency_ms,retries,route,command) VALUES ('unknown_command','error',100,0,'cmd_exec','/unknowncommand')")
    conn.execute("INSERT INTO op_events(event_type,status,latency_ms,retries,route,command) VALUES ('unknown_command','error',100,0,'cmd_exec','/unknowncommand')")
    conn.commit()

    report = diagnose_report(days=7, get_db=_ctx(conn))
    assert "No diagnosis data yet" not in report

    row = conn.execute("SELECT events_total, failures, command_unknown FROM op_signals_daily ORDER BY day DESC LIMIT 1").fetchone()
    assert row is not None
    assert int(row["events_total"]) >= 5
    # Only the real unknown (/statu) should be counted — 3 tester noise rows excluded
    assert int(row["command_unknown"]) == 1


def test_diagnose_noise_excluded_from_unknown_cmds_display():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE op_events (id INTEGER PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, event_type TEXT, route TEXT, command TEXT, status TEXT, latency_ms INTEGER, retries INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE op_signals_daily (day TEXT PRIMARY KEY, events_total INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, command_unknown INTEGER NOT NULL DEFAULT 0, retries_total INTEGER NOT NULL DEFAULT 0, median_latency_ms REAL NOT NULL DEFAULT 0, p95_latency_ms REAL NOT NULL DEFAULT 0, updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    # Only tester noise unknowns — should not appear in report's top unknown list
    for _ in range(10):
        conn.execute("INSERT INTO op_events(event_type,status,command) VALUES ('unknown_command','error','/unknowncommand')")
    conn.commit()

    report = diagnose_report(days=7, get_db=_ctx(conn))
    assert "/unknowncommand" not in report
    assert "discoverability" not in report

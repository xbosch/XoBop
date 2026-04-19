import sqlite3

from xobop.improve_generate import generate_improvement_proposal


def _db_ctx(conn):
    class _Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, exc_type, exc, tb):
            return False

    return lambda: _Ctx()


def test_generate_uses_diagnosis_text_not_only_sql():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE op_events (id INTEGER PRIMARY KEY, timestamp TEXT, route TEXT, status TEXT, event_type TEXT, command TEXT)")
    conn.execute("CREATE TABLE op_signals_daily (day TEXT, p95_latency_ms REAL)")
    conn.execute("""
    CREATE TABLE improvement_proposals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
      status TEXT,
      risk_level TEXT,
      title TEXT,
      problem_statement TEXT,
      suspected_cause TEXT,
      proposed_change TEXT,
      expected_benefit TEXT,
      metric_target TEXT,
      failure_class TEXT NOT NULL DEFAULT '',
      command TEXT
    )
    """)

    get_db = _db_ctx(conn)

    def diag(_days: int) -> str:
        return "Diagnosis window... Likely causes:\n- Command discoverability issue: high unknown-command volume"

    pid, out = generate_improvement_proposal(days=7, get_db=get_db, diagnose_report_fn=diag)
    assert pid >= 1
    assert "unknown command" in out.lower() or "unknown-command" in out.lower()

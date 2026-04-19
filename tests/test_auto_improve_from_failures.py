import sqlite3

from xobop.auto_improve import maybe_generate_auto_proposals


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_query TEXT,
            result TEXT,
            status TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE improvement_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'proposed',
            risk_level TEXT NOT NULL DEFAULT 'low',
            title TEXT NOT NULL,
            problem_statement TEXT NOT NULL,
            suspected_cause TEXT NOT NULL,
            proposed_change TEXT NOT NULL,
            expected_benefit TEXT NOT NULL,
            metric_target TEXT NOT NULL
        )
        """
    )
    return conn


def test_auto_improve_creates_proposal_when_cluster_threshold_met():
    conn = _db()
    for _ in range(6):
        conn.execute(
            "INSERT INTO runs(user_query,result,status) VALUES (?,?,?)",
            ("Write a python script and run it. Post me here the results, I want a plot.", "Error: Command not found: './run_plot.sh'", "error"),
        )
    conn.commit()

    rep = maybe_generate_auto_proposals(
        get_db=lambda: conn,
        enabled=True,
        dry_run=False,
        threshold=5,
        max_per_day=5,
        dedup_days=7,
    )
    assert rep["created"] >= 1
    n = conn.execute("SELECT COUNT(*) FROM improvement_proposals").fetchone()[0]
    assert n >= 1


def test_auto_improve_dry_run_creates_no_rows():
    conn = _db()
    for _ in range(6):
        conn.execute(
            "INSERT INTO runs(user_query,result,status) VALUES (?,?,?)",
            ("same query", "same error", "error"),
        )
    conn.commit()

    rep = maybe_generate_auto_proposals(
        get_db=lambda: conn,
        enabled=True,
        dry_run=True,
        threshold=5,
        max_per_day=5,
        dedup_days=7,
    )
    assert rep["created"] == 0
    n = conn.execute("SELECT COUNT(*) FROM improvement_proposals").fetchone()[0]
    assert n == 0

import sqlite3

from xobop.auto_improve import _cluster_signature, auto_improve_flags, maybe_generate_auto_proposals


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


def test_auto_improve_flags_use_get_setting_when_provided():
    cfg = {
        "AUTO_IMPROVE_ENABLED": "1",
        "AUTO_IMPROVE_DRY_RUN": "0",
        "AUTO_IMPROVE_MIN_CLUSTER": "7",
        "AUTO_IMPROVE_MAX_PER_DAY": "9",
        "AUTO_IMPROVE_DEDUP_DAYS": "11",
    }

    def get_setting(k: str, d: str) -> str:
        return cfg.get(k, d)

    assert auto_improve_flags(get_setting=get_setting) == (True, False, 7, 9, 11)


def test_followup_insert_includes_created_ids():
    conn = _db()
    query = "Write a python script and run it. Post me here the results, I want a plot."
    result = "Error: Command not found: './run_plot.sh'"
    sig = _cluster_signature(query, result, "error")

    # existing auto proposal for this signature triggers followup path instead of normal create
    conn.execute(
        """
        INSERT INTO improvement_proposals
        (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target)
        VALUES ('proposed','low','seed','p','c','chg','b',?)
        """,
        (f"AUTO_SIG:{sig};COUNT:6;SOURCE:auto_failure_cluster",),
    )

    for _ in range(13):
        conn.execute("INSERT INTO runs(user_query,result,status) VALUES (?,?,?)", (query, result, "error"))
    conn.commit()

    rep = maybe_generate_auto_proposals(
        get_db=lambda: conn,
        enabled=True,
        dry_run=False,
        threshold=5,
        max_per_day=10,
        dedup_days=7,
    )

    assert rep["followups"] >= 1
    assert rep["created"] >= 1
    assert len(rep.get("created_ids", [])) >= 1
    assert rep.get("safe_for_autopromote") is False
    row = conn.execute(
        "SELECT id,risk_level,metric_target FROM improvement_proposals WHERE metric_target LIKE 'AUTO_FOLLOWUP_SIG:%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert int(row["id"]) in rep["created_ids"]
    assert row["risk_level"] == "medium"

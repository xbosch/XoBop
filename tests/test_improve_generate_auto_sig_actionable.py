import sqlite3

from xobop.improve_generate import compile_proposal_to_change


def _ctx(conn):
    class C:
        def __enter__(self):
            return conn

        def __exit__(self, exc_type, exc, tb):
            return False

    return lambda: C()


def test_compile_auto_sig_proposal_into_retry_artifact():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE improvement_proposals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT,
          problem_statement TEXT,
          suspected_cause TEXT,
          proposed_change TEXT,
          metric_target TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE change_artifacts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          proposal_id INTEGER,
          artifact_type TEXT,
          target TEXT,
          artifact_json TEXT,
          expected_metric TEXT,
          expected_delta REAL,
          status TEXT,
          created_ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO improvement_proposals(title,problem_statement,suspected_cause,proposed_change,metric_target) VALUES(?,?,?,?,?)",
        (
            "Auto: reduce recurring error cluster (6x)",
            "Recurring error failures detected",
            "timeouts",
            "auto cluster remediation",
            "AUTO_SIG:abc123;COUNT:6;SOURCE:auto_failure_cluster",
        ),
    )
    conn.commit()

    def _artifact_contract(_atype, _target):
        return {"apply_points": [], "invariants": [], "contract_version": "v1", "contract_hash": "h", "family": "retry"}

    out = compile_proposal_to_change(
        proposal_id=1,
        get_db=_ctx(conn),
        get_command_aliases=lambda: {},
        get_exec_retry_limit=lambda: 0,
        get_last_applied_artifact_id=lambda: 0,
        get_mode=lambda: "normal",
        get_runtime_prompt=lambda _m: "prompt",
        artifact_contract=_artifact_contract,
        allowed_artifact_families=lambda: {"retry", "alias", "lineage", "prompt", "skill_policy"},
    )

    assert "compiled" in out.lower() and "proposal #1" in out.lower()
    row = conn.execute("SELECT artifact_type,target,expected_metric,status FROM change_artifacts WHERE proposal_id=1 ORDER BY id DESC LIMIT 1").fetchone()
    assert row["artifact_type"] == "config_change"
    assert row["target"] == "runtime_retry_policy"
    assert row["expected_metric"] == "failure_rate"
    assert row["status"] == "compiled"

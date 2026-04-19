import json
import sqlite3

from xobop.improve_generate import compile_proposal_to_change


def _db(proposal_title="Stabilize failing route: run_shell", proposal_change="Add pre-validation and clearer blocked/error responses for route 'run_shell'."):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE improvement_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT,
            risk_level TEXT,
            title TEXT,
            problem_statement TEXT,
            suspected_cause TEXT,
            proposed_change TEXT,
            expected_benefit TEXT,
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
            status TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE op_events (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            route TEXT,
            status TEXT,
            event_type TEXT,
            command TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO improvement_proposals(status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target) VALUES (?,?,?,?,?,?,?,?)",
        (
            "proposed", "medium",
            proposal_title,
            "Route 'run_shell' instability detected in diagnosis window.",
            "Route-specific policy/path/validation friction.",
            proposal_change,
            "Lower route failure rate and fewer retries.",
            "failures on route 'run_shell' reduced by >=40%",
        ),
    )
    conn.commit()
    return conn


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_):
        return False


def _compile(conn):
    return compile_proposal_to_change(
        proposal_id=1,
        get_db=lambda: _Ctx(conn),
        get_command_aliases=lambda: {},
        get_exec_retry_limit=lambda: 1,
        get_last_applied_artifact_id=lambda: 0,
        get_mode=lambda: "strict",
        get_runtime_prompt=lambda _m: "current prompt",
        artifact_contract=lambda _t, _tgt: {
            "family": "config",
            "apply_points": [],
            "invariants": [],
            "contract_version": "1",
            "contract_hash": "abc",
        },
        allowed_artifact_families=lambda: ["config"],
    )


def test_route_instability_proposal_is_actionable():
    conn = _db()
    result = _compile(conn)
    assert "non-actionable" not in result
    assert "Artifact" in result
    artifact_row = conn.execute("SELECT artifact_type, target, artifact_json FROM change_artifacts LIMIT 1").fetchone()
    assert artifact_row["artifact_type"] == "config_change"
    assert artifact_row["target"] == "runtime_retry_policy"
    artifact = json.loads(artifact_row["artifact_json"])
    assert artifact["key"] == "exec_retry_limit"
    assert int(artifact["value"]) == 2  # capped at min(3, 1+1)


def test_route_instability_via_instability_keyword():
    conn = _db(
        proposal_title="Fix route instability on cmd_exec",
        proposal_change="Investigate instability and route failures.",
    )
    result = _compile(conn)
    assert "non-actionable" not in result
    assert "config_change" in result


def test_failing_route_keyword_matches():
    conn = _db(
        proposal_title="Address failing route: http_get",
        proposal_change="Improve error handling for failing route traffic.",
    )
    result = _compile(conn)
    assert "non-actionable" not in result


def test_route_name_extracted_from_non_standard_title():
    """Title like 'Add Retry Policy for browse_page Route' — no 'failing route:' prefix."""
    conn = _db(
        proposal_title="Add Retry Policy for browse_page Route",
        proposal_change="Investigate instability on browse_page route and add retries.",
    )
    # Seed blocked failures for browse_page
    for _ in range(6):
        conn.execute("INSERT INTO op_events(timestamp,route,status,event_type) VALUES(datetime('now','-1 day'),'browse_page','blocked','route_selected')")
    conn.commit()
    result = compile_proposal_to_change(
        proposal_id=1,
        get_db=lambda: _Ctx(conn),
        get_command_aliases=lambda: {},
        get_exec_retry_limit=lambda: 1,
        get_last_applied_artifact_id=lambda: 0,
        get_mode=lambda: "strict",
        get_runtime_prompt=lambda _m: "prompt",
        artifact_contract=lambda _t, _tgt: {
            "family": "skill_policy",
            "apply_points": [], "invariants": [],
            "contract_version": "1", "contract_hash": "abc",
        },
        allowed_artifact_families=lambda: ["skill_policy"],
    )
    assert "non-actionable" not in result
    row = conn.execute("SELECT artifact_type FROM change_artifacts LIMIT 1").fetchone()
    assert row["artifact_type"] == "skill_policy_change"


def test_blocked_dominant_compiles_skill_policy_not_retry():
    conn = _db()
    # Seed op_events: blocked >> errored for run_shell
    for _ in range(8):
        conn.execute("INSERT INTO op_events(timestamp,route,status,event_type) VALUES(datetime('now','-1 day'),'run_shell','blocked','route_selected')")
    for _ in range(2):
        conn.execute("INSERT INTO op_events(timestamp,route,status,event_type) VALUES(datetime('now','-1 day'),'run_shell','error','route_selected')")
    conn.commit()

    result = compile_proposal_to_change(
        proposal_id=1,
        get_db=lambda: _Ctx(conn),
        get_command_aliases=lambda: {},
        get_exec_retry_limit=lambda: 1,
        get_last_applied_artifact_id=lambda: 0,
        get_mode=lambda: "strict",
        get_runtime_prompt=lambda _m: "prompt",
        artifact_contract=lambda _t, _tgt: {
            "family": "skill_policy",
            "apply_points": [], "invariants": [],
            "contract_version": "1", "contract_hash": "abc",
        },
        allowed_artifact_families=lambda: ["skill_policy"],
    )
    assert "non-actionable" not in result
    row = conn.execute("SELECT artifact_type, target FROM change_artifacts LIMIT 1").fetchone()
    assert row["artifact_type"] == "skill_policy_change"
    assert row["target"] == "runtime_skill_config"


def test_noop_guard_rejects_unchanged_retry_limit():
    conn = _db()
    # get_exec_retry_limit returns 3 (already at max); proposed value would also be 3
    result = compile_proposal_to_change(
        proposal_id=1,
        get_db=lambda: _Ctx(conn),
        get_command_aliases=lambda: {},
        get_exec_retry_limit=lambda: 3,
        get_last_applied_artifact_id=lambda: 0,
        get_mode=lambda: "strict",
        get_runtime_prompt=lambda _m: "prompt",
        artifact_contract=lambda _t, _tgt: {
            "family": "config",
            "apply_points": [], "invariants": [],
            "contract_version": "1", "contract_hash": "abc",
        },
        allowed_artifact_families=lambda: ["config"],
    )
    assert "non-actionable" in result
    assert "exec_retry_limit already at 3" in result
    # No artifact inserted
    count = conn.execute("SELECT COUNT(*) AS n FROM change_artifacts").fetchone()["n"]
    assert count == 0

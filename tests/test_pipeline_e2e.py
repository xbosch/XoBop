"""End-to-end integration test for the full improvement pipeline.

Covers: proposal generation → compilation → simulation pass (mocked) →
        evolution memory capture → v2 fields verified.

Uses real module logic throughout; only external dependencies (LLM, Telegram)
are absent. DB is an in-memory SQLite with the full migrated schema.
"""
import json
import sqlite3

from xobop.migrations import apply_migrations
from xobop.schema import ensure_core_schema
from xobop.improve_generate import generate_improvement_proposal, compile_proposal_to_change
from xobop.improve_pipeline_ops import (
    capture_evolution_memory_sync,
    latest_impact_confidence,
    latest_impact_delta,
)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_core_schema(conn)
    apply_migrations(conn)
    # Seed op_events with route failures so the generator picks route_failure class
    conn.execute(
        "INSERT INTO op_events(timestamp,route,status,event_type,command) VALUES(datetime('now','-1 day'),'run_shell','error','route_selected','/run')"
    )
    conn.execute(
        "INSERT INTO op_events(timestamp,route,status,event_type,command) VALUES(datetime('now','-1 day'),'run_shell','error','route_selected','/run')"
    )
    conn.execute(
        "INSERT INTO op_events(timestamp,route,status,event_type,command) VALUES(datetime('now','-1 day'),'run_shell','ok','route_selected','/status')"
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


def _db_factory(conn):
    return lambda: _Ctx(conn)


def test_proposal_generation_stores_failure_class():
    conn = _make_db()
    get_db = _db_factory(conn)

    pid, out = generate_improvement_proposal(
        days=7,
        get_db=get_db,
        diagnose_report_fn=lambda _d: "Diagnosis: no data",
    )

    assert pid >= 1
    assert "Failure class:" in out

    row = conn.execute("SELECT failure_class FROM improvement_proposals WHERE id=?", (pid,)).fetchone()
    assert row is not None
    assert row["failure_class"] in {"route_failure", "unknown_command", "latency", "general"}


def test_compile_produces_artifact_for_route_failure():
    conn = _make_db()
    get_db = _db_factory(conn)

    # Insert a route_failure proposal directly
    conn.execute(
        """INSERT INTO improvement_proposals
        (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target,failure_class)
        VALUES('proposed','medium',?,?,?,?,?,?,?)""",
        (
            "Stabilize failing route: run_shell",
            "Route 'run_shell' instability detected.",
            "Route-specific friction.",
            "Add pre-validation.",
            "Lower failure rate.",
            "failures on route 'run_shell' reduced by >=40%",
            "route_failure",
        ),
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    result = compile_proposal_to_change(
        proposal_id=pid,
        get_db=get_db,
        get_command_aliases=lambda: {},
        get_exec_retry_limit=lambda: 1,
        get_last_applied_artifact_id=lambda: 0,
        get_mode=lambda: "strict",
        get_runtime_prompt=lambda _m: "prompt",
        artifact_contract=lambda _t, _tgt: {
            "family": "config",
            "apply_points": [],
            "invariants": [],
            "contract_version": "1",
            "contract_hash": "abc",
        },
        allowed_artifact_families=lambda: ["config"],
    )

    assert "non-actionable" not in result
    assert "Artifact" in result

    art = conn.execute(
        "SELECT artifact_type, target FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
    assert art["artifact_type"] == "config_change"
    assert art["target"] == "runtime_retry_policy"


def test_evolution_memory_capture_populates_v2_fields():
    conn = _make_db()
    get_db = _db_factory(conn)

    # Full pipeline: proposal + artifact + runs + promotion status
    conn.execute(
        """INSERT INTO improvement_proposals
        (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target,failure_class)
        VALUES('promoted','low',?,?,?,?,?,?,?)""",
        (
            "Stabilize failing route: run_shell",
            "Route 'run_shell' instability detected.",
            "Route friction.",
            "Add pre-validation.",
            "Lower rate.",
            "failures reduced >=40%",
            "route_failure",
        ),
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    conn.execute(
        """INSERT INTO change_artifacts(proposal_id,artifact_type,target,artifact_json,expected_metric,expected_delta,status,applied_ts)
        VALUES(?,?,?,?,?,?,'promoted',datetime('now'))""",
        (pid, "config_change", "runtime_retry_policy", json.dumps({"key": "exec_retry_limit", "value": "2"}), "failure_rate", -0.10),
    )
    conn.execute(
        "INSERT INTO improvement_runs(proposal_id,phase,status,before_json,after_json,notes) VALUES(?,?,?,?,?,?)",
        (pid, "simulation", "ok", "{}", "{}", ""),
    )
    conn.execute(
        "INSERT INTO improvement_runs(proposal_id,phase,status,before_json,after_json,notes) VALUES(?,?,?,?,?,?)",
        (
            pid, "impact_compare", "ok",
            json.dumps({"events": 50, "fail_rate": 12.0, "unknowns": 0, "avg_latency": 300}),
            json.dumps({"events": 50, "fail_rate": 8.0, "unknowns": 0, "avg_latency": 310}),
            "delta={\"fail_rate\": -4.0, \"unknowns\": 0.0, \"avg_latency\": 10.0}|confidence=high",
        ),
    )
    conn.execute(
        "INSERT INTO improvement_runs(proposal_id,phase,status,notes) VALUES(?,?,?,?)",
        (pid, "promotion", "ok", ""),
    )
    conn.commit()

    result = capture_evolution_memory_sync(proposal_id=pid, get_db=get_db)
    assert "Evolution memory captured" in result

    row = conn.execute(
        "SELECT failure_class, artifact_family, target_route, metric_delta, outcome FROM evolution_memory WHERE proposal_id=?",
        (pid,),
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "promoted"
    assert row["failure_class"] == "route_failure"
    assert row["artifact_family"] == "config"
    assert row["target_route"] == "run_shell"
    assert row["metric_delta"] is not None
    assert float(row["metric_delta"]) < 0  # failure rate went down


def test_impact_confidence_parsed_from_notes():
    conn = _make_db()
    get_db = _db_factory(conn)

    conn.execute(
        """INSERT INTO improvement_proposals
        (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target)
        VALUES('proposed','low','T','P','C','Ch','B','M')"""
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    # Write a LOW confidence impact compare run
    conn.execute(
        "INSERT INTO improvement_runs(proposal_id,phase,status,before_json,after_json,notes) VALUES(?,?,?,?,?,?)",
        (pid, "impact_compare", "ok", "{}", "{}",
         'delta={"fail_rate": -1.5}|confidence=low'),
    )
    conn.commit()

    assert latest_impact_confidence(proposal_id=pid, get_db=get_db) == "low"
    delta = latest_impact_delta(proposal_id=pid, get_db=get_db)
    assert abs(delta.get("fail_rate", 0) - (-1.5)) < 0.001

    # Write a HIGH confidence run
    conn.execute(
        "INSERT INTO improvement_runs(proposal_id,phase,status,before_json,after_json,notes) VALUES(?,?,?,?,?,?)",
        (pid, "impact_compare", "ok", "{}", "{}",
         'delta={"fail_rate": -2.0}|confidence=high'),
    )
    conn.commit()
    assert latest_impact_confidence(proposal_id=pid, get_db=get_db) == "high"

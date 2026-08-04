"""Unit tests for run_pipeline_sweep, the overnight compile->simulate->promote sweep.

Uses fake compile/simulate/promote callables (real ones are covered by
test_pipeline_e2e.py) so this stays focused on the sweep's selection logic:
which proposals it picks, how it counts outcomes, and that it always calls
auto_promote_low_sync exactly once regardless of batch contents.
"""
import sqlite3

from xobop.migrations import apply_migrations
from xobop.schema import ensure_core_schema
from xobop.improve_pipeline_ops import run_pipeline_sweep


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_):
        return False


def _db_factory(conn):
    return lambda: _Ctx(conn)


def _insert_proposal(conn, status="proposed"):
    conn.execute(
        """INSERT INTO improvement_proposals
        (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target)
        VALUES(?,?,?,?,?,?,?,?)""",
        (status, "low", "T", "P", "C", "Ch", "B", "M"),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def _insert_artifact(conn, proposal_id, status):
    conn.execute(
        "INSERT INTO change_artifacts(proposal_id,artifact_type,target,artifact_json,status) VALUES(?,?,?,?,?)",
        (proposal_id, "config_change", "runtime_retry_policy", "{}", status),
    )


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_core_schema(conn)
    apply_migrations(conn)
    return conn


def test_sweep_skips_proposals_already_sim_pass_or_applied():
    conn = _make_db()
    get_db = _db_factory(conn)

    pid_needs_work = _insert_proposal(conn)
    pid_sim_pass = _insert_proposal(conn)
    _insert_artifact(conn, pid_sim_pass, "sim_pass")
    pid_applied = _insert_proposal(conn)
    _insert_artifact(conn, pid_applied, "applied")
    conn.commit()

    seen = []

    def fake_compile(pid):
        seen.append(pid)
        return f"Artifact #1 compiled for proposal #{pid}: config_change -> runtime_retry_policy"

    def fake_simulate(pid):
        return f"Simulation for proposal #{pid}: PASS"

    result = run_pipeline_sweep(
        get_db=get_db,
        compile_proposal_to_change=fake_compile,
        simulate_proposal_sync=fake_simulate,
        auto_promote_low_sync=lambda: "Auto-promotion complete: promoted=0 blocked=0",
        max_proposals=10,
    )

    assert seen == [pid_needs_work]
    assert "compiled=1" in result
    assert "sim_pass=1" in result


def test_sweep_counts_non_actionable_compiles_as_skipped():
    conn = _make_db()
    get_db = _db_factory(conn)
    pid = _insert_proposal(conn)
    conn.commit()

    result = run_pipeline_sweep(
        get_db=get_db,
        compile_proposal_to_change=lambda pid: f"⛔ Proposal #{pid} is non-actionable: cannot compile to allowed artifact type.",
        simulate_proposal_sync=lambda pid: "Simulation for proposal #{}: PASS".format(pid),
        auto_promote_low_sync=lambda: "Auto-promotion complete: promoted=0 blocked=0",
        max_proposals=10,
    )

    assert "compiled=0" in result
    assert "compile_skipped=1" in result
    assert "sim_pass=0" in result


def test_sweep_respects_max_proposals_cap():
    conn = _make_db()
    get_db = _db_factory(conn)
    for _ in range(5):
        _insert_proposal(conn)
    conn.commit()

    seen = []
    run_pipeline_sweep(
        get_db=get_db,
        compile_proposal_to_change=lambda pid: (seen.append(pid), f"Artifact #1 compiled for proposal #{pid}: a -> b")[1],
        simulate_proposal_sync=lambda pid: f"Simulation for proposal #{pid}: PASS",
        auto_promote_low_sync=lambda: "Auto-promotion complete: promoted=0 blocked=0",
        max_proposals=2,
    )

    assert len(seen) == 2


def test_sweep_always_calls_auto_promote_once():
    conn = _make_db()
    get_db = _db_factory(conn)
    conn.commit()  # no proposals at all

    calls = []
    result = run_pipeline_sweep(
        get_db=get_db,
        compile_proposal_to_change=lambda pid: "unused",
        simulate_proposal_sync=lambda pid: "unused",
        auto_promote_low_sync=lambda: calls.append(1) or "Auto-promotion complete: promoted=0 blocked=0",
        max_proposals=10,
    )

    assert len(calls) == 1
    assert "Auto-promotion complete" in result

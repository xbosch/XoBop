"""LLM proposal path should see conditioned evolution-memory priors and the
human-edited priorities steering doc, and generate_improvement_proposal
should no longer discard the prior note from its output once the LLM path
succeeds (previously gated by `and not llm_used`).
"""
import json
import sqlite3

from xobop.improve_generate import (
    generate_improvement_proposal,
    llm_artifact_proposal_from_diagnosis,
)


def _db_ctx(conn):
    class _Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, exc_type, exc, tb):
            return False

    return lambda: _Ctx()


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE op_events (id INTEGER PRIMARY KEY, timestamp TEXT, route TEXT, status TEXT, event_type TEXT, command TEXT)")
    conn.execute("CREATE TABLE op_signals_daily (day TEXT, p95_latency_ms REAL)")
    conn.execute("""
    CREATE TABLE improvement_proposals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
      status TEXT, risk_level TEXT, title TEXT, problem_statement TEXT,
      suspected_cause TEXT, proposed_change TEXT, expected_benefit TEXT,
      metric_target TEXT, failure_class TEXT NOT NULL DEFAULT '', command TEXT
    )
    """)
    return conn


def test_llm_prompt_includes_prior_context_and_priorities():
    seen_prompts = []

    def fake_llm_complete(prompt: str) -> str:
        seen_prompts.append(prompt)
        return json.dumps({
            "title": "t", "problem": "p", "cause": "c", "change": "ch",
            "benefit": "b", "metric": "m", "risk": "low", "artifact_family": "config",
        })

    result = llm_artifact_proposal_from_diagnosis(
        diagnosis_report="some diagnosis",
        allowed_families=["config"],
        llm_complete=fake_llm_complete,
        prior_context="- config on run_shell (promoted, Δfail_rate=-2.00pp): fixed it",
        priorities_text="Focus on shell reliability this week.",
    )

    assert result is not None
    assert len(seen_prompts) == 1
    assert "fixed it" in seen_prompts[0]
    assert "Focus on shell reliability this week." in seen_prompts[0]


def test_generate_improvement_proposal_keeps_prior_note_when_llm_used():
    conn = _make_conn()
    get_db = _db_ctx(conn)

    conditioned_rows = [
        {"artifact_family": "config", "target_route": "run_shell", "outcome": "promoted", "metric_delta": -2.0, "lesson": "fixed it"},
    ]

    def fake_llm_complete(prompt: str) -> str:
        return json.dumps({
            "title": "LLM title", "problem": "p", "cause": "c", "change": "ch",
            "benefit": "b", "metric": "m", "risk": "low", "artifact_family": "config",
        })

    pid, out = generate_improvement_proposal(
        days=7,
        get_db=get_db,
        diagnose_report_fn=lambda _d: "Diagnosis: route failures on run_shell",
        llm_proposal_fn=fake_llm_complete,
        allowed_families_fn=lambda: ["config"],
        conditioned_evolution_rows=conditioned_rows,
    )

    assert pid >= 1
    assert "[llm]" in out
    assert "Prior:" in out  # previously suppressed whenever llm_used was True

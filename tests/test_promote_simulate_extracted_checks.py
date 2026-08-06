"""Focused unit tests for the guard/check functions extracted from
_promote_proposal_sync and _simulate_proposal_sync (xobop_core_tail.py)
during today's split of those two functions.

Only covers the functions that are genuinely pure given their explicit
parameters (no DB/subprocess/sandbox dependency) -- _promote_guard_impact_confidence,
_promote_guard_retry_net_benefit (both call out to DB-backed
_latest_impact_confidence/_latest_impact_delta), and _sim_check_compile/
_sim_check_eval_suite/_sim_check_shell_replay_controller (subprocess/sandbox)
are exercised instead by the existing pipeline test suite
(tests/test_pipeline_e2e.py) and by an end-to-end scratch-DB dry run, same
as before the split -- this was a pure extraction, not new logic.
"""
import xobop_core_tail as t


def _row(**kwargs):
    return dict(kwargs)


# ---------------------------------------------------------------------------
# _promote_proposal_sync guards
# ---------------------------------------------------------------------------

def test_guard_terminal_status_blocks_promoted_and_rejected():
    assert t._promote_guard_terminal_status(5, "promoted") == "Proposal #5 already promoted."
    assert t._promote_guard_terminal_status(5, "rejected") == "Proposal #5 already rejected."
    assert t._promote_guard_terminal_status(5, "proposed") is None


def test_guard_force_reason_requires_nonempty_reason():
    assert t._promote_guard_force_reason(True, "") is not None
    assert t._promote_guard_force_reason(True, "   ") is not None
    assert t._promote_guard_force_reason(True, "emergency fix") is None
    assert t._promote_guard_force_reason(False, "") is None


def test_guard_baseline_phase_blocks_unless_forced():
    assert t._promote_guard_baseline_phase("baseline", False) is not None
    assert t._promote_guard_baseline_phase("baseline", True) is None
    assert t._promote_guard_baseline_phase("improving", False) is None


def test_guard_simulation_gate_requires_ok():
    assert t._promote_guard_simulation_gate(None) is not None
    assert t._promote_guard_simulation_gate("error") is not None
    assert t._promote_guard_simulation_gate("ok") is None


def test_guard_manual_policy_blocks_unless_forced():
    assert t._promote_guard_manual_policy("manual", False) is not None
    assert t._promote_guard_manual_policy("manual", True) is None
    assert t._promote_guard_manual_policy("assisted", False) is None


def test_guard_risk_level_high_always_needs_force():
    assert t._promote_guard_risk_level("high", "auto_low", False) is not None
    assert t._promote_guard_risk_level("high", "auto_low", True) is None


def test_guard_risk_level_medium_needs_force_under_manual_or_assisted():
    assert t._promote_guard_risk_level("medium", "manual", False) is not None
    assert t._promote_guard_risk_level("medium", "assisted", False) is not None
    assert t._promote_guard_risk_level("medium", "auto_low", False) is None
    assert t._promote_guard_risk_level("medium", "manual", True) is None


def test_guard_prompt_artifact_always_needs_force():
    art = _row(artifact_type="prompt_change", target="runtime_prompt")
    assert t._promote_guard_prompt_artifact_force(art, False) is not None
    assert t._promote_guard_prompt_artifact_force(art, True) is None
    other = _row(artifact_type="config_change", target="runtime_retry_policy")
    assert t._promote_guard_prompt_artifact_force(other, False) is None


def test_guard_impact_regression_blocks_unless_forced():
    assert t._promote_guard_impact_regression("error", False) is not None
    assert t._promote_guard_impact_regression("error", True) is None
    assert t._promote_guard_impact_regression("ok", False) is None


# ---------------------------------------------------------------------------
# _simulate_proposal_sync checks
# ---------------------------------------------------------------------------

def test_sim_check_prompt_golden_replay_not_applicable_returns_none():
    art = _row(artifact_type="config_change", target="runtime_retry_policy", artifact_json="{}")
    assert t._sim_check_prompt_golden_replay(art) is None


def test_sim_check_prompt_golden_replay_passes_good_prompt():
    import json
    art = _row(
        artifact_type="prompt_change", target="runtime_prompt",
        artifact_json=json.dumps({"value": "Be safe and use tools carefully. " * 3}),
    )
    name, ok, note = t._sim_check_prompt_golden_replay(art)
    assert name == "prompt_golden_replay"
    assert ok is True


def test_sim_check_prompt_golden_replay_fails_short_prompt():
    import json
    art = _row(artifact_type="prompt_change", target="runtime_prompt", artifact_json=json.dumps({"value": "hi"}))
    name, ok, note = t._sim_check_prompt_golden_replay(art)
    assert ok is False
    assert "missing" in note


def test_sim_check_skill_policy_simulation_not_applicable_returns_none():
    art = _row(artifact_type="prompt_change", target="runtime_prompt", artifact_json="{}")
    assert t._sim_check_skill_policy_simulation(art) is None


def test_sim_check_replay_regression_passes_on_unmodified_router():
    name, ok, note = t._sim_check_replay_regression()
    assert name == "replay_regression"
    assert ok is True

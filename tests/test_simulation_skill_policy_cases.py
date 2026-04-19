from xobop.simulation.golden_traces import skill_policy_simulation_cases


def test_skill_policy_simulation_cases_present_and_cover_policy_key():
    cases = skill_policy_simulation_cases()
    assert cases and len(cases) >= 3
    key_case = next(c for c in cases if c.get("case_id") == "sp_allowed_policy_key")
    allowed = {str(x).lower() for x in key_case.get("allowed", [])}
    assert "max_calls_per_hour" in allowed
    assert "write_root" in allowed

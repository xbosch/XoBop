from xobop.improve import artifact_contract, apply_artifact_to_runtime_settings


def test_skill_policy_family_contract():
    c = artifact_contract("skill_policy_change", "runtime_skill_config")
    assert c["family"] == "skill_policy"
    assert "skill_policy_keys_allowed" in c["invariants"]


def test_apply_skill_policy_change_calls_setter():
    calls = []

    def _set_skill_policy(skill, key, value):
        calls.append((skill, key, value))

    out = apply_artifact_to_runtime_settings(
        "skill_policy_change",
        "runtime_skill_config",
        {"skill": "run_shell", "policy": "max_calls_per_hour", "value": "120"},
        get_command_aliases=lambda: {},
        set_command_aliases=lambda _v: None,
        set_exec_retry_limit=lambda _v: None,
        set_last_applied_artifact_id=lambda _v: None,
        set_runtime_prompt=lambda _m, _v: None,
        set_skill_policy=_set_skill_policy,
    )
    assert calls == [("run_shell", "max_calls_per_hour", "120")]
    assert "skill_policy[run_shell:max_calls_per_hour]=120" in out

from __future__ import annotations


def shell_replay_cases() -> list[dict]:
    return [
        {"case_id": "target_safe_ls", "case_class": "targeted", "command": "ls", "expect_ok": True},
        {"case_id": "target_safe_pwd", "case_class": "targeted", "command": "pwd", "expect_ok": True},
        {"case_id": "target_block_rm_root", "case_class": "targeted", "command": "rm -rf /", "expect_ok": False},
        {"case_id": "target_malformed", "case_class": "targeted", "command": "echo 'unterminated", "expect_ok": False},
    ]


def shell_golden_cases() -> list[dict]:
    return [
        {"case_id": "golden_safe", "command": "ls", "expect_ok": True},
        {"case_id": "golden_blocked", "command": "rm -rf /", "expect_ok": False},
        {"case_id": "golden_malformed", "command": "echo 'unterminated", "expect_ok": False},
        {"case_id": "golden_restricted", "command": "mkfs.ext4 /dev/sda", "expect_ok": False},
        {"case_id": "golden_known_route", "command": "git status", "expect_ok": True},
    ]


def skill_policy_simulation_cases() -> list[dict]:
    return [
        {
            "case_id": "sp_non_empty_skill",
            "type": "non_empty",
            "field": "skill",
            "expect_ok": True,
        },
        {
            "case_id": "sp_non_empty_value",
            "type": "non_empty",
            "field": "value",
            "expect_ok": True,
        },
        {
            "case_id": "sp_allowed_policy_key",
            "type": "allowed_values",
            "field": "policy",
            "allowed": ["max_calls_per_hour", "allow_domains", "write_root", "default_cwd", "allow_raw_shell"],
            "expect_ok": True,
        },
    ]

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

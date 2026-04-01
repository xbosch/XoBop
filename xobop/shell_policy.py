from __future__ import annotations


def classify_shell(command: str, *, destructive_patterns, write_patterns, read_patterns, timeout_read: int, timeout_write: int, policy_cls):
    cmd = command.strip()
    for p in destructive_patterns:
        if p.search(cmd):
            return policy_cls("destructive", True, timeout_write)
    for p in write_patterns:
        if p.search(cmd):
            return policy_cls("write", False, timeout_write)
    for p in read_patterns:
        if p.search(cmd):
            return policy_cls("read", False, timeout_read)
    return policy_cls("unknown", False, timeout_read)


def user_forced(query: str, force_re) -> bool:
    return bool(force_re.search(query))

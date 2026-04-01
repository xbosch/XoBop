from __future__ import annotations


def parse_int_or_default(raw: str, default: int) -> int:
    try:
        return int(raw) if raw else default
    except Exception:
        return default

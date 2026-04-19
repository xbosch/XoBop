from __future__ import annotations

from pathlib import Path


def count_entries(path: str) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    return sum(1 for _ in p.iterdir())

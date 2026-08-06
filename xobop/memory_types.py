from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MemoryItem:
    id: int
    text: str
    mem_type: str
    importance: float
    created_ts: str
    last_access_ts: str

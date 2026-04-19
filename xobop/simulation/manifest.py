from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any


@dataclass
class IntentManifest:
    trace_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, effect: dict[str, Any]) -> None:
        row = dict(effect)
        row["ts"] = time()
        self.entries.append(row)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        blocked = 0
        for e in self.entries:
            k = str(e.get("type", "unknown"))
            counts[k] = counts.get(k, 0) + 1
            if e.get("blocked"):
                blocked += 1
        return {
            "trace_id": self.trace_id,
            "effects": counts,
            "blocked": blocked,
            "side_effects_intercepted": len(self.entries),
            "live_side_effects": 0,
        }

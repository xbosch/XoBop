from __future__ import annotations

import json
from typing import Callable


def get_bg_failure_count(*, get_setting: Callable[[str, str], str], label: str) -> int:
    try:
        raw = get_setting("bg_failure_counts_json", "{}") or "{}"
        data = json.loads(raw)
        if not isinstance(data, dict):
            return 0
        return int(data.get(label, 0) or 0)
    except Exception:
        return 0


def set_bg_failure_count(*, get_setting: Callable[[str, str], str], set_setting: Callable[[str, str], None], label: str, value: int) -> None:
    try:
        raw = get_setting("bg_failure_counts_json", "{}") or "{}"
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data[str(label)] = int(value)
    if len(data) > 128:
        for k in sorted(list(data.keys()))[:-128]:
            data.pop(k, None)
    set_setting("bg_failure_counts_json", json.dumps(data, sort_keys=True))

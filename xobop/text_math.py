from __future__ import annotations

import math
import re
from datetime import datetime, timezone


def split_words(text: str, stopwords: set[str]) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9_]+", text.lower()) if w not in stopwords]


def deterministic_hash(token: str) -> int:
    h = 2166136261
    for ch in token:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def hours_since(ts_text: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return 99999.0
    now = datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def recency_score(ts_text: str) -> float:
    hours = hours_since(ts_text)
    return max(0.0, min(1.0, math.exp(-hours / (24.0 * 14.0))))

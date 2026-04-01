from __future__ import annotations

import math
from collections import Counter
from typing import Callable


def fallback_embedding(
    text: str,
    *,
    dims: int,
    split_words: Callable[[str], list[str]],
    deterministic_hash: Callable[[str], int],
) -> list[float]:
    vec = [0.0] * dims
    counts = Counter(split_words(text))
    if not counts:
        return vec
    for tok, count in counts.items():
        idx = deterministic_hash(tok) % dims
        sign = -1.0 if (deterministic_hash(tok + "!") % 2) else 1.0
        vec[idx] += sign * float(count)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]

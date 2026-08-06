from __future__ import annotations

from xobop.text_math import cosine


def find_best_match(embedding: list[float], candidates: list[tuple[int, list[float]]], threshold: float) -> int | None:
    """Return the id of the closest candidate if it clears threshold, else None."""
    best_id: int | None = None
    best_score = -1.0
    for cid, cand_emb in candidates:
        score = cosine(embedding, cand_emb)
        if score > best_score:
            best_score = score
            best_id = cid
    if best_id is not None and best_score >= threshold:
        return best_id
    return None


def cluster_duplicates(items: list[dict], threshold: float) -> dict[int, list[int]]:
    """Greedily cluster near-duplicate embeddings.

    items: dicts with "id" and "embedding", pre-ordered best-first (the
    ordering decides which entry in a cluster survives). Returns
    {survivor_id: [merged_id, ...]} for every item, including singletons
    (empty merged list).
    """
    survivors: list[dict] = []
    clusters: dict[int, list[int]] = {}
    for item in items:
        match = None
        for s in survivors:
            if cosine(item["embedding"], s["embedding"]) >= threshold:
                match = s
                break
        if match is None:
            survivors.append(item)
            clusters[item["id"]] = []
        else:
            clusters[match["id"]].append(item["id"])
    return clusters

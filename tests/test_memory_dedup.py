"""Unit tests for the pure memory-dedup helpers in xobop/memory_dedup.py.

These are the building blocks used by _add_memory's insert-time gate and by
/memdedup's retroactive sweep in xobop_core_base.py. Kept here as pure-logic
tests (no DB, no Telegram) since the real functions close over module
globals and aren't independently importable -- mirrors how
xobop/improve_pipeline_ops.py is tested separately from xobop_core_base.py.
"""
from xobop.memory_dedup import cluster_duplicates, find_best_match


def test_find_best_match_returns_none_when_nothing_clears_threshold():
    embedding = [1.0, 0.0, 0.0]
    candidates = [(1, [0.0, 1.0, 0.0]), (2, [0.0, 0.0, 1.0])]
    assert find_best_match(embedding, candidates, threshold=0.92) is None


def test_find_best_match_returns_id_of_closest_candidate_above_threshold():
    embedding = [1.0, 0.0, 0.0]
    candidates = [(1, [0.99, 0.01, 0.0]), (2, [1.0, 0.0, 0.0])]
    assert find_best_match(embedding, candidates, threshold=0.92) == 2


def test_find_best_match_empty_candidates():
    assert find_best_match([1.0, 0.0], [], threshold=0.92) is None


def test_cluster_duplicates_groups_near_identical_embeddings():
    items = [
        {"id": 1, "embedding": [1.0, 0.0, 0.0]},
        {"id": 2, "embedding": [1.0, 0.0, 0.0]},
        {"id": 3, "embedding": [0.0, 1.0, 0.0]},
    ]
    clusters = cluster_duplicates(items, threshold=0.92)
    assert clusters[1] == [2]
    assert clusters[3] == []
    assert 2 not in clusters  # merged into survivor 1, not its own cluster


def test_cluster_duplicates_no_duplicates_all_singletons():
    items = [
        {"id": 1, "embedding": [1.0, 0.0, 0.0]},
        {"id": 2, "embedding": [0.0, 1.0, 0.0]},
        {"id": 3, "embedding": [0.0, 0.0, 1.0]},
    ]
    clusters = cluster_duplicates(items, threshold=0.92)
    assert clusters == {1: [], 2: [], 3: []}


def test_cluster_duplicates_survivor_is_first_in_order():
    # Ordering (best-first) is the caller's responsibility; cluster_duplicates
    # just keeps whichever item it sees first as the survivor.
    items = [
        {"id": 10, "embedding": [1.0, 0.0]},
        {"id": 5, "embedding": [1.0, 0.0]},
    ]
    clusters = cluster_duplicates(items, threshold=0.92)
    assert clusters == {10: [5]}

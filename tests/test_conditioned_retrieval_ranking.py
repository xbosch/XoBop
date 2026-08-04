"""fetch_conditioned_evolution_async should rank proven-good priors above
neutral/bad ones when cosine similarity is tied, using metric_delta as a
bounded tie-breaking bonus (negative metric_delta = fail_rate went down =
improvement, matching update_learned_query_vector's existing sign convention).
"""
import asyncio
import json
import sqlite3

from xobop.migrations import apply_migrations
from xobop.schema import ensure_core_schema
from xobop.improve_pipeline_ops import fetch_conditioned_evolution_async


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_):
        return False


def _db_factory(conn):
    return lambda: _Ctx(conn)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_core_schema(conn)
    apply_migrations(conn)
    return conn


def _run(coro):
    return asyncio.run(coro)


async def _fixed_embed(_text):
    return [1.0, 0.0, 0.0], 3


def test_better_outcome_ranks_first_at_equal_similarity():
    conn = _make_db()
    get_db = _db_factory(conn)

    same_emb = json.dumps([1.0, 0.0, 0.0])
    conn.execute(
        "INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,failure_class,artifact_family,target_route,metric_delta,embedding_json) "
        "VALUES(1,'sig1','promoted','neutral fix','route_failure','config','run_shell',0.0,?)",
        (same_emb,),
    )
    conn.execute(
        "INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,failure_class,artifact_family,target_route,metric_delta,embedding_json) "
        "VALUES(2,'sig2','promoted','clear improvement','route_failure','config','run_shell',-2.0,?)",
        (same_emb,),
    )
    conn.commit()

    rows = _run(
        fetch_conditioned_evolution_async(
            diagnosis_text="route_failure on run_shell",
            get_db=get_db,
            embed_fn=_fixed_embed,
            limit=5,
            failure_class="",
        )
    )

    assert len(rows) == 2
    assert rows[0]["lesson"] == "clear improvement"


def test_regressed_outcome_ranks_below_neutral_at_equal_similarity():
    conn = _make_db()
    get_db = _db_factory(conn)

    same_emb = json.dumps([1.0, 0.0, 0.0])
    conn.execute(
        "INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,failure_class,artifact_family,target_route,metric_delta,embedding_json) "
        "VALUES(1,'sig1','promoted','neutral fix','route_failure','config','run_shell',0.0,?)",
        (same_emb,),
    )
    conn.execute(
        "INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,failure_class,artifact_family,target_route,metric_delta,embedding_json) "
        "VALUES(2,'sig2','regressed','made it worse','route_failure','config','run_shell',2.0,?)",
        (same_emb,),
    )
    conn.commit()

    rows = _run(
        fetch_conditioned_evolution_async(
            diagnosis_text="route_failure on run_shell",
            get_db=get_db,
            embed_fn=_fixed_embed,
            limit=5,
            failure_class="",
        )
    )

    assert len(rows) == 2
    assert rows[0]["lesson"] == "neutral fix"
    assert rows[1]["lesson"] == "made it worse"

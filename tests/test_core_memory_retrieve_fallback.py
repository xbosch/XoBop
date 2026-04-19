import asyncio

from xobop.core_memory_runtime import retrieve_memory_impl


class _Row(dict):
    pass


def test_retrieve_memory_falls_back_when_vectors_mismatch():
    called = {"lex": 0}

    def retrieve_memory_lexical(query, k=3):
        called["lex"] += 1
        return ["lexical"]

    async def embed_text(_q):
        return [0.1, 0.2, 0.3], "text-embedding-3-small"

    def memory_rows():
        # vector dimension mismatch on purpose
        return [_Row(id=1, text="a", mem_type="note", importance=0.5, created_ts="", last_access_ts="", embedding_json="[0.1,0.2]")]

    async def run():
        return await retrieve_memory_impl(
            get_db=lambda: None,
            query="hello",
            k=1,
            max_memory_results=5,
            embed_text=embed_text,
            retrieve_memory_lexical=retrieve_memory_lexical,
            memory_rows=memory_rows,
            cosine=lambda a, b: 0.9,
            recency_score=lambda _ts: 0.5,
        )

    out = asyncio.run(run())
    assert out == ["lexical"]
    assert called["lex"] == 1

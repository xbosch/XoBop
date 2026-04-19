import asyncio
import sqlite3

from xobop.core_memory_queue_runtime import (
    enqueue_memory_promotion,
    memory_promotion_prefilter_reason,
    promote_memory_now,
)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE memory_promotion_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            decision_reason TEXT,
            created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            picked_ts DATETIME,
            done_ts DATETIME
        )
        """
    )
    return conn


def test_prefilter_blocks_instruction_blob():
    t = "Step 1: create folder. Step 2: run python script. return exactly this output."
    assert memory_promotion_prefilter_reason(t) == "filtered_instruction_blob"


def test_enqueue_skips_instruction_blob():
    conn = _db()
    enqueue_memory_promotion(get_db=lambda: conn, text="Step 1: run python. Step 2: chmod +x run.sh", source="user")
    n = conn.execute("SELECT COUNT(*) AS n FROM memory_promotion_queue").fetchone()["n"]
    assert int(n) == 0


def test_promote_now_blocks_instruction_blob_even_if_called_directly():
    async def add_memory(*_a, **_k):
        raise AssertionError("should not add instruction blob")

    ok, reason = asyncio.run(
        promote_memory_now(
            text="Step 1) run bash script && chmod +x file",
            source="user",
            add_memory=add_memory,
            append_daily_memory=lambda _t: None,
            promote_long_memory=lambda _t: None,
        )
    )
    assert ok is False
    assert reason == "filtered_instruction_blob"

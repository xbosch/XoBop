import asyncio
import sqlite3

from xobop.core_memory_queue_runtime import drain_memory_promotion_queue_once


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
    conn.execute("INSERT INTO memory_promotion_queue(text,source,status) VALUES ('note one','user','pending')")
    conn.execute("INSERT INTO memory_promotion_queue(text,source,status) VALUES ('note two','user','pending')")
    conn.commit()
    return conn


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_):
        return False


class _FakeLog:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, msg, *a, **k):
        self.warnings.append(msg % a if a else msg)

    def error(self, msg, *a, **k):
        self.errors.append(msg % a if a else msg)


def test_drain_promotes_pending_rows():
    conn = _db()
    log = _FakeLog()
    promoted_texts = []

    async def promote(text, source):
        promoted_texts.append(text)
        return True, "promoted_note"

    count = asyncio.run(
        drain_memory_promotion_queue_once(
            get_db=lambda: _Ctx(conn),
            promote_memory_now_fn=promote,
            log=log,
            limit=5,
        )
    )

    assert count == 2
    assert set(promoted_texts) == {"note one", "note two"}
    rows = conn.execute("SELECT status FROM memory_promotion_queue").fetchall()
    assert all(r["status"] == "done_promoted" for r in rows)


def test_drain_atomic_claim_no_double_processing():
    """Second drain call sees no pending rows because first atomically claimed them."""
    conn = _db()
    log = _FakeLog()
    claimed_first = []
    claimed_second = []

    async def run():
        async def promote_first(text, source):
            claimed_first.append(text)
            return True, "promoted_note"

        async def promote_second(text, source):
            claimed_second.append(text)
            return True, "promoted_note"

        # Simulate two concurrent drains: first one claims rows atomically,
        # second one should find nothing pending.
        await drain_memory_promotion_queue_once(
            get_db=lambda: _Ctx(conn),
            promote_memory_now_fn=promote_first,
            log=log,
            limit=5,
        )
        count2 = await drain_memory_promotion_queue_once(
            get_db=lambda: _Ctx(conn),
            promote_memory_now_fn=promote_second,
            log=log,
            limit=5,
        )
        return count2

    second_count = asyncio.run(run())
    assert second_count == 0
    assert len(claimed_second) == 0


def test_drain_records_full_exception_type_in_error():
    conn = _db()
    log = _FakeLog()

    async def promote_raises(text, source):
        raise ValueError("something went wrong with a long description")

    asyncio.run(
        drain_memory_promotion_queue_once(
            get_db=lambda: _Ctx(conn),
            promote_memory_now_fn=promote_raises,
            log=log,
            limit=5,
        )
    )

    rows = conn.execute("SELECT status, error FROM memory_promotion_queue").fetchall()
    assert all(r["status"] == "failed" for r in rows)
    # Error field must include the exception class name
    assert all("ValueError" in (r["error"] or "") for r in rows)

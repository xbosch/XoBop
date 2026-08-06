import sqlite3

from xobop.migrations import apply_migrations
from xobop.schema import ensure_core_schema
from xobop.digest_ops import build_daily_digest


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


def test_empty_db_returns_none():
    conn = _make_db()
    get_db = _db_factory(conn)
    assert build_daily_digest(get_db=get_db) is None


def test_includes_reminders_due_soon():
    conn = _make_db()
    get_db = _db_factory(conn)
    conn.execute(
        "INSERT INTO reminders(chat_id,text,due_ts,status) VALUES (1,'take meds',datetime('now','+2 hour'),'pending')"
    )
    conn.commit()

    digest = build_daily_digest(get_db=get_db)
    assert digest is not None
    assert "Reminders due soon" in digest
    assert "take meds" in digest


def test_excludes_far_future_reminders():
    conn = _make_db()
    get_db = _db_factory(conn)
    conn.execute(
        "INSERT INTO reminders(chat_id,text,due_ts,status) VALUES (1,'next year',datetime('now','+400 day'),'pending')"
    )
    conn.commit()
    assert build_daily_digest(get_db=get_db) is None


def test_includes_important_stale_memories():
    conn = _make_db()
    get_db = _db_factory(conn)
    conn.execute(
        "INSERT INTO memory_items(text,mem_type,importance,pinned,last_access_ts) "
        "VALUES ('critical fact','note',0.9,0,datetime('now','-30 day'))"
    )
    conn.commit()

    digest = build_daily_digest(get_db=get_db)
    assert digest is not None
    assert "critical fact" in digest


def test_excludes_recently_accessed_or_low_importance_memories():
    conn = _make_db()
    get_db = _db_factory(conn)
    conn.execute(
        "INSERT INTO memory_items(text,mem_type,importance,pinned,last_access_ts) "
        "VALUES ('recent important','note',0.9,0,datetime('now'))"
    )
    conn.execute(
        "INSERT INTO memory_items(text,mem_type,importance,pinned,last_access_ts) "
        "VALUES ('stale unimportant','note',0.3,0,datetime('now','-30 day'))"
    )
    conn.commit()
    assert build_daily_digest(get_db=get_db) is None


def test_includes_pending_proposal_count_and_last_sweep():
    conn = _make_db()
    get_db = _db_factory(conn)
    conn.execute(
        """INSERT INTO improvement_proposals
        (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target)
        VALUES('proposed','low','T','P','C','Ch','B','M')"""
    )
    conn.execute(
        "INSERT INTO cron_runs(job_name,status,duration_ms,output,error) VALUES ('improve-pipeline-sweep-nightly','ok',100,'Pipeline sweep: considered=1 compiled=1','')"
    )
    conn.commit()

    digest = build_daily_digest(get_db=get_db)
    assert digest is not None
    assert "1 proposal(s) pending review" in digest
    assert "Pipeline sweep: considered=1" in digest

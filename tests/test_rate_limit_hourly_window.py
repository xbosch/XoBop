"""Tests for the lookback_minutes parameter added to runtime_ops.enforce_rate_limit,
which activates skill_policies.max_calls_per_hour -- previously fully plumbed on
the write side (self-improve artifact pipeline) but never enforced at runtime.
"""
import sqlite3

from xobop.migrations import apply_migrations
from xobop.runtime_ops import enforce_rate_limit
from xobop.schema import ensure_core_schema


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_):
        return False


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_core_schema(conn)
    apply_migrations(conn)
    return lambda: _Ctx(conn), conn


def test_default_lookback_is_one_minute_unchanged():
    get_db, conn = _make_db()
    conn.execute("INSERT INTO rate_events(bucket,weight,timestamp) VALUES ('tool_calls',1,datetime('now','-30 second'))")
    conn.commit()
    ok, _ = enforce_rate_limit(get_db, "tool_calls", limit=1)
    assert ok is False  # existing event within the last minute already at the limit


def test_hourly_window_counts_events_a_minute_limit_would_miss():
    get_db, conn = _make_db()
    # 10 minutes old -- outside the default 1-minute window, inside a 60-minute one.
    conn.execute("INSERT INTO rate_events(bucket,weight,timestamp) VALUES ('skill_hourly:browse_page',1,datetime('now','-10 minutes'))")
    conn.commit()

    ok_default, _ = enforce_rate_limit(get_db, "skill_hourly:browse_page", limit=1)
    assert ok_default is True  # 1-minute window: the old event doesn't count

    ok_hourly, msg = enforce_rate_limit(get_db, "skill_hourly:browse_page", limit=1, lookback_minutes=60)
    assert ok_hourly is False
    assert "last 60 minutes" in msg


def test_hourly_cap_allows_calls_under_the_limit():
    get_db, conn = _make_db()
    for _ in range(3):
        conn.execute("INSERT INTO rate_events(bucket,weight,timestamp) VALUES ('skill_hourly:run_shell',1,datetime('now','-5 minutes'))")
    conn.commit()

    ok, _ = enforce_rate_limit(get_db, "skill_hourly:run_shell", limit=5, lookback_minutes=60)
    assert ok is True


def test_events_older_than_lookback_window_dont_count():
    get_db, conn = _make_db()
    conn.execute("INSERT INTO rate_events(bucket,weight,timestamp) VALUES ('skill_hourly:browse_page',1,datetime('now','-90 minutes'))")
    conn.commit()

    ok, _ = enforce_rate_limit(get_db, "skill_hourly:browse_page", limit=1, lookback_minutes=60)
    assert ok is True

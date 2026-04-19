"""Tests for /improve_list limit, /improve_reject, /improve_set_retry, threshold alignment."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from xobop.commands_improve import (
    cmd_improve_list_impl,
    cmd_improve_reject_impl,
    cmd_improve_set_retry_impl,
)
from xobop.improve_generate import generate_improvement_proposal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeUpdate:
    def __init__(self, text):
        class _Msg:
            pass
        self.message = _Msg()
        self.message.text = text
        self.effective_chat = type("C", (), {"id": 1})()


class _Ctx:
    def __init__(self, conn):
        self._conn = conn
    def __enter__(self): return self._conn
    def __exit__(self, *_): return False


def _db_with_proposals(n=25):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE improvement_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            risk_level TEXT,
            title TEXT,
            problem_statement TEXT,
            suspected_cause TEXT,
            proposed_change TEXT,
            expected_benefit TEXT,
            metric_target TEXT
        )
    """)
    for i in range(n):
        conn.execute(
            "INSERT INTO improvement_proposals(status,risk_level,title) VALUES(?,?,?)",
            ("proposed", "low", f"Proposal {i+1}"),
        )
    conn.commit()
    return conn


def _run(coro): return asyncio.run(coro)


def _replies_for(update, impl_fn, **kwargs):
    replies = []
    async def run():
        await impl_fn(
            update=update,
            owner_only=lambda _u: True,
            reply=lambda _u, msg: replies.append(msg) or asyncio.sleep(0),
            **kwargs,
        )
    _run(run())
    return replies


# ---------------------------------------------------------------------------
# Fix 3 — /improve_list limit
# ---------------------------------------------------------------------------

def test_improve_list_default_limit_is_20():
    conn = _db_with_proposals(25)
    replies = _replies_for(_FakeUpdate("/improve_list"), cmd_improve_list_impl, get_db=lambda: _Ctx(conn))
    assert len(replies) == 1
    assert replies[0].count("\n") == 19  # 20 lines → 19 newlines


def test_improve_list_respects_explicit_limit():
    conn = _db_with_proposals(25)
    replies = _replies_for(_FakeUpdate("/improve_list 5"), cmd_improve_list_impl, get_db=lambda: _Ctx(conn))
    assert replies[0].count("\n") == 4  # 5 lines


def test_improve_list_cap_at_50():
    conn = _db_with_proposals(60)
    replies = _replies_for(_FakeUpdate("/improve_list 100"), cmd_improve_list_impl, get_db=lambda: _Ctx(conn))
    assert replies[0].count("\n") == 49  # capped at 50


# ---------------------------------------------------------------------------
# Fix 4 — /improve_reject
# ---------------------------------------------------------------------------

def test_improve_reject_sets_status():
    conn = _db_with_proposals(3)
    _replies_for(_FakeUpdate("/improve_reject 1 stale"), cmd_improve_reject_impl, get_db=lambda: _Ctx(conn))
    row = conn.execute("SELECT status FROM improvement_proposals WHERE id=1").fetchone()
    assert row["status"] == "rejected"


def test_improve_reject_reply_confirms():
    conn = _db_with_proposals(1)
    replies = _replies_for(_FakeUpdate("/improve_reject 1 old noise"), cmd_improve_reject_impl, get_db=lambda: _Ctx(conn))
    assert "rejected" in replies[0].lower()


def test_improve_reject_refuses_promoted():
    conn = _db_with_proposals(1)
    conn.execute("UPDATE improvement_proposals SET status='promoted' WHERE id=1")
    conn.commit()
    replies = _replies_for(_FakeUpdate("/improve_reject 1"), cmd_improve_reject_impl, get_db=lambda: _Ctx(conn))
    assert "promoted" in replies[0].lower()
    row = conn.execute("SELECT status FROM improvement_proposals WHERE id=1").fetchone()
    assert row["status"] == "promoted"  # unchanged


def test_improve_reject_not_found():
    conn = _db_with_proposals(1)
    replies = _replies_for(_FakeUpdate("/improve_reject 999"), cmd_improve_reject_impl, get_db=lambda: _Ctx(conn))
    assert "not found" in replies[0].lower()


# ---------------------------------------------------------------------------
# Fix 1 — /improve_set_retry
# ---------------------------------------------------------------------------

def test_improve_set_retry_stores_value():
    stored = {}
    exported = {}

    async def run():
        await cmd_improve_set_retry_impl(
            update=_FakeUpdate("/improve_set_retry 1"),
            owner_only=lambda _u: True,
            reply=lambda _u, msg: asyncio.sleep(0),
            get_exec_retry_limit=lambda: 3,
            set_exec_retry_limit=lambda v: stored.update({"v": v}),
            export_snapshot_fn=lambda: exported.update({"called": True}),
        )
    _run(run())
    assert stored["v"] == 1
    assert exported["called"] is True


def test_improve_set_retry_rejects_out_of_range():
    calls = []

    async def run():
        await cmd_improve_set_retry_impl(
            update=_FakeUpdate("/improve_set_retry 5"),
            owner_only=lambda _u: True,
            reply=lambda _u, msg: (calls.append(msg), asyncio.sleep(0))[1],
            get_exec_retry_limit=lambda: 3,
            set_exec_retry_limit=lambda v: None,
            export_snapshot_fn=lambda: None,
        )
    _run(run())
    assert calls  # replied with usage hint
    assert "0-3" in calls[0]


# ---------------------------------------------------------------------------
# Fix 2 — failure_class threshold alignment
# ---------------------------------------------------------------------------

def _minimal_db_with_events(unknown_count: int):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE op_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT, route TEXT, command TEXT,
            status TEXT, latency_ms INTEGER, retries INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE op_signals_daily (
            day TEXT PRIMARY KEY,
            events_total INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            command_unknown INTEGER NOT NULL DEFAULT 0,
            retries_total INTEGER NOT NULL DEFAULT 0,
            median_latency_ms REAL NOT NULL DEFAULT 0,
            p95_latency_ms REAL NOT NULL DEFAULT 0,
            updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE improvement_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT DEFAULT 'proposed',
            risk_level TEXT DEFAULT 'low',
            title TEXT,
            problem_statement TEXT,
            suspected_cause TEXT,
            proposed_change TEXT,
            expected_benefit TEXT,
            metric_target TEXT,
            failure_class TEXT DEFAULT '',
            created_ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Insert total events to keep rate low
    for _ in range(500):
        conn.execute("INSERT INTO op_events(event_type,status,route) VALUES('route_selected','ok','cmd_status')")
    # Insert unknown command events
    for _ in range(unknown_count):
        conn.execute("INSERT INTO op_events(event_type,status,command) VALUES('unknown_command','error','/statu')")
    conn.commit()
    return conn


def test_failure_class_count3_does_not_trigger_without_diagnosis():
    """3 unknown events (below diagnose_report threshold) should NOT yield unknown_command class."""
    conn = _minimal_db_with_events(3)
    _pid, out = generate_improvement_proposal(
        days=7,
        get_db=lambda: _Ctx(conn),
        diagnose_report_fn=lambda _days: "System healthy. No discoverability issues. Latency nominal.",
    )
    assert "Failure class: unknown_command" not in out


def test_failure_class_follows_diagnosis_flag():
    """When diagnosis_text mentions 'unknown commands', class should be unknown_command."""
    conn = _minimal_db_with_events(2)
    _pid, out = generate_improvement_proposal(
        days=7,
        get_db=lambda: _Ctx(conn),
        diagnose_report_fn=lambda _days: "Warning: unknown commands detected above threshold.",
    )
    assert "Failure class: unknown_command" in out

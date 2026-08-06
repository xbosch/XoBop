"""Unit tests for check_due_reminders (xobop/reminders_ops.py), the pure
logic behind the reminders_check cron skill_type."""
import asyncio
import sqlite3

from xobop.reminders_ops import check_due_reminders


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
    conn.execute(
        """
        CREATE TABLE reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            due_ts DATETIME NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            notified_ts DATETIME
        )
        """
    )
    return conn


def _insert(conn, text, due_ts, status="pending"):
    conn.execute(
        "INSERT INTO reminders(chat_id,text,due_ts,status) VALUES (1,?,?,?)",
        (text, due_ts, status),
    )


def _run(coro):
    return asyncio.run(coro)


def test_only_past_due_pending_reminders_are_notified_and_marked_sent():
    conn = _make_db()
    get_db = _db_factory(conn)

    _insert(conn, "past due", "2026-01-01 00:00:00")
    _insert(conn, "future", "2099-01-01 00:00:00")
    _insert(conn, "already sent", "2026-01-01 00:00:00", status="sent")
    conn.commit()

    notified = []

    async def fake_notify(text):
        notified.append(text)

    result = _run(check_due_reminders(
        get_db=get_db,
        notify_owner=fake_notify,
        utcnow=lambda: __import__("datetime").datetime(2026, 8, 4, 12, 0, 0),
    ))

    assert notified == ["⏰ Reminder: past due"]
    assert "1 reminder(s) sent" in result

    rows = {r["text"]: r["status"] for r in conn.execute("SELECT text,status FROM reminders")}
    assert rows["past due"] == "sent"
    assert rows["future"] == "pending"
    assert rows["already sent"] == "sent"


def test_no_due_reminders_returns_none_due_message():
    conn = _make_db()
    get_db = _db_factory(conn)
    _insert(conn, "future", "2099-01-01 00:00:00")
    conn.commit()

    async def fake_notify(text):
        raise AssertionError("should not notify")

    result = _run(check_due_reminders(
        get_db=get_db,
        notify_owner=fake_notify,
        utcnow=lambda: __import__("datetime").datetime(2026, 8, 4, 12, 0, 0),
    ))

    assert result == "No reminders due."

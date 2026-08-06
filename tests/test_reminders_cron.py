"""Unit tests for check_due_reminders (xobop/reminders_ops.py), the pure
logic behind the reminders_check cron skill_type."""
import asyncio
import sqlite3

from xobop.migrations import apply_migrations
from xobop.schema import ensure_core_schema
from xobop.reminders_ops import check_due_reminders, MAX_REMINDER_ATTEMPTS


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


def _insert(conn, text, due_ts, status="pending", attempts=0):
    conn.execute(
        "INSERT INTO reminders(chat_id,text,due_ts,status,attempts) VALUES (1,?,?,?,?)",
        (text, due_ts, status, attempts),
    )


def _run(coro):
    return asyncio.run(coro)


def _utcnow():
    from datetime import datetime
    return datetime(2026, 8, 4, 12, 0, 0)


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

    result = _run(check_due_reminders(get_db=get_db, notify_owner=fake_notify, utcnow=_utcnow))

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

    result = _run(check_due_reminders(get_db=get_db, notify_owner=fake_notify, utcnow=_utcnow))
    assert result == "No reminders due."


def test_one_failing_reminder_does_not_block_the_rest_of_the_batch():
    conn = _make_db()
    get_db = _db_factory(conn)
    _insert(conn, "bad one", "2026-01-01 00:00:00")
    _insert(conn, "good one", "2026-01-01 00:00:01")
    conn.commit()

    notified = []

    async def flaky_notify(text):
        if "bad one" in text:
            raise RuntimeError("simulated Telegram failure")
        notified.append(text)

    result = _run(check_due_reminders(get_db=get_db, notify_owner=flaky_notify, utcnow=_utcnow))

    assert notified == ["⏰ Reminder: good one"]
    rows = {r["text"]: dict(r) for r in conn.execute("SELECT text,status,attempts,last_error FROM reminders")}
    assert rows["good one"]["status"] == "sent"
    assert rows["bad one"]["status"] == "pending"  # retried next tick, not stuck forever silently
    assert rows["bad one"]["attempts"] == 1
    assert "simulated Telegram failure" in rows["bad one"]["last_error"]


def test_reminder_marked_failed_after_max_attempts_with_fallback_alert():
    conn = _make_db()
    get_db = _db_factory(conn)
    _insert(conn, "chronic failure", "2026-01-01 00:00:00", attempts=MAX_REMINDER_ATTEMPTS - 1)
    conn.commit()

    notified = []

    async def always_fails(text):
        if text.startswith("⏰"):
            raise RuntimeError("still broken")
        notified.append(text)  # the fallback alert path

    result = _run(check_due_reminders(get_db=get_db, notify_owner=always_fails, utcnow=_utcnow))

    row = conn.execute("SELECT status, attempts FROM reminders WHERE text='chronic failure'").fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == MAX_REMINDER_ATTEMPTS
    assert any("failed repeatedly" in n for n in notified)
    assert "1 gave up" in result


def test_failed_reminder_is_not_retried_again():
    conn = _make_db()
    get_db = _db_factory(conn)
    _insert(conn, "already gave up", "2026-01-01 00:00:00", status="failed", attempts=MAX_REMINDER_ATTEMPTS)
    conn.commit()

    async def fake_notify(text):
        raise AssertionError("a failed reminder should never be retried")

    result = _run(check_due_reminders(get_db=get_db, notify_owner=fake_notify, utcnow=_utcnow))
    assert result == "No reminders due."

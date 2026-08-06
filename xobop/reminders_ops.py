from __future__ import annotations

from typing import Awaitable, Callable

MAX_REMINDER_ATTEMPTS = 5


async def check_due_reminders(*, get_db, notify_owner: Callable[[str], Awaitable[None]], utcnow) -> str:
    """Notify the owner for every pending reminder whose due_ts has passed, then mark it sent.

    Each reminder is isolated with its own try/except: one that fails to
    send (e.g. a transient Telegram API error) no longer blocks every other
    due reminder behind it in the batch. A reminder that keeps failing gets
    retried up to MAX_REMINDER_ATTEMPTS times (tracked via reminders.attempts),
    then marked 'failed' (terminal, stops retrying) with one best-effort
    short fallback alert so the owner isn't left silently unaware.
    """
    now_str = utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, text, attempts FROM reminders WHERE status='pending' AND due_ts <= ? ORDER BY due_ts ASC",
            (now_str,),
        ).fetchall()

    sent = 0
    failed = 0
    for r in rows:
        rid = int(r["id"])
        try:
            await notify_owner(f"⏰ Reminder: {r['text']}")
        except Exception as exc:
            attempts = int(r["attempts"] or 0) + 1
            with get_db() as conn:
                if attempts >= MAX_REMINDER_ATTEMPTS:
                    conn.execute(
                        "UPDATE reminders SET status='failed', attempts=?, last_error=? WHERE id=?",
                        (attempts, str(exc)[:500], rid),
                    )
                else:
                    conn.execute(
                        "UPDATE reminders SET attempts=?, last_error=? WHERE id=?",
                        (attempts, str(exc)[:500], rid),
                    )
            if attempts >= MAX_REMINDER_ATTEMPTS:
                failed += 1
                try:
                    await notify_owner(f"⚠️ Reminder #{rid} failed repeatedly, check /reminders")
                except Exception:
                    pass
            continue

        with get_db() as conn:
            conn.execute(
                "UPDATE reminders SET status='sent', notified_ts=CURRENT_TIMESTAMP WHERE id=?",
                (rid,),
            )
        sent += 1

    if not rows:
        return "No reminders due."
    return f"{sent} reminder(s) sent, {failed} gave up after {MAX_REMINDER_ATTEMPTS} attempts."

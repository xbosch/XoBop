from __future__ import annotations

from typing import Awaitable, Callable


async def check_due_reminders(*, get_db, notify_owner: Callable[[str], Awaitable[None]], utcnow) -> str:
    """Notify the owner for every pending reminder whose due_ts has passed, then mark it sent."""
    now_str = utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, text FROM reminders WHERE status='pending' AND due_ts <= ? ORDER BY due_ts ASC",
            (now_str,),
        ).fetchall()

    for r in rows:
        await notify_owner(f"⏰ Reminder: {r['text']}")
        with get_db() as conn:
            conn.execute(
                "UPDATE reminders SET status='sent', notified_ts=CURRENT_TIMESTAMP WHERE id=?",
                (int(r["id"]),),
            )

    return f"{len(rows)} reminder(s) sent." if rows else "No reminders due."

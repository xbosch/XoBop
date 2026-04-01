from __future__ import annotations

import asyncio
from typing import Callable


def record_rate_event(get_db: Callable, bucket: str, weight: int = 1) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO rate_events (bucket,weight) VALUES (?,?)",
            (bucket, weight),
        )
        conn.execute(
            "DELETE FROM rate_events WHERE timestamp < datetime('now', '-2 hours')"
        )


def count_rate_events(get_db: Callable, bucket: str, lookback_minutes: int = 1) -> int:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(weight), 0) AS total
            FROM rate_events
            WHERE bucket=? AND timestamp >= datetime('now', ?)
            """,
            (bucket, f"-{lookback_minutes} minutes"),
        ).fetchone()
    return int(row["total"] or 0)


def enforce_rate_limit(get_db: Callable, bucket: str, limit: int, weight: int = 1) -> tuple[bool, str]:
    current = count_rate_events(get_db, bucket)
    if current + weight > limit:
        return False, f"⛔ Rate limit exceeded for {bucket}: {current}/{limit} in last minute."
    record_rate_event(get_db, bucket, weight)
    return True, ""


def log_future_exception(log, fut: asyncio.Future, label: str) -> None:
    exc = fut.exception()
    if exc:
        log.error("Background task [%s] raised: %s", label, exc)


async def to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def fire_and_forget(loop: asyncio.AbstractEventLoop, fn, label: str, *, log) -> None:
    fut = loop.run_in_executor(None, fn)
    fut.add_done_callback(lambda f: log_future_exception(log, f, label))

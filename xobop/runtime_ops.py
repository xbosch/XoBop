from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
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


def enforce_rate_limit(get_db: Callable, bucket: str, limit: int, weight: int = 1, lookback_minutes: int = 1) -> tuple[bool, str]:
    current = count_rate_events(get_db, bucket, lookback_minutes=lookback_minutes)
    if current + weight > limit:
        window = "last minute" if lookback_minutes == 1 else f"last {lookback_minutes} minutes"
        return False, f"⛔ Rate limit exceeded for {bucket}: {current}/{limit} in {window}."
    record_rate_event(get_db, bucket, weight)
    return True, ""


_BG_FAILURE_COUNTS: OrderedDict[str, int] = OrderedDict()
_BG_FAILURE_COUNTS_MAX = 128
_PENDING_BG_FUTURES: set[asyncio.Future] = set()


def _normalize_bg_label(label: str) -> str:
    base = str(label or "").strip().lower()
    if not base:
        return "unknown"
    # Bound key cardinality: collapse dynamic suffixes after ':'
    return base.split(":", 1)[0]


def _touch_bg_counter(key: str, value: int) -> None:
    _BG_FAILURE_COUNTS[key] = int(value)
    _BG_FAILURE_COUNTS.move_to_end(key)
    while len(_BG_FAILURE_COUNTS) > _BG_FAILURE_COUNTS_MAX:
        _BG_FAILURE_COUNTS.popitem(last=False)


def log_future_exception(
    log,
    fut: asyncio.Future,
    label: str,
    *,
    on_repeated_failure=None,
    alert_threshold: int = 3,
    get_failure_count=None,
    set_failure_count=None,
) -> None:
    key = _normalize_bg_label(label)
    exc = fut.exception()
    if not exc:
        _touch_bg_counter(key, 0)
        if set_failure_count:
            try:
                set_failure_count(key, 0)
            except Exception:
                pass
        return

    persisted = 0
    if get_failure_count:
        try:
            persisted = int(get_failure_count(key) or 0)
        except Exception:
            persisted = 0
    count = max(int(_BG_FAILURE_COUNTS.get(key, 0)), persisted) + 1
    _touch_bg_counter(key, count)
    if set_failure_count:
        try:
            set_failure_count(key, count)
        except Exception:
            pass
    log.error("Background task [%s] raised (%d): %s", label, count, exc)
    if on_repeated_failure and count >= int(alert_threshold):
        try:
            on_repeated_failure(label, exc, count)
        except Exception:
            pass


async def to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def wait_for_background_futures(timeout_s: float = 2.0) -> None:
    if not _PENDING_BG_FUTURES:
        return
    pending = [f for f in list(_PENDING_BG_FUTURES) if not f.done()]
    if not pending:
        return
    try:
        await asyncio.wait(pending, timeout=max(0.0, float(timeout_s)))
    except Exception:
        pass


def fire_and_forget(
    loop: asyncio.AbstractEventLoop,
    fn,
    label: str,
    *,
    log,
    retries: int = 0,
    retry_delay_s: float = 0.25,
    on_repeated_failure=None,
    alert_threshold: int = 3,
    get_failure_count=None,
    set_failure_count=None,
) -> None:
    def _runner():
        attempts = int(retries) + 1
        last = None
        for i in range(attempts):
            try:
                return fn()
            except Exception as exc:
                last = exc
                if i + 1 < attempts:
                    time.sleep(float(retry_delay_s))
        raise last

    fut = loop.run_in_executor(None, _runner)
    _PENDING_BG_FUTURES.add(fut)

    def _done(f):
        _PENDING_BG_FUTURES.discard(f)
        log_future_exception(
            log,
            f,
            label,
            on_repeated_failure=on_repeated_failure,
            alert_threshold=alert_threshold,
            get_failure_count=get_failure_count,
            set_failure_count=set_failure_count,
        )

    fut.add_done_callback(_done)

from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable


def _looks_like_instruction_blob(text: str) -> bool:
    t = (text or "").lower()
    # Removed generic verbs ("run ", "execute ", "create ") — too common in legitimate notes
    imperative_hits = sum(1 for k in ["step 1", "step 2", "you must", "return exactly", "do not"] if k in t)
    shellish_hits = sum(1 for k in ["chmod", "pip install", "python ", "bash ", "#!/bin/", "&&", "||"] if k in t)
    has_code_fence = "```" in t
    # Match numbered list items at line/string start (e.g. "1. ", "1) ", "step 1. ")
    is_listed_procedure = bool(re.search(r"(?:^|\n)\s*(?:step\s+)?\d+[.)]\s+\S", t, re.MULTILINE))
    return imperative_hits >= 2 or shellish_hits >= 2 or (has_code_fence and (imperative_hits >= 1 or shellish_hits >= 1)) or is_listed_procedure


def memory_promotion_prefilter_reason(text: str) -> str:
    clean = (text or "").strip()
    if len(clean) < 24:
        return "too_short"
    if _looks_like_instruction_blob(clean):
        return "filtered_instruction_blob"
    lowered = clean.lower()
    if any(w in lowered for w in ["prefer", "always", "never", "remember", "important", "from now on"]):
        return "candidate_preference"
    if any(w in lowered for w in ["task", "todo", "deadline", "meeting"]):
        return "candidate_observation"
    if len(clean) > 180:
        return "candidate_note"
    return "filtered_low_importance"


def enqueue_memory_promotion(*, get_db: Callable, text: str, source: str = "user") -> None:
    clean = (text or "").strip()
    reason = memory_promotion_prefilter_reason(clean)
    if reason.startswith("filtered") or reason == "too_short":
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_promotion_queue(text,source,status,decision_reason) VALUES (?,?, 'pending', ?)",
            (clean, source or "user", reason),
        )


async def promote_memory_now(
    *,
    text: str,
    source: str,
    add_memory: Callable[..., Awaitable[None]],
    append_daily_memory: Callable[[str], None],
    promote_long_memory: Callable[[str], None],
) -> tuple[bool, str]:
    clean = (text or "").strip()
    if len(clean) < 24:
        return False, "too_short"
    if _looks_like_instruction_blob(clean):
        return False, "filtered_instruction_blob"
    lowered = clean.lower()
    importance = 0.45
    mem_type = "note"
    if any(w in lowered for w in ["prefer", "always", "never", "remember", "important", "from now on"]):
        importance = 0.85
        mem_type = "preference"
    elif any(w in lowered for w in ["task", "todo", "deadline", "meeting"]):
        importance = 0.7
        mem_type = "observation"
    elif len(clean) > 180:
        importance = 0.55
    else:
        return False, "filtered_low_importance"

    await add_memory(clean, mem_type=mem_type, importance=importance, source=source, pinned=False)
    append_daily_memory(clean)
    if mem_type in {"preference", "observation"} and importance >= 0.8:
        promote_long_memory(clean)
    return True, f"promoted_{mem_type}"


async def drain_memory_promotion_queue_once(
    *,
    get_db: Callable,
    promote_memory_now_fn: Callable[[str, str], Awaitable[tuple[bool, str]]],
    log,
    limit: int = 5,
) -> int:
    # Atomically claim pending rows: single UPDATE+RETURNING prevents concurrent
    # drain calls from picking the same item (SQLite serialises writes).
    with get_db() as conn:
        rows = conn.execute(
            """
            UPDATE memory_promotion_queue
            SET status='processing', picked_ts=CURRENT_TIMESTAMP
            WHERE id IN (
                SELECT id FROM memory_promotion_queue
                WHERE status='pending'
                ORDER BY id ASC
                LIMIT ?
            )
            RETURNING id, text, source
            """,
            (int(limit),),
        ).fetchall()

    done = 0
    for r in rows:
        qid = int(r["id"])
        try:
            promoted, reason = await promote_memory_now_fn(str(r["text"]), str(r["source"] or "user"))
            final_status = "done_promoted" if promoted else "done_filtered"
            with get_db() as conn:
                conn.execute(
                    "UPDATE memory_promotion_queue SET status=?,done_ts=CURRENT_TIMESTAMP,error=NULL,decision_reason=? WHERE id=?",
                    (final_status, reason, qid),
                )
            done += 1
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {str(exc)}"[:500]
            try:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE memory_promotion_queue SET status='failed',error=?,decision_reason='exception',done_ts=CURRENT_TIMESTAMP WHERE id=?",
                        (error_msg, qid),
                    )
            except Exception as db_exc:
                log.error("Failed to record promotion failure for queue id=%s: %s", qid, db_exc)
            log.warning("Could not promote memory [queue id=%s]: %s", qid, exc)
    return done


def cleanup_memory_promotion_queue(*, get_db: Callable, retention_days: int = 7) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM memory_promotion_queue WHERE status IN ('done_promoted','done_filtered','failed') AND done_ts < datetime('now', ?)",
            (f"-{int(retention_days)} day",),
        )


async def memory_promotion_loop(
    *,
    drain_once: Callable[[int], Awaitable[int]],
    cleanup: Callable[[int], None],
    log,
    sleep_seconds: int = 15,
) -> None:
    tick = 0
    while True:
        try:
            await drain_once(10)
            tick += 1
            if tick % 20 == 0:
                cleanup(7)
        except Exception as exc:
            log.warning("Memory promotion loop failed: %s", exc)
        await asyncio.sleep(int(sleep_seconds))

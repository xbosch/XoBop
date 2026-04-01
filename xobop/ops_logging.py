from __future__ import annotations

import json
from typing import Callable, Optional


def record_op_event(
    *,
    get_db: Callable,
    event_type: str,
    chat_id: int = 0,
    route: str = "",
    command: str = "",
    status: str = "ok",
    latency_ms: Optional[int] = None,
    retries: int = 0,
    metadata: Optional[dict] = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO op_events (chat_id,event_type,route,command,status,latency_ms,retries,metadata_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                int(chat_id),
                event_type,
                route,
                command,
                status,
                latency_ms,
                int(retries),
                json.dumps(metadata or {}),
            ),
        )


def record_run(
    *,
    get_db: Callable,
    redact: Callable[[str], str],
    text_limit: int,
    turn_id: str,
    chat_id: int,
    user_query: str,
    mode: str,
    tool_name: Optional[str],
    tool_args: Optional[dict],
    result: str,
    exit_code: Optional[int],
    duration_ms: int,
    status: str,
    artifact_path: Optional[str] = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO runs
              (chat_id,turn_id,user_query,mode,tool_name,tool_args,result,exit_code,duration_ms,status,artifact_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                chat_id,
                turn_id,
                redact(user_query),
                mode,
                tool_name,
                json.dumps(tool_args) if tool_args else None,
                redact(result[:text_limit]),
                exit_code,
                duration_ms,
                status,
                artifact_path,
            ),
        )


def update_skill_usage(*, get_db: Callable, skill_name: str, duration_ms: int, status: str) -> None:
    err_inc = 0 if status == "ok" else 1
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO skill_usage_stats (skill_name,call_count,error_count,total_duration_ms,last_used_ts)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(skill_name) DO UPDATE SET
              call_count=skill_usage_stats.call_count + 1,
              error_count=skill_usage_stats.error_count + ?,
              total_duration_ms=skill_usage_stats.total_duration_ms + ?,
              last_used_ts=CURRENT_TIMESTAMP
            """,
            (skill_name, 1, err_inc, duration_ms, err_inc, duration_ms),
        )

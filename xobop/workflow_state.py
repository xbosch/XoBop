from __future__ import annotations

import json
from typing import Callable


def save_workflow_state(*, get_db: Callable, chat_id: int, key: str, value) -> None:
    k = (key or "").strip()
    if not k:
        return
    with get_db() as conn:
        if value is None:
            conn.execute("DELETE FROM workflow_state WHERE chat_id=? AND key=?", (int(chat_id), k))
            return
        conn.execute(
            """
            INSERT INTO workflow_state(chat_id,key,value_json,updated_ts)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id,key)
            DO UPDATE SET value_json=excluded.value_json, updated_ts=CURRENT_TIMESTAMP
            """,
            (int(chat_id), k, json.dumps(value, sort_keys=True)),
        )


def load_workflow_state_by_key(*, get_db: Callable, key: str) -> dict[int, object]:
    k = (key or "").strip()
    if not k:
        return {}
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id,value_json FROM workflow_state WHERE key=?", (k,)).fetchall()
    out: dict[int, object] = {}
    for r in rows:
        try:
            out[int(r["chat_id"])] = json.loads(r["value_json"])
        except Exception:
            continue
    return out

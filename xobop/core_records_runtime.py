from __future__ import annotations

import json
from typing import Callable, Optional


def save_session_state(*, get_db: Callable, key: str, obj: dict) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (key,value,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, json.dumps(obj)),
        )


def load_session_state(*, get_db: Callable, key: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM sessions WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except Exception:
        return None


def get_fact_value(*, get_db: Callable, key: str) -> str:
    k = (key or "").strip().lower()
    if not k:
        return ""
    with get_db() as conn:
        row = conn.execute("SELECT value FROM facts WHERE key=?", (k,)).fetchone()
    return str(row["value"]) if row and row["value"] is not None else ""


def save_fact(*, get_db: Callable, key: str, value: str, source: str = "") -> str:
    k, v = key.lower().strip(), value.strip()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO facts (key,value,source,timestamp) VALUES (?,?,?,CURRENT_TIMESTAMP)",
            (k, v, source),
        )
    return f"✓ Saved: *{k}* = {v}"


def delete_fact(*, get_db: Callable, key: str) -> str:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM facts WHERE key=?", (key.lower().strip(),))
    return f"✓ Deleted: {key}" if cur.rowcount else f"Not found: {key}"


def list_facts(*, get_db: Callable) -> str:
    with get_db() as conn:
        rows = conn.execute("SELECT key,value,source FROM facts ORDER BY timestamp DESC").fetchall()
    return "\n".join(
        f"• *{r['key']}*: {r['value']}" + (f" _(src:{r['source']})_" if r["source"] else "")
        for r in rows
    ) or "No facts."


def add_task(*, get_db: Callable, desc: str, priority: str = "normal", source: str = "") -> str:
    d = desc.strip()
    p = priority.lower().strip() if priority.lower().strip() in {"low", "normal", "high"} else "normal"
    if not d:
        return "Need description."
    with get_db() as conn:
        conn.execute("INSERT INTO tasks (description,priority,source) VALUES (?,?,?)", (d, p, source))
    return f"✓ Task added [{p}]: {d}"


def close_task(*, get_db: Callable, task_id: int) -> str:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='done',done_ts=CURRENT_TIMESTAMP WHERE id=? AND status='open'",
            (task_id,),
        )
    return f"✓ Task #{task_id} closed." if cur.rowcount else f"Task #{task_id} not found/open."


def list_tasks(*, get_db: Callable) -> str:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id,description,priority FROM tasks WHERE status='open'
            ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, timestamp
            """
        ).fetchall()
    return "\n".join(f"#{r['id']} [{r['priority']}] {r['description']}" for r in rows) or "No open tasks."

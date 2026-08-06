from __future__ import annotations

from typing import Callable


def build_daily_digest(*, get_db: Callable) -> str | None:
    """Build the proactive morning digest, or None if there's nothing worth reporting."""
    sections: list[str] = []

    with get_db() as conn:
        due_rows = conn.execute(
            "SELECT text, due_ts FROM reminders WHERE status='pending' AND due_ts <= datetime('now','+1 day') "
            "ORDER BY due_ts ASC LIMIT 10"
        ).fetchall()
    if due_rows:
        lines = "\n".join(f"- {r['due_ts']}: {r['text']}" for r in due_rows)
        sections.append(f"⏰ Reminders due soon:\n{lines}")

    with get_db() as conn:
        mem_rows = conn.execute(
            "SELECT text FROM memory_items WHERE is_deleted=0 AND importance >= 0.8 "
            "AND last_access_ts <= datetime('now','-14 day') ORDER BY importance DESC LIMIT 5"
        ).fetchall()
    if mem_rows:
        lines = "\n".join(f"- {r['text']}" for r in mem_rows)
        sections.append(f"🧠 Important memories you haven't touched in a while:\n{lines}")

    with get_db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM improvement_proposals WHERE status='proposed'"
        ).fetchone()["n"]
        last_sweep = conn.execute(
            "SELECT status, output FROM cron_runs WHERE job_name='improve-pipeline-sweep-nightly' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if pending or last_sweep:
        line = f"🔧 Self-improve: {pending} proposal(s) pending review"
        if last_sweep:
            first_line = (last_sweep["output"] or "").splitlines()[0] if last_sweep["output"] else ""
            line += f"; last sweep {last_sweep['status']}: {first_line}"
        sections.append(line)

    if not sections:
        return None
    return "☀️ Daily digest\n\n" + "\n\n".join(sections)

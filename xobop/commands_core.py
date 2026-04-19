from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable


def build_status_text(
    *,
    chat_id: int,
    model: str,
    embedding_model: str,
    get_mode: Callable[[], str],
    get_db: Callable,
) -> str:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM conversations WHERE chat_id=?", (chat_id,)).fetchone()["n"]
        recent = conn.execute("SELECT timestamp FROM conversations WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        open_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
        enabled_skills = conn.execute("SELECT COUNT(*) AS n FROM skills WHERE enabled=1").fetchone()["n"]
        cron_count = conn.execute("SELECT COUNT(*) AS n FROM cron_jobs WHERE enabled=1").fetchone()["n"]
        memory_count = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()["n"]
    last = recent["timestamp"] if recent else "never"
    proc_name = os.path.basename(sys.argv[0] or "python")
    return (
        f"• PID: {os.getpid()} ({proc_name})\n"
        f"• Messages: {total}\n"
        f"• Last activity: {last}\n"
        f"• Model: {model}\n"
        f"• Embedding model: {embedding_model}\n"
        f"• Open tasks: {open_tasks}\n"
        f"• Memories: {memory_count}\n"
        f"• Enabled skills: {enabled_skills}\n"
        f"• Active cron jobs: {cron_count}\n"
        f"• Mode: {get_mode()}"
    )


def build_procinfo_text(*, token: str, get_mode: Callable[[], str], get_setting: Callable[[str, str], str]) -> str:
    argv_name = os.path.basename(sys.argv[0] or "python")
    try:
        with open("/proc/self/comm", "r", encoding="utf-8", errors="ignore") as fh:
            comm_name = fh.read().strip()
    except Exception:
        comm_name = argv_name
    bot_id = (token.split(":", 1)[0] if ":" in token else "unknown")
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        commit = "unknown"
    lock = get_setting("runtime_lock_json", "{}")
    return (
        f"proc: {comm_name}\n"
        f"argv0: {argv_name}\n"
        f"pid: {os.getpid()}\n"
        f"mode: {get_mode()}\n"
        f"bot_id: {bot_id}\n"
        f"commit: {commit}\n"
        f"runtime_lock: {lock}"
    )


def build_attachdiag_text(*, path: str | None, diag: dict) -> str:
    if not path:
        return "No recent attachment in this chat."
    return (
        f"Attachment diag\n"
        f"• path: {path}\n"
        f"• extractor: {diag.get('extractor', 'n/a')}\n"
        f"• status: {diag.get('status', 'n/a')}\n"
        f"• mime: {diag.get('mime', 'n/a')}\n"
        f"• size: {diag.get('size', 'n/a')}\n"
        f"• preview_chars: {diag.get('preview_chars', 'n/a')}"
    )


def build_state_check_text(*, state_check: dict) -> str:
    if state_check.get("match"):
        snap = state_check.get("snapshot", {})
        return f"State check: ✅ match\nlast_applied_artifact_id: {int(snap.get('last_applied_artifact_id', 0))}"
    mism = state_check.get("mismatched", {})
    return f"State check: ❌ drift detected\nkeys: {', '.join(mism.keys()) if mism else 'unknown'}\nUse `/state_repair`."

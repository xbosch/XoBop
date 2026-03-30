from __future__ import annotations

import re
from typing import Literal, Optional, TypedDict


IntentName = Literal[
    "cron_list",
    "download_count",
    "email_send",
    "email_status",
    "execute_last",
    "git_commit_push",
    "direct_shell",
    "none",
]


class Intent(TypedDict):
    name: IntentName
    text: str


def classify(text: str) -> Intent:
    t = text.strip().lower()

    if _is_cron_list_query(t):
        return {"name": "cron_list", "text": text}
    if _is_download_count_query(t):
        return {"name": "download_count", "text": text}
    if _is_email_status_query(t):
        return {"name": "email_status", "text": text}
    if _is_email_send_query(t):
        return {"name": "email_send", "text": text}
    if _is_git_commit_push_query(t):
        return {"name": "git_commit_push", "text": text}
    if _is_execute_last_query(t):
        return {"name": "execute_last", "text": text}
    if _is_direct_shell_query(t):
        return {"name": "direct_shell", "text": text}
    return {"name": "none", "text": text}


def _is_cron_list_query(t: str) -> bool:
    if "/cronlist" in t or t == "cronlist":
        return True
    return ("cron" in t) and any(k in t for k in ["what", "show", "list", "again", "which", "tasks", "jobs"])


def _is_download_count_query(t: str) -> bool:
    return ("download" in t or "downloads" in t) and any(k in t for k in ["how many", "count", "files"])


def _is_email_send_query(t: str) -> bool:
    return ("send" in t) and ("email" in t or "mail" in t)


def _is_email_status_query(t: str) -> bool:
    return any(k in t for k in ["did you send", "was it sent", "email sent"])


def _is_execute_last_query(t: str) -> bool:
    return t in {"do it", "run it", "execute", "execute it", "yes do it", "please do it", "force"}


def _is_git_commit_push_query(t: str) -> bool:
    return t in {"add, commit and push", "add commit and push", "add commit push", "commit and push", "git add commit push"}


def _is_direct_shell_query(t: str) -> bool:
    # keep case-insensitive command prefix detection
    return bool(re.match(r"^(git|python3|sqlite3|ls|pwd|whoami|cat|cp|mv|mkdir|rm|find|grep|sed|awk|ps|top|du|df|systemctl|journalctl)\b", t.strip(), re.I))

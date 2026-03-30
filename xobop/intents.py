from __future__ import annotations

import re
from typing import Optional


def is_direct_shell_intent(text: str) -> bool:
    return bool(re.match(r"^(git|python3|sqlite3|ls|pwd|whoami|cat|cp|mv|mkdir|rm|find|grep|sed|awk|ps|top|du|df|systemctl|journalctl)\b", text, re.I))


def is_download_count_intent(text: str) -> bool:
    t = text.strip().lower()
    return ("download" in t or "downloads" in t) and ("how many" in t or "count" in t or "files" in t)


def is_execute_last_intent(text: str) -> bool:
    t = text.strip().lower()
    return t in {"do it", "run it", "execute", "execute it", "yes do it", "please do it", "force"}


def is_git_commit_push_intent(text: str) -> bool:
    t = text.strip().lower()
    return t in {
        "add, commit and push",
        "add commit and push",
        "add commit push",
        "commit and push",
        "git add commit push",
    }


def is_cron_list_intent(text: str) -> bool:
    t = text.strip().lower()
    cron_tokens = ["cron", "schedule", "scheduled", "tasks", "jobs"]
    question_tokens = ["what", "show", "list", "again", "which"]
    if any(a in t for a in ["/cronlist", "cronlist"]):
        return True
    if "cron" in t and any(q in t for q in question_tokens):
        return True
    if "scheduled" in t and "job" in t:
        return True
    if "cron tasks" in t or "cron jobs" in t:
        return True
    return any(c in t for c in cron_tokens) and any(q in t for q in question_tokens)


def extract_git_dir(cmd: str) -> Optional[str]:
    m = re.search(r"\bgit\s+-C\s+(\S+)", cmd)
    if m:
        return m.group(1)
    return None


def refers_to_last_attachment(text: str) -> bool:
    t = text.strip().lower()
    keys = ["use this", "that picture", "this picture", "this photo", "the picture", "the photo", "attach it", "attachment", "image"]
    return any(k in t for k in keys)


def is_email_intent(text: str) -> bool:
    t = text.strip().lower()
    return ("send" in t) and ("email" in t or "mail" in t)


def is_email_status_intent(text: str) -> bool:
    t = text.strip().lower()
    return "did you send" in t or "was it sent" in t or "email sent" in t


def extract_recipient_email(text: str) -> Optional[str]:
    t = text.strip().lower()
    m = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text)
    if m:
        return m.group(1)
    if "wife" in t or "neri" in t:
        return "neri1982@gmail.com"
    return None


def extract_email_body(text: str) -> str:
    m = re.search(r"\bsaying\b\s+(.*)$", text, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m2 = re.search(r"\bthat\b\s+(.*)$", text, re.I)
    if m2 and m2.group(1).strip():
        return m2.group(1).strip()
    return "I will be late."


def wants_attachment(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ["attach", "attachment", "photo", "picture", "image"])

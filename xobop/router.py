from __future__ import annotations

import os
import re
from typing import Literal, Optional, TypedDict


IntentName = Literal[
    "cron_list",
    "download_count",
    "lan_clients",
    "web_search",
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


_DEFAULT_DIRECT_SHELL_PREFIXES = [
    "git", "python3", "sqlite3", "ls", "pwd", "whoami", "cat", "cp", "mv",
    "mkdir", "rm", "find", "grep", "sed", "awk", "ps", "top", "du", "df",
    "systemctl", "journalctl",
]


def _load_direct_shell_prefixes() -> list[str]:
    raw = os.getenv("XOBOP_DIRECT_SHELL_PREFIXES", "").strip()
    if not raw:
        return list(_DEFAULT_DIRECT_SHELL_PREFIXES)
    parsed = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return parsed or list(_DEFAULT_DIRECT_SHELL_PREFIXES)


def _direct_shell_prefixes_pattern() -> re.Pattern[str]:
    prefixes = _load_direct_shell_prefixes()
    escaped = "|".join(re.escape(p) for p in prefixes)
    return re.compile(rf"^({escaped})\b", re.I)


def is_multiline_conversation(text: str) -> bool:
    raw = (text or "").strip()
    if "\n" not in raw:
        return False
    first = raw.splitlines()[0].strip().lower()
    if first.startswith("/"):
        return False
    if _is_direct_shell_query(first):
        return False
    if _is_email_send_query(first) or _is_email_status_query(first):
        return False
    return True


def classify(text: str) -> Intent:
    t = text.strip().lower()

    if _is_cron_list_query(t):
        return {"name": "cron_list", "text": text}
    if _is_download_count_query(t):
        return {"name": "download_count", "text": text}
    if _is_lan_clients_query(t):
        return {"name": "lan_clients", "text": text}
    if _is_web_search_query(t):
        return {"name": "web_search", "text": text}
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


def _is_lan_clients_query(t: str) -> bool:
    lan_tokens = ["lan", "network", "devices", "clients", "mac", "hostname", "ip"]
    ask_tokens = ["show", "list", "discover", "found", "discovered", "all"]
    return any(k in t for k in lan_tokens) and any(k in t for k in ask_tokens)


def _is_web_search_query(t: str) -> bool:
    web_tokens = ["web", "internet", "online"]
    search_tokens = ["find", "search", "lookup", "look up"]
    return any(w in t for w in web_tokens) and any(s in t for s in search_tokens)


def _is_email_send_query(t: str) -> bool:
    s = t.strip()
    # High-confidence only: avoid accidental side-effects on general text.
    if re.match(r"^(please\s+)?send\s+(an\s+)?(email|mail)\b", s):
        return True
    if re.match(r"^(email|mail)\s+", s):
        return True
    if re.search(r"\bsend\s+(an\s+)?(email|mail)\s+to\b", s):
        return True
    return False


def _is_email_status_query(t: str) -> bool:
    return any(k in t for k in ["did you send", "was it sent", "email sent"])


def _is_execute_last_query(t: str) -> bool:
    # Keep explicit execution intents only; avoid hijacking generic confirmations like "do it".
    return t in {"run it", "execute", "execute it", "rerun last", "force"}


def _is_git_commit_push_query(t: str) -> bool:
    return t in {"add, commit and push", "add commit and push", "add commit push", "commit and push", "git add commit push"}


def _is_direct_shell_query(t: str) -> bool:
    s = t.strip()
    # Avoid accidental shell routing for natural-language requests starting with "find ...".
    if re.match(r"^find\s+(all|info|information|details)\b", s, re.I):
        return False
    # case-insensitive command prefix detection with env-configurable prefix list.
    return bool(_direct_shell_prefixes_pattern().match(s))

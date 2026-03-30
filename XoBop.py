from __future__ import annotations

"""
XaviExecutor V5
March 2026

Single-user personal execution system with:
- async Telegram runtime
- DB-backed skills and cron jobs
- persistent structured memory
- semantic memory with embedding retrieval
- global policy enforcement and rate limiting
- skill usage metrics and health reporting
- safer filesystem boundaries

Design rule
-----------
The database defines state, skills, policies, and schedules.
Python defines the trusted execution engine.
This version remains single user by design.
"""

import asyncio
import json
import logging
import math
import os
import re
import shlex
import shutil
import signal
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from croniter import croniter
from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from xobop.state import STATE
from xobop.capabilities_shell import exec_shell_command
from xobop.db import db_session, sqlite_online_backup
from xobop.handlers_runtime import process_media_message, process_text_message

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("xavi")
# Reduce noisy/sensitive transport logs (can leak bot token in URL paths)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# =============================================================================
# Configuration
# =============================================================================

load_dotenv()


def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


TOKEN = _require_env("TELEGRAM_TOKEN")
OPENAI_KEY = _require_env("OPENAI_API_KEY")
OWNER_ID = int(_require_env("OWNER_ID"))

MODEL = os.getenv("MODEL", "gpt-4.1-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
DB_FILE = os.getenv("DB_FILE", "memory.db")
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "strict").strip().lower()
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts")).resolve()
ALLOWED_WRITE_ROOT = Path(os.getenv("ALLOWED_WRITE_ROOT", os.getcwd())).resolve()
ALLOW_RAW_SHELL = os.getenv("ALLOW_RAW_SHELL", "0").strip() == "1"
READ_ROOT = Path(os.getenv("READ_ROOT", str(ALLOWED_WRITE_ROOT))).resolve()
EMBEDDING_FALLBACK = os.getenv("EMBEDDING_FALLBACK", "1").strip() == "1"
FILE_MEMORY_ROOT = Path(os.getenv("FILE_MEMORY_ROOT", str(ALLOWED_WRITE_ROOT))).resolve()
LONG_MEMORY_FILE = Path(os.getenv("LONG_MEMORY_FILE", str(FILE_MEMORY_ROOT / "MEMORY.md"))).resolve()
DAILY_MEMORY_DIR = Path(os.getenv("DAILY_MEMORY_DIR", str(FILE_MEMORY_ROOT / "memory"))).resolve()
DAILY_MEMORY_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_LOAD_LIMIT = 40
MAX_HISTORY = 80
TRIM_TO = 60
MAX_TOOL_ROUNDS = 8
TEXT_LIMIT = 12_000
ARTIFACT_TEXT_THRESHOLD = 8_000
RUNS_PREVIEW_LIMIT = 10
SHELL_TIMEOUT_READ = 45
SHELL_TIMEOUT_WRITE = 60
REQUEST_TIMEOUT = 15
MAX_BROWSE_BYTES = 2_000_000
MAX_READ_BYTES = 2_000_000
CRON_MAX_OUTPUT_CHARS = 3_000
MAX_CRONRUNS_LIMIT = 50
MAX_MEMORY_RESULTS = 5
MAX_MEMORY_ITEMS = 5_000
MAX_MEMORY_TEXT = 2_000
MAX_HEALTH_ERRORS = 50

ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

client = AsyncOpenAI(api_key=OPENAI_KEY)

# =============================================================================
# Policies
# =============================================================================

MODES = {"strict", "normal"}

_SECRET_RE = re.compile(
    r"(?:"
    r"API_KEY\s*=\s*\S+"
    r"|Bearer\s+\S+"
    r"|sk-[A-Za-z0-9]+"
    r"|xai-[A-Za-z0-9]+"
    r"|\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"
    r")",
    re.IGNORECASE,
)

FORCE_RE = re.compile(r"(^|\s)(force|/force|force:|forced)(\s|$)", re.IGNORECASE)

DESTRUCTIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"\brm\b", r"\bmv\b", r"\bchmod\b", r"\bchown\b",
    r"\bpkill\b", r"\bkill\b", r"\bkillall\b", r"\bdd\b",
    r"\bmkfs\b", r"\bmount\b", r"\bumount\b", r">\s*/",
    r"\btruncate\b", r"\bfind\b.*\b-delete\b",
]]
WRITE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"\becho\b.*>", r"\btee\b", r"\bcp\b", r"\bmkdir\b",
    r"\btouch\b", r"\bsed\b.*-i", r"\bperl\b.*-i", r"\bpython\b.*\s-c\s",
]]
READ_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"\bls\b", r"\bcat\b", r"\bhead\b", r"\btail\b", r"\bgrep\b",
    r"\bfind\b", r"\bpwd\b", r"\bwhoami\b", r"\bps\b",
    r"\bdf\b", r"\bdu\b", r"\bfile\b",
]]

_MODE_VISIBILITY_COL: dict[str, str] = {
    "strict": "visible_in_strict",
    "normal": "visible_in_normal",
}


@dataclass
class ShellPolicy:
    kind: str
    requires_force: bool
    timeout: int


@dataclass
class SkillDef:
    name: str
    description: str
    schema: dict[str, Any]
    handler_type: str
    risk_level: str
    timeout_sec: int
    requires_force: bool
    enabled: bool
    visible_in_strict: bool
    visible_in_normal: bool


@dataclass
class MemoryItem:
    id: int
    text: str
    mem_type: str
    importance: float
    created_ts: str
    last_access_ts: str


# =============================================================================
# Database
# =============================================================================

def get_db():
    return db_session(DB_FILE)


def _init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_chat_id_id ON conversations(chat_id, id);

            CREATE TABLE IF NOT EXISTS facts (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                source TEXT DEFAULT '',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                source TEXT DEFAULT '',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                done_ts DATETIME
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                chat_id INTEGER NOT NULL,
                user_query TEXT NOT NULL,
                mode TEXT NOT NULL,
                tool_name TEXT,
                tool_args TEXT,
                result TEXT,
                exit_code INTEGER,
                duration_ms INTEGER,
                status TEXT NOT NULL DEFAULT 'ok',
                artifact_path TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_chat_id_id ON runs(chat_id, id);
            CREATE INDEX IF NOT EXISTS idx_runs_status_ts ON runs(status, timestamp);

            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                schema_json TEXT NOT NULL,
                handler_type TEXT NOT NULL,
                risk_level TEXT NOT NULL DEFAULT 'normal',
                timeout_sec INTEGER NOT NULL DEFAULT 30,
                requires_force INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                visible_in_strict INTEGER NOT NULL DEFAULT 1,
                visible_in_normal INTEGER NOT NULL DEFAULT 1,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS skill_policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                policy_key TEXT NOT NULL,
                policy_value TEXT NOT NULL,
                UNIQUE(skill_name, policy_key)
            );

            CREATE TABLE IF NOT EXISTS skill_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                skill_name TEXT NOT NULL,
                action TEXT NOT NULL,
                note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS skill_usage_stats (
                skill_name TEXT PRIMARY KEY,
                call_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                total_duration_ms INTEGER NOT NULL DEFAULT 0,
                last_used_ts DATETIME
            );

            CREATE TABLE IF NOT EXISTS cron_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                cron_expr TEXT NOT NULL,
                skill_type TEXT NOT NULL,
                skill_args TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                alert_owner INTEGER NOT NULL DEFAULT 1,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_run_ts DATETIME,
                last_status TEXT DEFAULT 'never'
            );

            CREATE TABLE IF NOT EXISTS cron_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                output TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                mem_type TEXT NOT NULL DEFAULT 'note',
                importance REAL NOT NULL DEFAULT 0.5,
                source TEXT DEFAULT '',
                pinned INTEGER NOT NULL DEFAULT 0,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_access_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                access_count INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_memory_items_active ON memory_items(is_deleted, pinned, created_ts);

            CREATE TABLE IF NOT EXISTS memory_vectors (
                memory_id INTEGER PRIMARY KEY,
                embedding_json TEXT NOT NULL,
                model TEXT NOT NULL,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS global_policies (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS runtime_prompts (
                mode TEXT PRIMARY KEY,
                prompt_text TEXT NOT NULL,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                bucket TEXT NOT NULL,
                weight INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_rate_events_bucket_ts ON rate_events(bucket, timestamp);

            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notification_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                delivered INTEGER NOT NULL DEFAULT 0,
                delivered_ts DATETIME
            );
        """)
    _bootstrap_default_skills()
    _bootstrap_default_policies()
    _bootstrap_default_prompts()


# =============================================================================
# Settings, policies, and defaults
# =============================================================================

DEFAULT_GLOBAL_POLICIES: dict[str, str] = {
    "tool_calls_per_minute": "30",
    "shell_calls_per_minute": "8",
    "max_write_size": "200000",
    "blocked_write_suffixes": ".py,.sh,.env,.db,.sqlite,.sqlite3",
    "allowed_http_domains": "",
    "memory_cleanup_keep": str(MAX_MEMORY_ITEMS),
}

DEFAULT_SKILLS: list[dict[str, Any]] = [
    {
        "name": "run_shell",
        "description": (
            "Run a shell command. Uses argv with shell=False by default. "
            "Set raw=true to use shell=True if explicitly enabled. "
            "Supports optional cwd and timeout_override."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "raw": {"type": "boolean"},
                "timeout_override": {"type": "integer"},
            },
            "required": ["command"],
        },
        "handler_type": "shell_command",
        "risk_level": "high",
        "timeout_sec": SHELL_TIMEOUT_WRITE,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
        "policies": {
            "default_cwd": os.getcwd(),
            "allow_raw_shell": "1" if ALLOW_RAW_SHELL else "0",
            "allowed_commands": "",
        },
    },
    {
        "name": "browse_page",
        "description": "Fetch a URL and return plain text content with a byte cap.",
        "schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "handler_type": "http_get",
        "risk_level": "low",
        "timeout_sec": REQUEST_TIMEOUT,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
        "policies": {"max_bytes": str(MAX_BROWSE_BYTES)},
    },
    {
        "name": "read_file",
        "description": "Read a file under READ_ROOT with a byte cap.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler_type": "file_read",
        "risk_level": "low",
        "timeout_sec": 15,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
        "policies": {"read_root": str(READ_ROOT), "max_bytes": str(MAX_READ_BYTES)},
    },
    {
        "name": "write_file",
        "description": "Write content to a file under ALLOWED_WRITE_ROOT. Set overwrite=true to replace.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["path", "content"],
        },
        "handler_type": "file_write",
        "risk_level": "high",
        "timeout_sec": 30,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
        "policies": {"write_root": str(ALLOWED_WRITE_ROOT)},
    },
    {
        "name": "append_file",
        "description": "Append content to a file under ALLOWED_WRITE_ROOT.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        "handler_type": "file_append",
        "risk_level": "high",
        "timeout_sec": 30,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
        "policies": {"write_root": str(ALLOWED_WRITE_ROOT)},
    },
]


def _get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key,value,timestamp) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, value),
        )


def _get_mode() -> str:
    return _get_setting("mode", "strict") or "strict"


def _in_quiet_hours() -> bool:
    q = (_get_setting("quiet_hours", "") or "").strip()
    if not q:
        return False
    try:
        start_s, end_s = [x.strip() for x in q.split("-", 1)]
        sh, sm = [int(x) for x in start_s.split(":", 1)]
        eh, em = [int(x) for x in end_s.split(":", 1)]
    except Exception:
        return False
    now = datetime.now().hour * 60 + datetime.now().minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def _queue_notification(chat_id: int, text: str) -> None:
    with get_db() as conn:
        conn.execute("INSERT INTO notification_queue (chat_id,text,delivered) VALUES (?,?,0)", (chat_id, text[:TEXT_LIMIT]))


async def _notify_owner(text: str) -> None:
    if _app is None:
        return
    if _in_quiet_hours():
        _queue_notification(OWNER_ID, text)
        return
    await _app.bot.send_message(chat_id=OWNER_ID, text=text)


def _save_session_state(key: str, obj: dict) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (key,value,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, json.dumps(obj)),
        )


def _load_session_state(key: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM sessions WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except Exception:
        return None


def _get_policy(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM global_policies WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_policy(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO global_policies (key,value,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, value),
        )


def _bootstrap_default_policies() -> None:
    with get_db() as conn:
        rows = conn.execute("SELECT key FROM global_policies").fetchall()
    existing = {r["key"] for r in rows}
    for k, v in DEFAULT_GLOBAL_POLICIES.items():
        if k not in existing:
            _set_policy(k, v)


def _bootstrap_default_prompts() -> None:
    with get_db() as conn:
        rows = conn.execute("SELECT mode FROM runtime_prompts").fetchall()
    existing = {r["mode"] for r in rows}
    defaults = {
        "strict": STRICT_PROMPT,
        "normal": NORMAL_PROMPT,
    }
    with get_db() as conn:
        for mode, prompt in defaults.items():
            if mode not in existing:
                conn.execute(
                    "INSERT INTO runtime_prompts (mode,prompt_text,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
                    (mode, prompt),
                )


def _get_runtime_prompt(mode: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT prompt_text FROM runtime_prompts WHERE mode=?", (mode,)).fetchone()
    if row and row["prompt_text"].strip():
        return row["prompt_text"]
    return STRICT_PROMPT if mode == "strict" else NORMAL_PROMPT


def _set_runtime_prompt(mode: str, prompt_text: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runtime_prompts (mode,prompt_text,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
            (mode, prompt_text),
        )


def _audit_skill(skill_name: str, action: str, note: str = "") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO skill_audit (skill_name,action,note) VALUES (?,?,?)",
            (skill_name, action, note),
        )


def _upsert_skill(
    name: str,
    description: str,
    schema: dict,
    handler_type: str,
    risk_level: str = "normal",
    timeout_sec: int = 30,
    requires_force: bool = False,
    enabled: bool = True,
    visible_in_strict: bool = True,
    visible_in_normal: bool = True,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO skills
              (name,description,schema_json,handler_type,risk_level,
               timeout_sec,requires_force,enabled,visible_in_strict,visible_in_normal,updated_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
              description=excluded.description,
              schema_json=excluded.schema_json,
              handler_type=excluded.handler_type,
              risk_level=excluded.risk_level,
              timeout_sec=excluded.timeout_sec,
              requires_force=excluded.requires_force,
              visible_in_strict=excluded.visible_in_strict,
              visible_in_normal=excluded.visible_in_normal,
              updated_ts=CURRENT_TIMESTAMP
            """,
            (
                name,
                description,
                json.dumps(schema),
                handler_type,
                risk_level,
                timeout_sec,
                1 if requires_force else 0,
                1 if enabled else 0,
                1 if visible_in_strict else 0,
                1 if visible_in_normal else 0,
            ),
        )
    _audit_skill(name, "upsert")


def _set_skill_policy(skill_name: str, key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO skill_policies (skill_name,policy_key,policy_value)
            VALUES (?,?,?)
            ON CONFLICT(skill_name,policy_key) DO UPDATE SET policy_value=excluded.policy_value
            """,
            (skill_name, key, value),
        )


def _bootstrap_default_skills() -> None:
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    if count > 0:
        return
    for s in DEFAULT_SKILLS:
        _upsert_skill(
            name=s["name"],
            description=s["description"],
            schema=s["schema"],
            handler_type=s["handler_type"],
            risk_level=s["risk_level"],
            timeout_sec=s["timeout_sec"],
            requires_force=bool(s["requires_force"]),
            enabled=bool(s["enabled"]),
            visible_in_strict=bool(s["visible_in_strict"]),
            visible_in_normal=bool(s["visible_in_normal"]),
        )
        for k, v in s.get("policies", {}).items():
            _set_skill_policy(s["name"], k, str(v))


def _row_to_skill(row: sqlite3.Row) -> SkillDef:
    return SkillDef(
        name=row["name"],
        description=row["description"],
        schema=json.loads(row["schema_json"]),
        handler_type=row["handler_type"],
        risk_level=row["risk_level"],
        timeout_sec=int(row["timeout_sec"]),
        requires_force=bool(row["requires_force"]),
        enabled=bool(row["enabled"]),
        visible_in_strict=bool(row["visible_in_strict"]),
        visible_in_normal=bool(row["visible_in_normal"]),
    )


def _get_skill(name: str) -> SkillDef:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM skills WHERE name=? AND enabled=1", (name,)).fetchone()
    if not row:
        raise RuntimeError(f"Skill not found or disabled: {name}")
    return _row_to_skill(row)


def _load_skill_policies(skill_name: str) -> dict[str, str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT policy_key,policy_value FROM skill_policies WHERE skill_name=?",
            (skill_name,),
        ).fetchall()
    return {r["policy_key"]: r["policy_value"] for r in rows}


def _load_skill_defs(mode: str) -> list[SkillDef]:
    col = _MODE_VISIBILITY_COL.get(mode, "visible_in_strict")
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM skills WHERE enabled=1 AND {col}=1 ORDER BY id").fetchall()
    return [_row_to_skill(r) for r in rows]


def _build_tools_from_db(mode: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.schema,
            },
        }
        for s in _load_skill_defs(mode)
    ]


# =============================================================================
# Helpers
# =============================================================================

STOPWORDS = {
    "the", "and", "for", "that", "with", "from", "this", "have", "will", "your", "you",
    "into", "about", "what", "when", "where", "which", "there", "their", "then", "than",
    "just", "also", "only", "were", "been", "being", "they", "them", "does", "did", "done",
}

STRICT_PROMPT = """You are XaviExecutor.
You are concise, obedient, and action oriented.

Rules:
1. Prefer tools over discussion for actionable requests.
2. Read-only actions are executed directly.
3. Non-destructive write actions are executed directly.
4. Destructive actions require explicit force from the user.
5. Do not invent results. Report only what actually happened.
6. Keep replies short. At most 2 lines unless the user explicitly requests detail.
7. After tool use, summarize the real outcome only."""

NORMAL_PROMPT = """You are XaviExecutor.
Be concise, practical, and action oriented.

Rules:
1. Prefer tools when they help complete the request.
2. Ask only when a critical ambiguity blocks action.
3. Destructive actions require explicit force from the user.
4. Do not invent results. Report only what actually happened.
5. Keep replies short."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def _store_artifact(prefix: str, content: str) -> Path:
    path = ARTIFACT_DIR / f"{prefix}_{_utc_stamp()}_{uuid.uuid4().hex[:8]}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def _shorten_or_artifact(prefix: str, text: str) -> tuple[str, Optional[str]]:
    if len(text) <= ARTIFACT_TEXT_THRESHOLD:
        return text[:TEXT_LIMIT], None
    artifact_path = _store_artifact(prefix, text)
    summary = text[:1500] + f"\n[TRUNCATED — full output saved to {artifact_path}]"
    return summary, str(artifact_path)


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _classify_shell(command: str) -> ShellPolicy:
    cmd = command.strip()
    for p in DESTRUCTIVE_PATTERNS:
        if p.search(cmd):
            return ShellPolicy("destructive", True, SHELL_TIMEOUT_WRITE)
    for p in WRITE_PATTERNS:
        if p.search(cmd):
            return ShellPolicy("write", False, SHELL_TIMEOUT_WRITE)
    for p in READ_PATTERNS:
        if p.search(cmd):
            return ShellPolicy("read", False, SHELL_TIMEOUT_READ)
    return ShellPolicy("unknown", False, SHELL_TIMEOUT_READ)


def _user_forced(query: str) -> bool:
    return bool(FORCE_RE.search(query))


def _read_text_limited(path: Path, max_bytes: int = MAX_READ_BYTES) -> str:
    with path.open("rb") as f:
        raw = f.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="ignore")
    if truncated:
        text += "\n[TRUNCATED]"
    return text


def _fetch_text_limited(url: str, max_bytes: int = MAX_BROWSE_BYTES) -> str:
    with requests.get(url, timeout=REQUEST_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                remaining = max_bytes - (total - len(chunk))
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                break
            chunks.append(chunk)
    html = b"".join(chunks).decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    if total > max_bytes:
        text += "\n[TRUNCATED]"
    return text


def _split_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9_]+", text.lower()) if w not in STOPWORDS]


def _deterministic_hash(token: str) -> int:
    h = 2166136261
    for ch in token:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _fallback_embedding(text: str, dims: int = 256) -> list[float]:
    vec = [0.0] * dims
    counts = Counter(_split_words(text))
    if not counts:
        return vec
    for tok, count in counts.items():
        idx = _deterministic_hash(tok) % dims
        sign = -1.0 if (_deterministic_hash(tok + "!") % 2) else 1.0
        vec[idx] += sign * float(count)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _hours_since(ts_text: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return 99999.0
    now = datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _recency_score(ts_text: str) -> float:
    hours = _hours_since(ts_text)
    return max(0.0, min(1.0, math.exp(-hours / (24.0 * 14.0))))


def _domain_allowed(url: str) -> bool:
    raw = _get_policy("allowed_http_domains", "").strip()
    if not raw:
        return True
    allowed = [d.strip().lower() for d in raw.split(",") if d.strip()]
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def _blocked_suffix(path: Path) -> Optional[str]:
    blocked_raw = _get_policy("blocked_write_suffixes", DEFAULT_GLOBAL_POLICIES["blocked_write_suffixes"])
    blocked = [s.strip().lower() for s in blocked_raw.split(",") if s.strip()]
    lower_name = path.name.lower()
    for suffix in blocked:
        if lower_name.endswith(suffix):
            return suffix
    return None


def _record_rate_event(bucket: str, weight: int = 1) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO rate_events (bucket,weight) VALUES (?,?)",
            (bucket, weight),
        )
        conn.execute(
            "DELETE FROM rate_events WHERE timestamp < datetime('now', '-2 hours')"
        )


def _count_rate_events(bucket: str, lookback_minutes: int = 1) -> int:
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


def _enforce_rate_limit(bucket: str, limit: int, weight: int = 1) -> tuple[bool, str]:
    current = _count_rate_events(bucket)
    if current + weight > limit:
        return False, f"⛔ Rate limit exceeded for {bucket}: {current}/{limit} in last minute."
    _record_rate_event(bucket, weight)
    return True, ""


def _update_skill_usage(skill_name: str, duration_ms: int, status: str) -> None:
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


# =============================================================================
# Async helpers
# =============================================================================


def _log_future_exception(fut: asyncio.Future, label: str) -> None:
    exc = fut.exception()
    if exc:
        log.error("Background task [%s] raised: %s", label, exc)


def _fire_and_forget(loop: asyncio.AbstractEventLoop, fn, label: str) -> None:
    fut = loop.run_in_executor(None, fn)
    fut.add_done_callback(lambda f: _log_future_exception(f, label))


# =============================================================================
# Async shell execution
# =============================================================================

async def _run_shell_async(command: str, cwd: str, timeout: int, raw: bool) -> tuple[str, str, int]:
    if raw:
        if not ALLOW_RAW_SHELL:
            raise RuntimeError("raw shell disabled (set ALLOW_RAW_SHELL=1)")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            preexec_fn=os.setsid,
        )
    else:
        argv = shlex.split(command)
        if not argv:
            raise RuntimeError("empty command")
        exe = shutil.which(argv[0])
        if exe is None:
            raise FileNotFoundError(f"Command not found: {argv[0]!r}")
        proc = await asyncio.create_subprocess_exec(
            exe,
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            preexec_fn=os.setsid,
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            stdout_b.decode("utf-8", errors="ignore"),
            stderr_b.decode("utf-8", errors="ignore"),
            proc.returncode or 0,
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise asyncio.TimeoutError(f"Shell command timed out after {timeout}s: {command}")


# =============================================================================
# Memory
# =============================================================================

history_by_chat: dict[int, list[dict[str, str]]] = {}


def _load_recent_history(chat_id: int, limit: int = HISTORY_LOAD_LIMIT) -> list[dict[str, str]]:
    # Keep only plain chat roles for replay.
    # Stored 'tool' logs are audit artifacts and cannot be replayed as chat messages
    # unless paired with the original assistant tool_calls object.
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role,content FROM conversations WHERE chat_id=? AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _get_history(chat_id: int) -> list[dict[str, str]]:
    if chat_id not in history_by_chat:
        history_by_chat[chat_id] = _load_recent_history(chat_id)
    return history_by_chat[chat_id]


def _save_message(chat_id: int, role: str, content: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO conversations (chat_id,role,content) VALUES (?,?,?)",
            (chat_id, role, _redact(content)),
        )


async def _embed_text(text: str) -> tuple[list[float], str]:
    safe_text = text[:MAX_MEMORY_TEXT]
    try:
        resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=safe_text)
        data = resp.data[0].embedding
        return [float(x) for x in data], EMBEDDING_MODEL
    except Exception as exc:
        if not EMBEDDING_FALLBACK:
            raise RuntimeError(f"Embedding request failed: {exc}") from exc
        log.warning("Embedding API unavailable, using fallback embedding: %s", exc)
        return _fallback_embedding(safe_text), "fallback-hash-256"


def _insert_memory_item_sync(text: str, mem_type: str, importance: float, source: str, pinned: bool) -> int:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO memory_items (text,mem_type,importance,source,pinned)
            VALUES (?,?,?,?,?)
            """,
            (text[:MAX_MEMORY_TEXT], mem_type, importance, source, 1 if pinned else 0),
        )
        row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
        return int(row["id"])


def _save_memory_vector_sync(memory_id: int, embedding: list[float], model_name: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_vectors (memory_id,embedding_json,model,created_ts)
            VALUES (?,?,?,CURRENT_TIMESTAMP)
            """,
            (memory_id, json.dumps(embedding), model_name),
        )


def _delete_memory_sync(memory_id: int) -> str:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE memory_items SET is_deleted=1 WHERE id=? AND is_deleted=0",
            (memory_id,),
        )
    return f"✓ Memory {memory_id} deleted." if cur.rowcount else f"Memory not found: {memory_id}"


async def _add_memory(text: str, mem_type: str = "note", importance: float = 0.6, source: str = "", pinned: bool = False) -> int:
    clean_text = text.strip()[:MAX_MEMORY_TEXT]
    if not clean_text:
        raise RuntimeError("empty memory")
    memory_id = _insert_memory_item_sync(clean_text, mem_type, importance, source, pinned)
    embedding, model_name = await _embed_text(clean_text)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _save_memory_vector_sync(memory_id, embedding, model_name))
    return memory_id


def _promote_fact_to_memory_sync(key: str, value: str) -> None:
    text = f"Fact: {key} = {value}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_items WHERE text=? AND is_deleted=0 LIMIT 1",
            (text,),
        ).fetchone()
    if row:
        with get_db() as conn:
            conn.execute(
                "UPDATE memory_items SET importance=MAX(importance, 0.95), pinned=1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
        return
    # sync wrapper only inserts row, async caller will backfill vector through _add_memory when needed.


async def _save_fact_memory(key: str, value: str) -> None:
    text = f"Fact: {key} = {value}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_items WHERE text=? AND is_deleted=0 LIMIT 1",
            (text,),
        ).fetchone()
    if row:
        with get_db() as conn:
            conn.execute(
                "UPDATE memory_items SET importance=MAX(importance, 0.95), pinned=1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
        return
    await _add_memory(text, mem_type="fact", importance=0.95, source="fact", pinned=True)


def _memory_rows() -> list[sqlite3.Row]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT mi.id, mi.text, mi.mem_type, mi.importance, mi.created_ts, mi.last_access_ts, mv.embedding_json
            FROM memory_items mi
            JOIN memory_vectors mv ON mv.memory_id = mi.id
            WHERE mi.is_deleted=0
            ORDER BY mi.id DESC
            """
        ).fetchall()
    return rows


def _retrieve_memory_lexical(query: str, k: int = MAX_MEMORY_RESULTS) -> list[MemoryItem]:
    terms = [w for w in _split_words(query) if len(w) >= 3][:6]
    if not terms:
        return []

    like_clause = " OR ".join(["LOWER(text) LIKE ?" for _ in terms])
    vals = tuple(f"%{t}%" for t in terms)
    sql = f"""
        SELECT id,text,mem_type,importance,created_ts,last_access_ts
        FROM memory_items
        WHERE is_deleted=0 AND ({like_clause})
        ORDER BY pinned DESC, importance DESC, last_access_ts DESC, id DESC
        LIMIT ?
    """
    with get_db() as conn:
        rows = conn.execute(sql, vals + (max(1, min(k, MAX_MEMORY_RESULTS)),)).fetchall()
        ids = [int(r["id"]) for r in rows]
        if ids:
            conn.executemany(
                "UPDATE memory_items SET access_count = access_count + 1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                [(i,) for i in ids],
            )

    return [
        MemoryItem(
            id=int(r["id"]),
            text=r["text"],
            mem_type=r["mem_type"],
            importance=float(r["importance"]),
            created_ts=r["created_ts"],
            last_access_ts=r["last_access_ts"],
        )
        for r in rows
    ]


async def _retrieve_memory(query: str, k: int = MAX_MEMORY_RESULTS) -> list[MemoryItem]:
    q_emb, model_name = await _embed_text(query)
    if str(model_name).startswith("fallback"):
        return _retrieve_memory_lexical(query, k=k)

    rows = _memory_rows()
    scored: list[tuple[float, MemoryItem]] = []
    for r in rows:
        try:
            emb = json.loads(r["embedding_json"])
        except Exception:
            continue
        sim = _cosine(q_emb, emb)
        rec = _recency_score(r["last_access_ts"])
        score = 0.7 * sim + 0.2 * float(r["importance"]) + 0.1 * rec
        item = MemoryItem(
            id=int(r["id"]),
            text=r["text"],
            mem_type=r["mem_type"],
            importance=float(r["importance"]),
            created_ts=r["created_ts"],
            last_access_ts=r["last_access_ts"],
        )
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [item for _score, item in scored[: max(1, min(k, MAX_MEMORY_RESULTS))]]
    ids = [item.id for item in top]
    if ids:
        with get_db() as conn:
            conn.executemany(
                "UPDATE memory_items SET access_count = access_count + 1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                [(i,) for i in ids],
            )
    return top


def _list_memories(limit: int = 20) -> str:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, mem_type, importance, text, last_access_ts, access_count
            FROM memory_items
            WHERE is_deleted=0
            ORDER BY pinned DESC, importance DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
    if not rows:
        return "No memories."
    return "\n".join(
        f"#{r['id']} [{r['mem_type']}] imp={r['importance']:.2f} hits={r['access_count']} — {r['text'][:140]}"
        for r in rows
    )


def _memory_count() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()
    return int(row["n"])


def _prune_memory_sync() -> None:
    keep = int(_get_policy("memory_cleanup_keep", str(MAX_MEMORY_ITEMS)))
    with get_db() as conn:
        conn.execute(
            """
            UPDATE memory_items
            SET is_deleted=1
            WHERE id IN (
                SELECT id FROM memory_items
                WHERE is_deleted=0 AND pinned=0
                ORDER BY importance DESC, last_access_ts DESC, id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (keep,),
        )


def _today_daily_path() -> Path:
    return DAILY_MEMORY_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def _yesterday_daily_path() -> Path:
    return DAILY_MEMORY_DIR / f"{(datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')}.md"


def _append_daily_memory(text: str) -> None:
    p = _today_daily_path()
    if not p.exists():
        p.write_text(f"# {datetime.now().strftime('%Y-%m-%d')}\n\n", encoding='utf-8')
    clean = text.strip().replace("\n", " ")
    line = f"- {clean}\n"
    existing = p.read_text(encoding='utf-8', errors='ignore')
    if clean and clean not in existing:
        with p.open('a', encoding='utf-8') as f:
            f.write(line)


def _promote_long_memory(text: str) -> None:
    clean = text.strip().replace("\n", " ")
    if not clean:
        return
    if not LONG_MEMORY_FILE.exists():
        LONG_MEMORY_FILE.write_text("# MEMORY.md\n\n## Promoted notes\n", encoding='utf-8')
    existing = LONG_MEMORY_FILE.read_text(encoding='utf-8', errors='ignore')
    if clean in existing:
        return
    with LONG_MEMORY_FILE.open('a', encoding='utf-8') as f:
        f.write(f"- {clean}\n")


def _build_file_memory_block() -> str:
    chunks: list[str] = []
    if LONG_MEMORY_FILE.exists():
        txt = LONG_MEMORY_FILE.read_text(encoding='utf-8', errors='ignore').strip()
        if txt:
            chunks.append("Long-term file memory:\n" + txt[:4000])
    for p in (_yesterday_daily_path(), _today_daily_path()):
        if p.exists():
            txt = p.read_text(encoding='utf-8', errors='ignore').strip()
            if txt:
                chunks.append(f"Daily memory ({p.name}):\n" + txt[:2000])
    return ("\n\n" + "\n\n".join(chunks)) if chunks else ""


async def _maybe_promote_memory(text: str, source: str = "user") -> None:
    clean = text.strip()
    if len(clean) < 24:
        return
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
        return
    try:
        await _add_memory(clean, mem_type=mem_type, importance=importance, source=source, pinned=False)
        _append_daily_memory(clean)
        if mem_type in {"preference", "observation"} and importance >= 0.8:
            _promote_long_memory(clean)
    except Exception as exc:
        log.warning("Could not promote memory: %s", exc)


def _build_structured_memory_block(query: str) -> str:
    q = query.lower()
    parts = []
    if any(w in q for w in ("remember", "fact", "know", "status")):
        parts.append("Saved facts:\n" + _get_facts())
    if any(w in q for w in ("task", "done", "todo")):
        parts.append("Open tasks:\n" + _get_tasks())
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


async def _build_memory_block(query: str) -> str:
    semantic = await _retrieve_memory(query, k=MAX_MEMORY_RESULTS)
    sem_block = ""
    if semantic:
        sem_block = "\n\nRelevant memory:\n" + "\n".join(f"- {m.text}" for m in semantic)

    # Telemetry: make memory read-path observable in logs.
    try:
        ids = [m.id for m in semantic]
        log.info("memory_read query=%r hits=%d ids=%s", query[:80], len(semantic), ids)
    except Exception:
        pass

    file_block = _build_file_memory_block()
    return _build_structured_memory_block(query) + sem_block + file_block


# =============================================================================
# Facts and tasks
# =============================================================================


def _save_fact(key: str, value: str, source: str = "") -> str:
    key, value = key.lower().strip(), value.strip()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO facts (key,value,source,timestamp) VALUES (?,?,?,CURRENT_TIMESTAMP)",
            (key, value, source),
        )
    return f"✓ Saved: *{key}* = {value}"


def _delete_fact(key: str) -> str:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM facts WHERE key=?", (key.lower().strip(),))
    return f"✓ Deleted: {key}" if cur.rowcount else f"Not found: {key}"


def _get_facts() -> str:
    with get_db() as conn:
        rows = conn.execute("SELECT key,value,source FROM facts ORDER BY timestamp DESC").fetchall()
    return "\n".join(
        f"• *{r['key']}*: {r['value']}" + (f" _(src:{r['source']})_" if r["source"] else "")
        for r in rows
    ) or "No facts."


def _add_task(desc: str, priority: str = "normal", source: str = "") -> str:
    desc = desc.strip()
    priority = priority.lower().strip() if priority.lower().strip() in {"low", "normal", "high"} else "normal"
    if not desc:
        return "Need description."
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tasks (description,priority,source) VALUES (?,?,?)",
            (desc, priority, source),
        )
    return f"✓ Task added [{priority}]: {desc}"


def _close_task(task_id: int) -> str:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='done',done_ts=CURRENT_TIMESTAMP WHERE id=? AND status='open'",
            (task_id,),
        )
    return f"✓ Task #{task_id} closed." if cur.rowcount else f"Task #{task_id} not found/open."


def _get_tasks() -> str:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id,description,priority FROM tasks WHERE status='open'
            ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, timestamp
            """
        ).fetchall()
    return "\n".join(f"#{r['id']} [{r['priority']}] {r['description']}" for r in rows) or "No open tasks."


# =============================================================================
# Cron jobs
# =============================================================================


def _parse_cron_expr(expr: str) -> Optional[str]:
    expr = expr.strip()
    if croniter.is_valid(expr):
        return expr
    return None


async def _parse_nl_to_cron(natural: str) -> Optional[str]:
    prompt = (
        "Convert this schedule description to a 5-field cron expression (minute hour dom month dow).\n"
        "Reply ONLY with the cron expression. No explanation.\n"
        "Examples: 'every day at 9am' -> '0 9 * * *', 'every 30 minutes' -> '*/30 * * * *'\n\n"
        f"Schedule: {natural}"
    )
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=32,
            temperature=0,
        )
        candidate = (resp.choices[0].message.content or "").strip().strip("`")
        if croniter.is_valid(candidate):
            return candidate
        return None
    except Exception as exc:
        log.error("NL→cron failed: %s", exc)
        return None


def _add_cron_job(name: str, description: str, cron_expr: str, skill_type: str, skill_args: dict, alert_owner: bool = True) -> str:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO cron_jobs (name,description,cron_expr,skill_type,skill_args,alert_owner)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
              description=excluded.description,
              cron_expr=excluded.cron_expr,
              skill_type=excluded.skill_type,
              skill_args=excluded.skill_args,
              alert_owner=excluded.alert_owner,
              enabled=1,
              updated_ts=CURRENT_TIMESTAMP
            """,
            (name, description, cron_expr, skill_type, json.dumps(skill_args), 1 if alert_owner else 0),
        )
        row = conn.execute("SELECT name,cron_expr,skill_type FROM cron_jobs WHERE name=?", (name,)).fetchone()
    if not row:
        return f"⚠️ Failed to persist cron job: {name}"
    return f"✓ Cron job '{name}' upserted: [{row['cron_expr']}] → {row['skill_type']}"


def _remove_cron_job(name: str) -> str:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM cron_jobs WHERE name=?", (name,))
    return f"✓ Removed cron job: {name}" if cur.rowcount else f"Job not found: {name}"


def _toggle_cron_job(name: str, enabled: bool) -> str:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE cron_jobs SET enabled=?,updated_ts=CURRENT_TIMESTAMP WHERE name=?",
            (1 if enabled else 0, name),
        )
    state = "enabled" if enabled else "disabled"
    return f"✓ Job '{name}' {state}." if cur.rowcount else f"Job not found: {name}"


def _list_cron_jobs() -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name,cron_expr,skill_type,enabled,last_run_ts,last_status FROM cron_jobs ORDER BY id"
        ).fetchall()
    if not rows:
        return "No cron jobs."
    lines = []
    for r in rows:
        state = "✅" if r["enabled"] else "⏸"
        last = r["last_run_ts"] or "never"
        lines.append(
            f"{state} *{r['name']}* [{r['cron_expr']}] → {r['skill_type']} | last: {last} ({r['last_status']})"
        )
    return "\n".join(lines)


def _get_cron_runs(name: str, limit: int = 5) -> str:
    limit = min(max(1, limit), MAX_CRONRUNS_LIMIT)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT timestamp,status,duration_ms,output,error FROM cron_runs
            WHERE job_name=? ORDER BY id DESC LIMIT ?
            """,
            (name, limit),
        ).fetchall()
    if not rows:
        return f"No runs for job: {name}"
    lines = []
    for r in rows:
        line = f"• {r['timestamp']} | {r['status']} | {r['duration_ms']}ms"
        if r["error"]:
            line += f"\n  ⚠️ {r['error'][:200]}"
        lines.append(line)
    return "\n".join(lines)


def _record_cron_run(name: str, status: str, duration_ms: int, output: str, error: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO cron_runs (job_name,status,duration_ms,output,error) VALUES (?,?,?,?,?)",
            (name, status, duration_ms, output[:TEXT_LIMIT], error[:2000]),
        )
        conn.execute(
            "UPDATE cron_jobs SET last_run_ts=CURRENT_TIMESTAMP,last_status=? WHERE name=?",
            (status, name),
        )


_app: Optional[Any] = None
_state_task: Optional[asyncio.Task] = None
_artifact_task: Optional[asyncio.Task] = None
_notify_task: Optional[asyncio.Task] = None
_seen_artifacts: set[str] = set()
_prompt_proposal: Optional[dict[str, str]] = None


async def _run_cron_job(job_name: str) -> None:
    log.info("Cron job starting: %s", job_name)
    start = time.monotonic()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cron_jobs WHERE name=? AND enabled=1", (job_name,)).fetchone()
    if not row:
        return

    skill_type = row["skill_type"]
    skill_args = json.loads(row["skill_args"])
    alert_owner = bool(row["alert_owner"])
    output = ""
    error = ""
    status = "ok"

    try:
        if skill_type == "agent":
            query = skill_args.get("query", f"Run cron job: {job_name}")
            output = await _run_agent(chat_id=0, query=query, force=True)
        elif skill_type in ("run_shell", "browse_page", "read_file", "write_file", "append_file"):
            output = await _call_skill(
                chat_id=0,
                user_query=f"cron:{job_name} force",
                mode="normal",
                skill_name=skill_type,
                args=skill_args,
            )
        elif skill_type == "memory_cleanup":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _prune_memory_sync)
            output = "Memory cleanup completed."
        elif skill_type == "db_backup":
            backup_dir = skill_args.get("backup_dir", str(Path("backups").resolve()))
            loop = asyncio.get_running_loop()
            dst = await loop.run_in_executor(None, lambda: sqlite_online_backup(DB_FILE, backup_dir))
            output = f"DB backup completed: {dst}"
        else:
            raise ValueError(f"Unknown cron skill_type: {skill_type}")
    except Exception as exc:
        log.exception("Cron job %s failed", job_name)
        error = str(exc)
        status = "error"

    duration_ms = int((time.monotonic() - start) * 1000)
    loop = asyncio.get_running_loop()
    _fire_and_forget(
        loop,
        lambda: _record_cron_run(job_name, status, duration_ms, output, error),
        label=f"record_cron_run:{job_name}",
    )

    if alert_owner and _app is not None:
        truncated_output = output[:CRON_MAX_OUTPUT_CHARS]
        msg = (
            f"✅ *Cron: {job_name}* ({duration_ms}ms)\n{truncated_output}"
            if status == "ok"
            else f"🚨 *Cron FAILED: {job_name}*\n`{error[:500]}`"
        )
        try:
            await _notify_owner(msg)
        except TelegramError as exc:
            log.error("Failed to send cron alert: %s", exc)


def _build_scheduler() -> AsyncIOScheduler:
    jobstore_url = f"sqlite:///{DB_FILE}"
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
        executors={"default": AsyncIOExecutor()},
        timezone="UTC",
    )
    return scheduler


def _sync_scheduler(scheduler: AsyncIOScheduler) -> None:
    existing = {job.id for job in scheduler.get_jobs()}
    with get_db() as conn:
        rows = conn.execute("SELECT name,cron_expr,enabled FROM cron_jobs").fetchall()

    active_names = set()
    for r in rows:
        name = r["name"]
        if not r["enabled"]:
            if name in existing:
                scheduler.remove_job(name)
            continue
        active_names.add(name)
        trigger = CronTrigger.from_crontab(r["cron_expr"], timezone="UTC")
        if name in existing:
            scheduler.reschedule_job(name, trigger=trigger)
        else:
            scheduler.add_job(_run_cron_job, trigger=trigger, id=name, args=[name], replace_existing=True)

    for job_id in existing - active_names:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


async def _state_persist_loop() -> None:
    while True:
        try:
            payload = {"pending_action_by_chat": STATE.pending_action_by_chat}
            _save_session_state("runtime_state", payload)
        except Exception as exc:
            log.warning("State persist failed: %s", exc)
        await asyncio.sleep(20)


async def _artifact_watch_loop() -> None:
    global _seen_artifacts
    art = Path(ARTIFACT_DIR)
    art.mkdir(parents=True, exist_ok=True)
    _seen_artifacts = {str(p) for p in art.rglob("*") if p.is_file()}
    while True:
        try:
            current = {str(p) for p in art.rglob("*") if p.is_file()}
            new_files = sorted(current - _seen_artifacts)
            _seen_artifacts = current
            if new_files:
                newest = new_files[-1]
                await _notify_owner(f"🆕 New artifact detected:\n{newest}")
        except Exception as exc:
            log.warning("Artifact watcher failed: %s", exc)
        await asyncio.sleep(30)


async def _notification_flush_loop() -> None:
    while True:
        try:
            if not _in_quiet_hours() and _app is not None:
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT id,chat_id,text FROM notification_queue WHERE delivered=0 ORDER BY id ASC LIMIT 5"
                    ).fetchall()
                for r in rows:
                    try:
                        await _app.bot.send_message(chat_id=int(r["chat_id"]), text=r["text"])
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE notification_queue SET delivered=1,delivered_ts=CURRENT_TIMESTAMP WHERE id=?",
                                (int(r["id"]),),
                            )
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("Notification flush failed: %s", exc)
        await asyncio.sleep(30)


# =============================================================================
# Skill execution
# =============================================================================


def _record_run(
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
              (chat_id,user_query,mode,tool_name,tool_args,result,exit_code,duration_ms,status,artifact_path)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                chat_id,
                _redact(user_query),
                mode,
                tool_name,
                json.dumps(tool_args) if tool_args else None,
                _redact(result[:TEXT_LIMIT]),
                exit_code,
                duration_ms,
                status,
                artifact_path,
            ),
        )


async def _exec_file_read(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    root = Path(policies.get("read_root", str(READ_ROOT))).resolve()
    max_bytes = int(policies.get("max_bytes", str(MAX_READ_BYTES)))
    path = Path(args["path"]).expanduser().resolve()
    if not _is_under_root(path, root):
        return f"⛔ Path outside read root: {root}", None, "blocked", None
    loop = asyncio.get_running_loop()
    full = await loop.run_in_executor(None, lambda: _read_text_limited(path, max_bytes))
    result, artifact = _shorten_or_artifact(skill.name, full)
    return result, None, "ok", artifact


async def _exec_http_get(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    url = args["url"]
    if not _domain_allowed(url):
        return "⛔ URL domain blocked by global policy.", None, "blocked", None
    max_bytes = int(policies.get("max_bytes", str(MAX_BROWSE_BYTES)))
    loop = asyncio.get_running_loop()
    full = await loop.run_in_executor(None, lambda: _fetch_text_limited(url, max_bytes))
    result, artifact = _shorten_or_artifact(skill.name, full)
    return result, None, "ok", artifact


async def _exec_file_write(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    root = Path(policies.get("write_root", str(ALLOWED_WRITE_ROOT))).resolve()
    path = Path(args["path"]).expanduser().resolve()
    overwrite = bool(args.get("overwrite", False))
    content = args["content"]
    if not _is_under_root(path, root):
        return f"⛔ Write blocked outside root: {root}", None, "blocked", None
    blocked = _blocked_suffix(path)
    if blocked:
        return f"⛔ Writing files with suffix {blocked} is blocked.", None, "blocked", None
    if len(content.encode("utf-8")) > int(_get_policy("max_write_size", DEFAULT_GLOBAL_POLICIES["max_write_size"])):
        return "⛔ Write size exceeds global policy.", None, "blocked", None
    if path.exists() and not overwrite:
        return f"⛔ Blocked overwrite without overwrite=true: {path}", None, "blocked", None

    loop = asyncio.get_running_loop()

    def _do_write():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    await loop.run_in_executor(None, _do_write)
    return f"Written {len(content):,} chars → {path}", None, "ok", None


async def _exec_file_append(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    root = Path(policies.get("write_root", str(ALLOWED_WRITE_ROOT))).resolve()
    path = Path(args["path"]).expanduser().resolve()
    content = args["content"]
    if not _is_under_root(path, root):
        return f"⛔ Write blocked outside root: {root}", None, "blocked", None
    blocked = _blocked_suffix(path)
    if blocked:
        return f"⛔ Writing files with suffix {blocked} is blocked.", None, "blocked", None
    if len(content.encode("utf-8")) > int(_get_policy("max_write_size", DEFAULT_GLOBAL_POLICIES["max_write_size"])):
        return "⛔ Write size exceeds global policy.", None, "blocked", None

    loop = asyncio.get_running_loop()

    def _do_append():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

    await loop.run_in_executor(None, _do_append)
    return f"Appended {len(content):,} chars → {path}", None, "ok", None


async def _exec_shell_command(skill: SkillDef, policies: dict, user_query: str, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    return await exec_shell_command(
        skill_name=skill.name,
        requires_force=skill.requires_force,
        policies=policies,
        user_query=user_query,
        args=args,
        classify_shell=_classify_shell,
        user_forced=_user_forced,
        run_shell_async=_run_shell_async,
        shorten_or_artifact=_shorten_or_artifact,
    )


async def _call_skill(chat_id: int, user_query: str, mode: str, skill_name: str, args: dict) -> str:
    start = time.monotonic()
    exit_code: Optional[int] = None
    status = "ok"
    artifact_path: Optional[str] = None
    result = f"Unknown skill: {skill_name}"

    tool_limit = int(_get_policy("tool_calls_per_minute", DEFAULT_GLOBAL_POLICIES["tool_calls_per_minute"]))
    ok, msg = _enforce_rate_limit("tool_calls", tool_limit)
    if not ok:
        return msg

    try:
        skill = _get_skill(skill_name)
        policies = _load_skill_policies(skill.name)

        if skill.handler_type == "shell_command":
            shell_limit = int(_get_policy("shell_calls_per_minute", DEFAULT_GLOBAL_POLICIES["shell_calls_per_minute"]))
            ok, msg = _enforce_rate_limit("shell_calls", shell_limit)
            if not ok:
                return msg

        if skill.requires_force and not _user_forced(user_query):
            result, status = f"⛔ Skill '{skill.name}' requires force.", "blocked"
        elif skill.handler_type == "file_read":
            result, exit_code, status, artifact_path = await _exec_file_read(skill, policies, args)
        elif skill.handler_type == "http_get":
            result, exit_code, status, artifact_path = await _exec_http_get(skill, policies, args)
        elif skill.handler_type == "file_write":
            result, exit_code, status, artifact_path = await _exec_file_write(skill, policies, args)
        elif skill.handler_type == "file_append":
            result, exit_code, status, artifact_path = await _exec_file_append(skill, policies, args)
        elif skill.handler_type == "shell_command":
            result, exit_code, status, artifact_path = await _exec_shell_command(skill, policies, user_query, args)
        else:
            result, status = f"Unknown handler_type: {skill.handler_type}", "error"

    except asyncio.TimeoutError as exc:
        result, status = f"⏱ Timeout: {exc}", "timeout"
    except Exception as exc:
        log.exception("Skill %s failed", skill_name)
        result, status = f"Error: {exc}", "error"

    duration_ms = int((time.monotonic() - start) * 1000)
    loop = asyncio.get_running_loop()
    _fire_and_forget(
        loop,
        lambda: _record_run(chat_id, user_query, mode, skill_name, args, result, exit_code, duration_ms, status, artifact_path),
        label=f"record_run:{skill_name}",
    )
    _fire_and_forget(
        loop,
        lambda: _update_skill_usage(skill_name, duration_ms, status),
        label=f"skill_usage:{skill_name}",
    )
    return result


# =============================================================================
# Agent loop
# =============================================================================

async def _run_agent(chat_id: int, query: str, force: bool = False) -> str:
    mode = _get_mode()
    hist = _get_history(chat_id)
    memory_block = await _build_memory_block(query)
    tools = _build_tools_from_db(mode)

    effective_query = query + (" force" if force and not _user_forced(query) else "")
    user_msg = {"role": "user", "content": effective_query}
    base_prompt = _get_runtime_prompt(mode)
    messages: list[dict[str, Any]] = (
        [{"role": "system", "content": base_prompt + memory_block}] + hist[-HISTORY_LOAD_LIMIT:] + [user_msg]
    )

    if chat_id != 0:
        loop = asyncio.get_running_loop()
        _fire_and_forget(loop, lambda: _save_message(chat_id, "user", query), label="save_user_msg")
        hist.append(user_msg)
        asyncio.create_task(_maybe_promote_memory(query, source="user"))

    final_text = "✓"
    round_count = 0

    while round_count < MAX_TOOL_ROUNDS:
        try:
            response = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.10 if mode == "strict" else 0.25,
                max_tokens=4096,
            )
        except RateLimitError as exc:
            retry_after = 5
            try:
                retry_after = int(exc.response.headers.get("retry-after", 5))
            except Exception:
                pass
            wait = min(retry_after + round_count * 2, 60)
            log.warning("Rate limit — sleeping %ds", wait)
            await asyncio.sleep(wait)
            round_count += 1
            continue
        except Exception as exc:
            log.exception("LLM call failed")
            return f"LLM error: {str(exc)[:200]}"

        msg = response.choices[0].message
        final_text = msg.content or ""

        if not msg.tool_calls:
            break

        messages.append(msg.model_dump(exclude_none=True))

        for tc in msg.tool_calls:
            tool_name = tc.function.name
            tool_args: dict[str, Any] = {}
            try:
                tool_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as exc:
                result = f"Error: could not parse tool arguments — {exc}"
                log.warning("Tool call %s had bad JSON args: %s", tool_name, exc)
            else:
                result = await _call_skill(chat_id, effective_query, mode, tool_name, tool_args)

            if chat_id != 0:
                loop = asyncio.get_running_loop()
                _fire_and_forget(
                    loop,
                    lambda r=result, n=tool_name, a=tool_args: _save_message(chat_id, "tool", f"[{n}] {json.dumps(a)}\n{r}"),
                    label=f"save_tool_msg:{tool_name}",
                )
            messages.append({"role": "tool", "content": result, "tool_call_id": tc.id})

        round_count += 1
    else:
        log.warning("Hit MAX_TOOL_ROUNDS (%d), forcing final reply", MAX_TOOL_ROUNDS)
        try:
            final_resp = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.10 if mode == "strict" else 0.25,
                max_tokens=512,
            )
            final_text = final_resp.choices[0].message.content or "✓"
        except Exception:
            pass

    final_text = _redact((final_text or "✓").strip())
    if chat_id != 0:
        loop = asyncio.get_running_loop()
        _fire_and_forget(loop, lambda: _save_message(chat_id, "assistant", final_text), label="save_assistant_msg")
        hist.append({"role": "assistant", "content": final_text})
        if len(hist) > MAX_HISTORY:
            history_by_chat[chat_id] = hist[-TRIM_TO:]
    return final_text


# =============================================================================
# Status and metrics
# =============================================================================


def _health_report() -> str:
    with get_db() as conn:
        db_size = Path(DB_FILE).resolve().stat().st_size if Path(DB_FILE).exists() else 0
        messages = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        memories = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()["n"]
        skills = conn.execute("SELECT COUNT(*) AS n FROM skills WHERE enabled=1").fetchone()["n"]
        cron = conn.execute("SELECT COUNT(*) AS n FROM cron_jobs WHERE enabled=1").fetchone()["n"]
        errors = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status!='ok' AND timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["n"]
        avg_ms = conn.execute(
            "SELECT COALESCE(AVG(duration_ms),0) AS v FROM runs WHERE timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["v"]
    return (
        f"• DB size: {db_size:,} bytes\n"
        f"• Messages: {messages}\n"
        f"• Memories: {memories}\n"
        f"• Enabled skills: {skills}\n"
        f"• Active cron jobs: {cron}\n"
        f"• Avg tool latency 1h: {float(avg_ms):.1f} ms\n"
        f"• Errors 1h: {int(errors)}"
    )


def _top_skills(limit: int = 10) -> str:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT skill_name, call_count, error_count,
                   CASE WHEN call_count > 0 THEN total_duration_ms * 1.0 / call_count ELSE 0 END AS avg_ms,
                   last_used_ts
            FROM skill_usage_stats
            ORDER BY call_count DESC, last_used_ts DESC
            LIMIT ?
            """,
            (max(1, min(limit, 20)),),
        ).fetchall()
    if not rows:
        return "No skill usage yet."
    return "\n".join(
        f"• *{r['skill_name']}*: calls={r['call_count']} errors={r['error_count']} avg={float(r['avg_ms']):.1f}ms"
        for r in rows
    )


# =============================================================================
# Telegram command handlers
# =============================================================================


def _owner_only(update: Update) -> bool:
    return update.effective_chat.id == OWNER_ID


async def _reply(update: Update, text: str) -> None:
    # Persist all command-path assistant replies for observability/tests.
    try:
        chat_id = update.effective_chat.id if update and update.effective_chat else 0
        if chat_id:
            loop = asyncio.get_running_loop()
            _fire_and_forget(loop, lambda: _save_message(chat_id, "assistant", text), label="save_reply_msg")
    except Exception:
        pass

    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except TelegramError:
        try:
            await update.message.reply_text(text)
        except TelegramError as exc:
            log.error("Failed to send reply: %s", exc)


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    body = update.message.text.partition(" ")[2].strip()
    parts = body.split(":", 1)
    if len(parts) == 2:
        key, value = parts[0], parts[1]
        await _reply(update, _save_fact(key, value))
        asyncio.create_task(_save_fact_memory(key.strip().lower(), value.strip()))
    else:
        await _reply(update, "Use: `/remember key: value`")


async def cmd_delfact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    key = update.message.text.partition(" ")[2].strip()
    await _reply(update, _delete_fact(key) if key else "Use: `/delfact key`")


async def cmd_addtask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    body = update.message.text.partition(" ")[2].strip()
    parts = body.split(":", 1)
    if len(parts) == 2 and parts[0].strip().lower() in {"low", "normal", "high"}:
        priority, desc = parts[0].strip(), parts[1].strip()
    else:
        priority, desc = "normal", body
    await _reply(update, _add_task(desc, priority) if desc else "Need description.")


async def cmd_donetask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        await _reply(update, _close_task(int(raw)))
    except ValueError:
        await _reply(update, "Use: `/donetask <id>`")


async def cmd_facts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_only(update):
        await _reply(update, _get_facts())


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_only(update):
        await _reply(update, _get_tasks())


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    requested = update.message.text.partition(" ")[2].strip().lower()
    if requested in MODES:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: _set_setting("mode", requested))
        await _reply(update, f"Mode set to *{requested}*.")
    else:
        await _reply(update, f"Current mode: *{_get_mode()}*. Use `/mode strict` or `/mode normal`.")


async def cmd_promptshow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    mode = _get_mode()
    prompt = _get_runtime_prompt(mode)
    await _reply(update, f"*Mode:* {mode}\n*Prompt chars:* {len(prompt)}\n\n{prompt}")


async def cmd_promptset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        await _reply(update, "Use: `/promptset <strict|normal> <prompt text>` or `/promptset <strict|normal> reset`")
        return
    parts = raw.split(" ", 1)
    mode = parts[0].strip().lower()
    if mode not in MODES:
        await _reply(update, "Mode must be `strict` or `normal`.")
        return
    if len(parts) < 2 or not parts[1].strip():
        await _reply(update, "Missing prompt text. Example: `/promptset strict You are...`")
        return

    value = parts[1].strip()
    if value.lower() == "reset":
        value = STRICT_PROMPT if mode == "strict" else NORMAL_PROMPT

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _set_runtime_prompt(mode, value))
    await _reply(update, f"✅ Updated *{mode}* prompt ({len(value)} chars).")


async def cmd_quiethours(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        cur = _get_setting("quiet_hours", "") or "off"
        await _reply(update, f"Current quiet hours: {cur}. Use `/quiethours HH:MM-HH:MM`.")
        return
    if not re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", raw):
        await _reply(update, "Format: `/quiethours HH:MM-HH:MM` (24h)")
        return
    _set_setting("quiet_hours", raw)
    await _reply(update, f"✅ Quiet hours set: {raw}")


async def cmd_quietoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    _set_setting("quiet_hours", "")
    await _reply(update, "✅ Quiet hours disabled")


async def cmd_quietflush(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    with get_db() as conn:
        rows = conn.execute("SELECT id,text FROM notification_queue WHERE delivered=0 ORDER BY id ASC LIMIT 20").fetchall()
    sent = 0
    for r in rows:
        try:
            await _app.bot.send_message(chat_id=OWNER_ID, text=r["text"])
            with get_db() as conn:
                conn.execute("UPDATE notification_queue SET delivered=1,delivered_ts=CURRENT_TIMESTAMP WHERE id=?", (int(r["id"]),))
            sent += 1
        except Exception:
            pass
    await _reply(update, f"Flushed {sent} queued notifications.")


async def cmd_promptproposal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _prompt_proposal
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        if _prompt_proposal:
            await _reply(update, f"Pending proposal: mode={_prompt_proposal['mode']} chars={len(_prompt_proposal['text'])}. Use `/promptapprove` or `/promptreject`.")
        else:
            await _reply(update, "Use: `/promptproposal <strict|normal> <text>`")
        return
    parts = raw.split(" ", 1)
    mode = parts[0].strip().lower()
    if mode not in MODES:
        await _reply(update, "Mode must be `strict` or `normal`.")
        return
    if len(parts) < 2 or not parts[1].strip():
        await _reply(update, "Missing proposal text.")
        return
    _prompt_proposal = {"mode": mode, "text": parts[1].strip()}
    await _reply(update, f"📝 Proposal stored for {mode} ({len(parts[1].strip())} chars). Run `/promptapprove` to apply or `/promptreject` to discard.")


async def cmd_promptapprove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _prompt_proposal
    if not _owner_only(update):
        return
    if not _prompt_proposal:
        await _reply(update, "No pending prompt proposal.")
        return
    mode = _prompt_proposal["mode"]
    text = _prompt_proposal["text"]
    _set_runtime_prompt(mode, text)
    _prompt_proposal = None
    await _reply(update, f"✅ Proposal applied to {mode} prompt.")


async def cmd_promptreject(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _prompt_proposal
    if not _owner_only(update):
        return
    _prompt_proposal = None
    await _reply(update, "✅ Prompt proposal discarded.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM conversations WHERE chat_id=?", (chat_id,)).fetchone()["n"]
        recent = conn.execute("SELECT timestamp FROM conversations WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        open_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
        enabled_skills = conn.execute("SELECT COUNT(*) AS n FROM skills WHERE enabled=1").fetchone()["n"]
        cron_count = conn.execute("SELECT COUNT(*) AS n FROM cron_jobs WHERE enabled=1").fetchone()["n"]
        memory_count = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()["n"]
    last = recent["timestamp"] if recent else "never"
    await _reply(
        update,
        (
            f"• Messages: {total}\n"
            f"• Last activity: {last}\n"
            f"• Model: {MODEL}\n"
            f"• Embedding model: {EMBEDDING_MODEL}\n"
            f"• Open tasks: {open_tasks}\n"
            f"• Memories: {memory_count}\n"
            f"• Enabled skills: {enabled_skills}\n"
            f"• Active cron jobs: {cron_count}\n"
            f"• Mode: {_get_mode()}"
        ),
    )


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_only(update):
        await _reply(update, _health_report())


async def cmd_topskills(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_only(update):
        await _reply(update, _top_skills())


async def cmd_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        limit = int(raw) if raw else 20
    except ValueError:
        limit = 20
    await _reply(update, _list_memories(limit))


async def cmd_memadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await _reply(update, "Use: `/memadd text`")
        return
    memory_id = await _add_memory(text, mem_type="manual", importance=0.9, source="telegram", pinned=True)
    await _reply(update, f"✓ Memory #{memory_id} added.")


async def cmd_memdel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        memory_id = int(raw)
    except ValueError:
        await _reply(update, "Use: `/memdel <id>`")
        return
    await _reply(update, _delete_memory_sync(memory_id))


async def cmd_exec(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    command = update.message.text.partition(" ")[2].strip()
    if not command:
        await _reply(update, "Use: `/exec <command>`")
        return
    STATE.last_exec_by_chat[chat_id] = command
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        result = await _call_skill(chat_id, update.message.text, _get_mode(), "run_shell", {"command": command})
        await _reply(update, result)
    except Exception as exc:
        log.exception("cmd_exec failed")
        await _reply(update, f"⚠️ Error: {str(exc)[:200]}")


async def cmd_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    with get_db() as conn:
        rows = conn.execute("SELECT name,enabled,risk_level,handler_type FROM skills ORDER BY id").fetchall()
    if not rows:
        await _reply(update, "No skills.")
        return
    lines = [
        f"{'✅' if r['enabled'] else '⏸'} *{r['name']}* [{r['risk_level']}] ({r['handler_type']})"
        for r in rows
    ]
    await _reply(update, "\n".join(lines))


async def cmd_skillon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/skillon <name>`")
        return
    with get_db() as conn:
        cur = conn.execute("UPDATE skills SET enabled=1,updated_ts=CURRENT_TIMESTAMP WHERE name=?", (name,))
    if cur.rowcount:
        _audit_skill(name, "enabled")
        await _reply(update, f"✓ Skill '{name}' enabled.")
    else:
        await _reply(update, f"Skill not found: {name}")


async def cmd_skilloff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/skilloff <name>`")
        return
    with get_db() as conn:
        cur = conn.execute("UPDATE skills SET enabled=0,updated_ts=CURRENT_TIMESTAMP WHERE name=?", (name,))
    if cur.rowcount:
        _audit_skill(name, "disabled")
        await _reply(update, f"✓ Skill '{name}' disabled.")
    else:
        await _reply(update, f"Skill not found: {name}")


async def cmd_cronadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    body = update.message.text.partition(" ")[2].strip()

    # tolerate optional trailing force token: "... | {json} force"
    body = re.sub(r"\s+force\s*$", "", body, flags=re.I)

    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 3:
        await _reply(update, "Use: `/cronadd name | schedule | skill_type | description | args_json`")
        return

    name = parts[0]
    schedule = parts[1]
    skill_type = parts[2]
    description = parts[3] if len(parts) > 3 else ""
    args_raw = parts[4] if len(parts) > 4 else "{}"

    try:
        skill_args = json.loads(args_raw)
    except json.JSONDecodeError:
        await _reply(update, f"Invalid JSON for args: {args_raw}")
        return

    cron_expr = _parse_cron_expr(schedule)
    if not cron_expr:
        await _reply(update, f"Trying to parse as natural language: _{schedule}_")
        cron_expr = await _parse_nl_to_cron(schedule)
        if not cron_expr:
            await _reply(update, f"Could not parse schedule: {schedule}")
            return
        await _reply(update, f"Schedule interpreted as cron: `{cron_expr}`")

    result = _add_cron_job(name, description, cron_expr, skill_type, skill_args)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        _sync_scheduler(scheduler)
    await _reply(update, result)


async def cmd_cronrm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronrm <n>`")
        return
    result = _remove_cron_job(name)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        try:
            scheduler.remove_job(name)
        except Exception:
            pass
    await _reply(update, result)


async def cmd_cronlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_only(update):
        await _reply(update, _list_cron_jobs())


async def cmd_cronon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronon <n>`")
        return
    result = _toggle_cron_job(name, True)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        _sync_scheduler(scheduler)
    await _reply(update, result)


async def cmd_cronoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronoff <n>`")
        return
    result = _toggle_cron_job(name, False)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        try:
            scheduler.remove_job(name)
        except Exception:
            pass
    await _reply(update, result)


async def cmd_cronruns(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    parts = update.message.text.partition(" ")[2].strip().split()
    name = parts[0] if parts else ""
    try:
        limit = int(parts[1]) if len(parts) > 1 else 5
    except ValueError:
        await _reply(update, "Limit must be a number. Use: `/cronruns <name> [limit]`")
        return
    if not name:
        await _reply(update, "Use: `/cronruns <n> [limit]`")
        return
    await _reply(update, _get_cron_runs(name, limit))


async def cmd_cronrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronrun <n>`")
        return
    with get_db() as conn:
        row = conn.execute("SELECT name FROM cron_jobs WHERE name=?", (name,)).fetchone()
    if not row:
        await _reply(update, f"Job not found: {name}")
        return
    await _reply(update, f"▶️ Running job: _{name}_…")
    await update.message.chat.send_action(ChatAction.TYPING)
    asyncio.create_task(_run_cron_job(name))


async def cmd_clearctx(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    history_by_chat[update.effective_chat.id] = []
    await _reply(update, "Context cleared.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    await _reply(update, """*XaviExecutor V5 Commands*

*Memory*
/remember key: value — save a fact
/delfact key — delete a fact
/facts — list all facts
/memories [limit] — list semantic memory
/memadd text — add pinned memory
/memdel id — delete memory
/addtask [priority:] desc — add a task
/donetask id — close a task
/tasks — list open tasks

*Control*
/mode [strict|normal] — get or set mode
/promptshow — show active runtime prompt
/promptset mode text — set runtime prompt (strict/normal)
/promptproposal mode text — stage prompt change proposal
/promptapprove — apply staged prompt proposal
/promptreject — reject staged prompt proposal
/quiethours HH:MM-HH:MM — queue notifications during quiet window
/quietoff — disable quiet hours
/quietflush — flush queued notifications now
/status — system status
/health — health summary
/topskills — most used skills
/clearctx — clear conversation context
/exec command — run through agent

*Skills*
/skills — list skills
/skillon name — enable skill
/skilloff name — disable skill

*Cron Jobs*
/cronadd name | schedule | skill_type | description | args_json
/cronrm name — remove job
/cronlist — list jobs
/cronon name — enable job
/cronoff name — disable job
/cronrun name — run now
/cronruns name [limit] — recent runs

Cron skill_types: run_shell, browse_page, read_file, write_file, append_file, agent, memory_cleanup, db_backup
Schedule: cron syntax or natural language like every day at 9am
""")


# intent and email helpers moved to xobop/intents.py and xobop/emailer.py


async def handle_media_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await process_media_message(
        update=update,
        state=STATE,
        artifact_dir=str(ARTIFACT_DIR),
        owner_only=_owner_only,
        reply=_reply,
        log=log,
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    try:
        await process_text_message(
            update=update,
            text=text,
            chat_id=chat_id,
            state=STATE,
            reply=_reply,
            call_skill=_call_skill,
            mode_getter=_get_mode,
            list_cron_jobs=_list_cron_jobs,
            run_agent=_run_agent,
            owner_only=_owner_only,
            cmd_cronadd=lambda u: cmd_cronadd(u, ctx),
            cmd_cronlist=lambda u: cmd_cronlist(u, ctx),
            cmd_cronrun=lambda u: cmd_cronrun(u, ctx),
            cmd_cronruns=lambda u: cmd_cronruns(u, ctx),
            log=log,
        )
    except Exception as exc:
        log.exception("Handler error")
        await _reply(update, f"⚠️ Error: {str(exc)[:200]}")


# =============================================================================
# Application setup
# =============================================================================

async def _post_init(app: Application) -> None:
    global _app, _state_task, _artifact_task, _notify_task
    _app = app
    scheduler = _build_scheduler()
    app.bot_data["scheduler"] = scheduler
    _sync_scheduler(scheduler)
    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # hydrate pending actions from persisted session state
    snapshot = _load_session_state("runtime_state") or {}
    pending = snapshot.get("pending_action_by_chat", {})
    if isinstance(pending, dict):
        STATE.pending_action_by_chat = {int(k): v for k, v in pending.items()}
        if STATE.pending_action_by_chat:
            try:
                await _notify_owner("Resuming pending action state from previous session.")
            except Exception:
                pass

    _state_task = asyncio.create_task(_state_persist_loop())
    _artifact_task = asyncio.create_task(_artifact_watch_loop())
    _notify_task = asyncio.create_task(_notification_flush_loop())


async def _post_shutdown(app: Application) -> None:
    global _state_task, _artifact_task, _notify_task
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")

    for t in (_state_task, _artifact_task, _notify_task):
        if t and not t.done():
            t.cancel()
    _state_task = None
    _artifact_task = None
    _notify_task = None


async def _graceful_shutdown(app: Application) -> None:
    log.info("Shutdown signal received — stopping…")
    await app.stop()
    await app.shutdown()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    log.exception("Unhandled handler error: %s", err)
    try:
        if update and getattr(update, "effective_message", None):
            await update.effective_message.reply_text("⚠️ Internal error. Please retry.")
    except Exception:
        pass


def main() -> None:
    _init_db()
    if _get_setting("mode") not in MODES:
        _set_setting("mode", DEFAULT_MODE if DEFAULT_MODE in MODES else "strict")

    log.info(
        "XaviExecutor V5 | Owner: %d | Model: %s | Embeddings: %s | Mode: %s | Write root: %s",
        OWNER_ID,
        MODEL,
        EMBEDDING_MODEL,
        _get_mode(),
        ALLOWED_WRITE_ROOT,
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("delfact", cmd_delfact))
    app.add_handler(CommandHandler("facts", cmd_facts))
    app.add_handler(CommandHandler("memories", cmd_memories))
    app.add_handler(CommandHandler("memadd", cmd_memadd))
    app.add_handler(CommandHandler("memdel", cmd_memdel))
    app.add_handler(CommandHandler("addtask", cmd_addtask))
    app.add_handler(CommandHandler("donetask", cmd_donetask))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("promptshow", cmd_promptshow))
    app.add_handler(CommandHandler("promptset", cmd_promptset))
    app.add_handler(CommandHandler("promptproposal", cmd_promptproposal))
    app.add_handler(CommandHandler("promptapprove", cmd_promptapprove))
    app.add_handler(CommandHandler("promptreject", cmd_promptreject))
    app.add_handler(CommandHandler("quiethours", cmd_quiethours))
    app.add_handler(CommandHandler("quietoff", cmd_quietoff))
    app.add_handler(CommandHandler("quietflush", cmd_quietflush))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("topskills", cmd_topskills))
    app.add_handler(CommandHandler("exec", cmd_exec))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("skillon", cmd_skillon))
    app.add_handler(CommandHandler("skilloff", cmd_skilloff))
    app.add_handler(CommandHandler("clearctx", cmd_clearctx))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cronadd", cmd_cronadd))
    app.add_handler(CommandHandler("cronrm", cmd_cronrm))
    app.add_handler(CommandHandler("cronlist", cmd_cronlist))
    app.add_handler(CommandHandler("cronon", cmd_cronon))
    app.add_handler(CommandHandler("cronoff", cmd_cronoff))
    app.add_handler(CommandHandler("cronrun", cmd_cronrun))
    app.add_handler(CommandHandler("cronruns", cmd_cronruns))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, handle_media_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_on_error)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_graceful_shutdown(app)))

    app.run_polling(drop_pending_updates=True, allowed_updates=["message"], close_loop=False)


if __name__ == "__main__":
    main()

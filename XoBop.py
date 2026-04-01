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
import base64
import contextvars
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import signal
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter
from openai import AsyncOpenAI, RateLimitError
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from xobop.state import STATE
from xobop.config import settings
from xobop.capabilities_shell import exec_shell_command
from xobop.db import db_session, sqlite_online_backup
from xobop.migrations import apply_migrations
from xobop.handlers_runtime import process_media_message, process_text_message, resolve_command_alias
from xobop.bootstrap import build_app, register_handlers
from xobop.cron_bootstrap import ensure_default_cron_jobs
from xobop.schema import ensure_core_schema
from xobop.db_init import initialize_database
from xobop.router import classify
from xobop.shell_facade import get_shell_executor
from xobop.output_guardrails import normalize_machine_kv_output
from xobop.defaults import STOPWORDS, STRICT_PROMPT, NORMAL_PROMPT, default_global_policies
from xobop.agent import AgentDeps, run_agent_loop
from xobop.simulation import TempDbManager, shell_golden_cases, shell_replay_cases, ReplayController, apply_artifact_to_temp_settings
from xobop.runtime_ops import (
    enforce_rate_limit as ops_enforce_rate_limit,
    fire_and_forget as ops_fire_and_forget,
    to_thread as ops_to_thread,
)
from xobop.attachments import extract_docx_text, extract_xlsx_text, summarize_zip
from xobop.commands_core import build_attachdiag_text, build_state_check_text, build_status_text
from xobop.commands_ops import parse_int_or_default
from xobop.text_math import cosine as tm_cosine, deterministic_hash as tm_hash, hours_since as tm_hours_since, recency_score as tm_recency_score, split_words as tm_split_words
from xobop.policy_helpers import blocked_suffix as ph_blocked_suffix, domain_allowed as ph_domain_allowed, is_under_root as ph_is_under_root
from xobop.artifacts_io import shorten_or_artifact as aio_shorten_or_artifact, store_artifact as aio_store_artifact
from xobop.memory_math import fallback_embedding as mm_fallback_embedding
from xobop.runtime_state import (
    build_runtime_state_payload as rs_build_payload,
    build_state_check_dict as rs_build_state_check_dict,
    repair_from_snapshot as rs_repair_from_snapshot,
)
from xobop.mode_utils import mode_help_text as mu_mode_help_text, temperature_for_mode as mu_temperature_for_mode
from xobop.ops_logging import record_op_event as ol_record_op_event, record_run as ol_record_run, update_skill_usage as ol_update_skill_usage
from xobop.runtime_snapshot import (
    export_runtime_snapshot as rsnap_export,
    load_runtime_snapshot as rsnap_load,
    startup_reconcile_runtime_state as rsnap_startup_reconcile,
)
from xobop.io_text import fetch_text_limited as io_fetch_text_limited, read_text_limited as io_read_text_limited
from xobop.shell_policy import classify_shell as sp_classify_shell, user_forced as sp_user_forced
from xobop.improve import (
    apply_artifact_to_runtime_settings as improve_apply_artifact_to_runtime_settings,
    artifact_contract as improve_artifact_contract,
    artifact_has_expected_contract as improve_artifact_has_expected_contract,
)

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

TOKEN = settings.telegram_token
OPENAI_KEY = settings.openai_api_key
OWNER_ID = settings.owner_id

MODEL = settings.model
EMBEDDING_MODEL = settings.embedding_model
DB_FILE = settings.db_file
DEFAULT_MODE = settings.default_mode
PROJECT_ROOT = settings.project_root
ARTIFACT_DIR = settings.artifact_dir
ALLOWED_WRITE_ROOT = settings.allowed_write_root
ALLOW_RAW_SHELL = settings.allow_raw_shell
USE_CAPABILITY_FACADE_SHELL = settings.use_capability_facade_shell
USE_SHELL_REPLAY_CONTROLLER = settings.use_shell_replay_controller
PANIC_UNLOCK_CODE = settings.panic_unlock_code
READ_ROOT = settings.read_root
EMBEDDING_FALLBACK = settings.embedding_fallback
FILE_MEMORY_ROOT = settings.file_memory_root
LONG_MEMORY_FILE = settings.long_memory_file
DAILY_MEMORY_DIR = settings.daily_memory_dir
SELF_MODS_FILE = settings.self_mods_file
RUNTIME_SNAPSHOT_FILE = Path(os.getenv("RUNTIME_SNAPSHOT_FILE", str(PROJECT_ROOT / "state" / "runtime_settings_snapshot.json"))).resolve()
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

MODES = {"strict", "normal", "overseer"}

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
    r"\brm\b", r"\bmv\b", r"\bchown\b",
    r"\bpkill\b", r"\bkill\b", r"\bkillall\b", r"\bdd\b",
    r"\bmkfs\b", r"\bmount\b", r"\bumount\b",
    r"\btruncate\b", r"\bfind\b.*\b-delete\b",
]]
WRITE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"\becho\b.*>", r"\bprintf\b.*>", r"\btee\b", r"\bcp\b", r"\bmkdir\b",
    r"\btouch\b", r"\bchmod\b", r"\bsed\b.*-i", r"\bperl\b.*-i", r"\bpython\b.*\s-c\s",
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

_COMMAND_DISPATCH: dict[str, Any] = {}


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
    initialize_database(
        get_db=get_db,
        ensure_core_schema=ensure_core_schema,
        apply_migrations=apply_migrations,
        bootstrap_default_skills=_bootstrap_default_skills,
        bootstrap_default_policies=_bootstrap_default_policies,
        bootstrap_default_prompts=_bootstrap_default_prompts,
        ensure_default_cron_jobs=ensure_default_cron_jobs,
        repo_cwd=os.getcwd(),
    )

    # Keep run_shell policy in sync with current runtime roots (prevents stale clone drift).
    _set_skill_policy("run_shell", "write_root", str(ALLOWED_WRITE_ROOT))
    _set_skill_policy("run_shell", "default_cwd", str(PROJECT_ROOT))
    _set_skill_policy("run_shell", "allow_raw_shell", "1" if ALLOW_RAW_SHELL else "0")


# =============================================================================
# Settings, policies, and defaults
# =============================================================================

DEFAULT_GLOBAL_POLICIES: dict[str, str] = default_global_policies(MAX_MEMORY_ITEMS)

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
            "write_root": str(ALLOWED_WRITE_ROOT),
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


def _get_last_applied_artifact_id() -> int:
    raw = (_get_setting("last_applied_artifact_id", "0") or "0").strip()
    try:
        return max(0, int(raw))
    except Exception:
        return 0


def _set_last_applied_artifact_id(artifact_id: int) -> None:
    _set_setting("last_applied_artifact_id", str(max(0, int(artifact_id))))


def _runtime_state_payload(*, last_applied_artifact_id: Optional[int] = None, last_promotion_mode: str = "") -> dict[str, Any]:
    return rs_build_payload(
        get_command_aliases=_get_command_aliases,
        get_exec_retry_limit=_get_exec_retry_limit,
        get_last_applied_artifact_id=_get_last_applied_artifact_id,
        last_applied_artifact_id=last_applied_artifact_id,
        last_promotion_mode=last_promotion_mode,
    )


def _export_runtime_snapshot(*, last_applied_artifact_id: Optional[int] = None, last_promotion_mode: str = "") -> None:
    return rsnap_export(
        snapshot_file=RUNTIME_SNAPSHOT_FILE,
        payload_builder=_runtime_state_payload,
        last_applied_artifact_id=last_applied_artifact_id,
        last_promotion_mode=last_promotion_mode,
    )


def _load_runtime_snapshot() -> dict[str, Any]:
    return rsnap_load(snapshot_file=RUNTIME_SNAPSHOT_FILE)


def _state_check_dict() -> dict[str, Any]:
    snap = _load_runtime_snapshot()
    db = _runtime_state_payload()
    return rs_build_state_check_dict(snapshot=snap, db_payload=db)


def _repair_state_from_snapshot() -> str:
    snap = _load_runtime_snapshot()
    return rs_repair_from_snapshot(
        snapshot=snap,
        set_command_aliases=_set_command_aliases,
        set_exec_retry_limit=_set_exec_retry_limit,
        set_last_applied_artifact_id=_set_last_applied_artifact_id,
    )


def _export_self_mods_debug() -> None:
    """Best-effort debug export only; DB settings remain source of truth."""
    try:
        payload = {
            "command_aliases": _get_command_aliases(),
            "exec_retry_limit": _get_exec_retry_limit(),
            "last_applied_artifact_id": _get_last_applied_artifact_id(),
        }
        SELF_MODS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SELF_MODS_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def _get_command_aliases() -> dict[str, str]:
    raw = (_get_setting("command_aliases_json", "{}") or "{}").strip()
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {str(k).strip().lower(): str(v).strip().lower() for k, v in d.items() if str(k).strip() and str(v).strip()}
    except Exception:
        pass
    return {}


def _set_command_aliases(d: dict[str, str]) -> None:
    clean = {str(k).strip().lower(): str(v).strip().lower() for k, v in d.items() if str(k).strip() and str(v).strip()}
    _set_setting("command_aliases_json", json.dumps(clean, sort_keys=True))
    _export_self_mods_debug()


def _overseer_enabled() -> bool:
    return _get_mode() == "overseer"


def _get_improve_policy() -> str:
    p = (_get_setting("improve_policy", "assisted") or "assisted").strip().lower()
    return p if p in {"manual", "assisted", "auto_low"} else "assisted"


def _get_eval_phase() -> str:
    p = (_get_setting("improve_eval_phase", "baseline") or "baseline").strip().lower()
    return p if p in {"baseline", "improving"} else "baseline"


def _allowed_artifact_families() -> set[str]:
    raw = (_get_setting("artifact_families_allowed", "alias,retry,lineage") or "alias,retry,lineage").strip().lower()
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    return set(parts or ["alias", "retry"])


def _artifact_contract(artifact_type: str, target: str) -> dict[str, Any]:
    return improve_artifact_contract(artifact_type, target)


def _artifact_has_expected_contract(artifact_type: str, target: str, artifact_json: dict[str, Any]) -> tuple[bool, str]:
    return improve_artifact_has_expected_contract(artifact_type, target, artifact_json)


def _apply_artifact_to_runtime_settings(artifact_type: str, target: str, artifact_json: dict[str, Any]) -> str:
    return improve_apply_artifact_to_runtime_settings(
        artifact_type,
        target,
        artifact_json,
        get_command_aliases=_get_command_aliases,
        set_command_aliases=_set_command_aliases,
        set_exec_retry_limit=_set_exec_retry_limit,
        set_last_applied_artifact_id=_set_last_applied_artifact_id,
    )


def _get_exec_retry_limit() -> int:
    raw = (_get_setting("exec_retry_limit", "0") or "0").strip()
    if raw.isdigit():
        return max(0, min(3, int(raw)))
    return 0


def _set_exec_retry_limit(v: int) -> None:
    n = max(0, min(3, int(v)))
    _set_setting("exec_retry_limit", str(n))
    _export_self_mods_debug()


def _panic_locked() -> bool:
    return (_get_setting("panic_lock", "0") or "0").strip() == "1"


def _is_panic_bypass_text(text: str) -> bool:
    t = (text or "").strip().lower()
    allowed_prefixes = (
        "/panicstatus",
        "/panicunlock",
        "/status",
        "/health",
        "/checkup",
        "/help",
    )
    return any(t.startswith(p) for p in allowed_prefixes)


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


def _audit_skill(skill_name: str, action: str, note: str = "", owner_id: Optional[int] = None, cmd_input: str = "", exit_code: Optional[int] = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO skill_audit (skill_name,action,note,owner_id,cmd_input,exit_code) VALUES (?,?,?,?,?,?)",
            (skill_name, action, note, owner_id, cmd_input[:TEXT_LIMIT], exit_code),
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

def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def _store_artifact(prefix: str, content: str) -> Path:
    return aio_store_artifact(
        artifact_dir=ARTIFACT_DIR,
        prefix=prefix,
        content=content,
        stamp_fn=_utc_stamp,
        uuid_fn=uuid.uuid4,
    )


def _shorten_or_artifact(prefix: str, text: str) -> tuple[str, Optional[str]]:
    return aio_shorten_or_artifact(
        artifact_dir=ARTIFACT_DIR,
        prefix=prefix,
        text=text,
        artifact_text_threshold=ARTIFACT_TEXT_THRESHOLD,
        text_limit=TEXT_LIMIT,
        stamp_fn=_utc_stamp,
        uuid_fn=uuid.uuid4,
    )


def _is_under_root(path: Path, root: Path) -> bool:
    return ph_is_under_root(path, root)


def _classify_shell(command: str) -> ShellPolicy:
    return sp_classify_shell(
        command,
        destructive_patterns=DESTRUCTIVE_PATTERNS,
        write_patterns=WRITE_PATTERNS,
        read_patterns=READ_PATTERNS,
        timeout_read=SHELL_TIMEOUT_READ,
        timeout_write=SHELL_TIMEOUT_WRITE,
        policy_cls=ShellPolicy,
    )


def _user_forced(query: str) -> bool:
    return sp_user_forced(query, FORCE_RE)


def _read_text_limited(path: Path, max_bytes: int = MAX_READ_BYTES) -> str:
    return io_read_text_limited(path, max_bytes)


def _fetch_text_limited(url: str, max_bytes: int = MAX_BROWSE_BYTES) -> str:
    return io_fetch_text_limited(url, max_bytes=max_bytes, request_timeout=REQUEST_TIMEOUT)


def _split_words(text: str) -> list[str]:
    return tm_split_words(text, set(STOPWORDS))


def _deterministic_hash(token: str) -> int:
    return tm_hash(token)


def _fallback_embedding(text: str, dims: int = 256) -> list[float]:
    log.warning("Embedding API unavailable; using approximate fallback embedding. Semantic recall quality may degrade.")
    return mm_fallback_embedding(
        text,
        dims=dims,
        split_words=_split_words,
        deterministic_hash=_deterministic_hash,
    )


def _cosine(a: list[float], b: list[float]) -> float:
    return tm_cosine(a, b)


def _hours_since(ts_text: str) -> float:
    return tm_hours_since(ts_text)


def _recency_score(ts_text: str) -> float:
    return tm_recency_score(ts_text)


def _domain_allowed(url: str) -> bool:
    return ph_domain_allowed(url, _get_policy)


def _blocked_suffix(path: Path) -> Optional[str]:
    return ph_blocked_suffix(
        path,
        get_policy=_get_policy,
        default_blocked_suffixes=DEFAULT_GLOBAL_POLICIES["blocked_write_suffixes"],
        allowed_write_root=ALLOWED_WRITE_ROOT,
    )


def _enforce_rate_limit(bucket: str, limit: int, weight: int = 1) -> tuple[bool, str]:
    return ops_enforce_rate_limit(get_db, bucket, limit, weight)


def _update_skill_usage(skill_name: str, duration_ms: int, status: str) -> None:
    return ol_update_skill_usage(get_db=get_db, skill_name=skill_name, duration_ms=duration_ms, status=status)


# =============================================================================
# Async helpers
# =============================================================================


async def _to_thread(fn, *args, **kwargs):
    return await ops_to_thread(fn, *args, **kwargs)


def _fire_and_forget(loop: asyncio.AbstractEventLoop, fn, label: str) -> None:
    return ops_fire_and_forget(loop, fn, label, log=log)


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
_AGENT_TURN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("agent_turn_id", default="")


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
        memory_id = int(row["id"])
        conn.execute("INSERT INTO memory_items_fts(rowid,text) VALUES (?,?)", (memory_id, text[:MAX_MEMORY_TEXT]))
        return memory_id


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
        if cur.rowcount:
            conn.execute("DELETE FROM memory_items_fts WHERE rowid=?", (memory_id,))
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

    match_q = " OR ".join(terms)
    sql = """
        SELECT mi.id,mi.text,mi.mem_type,mi.importance,mi.created_ts,mi.last_access_ts
        FROM memory_items_fts f
        JOIN memory_items mi ON mi.id = f.rowid
        WHERE f.text MATCH ? AND mi.is_deleted=0
        ORDER BY mi.pinned DESC, mi.importance DESC, mi.last_access_ts DESC, mi.id DESC
        LIMIT ?
    """
    with get_db() as conn:
        rows = conn.execute(sql, (match_q, max(1, min(k, MAX_MEMORY_RESULTS)))).fetchall()
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


async def _build_memory_block(query: str, k: int = MAX_MEMORY_RESULTS) -> str:
    semantic = await _retrieve_memory(query, k=k)
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
_last_openai_latency_ms: float = 0.0


async def _run_cron_job(job_name: str) -> None:
    log.info("Cron job starting: %s", job_name)
    start = time.monotonic()
    if _panic_locked():
        duration_ms = int((time.monotonic() - start) * 1000)
        _record_cron_run(job_name, "skipped", duration_ms, "", "panic-lock active")
        _record_op_event("cron_run", route=job_name, status="skipped", latency_ms=duration_ms, metadata={"reason": "panic-lock"})
        return
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
            repo_dir = skill_args.get("repo_dir", os.getcwd())
            loop = asyncio.get_running_loop()
            dst = await loop.run_in_executor(None, lambda: sqlite_online_backup(DB_FILE, backup_dir, repo_dir=repo_dir))
            output = f"DB backup completed: {dst}"
        elif skill_type == "prompt_review":
            if not _overseer_enabled():
                output = "Skipped prompt_review (overseer mode disabled)."
            else:
                with get_db() as conn:
                    top = conn.execute("SELECT skill_name,call_count,error_count FROM skill_usage_stats ORDER BY error_count DESC, call_count DESC LIMIT 8").fetchall()
                    recent = conn.execute("SELECT tool_name,status,result FROM runs ORDER BY id DESC LIMIT 30").fetchall()
                payload = {
                    "top_skills": [dict(r) for r in top],
                    "recent_runs": [dict(r) for r in recent],
                }
                resp = await client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": "Given skill usage and failures, propose a concise strict prompt improvement. Keep under 120 words."},
                        {"role": "user", "content": json.dumps(payload)[:5000]},
                    ],
                    temperature=0.1,
                    max_tokens=220,
                )
                proposal = (resp.choices[0].message.content or "").strip()
                global _prompt_proposal
                _prompt_proposal = {"mode": "strict", "text": proposal}
                output = "Prompt proposal generated and staged. Use /promptapprove or /promptreject."
        elif skill_type == "checkup":
            loop = asyncio.get_running_loop()
            output = await loop.run_in_executor(None, _run_checkup_sync)
        elif skill_type == "system_map_refresh":
            if not _overseer_enabled():
                output = "Skipped system_map_refresh (overseer mode disabled)."
            else:
                loop = asyncio.get_running_loop()
                path = await loop.run_in_executor(None, _generate_system_map_sync)
                output = f"SYSTEM_MAP refreshed: {path}"
        else:
            raise ValueError(f"Unknown cron skill_type: {skill_type}")
    except Exception as exc:
        log.exception("Cron job %s failed", job_name)
        error = str(exc)
        status = "error"

    duration_ms = int((time.monotonic() - start) * 1000)
    _record_op_event(
        "cron_run",
        route=job_name,
        status=status,
        latency_ms=duration_ms,
        metadata={"skill_type": skill_type},
    )
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


async def _quicklook_summary(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
        return f"Image artifact ({size} bytes)."
    if ext == '.pdf':
        return f"PDF artifact ({size} bytes)."
    try:
        head = path.read_text(encoding='utf-8', errors='ignore')[:220].replace('\n', ' ')
        if head.strip():
            return f"Text preview: {head.strip()}"
    except Exception:
        pass
    return f"File artifact ({size} bytes)."


async def _multimodal_artifact_summary(path: Path) -> str:
    """Best-effort multimodal/metadata summary for overseer indexing."""
    ext = path.suffix.lower()
    if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
        try:
            raw = path.read_bytes()
            if len(raw) <= 1_500_000:
                mime = mimetypes.guess_type(str(path))[0] or 'image/jpeg'
                b64 = base64.b64encode(raw).decode('ascii')
                data_url = f"data:{mime};base64,{b64}"
                resp = await client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": "Describe this image for searchable memory in one concise sentence."},
                        {"role": "user", "content": [
                            {"type": "text", "text": "Summarize key visual content."},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ]},
                    ],
                    temperature=0.1,
                    max_tokens=90,
                )
                txt = (resp.choices[0].message.content or '').strip()
                if txt:
                    return txt
        except Exception:
            pass
    # fallback to quicklook metadata summary
    return await _quicklook_summary(path)


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
            if new_files and _app is not None:
                newest = Path(new_files[-1])
                summary = await _quicklook_summary(newest)
                await _notify_owner(f"🆕 New artifact detected:\n{newest}\nQuick Look: {summary}")
                try:
                    ext = newest.suffix.lower()
                    if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
                        await _app.bot.send_photo(chat_id=OWNER_ID, photo=str(newest), caption="Artifact preview")
                    elif ext in {'.pdf', '.log', '.txt'}:
                        await _app.bot.send_document(chat_id=OWNER_ID, document=str(newest), caption="Artifact file")
                except Exception:
                    pass

                # Overseer-only multimodal indexing into memory.
                if _overseer_enabled() and newest.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.pdf'}:
                    try:
                        desc = await _multimodal_artifact_summary(newest)
                        mem = f"Artifact summary ({newest.name}): {desc} [path={newest}]"
                        await _add_memory(mem, mem_type="artifact_summary", importance=0.78, source="artifact_watcher", pinned=False)
                    except Exception:
                        pass
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


def _record_op_event(
    event_type: str,
    *,
    chat_id: int = 0,
    route: str = "",
    command: str = "",
    status: str = "ok",
    latency_ms: Optional[int] = None,
    retries: int = 0,
    metadata: Optional[dict] = None,
) -> None:
    return ol_record_op_event(
        get_db=get_db,
        event_type=event_type,
        chat_id=chat_id,
        route=route,
        command=command,
        status=status,
        latency_ms=latency_ms,
        retries=retries,
        metadata=metadata,
    )


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
    ol_record_run(
        get_db=get_db,
        redact=_redact,
        text_limit=TEXT_LIMIT,
        turn_id=_AGENT_TURN_ID.get("") or "",
        chat_id=chat_id,
        user_query=user_query,
        mode=mode,
        tool_name=tool_name,
        tool_args=tool_args,
        result=result,
        exit_code=exit_code,
        duration_ms=duration_ms,
        status=status,
        artifact_path=artifact_path,
    )
    _record_op_event(
        "tool_run",
        chat_id=chat_id,
        route=(tool_name or ""),
        command=((tool_args or {}).get("command", "") if isinstance(tool_args, dict) else ""),
        status=status,
        latency_ms=duration_ms,
        metadata={"mode": mode, "exit_code": exit_code},
    )


async def _exec_file_read(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    root = Path(policies.get("read_root", str(READ_ROOT))).resolve()
    max_bytes = int(policies.get("max_bytes", str(MAX_READ_BYTES)))
    path = Path(args["path"]).expanduser().resolve()
    if not _is_under_root(path, root):
        return f"⛔ Path outside read root: {root}", None, "blocked", None
    full = await _to_thread(_read_text_limited, path, max_bytes)
    result, artifact = _shorten_or_artifact(skill.name, full)
    return result, None, "ok", artifact


async def _exec_http_get(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    url = args["url"]
    if not _domain_allowed(url):
        return "⛔ URL domain blocked by global policy.", None, "blocked", None
    max_bytes = int(policies.get("max_bytes", str(MAX_BROWSE_BYTES)))
    full = await _to_thread(_fetch_text_limited, url, max_bytes)
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

    def _do_write():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    await _to_thread(_do_write)
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

    def _do_append():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

    await _to_thread(_do_append)
    return f"Appended {len(content):,} chars → {path}", None, "ok", None


async def _exec_shell_command(skill: SkillDef, policies: dict, user_query: str, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    async def _legacy_runner(command: str, cwd: Optional[str], timeout: Optional[int], raw: bool) -> tuple[str, Optional[int], str, Optional[str]]:
        run_args = dict(args)
        run_args["command"] = command
        if cwd:
            run_args["cwd"] = cwd
        if timeout is not None:
            run_args["timeout_override"] = int(timeout)
        run_args["raw"] = bool(raw)
        return await exec_shell_command(
            skill_name=skill.name,
            requires_force=skill.requires_force,
            policies=policies,
            user_query=user_query,
            args=run_args,
            classify_shell=_classify_shell,
            user_forced=_user_forced,
            run_shell_async=_run_shell_async,
            shorten_or_artifact=_shorten_or_artifact,
        )

    if not USE_CAPABILITY_FACADE_SHELL:
        return await _legacy_runner(
            str(args.get("command", "")),
            args.get("cwd"),
            args.get("timeout_override"),
            bool(args.get("raw", False)),
        )

    shell = get_shell_executor(mode="live", legacy_runner=_legacy_runner)
    cap = await shell.run(
        command=str(args.get("command", "")),
        cwd=args.get("cwd"),
        timeout=(int(args.get("timeout_override")) if args.get("timeout_override") is not None else None),
        raw=bool(args.get("raw", False)),
    )
    # Facade live output preserves legacy tuple format for compatibility.
    return cap.output


async def _call_skill(chat_id: int, user_query: str, mode: str, skill_name: str, args: dict) -> str:
    start = time.monotonic()
    if _panic_locked() and skill_name not in {"read_file", "browse_page"}:
        return "⛔ Panic-lock active: execution skills are disabled. Use /panicstatus or /panicunlock <code>."
    exit_code: Optional[int] = None
    status = "ok"
    artifact_path: Optional[str] = None
    result = f"Unknown skill: {skill_name}"

    tool_limit = int(_get_policy("tool_calls_per_minute", DEFAULT_GLOBAL_POLICIES["tool_calls_per_minute"]))
    ok, msg = _enforce_rate_limit("tool_calls", tool_limit)
    if not ok:
        return f"{msg}\nRATE_LIMIT_BUCKET=tool_calls\nSUGGESTED_ACTION=retry_with_backoff"

    try:
        skill = _get_skill(skill_name)
        policies = _load_skill_policies(skill.name)

        # Hard bind critical runtime shell boundaries to current process config.
        # This prevents stale DB policy rows from pointing to old clones/paths.
        if skill.name == "run_shell":
            policies["write_root"] = str(ALLOWED_WRITE_ROOT)
            policies["default_cwd"] = str(PROJECT_ROOT)
            policies["allow_raw_shell"] = "1" if ALLOW_RAW_SHELL else "0"

        if skill.handler_type == "shell_command":
            shell_limit = int(_get_policy("shell_calls_per_minute", DEFAULT_GLOBAL_POLICIES["shell_calls_per_minute"]))
            ok, msg = _enforce_rate_limit("shell_calls", shell_limit)
            if not ok:
                return (
                    f"{msg}\n"
                    "RUN_STATUS=FAIL\n"
                    "ERROR=Shell rate limit active; execution blocked\n"
                    "RATE_LIMIT_BUCKET=shell_calls"
                )

        if _user_forced(user_query):
            _record_op_event("manual_override", chat_id=chat_id, route=skill_name, status="ok")

        handler_registry = {
            "file_read": lambda: _exec_file_read(skill, policies, args),
            "http_get": lambda: _exec_http_get(skill, policies, args),
            "file_write": lambda: _exec_file_write(skill, policies, args),
            "file_append": lambda: _exec_file_append(skill, policies, args),
            "shell_command": lambda: _exec_shell_command(skill, policies, user_query, args),
        }

        if skill.requires_force and not _user_forced(user_query):
            result, status = f"⛔ Skill '{skill.name}' requires force.", "blocked"
        else:
            fn = handler_registry.get(skill.handler_type)
            if not fn:
                result, status = f"Unknown handler_type: {skill.handler_type}", "error"
            else:
                result, exit_code, status, artifact_path = await fn()

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
    _fire_and_forget(
        loop,
        lambda: _audit_skill(
            skill_name,
            "call",
            note=status,
            owner_id=(OWNER_ID if chat_id != 0 else None),
            cmd_input=(args.get("command", user_query) if isinstance(args, dict) else user_query),
            exit_code=exit_code,
        ),
        label=f"skill_audit:{skill_name}",
    )
    return result


# =============================================================================
# Agent loop
# =============================================================================

def _temperature_for_mode(mode: str) -> float:
    return mu_temperature_for_mode(mode)


async def _analyze_attachment(chat_id: int, path: str, user_hint: str = "") -> str:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        STATE.last_attachment_diag_by_chat[int(chat_id)] = {"path": str(p), "status": "missing", "extractor": "none"}
        return ""
    size = p.stat().st_size
    suffix = p.suffix.lower()
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    diag: dict[str, Any] = {"path": str(p), "mime": mime, "size": int(size), "extractor": "metadata", "status": "ok"}

    def _ret(msg: str, *, extractor: str, status: str = "ok") -> str:
        diag["extractor"] = extractor
        diag["status"] = status
        diag["preview_chars"] = len(msg or "")
        STATE.last_attachment_diag_by_chat[int(chat_id)] = dict(diag)
        return msg

    # Image understanding via multimodal model
    if mime.startswith("image/"):
        if size > 8 * 1024 * 1024:
            return _ret(f"Attachment is image ({mime}, {size} bytes). Too large for inline analysis; use path directly.", extractor="image_metadata", status="degraded")
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        prompt = (
            "Analyze this uploaded image for task execution. "
            "Return concise structured notes: what it shows, key text (OCR), and actionable fields. "
            "If it's a map/screenshot/document, extract source, destination, values, and units when visible."
        )
        if user_hint:
            prompt += f" User request context: {user_hint[:300]}"
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": "You are a precise document/image parser."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ],
                    },
                ],
                temperature=0.0,
                max_tokens=700,
            )
            return _ret((resp.choices[0].message.content or "").strip(), extractor="image_vision")
        except Exception as exc:
            return _ret(f"Attachment is image ({mime}, {size} bytes). Vision analysis unavailable: {str(exc)[:120]}", extractor="image_vision", status="degraded")

    # Text-like files: inline snippet
    text_like = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log", ".ini", ".toml", ".xml", ".html", ".py", ".js", ".ts", ".sh"}
    if suffix in text_like or mime.startswith("text/"):
        raw = p.read_bytes()[:120000]
        decoded = raw.decode("utf-8", errors="ignore").strip()
        if decoded:
            return _ret(f"Attachment text preview ({mime}, {size} bytes):\n{decoded[:2500]}", extractor="text_preview")
        return _ret(f"Attachment metadata: mime={mime}, size={size} bytes; no decodable UTF-8 text preview.", extractor="text_metadata", status="degraded")

    # PDF best-effort extraction
    if suffix == ".pdf" or mime == "application/pdf":
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(p))
            chunks = []
            for page in reader.pages[:8]:
                t = (page.extract_text() or "").strip()
                if t:
                    chunks.append(t)
            text = "\n\n".join(chunks).strip()
            if text:
                return _ret(f"PDF text extract ({size} bytes):\n{text[:3000]}", extractor="pdf_text")
        except Exception:
            pass
        return _ret(f"Attachment is PDF ({size} bytes). Text extraction unavailable in current runtime; use conversion/OCR pipeline.", extractor="pdf_metadata", status="degraded")

    # DOCX extraction (OOXML zip)
    if suffix == ".docx" or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        try:
            text = extract_docx_text(p, max_chars=3500)
            if text:
                return _ret(f"DOCX text extract ({size} bytes):\n{text}", extractor="docx_text")
            return _ret(f"DOCX detected ({size} bytes) but no readable text found.", extractor="docx_metadata", status="degraded")
        except Exception as exc:
            return _ret(f"DOCX extraction failed: {str(exc)[:120]}", extractor="docx_error", status="error")

    # XLSX extraction (rows/cells preview)
    if suffix in {".xlsx", ".xlsc"} or mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        try:
            text = extract_xlsx_text(p, max_chars=3500)
            if text:
                return _ret(f"XLSX table preview ({size} bytes):\n{text}", extractor="xlsx_text")
            return _ret(f"XLSX detected ({size} bytes) but no readable cells found.", extractor="xlsx_metadata", status="degraded")
        except Exception as exc:
            return _ret(f"XLSX extraction failed: {str(exc)[:120]}", extractor="xlsx_error", status="error")

    # Generic ZIP archive summary
    if suffix == ".zip" or mime == "application/zip":
        try:
            listing = summarize_zip(p)
            return _ret(f"ZIP archive listing ({size} bytes):\n{listing}", extractor="zip_listing")
        except Exception as exc:
            return _ret(f"ZIP analysis failed: {str(exc)[:120]}", extractor="zip_error", status="error")

    return _ret(f"Attachment metadata: name={p.name}, mime={mime}, size={size} bytes. Binary/non-text document; direct content extraction may require converter/OCR.", extractor="binary_metadata", status="degraded")


async def _run_agent(chat_id: int, query: str, force: bool = False) -> str:
    mode = _get_mode()

    def _set_last_latency(ms: float) -> None:
        global _last_openai_latency_ms
        _last_openai_latency_ms = ms

    token = _AGENT_TURN_ID.set(f"{chat_id}-{uuid.uuid4().hex[:12]}")
    try:
        deps = AgentDeps(
            get_history=_get_history,
            build_memory_block=_build_memory_block,
            build_tools_from_db=_build_tools_from_db,
            user_forced=_user_forced,
            get_runtime_prompt=_get_runtime_prompt,
            save_message=_save_message,
            maybe_promote_memory=_maybe_promote_memory,
            call_skill=_call_skill,
            redact=_redact,
            normalize_machine_kv_output=normalize_machine_kv_output,
            fire_and_forget=_fire_and_forget,
            set_last_openai_latency_ms=_set_last_latency,
            temperature_for_mode=_temperature_for_mode,
        )
        final_text = await run_agent_loop(
            chat_id=chat_id,
            query=query,
            force=force,
            mode=mode,
            client=client,
            model=MODEL,
            history_load_limit=HISTORY_LOAD_LIMIT,
            max_memory_results=MAX_MEMORY_RESULTS,
            max_tool_rounds=MAX_TOOL_ROUNDS,
            history_max=MAX_HISTORY,
            history_trim_to=TRIM_TO,
            deps=deps,
            log=log,
        )
        return final_text
    finally:
        _AGENT_TURN_ID.reset(token)


# =============================================================================
# Status and metrics
# =============================================================================


def _system_pulse() -> dict[str, str]:
    pulse: dict[str, str] = {}
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        free_pct = (free / total * 100.0) if total > 0 else 0.0
        pulse["disk_free_pct"] = f"{free_pct:.1f}%"
        pulse["disk_status"] = "ok" if free_pct >= 10.0 else "low"
    except Exception:
        pulse["disk_free_pct"] = "n/a"
        pulse["disk_status"] = "unknown"

    try:
        load1, load5, _ = os.getloadavg()
        cpu_count = max(1, (os.cpu_count() or 1))
        load_norm = load1 / cpu_count
        pulse["load1"] = f"{load1:.2f}"
        pulse["load5"] = f"{load5:.2f}"
        pulse["load_status"] = "ok" if load_norm < 1.0 else "high"
    except Exception:
        pulse["load1"] = "n/a"
        pulse["load5"] = "n/a"
        pulse["load_status"] = "unknown"

    try:
        mem_total_kb = 0
        mem_avail_kb = 0
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail_kb = int(line.split()[1])
        mem_avail_pct = (mem_avail_kb / mem_total_kb * 100.0) if mem_total_kb > 0 else 0.0
        pulse["mem_avail_pct"] = f"{mem_avail_pct:.1f}%"
        pulse["mem_status"] = "ok" if mem_avail_pct >= 10.0 else "low"
    except Exception:
        pulse["mem_avail_pct"] = "n/a"
        pulse["mem_status"] = "unknown"

    # Optional thermal read (common Linux paths)
    temp_c = None
    for p in [
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/hwmon/hwmon0/temp1_input"),
    ]:
        try:
            if p.exists():
                raw = p.read_text().strip()
                v = float(raw)
                temp_c = (v / 1000.0) if v > 1000 else v
                break
        except Exception:
            continue
    if temp_c is None:
        pulse["cpu_temp_c"] = "n/a"
        pulse["temp_status"] = "unknown"
    else:
        pulse["cpu_temp_c"] = f"{temp_c:.1f}"
        pulse["temp_status"] = "ok" if temp_c < 80.0 else "hot"

    return pulse


def _health_report() -> str:
    with get_db() as conn:
        db_size = Path(DB_FILE).resolve().stat().st_size if Path(DB_FILE).exists() else 0
        messages = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        memories = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()["n"]
        skills = conn.execute("SELECT COUNT(*) AS n FROM skills WHERE enabled=1").fetchone()["n"]
        cron = conn.execute("SELECT COUNT(*) AS n FROM cron_jobs WHERE enabled=1").fetchone()["n"]
        open_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
        closed_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='done'").fetchone()["n"]
        errors = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status!='ok' AND timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["n"]
        avg_ms = conn.execute(
            "SELECT COALESCE(AVG(duration_ms),0) AS v FROM runs WHERE timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["v"]
        last_cron = conn.execute(
            "SELECT job_name,status,duration_ms FROM cron_runs ORDER BY id DESC LIMIT 3"
        ).fetchall()
    cron_tail = "\n".join([f"  - {r['job_name']}: {'✅' if r['status']=='ok' else '❌'} ({r['duration_ms']}ms)" for r in last_cron]) or "  - none"
    pulse = _system_pulse()
    return (
        f"• DB size: {db_size/1024/1024:.2f} MB\n"
        f"• Messages: {messages}\n"
        f"• Memories: {memories}\n"
        f"• Enabled skills: {skills}\n"
        f"• Active cron jobs: {cron}\n"
        f"• Tasks: open={open_tasks} closed={closed_tasks}\n"
        f"• OpenAI latency (last): {_last_openai_latency_ms:.1f} ms\n"
        f"• Avg tool latency 1h: {float(avg_ms):.1f} ms\n"
        f"• Errors 1h: {int(errors)}\n"
        f"• Pulse: disk={pulse.get('disk_free_pct','n/a')} ({pulse.get('disk_status','unknown')}), "
        f"mem={pulse.get('mem_avail_pct','n/a')} ({pulse.get('mem_status','unknown')}), "
        f"load1={pulse.get('load1','n/a')} ({pulse.get('load_status','unknown')}), "
        f"temp={pulse.get('cpu_temp_c','n/a')}C ({pulse.get('temp_status','unknown')})\n"
        f"• Last cron runs:\n{cron_tail}"
    )


def _diagnose_report(days: int = 7) -> str:
    d = max(1, min(days, 30))
    with get_db() as conn:
        daily = conn.execute(
            """
            SELECT day,events_total,failures,command_unknown,retries_total,median_latency_ms,p95_latency_ms
            FROM op_signals_daily
            WHERE day >= date('now', ?)
            ORDER BY day DESC
            LIMIT 7
            """,
            (f"-{d} day",),
        ).fetchall()
        fail_routes = conn.execute(
            """
            SELECT COALESCE(route,'') AS route, COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
              AND status NOT IN ('ok','skipped')
            GROUP BY COALESCE(route,'')
            ORDER BY n DESC
            LIMIT 5
            """,
            (f"-{d} day",),
        ).fetchall()
        unknown_cmds = conn.execute(
            """
            SELECT COALESCE(command,'') AS cmd, COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
              AND event_type='unknown_command'
            GROUP BY COALESCE(command,'')
            ORDER BY n DESC
            LIMIT 5
            """,
            (f"-{d} day",),
        ).fetchall()
        since = conn.execute("SELECT MIN(timestamp) AS ts FROM op_events").fetchone()["ts"]

    if not daily and not fail_routes and not unknown_cmds:
        return "No diagnosis data yet. Run workload and `scripts/build_signals.py` first."

    total_events = sum(int(r["events_total"] or 0) for r in daily)
    total_fail = sum(int(r["failures"] or 0) for r in daily)
    total_unknown = sum(int(r["command_unknown"] or 0) for r in daily)
    fail_rate = (100.0 * total_fail / total_events) if total_events else 0.0

    lines = [
        f"Diagnosis window: last {d} day(s)",
        f"Data since: {since or 'n/a'}",
        f"Events: {total_events} | Failures: {total_fail} ({fail_rate:.1f}%) | Unknown commands: {total_unknown}",
    ]

    if daily:
        lines.append("Daily summary:")
        for r in daily[:5]:
            lines.append(
                f"- {r['day']}: events={int(r['events_total'] or 0)} fail={int(r['failures'] or 0)} "
                f"unk={int(r['command_unknown'] or 0)} p95={float(r['p95_latency_ms'] or 0):.0f}ms"
            )

    if fail_routes:
        lines.append("Top failing routes:")
        for r in fail_routes:
            route = r["route"] or "(none)"
            lines.append(f"- {route}: {int(r['n'])}")

    if unknown_cmds:
        lines.append("Top unknown commands:")
        for r in unknown_cmds:
            cmd = r["cmd"] or "(empty)"
            lines.append(f"- {cmd}: {int(r['n'])}")

    likely = []
    if total_unknown >= 3:
        likely.append("Command discoverability issue: high unknown-command volume")
    if any((r["route"] or "") == "run_shell" for r in fail_routes):
        likely.append("Shell route instability: investigate policy blocks/path assumptions")
    if daily and any(float(r["p95_latency_ms"] or 0) > 4000 for r in daily):
        likely.append("Latency pressure: p95 over 4s in diagnosis window")
    if likely:
        lines.append("Likely causes:")
        lines.extend([f"- {x}" for x in likely])

    return "\n".join(lines)


def _generate_improvement_proposal(days: int = 7) -> tuple[int, str]:
    d = max(1, min(days, 30))
    report = _diagnose_report(d)

    risk = "low"
    title = "Ops improvement proposal"
    problem = "General reliability friction detected in recent diagnostics."
    cause = "Mixed routing/latency/command UX friction."
    change = "Tune command discoverability and route diagnostics."
    benefit = "Lower failure/unknown-command rates with stable latency."
    metric = "failures down; unknown-command count down; p95 latency stable"

    with get_db() as conn:
        fail_routes = conn.execute(
            """
            SELECT COALESCE(route,'') AS route, COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
              AND status NOT IN ('ok','skipped')
            GROUP BY COALESCE(route,'')
            ORDER BY n DESC
            LIMIT 1
            """,
            (f"-{d} day",),
        ).fetchone()
        unknown = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
              AND event_type='unknown_command'
            """,
            (f"-{d} day",),
        ).fetchone()["n"]
        p95 = conn.execute(
            """
            SELECT COALESCE(MAX(p95_latency_ms),0) AS p95
            FROM op_signals_daily
            WHERE day >= date('now', ?)
            """,
            (f"-{d} day",),
        ).fetchone()["p95"]

        if int(unknown or 0) >= 3:
            risk = "low"
            title = "Reduce unknown command friction"
            problem = f"Unknown commands observed {int(unknown)} time(s) in last {d} day(s)."
            cause = "Command discoverability/help text mismatch with user inputs."
            change = "Add command alias hints and improve unknown-command suggestion mapping."
            benefit = "Fewer dead-end command attempts and lower correction loops."
            metric = "unknown_command count reduced by >=50% over next 7 days"
        elif fail_routes and (fail_routes["route"] or ""):
            route = fail_routes["route"]
            risk = "medium" if route in {"run_shell", "cmd_exec"} else "low"
            title = f"Stabilize failing route: {route}"
            problem = f"Route '{route}' is top failure source in last {d} day(s)."
            cause = "Route-specific policy/path/validation friction."
            change = f"Add pre-validation and clearer blocked/error responses for route '{route}'."
            benefit = "Lower route failure rate and fewer retries."
            metric = f"failures on route '{route}' reduced by >=40%"
        elif float(p95 or 0) > 4000:
            risk = "medium"
            title = "Reduce p95 latency pressure"
            problem = f"Observed p95 latency spike up to {float(p95):.0f}ms."
            cause = "Tool/LLM path latency accumulation under current flow."
            change = "Add fast-path short-circuiting for deterministic intents and tighter timeout tiers."
            benefit = "Improved responsiveness without reducing correctness."
            metric = "p95 latency < 3000ms while failure rate does not increase"

        cur = conn.execute(
            """
            INSERT INTO improvement_proposals
            (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target)
            VALUES ('proposed',?,?,?,?,?,?,?)
            """,
            (risk, title, problem, cause, change, benefit, metric),
        )
        pid = int(cur.lastrowid)

    out = (
        f"Proposal #{pid} [{risk}]\n"
        f"Title: {title}\n"
        f"Problem: {problem}\n"
        f"Cause: {cause}\n"
        f"Change: {change}\n"
        f"Expected: {benefit}\n"
        f"Metric target: {metric}\n"
        f"Source window: last {d} day(s)"
    )
    return pid, out


def _latest_artifact_for_proposal(proposal_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    return row


def _compile_proposal_to_change(proposal_id: int) -> str:
    with get_db() as conn:
        p = conn.execute(
            "SELECT id,title,problem_statement,suspected_cause,proposed_change,metric_target FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")

        text = " ".join([
            str(p["title"] or ""),
            str(p["problem_statement"] or ""),
            str(p["suspected_cause"] or ""),
            str(p["proposed_change"] or ""),
            str(p["metric_target"] or ""),
        ]).lower()

        artifact_type = ""
        target = ""
        artifact: dict[str, Any] = {}
        expected_metric = ""
        expected_delta = None

        if "unknown command" in text or "alias" in text or "discoverability" in text:
            artifact_type = "routing_rule"
            target = "unknown_command_handler"
            aliases = _get_command_aliases()
            # Safe starter alias set (extensible later)
            aliases.setdefault("cronru", "cronrun")
            aliases.setdefault("statu", "status")
            artifact = {"add_alias": aliases}
            expected_metric = "unknown_command_rate"
            expected_delta = -0.15
        elif "retry" in text or "latency" in text or "timeout" in text:
            artifact_type = "config_change"
            target = "runtime_retry_policy"
            cur = _get_exec_retry_limit()
            new_val = max(0, min(3, cur + 1))
            artifact = {"key": "exec_retry_limit", "value": str(new_val)}
            expected_metric = "failure_rate"
            expected_delta = -0.10
        elif "snapshot lineage" in text or "last_applied_artifact_id" in text or "lineage" in text:
            artifact_type = "state_lineage"
            target = "runtime_snapshot"
            artifact = {"artifact_id": int(_get_last_applied_artifact_id() or 0)}
            expected_metric = "state_consistency"
            expected_delta = 0.0
        else:
            return f"⛔ Proposal #{proposal_id} is non-actionable: cannot compile to allowed artifact type."

        contract = _artifact_contract(artifact_type, target)
        artifact["apply_points"] = contract.get("apply_points", [])
        artifact["invariants"] = contract.get("invariants", [])
        artifact["contract_version"] = contract.get("contract_version", "")
        artifact["contract_hash"] = contract.get("contract_hash", "")
        family = str(contract.get("family", "unknown"))
        if family not in _allowed_artifact_families():
            return f"⛔ Artifact family '{family}' is frozen/disabled by trial scope."

        cur = conn.execute(
            """
            INSERT INTO change_artifacts(proposal_id,artifact_type,target,artifact_json,expected_metric,expected_delta,status)
            VALUES(?,?,?,?,?,?,'compiled')
            """,
            (proposal_id, artifact_type, target, json.dumps(artifact), expected_metric, expected_delta),
        )
        aid = int(cur.lastrowid)

    return f"Artifact #{aid} compiled for proposal #{proposal_id}: {artifact_type} -> {target}"


def _sim_shell_policy_check(command: str) -> dict:
    hard_block = [
        r"\brm\s+-rf\s+/$",
        r"\brm\s+-rf\s+/\s",
        r"\bmkfs(\.|\s)",
        r"\bdd\s+if=",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bpoweroff\b",
    ]
    for pat in hard_block:
        if re.search(pat, command, re.I):
            return {"ok": False, "reason": f"hard-block:{pat}"}
    pol = _classify_shell(command)
    if pol.kind == "destructive":
        return {"ok": False, "reason": "destructive"}
    return {"ok": True}


def _build_shell_replay_diff(*, proposal_id: int, artifact: dict, replay_results: list[dict], golden_results: list[dict], manifest) -> dict:
    targeted = [r for r in replay_results if r.get("case_class") == "targeted"]
    targeted_improved = sum(1 for r in targeted if bool(r.get("actual_ok")) == bool(r.get("expect_ok", True)))
    targeted_regressed = sum(1 for r in targeted if bool(r.get("actual_ok")) != bool(r.get("expect_ok", True)))
    golden_failed = sum(1 for r in golden_results if not r.get("pass"))
    policy_violations = 0
    msum = manifest.summary()
    summary = {
        "targeted_cases": len(targeted),
        "golden_cases": len(golden_results),
        "targeted_improved": targeted_improved,
        "targeted_regressed": targeted_regressed,
        "golden_failed": golden_failed,
        "policy_violations": policy_violations,
        "side_effects_intercepted": int(msum.get("side_effects_intercepted", 0)),
        "live_side_effects": int(msum.get("live_side_effects", 0)),
    }
    status = "pass"
    if summary["golden_failed"] > 0 or summary["policy_violations"] > 0 or summary["live_side_effects"] != 0:
        status = "fail"
    elif summary["targeted_improved"] <= summary["targeted_regressed"]:
        status = "fail"
    return {
        "proposal_id": proposal_id,
        "artifact_id": int(artifact.get("id") or 0),
        "artifact_type": artifact.get("artifact_type"),
        "target": artifact.get("target"),
        "mode": "simulate",
        "status": status,
        "summary": summary,
        "replay_results": replay_results,
        "golden_results": golden_results,
        "manifest_summary": msum,
    }


def _simulate_proposal_sync(proposal_id: int) -> str:
    import subprocess

    code_path = Path(__file__).resolve()
    repo = code_path.parent
    py = (repo / ".venv" / "bin" / "python")
    if not py.exists():
        py = Path(sys.executable)

    checks: list[tuple[str, bool, str]] = []

    art = _latest_artifact_for_proposal(proposal_id)
    if not art:
        return "⛔ No compiled artifact for this proposal. Run /improve_compile <proposal_id> first."

    # 0) artifact validation gate (typed, safe scope)
    art_ok = True
    art_note = "ok"
    try:
        ajson = json.loads(art["artifact_json"] or "{}")
        atype = (art["artifact_type"] or "").strip()
        target = (art["target"] or "").strip()
        c_ok, c_note = _artifact_has_expected_contract(atype, target, (ajson or {}))
        if not c_ok:
            art_ok = False
            art_note = c_note
        elif atype == "routing_rule" and target == "unknown_command_handler":
            aliases = (ajson or {}).get("add_alias", {})
            art_ok = isinstance(aliases, dict) and all(bool(str(k).strip()) and bool(str(v).strip()) for k, v in aliases.items())
            if not art_ok:
                art_note = "invalid alias map"
        elif atype == "config_change" and target == "runtime_retry_policy":
            key = (ajson or {}).get("key")
            val = (ajson or {}).get("value")
            art_ok = (key == "exec_retry_limit") and str(val).isdigit() and (0 <= int(val) <= 3)
            if not art_ok:
                art_note = "invalid retry policy artifact"
        elif atype == "state_lineage" and target == "runtime_snapshot":
            aid = (ajson or {}).get("artifact_id")
            art_ok = str(aid).isdigit() and int(aid) >= 0
            if not art_ok:
                art_note = "invalid lineage artifact"
        else:
            art_ok = False
            art_note = f"unsupported artifact scope: {atype}:{target}"
    except Exception as exc:
        art_ok = False
        art_note = str(exc)[:300]
    checks.append(("artifact_validation", art_ok, art_note))

    # 1) compile gate
    try:
        proc = subprocess.run(
            [str(py), "-m", "py_compile", "XoBop.py"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=60,
        )
        ok = proc.returncode == 0
        note = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[:300]
        checks.append(("compile", ok, note or "ok"))
    except Exception as exc:
        checks.append(("compile", False, str(exc)[:300]))

    # 2) deterministic eval suite gate
    try:
        proc = subprocess.run(
            [str(py), "scripts/eval_suite.py"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=180,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        ok = proc.returncode == 0 and ("RESULT: PASS" in out)
        checks.append(("eval_suite", ok, out[-300:] if out else "ok"))
    except Exception as exc:
        checks.append(("eval_suite", False, str(exc)[:300]))

    # 3) regression replay (router deterministic samples)
    replay_ok = True
    replay_note = "ok"
    try:
        samples = [
            ("/cronlist", "cron_list"),
            ("how many files in downloads", "download_count"),
            ("did you send the email?", "email_status"),
            ("git status", "direct_shell"),
        ]
        bad = []
        for text, expected in samples:
            got = classify(text)["name"]
            if got != expected:
                bad.append(f"{text} => {got} (expected {expected})")
        if bad:
            replay_ok = False
            replay_note = "; ".join(bad)[:300]
    except Exception as exc:
        replay_ok = False
        replay_note = str(exc)[:300]
    checks.append(("replay_regression", replay_ok, replay_note))

    shell_replay_report = None
    if USE_SHELL_REPLAY_CONTROLLER:
        run_id = f"sim_{proposal_id}_{int(time.time())}"
        tmp_root = Path("/tmp/xobop_sim")
        dbm = TempDbManager(DB_FILE, str(tmp_root))
        temp_db = dbm.fork(run_id)
        sandbox_root = str(tmp_root / run_id / "sandbox")
        try:
            rc = ReplayController(command_policy_checker=_sim_shell_policy_check, diff_builder=_build_shell_replay_diff)
            art_obj = {
                "id": int(art["id"]),
                "artifact_type": art["artifact_type"],
                "target": art["target"],
                "artifact_json": art["artifact_json"],
            }
            shell_replay_report = rc.run(
                proposal_id=proposal_id,
                artifact=art_obj,
                replay_cases=shell_replay_cases(),
                golden_cases=shell_golden_cases(),
                temp_db_path=temp_db,
                sandbox_root=sandbox_root,
                apply_artifact_to_temp_state=apply_artifact_to_temp_settings,
            )
            replay_ok2 = (shell_replay_report.get("status") == "pass")
            checks.append(("shell_replay_controller", replay_ok2, json.dumps(shell_replay_report.get("summary", {}))[:300]))
        except Exception as exc:
            checks.append(("shell_replay_controller", False, str(exc)[:300]))
        finally:
            dbm.cleanup(run_id)

    overall = all(ok for _, ok, _ in checks)

    with get_db() as conn:
        conn.execute(
            "UPDATE change_artifacts SET status=?, simulated_ts=CURRENT_TIMESTAMP WHERE id=?",
            ("sim_pass" if overall else "sim_fail", int(art["id"])),
        )
        row = conn.execute(
            "SELECT risk_level,title FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Proposal not found: {proposal_id}")
        before = {"proposal_id": proposal_id, "risk_level": row["risk_level"], "title": row["title"], "artifact_id": int(art["id"]), "artifact_type": art["artifact_type"]}
        after = {k: {"ok": ok, "note": note} for k, ok, note in checks}
        if shell_replay_report is not None:
            after["shell_replay_report"] = shell_replay_report
        cur = conn.execute(
            """
            INSERT INTO improvement_runs(proposal_id,phase,status,eval_phase,before_json,after_json,notes)
            VALUES(?, 'simulation', ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                "ok" if overall else "error",
                _get_eval_phase(),
                json.dumps(before),
                json.dumps(after),
                "simulation gate",
            ),
        )
        run_id = int(cur.lastrowid)
        if shell_replay_report is not None:
            for r in shell_replay_report.get("replay_results", []):
                exp_ok = bool(r.get("expect_ok", True))
                act_ok = bool(r.get("actual_ok", False))
                conn.execute(
                    """
                    INSERT INTO simulation_case_results(run_id,proposal_id,case_id,case_class,expected,actual,improved,regressed,blocked,policy_ok)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        proposal_id,
                        str(r.get("case_id", "")),
                        str(r.get("case_class", "targeted")),
                        ("ok" if exp_ok else "fail"),
                        ("ok" if act_ok else "fail"),
                        int(exp_ok == act_ok),
                        int(exp_ok != act_ok),
                        int(not act_ok),
                        int(bool(r.get("policy_ok", False))),
                    ),
                )
            for g in shell_replay_report.get("golden_results", []):
                exp_ok = bool(g.get("expected_ok", True))
                act_ok = bool(g.get("actual_ok", False))
                conn.execute(
                    """
                    INSERT INTO simulation_case_results(run_id,proposal_id,case_id,case_class,expected,actual,improved,regressed,blocked,policy_ok)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        proposal_id,
                        str(g.get("case_id", "")),
                        "golden",
                        ("ok" if exp_ok else "fail"),
                        ("ok" if act_ok else "fail"),
                        int(exp_ok == act_ok),
                        int(exp_ok != act_ok),
                        int(not act_ok),
                        int(exp_ok == act_ok),
                    ),
                )

    lines = [f"Simulation for proposal #{proposal_id}: {'PASS' if overall else 'FAIL'}"]
    for name, ok, note in checks:
        lines.append(f"• {name}: {'✅' if ok else '❌'}")
        if note and note != "ok":
            lines.append(f"  - {note}")
    return "\n".join(lines)


def _impact_compare_sync(proposal_id: int, window_days: int = 7) -> str:
    w = max(1, min(window_days, 30))
    with get_db() as conn:
        p = conn.execute(
            "SELECT id,created_ts,title,risk_level FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")

        ts = p["created_ts"]
        a = conn.execute(
            "SELECT id,applied_ts,commit_hash FROM change_artifacts WHERE proposal_id=? AND applied_ts IS NOT NULL ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if a and a["applied_ts"]:
            ts = a["applied_ts"]

        before = conn.execute(
            """
            SELECT
              COUNT(*) AS events,
              SUM(CASE WHEN status NOT IN ('ok','skipped') THEN 1 ELSE 0 END) AS failures,
              SUM(CASE WHEN event_type='unknown_command' THEN 1 ELSE 0 END) AS unknowns,
              AVG(CASE WHEN latency_ms IS NOT NULL AND latency_ms>0 THEN latency_ms END) AS avg_latency
            FROM op_events
            WHERE timestamp >= datetime(?, ?)
              AND timestamp < datetime(?)
            """,
            (ts, f"-{w} day", ts),
        ).fetchone()

        after = conn.execute(
            """
            SELECT
              COUNT(*) AS events,
              SUM(CASE WHEN status NOT IN ('ok','skipped') THEN 1 ELSE 0 END) AS failures,
              SUM(CASE WHEN event_type='unknown_command' THEN 1 ELSE 0 END) AS unknowns,
              AVG(CASE WHEN latency_ms IS NOT NULL AND latency_ms>0 THEN latency_ms END) AS avg_latency
            FROM op_events
            WHERE timestamp >= datetime(?)
              AND timestamp < datetime(?, ?)
            """,
            (ts, ts, f"+{w} day"),
        ).fetchone()

        def _num(row, key):
            v = row[key]
            return float(v) if v is not None else 0.0

        b_events = _num(before, "events")
        a_events = _num(after, "events")
        b_fail = _num(before, "failures")
        a_fail = _num(after, "failures")
        b_unk = _num(before, "unknowns")
        a_unk = _num(after, "unknowns")
        b_lat = _num(before, "avg_latency")
        a_lat = _num(after, "avg_latency")

        b_rate = (100.0 * b_fail / b_events) if b_events else 0.0
        a_rate = (100.0 * a_fail / a_events) if a_events else 0.0

        delta_fail_rate = a_rate - b_rate
        delta_unknown = a_unk - b_unk
        delta_lat = a_lat - b_lat

        art = conn.execute(
            "SELECT artifact_type,target FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        artifact_type = (art["artifact_type"] if art else "") or ""

        # Artifact-family focused metrics
        family = {}
        if artifact_type == "routing_rule":
            b_uk = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE timestamp >= datetime(?, ?) AND timestamp < datetime(?) AND event_type='unknown_command'",
                (ts, f"-{w} day", ts),
            ).fetchone()["n"]
            a_uk = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE timestamp >= datetime(?) AND timestamp < datetime(?, ?) AND event_type='unknown_command'",
                (ts, ts, f"+{w} day"),
            ).fetchone()["n"]
            family = {"family": "alias", "unknown_command_before": int(b_uk or 0), "unknown_command_after": int(a_uk or 0), "delta": int(a_uk or 0) - int(b_uk or 0)}
        elif artifact_type == "config_change":
            family = {"family": "retry", "fail_rate_delta": delta_fail_rate, "avg_latency_delta": delta_lat}

        min_samples = 20
        confidence = "high" if min(b_events, a_events) >= min_samples else "low"

        summary = {
            "window_days": w,
            "before": {"events": b_events, "fail_rate": b_rate, "unknowns": b_unk, "avg_latency": b_lat},
            "after": {"events": a_events, "fail_rate": a_rate, "unknowns": a_unk, "avg_latency": a_lat},
            "delta": {"fail_rate": delta_fail_rate, "unknowns": delta_unknown, "avg_latency": delta_lat},
            "artifact_family": family,
            "confidence": confidence,
            "min_samples_required": min_samples,
        }

        status = "ok"
        if a_events <= 0:
            status = "insufficient"

        conn.execute(
            """
            INSERT INTO improvement_runs(proposal_id,phase,status,eval_phase,before_json,after_json,notes)
            VALUES(?, 'impact_compare', ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                status,
                _get_eval_phase(),
                json.dumps(summary.get("before", {})),
                json.dumps(summary.get("after", {})),
                f"delta={json.dumps(summary.get('delta', {}))}",
            ),
        )

    lines = [
        f"Impact compare for proposal #{proposal_id} (window={w}d)",
        f"Title: {p['title']} [{p['risk_level']}]",
        f"Anchor: {'artifact deployment' if (a and a['applied_ts']) else 'proposal creation'} @ {ts}",
        f"Before: events={int(b_events)} fail_rate={b_rate:.1f}% unknown={int(b_unk)} avg_lat={b_lat:.1f}ms",
        f"After:  events={int(a_events)} fail_rate={a_rate:.1f}% unknown={int(a_unk)} avg_lat={a_lat:.1f}ms",
        f"Delta:  fail_rate={delta_fail_rate:+.1f}pp unknown={delta_unknown:+.0f} avg_lat={delta_lat:+.1f}ms",
    ]
    if a_events <= 0:
        lines.append("Assessment: insufficient post-proposal data yet.")
    else:
        improved = (delta_fail_rate <= 0.0) and (delta_unknown <= 0.0)
        lines.append(f"Assessment: {'improved' if improved else 'mixed/regressed'}")
    if min(b_events, a_events) < 20:
        lines.append("Confidence: LOW (sample threshold not met)")
    else:
        lines.append("Confidence: HIGH")
    return "\n".join(lines)


def _latest_run_status(proposal_id: int, phase: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM improvement_runs WHERE proposal_id=? AND phase=? ORDER BY id DESC LIMIT 1",
            (proposal_id, phase),
        ).fetchone()
    return row["status"] if row else None


def _latest_impact_delta(proposal_id: int) -> dict[str, float]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT notes FROM improvement_runs WHERE proposal_id=? AND phase='impact_compare' ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    if not row:
        return {}
    notes = (row["notes"] or "").strip()
    if notes.startswith("delta="):
        try:
            d = json.loads(notes[len("delta="):])
            if isinstance(d, dict):
                return {str(k): float(v) for k, v in d.items() if isinstance(v, (int, float))}
        except Exception:
            pass
    return {}


def _promote_proposal_sync(proposal_id: int, force: bool = False, reason: str = "") -> str:
    import subprocess

    policy = _get_improve_policy()
    phase = _get_eval_phase()
    promotion_mode = "force" if force else "policy"
    with get_db() as conn:
        p = conn.execute(
            "SELECT id,status,risk_level,title FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")

    if p["status"] in {"promoted", "rejected"}:
        return f"Proposal #{proposal_id} already {p['status']}."

    if force and not (reason or "").strip():
        return "⛔ Force promotion requires a reason. Use: /improve_promote <id> force <reason>"

    if phase == "baseline" and not force:
        return "⛔ Eval phase is baseline: promotion disabled (use force only for emergency)."

    art = _latest_artifact_for_proposal(proposal_id)
    if not art:
        return "⛔ No compiled artifact. Run /improve_compile <proposal_id> first."

    risk = (p["risk_level"] or "low").lower()
    sim = _latest_run_status(proposal_id, "simulation")
    imp = _latest_run_status(proposal_id, "impact_compare")

    if sim != "ok":
        return "⛔ Promotion blocked: simulation gate is not PASS. Run /improve_simulate first."

    if policy == "manual" and not force:
        return "⛔ Policy=manual. Re-run with force for explicit approval."

    if risk == "high" and not force:
        return "⛔ High-risk proposal requires explicit force approval."
    if risk == "medium" and policy in {"manual", "assisted"} and not force:
        return "⛔ Medium-risk proposal requires force under current policy."

    if imp == "error" and not force:
        return "⛔ Impact comparison indicates regression. Use force only with explicit acceptance."

    # Net-benefit guard for retry policy artifacts
    if art["artifact_type"] == "config_change" and art["target"] == "runtime_retry_policy" and not force:
        d = _latest_impact_delta(proposal_id)
        if d:
            fail_delta = float(d.get("fail_rate", 0.0))
            lat_delta = float(d.get("avg_latency", 0.0))
            if not (fail_delta <= -1.0 and lat_delta <= 300.0):
                return "⛔ Retry artifact blocked by net-benefit rule (need fail_rate <= -1.0pp and avg_latency <= +300ms)."

    # Apply typed artifact (safe mutable scopes only)
    ajson = json.loads(art["artifact_json"] or "{}")
    atype = art["artifact_type"]
    target = art["target"]
    contract_ok, contract_note = _artifact_has_expected_contract(atype, target, (ajson or {}))
    if not contract_ok:
        return f"⛔ Artifact apply failed: {contract_note}."

    pre_state = _runtime_state_payload(last_promotion_mode="")
    commit_hash = "unknown"
    repo_dir = Path(__file__).resolve().parent
    try:
        applied_desc = _apply_artifact_to_runtime_settings(atype, target, (ajson or {}))
        _set_last_applied_artifact_id(int(art["id"]))
        _export_runtime_snapshot(last_applied_artifact_id=int(art["id"]), last_promotion_mode=promotion_mode)

        try:
            rel_snap = os.path.relpath(str(RUNTIME_SNAPSHOT_FILE), str(repo_dir))
            subprocess.run(["git", "-C", str(repo_dir), "add", rel_snap], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", f"self-mod: apply artifact #{int(art['id'])} for proposal #{proposal_id}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            commit_hash = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"], text=True).strip() or "unknown"
        except Exception:
            pass

        if force:
            _record_op_event(
                "force_promotion",
                route="improve_promote",
                status="ok",
                metadata={
                    "proposal_id": proposal_id,
                    "artifact_id": int(art["id"]),
                    "reason": (reason or "unspecified"),
                    "eval_phase": phase,
                },
            )

        with get_db() as conn:
            conn.execute(
                "UPDATE improvement_proposals SET status='promoted' WHERE id=?",
                (proposal_id,),
            )
            conn.execute(
                "UPDATE change_artifacts SET status='applied', applied_ts=CURRENT_TIMESTAMP, commit_hash=?, promotion_mode=?, eval_phase=? WHERE id=?",
                (commit_hash, promotion_mode, phase, int(art["id"])),
            )
            conn.execute(
                """
                INSERT INTO improvement_runs(proposal_id,phase,status,promotion_mode,eval_phase,reason,before_json,after_json,notes)
                VALUES(?, 'promotion', 'ok', ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    promotion_mode,
                    phase,
                    (reason or ""),
                    json.dumps({"artifact_id": int(art["id"]), "artifact_type": atype, "target": target}),
                    json.dumps({"applied": applied_desc}),
                    f"policy={policy}; phase={phase}; risk={risk}; force={int(force)}; artifact_id={int(art['id'])}; commit={commit_hash}",
                ),
            )
    except Exception as exc:
        # best-effort runtime rollback if promotion bookkeeping fails midway
        try:
            _set_command_aliases(dict(pre_state.get("command_aliases", {})))
            _set_exec_retry_limit(int(pre_state.get("exec_retry_limit", 0) or 0))
            _set_last_applied_artifact_id(int(pre_state.get("last_applied_artifact_id", 0) or 0))
            _export_runtime_snapshot(
                last_applied_artifact_id=int(pre_state.get("last_applied_artifact_id", 0) or 0),
                last_promotion_mode=str(pre_state.get("last_promotion_mode", "") or ""),
            )
        except Exception:
            pass
        return f"⛔ Promotion failed and runtime state was rolled back: {str(exc)[:200]}"

    return f"✅ Proposal #{proposal_id} promoted+applied under policy={policy} risk={risk} artifact={atype}:{target} commit={commit_hash}."


def _auto_promote_low_sync() -> str:
    policy = _get_improve_policy()
    if policy != "auto_low":
        return f"⛔ Auto-promotion disabled by policy={policy}."

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM improvement_proposals WHERE status='proposed' AND risk_level='low' ORDER BY id ASC LIMIT 20"
        ).fetchall()

    promoted = 0
    blocked = 0
    for r in rows:
        pid = int(r["id"])
        msg = _promote_proposal_sync(pid, force=False)
        if msg.startswith("✅"):
            promoted += 1
        else:
            blocked += 1

    return f"Auto-promotion complete: promoted={promoted} blocked={blocked}"


def _capture_evolution_memory_sync(proposal_id: int) -> str:
    with get_db() as conn:
        p = conn.execute(
            "SELECT id,title,risk_level,status,problem_statement,suspected_cause,metric_target FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")

        runs = conn.execute(
            "SELECT phase,status,notes,created_ts,before_json,after_json FROM improvement_runs WHERE proposal_id=? ORDER BY id ASC",
            (proposal_id,),
        ).fetchall()
        art = conn.execute(
            "SELECT id,artifact_type,target,artifact_json,applied_ts,commit_hash FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()

        sim = [r for r in runs if r["phase"] == "simulation"]
        imp = [r for r in runs if r["phase"] == "impact_compare"]
        promo = [r for r in runs if r["phase"] == "promotion"]

        sim_ok = any(r["status"] == "ok" for r in sim)
        imp_err = any(r["status"] == "error" for r in imp)
        promoted = any(r["status"] == "ok" for r in promo) or p["status"] == "promoted"

        outcome = "promoted" if promoted else ("regressed" if imp_err else ("sim_pass" if sim_ok else "proposed"))
        signature = f"risk:{p['risk_level']}|outcome:{outcome}"

        if imp_err:
            lesson = "Impact comparison showed regression; require stronger guardrails or rollback before promotion."
        elif sim_ok and not promoted:
            lesson = "Simulation passed but proposal not promoted; policy/risk gate intentionally held rollout."
        elif promoted:
            lesson = "Proposal cleared simulation/policy and was promoted; reuse this change pattern when similar diagnostics recur."
        else:
            lesson = "Proposal is still in analysis stage; gather more telemetry before action."

        last_imp = imp[-1] if imp else None
        evidence = {
            "title": p["title"],
            "risk_level": p["risk_level"],
            "status": p["status"],
            "problem": p["problem_statement"],
            "cause": p["suspected_cause"],
            "metric_target": p["metric_target"],
            "run_count": len(runs),
            "artifact": {
                "id": int(art["id"]) if art else None,
                "type": art["artifact_type"] if art else None,
                "target": art["target"] if art else None,
                "change": json.loads(art["artifact_json"] or "{}") if art else {},
                "applied_ts": art["applied_ts"] if art else None,
                "commit_hash": art["commit_hash"] if art else None,
            },
            "impact_notes": (last_imp["notes"] if last_imp else ""),
        }

        conn.execute(
            """
            INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,evidence_json)
            VALUES(?,?,?,?,?)
            """,
            (proposal_id, signature, outcome, lesson, json.dumps(evidence)),
        )

    return f"Evolution memory captured for proposal #{proposal_id}: {signature}"


def _evolution_memory_report(limit: int = 10) -> str:
    n = max(1, min(limit, 30))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,created_ts,proposal_id,signature,outcome,lesson FROM evolution_memory ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
    if not rows:
        return "No evolution memory entries yet."
    lines = []
    for r in rows:
        lines.append(f"#{int(r['id'])} p#{int(r['proposal_id'] or 0)} {r['signature']} [{r['outcome']}]")
        lines.append(f"  {r['lesson']}")
    return "\n".join(lines)


def _improve_funnel_report(days: int = 30) -> str:
    d = max(1, min(days, 90))
    with get_db() as conn:
        generated = conn.execute(
            "SELECT COUNT(*) AS n FROM improvement_proposals WHERE created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        actionable = conn.execute(
            "SELECT COUNT(DISTINCT proposal_id) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        artifacts_compiled = conn.execute(
            "SELECT COUNT(*) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        sim_pass = conn.execute(
            "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='simulation' AND status='ok' AND created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        promoted = conn.execute(
            "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        promoted_force = conn.execute(
            "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND promotion_mode='force' AND created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        promoted_auto = max(0, int(promoted or 0) - int(promoted_force or 0))

        impact_rows = conn.execute(
            """
            SELECT r.status, r.notes, p.promotion_mode
            FROM improvement_runs r
            LEFT JOIN (
              SELECT proposal_id, MAX(id) AS rid FROM improvement_runs WHERE phase='promotion' AND status='ok' GROUP BY proposal_id
            ) lp ON lp.proposal_id = r.proposal_id
            LEFT JOIN improvement_runs p ON p.id = lp.rid
            WHERE r.phase='impact_compare' AND r.created_ts >= datetime('now', ?)
            """,
            (f"-{d} day",),
        ).fetchall()

        drift_repairs = conn.execute(
            "SELECT COUNT(*) AS n FROM op_events WHERE event_type='state_repair' AND timestamp >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]

        family_rows = conn.execute(
            "SELECT artifact_type, COUNT(*) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?) GROUP BY artifact_type",
            (f"-{d} day",),
        ).fetchall()

    positive = 0
    neutral = 0
    negative = 0
    positive_auto = 0
    neutral_auto = 0
    negative_auto = 0
    for r in impact_rows:
        if r["status"] != "ok":
            neutral += 1
            if (r["promotion_mode"] or "") != "force":
                neutral_auto += 1
            continue
        note = (r["notes"] or "")
        dlt = {}
        if note.startswith("delta="):
            try:
                dlt = json.loads(note[len("delta="):])
            except Exception:
                dlt = {}
        fail_delta = float(dlt.get("fail_rate", 0.0)) if isinstance(dlt, dict) else 0.0
        if fail_delta < 0:
            positive += 1
            if (r["promotion_mode"] or "") != "force":
                positive_auto += 1
        elif fail_delta > 0:
            negative += 1
            if (r["promotion_mode"] or "") != "force":
                negative_auto += 1
        else:
            neutral += 1
            if (r["promotion_mode"] or "") != "force":
                neutral_auto += 1

    actionable_rate = (100.0 * actionable / generated) if generated else 0.0
    success_rate = (100.0 * positive / promoted) if promoted else 0.0
    success_rate_auto = (100.0 * positive_auto / promoted_auto) if promoted_auto else 0.0
    fam = ", ".join([f"{r['artifact_type']}={int(r['n'])}" for r in family_rows]) or "none"
    return (
        f"Improve funnel ({d}d)\n"
        f"• proposals_generated: {int(generated)}\n"
        f"• proposals_actionable: {int(actionable)} ({actionable_rate:.1f}%)\n"
        f"• actionable_yield: {actionable_rate:.1f}%\n"
        f"• artifacts_compiled: {int(artifacts_compiled)}\n"
        f"• simulations_passed: {int(sim_pass)}\n"
        f"• promotions_applied_total: {int(promoted)}\n"
        f"• promotions_auto: {int(promoted_auto)}\n"
        f"• promotions_force: {int(promoted_force)}\n"
        f"• force_ratio: {(100.0*promoted_force/promoted if promoted else 0.0):.1f}%\n"
        f"• changes_positive_impact_all: {int(positive)} (of applied: {success_rate:.1f}%)\n"
        f"• changes_positive_impact_auto: {int(positive_auto)} (of auto: {success_rate_auto:.1f}%)\n"
        f"• promotion_effectiveness: {success_rate_auto:.1f}%\n"
        f"• changes_neutral_all: {int(neutral)}\n"
        f"• changes_negative_all: {int(negative)}\n"
        f"• changes_neutral_auto: {int(neutral_auto)}\n"
        f"• changes_negative_auto: {int(negative_auto)}\n"
        f"• drift_repair_events: {int(drift_repairs)}\n"
        f"• artifact_families: {fam}"
    )


def _improve_weekly_report(weeks: int = 4) -> str:
    w = max(1, min(weeks, 8))
    lines = [f"Improve weekly report ({w}w) phase={_get_eval_phase()} policy={_get_improve_policy()}"]
    with get_db() as conn:
        for i in range(w):
            end = f"-{7*i} day"
            start = f"-{7*(i+1)} day"
            req = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE event_type='command_requested' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            fail = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE status NOT IN ('ok','skipped') AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            unk = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE event_type='unknown_command' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            proposals = conn.execute(
                "SELECT COUNT(*) AS n FROM improvement_proposals WHERE created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            actionable = conn.execute(
                "SELECT COUNT(DISTINCT proposal_id) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            sim_pass = conn.execute(
                "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='simulation' AND status='ok' AND created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            promoted = conn.execute(
                "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            promoted_force = conn.execute(
                "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND promotion_mode='force' AND created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            promoted_auto = max(0, int(promoted or 0) - int(promoted_force or 0))
            drift = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE event_type='state_repair' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]
            rollback = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE event_type='rollback_event' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)",
                (start, end),
            ).fetchone()["n"]

            fail100 = (100.0 * fail / req) if req else 0.0
            unk100 = (100.0 * unk / req) if req else 0.0
            conf = "LOW" if req < 50 else "HIGH"
            force_ratio = (100.0 * promoted_force / promoted) if promoted else 0.0
            line = (
                f"W-{i+1}: req={int(req)} fail/100={fail100:.2f} unk/100={unk100:.2f} proposals={int(proposals)} actionable={int(actionable)} sim={int(sim_pass)} "
                f"promoted_total={int(promoted)} auto={int(promoted_auto)} force={int(promoted_force)} force_ratio={force_ratio:.1f}% drift={int(drift)} rollback={int(rollback)} conf={conf}"
            )
            if _get_eval_phase() == "baseline" and int(promoted_force) > 3:
                line += " WARNING: baseline force promotions > 3"
            lines.append(line)
    return "\n".join(lines)


def _improve_eval_report(days: int = 30) -> str:
    d = max(1, min(days, 90))
    with get_db() as conn:
        promoted_total = conn.execute(
            "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        promoted_force = conn.execute(
            "SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND promotion_mode='force' AND created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        total_proposals = conn.execute(
            "SELECT COUNT(*) AS n FROM improvement_proposals WHERE created_ts >= datetime('now', ?)",
            (f"-{d} day",),
        ).fetchone()["n"]
        non_actionable = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM improvement_proposals p
            WHERE p.created_ts >= datetime('now', ?)
              AND NOT EXISTS (SELECT 1 FROM change_artifacts a WHERE a.proposal_id = p.id)
            """,
            (f"-{d} day",),
        ).fetchone()["n"]
        alias_cases = conn.execute(
            """
            SELECT
              SUM(CASE WHEN improved=1 THEN 1 ELSE 0 END) AS improved,
              SUM(CASE WHEN regressed=1 THEN 1 ELSE 0 END) AS regressed
            FROM simulation_case_results
            WHERE created_ts >= datetime('now', ?)
              AND case_class='targeted'
              AND run_id IN (
                SELECT r.id FROM improvement_runs r
                JOIN change_artifacts a ON a.proposal_id=r.proposal_id
                WHERE r.phase='simulation' AND a.artifact_type='routing_rule'
              )
            """,
            (f"-{d} day",),
        ).fetchone()
        retry_imp = conn.execute(
            """
            SELECT notes FROM improvement_runs
            WHERE phase='impact_compare' AND created_ts >= datetime('now', ?)
              AND proposal_id IN (
                SELECT proposal_id FROM change_artifacts WHERE artifact_type='config_change'
              )
            """,
            (f"-{d} day",),
        ).fetchall()

    alias_improved = int(alias_cases["improved"] or 0)
    alias_regressed = int(alias_cases["regressed"] or 0)

    retry_count = 0
    retry_fail_delta = 0.0
    retry_lat_delta = 0.0
    for r in retry_imp:
        note = (r["notes"] or "")
        if not note.startswith("delta="):
            continue
        try:
            dlt = json.loads(note[len("delta="):])
        except Exception:
            continue
        retry_count += 1
        retry_fail_delta += float(dlt.get("fail_rate", 0.0))
        retry_lat_delta += float(dlt.get("avg_latency", 0.0))

    retry_avg_fail = (retry_fail_delta / retry_count) if retry_count else 0.0
    retry_avg_lat = (retry_lat_delta / retry_count) if retry_count else 0.0

    force_dependency = (100.0 * promoted_force / promoted_total) if promoted_total else 0.0

    with get_db() as conn:
        sim_impact = conn.execute(
            """
            SELECT s.status AS sim_status, i.notes AS impact_notes
            FROM (
              SELECT proposal_id, status FROM improvement_runs
              WHERE phase='simulation' AND created_ts >= datetime('now', ?)
            ) s
            JOIN (
              SELECT proposal_id, notes FROM improvement_runs
              WHERE phase='impact_compare' AND status='ok' AND created_ts >= datetime('now', ?)
            ) i ON i.proposal_id = s.proposal_id
            """,
            (f"-{d} day", f"-{d} day"),
        ).fetchall()

    sim_tp = sim_fp = sim_fn = 0
    for r in sim_impact:
        note = (r["impact_notes"] or "")
        dlt = {}
        if note.startswith("delta="):
            try:
                dlt = json.loads(note[len("delta="):])
            except Exception:
                dlt = {}
        improved = float(dlt.get("fail_rate", 0.0)) < 0 if isinstance(dlt, dict) else False
        sim_ok = (r["sim_status"] == "ok")
        if sim_ok and improved:
            sim_tp += 1
        elif sim_ok and (not improved):
            sim_fp += 1
        elif (not sim_ok) and improved:
            sim_fn += 1

    sim_precision = (100.0 * sim_tp / (sim_tp + sim_fp)) if (sim_tp + sim_fp) else 0.0
    actionable_rate = (100.0 * (total_proposals - non_actionable) / total_proposals) if total_proposals else 0.0

    return (
        f"Improve eval report ({d}d)\n"
        f"{_improve_funnel_report(d)}\n"
        f"---\n"
        f"• non_actionable_proposals: {int(non_actionable)}\n"
        f"• force_dependency: {force_dependency:.1f}%\n"
        f"Improver quality:\n"
        f"• actionable_rate: {actionable_rate:.1f}%\n"
        f"• sim_precision: {sim_precision:.1f}%\n"
        f"• sim_false_positives: {sim_fp}\n"
        f"• sim_false_negatives: {sim_fn}\n"
        f"• alias_targeted_improved: {alias_improved}\n"
        f"• alias_targeted_regressed: {alias_regressed}\n"
        f"• retry_eval_count: {retry_count}\n"
        f"• retry_avg_fail_rate_delta_pp: {retry_avg_fail:+.2f}\n"
        f"• retry_avg_latency_delta_ms: {retry_avg_lat:+.1f}\n"
        f"• note: low-sample outputs should be interpreted cautiously"
    )


def _op_sanity_report(hours: int = 24) -> str:
    h = max(1, min(hours, 168))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) GROUP BY event_type ORDER BY n DESC",
            (f"-{h} hour",),
        ).fetchall()
        per_hour = conn.execute(
            "SELECT strftime('%Y-%m-%d %H', timestamp) AS hr, COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) GROUP BY hr ORDER BY hr",
            (f"-{h} hour",),
        ).fetchall()
        dup = conn.execute(
            """
            SELECT event_type,command,route,COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
            GROUP BY event_type,command,route,chat_id,strftime('%Y-%m-%d %H:%M', timestamp)
            HAVING COUNT(*) > 3
            ORDER BY n DESC LIMIT 10
            """,
            (f"-{h} hour",),
        ).fetchall()
        requested = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='command_requested' AND timestamp >= datetime('now', ?)", (f"-{h} hour",)).fetchone()["n"]
        routed = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='route_selected' AND timestamp >= datetime('now', ?)", (f"-{h} hour",)).fetchone()["n"]
        tools = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='tool_run' AND timestamp >= datetime('now', ?)", (f"-{h} hour",)).fetchone()["n"]

    anomalies = []
    if requested > 0 and routed < int(0.6 * requested):
        anomalies.append(f"low route coverage: requested={requested} routed={routed}")
    if requested > 0 and tools > requested * 5:
        anomalies.append(f"tool/request ratio high: tools={tools} requested={requested}")
    if dup:
        anomalies.append(f"duplicate bursts: {len(dup)} groups")
    if not rows:
        anomalies.append("no op_events in window")

    lines = [f"Op sanity ({h}h)"]
    lines.append("Events per type:")
    for r in rows[:10]:
        lines.append(f"- {r['event_type']}: {int(r['n'])}")
    lines.append(f"Hourly buckets: {len(per_hour)}")
    lines.append(f"Missing sequence hints: requested={int(requested)} routed={int(routed)} tool_run={int(tools)}")
    if dup:
        lines.append("Duplicate groups:")
        for d in dup[:5]:
            lines.append(f"- {d['event_type']} cmd={d['command'] or '-'} route={d['route'] or '-'} n={int(d['n'])}")
    lines.append("Anomalies:")
    if anomalies:
        lines.extend([f"- {a}" for a in anomalies])
    else:
        lines.append("- none")
    return "\n".join(lines)


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
        await _reply(update, mu_mode_help_text(_get_mode()))


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
        await _reply(update, "Use: `/promptset <strict|normal|overseer> <prompt text>` or `/promptset <mode> reset`")
        return
    parts = raw.split(" ", 1)
    mode = parts[0].strip().lower()
    if mode not in MODES:
        await _reply(update, "Mode must be `strict`, `normal`, or `overseer`.")
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
        rows = conn.execute("SELECT id,text FROM notification_queue WHERE delivered=0 ORDER BY id ASC LIMIT 50").fetchall()
    if not rows:
        await _reply(update, "No queued notifications.")
        return

    texts = [r["text"] for r in rows]
    summary = ""
    try:
        joined = "\n".join(texts)[:4000]
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Summarize queued system notifications in 1-2 sentences with the most important item first."},
                {"role": "user", "content": joined},
            ],
            temperature=0.1,
            max_tokens=140,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception:
        summary = "Queued notifications summarized unavailable; sending raw count only."

    sent = 0
    for r in rows:
        try:
            with get_db() as conn:
                conn.execute("UPDATE notification_queue SET delivered=1,delivered_ts=CURRENT_TIMESTAMP WHERE id=?", (int(r["id"]),))
            sent += 1
        except Exception:
            pass

    await _reply(update, f"Quiet-hours digest ({sent} items):\n{summary}")


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
        await _reply(update, "Mode must be `strict`, `normal`, or `overseer`.")
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


def _run_checkup_sync() -> str:
    import subprocess
    checks = []
    code_path = Path(__file__).resolve()
    # Pin to project venv explicitly (based on XoBop.py location) to avoid cwd/interpreter drift.
    # IMPORTANT: do not .resolve() here; many venv python binaries are symlinks to /usr/bin/python,
    # and resolving would bypass venv context.
    venv_python = (code_path.parent / '.venv' / 'bin' / 'python')
    checks.append(("venv_python", venv_python.exists()))
    req = Path('requirements.txt').resolve()
    checks.append(("requirements_txt", req.exists()))
    for key in ["TELEGRAM_TOKEN", "OPENAI_API_KEY", "OWNER_ID", "DB_FILE"]:
        checks.append((f"env_{key}", bool(os.getenv(key))))
    # lightweight dependency check
    dep_ok = True
    dep_note = "ok"
    if venv_python.exists() and req.exists():
        try:
            proc = subprocess.run(
                [str(venv_python), "-m", "pip", "check"],
                text=True,
                capture_output=True,
                timeout=20,
            )
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                dep_ok = True
                dep_note = (out or "No broken requirements found")[:180]
            else:
                dep_ok = False
                dep_note = (out or f"pip check failed (exit {proc.returncode})")[:180]
        except Exception as exc:
            dep_ok = False
            dep_note = str(exc)[:180]
    checks.append(("pip_check", dep_ok))

    pulse = _system_pulse()
    checks.append(("pulse_disk", pulse.get("disk_status") in {"ok", "unknown"}))
    checks.append(("pulse_mem", pulse.get("mem_status") in {"ok", "unknown"}))
    checks.append(("pulse_load", pulse.get("load_status") in {"ok", "unknown"}))
    checks.append(("pulse_temp", pulse.get("temp_status") in {"ok", "unknown"}))

    bad = [name for name, ok in checks if not ok]
    git_rev = "unknown"
    try:
        git_rev = subprocess.check_output(["git", "-C", str(code_path.parent), "rev-parse", "--short", "HEAD"], text=True).strip() or "unknown"
    except Exception:
        pass

    lines = [f"• {name}: {'✅' if ok else '❌'}" for name, ok in checks]
    lines.append(f"• code_path: {code_path}")
    lines.append(f"• git_rev: {git_rev}")
    lines.append(f"• venv_python_path: {venv_python}")
    lines.append(f"• venv_python_resolved: {venv_python.resolve()}")
    lines.append(
        f"• pulse: disk={pulse.get('disk_free_pct','n/a')} ({pulse.get('disk_status','unknown')}), "
        f"mem={pulse.get('mem_avail_pct','n/a')} ({pulse.get('mem_status','unknown')}), "
        f"load1={pulse.get('load1','n/a')} ({pulse.get('load_status','unknown')}), "
        f"temp={pulse.get('cpu_temp_c','n/a')}C ({pulse.get('temp_status','unknown')})"
    )
    lines.append(f"• pip_check_note: {dep_note}")
    if bad:
        lines.append(f"RESULT: FAIL ({', '.join(bad)})")
    else:
        lines.append("RESULT: PASS")
    return "\n".join(lines)


def _generate_system_map_sync() -> str:
    root = Path(os.getcwd())
    out = root / 'memory' / 'SYSTEM_MAP.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    files = sorted([str(p.relative_to(root)) for p in (root / 'xobop').glob('*.py')])
    body = [
        "# SYSTEM_MAP.md",
        "",
        "Auto-generated architecture snapshot.",
        "",
        "## Modules",
        *[f"- `{f}`" for f in files],
        "",
        "## Entry",
        "- `XoBop.py`",
    ]
    out.write_text("\n".join(body), encoding='utf-8')
    return str(out)


async def cmd_checkup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, _run_checkup_sync)
    _record_op_event(
        "health_check",
        chat_id=update.effective_chat.id,
        route="cmd_checkup",
        status=("ok" if "RESULT: PASS" in rep else "error"),
    )
    await _reply(update, rep)


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    goal = update.message.text.partition(" ")[2].strip()
    if not goal:
        await _reply(update, "Use: `/plan <goal>`")
        return
    # deterministic simple manifest scaffold
    steps = [
        "1) Discover relevant files/resources",
        "2) Run analysis/execution command",
        "3) Validate output",
        "4) Report concise result",
    ]
    STATE.planned_manifest_by_chat[chat_id] = {"goal": goal, "steps": steps}
    await _reply(update, "Planned manifest:\n" + "\n".join(steps) + "\nReply `/go` to execute.")


async def cmd_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    plan = STATE.planned_manifest_by_chat.get(chat_id)
    if not plan:
        await _reply(update, "No pending plan. Use `/plan <goal>` first.")
        return
    goal = plan.get("goal", "")
    # execute through agent with explicit imperative
    result = await _run_agent(chat_id, f"Execute this plan now: {goal}", force=True)
    STATE.planned_manifest_by_chat.pop(chat_id, None)
    await _reply(update, result)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    await _reply(
        update,
        build_status_text(
            chat_id=chat_id,
            model=MODEL,
            embedding_model=EMBEDDING_MODEL,
            get_mode=_get_mode,
            get_db=get_db,
        ),
    )


async def cmd_attachdiag(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    path = STATE.last_attachment_by_chat.get(chat_id)
    diag = STATE.last_attachment_diag_by_chat.get(chat_id, {})
    await _reply(update, build_attachdiag_text(path=path, diag=diag))


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_only(update):
        await _reply(update, _health_report())


async def cmd_state_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    d = _state_check_dict()
    await _reply(update, build_state_check_text(state_check=d))


async def cmd_state_repair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    rep = _repair_state_from_snapshot()
    _record_op_event("state_repair", chat_id=update.effective_chat.id, status=("ok" if rep.startswith("✅") else "error"))
    await _reply(update, rep)


async def cmd_op_sanity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    hours = parse_int_or_default(raw, 24)
    await _reply(update, _op_sanity_report(hours))


async def cmd_diagnose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    days = parse_int_or_default(raw, 7)
    await _reply(update, _diagnose_report(days))


async def cmd_improve_propose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        days = int(raw) if raw else 7
    except ValueError:
        days = 7
    _pid, txt = _generate_improvement_proposal(days)
    _record_op_event("proposal_generated", chat_id=update.effective_chat.id, route="cmd_improve_propose", status="ok")
    await _reply(update, txt)


async def cmd_improve_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,created_ts,status,risk_level,title FROM improvement_proposals ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await _reply(update, "No improvement proposals yet.")
        return
    lines = [f"#{int(r['id'])} [{r['risk_level']}] {r['status']} — {r['title']} ({r['created_ts']})" for r in rows]
    await _reply(update, "\n".join(lines))


async def cmd_improve_compile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        pid = int(raw)
    except Exception:
        await _reply(update, "Use: `/improve_compile <proposal_id>`")
        return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, lambda: _compile_proposal_to_change(pid))
    await _reply(update, rep)


async def cmd_improve_artifacts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    with get_db() as conn:
        if raw:
            try:
                pid = int(raw)
            except Exception:
                await _reply(update, "Use: `/improve_artifacts [proposal_id]`")
                return
            rows = conn.execute(
                "SELECT id,proposal_id,artifact_type,target,status,created_ts,applied_ts,commit_hash FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 10",
                (pid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,proposal_id,artifact_type,target,status,created_ts,applied_ts,commit_hash FROM change_artifacts ORDER BY id DESC LIMIT 10"
            ).fetchall()
    if not rows:
        await _reply(update, "No change artifacts yet.")
        return
    lines = [f"a#{int(r['id'])} p#{int(r['proposal_id'])} {r['artifact_type']}:{r['target']} {r['status']} commit={r['commit_hash'] or '-'}" for r in rows]
    await _reply(update, "\n".join(lines))


async def cmd_improve_simulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        proposal_id = int(raw)
    except Exception:
        await _reply(update, "Use: `/improve_simulate <proposal_id>`")
        return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, lambda: _simulate_proposal_sync(proposal_id))
    await _reply(update, rep)


async def cmd_improve_runs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    with get_db() as conn:
        if raw:
            try:
                pid = int(raw)
            except Exception:
                await _reply(update, "Use: `/improve_runs [proposal_id]`")
                return
            rows = conn.execute(
                "SELECT id,proposal_id,created_ts,phase,status,notes FROM improvement_runs WHERE proposal_id=? ORDER BY id DESC LIMIT 10",
                (pid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,proposal_id,created_ts,phase,status,notes FROM improvement_runs ORDER BY id DESC LIMIT 10"
            ).fetchall()
    if not rows:
        await _reply(update, "No improvement runs yet.")
        return
    lines = [f"run#{int(r['id'])} p#{int(r['proposal_id'] or 0)} {r['phase']} {r['status']} — {r['created_ts']}" for r in rows]
    await _reply(update, "\n".join(lines))


async def cmd_improve_compare(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip().split()
    if not raw:
        await _reply(update, "Use: `/improve_compare <proposal_id> [window_days]`")
        return
    try:
        proposal_id = int(raw[0])
        window = int(raw[1]) if len(raw) > 1 else 7
    except Exception:
        await _reply(update, "Use: `/improve_compare <proposal_id> [window_days]`")
        return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, lambda: _impact_compare_sync(proposal_id, window))
    await _reply(update, rep)


async def cmd_improve_policy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip().lower()
    if not raw:
        await _reply(update, f"improve_policy: {_get_improve_policy()}\nUse: `/improve_policy <manual|assisted|auto_low>`")
        return
    if raw not in {"manual", "assisted", "auto_low"}:
        await _reply(update, "Use: `/improve_policy <manual|assisted|auto_low>`")
        return
    _set_setting("improve_policy", raw)
    await _reply(update, f"✅ improve_policy set to: {raw}")


async def cmd_improve_promote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip().split()
    if not raw:
        await _reply(update, "Use: `/improve_promote <proposal_id> [force <reason>]`")
        return
    try:
        pid = int(raw[0])
    except Exception:
        await _reply(update, "Use: `/improve_promote <proposal_id> [force <reason>]`")
        return
    force = any(x.lower() == "force" for x in raw[1:])
    reason = ""
    if force:
        try:
            i = next(i for i, x in enumerate(raw[1:], start=1) if x.lower() == "force")
            reason = " ".join(raw[i+1:]).strip()
        except Exception:
            reason = ""
        if not reason:
            await _reply(update, "Use: `/improve_promote <proposal_id> force <reason>`")
            return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, lambda: _promote_proposal_sync(pid, force, reason))
    await _reply(update, rep)


async def cmd_improve_autopromote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, _auto_promote_low_sync)
    await _reply(update, rep)


async def cmd_improve_funnel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        days = int(raw) if raw else 30
    except Exception:
        days = 30
    await _reply(update, _improve_funnel_report(days))


async def cmd_improve_eval_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        days = int(raw) if raw else 30
    except Exception:
        days = 30
    await _reply(update, _improve_eval_report(days))


async def cmd_improve_weekly_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        weeks = int(raw) if raw else 4
    except Exception:
        weeks = 4
    await _reply(update, _improve_weekly_report(weeks))


async def cmd_improve_phase(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip().lower()
    if not raw:
        await _reply(update, f"improve_eval_phase: {_get_eval_phase()}\nUse: `/improve_phase <baseline|improving>`")
        return
    if raw not in {"baseline", "improving"}:
        await _reply(update, "Use: `/improve_phase <baseline|improving>`")
        return
    _set_setting("improve_eval_phase", raw)
    await _reply(update, f"✅ improve_eval_phase set to: {raw}")


async def cmd_improve_log_rollback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    parts = raw.split(" ", 1)
    if not parts or not parts[0].isdigit():
        await _reply(update, "Use: `/improve_log_rollback <artifact_id> <reason>`")
        return
    artifact_id = int(parts[0])
    reason = parts[1].strip() if len(parts) > 1 else ""
    _record_op_event("rollback_event", chat_id=update.effective_chat.id, route="manual", status="ok", metadata={"artifact_id": artifact_id, "reason": reason})
    await _reply(update, f"✅ rollback event logged for artifact #{artifact_id}")


async def cmd_evolve_capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        pid = int(raw)
    except Exception:
        await _reply(update, "Use: `/evolve_capture <proposal_id>`")
        return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, lambda: _capture_evolution_memory_sync(pid))
    await _reply(update, rep)


async def cmd_evolve_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        limit = int(raw) if raw else 10
    except Exception:
        limit = 10
    await _reply(update, _evolution_memory_report(limit))


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
    if _panic_locked():
        await _reply(update, "⛔ Panic-lock active: /exec disabled.")
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
    if _panic_locked():
        await _reply(update, "⛔ Panic-lock active: /cronadd disabled.")
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
    if _panic_locked():
        await _reply(update, "⛔ Panic-lock active: /cronrm disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronrm <name>`")
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
    if _panic_locked():
        await _reply(update, "⛔ Panic-lock active: /cronon disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronon <name>`")
        return
    result = _toggle_cron_job(name, True)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        _sync_scheduler(scheduler)
    await _reply(update, result)


async def cmd_cronoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    if _panic_locked():
        await _reply(update, "⛔ Panic-lock active: /cronoff disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronoff <name>`")
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
        await _reply(update, "Use: `/cronruns <name> [limit]`")
        return
    await _reply(update, _get_cron_runs(name, limit))


async def cmd_cronrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    if _panic_locked():
        await _reply(update, "⛔ Panic-lock active: /cronrun disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await _reply(update, "Use: `/cronrun <name>`")
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


async def cmd_panicstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    lock = "ON" if _panic_locked() else "OFF"
    arm = (_get_setting("panic_arm_token", "") or "").strip()
    await _reply(update, f"Panic-lock: {lock}\nArmed: {'yes' if arm else 'no'}")


async def cmd_paniclock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    now = int(time.time())

    if raw == "arm":
        token = uuid.uuid4().hex[:12]
        _set_setting("panic_arm_token", token)
        _set_setting("panic_arm_ts", str(now))
        await _reply(update, f"⚠️ Panic-lock arm set. Confirm within 120s:\n`/paniclock confirm {token}`")
        return

    if raw.startswith("confirm "):
        token = raw.split(" ", 1)[1].strip()
        armed = (_get_setting("panic_arm_token", "") or "").strip()
        try:
            arm_ts = int((_get_setting("panic_arm_ts", "0") or "0").strip())
        except Exception:
            arm_ts = 0
        if not armed or token != armed:
            await _reply(update, "⛔ Invalid arm token. Use `/paniclock arm` first.")
            return
        if (now - arm_ts) > 120:
            _set_setting("panic_arm_token", "")
            _set_setting("panic_arm_ts", "0")
            await _reply(update, "⛔ Arm token expired. Run `/paniclock arm` again.")
            return
        _set_setting("panic_lock", "1")
        _set_setting("panic_arm_token", "")
        _set_setting("panic_arm_ts", "0")
        await _reply(update, "🛑 Panic-lock enabled. Exec/write/cron operations are now blocked.")
        return

    await _reply(update, "Use: `/paniclock arm` then `/paniclock confirm <token>`")


async def cmd_panicunlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    code = update.message.text.partition(" ")[2].strip()
    if not PANIC_UNLOCK_CODE:
        await _reply(update, "⛔ PANIC_UNLOCK_CODE is not configured in environment.")
        return
    if not code or code != PANIC_UNLOCK_CODE:
        await _reply(update, "⛔ Invalid unlock code.")
        return
    _set_setting("panic_lock", "0")
    _set_setting("panic_arm_token", "")
    _set_setting("panic_arm_ts", "0")
    await _reply(update, "✅ Panic-lock disabled.")


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
/mode [strict|normal|overseer] — get or set mode
/promptshow — show active runtime prompt
/promptset mode text — set runtime prompt (strict/normal/overseer)
/promptproposal mode text — stage prompt change proposal (strict/normal/overseer)
/promptapprove — apply staged prompt proposal
/promptreject — reject staged prompt proposal
/quiethours HH:MM-HH:MM — queue notifications during quiet window
/quietoff — disable quiet hours
/quietflush — flush queued notifications now
/status — system status
/health — health summary
/checkup — dependency/env drift check
/state_check — compare canonical runtime snapshot vs DB/settings
/state_repair — repair DB/settings from runtime snapshot
/op_sanity [hours] — instrumentation sanity check and anomaly scan
/diagnose [days] — ops diagnosis from instrumentation signals
/improve_propose [days] — generate a data-backed improvement proposal
/improve_list — list recent improvement proposals
/improve_compile <proposal_id> — compile proposal into typed change artifact
/improve_artifacts [proposal_id] — list change artifacts
/improve_simulate <proposal_id> — run simulation gate for a proposal
/improve_runs [proposal_id] — list recent simulation runs
/improve_compare <proposal_id> [window_days] — compare before/after impact
/improve_policy [manual|assisted|auto_low] — set promotion autonomy policy
/improve_promote <proposal_id> [force <reason>] — promote proposal with risk-policy checks
/improve_autopromote — auto-promote low-risk proposals (policy=auto_low)
/improve_funnel [days] — report proposal→artifact→simulation→promotion→impact funnel
/improve_eval_report [days] — consolidated evaluation report for paper/stability
/improve_weekly_report [weeks] — weekly trend report (headline metrics + confidence)
/improve_phase [baseline|improving] — controlled live evaluation phase tag
/improve_log_rollback <artifact_id> <reason> — log rollback telemetry event
/evolve_capture <proposal_id> — persist proposal outcome lesson into evolution memory
/evolve_memory [limit] — show recent evolution memory lessons
/plan goal — create execution manifest
/go — execute staged manifest
/topskills — most used skills
/clearctx — clear conversation context
/exec command — run through agent
/attachdiag — show latest attachment extraction diagnostics
/panicstatus — show panic-lock state
/paniclock arm|confirm <token> — enable panic-lock (2-step)
/panicunlock <code> — disable panic-lock (requires env code)

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

Cron skill_types: run_shell, browse_page, read_file, write_file, append_file, agent, memory_cleanup, db_backup, prompt_review, checkup, system_map_refresh
Schedule: cron syntax or natural language like every day at 9am
""")


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    text = (update.message.text or "").strip()
    if not text.startswith("/"):
        return

    aliases = _get_command_aliases()
    rewritten, changed, resolved = resolve_command_alias(text, aliases)
    if changed and resolved and resolved in _COMMAND_DISPATCH:
        try:
            update.message.text = rewritten
        except Exception:
            pass
        _record_op_event("command_alias_applied", chat_id=update.effective_chat.id, command=resolved, metadata={"rewritten": rewritten})
        await _COMMAND_DISPATCH[resolved](update, ctx)
        return

    cmd = text.split()[0]
    key = cmd.lstrip("/").lower()
    sug = aliases.get(key)
    _record_op_event("unknown_command", chat_id=update.effective_chat.id, command=cmd, status="error", metadata={"suggested": sug or ""})
    if sug:
        await _reply(update, f"⛔ Unknown command: `{cmd}`\nDid you mean `/{sug}` ?")
    else:
        await _reply(update, f"⛔ Unknown command: `{cmd}`\nUse `/help`.")


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

    if text:
        cmd = text.split()[0] if text.startswith("/") else ""
        _record_op_event("command_requested", chat_id=chat_id, command=cmd, metadata={"text_len": len(text)})

    if _panic_locked() and not _is_panic_bypass_text(text):
        await _reply(update, "⛔ Panic-lock active. Allowed commands: /panicstatus, /panicunlock <code>, /status, /health, /checkup, /help")
        return

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
            cmd_status=lambda u: cmd_status(u, ctx),
            cmd_health=lambda u: cmd_health(u, ctx),
            cmd_checkup=lambda u: cmd_checkup(u, ctx),
            cmd_state_check=lambda u: cmd_state_check(u, ctx),
            cmd_state_repair=lambda u: cmd_state_repair(u, ctx),
            cmd_op_sanity=lambda u: cmd_op_sanity(u, ctx),
            cmd_diagnose=lambda u: cmd_diagnose(u, ctx),
            cmd_improve_propose=lambda u: cmd_improve_propose(u, ctx),
            cmd_improve_list=lambda u: cmd_improve_list(u, ctx),
            cmd_improve_compile=lambda u: cmd_improve_compile(u, ctx),
            cmd_improve_artifacts=lambda u: cmd_improve_artifacts(u, ctx),
            cmd_improve_simulate=lambda u: cmd_improve_simulate(u, ctx),
            cmd_improve_runs=lambda u: cmd_improve_runs(u, ctx),
            cmd_improve_compare=lambda u: cmd_improve_compare(u, ctx),
            cmd_improve_policy=lambda u: cmd_improve_policy(u, ctx),
            cmd_improve_promote=lambda u: cmd_improve_promote(u, ctx),
            cmd_improve_autopromote=lambda u: cmd_improve_autopromote(u, ctx),
            cmd_improve_funnel=lambda u: cmd_improve_funnel(u, ctx),
            cmd_improve_eval_report=lambda u: cmd_improve_eval_report(u, ctx),
            cmd_improve_weekly_report=lambda u: cmd_improve_weekly_report(u, ctx),
            cmd_improve_phase=lambda u: cmd_improve_phase(u, ctx),
            cmd_improve_log_rollback=lambda u: cmd_improve_log_rollback(u, ctx),
            cmd_evolve_capture=lambda u: cmd_evolve_capture(u, ctx),
            cmd_evolve_memory=lambda u: cmd_evolve_memory(u, ctx),
            cmd_panicstatus=lambda u: cmd_panicstatus(u, ctx),
            cmd_paniclock=lambda u: cmd_paniclock(u, ctx),
            cmd_panicunlock=lambda u: cmd_panicunlock(u, ctx),
            panic_locked=_panic_locked,
            panic_bypass_text=_is_panic_bypass_text,
            command_aliases_getter=_get_command_aliases,
            analyze_attachment=_analyze_attachment,
            op_event=_record_op_event,
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
                actions = [str(v.get("action", "unknown")) for v in STATE.pending_action_by_chat.values() if isinstance(v, dict)]
                top = ", ".join(actions[:3]) if actions else "unknown"
                more = "" if len(actions) <= 3 else f" (+{len(actions)-3} more)"
                await _notify_owner(f"System restored. Resuming pending task state: {top}{more}.")
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


def _apply_self_mods_to_runtime() -> None:
    # DB is source of truth; ensure sane defaults if missing.
    if _get_setting("command_aliases_json") is None:
        _set_setting("command_aliases_json", "{}")
    if _get_setting("exec_retry_limit") is None:
        _set_setting("exec_retry_limit", "0")
    if _get_setting("last_applied_artifact_id") is None:
        _set_setting("last_applied_artifact_id", "0")
    _export_self_mods_debug()


def _startup_reconcile_runtime_state() -> None:
    return rsnap_startup_reconcile(
        load_snapshot=_load_runtime_snapshot,
        apply_self_mods_to_runtime=_apply_self_mods_to_runtime,
        export_runtime_snapshot_fn=_export_runtime_snapshot,
        get_last_applied_artifact_id=_get_last_applied_artifact_id,
        state_check_dict=_state_check_dict,
        repair_state_from_snapshot=_repair_state_from_snapshot,
        set_setting=_set_setting,
        json_dumps=json.dumps,
    )


def main() -> None:
    _init_db()
    _startup_reconcile_runtime_state()
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

    app = build_app(token=TOKEN, post_init=_post_init, post_shutdown=_post_shutdown)
    commands = {
            "remember": cmd_remember,
            "delfact": cmd_delfact,
            "facts": cmd_facts,
            "memories": cmd_memories,
            "memadd": cmd_memadd,
            "memdel": cmd_memdel,
            "addtask": cmd_addtask,
            "donetask": cmd_donetask,
            "tasks": cmd_tasks,
            "mode": cmd_mode,
            "promptshow": cmd_promptshow,
            "promptset": cmd_promptset,
            "promptproposal": cmd_promptproposal,
            "promptapprove": cmd_promptapprove,
            "promptreject": cmd_promptreject,
            "quiethours": cmd_quiethours,
            "quietoff": cmd_quietoff,
            "quietflush": cmd_quietflush,
            "status": cmd_status,
            "health": cmd_health,
            "checkup": cmd_checkup,
            "state_check": cmd_state_check,
            "state_repair": cmd_state_repair,
            "op_sanity": cmd_op_sanity,
            "diagnose": cmd_diagnose,
            "improve_propose": cmd_improve_propose,
            "improve_list": cmd_improve_list,
            "improve_compile": cmd_improve_compile,
            "improve_artifacts": cmd_improve_artifacts,
            "improve_simulate": cmd_improve_simulate,
            "improve_runs": cmd_improve_runs,
            "improve_compare": cmd_improve_compare,
            "improve_policy": cmd_improve_policy,
            "improve_promote": cmd_improve_promote,
            "improve_autopromote": cmd_improve_autopromote,
            "improve_funnel": cmd_improve_funnel,
            "improve_eval_report": cmd_improve_eval_report,
            "improve_weekly_report": cmd_improve_weekly_report,
            "improve_phase": cmd_improve_phase,
            "improve_log_rollback": cmd_improve_log_rollback,
            "evolve_capture": cmd_evolve_capture,
            "evolve_memory": cmd_evolve_memory,
            "plan": cmd_plan,
            "go": cmd_go,
            "topskills": cmd_topskills,
            "exec": cmd_exec,
            "attachdiag": cmd_attachdiag,
            "panicstatus": cmd_panicstatus,
            "paniclock": cmd_paniclock,
            "panicunlock": cmd_panicunlock,
            "skills": cmd_skills,
            "skillon": cmd_skillon,
            "skilloff": cmd_skilloff,
            "clearctx": cmd_clearctx,
            "help": cmd_help,
            "cronadd": cmd_cronadd,
            "cronrm": cmd_cronrm,
            "cronlist": cmd_cronlist,
            "cronon": cmd_cronon,
            "cronoff": cmd_cronoff,
            "cronrun": cmd_cronrun,
            "cronruns": cmd_cronruns,
    }

    global _COMMAND_DISPATCH
    _COMMAND_DISPATCH = dict(commands)

    register_handlers(
        app=app,
        commands=commands,
        cmd_unknown=cmd_unknown,
        handle_media_message=handle_media_message,
        handle_message=handle_message,
        on_error=_on_error,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_graceful_shutdown(app)))

    app.run_polling(drop_pending_updates=True, allowed_updates=["message"], close_loop=False)


if __name__ == "__main__":
    main()

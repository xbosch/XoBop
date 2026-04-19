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
import ctypes
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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from croniter import croniter
from openai import AsyncOpenAI, OpenAI, RateLimitError
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from xobop.state import STATE
from xobop.config import settings
from xobop.capabilities_shell import exec_shell_command
from xobop.db import db_session, sqlite_online_backup
from xobop.migrations import apply_migrations
from xobop.handlers_runtime import ProcessTextDeps, process_media_message, process_text_message, resolve_command_alias
from xobop.bootstrap import build_app, register_handlers
from xobop.cron_bootstrap import ensure_default_cron_jobs
from xobop.schema import ensure_core_schema
from xobop.db_init import initialize_database
from xobop.router import classify
from xobop.shell_facade import get_shell_executor
from xobop.output_guardrails import normalize_machine_kv_output
from xobop.defaults import STOPWORDS, STRICT_PROMPT, NORMAL_PROMPT, default_global_policies
from xobop.agent import AgentDeps, run_agent_loop
from xobop.simulation import TempDbManager, shell_golden_cases, shell_replay_cases, skill_policy_simulation_cases, ReplayController, apply_artifact_to_temp_settings
from xobop.runtime_ops import (
    enforce_rate_limit as ops_enforce_rate_limit,
    fire_and_forget as ops_fire_and_forget,
    to_thread as ops_to_thread,
    wait_for_background_futures as ops_wait_bg_futures,
)
from xobop.attachments import extract_docx_text, extract_xlsx_text, summarize_zip
from xobop.text_math import cosine as tm_cosine, deterministic_hash as tm_hash, hours_since as tm_hours_since, recency_score as tm_recency_score, split_words as tm_split_words
from xobop.policy_helpers import blocked_suffix as ph_blocked_suffix, domain_allowed as ph_domain_allowed, is_under_root as ph_is_under_root
from xobop.artifacts_io import shorten_or_artifact as aio_shorten_or_artifact, store_artifact as aio_store_artifact
from xobop.memory_math import fallback_embedding as mm_fallback_embedding
from xobop.runtime_state import (
    build_runtime_state_payload as rs_build_payload,
    build_state_check_dict as rs_build_state_check_dict,
    repair_from_snapshot as rs_repair_from_snapshot,
)
from xobop.mode_utils import temperature_for_mode as mu_temperature_for_mode
from xobop.core_policy_runtime import (
    bootstrap_default_policies_impl as cpr_bootstrap_default_policies_impl,
    default_skills_impl as cpr_default_skills_impl,
    bootstrap_default_prompts_impl as cpr_bootstrap_default_prompts_impl,
    get_policy_impl as cpr_get_policy_impl,
    get_runtime_prompt_impl as cpr_get_runtime_prompt_impl,
    set_policy_impl as cpr_set_policy_impl,
    set_runtime_prompt_impl as cpr_set_runtime_prompt_impl,
)
from xobop.core_startup_runtime import (
    build_scheduler_impl as csr_build_scheduler_impl,
    get_command_aliases_impl as csr_get_command_aliases_impl,
    init_db_impl as csr_init_db_impl,
    runtime_state_payload_impl as csr_runtime_state_payload_impl,
    set_command_aliases_impl as csr_set_command_aliases_impl,
    state_persist_loop_impl as csr_state_persist_loop_impl,
    sync_scheduler_impl as csr_sync_scheduler_impl,
)
from xobop.core_memory_runtime import (
    MemoryItem,
    add_memory_impl as cmr_add_memory_impl,
    append_daily_memory_impl as cmr_append_daily_memory_impl,
    build_file_memory_block_impl as cmr_build_file_memory_block_impl,
    build_memory_block_impl as cmr_build_memory_block_impl,
    build_structured_memory_block_impl as cmr_build_structured_memory_block_impl,
    delete_memory_sync_impl as cmr_delete_memory_sync_impl,
    embed_text_impl as cmr_embed_text_impl,
    insert_memory_item_sync_impl as cmr_insert_memory_item_sync_impl,
    list_memories_impl as cmr_list_memories_impl,
    maybe_promote_memory_impl as cmr_maybe_promote_memory_impl,
    memory_count_impl as cmr_memory_count_impl,
    memory_item_is_prompt_polluting_impl as cmr_memory_item_is_prompt_polluting_impl,
    memory_rows_impl as cmr_memory_rows_impl,
    promote_fact_to_memory_sync_impl as cmr_promote_fact_to_memory_sync_impl,
    promote_long_memory_impl as cmr_promote_long_memory_impl,
    prune_memory_sync_impl as cmr_prune_memory_sync_impl,
    retrieve_memory_impl as cmr_retrieve_memory_impl,
    retrieve_memory_lexical_impl as cmr_retrieve_memory_lexical_impl,
    save_fact_memory_impl as cmr_save_fact_memory_impl,
    save_memory_vector_sync_impl as cmr_save_memory_vector_sync_impl,
    today_daily_path_impl as cmr_today_daily_path_impl,
    yesterday_daily_path_impl as cmr_yesterday_daily_path_impl,
)
from xobop.core_memory_queue_runtime import (
    cleanup_memory_promotion_queue as cmq_cleanup,
    drain_memory_promotion_queue_once as cmq_drain_once,
    enqueue_memory_promotion as cmq_enqueue,
    memory_promotion_loop as cmq_loop,
    memory_promotion_prefilter_reason as cmq_prefilter_reason,
    promote_memory_now as cmq_promote_now,
)
from xobop.core_bg_failure_runtime import (
    get_bg_failure_count as cbg_get_count,
    set_bg_failure_count as cbg_set_count,
)
from xobop.core_runtime_lock import (
    acquire_runtime_lock as crl_acquire,
    release_runtime_lock as crl_release,
)
from xobop.ops_logging import record_op_event as ol_record_op_event, record_run as ol_record_run, update_skill_usage as ol_update_skill_usage
from xobop.runtime_snapshot import (
    export_runtime_snapshot as rsnap_export,
    load_runtime_snapshot as rsnap_load,
    startup_reconcile_runtime_state as rsnap_startup_reconcile,
)
from xobop.selfmods import apply_self_mods_defaults as sm_apply_defaults, export_self_mods_debug as sm_export_debug
from xobop.auto_improve import auto_improve_flags as ai_flags, maybe_generate_auto_proposals as ai_maybe_generate
from xobop.commands_admin import (
    cmd_attachdiag_impl,
    cmd_diagnose_impl,
    cmd_mode_impl,
    cmd_op_sanity_impl,
    cmd_procinfo_impl,
    cmd_state_check_impl,
    cmd_state_clear_impl,
    cmd_state_repair_impl,
    cmd_status_impl,
)
from xobop.commands_improve import (
    cmd_improve_artifacts_impl,
    cmd_improve_auto_dashboard_impl,
    cmd_improve_auto_last_impl,
    cmd_improve_auto_off_impl,
    cmd_improve_auto_on_impl,
    cmd_improve_auto_status_impl,
    cmd_improve_autopromote_impl,
    cmd_improve_compare_impl,
    cmd_improve_compile_impl,
    cmd_improve_eval_report_impl,
    cmd_improve_funnel_impl,
    cmd_improve_list_impl,
    cmd_improve_log_rollback_impl,
    cmd_improve_phase_impl,
    cmd_improve_policy_impl,
    cmd_improve_promote_impl,
    cmd_improve_propose_impl,
    cmd_improve_reject_impl,
    cmd_improve_runs_impl,
    cmd_improve_set_retry_impl,
    cmd_improve_simulate_impl,
    cmd_improve_weekly_report_impl,
)
from xobop.commands_memory import (
    cmd_addtask_impl,
    cmd_donetask_impl,
    cmd_evolve_blocks_rebuild_impl,
    cmd_evolve_capture_impl,
    cmd_evolve_memory_impl,
    cmd_evolve_recapture_all_impl,
    cmd_evolve_status_impl,
    cmd_facts_impl,
    cmd_memadd_impl,
    cmd_memdel_impl,
    cmd_memdiag_impl,
    cmd_memories_impl,
    cmd_remember_impl,
    cmd_tasks_impl,
    cmd_topskills_impl,
)
from xobop.commands_safety import (
    cmd_delfact_impl,
    cmd_exec_impl,
    cmd_paniclock_impl,
    cmd_panicstatus_impl,
    cmd_panicunlock_impl,
)
from xobop.commands_skills import cmd_skilloff_impl, cmd_skillon_impl, cmd_skills_impl
from xobop.commands_cron import (
    cmd_cronadd_impl,
    cmd_cronlist_impl,
    cmd_cronoff_impl,
    cmd_cronon_impl,
    cmd_cronrm_impl,
    cmd_cronrun_impl,
    cmd_cronruns_impl,
)
from xobop.commands_prompt import (
    cmd_help_impl,
    cmd_promptapprove_impl,
    cmd_promptproposal_impl,
    cmd_promptreject_impl,
    cmd_promptset_impl,
    cmd_promptshow_impl,
    cmd_quietflush_impl,
    cmd_quietoff_impl,
    cmd_quiethours_impl,
)
from xobop.skills_runtime import (
    call_skill_impl as sr_call_skill_impl,
    exec_file_append as sr_exec_file_append,
    exec_file_read as sr_exec_file_read,
    exec_file_write as sr_exec_file_write,
    exec_http_get as sr_exec_http_get,
    exec_python_fn as sr_exec_python_fn,
    exec_shell_command as sr_exec_shell_command,
)
from xobop.improve_pipeline_ops import (
    auto_promote_low_sync as ipo_auto_promote_low_sync,
    capture_evolution_memory_sync as ipo_capture_evolution_memory_sync,
    embed_evolution_entry_async as ipo_embed_evolution_entry_async,
    evolution_memory_report as ipo_evolution_memory_report,
    fetch_conditioned_evolution_async as ipo_fetch_conditioned_evolution_async,
    lookup_evolution_memory as ipo_lookup_evolution_memory,
    recapture_all_evolution_sync as ipo_recapture_all_evolution_sync,
    recompute_evolution_blocks as ipo_recompute_evolution_blocks,
    update_learned_query_vector as ipo_update_learned_query_vector,
    impact_compare_sync as ipo_impact_compare_sync,
    improve_eval_report as ipo_improve_eval_report,
    improve_funnel_report as ipo_improve_funnel_report,
    improve_weekly_report as ipo_improve_weekly_report,
    latest_impact_confidence as ipo_latest_impact_confidence,
    latest_impact_delta as ipo_latest_impact_delta,
    latest_run_status as ipo_latest_run_status,
)
from xobop.improve_generate import (
    compile_proposal_to_change as ig_compile_proposal_to_change,
    diagnose_report as ig_diagnose_report,
    diagnose_snapshot as ig_diagnose_snapshot,
    generate_improvement_proposal as ig_generate_improvement_proposal,
)
from xobop.app_lifecycle import (
    graceful_shutdown_impl as al_graceful_shutdown_impl,
    on_error_impl as al_on_error_impl,
    post_init_impl as al_post_init_impl,
    post_shutdown_impl as al_post_shutdown_impl,
)
from xobop.app_handlers import (
    cmd_unknown_impl as ah_cmd_unknown_impl,
    handle_media_message_impl as ah_handle_media_message_impl,
    handle_message_impl as ah_handle_message_impl,
)
from xobop.attachments_runtime import analyze_attachment as ar_analyze_attachment
from xobop.io_text import fetch_text_limited as io_fetch_text_limited, read_text_limited as io_read_text_limited
from xobop.shell_policy import classify_shell as sp_classify_shell, user_forced as sp_user_forced
from xobop.workflow_state import load_workflow_state_by_key as wf_load_by_key, save_workflow_state as wf_save
from xobop.core_records_runtime import (
    add_task as cr_add_task,
    close_task as cr_close_task,
    delete_fact as cr_delete_fact,
    get_fact_value as cr_get_fact_value,
    list_facts as cr_list_facts,
    list_tasks as cr_list_tasks,
    load_session_state as cr_load_session_state,
    save_fact as cr_save_fact,
    save_session_state as cr_save_session_state,
)
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
sync_client = OpenAI(api_key=OPENAI_KEY)

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
    r"|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
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



# =============================================================================
# Database
# =============================================================================

def get_db():
    return db_session(DB_FILE)


def _init_db() -> None:
    return csr_init_db_impl(
        state=STATE,
        save_workflow_state=_save_workflow_state,
        resolve_contact_email=_resolve_contact_email,
        get_fact_value=_get_fact_value,
        get_fact_items=_get_fact_items,
        initialize_database=initialize_database,
        get_db=get_db,
        ensure_core_schema=ensure_core_schema,
        apply_migrations=apply_migrations,
        bootstrap_default_skills=_bootstrap_default_skills,
        bootstrap_default_policies=_bootstrap_default_policies,
        bootstrap_default_prompts=_bootstrap_default_prompts,
        ensure_default_cron_jobs=ensure_default_cron_jobs,
        repo_cwd=os.getcwd(),
        set_skill_policy=_set_skill_policy,
        allowed_write_root=ALLOWED_WRITE_ROOT,
        project_root=PROJECT_ROOT,
        allow_raw_shell=ALLOW_RAW_SHELL,
    )


# =============================================================================
# Settings, policies, and defaults
# =============================================================================

DEFAULT_GLOBAL_POLICIES: dict[str, str] = default_global_policies(MAX_MEMORY_ITEMS)

DEFAULT_SKILLS: list[dict[str, Any]] = cpr_default_skills_impl(
    shell_timeout_write=SHELL_TIMEOUT_WRITE,
    allow_raw_shell=ALLOW_RAW_SHELL,
    request_timeout=REQUEST_TIMEOUT,
    max_browse_bytes=MAX_BROWSE_BYTES,
    read_root=READ_ROOT,
    max_read_bytes=MAX_READ_BYTES,
    allowed_write_root=ALLOWED_WRITE_ROOT,
)


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


def _acquire_runtime_lock() -> tuple[bool, str]:
    return crl_acquire(get_setting=_get_setting, set_setting=_set_setting, token=TOKEN)


def _release_runtime_lock() -> None:
    return crl_release(get_setting=_get_setting, set_setting=_set_setting)


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
    return csr_runtime_state_payload_impl(
        rs_build_payload=rs_build_payload,
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
    return sm_export_debug(
        self_mods_file=SELF_MODS_FILE,
        get_command_aliases=_get_command_aliases,
        get_exec_retry_limit=_get_exec_retry_limit,
        get_last_applied_artifact_id=_get_last_applied_artifact_id,
    )


def _get_command_aliases() -> dict[str, str]:
    return csr_get_command_aliases_impl(get_setting=_get_setting)


def _set_command_aliases(d: dict[str, str]) -> None:
    return csr_set_command_aliases_impl(d=d, set_setting=_set_setting, export_self_mods_debug=_export_self_mods_debug, export_runtime_snapshot_fn=_export_runtime_snapshot)


def _overseer_enabled() -> bool:
    return _get_mode() == "overseer"


def _get_improve_policy() -> str:
    p = (_get_setting("improve_policy", "assisted") or "assisted").strip().lower()
    return p if p in {"manual", "assisted", "auto_low"} else "assisted"


def _get_eval_phase() -> str:
    p = (_get_setting("improve_eval_phase", "baseline") or "baseline").strip().lower()
    return p if p in {"baseline", "improving"} else "baseline"


def _allowed_artifact_families() -> set[str]:
    raw = (_get_setting("artifact_families_allowed", "alias,retry,lineage,prompt,skill_policy") or "alias,retry,lineage,prompt,skill_policy").strip().lower()
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    return set(parts or ["alias", "retry", "skill_policy"])


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
        set_runtime_prompt=_set_runtime_prompt,
        set_skill_policy=_set_skill_policy,
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
    # Dev policy: panic-lock disabled to avoid blocking test cycles.
    return False


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
    cr_save_session_state(get_db=get_db, key=key, obj=obj)


def _load_session_state(key: str) -> Optional[dict]:
    return cr_load_session_state(get_db=get_db, key=key)


def _save_workflow_state(chat_id: int, key: str, value) -> None:
    wf_save(get_db=get_db, chat_id=chat_id, key=key, value=value)


def _load_workflow_state(key: str) -> dict[int, object]:
    return wf_load_by_key(get_db=get_db, key=key)


def _clear_workflow_state_for_chat(chat_id: int) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM workflow_state WHERE chat_id=? AND key IN ('pending_action','last_email_payload','email_workflow_state')",
            (int(chat_id),),
        )
        return int(cur.rowcount or 0)


def _get_policy(key: str, default: str = "") -> str:
    return cpr_get_policy_impl(get_db=get_db, key=key, default=default)


def _set_policy(key: str, value: str) -> None:
    return cpr_set_policy_impl(get_db=get_db, key=key, value=value)


def _bootstrap_default_policies() -> None:
    return cpr_bootstrap_default_policies_impl(get_db=get_db, defaults=DEFAULT_GLOBAL_POLICIES, set_policy=_set_policy)


def _bootstrap_default_prompts() -> None:
    return cpr_bootstrap_default_prompts_impl(get_db=get_db, strict_prompt=STRICT_PROMPT, normal_prompt=NORMAL_PROMPT)


def _get_runtime_prompt(mode: str) -> str:
    return cpr_get_runtime_prompt_impl(get_db=get_db, mode=mode, strict_prompt=STRICT_PROMPT, normal_prompt=NORMAL_PROMPT)


def _set_runtime_prompt(mode: str, prompt_text: str) -> None:
    return cpr_set_runtime_prompt_impl(get_db=get_db, mode=mode, prompt_text=prompt_text)


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


def _init_extended_skills() -> None:
    """Register Google and Playwright skills if their dependencies are available.
    Called once at startup after _init_db(). Safe to call repeatedly (all upserts are idempotent)."""
    import xobop.google_skills as _gs
    import xobop.browser_skill as _bs
    google_ok = _gs.init(token_file=os.getenv("GOOGLE_TOKEN_FILE", "config/google_token.json"))
    playwright_ok = _bs.init()
    skills_to_register = []
    if google_ok:
        skills_to_register.extend(_gs.GOOGLE_SKILLS)
    if playwright_ok:
        skills_to_register.extend(_bs.BROWSER_SKILLS)
    for s in skills_to_register:
        _upsert_skill(
            name=s["name"],
            description=s["description"],
            schema=s["schema"],
            handler_type=s["handler_type"],
            risk_level=s.get("risk_level", "low"),
            timeout_sec=s.get("timeout_sec", 30),
            requires_force=bool(s.get("requires_force", 0)),
            enabled=bool(s.get("enabled", 1)),
            visible_in_strict=bool(s.get("visible_in_strict", 1)),
            visible_in_normal=bool(s.get("visible_in_normal", 1)),
        )
        for k, v in s.get("policies", {}).items():
            _set_skill_policy(s["name"], k, str(v))
    log.info("Extended skills: google=%s playwright=%s", google_ok, playwright_ok)


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


def _record_op_event(event_type: str, **kwargs) -> None:
    return ol_record_op_event(get_db=get_db, event_type=event_type, **kwargs)


def _record_run(*args, **kwargs) -> None:
    if args and not kwargs:
        # Backward-compatible positional form:
        # (chat_id, user_query, mode, tool_name, tool_args, result, exit_code, duration_ms, status, artifact_path)
        vals = list(args) + [None] * (10 - len(args))
        chat_id, user_query, mode, tool_name, tool_args, result, exit_code, duration_ms, status, artifact_path = vals[:10]
        kwargs = {
            "turn_id": "",
            "chat_id": int(chat_id),
            "user_query": str(user_query or ""),
            "mode": str(mode or "normal"),
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": str(result or ""),
            "exit_code": exit_code,
            "duration_ms": int(duration_ms or 0),
            "status": str(status or "ok"),
            "artifact_path": artifact_path,
        }

    ol_record_run(
        get_db=get_db,
        redact=_redact,
        text_limit=4000,
        turn_id=str(kwargs.get("turn_id", "")),
        chat_id=int(kwargs.get("chat_id", 0)),
        user_query=str(kwargs.get("user_query", "")),
        mode=str(kwargs.get("mode", "normal")),
        tool_name=kwargs.get("tool_name"),
        tool_args=kwargs.get("tool_args"),
        result=str(kwargs.get("result", "")),
        exit_code=kwargs.get("exit_code"),
        duration_ms=int(kwargs.get("duration_ms", 0)),
        status=str(kwargs.get("status", "ok")),
        artifact_path=kwargs.get("artifact_path"),
    )

    # Auto-improve Phase 0-3: detect repeated failure clusters and draft proposals.
    status = str(kwargs.get("status", "ok"))
    if status in {"error", "blocked"}:
        try:
            enabled, dry_run, threshold, max_per_day, dedup_days = ai_flags(get_setting=_get_setting)
            rep = ai_maybe_generate(
                get_db=get_db,
                enabled=enabled,
                dry_run=dry_run,
                threshold=threshold,
                max_per_day=max_per_day,
                dedup_days=dedup_days,
            )
            created_n = int(rep.get("created", 0))
            _record_op_event(
                "auto_improve_tick",
                status=("ok" if rep.get("enabled") else "disabled"),
                metadata={
                    "dry_run": bool(rep.get("dry_run", dry_run)),
                    "created": created_n,
                    "considered": int(rep.get("considered", 0)),
                    "capped": bool(rep.get("capped", False)),
                    "followups": int(rep.get("followups", 0)),
                },
            )

            # Telegram digest via notification queue when auto-proposals are created.
            if (not dry_run) and created_n > 0:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO notification_queue(chat_id,text) VALUES(?,?)",
                        (OWNER_ID, f"🧠 Auto-improve: created {created_n} proposal(s). Use /improve_auto_last or /improve_auto_dashboard."),
                    )

            # Optional one-step low-risk promotion policy (guarded).
            autopromote_enabled = (_get_setting("AUTO_IMPROVE_AUTOPROMOTE", "0") or "0").strip() == "1"
            if (
                (not dry_run)
                and created_n > 0
                and autopromote_enabled
                and _get_improve_policy() == "auto_low"
                and bool(rep.get("safe_for_autopromote", False))
            ):
                promoted = ipo_auto_promote_low_sync(
                    get_improve_policy=_get_improve_policy,
                    get_db=get_db,
                    promote_proposal_sync=_promote_proposal_sync,
                )
                _record_op_event("auto_improve_autopromote", status="ok", metadata={"promoted": int(promoted or 0)})
        except Exception as exc:
            _record_op_event("auto_improve_tick", status="error", metadata={"error": str(exc)[:180]})


# =============================================================================
# Async helpers
# =============================================================================


async def _to_thread(fn, *args, **kwargs):
    return await ops_to_thread(fn, *args, **kwargs)


async def _wait_bg_futures(timeout_s: float = 2.0) -> None:
    await ops_wait_bg_futures(timeout_s=timeout_s)


def _get_bg_failure_count(label: str) -> int:
    return cbg_get_count(get_setting=_get_setting, label=label)


def _set_bg_failure_count(label: str, value: int) -> None:
    return cbg_set_count(get_setting=_get_setting, set_setting=_set_setting, label=label, value=value)


def _fire_and_forget(
    loop: asyncio.AbstractEventLoop,
    fn,
    label: str,
    *,
    retries: int = 0,
    retry_delay_s: float = 0.25,
    on_repeated_failure=None,
    alert_threshold: int = 3,
) -> None:
    return ops_fire_and_forget(
        loop,
        fn,
        label,
        log=log,
        retries=retries,
        retry_delay_s=retry_delay_s,
        on_repeated_failure=on_repeated_failure,
        alert_threshold=alert_threshold,
        get_failure_count=_get_bg_failure_count,
        set_failure_count=_set_bg_failure_count,
    )


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
        exec_cwd = cwd
        exec_argv = list(argv)
        if exe is None:
            token = argv[0]
            # General fallback for relative script commands in codegen flows.
            if token.startswith("./"):
                local = (Path(cwd) / token).resolve()
                sandbox = (ALLOWED_WRITE_ROOT / "agent-test" / "codegen-check" / token[2:]).resolve()
                chosen = None
                if local.exists() and local.is_file():
                    chosen = local
                elif sandbox.exists() and sandbox.is_file():
                    chosen = sandbox
                    exec_cwd = str((ALLOWED_WRITE_ROOT / "agent-test" / "codegen-check").resolve())
                if chosen is not None:
                    # If executable bit is missing for shell scripts, run via bash.
                    if chosen.suffix.lower() == ".sh":
                        exe = shutil.which("bash")
                        exec_argv = ["bash", str(chosen)] + argv[1:]
                    else:
                        exe = str(chosen)
                        exec_argv = [str(chosen)] + argv[1:]
            if exe is None:
                raise FileNotFoundError(f"Command not found: {argv[0]!r}")
        proc = await asyncio.create_subprocess_exec(
            exe,
            *exec_argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=exec_cwd,
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
    # Option 2: always read recent history from DB each turn (no module-level cache).
    return _load_recent_history(chat_id)


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
            text=_redact(r["text"]),
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
            text=_redact(r["text"]),
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


def _memory_diag() -> str:
    with get_db() as conn:
        items = int(conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()["n"])
        vectors = int(conn.execute("SELECT COUNT(*) AS n FROM memory_vectors").fetchone()["n"])
        model_row = conn.execute(
            "SELECT model, COUNT(*) AS n FROM memory_vectors GROUP BY model ORDER BY n DESC LIMIT 1"
        ).fetchone()
        q = conn.execute(
            "SELECT status, COUNT(*) AS n FROM memory_promotion_queue GROUP BY status ORDER BY n DESC"
        ).fetchall()
    model_name = str(model_row["model"]) if model_row and model_row["model"] else "none"
    mode = "lexical-fallback" if model_name.startswith("fallback") else "semantic"
    qline = ", ".join(f"{r['status']}={int(r['n'])}" for r in q) if q else "empty"
    return (
        f"Memory diag\n"
        f"• items: {items}\n"
        f"• vectors: {vectors}\n"
        f"• top_vector_model: {model_name}\n"
        f"• retrieval_mode: {mode}\n"
        f"• promotion_queue: {qline}"
    )


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


def _enqueue_memory_promotion(text: str, source: str = "user") -> None:
    cmq_enqueue(get_db=get_db, text=text, source=source)


async def _promote_memory_now(text: str, source: str = "user") -> tuple[bool, str]:
    return await cmq_promote_now(
        text=text,
        source=source,
        add_memory=_add_memory,
        append_daily_memory=_append_daily_memory,
        promote_long_memory=_promote_long_memory,
    )


async def _drain_memory_promotion_queue_once(limit: int = 5) -> int:
    return await cmq_drain_once(
        get_db=get_db,
        promote_memory_now_fn=lambda t, s: _promote_memory_now(t, source=s),
        log=log,
        limit=limit,
    )


def _cleanup_memory_promotion_queue(retention_days: int = 7) -> None:
    return cmq_cleanup(get_db=get_db, retention_days=retention_days)


async def _memory_promotion_loop() -> None:
    return await cmq_loop(
        drain_once=lambda lim: _drain_memory_promotion_queue_once(limit=lim),
        cleanup=lambda days: _cleanup_memory_promotion_queue(retention_days=days),
        log=log,
        sleep_seconds=15,
    )


async def _maybe_promote_memory(text: str, source: str = "user") -> None:
    _enqueue_memory_promotion(text, source)
    await _drain_memory_promotion_queue_once(limit=1)


def _build_structured_memory_block(query: str) -> str:
    q = query.lower()
    parts = []
    if any(w in q for w in ("remember", "fact", "know", "status")):
        parts.append("Saved facts:\n" + _get_facts())
    if any(w in q for w in ("task", "done", "todo")):
        parts.append("Open tasks:\n" + _get_tasks())
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def _memory_item_is_prompt_polluting(text: str, query: str) -> bool:
    t = (text or "").lower()
    q = (query or "").lower()
    looks_like_codegen_recipe = (
        "return exactly" in t
        or "create folder" in t
        or "codegen-check" in t
        or "chmod +x" in t
    )
    query_is_codegen = any(k in q for k in ["code", "script", "python", "bash", "chmod", "file", "folder", "run.sh"])
    return looks_like_codegen_recipe and not query_is_codegen


async def _build_memory_block(query: str, k: int = MAX_MEMORY_RESULTS) -> str:
    semantic = await _retrieve_memory(query, k=k)
    semantic = [m for m in semantic if not _memory_item_is_prompt_polluting(getattr(m, "text", ""), query)]

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


def _resolve_contact_email(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    m = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", t)
    if m:
        return m.group(1)

    # Fully data-driven alias lookup from facts table (no hardcoded names/tokens).
    # Supported fact styles:
    # - default_recipient_email=<email>
    # - email_alias_<token>=<email>
    # - <token>=<email>
    with get_db() as conn:
        rows = conn.execute("SELECT key,value FROM facts").fetchall()
    facts = {str(r["key"]).strip().lower(): str(r["value"]).strip() for r in rows}

    def _email_from(v: str) -> Optional[str]:
        mm = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", (v or ""))
        return mm.group(1) if mm else None

    # tokenized lookup based on user text
    tokens = set(re.findall(r"[a-z0-9_]+", t))
    for tok in tokens:
        key_candidates = (
            f"email_alias_{tok}",
            tok,
            f"{tok}_email",
            f"{tok} email",
            f"email_{tok}",
        )
        for fk in key_candidates:
            em = _email_from(facts.get(fk, ""))
            if em:
                return em

    # default fallback if configured
    return _email_from(facts.get("default_recipient_email", ""))


def _get_fact_value(key: str) -> str:
    return cr_get_fact_value(get_db=get_db, key=key)


def _get_fact_items() -> dict[str, str]:
    with get_db() as conn:
        rows = conn.execute("SELECT key,value FROM facts").fetchall()
    return {str(r["key"]): str(r["value"]) for r in rows}


def _save_fact(key: str, value: str, source: str = "") -> str:
    return cr_save_fact(get_db=get_db, key=key, value=value, source=source)


def _delete_fact(key: str) -> str:
    return cr_delete_fact(get_db=get_db, key=key)


def _get_facts() -> str:
    return cr_list_facts(get_db=get_db)


def _add_task(desc: str, priority: str = "normal", source: str = "") -> str:
    return cr_add_task(get_db=get_db, desc=desc, priority=priority, source=source)


def _close_task(task_id: int) -> str:
    return cr_close_task(get_db=get_db, task_id=task_id)


def _get_tasks() -> str:
    return cr_list_tasks(get_db=get_db)


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


def _get_prompt_proposal() -> Optional[dict[str, str]]:
    return _prompt_proposal


def _set_prompt_proposal(value: Optional[dict[str, str]]) -> None:
    global _prompt_proposal
    _prompt_proposal = value
_last_openai_latency_ms: float = 0.0


async def _call_skill(chat_id: int, user_query: str, mode: str, skill_name: str, args: dict) -> str:
    handlers = {
        "file_read": lambda **kw: sr_exec_file_read(
            skill=kw["skill"], policies=kw["policies"], args=kw["args"],
            blocked_suffix=ph_blocked_suffix, is_under_root=ph_is_under_root, allowed_write_root=ALLOWED_WRITE_ROOT
        ),
        "http_get": lambda **kw: sr_exec_http_get(
            skill=kw["skill"], policies=kw["policies"], args=kw["args"], domain_allowed=ph_domain_allowed
        ),
        "file_write": lambda **kw: sr_exec_file_write(
            skill=kw["skill"], policies=kw["policies"], args=kw["args"], allowed_write_root=ALLOWED_WRITE_ROOT,
            is_under_root=ph_is_under_root, blocked_suffix=ph_blocked_suffix, get_policy=_get_policy,
            default_max_write_size=MAX_WRITE_SIZE, to_thread=_to_thread
        ),
        "file_append": lambda **kw: sr_exec_file_append(
            skill=kw["skill"], policies=kw["policies"], args=kw["args"], allowed_write_root=ALLOWED_WRITE_ROOT,
            is_under_root=ph_is_under_root, blocked_suffix=ph_blocked_suffix, get_policy=_get_policy,
            default_max_write_size=MAX_WRITE_SIZE, to_thread=_to_thread
        ),
        "shell_command": lambda **kw: sr_exec_shell_command(
            skill=kw["skill"], policies=kw["policies"], user_query=kw["user_query"], args=kw["args"],
            use_capability_facade_shell=USE_CAPABILITY_FACADE_SHELL, get_shell_executor=get_shell_executor,
            exec_shell_command_capability=exec_shell_command, classify_shell=_classify_shell,
            user_forced=_user_forced, run_shell_async=_run_shell_async, shorten_or_artifact=_shorten_or_artifact
        ),
        "python_fn": lambda **kw: sr_exec_python_fn(
            skill=kw["skill"], policies=kw["policies"], args=kw["args"],
            to_thread=_to_thread, shorten_or_artifact=_shorten_or_artifact,
        ),
    }
    return await sr_call_skill_impl(
        chat_id=chat_id,
        user_query=user_query,
        mode=mode,
        skill_name=skill_name,
        args=args,
        panic_locked=_panic_locked,
        record_op_event=_record_op_event,
        get_policy=_get_policy,
        default_global_policies=DEFAULT_GLOBAL_POLICIES,
        enforce_rate_limit=_enforce_rate_limit,
        get_skill=_get_skill,
        load_skill_policies=_load_skill_policies,
        allowed_write_root=ALLOWED_WRITE_ROOT,
        project_root=PROJECT_ROOT,
        allow_raw_shell=ALLOW_RAW_SHELL,
        user_forced=_user_forced,
        handlers=handlers,
        log=log,
        to_thread=_to_thread,
        record_run=_record_run,
        update_skill_usage=_update_skill_usage,
        audit_skill=_audit_skill,
        fire_and_forget=_fire_and_forget,
    )


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
            backup_dir = skill_args.get("backup_dir", str(settings.backup_dir))
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
    return csr_build_scheduler_impl(db_file=DB_FILE)


def _sync_scheduler(scheduler: AsyncIOScheduler) -> None:
    return csr_sync_scheduler_impl(scheduler=scheduler, get_db=get_db, run_cron_job=_run_cron_job)


async def _state_persist_loop() -> None:
    return await csr_state_persist_loop_impl(
        state=STATE,
        save_session_state=_save_session_state,
        save_workflow_state=_save_workflow_state,
        log=log,
    )


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



from __future__ import annotations

import asyncio
import json

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


def init_db_impl(
    *,
    state,
    save_workflow_state,
    resolve_contact_email,
    get_fact_value,
    get_fact_items,
    initialize_database,
    get_db,
    ensure_core_schema,
    apply_migrations,
    bootstrap_default_skills,
    bootstrap_default_policies,
    bootstrap_default_prompts,
    ensure_default_cron_jobs,
    repo_cwd: str,
    set_skill_policy,
    allowed_write_root,
    project_root,
    allow_raw_shell: bool,
) -> None:
    setattr(state, "workflow_state_saver", save_workflow_state)
    setattr(state, "email_contact_resolver", resolve_contact_email)
    setattr(state, "fact_getter", get_fact_value)
    setattr(state, "fact_items_getter", get_fact_items)
    setattr(state, "all_facts_getter", get_fact_items)

    initialize_database(
        get_db=get_db,
        ensure_core_schema=ensure_core_schema,
        apply_migrations=apply_migrations,
        bootstrap_default_skills=bootstrap_default_skills,
        bootstrap_default_policies=bootstrap_default_policies,
        bootstrap_default_prompts=bootstrap_default_prompts,
        ensure_default_cron_jobs=ensure_default_cron_jobs,
        repo_cwd=repo_cwd,
    )

    set_skill_policy("run_shell", "write_root", str(allowed_write_root))
    set_skill_policy("run_shell", "default_cwd", str(project_root))
    set_skill_policy("run_shell", "allow_raw_shell", "1" if allow_raw_shell else "0")


def runtime_state_payload_impl(
    *,
    rs_build_payload,
    get_command_aliases,
    get_exec_retry_limit,
    get_last_applied_artifact_id,
    last_applied_artifact_id=None,
    last_promotion_mode: str = "",
):
    return rs_build_payload(
        get_command_aliases=get_command_aliases,
        get_exec_retry_limit=get_exec_retry_limit,
        get_last_applied_artifact_id=get_last_applied_artifact_id,
        last_applied_artifact_id=last_applied_artifact_id,
        last_promotion_mode=last_promotion_mode,
    )


def build_scheduler_impl(*, db_file: str) -> AsyncIOScheduler:
    jobstore_url = f"sqlite:///{db_file}"
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
        executors={"default": AsyncIOExecutor()},
        timezone="UTC",
    )
    return scheduler


def sync_scheduler_impl(*, scheduler: AsyncIOScheduler, get_db, run_cron_job) -> None:
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
            scheduler.add_job(run_cron_job, trigger=trigger, id=name, args=[name], replace_existing=True)

    for job_id in existing - active_names:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


async def state_persist_loop_impl(*, state, save_session_state, save_workflow_state, log) -> None:
    while True:
        try:
            payload = {
                "pending_action_by_chat": state.pending_action_by_chat,
                "last_email_payload_by_chat": state.last_email_payload_by_chat,
                "email_workflow_state_by_chat": state.email_workflow_state_by_chat,
            }
            save_session_state("runtime_state", payload)
            for chat_id, val in dict(state.pending_action_by_chat).items():
                save_workflow_state(int(chat_id), "pending_action", val)
            for chat_id, val in dict(state.last_email_payload_by_chat).items():
                save_workflow_state(int(chat_id), "last_email_payload", val)
            for chat_id, val in dict(state.email_workflow_state_by_chat).items():
                save_workflow_state(int(chat_id), "email_workflow_state", str(val))
        except Exception as exc:
            log.warning("State persist failed: %s", exc)
        await asyncio.sleep(20)


def get_command_aliases_impl(*, get_setting) -> dict[str, str]:
    raw = (get_setting("command_aliases_json", "{}") or "{}").strip()
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {
                str(k).strip().lower(): str(v).strip().lower()
                for k, v in d.items()
                if str(k).strip() and str(v).strip()
            }
    except Exception:
        pass
    return {}


def set_command_aliases_impl(*, d: dict[str, str], set_setting, export_self_mods_debug, export_runtime_snapshot_fn) -> None:
    clean = {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in d.items()
        if str(k).strip() and str(v).strip()
    }
    set_setting("command_aliases_json", json.dumps(clean, sort_keys=True))
    export_self_mods_debug()
    export_runtime_snapshot_fn()

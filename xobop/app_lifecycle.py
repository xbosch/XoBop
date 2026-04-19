from __future__ import annotations

import asyncio


async def post_init_impl(*, app, build_scheduler, sync_scheduler, log, load_session_state, load_workflow_state, state, notify_owner, state_persist_loop, artifact_watch_loop, notification_flush_loop, memory_promotion_loop, set_tasks):
    scheduler = build_scheduler()
    app.bot_data["scheduler"] = scheduler
    sync_scheduler(scheduler)
    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    snapshot = load_session_state("runtime_state") or {}
    pending = snapshot.get("pending_action_by_chat", {})
    payloads = snapshot.get("last_email_payload_by_chat", {})
    wf_states = snapshot.get("email_workflow_state_by_chat", {})

    # Prefer DB-backed workflow_state; fallback to session snapshot for compatibility.
    wf_pending = load_workflow_state("pending_action") if callable(load_workflow_state) else {}
    wf_payload = load_workflow_state("last_email_payload") if callable(load_workflow_state) else {}
    wf_state = load_workflow_state("email_workflow_state") if callable(load_workflow_state) else {}

    if isinstance(pending, dict):
        state.pending_action_by_chat = {int(k): v for k, v in pending.items()}
    if isinstance(payloads, dict):
        state.last_email_payload_by_chat = {int(k): v for k, v in payloads.items()}
    if isinstance(wf_states, dict):
        state.email_workflow_state_by_chat = {int(k): str(v) for k, v in wf_states.items()}

    if isinstance(wf_pending, dict) and wf_pending:
        state.pending_action_by_chat = {int(k): v for k, v in wf_pending.items()}
    if isinstance(wf_payload, dict) and wf_payload:
        state.last_email_payload_by_chat = {int(k): v for k, v in wf_payload.items()}
    if isinstance(wf_state, dict) and wf_state:
        state.email_workflow_state_by_chat = {int(k): str(v) for k, v in wf_state.items()}

    if state.pending_action_by_chat:
        try:
            actions = [str(v.get("action", "unknown")) for v in state.pending_action_by_chat.values() if isinstance(v, dict)]
            top = ", ".join(actions[:3]) if actions else "unknown"
            more = "" if len(actions) <= 3 else f" (+{len(actions)-3} more)"
            await notify_owner(f"System restored. Resuming pending task state: {top}{more}.")
        except Exception:
            pass

    t1 = asyncio.create_task(state_persist_loop())
    t2 = asyncio.create_task(artifact_watch_loop())
    t3 = asyncio.create_task(notification_flush_loop())
    t4 = asyncio.create_task(memory_promotion_loop())
    set_tasks(t1, t2, t3, t4)


async def post_shutdown_impl(*, app, log, tasks, clear_tasks, release_runtime_lock=None, wait_bg_futures=None):
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")

    for t in tasks:
        if t and not t.done():
            t.cancel()

    if callable(wait_bg_futures):
        try:
            await wait_bg_futures(timeout_s=2.0)
        except Exception:
            pass

    clear_tasks()
    if callable(release_runtime_lock):
        try:
            release_runtime_lock()
        except Exception:
            pass


async def graceful_shutdown_impl(*, app, log):
    log.info("Shutdown signal received — stopping…")
    await app.stop()
    await app.shutdown()


async def on_error_impl(*, update, context, log):
    err = context.error
    log.exception("Unhandled handler error: %s", err)
    try:
        if update and getattr(update, "effective_message", None):
            await update.effective_message.reply_text("⚠️ Internal error. Please retry.")
    except Exception:
        pass

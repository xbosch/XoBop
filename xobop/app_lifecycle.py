from __future__ import annotations

import asyncio


async def post_init_impl(*, app, build_scheduler, sync_scheduler, log, load_session_state, state, notify_owner, state_persist_loop, artifact_watch_loop, notification_flush_loop, set_tasks):
    scheduler = build_scheduler()
    app.bot_data["scheduler"] = scheduler
    sync_scheduler(scheduler)
    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    snapshot = load_session_state("runtime_state") or {}
    pending = snapshot.get("pending_action_by_chat", {})
    if isinstance(pending, dict):
        state.pending_action_by_chat = {int(k): v for k, v in pending.items()}
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
    set_tasks(t1, t2, t3)


async def post_shutdown_impl(*, app, log, tasks, clear_tasks):
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")

    for t in tasks:
        if t and not t.done():
            t.cancel()
    clear_tasks()


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

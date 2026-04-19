from __future__ import annotations

from pathlib import Path


async def artifact_watch_loop_impl(*, artifact_dir, app_getter, get_setting, quicklook_summary, notify_owner, overseer_enabled, multimodal_artifact_summary, add_memory, log, asyncio_mod):
    seen_artifacts: set[str] = set()
    art = Path(artifact_dir)
    art.mkdir(parents=True, exist_ok=True)
    seen_artifacts = {str(p) for p in art.rglob("*") if p.is_file()}
    while True:
        try:
            current = {str(p) for p in art.rglob("*") if p.is_file()}
            new_files = sorted(current - seen_artifacts)
            seen_artifacts = current
            app = app_getter()
            if new_files and app is not None:
                newest = Path(new_files[-1])
                if (get_setting("artifact_watch_notify", "0") or "0").strip() != "1":
                    continue
                if "/artifacts/inbox/" in str(newest):
                    continue
                summary = await quicklook_summary(newest)
                await notify_owner(f"🆕 New artifact detected:\n{newest}\nQuick Look: {summary}")

                if overseer_enabled() and newest.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}:
                    try:
                        desc = await multimodal_artifact_summary(newest)
                        mem = f"Artifact summary ({newest.name}): {desc} [path={newest}]"
                        await add_memory(mem, mem_type="artifact_summary", importance=0.78, source="artifact_watcher", pinned=False)
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("Artifact watcher failed: %s", exc)
        await asyncio_mod.sleep(30)


async def notification_flush_loop_impl(*, in_quiet_hours, app_getter, get_db, log, asyncio_mod):
    while True:
        try:
            app = app_getter()
            if not in_quiet_hours() and app is not None:
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT id,chat_id,text FROM notification_queue WHERE delivered=0 ORDER BY id ASC LIMIT 5"
                    ).fetchall()
                for r in rows:
                    try:
                        await app.bot.send_message(chat_id=int(r["chat_id"]), text=r["text"])
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE notification_queue SET delivered=1,delivered_ts=CURRENT_TIMESTAMP WHERE id=?",
                                (int(r["id"]),),
                            )
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("Notification flush failed: %s", exc)
        await asyncio_mod.sleep(30)

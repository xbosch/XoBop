from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from telegram.constants import ChatAction

from .emailer import smtp_send_email
from .intents import refers_to_last_attachment
from .router import classify
from .services import run_direct_shell, run_download_count, run_execute_last, run_git_commit_push
from .workflows import handle_attachment_arrival, handle_email_text


async def process_media_message(*, update, state, artifact_dir: str, owner_only, reply, log) -> bool:
    if not owner_only(update):
        return True
    msg = update.message
    if not msg:
        return True
    chat_id = update.effective_chat.id

    media_obj = None
    suffix = ""
    if msg.photo:
        media_obj = msg.photo[-1]
        suffix = ".jpg"
    elif msg.document:
        media_obj = msg.document
        name = msg.document.file_name or ""
        suffix = Path(name).suffix or ""
    if not media_obj:
        return False

    try:
        tg_file = await media_obj.get_file()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        inbox = Path(artifact_dir) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        local_path = inbox / f"chat{chat_id}_{ts}{suffix}"
        await tg_file.download_to_drive(custom_path=str(local_path))
        state.last_attachment_by_chat[chat_id] = str(local_path)

        req = handle_attachment_arrival(chat_id, str(local_path), state)
        if req:
            try:
                sent = smtp_send_email(
                    os.getenv("GMAIL_EMAIL") or os.getenv("GMAIL_USER") or "",
                    os.getenv("GMAIL_APP_PASSWORD") or "",
                    req["to"], req["subject"], req["body"], req["attachment"],
                )
                state.last_email_status_by_chat[chat_id] = sent
                await reply(update, f"Attachment stored: {local_path}\n{sent}")
            except Exception as exc:
                state.last_email_status_by_chat[chat_id] = f"EMAIL_FAILED {str(exc)[:180]}"
                await reply(update, f"Attachment stored: {local_path}\n⚠️ Email send failed: {str(exc)[:180]}")
            return True

        await reply(update, f"Attachment stored: {local_path}")
        return True
    except Exception as exc:
        log.exception("Failed to download attachment")
        await reply(update, f"⚠️ Could not store attachment: {str(exc)[:200]}")
        return True


async def process_text_message(
    *,
    update,
    text: str,
    chat_id: int,
    state,
    reply,
    call_skill,
    mode_getter,
    list_cron_jobs,
    run_agent,
    owner_only,
    cmd_cronadd,
    cmd_cronlist,
    cmd_cronrun,
    cmd_cronruns,
    cmd_status,
    cmd_health,
    cmd_checkup,
    cmd_diagnose,
    cmd_panicstatus,
    cmd_paniclock,
    cmd_panicunlock,
    panic_locked,
    panic_bypass_text,
    op_event,
    log,
) -> bool:
    if not owner_only(update):
        return True

    if panic_locked() and not panic_bypass_text(text):
        op_event("route_selected", chat_id=chat_id, route="panic_block", status="blocked")
        await reply(update, "⛔ Panic-lock active. Allowed commands: /panicstatus, /panicunlock <code>, /status, /health, /checkup, /help")
        return True

    if refers_to_last_attachment(text) and chat_id in state.last_attachment_by_chat:
        text = f"{text}\n\nUse this local attachment path: {state.last_attachment_by_chat[chat_id]}"

    if chat_id in state.pending_action_by_chat and refers_to_last_attachment(text) and chat_id not in state.last_attachment_by_chat:
        await reply(update, "Got it — please send the actual photo/file attachment now.")
        return True

    wf = handle_email_text(text, chat_id, state)
    if wf.get("handled"):
        if wf.get("reply"):
            await reply(update, wf["reply"])
            return True
        if wf.get("send_now"):
            try:
                sent = smtp_send_email(
                    os.getenv("GMAIL_EMAIL") or os.getenv("GMAIL_USER") or "",
                    os.getenv("GMAIL_APP_PASSWORD") or "",
                    wf["to"], wf["subject"], wf["body"], wf.get("attachment"),
                )
                state.last_email_status_by_chat[chat_id] = sent
                await reply(update, sent)
            except Exception as exc:
                state.last_email_status_by_chat[chat_id] = f"EMAIL_FAILED {str(exc)[:180]}"
                await reply(update, f"⚠️ Email send failed: {str(exc)[:180]}")
            return True

    lower = text.lower()
    if lower.startswith("/exec "):
        op_event("route_selected", chat_id=chat_id, route="cmd_exec")
        cmd = text[6:].strip()
        if not cmd:
            await reply(update, "Use: `/exec <command>`")
            return True
        state.last_exec_by_chat[chat_id] = cmd
        await update.message.chat.send_action(ChatAction.TYPING)
        result = await call_skill(chat_id, text, mode_getter(), "run_shell", {"command": cmd})
        await reply(update, result)
        return True
    if lower.startswith("/cronadd "):
        await cmd_cronadd(update)
        return True
    if lower.startswith("/cronlist"):
        await cmd_cronlist(update)
        return True
    if lower.startswith("/cronrun "):
        await cmd_cronrun(update)
        return True
    if lower.startswith("/cronruns "):
        await cmd_cronruns(update)
        return True
    if lower.startswith("/status"):
        op_event("route_selected", chat_id=chat_id, route="cmd_status")
        await cmd_status(update)
        return True
    if lower.startswith("/health"):
        op_event("route_selected", chat_id=chat_id, route="cmd_health")
        await cmd_health(update)
        return True
    if lower.startswith("/checkup"):
        op_event("health_check", chat_id=chat_id, route="cmd_checkup")
        await cmd_checkup(update)
        return True
    if lower.startswith("/diagnose"):
        op_event("route_selected", chat_id=chat_id, route="cmd_diagnose")
        await cmd_diagnose(update)
        return True
    if lower.startswith("/panicstatus"):
        await cmd_panicstatus(update)
        return True
    if lower.startswith("/paniclock"):
        await cmd_paniclock(update)
        return True
    if lower.startswith("/panicunlock"):
        await cmd_panicunlock(update)
        return True
    if text.startswith("/"):
        cmd = text.split()[0]
        op_event("unknown_command", chat_id=chat_id, command=cmd, status="error")
        await reply(update, f"⛔ Unknown command: `{cmd}`\nUse `/help`.")
        return True

    intent = classify(text)
    op_event("route_selected", chat_id=chat_id, route=f"intent:{intent['name']}")
    if intent["name"] == "cron_list":
        await reply(update, list_cron_jobs())
        return True
    if intent["name"] == "download_count":
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_download_count(lambda m: reply(update, m))
        return True
    if intent["name"] == "git_commit_push" and chat_id in state.last_exec_by_chat:
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_git_commit_push(text=text, chat_id=chat_id, state=state, call_skill=call_skill, mode_getter=mode_getter, reply=lambda m: reply(update, m))
        return True
    if intent["name"] == "execute_last" and chat_id in state.last_exec_by_chat:
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_execute_last(text=text, chat_id=chat_id, state=state, call_skill=call_skill, mode_getter=mode_getter, reply=lambda m: reply(update, m))
        return True
    if intent["name"] == "direct_shell":
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_direct_shell(text=text, chat_id=chat_id, state=state, call_skill=call_skill, mode_getter=mode_getter, reply=lambda m: reply(update, m))
        return True

    await update.message.chat.send_action(ChatAction.TYPING)
    result = await run_agent(chat_id, text)
    await reply(update, result)
    return True

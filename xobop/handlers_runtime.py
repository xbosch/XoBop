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


def resolve_command_alias(raw_text: str, aliases: dict[str, str]) -> tuple[str, bool, str | None]:
    text = (raw_text or "").strip()
    if not text.startswith("/"):
        return raw_text, False, None
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].strip().lower()
    rest = parts[1] if len(parts) > 1 else ""
    target = (aliases or {}).get(cmd)
    if not target:
        return raw_text, False, None
    resolved = str(target).strip().lower()
    rewritten = f"/{resolved}" + (f" {rest}" if rest else "")
    return rewritten, True, resolved


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
    cmd_state_check,
    cmd_state_repair,
    cmd_op_sanity,
    cmd_diagnose,
    cmd_improve_propose,
    cmd_improve_list,
    cmd_improve_compile,
    cmd_improve_artifacts,
    cmd_improve_simulate,
    cmd_improve_runs,
    cmd_improve_compare,
    cmd_improve_policy,
    cmd_improve_promote,
    cmd_improve_autopromote,
    cmd_improve_funnel,
    cmd_improve_eval_report,
    cmd_improve_weekly_report,
    cmd_improve_phase,
    cmd_improve_log_rollback,
    cmd_evolve_capture,
    cmd_evolve_memory,
    cmd_panicstatus,
    cmd_paniclock,
    cmd_panicunlock,
    panic_locked,
    panic_bypass_text,
    command_aliases_getter,
    analyze_attachment,
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
        attachment_path = state.last_attachment_by_chat[chat_id]
        attachment_context = ""
        if callable(analyze_attachment):
            try:
                attachment_context = (await analyze_attachment(chat_id, attachment_path, text)) or ""
            except Exception:
                attachment_context = ""
        if attachment_context:
            text = (
                f"{text}\n\nAttachment context (auto):\n{attachment_context}\n\n"
                f"Use this local attachment path: {attachment_path}"
            )
        else:
            text = f"{text}\n\nUse this local attachment path: {attachment_path}"

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

    aliases = command_aliases_getter() or {}
    text, alias_changed, resolved_command = resolve_command_alias(text, aliases)
    lower = text.lower()
    if alias_changed:
        op_event("command_alias_applied", chat_id=chat_id, command=resolved_command or "", metadata={"rewritten": text})

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
    cmd_token = lower.split()[0] if lower.startswith("/") else ""
    command_registry = {
        "/cronadd": (cmd_cronadd, None, None),
        "/cronlist": (cmd_cronlist, None, None),
        "/cronrun": (cmd_cronrun, None, None),
        "/cronruns": (cmd_cronruns, None, None),
        "/status": (cmd_status, "route_selected", "cmd_status"),
        "/health": (cmd_health, "route_selected", "cmd_health"),
        "/checkup": (cmd_checkup, "health_check", "cmd_checkup"),
        "/state_check": (cmd_state_check, "route_selected", "cmd_state_check"),
        "/state_repair": (cmd_state_repair, "route_selected", "cmd_state_repair"),
        "/op_sanity": (cmd_op_sanity, "route_selected", "cmd_op_sanity"),
        "/diagnose": (cmd_diagnose, "route_selected", "cmd_diagnose"),
        "/improve_propose": (cmd_improve_propose, "route_selected", "cmd_improve_propose"),
        "/improve_list": (cmd_improve_list, "route_selected", "cmd_improve_list"),
        "/improve_compile": (cmd_improve_compile, "route_selected", "cmd_improve_compile"),
        "/improve_artifacts": (cmd_improve_artifacts, "route_selected", "cmd_improve_artifacts"),
        "/improve_simulate": (cmd_improve_simulate, "route_selected", "cmd_improve_simulate"),
        "/improve_runs": (cmd_improve_runs, "route_selected", "cmd_improve_runs"),
        "/improve_compare": (cmd_improve_compare, "route_selected", "cmd_improve_compare"),
        "/improve_policy": (cmd_improve_policy, "route_selected", "cmd_improve_policy"),
        "/improve_promote": (cmd_improve_promote, "route_selected", "cmd_improve_promote"),
        "/improve_autopromote": (cmd_improve_autopromote, "route_selected", "cmd_improve_autopromote"),
        "/improve_funnel": (cmd_improve_funnel, "route_selected", "cmd_improve_funnel"),
        "/improve_eval_report": (cmd_improve_eval_report, "route_selected", "cmd_improve_eval_report"),
        "/improve_weekly_report": (cmd_improve_weekly_report, "route_selected", "cmd_improve_weekly_report"),
        "/improve_phase": (cmd_improve_phase, "route_selected", "cmd_improve_phase"),
        "/improve_log_rollback": (cmd_improve_log_rollback, "route_selected", "cmd_improve_log_rollback"),
        "/evolve_capture": (cmd_evolve_capture, "route_selected", "cmd_evolve_capture"),
        "/evolve_memory": (cmd_evolve_memory, "route_selected", "cmd_evolve_memory"),
        "/panicstatus": (cmd_panicstatus, None, None),
        "/paniclock": (cmd_paniclock, None, None),
        "/panicunlock": (cmd_panicunlock, None, None),
    }
    if cmd_token in command_registry:
        handler, event_name, route_name = command_registry[cmd_token]
        if event_name and route_name:
            op_event(event_name, chat_id=chat_id, route=route_name)
        await handler(update)
        return True
    if text.startswith("/"):
        cmd = text.split()[0]
        key = cmd.lstrip("/").lower()
        sug = aliases.get(key)
        op_event("unknown_command", chat_id=chat_id, command=cmd, status="error", metadata={"suggested": sug or ""})
        if sug:
            await reply(update, f"⛔ Unknown command: `{cmd}`\nDid you mean `/{sug}` ?")
        else:
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

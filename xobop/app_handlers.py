from __future__ import annotations


async def cmd_unknown_impl(*, update, owner_only, get_command_aliases, resolve_command_alias, command_dispatch, record_op_event, reply):
    if not owner_only(update):
        return
    text = (update.message.text or "").strip()
    if not text.startswith("/"):
        return

    aliases = get_command_aliases()
    rewritten, changed, resolved = resolve_command_alias(text, aliases)
    if changed and resolved and resolved in command_dispatch:
        try:
            update.message.text = rewritten
        except Exception:
            pass
        record_op_event("command_alias_applied", chat_id=update.effective_chat.id, command=resolved, metadata={"rewritten": rewritten})
        await command_dispatch[resolved](update)
        return

    cmd = text.split()[0]
    key = cmd.lstrip("/").lower()
    sug = aliases.get(key)
    record_op_event("unknown_command", chat_id=update.effective_chat.id, command=cmd, status="error", metadata={"suggested": sug or ""})
    if sug:
        await reply(update, f"⛔ Unknown command: `{cmd}`\nDid you mean `/{sug}` ?")
    else:
        await reply(update, f"⛔ Unknown command: `{cmd}`\nUse `/help`.")


async def handle_media_message_impl(*, update, process_media_message_fn):
    await process_media_message_fn(update)


async def handle_message_impl(*, update, text: str, chat_id: int, panic_locked, panic_bypass_text, reply, record_op_event, process_text_message_fn, log):
    if text:
        cmd = text.split()[0] if text.startswith("/") else ""
        record_op_event("command_requested", chat_id=chat_id, command=cmd, metadata={"text_len": len(text)})

    if panic_locked() and not panic_bypass_text(text):
        await reply(update, "⛔ Panic-lock active. Allowed commands: /panicstatus, /panicunlock <code>, /status, /health, /checkup, /help")
        return

    try:
        await process_text_message_fn(update, text, chat_id)
    except Exception as exc:
        log.exception("Handler error")
        await reply(update, f"⚠️ Error: {str(exc)[:200]}")

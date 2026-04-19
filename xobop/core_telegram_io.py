from __future__ import annotations

import re


def owner_only_impl(*, update, owner_id: int) -> bool:
    return update.effective_chat.id == owner_id


def _escape_underscores_md1(text: str) -> str:
    """Escape bare underscores for Telegram MarkdownV1 outside backtick code spans.

    MarkdownV1 treats _word_ as italic, consuming underscores from identifiers like
    op_events or runtime_snapshot. Escaping as \\_ renders a literal underscore.
    Content inside `...` or ```...``` code spans is left untouched.
    """
    segments = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    result = []
    for i, seg in enumerate(segments):
        if i % 2 == 1:  # inside a code span — leave as-is
            result.append(seg)
        else:
            result.append(seg.replace("_", r"\_"))
    return "".join(result)


async def reply_impl(*, update, text: str, asyncio_mod, fire_and_forget, save_message, telegram_error_cls, log):
    try:
        chat_id = update.effective_chat.id if update and update.effective_chat else 0
        if chat_id:
            loop = asyncio_mod.get_running_loop()
            fire_and_forget(loop, lambda: save_message(chat_id, "assistant", text), label="save_reply_msg")
    except Exception:
        pass

    try:
        await update.message.reply_text(_escape_underscores_md1(text), parse_mode="Markdown")
    except telegram_error_cls:
        try:
            await update.message.reply_text(text)
        except telegram_error_cls as exc:
            log.error("Failed to send reply: %s", exc)

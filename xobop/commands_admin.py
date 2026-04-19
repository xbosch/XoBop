from __future__ import annotations

from typing import Callable

from .commands_core import build_attachdiag_text, build_procinfo_text, build_state_check_text, build_status_text
from .commands_ops import parse_int_or_default
from .mode_utils import mode_help_text


async def cmd_status_impl(*, update, owner_only: Callable, reply: Callable, model: str, embedding_model: str, get_mode: Callable, get_db: Callable):
    if not owner_only(update):
        return
    chat_id = update.effective_chat.id
    await reply(
        update,
        build_status_text(
            chat_id=chat_id,
            model=model,
            embedding_model=embedding_model,
            get_mode=get_mode,
            get_db=get_db,
        ),
    )


async def cmd_procinfo_impl(*, update, owner_only: Callable, reply: Callable, token: str, get_mode: Callable, get_setting: Callable):
    if not owner_only(update):
        return
    await reply(update, build_procinfo_text(token=token, get_mode=get_mode, get_setting=get_setting))


async def cmd_attachdiag_impl(*, update, owner_only: Callable, reply: Callable, state):
    if not owner_only(update):
        return
    chat_id = update.effective_chat.id
    path = state.last_attachment_by_chat.get(chat_id)
    diag = state.last_attachment_diag_by_chat.get(chat_id, {})
    await reply(update, build_attachdiag_text(path=path, diag=diag))


async def cmd_state_check_impl(*, update, owner_only: Callable, reply: Callable, state_check_dict: Callable):
    if not owner_only(update):
        return
    await reply(update, build_state_check_text(state_check=state_check_dict()))


async def cmd_state_repair_impl(*, update, owner_only: Callable, reply: Callable, repair_state_from_snapshot: Callable, record_op_event: Callable):
    if not owner_only(update):
        return
    rep = repair_state_from_snapshot()
    record_op_event("state_repair", chat_id=update.effective_chat.id, status=("ok" if rep.startswith("✅") else "error"))
    await reply(update, rep)


async def cmd_state_clear_impl(*, update, owner_only: Callable, reply: Callable, clear_workflow_state_for_chat: Callable):
    if not owner_only(update):
        return
    chat_id = update.effective_chat.id
    count = clear_workflow_state_for_chat(chat_id)
    await reply(update, f"✅ Cleared workflow state keys: {count}")


async def cmd_op_sanity_impl(*, update, owner_only: Callable, reply: Callable, op_sanity_report: Callable):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    hours = parse_int_or_default(raw, 24)
    await reply(update, op_sanity_report(hours))


async def cmd_diagnose_impl(*, update, owner_only: Callable, reply: Callable, diagnose_report: Callable):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    days = parse_int_or_default(raw, 7)
    await reply(update, diagnose_report(days))


async def cmd_mode_impl(*, update, owner_only: Callable, reply: Callable, modes: set[str], set_setting: Callable, get_mode: Callable, to_thread: Callable):
    if not owner_only(update):
        return
    requested = update.message.text.partition(" ")[2].strip().lower()
    if requested in modes:
        await to_thread(set_setting, "mode", requested)
        await reply(update, f"Mode set to *{requested}*.")
    else:
        await reply(update, mode_help_text(get_mode()))

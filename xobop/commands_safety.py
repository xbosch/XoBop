from __future__ import annotations


async def cmd_delfact_impl(*, update, owner_only, reply, delete_fact):
    if not owner_only(update):
        return
    key = update.message.text.partition(" ")[2].strip()
    await reply(update, delete_fact(key) if key else "Use: `/delfact key`")


async def cmd_exec_impl(*, update, owner_only, reply, panic_locked, state, get_mode, call_skill, log, chat_action_typing):
    if not owner_only(update):
        return
    if panic_locked():
        await reply(update, "⛔ Panic-lock active: /exec disabled.")
        return
    chat_id = update.effective_chat.id
    command = update.message.text.partition(" ")[2].strip()
    if not command:
        await reply(update, "Use: `/exec <command>`")
        return
    state.last_exec_by_chat[chat_id] = command
    await chat_action_typing(update)
    try:
        result = await call_skill(chat_id, update.message.text, get_mode(), "run_shell", {"command": command})
        await reply(update, result)
    except Exception as exc:
        log.exception("cmd_exec failed")
        await reply(update, f"⚠️ Error: {str(exc)[:200]}")


async def cmd_panicstatus_impl(*, update, owner_only, reply, panic_locked, get_setting):
    if not owner_only(update):
        return
    lock = "ON" if panic_locked() else "OFF"
    arm = (get_setting("panic_arm_token", "") or "").strip()
    await reply(update, f"Panic-lock: {lock} (dev: lock feature disabled)\nArmed: {'yes' if arm else 'no'}")


async def cmd_paniclock_impl(*, update, owner_only, reply, now_ts: int, set_setting, get_setting, uuid_hex12: str):
    if not owner_only(update):
        return
    # Dev policy: disable panic-lock to avoid blocking operational testing loops.
    set_setting("panic_lock", "0")
    set_setting("panic_arm_token", "")
    set_setting("panic_arm_ts", "0")
    await reply(update, "ℹ️ Panic-lock is disabled in dev. No lock applied.")


async def cmd_panicunlock_impl(*, update, owner_only, reply, panic_unlock_code_getter, set_setting):
    if not owner_only(update):
        return
    # Dev-friendly behavior: owner can always unlock without code.
    # If a code is configured and provided, we still accept it; if absent/mismatch, unlock anyway.
    _ = (panic_unlock_code_getter() or "").strip()
    set_setting("panic_lock", "0")
    set_setting("panic_arm_token", "")
    set_setting("panic_arm_ts", "0")
    await reply(update, "✅ Panic-lock disabled.")

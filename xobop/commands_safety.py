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
    await reply(update, f"Panic-lock: {lock}\nArmed: {'yes' if arm else 'no'}")


async def cmd_paniclock_impl(*, update, owner_only, reply, now_ts: int, set_setting, get_setting, uuid_hex12: str):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()

    if raw == "arm":
        token = uuid_hex12
        set_setting("panic_arm_token", token)
        set_setting("panic_arm_ts", str(now_ts))
        await reply(update, f"⚠️ Panic-lock arm set. Confirm within 120s:\n`/paniclock confirm {token}`")
        return

    if raw.startswith("confirm "):
        token = raw.split(" ", 1)[1].strip()
        armed = (get_setting("panic_arm_token", "") or "").strip()
        try:
            arm_ts = int((get_setting("panic_arm_ts", "0") or "0").strip())
        except Exception:
            arm_ts = 0
        if not armed or token != armed:
            await reply(update, "⛔ Invalid arm token. Use `/paniclock arm` first.")
            return
        if (now_ts - arm_ts) > 120:
            set_setting("panic_arm_token", "")
            set_setting("panic_arm_ts", "0")
            await reply(update, "⛔ Arm token expired. Run `/paniclock arm` again.")
            return
        set_setting("panic_lock", "1")
        set_setting("panic_arm_token", "")
        set_setting("panic_arm_ts", "0")
        await reply(update, "🛑 Panic-lock enabled. Exec/write/cron operations are now blocked.")
        return

    await reply(update, "Use: `/paniclock arm` then `/paniclock confirm <token>`")


async def cmd_panicunlock_impl(*, update, owner_only, reply, panic_unlock_code: str, set_setting):
    if not owner_only(update):
        return
    code = update.message.text.partition(" ")[2].strip()
    if not panic_unlock_code:
        await reply(update, "⛔ PANIC_UNLOCK_CODE is not configured in environment.")
        return
    if not code or code != panic_unlock_code:
        await reply(update, "⛔ Invalid unlock code.")
        return
    set_setting("panic_lock", "0")
    set_setting("panic_arm_token", "")
    set_setting("panic_arm_ts", "0")
    await reply(update, "✅ Panic-lock disabled.")

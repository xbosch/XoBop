from __future__ import annotations


async def cmd_plan_impl(*, update, owner_only, reply, state):
    if not owner_only(update):
        return
    chat_id = update.effective_chat.id
    goal = update.message.text.partition(" ")[2].strip()
    if not goal:
        await reply(update, "Use: `/plan <goal>`")
        return
    steps = [
        "1) Discover relevant files/resources",
        "2) Run analysis/execution command",
        "3) Validate output",
        "4) Report concise result",
    ]
    state.planned_manifest_by_chat[chat_id] = {"goal": goal, "steps": steps}
    await reply(update, "Planned manifest:\n" + "\n".join(steps) + "\nReply `/go` to execute.")


async def cmd_go_impl(*, update, owner_only, reply, state, run_agent):
    if not owner_only(update):
        return
    chat_id = update.effective_chat.id
    plan = state.planned_manifest_by_chat.get(chat_id)
    if not plan:
        await reply(update, "No pending plan. Use `/plan <goal>` first.")
        return
    goal = plan.get("goal", "")
    result = await run_agent(chat_id, f"Execute this plan now: {goal}", force=True)
    state.planned_manifest_by_chat.pop(chat_id, None)
    await reply(update, result)


async def cmd_checkup_impl(*, update, owner_only, reply, asyncio_mod, run_checkup_sync, record_op_event):
    if not owner_only(update):
        return
    loop = asyncio_mod.get_running_loop()
    rep = await loop.run_in_executor(None, run_checkup_sync)
    record_op_event(
        "health_check",
        chat_id=update.effective_chat.id,
        route="cmd_checkup",
        status=("ok" if "RESULT: PASS" in rep else "error"),
    )
    await reply(update, rep)

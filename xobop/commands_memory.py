from __future__ import annotations


async def cmd_remember_impl(*, update, owner_only, reply, save_fact, save_fact_memory):
    if not owner_only(update):
        return
    body = update.message.text.partition(" ")[2].strip()
    parts = body.split(":", 1)
    if len(parts) == 2:
        key, value = parts[0], parts[1]
        await reply(update, save_fact(key, value))
        await save_fact_memory(key.strip().lower(), value.strip())
    else:
        await reply(update, "Use: `/remember key: value`")


async def cmd_addtask_impl(*, update, owner_only, reply, add_task):
    if not owner_only(update):
        return
    body = update.message.text.partition(" ")[2].strip()
    parts = body.split(":", 1)
    if len(parts) == 2 and parts[0].strip().lower() in {"low", "normal", "high"}:
        priority, desc = parts[0].strip(), parts[1].strip()
    else:
        priority, desc = "normal", body
    await reply(update, add_task(desc, priority) if desc else "Need description.")


async def cmd_donetask_impl(*, update, owner_only, reply, close_task):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        await reply(update, close_task(int(raw)))
    except ValueError:
        await reply(update, "Use: `/donetask <id>`")


async def cmd_facts_impl(*, update, owner_only, reply, get_facts):
    if owner_only(update):
        await reply(update, get_facts())


async def cmd_tasks_impl(*, update, owner_only, reply, get_tasks):
    if owner_only(update):
        await reply(update, get_tasks())


async def cmd_evolve_capture_impl(*, update, owner_only, reply, to_thread, capture_evolution_memory_sync):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        pid = int(raw)
    except Exception:
        await reply(update, "Use: `/evolve_capture <proposal_id>`")
        return
    rep = await to_thread(capture_evolution_memory_sync, pid)
    await reply(update, rep)


async def cmd_evolve_memory_impl(*, update, owner_only, reply, evolution_memory_report):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        limit = int(raw) if raw else 10
    except Exception:
        limit = 10
    await reply(update, evolution_memory_report(limit))


async def cmd_topskills_impl(*, update, owner_only, reply, top_skills):
    if owner_only(update):
        await reply(update, top_skills())


async def cmd_memories_impl(*, update, owner_only, reply, list_memories):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        limit = int(raw) if raw else 20
    except ValueError:
        limit = 20
    await reply(update, list_memories(limit))


async def cmd_memadd_impl(*, update, owner_only, reply, add_memory):
    if not owner_only(update):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await reply(update, "Use: `/memadd text`")
        return
    memory_id = await add_memory(text, mem_type="manual", importance=0.9, source="telegram", pinned=True)
    await reply(update, f"✓ Memory #{memory_id} added.")


async def cmd_memdel_impl(*, update, owner_only, reply, delete_memory_sync):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        memory_id = int(raw)
    except ValueError:
        await reply(update, "Use: `/memdel <id>`")
        return
    await reply(update, delete_memory_sync(memory_id))

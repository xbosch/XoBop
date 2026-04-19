from __future__ import annotations


async def cmd_skills_impl(*, update, owner_only, reply, get_db):
    if not owner_only(update):
        return
    with get_db() as conn:
        rows = conn.execute("SELECT name,enabled,risk_level,handler_type FROM skills ORDER BY id").fetchall()
    if not rows:
        await reply(update, "No skills.")
        return
    lines = [
        f"{'✅' if r['enabled'] else '⏸'} *{r['name']}* [{r['risk_level']}] ({r['handler_type']})"
        for r in rows
    ]
    await reply(update, "\n".join(lines))


async def cmd_skillon_impl(*, update, owner_only, reply, get_db, audit_skill):
    if not owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await reply(update, "Use: `/skillon <name>`")
        return
    with get_db() as conn:
        cur = conn.execute("UPDATE skills SET enabled=1,updated_ts=CURRENT_TIMESTAMP WHERE name=?", (name,))
    if cur.rowcount:
        audit_skill(name, "enabled")
        await reply(update, f"✓ Skill '{name}' enabled.")
    else:
        await reply(update, f"Skill not found: {name}")


async def cmd_skilloff_impl(*, update, owner_only, reply, get_db, audit_skill):
    if not owner_only(update):
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await reply(update, "Use: `/skilloff <name>`")
        return
    with get_db() as conn:
        cur = conn.execute("UPDATE skills SET enabled=0,updated_ts=CURRENT_TIMESTAMP WHERE name=?", (name,))
    if cur.rowcount:
        audit_skill(name, "disabled")
        await reply(update, f"✓ Skill '{name}' disabled.")
    else:
        await reply(update, f"Skill not found: {name}")

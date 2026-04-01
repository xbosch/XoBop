from __future__ import annotations

import json
import re


async def cmd_cronadd_impl(*, update, ctx, owner_only, reply, panic_locked, parse_cron_expr, parse_nl_to_cron, add_cron_job, sync_scheduler):
    if not owner_only(update):
        return
    if panic_locked():
        await reply(update, "⛔ Panic-lock active: /cronadd disabled.")
        return
    body = update.message.text.partition(" ")[2].strip()
    body = re.sub(r"\s+force\s*$", "", body, flags=re.I)
    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 3:
        await reply(update, "Use: `/cronadd name | schedule | skill_type | description | args_json`")
        return

    name = parts[0]
    schedule = parts[1]
    skill_type = parts[2]
    description = parts[3] if len(parts) > 3 else ""
    args_raw = parts[4] if len(parts) > 4 else "{}"

    try:
        skill_args = json.loads(args_raw)
    except json.JSONDecodeError:
        await reply(update, f"Invalid JSON for args: {args_raw}")
        return

    cron_expr = parse_cron_expr(schedule)
    if not cron_expr:
        await reply(update, f"Trying to parse as natural language: _{schedule}_")
        cron_expr = await parse_nl_to_cron(schedule)
        if not cron_expr:
            await reply(update, f"Could not parse schedule: {schedule}")
            return
        await reply(update, f"Schedule interpreted as cron: `{cron_expr}`")

    result = add_cron_job(name, description, cron_expr, skill_type, skill_args)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        sync_scheduler(scheduler)
    await reply(update, result)


async def cmd_cronrm_impl(*, update, ctx, owner_only, reply, panic_locked, remove_cron_job):
    if not owner_only(update):
        return
    if panic_locked():
        await reply(update, "⛔ Panic-lock active: /cronrm disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await reply(update, "Use: `/cronrm <name>`")
        return
    result = remove_cron_job(name)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        try:
            scheduler.remove_job(name)
        except Exception:
            pass
    await reply(update, result)


async def cmd_cronlist_impl(*, update, owner_only, reply, list_cron_jobs):
    if owner_only(update):
        await reply(update, list_cron_jobs())


async def cmd_cronon_impl(*, update, ctx, owner_only, reply, panic_locked, toggle_cron_job, sync_scheduler):
    if not owner_only(update):
        return
    if panic_locked():
        await reply(update, "⛔ Panic-lock active: /cronon disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await reply(update, "Use: `/cronon <name>`")
        return
    result = toggle_cron_job(name, True)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        sync_scheduler(scheduler)
    await reply(update, result)


async def cmd_cronoff_impl(*, update, ctx, owner_only, reply, panic_locked, toggle_cron_job):
    if not owner_only(update):
        return
    if panic_locked():
        await reply(update, "⛔ Panic-lock active: /cronoff disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await reply(update, "Use: `/cronoff <name>`")
        return
    result = toggle_cron_job(name, False)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        try:
            scheduler.remove_job(name)
        except Exception:
            pass
    await reply(update, result)


async def cmd_cronruns_impl(*, update, owner_only, reply, get_cron_runs):
    if not owner_only(update):
        return
    parts = update.message.text.partition(" ")[2].strip().split()
    name = parts[0] if parts else ""
    try:
        limit = int(parts[1]) if len(parts) > 1 else 5
    except ValueError:
        await reply(update, "Limit must be a number. Use: `/cronruns <name> [limit]`")
        return
    if not name:
        await reply(update, "Use: `/cronruns <name> [limit]`")
        return
    await reply(update, get_cron_runs(name, limit))


async def cmd_cronrun_impl(*, update, owner_only, reply, panic_locked, get_db, run_cron_job, chat_action_typing):
    if not owner_only(update):
        return
    if panic_locked():
        await reply(update, "⛔ Panic-lock active: /cronrun disabled.")
        return
    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await reply(update, "Use: `/cronrun <name>`")
        return
    with get_db() as conn:
        row = conn.execute("SELECT name FROM cron_jobs WHERE name=?", (name,)).fetchone()
    if not row:
        await reply(update, f"Job not found: {name}")
        return
    await reply(update, f"▶️ Running job: _{name}_…")
    await chat_action_typing(update)
    await run_cron_job(name)

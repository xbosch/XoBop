from __future__ import annotations


async def cmd_remind_impl(*, update, owner_only, reply, add_reminder, parse_when, utcnow):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    tokens = raw.split()
    if len(tokens) < 2:
        await reply(update, "Use: `/remind <when> <text>` — e.g. `/remind in 30m check the oven`")
        return

    # <when> can be 1-3 tokens ("in 30m", "at 15:30", "tomorrow at 9am"); try longest first.
    due = None
    text = ""
    for n in (3, 2, 1):
        if len(tokens) <= n:
            continue
        candidate_due = parse_when(" ".join(tokens[:n]), utcnow())
        if candidate_due is not None:
            due = candidate_due
            text = " ".join(tokens[n:]).strip()
            break

    if due is None or not text:
        await reply(update, "Couldn't parse the time. Try: `in 30m` / `in 2h` / `at 15:30` / `tomorrow at 9am`")
        return

    reminder_id = add_reminder(chat_id=update.effective_chat.id, text=text, due_ts=due)
    await reply(update, f"⏰ Reminder #{reminder_id} set for {due.strftime('%Y-%m-%d %H:%M')} UTC.")


async def cmd_reminders_impl(*, update, owner_only, reply, list_reminders):
    if not owner_only(update):
        return
    await reply(update, list_reminders())


async def cmd_remindcancel_impl(*, update, owner_only, reply, cancel_reminder):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        reminder_id = int(raw)
    except ValueError:
        await reply(update, "Use: `/remindcancel <id>`")
        return
    await reply(update, cancel_reminder(reminder_id))


async def cmd_remindevery_impl(*, update, ctx, owner_only, reply, parse_cron_expr, parse_nl_to_cron, add_cron_job, sync_scheduler, new_job_name, max_text_len=3500):
    if not owner_only(update):
        return
    body = update.message.text.partition(" ")[2].strip()
    parts = [p.strip() for p in body.split("|", 1)]
    if len(parts) < 2 or not parts[1]:
        await reply(update, "Use: `/remindevery <schedule> | <text>` — e.g. `/remindevery every day at 9am | take meds`")
        return

    schedule, text = parts[0], parts[1][:max_text_len]
    cron_expr = parse_cron_expr(schedule)
    if not cron_expr:
        cron_expr = await parse_nl_to_cron(schedule)
        if not cron_expr:
            await reply(update, f"Could not parse schedule: {schedule}")
            return

    name = new_job_name()
    result = add_cron_job(name, f"Recurring reminder: {text}", cron_expr, "notify", {"text": text}, False)
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        sync_scheduler(scheduler)
    await reply(update, f"{result}\n(cron: `{cron_expr}`) — manage with /cronlist, /cronrm, /cronoff.")

from __future__ import annotations

import re


def help_text() -> str:
    return """*XaviExecutor V5 Commands*\n\nUse /help in runtime for the complete command reference."""


async def cmd_promptshow_impl(*, update, owner_only, reply, get_mode, get_runtime_prompt):
    if not owner_only(update):
        return
    mode = get_mode()
    prompt = get_runtime_prompt(mode)
    await reply(update, f"*Mode:* {mode}\n*Prompt chars:* {len(prompt)}\n\n{prompt}")


async def cmd_promptset_impl(*, update, owner_only, reply, modes: set[str], set_runtime_prompt, strict_prompt: str, normal_prompt: str, to_thread):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        await reply(update, "Use: `/promptset <strict|normal|overseer> <prompt text>` or `/promptset <mode> reset`")
        return
    parts = raw.split(" ", 1)
    mode = parts[0].strip().lower()
    if mode not in modes:
        await reply(update, "Mode must be `strict`, `normal`, or `overseer`.")
        return
    if len(parts) < 2 or not parts[1].strip():
        await reply(update, "Missing prompt text. Example: `/promptset strict You are...`")
        return

    value = parts[1].strip()
    if value.lower() == "reset":
        value = strict_prompt if mode == "strict" else normal_prompt

    await to_thread(set_runtime_prompt, mode, value)
    await reply(update, f"✅ Updated *{mode}* prompt ({len(value)} chars).")


async def cmd_quiethours_impl(*, update, owner_only, reply, get_setting, set_setting):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        cur = get_setting("quiet_hours", "") or "off"
        await reply(update, f"Current quiet hours: {cur}. Use `/quiethours HH:MM-HH:MM`.")
        return
    if not re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", raw):
        await reply(update, "Format: `/quiethours HH:MM-HH:MM` (24h)")
        return
    set_setting("quiet_hours", raw)
    await reply(update, f"✅ Quiet hours set: {raw}")


async def cmd_quietoff_impl(*, update, owner_only, reply, set_setting):
    if not owner_only(update):
        return
    set_setting("quiet_hours", "")
    await reply(update, "✅ Quiet hours disabled")


async def cmd_quietflush_impl(*, update, owner_only, reply, get_db, client, model):
    if not owner_only(update):
        return
    with get_db() as conn:
        rows = conn.execute("SELECT id,text FROM notification_queue WHERE delivered=0 ORDER BY id ASC LIMIT 50").fetchall()
    if not rows:
        await reply(update, "No queued notifications.")
        return

    texts = [r["text"] for r in rows]
    summary = ""
    try:
        joined = "\n".join(texts)[:4000]
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Summarize queued system notifications in 1-2 sentences with the most important item first."},
                {"role": "user", "content": joined},
            ],
            temperature=0.1,
            max_tokens=140,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception:
        summary = "Queued notifications summarized unavailable; sending raw count only."

    sent = 0
    for r in rows:
        try:
            with get_db() as conn:
                conn.execute("UPDATE notification_queue SET delivered=1,delivered_ts=CURRENT_TIMESTAMP WHERE id=?", (int(r["id"]),))
            sent += 1
        except Exception:
            pass

    await reply(update, f"Quiet-hours digest ({sent} items):\n{summary}")

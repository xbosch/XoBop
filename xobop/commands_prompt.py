from __future__ import annotations

import re


HELP_TEXT = """*XaviExecutor V5 Commands*

*Memory*
/remember key: value — save a fact
/delfact key — delete a fact
/facts — list all facts
/memories [limit] — list semantic memory
/memadd text — add pinned memory
/memdel id — delete memory
/memdedup [confirm] [threshold] — preview (default) or apply (confirm) near-duplicate merge, threshold 0.5-0.999 default 0.92
/remind <when> <text> — one-shot reminder (in 30m / in 2h / at 15:30 / tomorrow at 9am)
/reminders — list pending reminders
/remindcancel id — cancel a pending reminder
/remindevery <schedule> | <text> — recurring reminder (cron syntax or natural language schedule); manage via /cronlist /cronrm /cronoff
/addtask [priority:] desc — add a task
/donetask id — close a task
/tasks — list open tasks

*Control*
/mode [strict|normal|overseer] — get or set mode
/promptshow — show active runtime prompt
/promptset mode text — set runtime prompt (strict/normal/overseer)
/promptproposal mode text — stage prompt change proposal (strict/normal/overseer)
/promptapprove — apply staged prompt proposal
/promptreject — reject staged prompt proposal
/quiethours HH:MM-HH:MM — queue notifications during quiet window
/quietoff — disable quiet hours
/quietflush — flush queued notifications now
/status — system status
/health — health summary
/checkup — dependency/env drift check
/state_check — compare canonical runtime snapshot vs DB/settings
/state_repair — repair DB/settings from runtime snapshot
/op_sanity [hours] — instrumentation sanity check and anomaly scan
/diagnose [days] — ops diagnosis from instrumentation signals
/improve_propose [days] — generate a data-backed improvement proposal
/improve_list — list recent improvement proposals
/improve_compile <proposal_id> — compile proposal into typed change artifact
/improve_artifacts [proposal_id] — list change artifacts
/improve_simulate <proposal_id> — run simulation gate for a proposal
/improve_runs [proposal_id] — list recent simulation runs
/improve_compare <proposal_id> [window_days] — compare before/after impact
/improve_policy [manual|assisted|auto_low] — set promotion autonomy policy
/improve_promote <proposal_id> [force <reason>] — promote proposal with risk-policy checks
/improve_reject <proposal_id> [reason] — reject a pending proposal
/improve_set_retry <0-3> — manually set exec_retry_limit (use to reset from stuck max)
/improve_autopromote — auto-promote low-risk proposals (policy=auto_low)
/improve_funnel [days] — report proposal→artifact→simulation→promotion→impact funnel
/improve_eval_report [days] — consolidated evaluation report for paper/stability
/improve_weekly_report [weeks] — weekly trend report (headline metrics + confidence)
/improve_phase [baseline|improving] — controlled live evaluation phase tag
/improve_log_rollback <artifact_id> <reason> — log rollback telemetry event
/evolve_capture <proposal_id> — persist proposal outcome lesson into evolution memory
/evolve_recapture_all — backfill evolution memory for all promoted proposals
/evolve_memory [limit] — show recent evolution memory lessons
/evolve_status — report learned query vectors and block compression state
/evolve_blocks_rebuild [N] — manually recompute temporal block summaries (default N=4)
/plan goal — create execution manifest
/go — execute staged manifest
/topskills — most used skills
/clearctx — clear conversation context
/exec command — run through agent
/attachdiag — show latest attachment extraction diagnostics
/panicstatus — show panic-lock state
/paniclock arm|confirm <token> — enable panic-lock (2-step)
/panicunlock <code> — disable panic-lock (requires env code)

*Skills*
/skills — list skills
/skillon name — enable skill
/skilloff name — disable skill

*Cron Jobs*
/cronadd name | schedule | skill_type | description | args_json
/cronrm name — remove job
/cronlist — list jobs
/cronon name — enable job
/cronoff name — disable job
/cronrun name — run now
/cronruns name [limit] — recent runs

Cron skill_types: run_shell, browse_page, read_file, write_file, append_file, agent, memory_cleanup, db_backup, prompt_review, checkup, system_map_refresh
Schedule: cron syntax or natural language like every day at 9am
"""


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


async def cmd_promptproposal_impl(*, update, owner_only, reply, modes: set[str], get_prompt_proposal, set_prompt_proposal):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    current = get_prompt_proposal()
    if not raw:
        if current:
            await reply(update, f"Pending proposal: mode={current['mode']} chars={len(current['text'])}. Use `/promptapprove` or `/promptreject`.")
        else:
            await reply(update, "Use: `/promptproposal <strict|normal> <text>`")
        return
    parts = raw.split(" ", 1)
    mode = parts[0].strip().lower()
    if mode not in modes:
        await reply(update, "Mode must be `strict`, `normal`, or `overseer`.")
        return
    if len(parts) < 2 or not parts[1].strip():
        await reply(update, "Missing proposal text.")
        return
    text = parts[1].strip()
    set_prompt_proposal({"mode": mode, "text": text})
    await reply(update, f"📝 Proposal stored for {mode} ({len(text)} chars). Run `/promptapprove` to apply or `/promptreject` to discard.")


async def cmd_promptapprove_impl(*, update, owner_only, reply, get_prompt_proposal, set_prompt_proposal, set_runtime_prompt):
    if not owner_only(update):
        return
    current = get_prompt_proposal()
    if not current:
        await reply(update, "No pending prompt proposal.")
        return
    mode = current["mode"]
    text = current["text"]
    set_runtime_prompt(mode, text)
    set_prompt_proposal(None)
    await reply(update, f"✅ Proposal applied to {mode} prompt.")


async def cmd_promptreject_impl(*, update, owner_only, reply, set_prompt_proposal):
    if not owner_only(update):
        return
    set_prompt_proposal(None)
    await reply(update, "✅ Prompt proposal discarded.")


async def cmd_help_impl(*, update, owner_only, reply):
    if not owner_only(update):
        return
    await reply(update, HELP_TEXT)


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

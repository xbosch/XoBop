from __future__ import annotations


async def cmd_improve_propose_impl(*, update, owner_only, reply, generate_improvement_proposal, record_op_event):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        days = int(raw) if raw else 7
    except ValueError:
        days = 7
    _pid, txt = generate_improvement_proposal(days)
    record_op_event("proposal_generated", chat_id=update.effective_chat.id, route="cmd_improve_propose", status="ok")
    await reply(update, txt)


async def cmd_improve_list_impl(*, update, owner_only, reply, get_db):
    if not owner_only(update):
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,created_ts,status,risk_level,title FROM improvement_proposals ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await reply(update, "No improvement proposals yet.")
        return
    lines = [f"#{int(r['id'])} [{r['risk_level']}] {r['status']} — {r['title']} ({r['created_ts']})" for r in rows]
    await reply(update, "\n".join(lines))


async def cmd_improve_compile_impl(*, update, owner_only, reply, to_thread, compile_proposal_to_change):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        pid = int(raw)
    except Exception:
        await reply(update, "Use: `/improve_compile <proposal_id>`")
        return
    rep = await to_thread(compile_proposal_to_change, pid)
    await reply(update, rep)


async def cmd_improve_artifacts_impl(*, update, owner_only, reply, get_db):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    with get_db() as conn:
        if raw:
            try:
                pid = int(raw)
            except Exception:
                await reply(update, "Use: `/improve_artifacts [proposal_id]`")
                return
            rows = conn.execute(
                "SELECT id,proposal_id,artifact_type,target,status,created_ts,applied_ts,commit_hash FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 10",
                (pid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,proposal_id,artifact_type,target,status,created_ts,applied_ts,commit_hash FROM change_artifacts ORDER BY id DESC LIMIT 10"
            ).fetchall()
    if not rows:
        await reply(update, "No change artifacts yet.")
        return
    lines = [f"a#{int(r['id'])} p#{int(r['proposal_id'])} {r['artifact_type']}:{r['target']} {r['status']} commit={r['commit_hash'] or '-'}" for r in rows]
    await reply(update, "\n".join(lines))


async def cmd_improve_simulate_impl(*, update, owner_only, reply, to_thread, simulate_proposal_sync):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        proposal_id = int(raw)
    except Exception:
        await reply(update, "Use: `/improve_simulate <proposal_id>`")
        return
    rep = await to_thread(simulate_proposal_sync, proposal_id)
    await reply(update, rep)


async def cmd_improve_runs_impl(*, update, owner_only, reply, get_db):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    with get_db() as conn:
        if raw:
            try:
                pid = int(raw)
            except Exception:
                await reply(update, "Use: `/improve_runs [proposal_id]`")
                return
            rows = conn.execute(
                "SELECT id,proposal_id,created_ts,phase,status,notes FROM improvement_runs WHERE proposal_id=? ORDER BY id DESC LIMIT 10",
                (pid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,proposal_id,created_ts,phase,status,notes FROM improvement_runs ORDER BY id DESC LIMIT 10"
            ).fetchall()
    if not rows:
        await reply(update, "No improvement runs yet.")
        return
    lines = [f"run#{int(r['id'])} p#{int(r['proposal_id'] or 0)} {r['phase']} {r['status']} — {r['created_ts']}" for r in rows]
    await reply(update, "\n".join(lines))


async def cmd_improve_compare_impl(*, update, owner_only, reply, to_thread, impact_compare_sync):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip().split()
    if not raw:
        await reply(update, "Use: `/improve_compare <proposal_id> [window_days]`")
        return
    try:
        proposal_id = int(raw[0])
        window = int(raw[1]) if len(raw) > 1 else 7
    except Exception:
        await reply(update, "Use: `/improve_compare <proposal_id> [window_days]`")
        return
    rep = await to_thread(impact_compare_sync, proposal_id, window)
    await reply(update, rep)


async def cmd_improve_policy_impl(*, update, owner_only, reply, get_setting, get_improve_policy, set_setting):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        fam = get_setting("artifact_families_allowed", "alias,retry,lineage,prompt") or "alias,retry,lineage,prompt"
        await reply(update, f"improve_policy: {get_improve_policy()}\nartifact_families_allowed: {fam}\nUse: `/improve_policy <manual|assisted|auto_low>` or `/improve_policy artifact_families_allowed <csv>`")
        return

    parts = raw.split(maxsplit=1)
    key = parts[0].strip().lower()
    if key == "artifact_families_allowed":
        if len(parts) < 2 or not parts[1].strip():
            await reply(update, "Use: `/improve_policy artifact_families_allowed alias,retry,lineage,prompt`")
            return
        val = ",".join([x.strip().lower() for x in parts[1].split(",") if x.strip()])
        set_setting("artifact_families_allowed", val)
        await reply(update, f"✅ artifact_families_allowed set to: {val}")
        return

    pol = raw.strip().lower()
    if pol not in {"manual", "assisted", "auto_low"}:
        await reply(update, "Use: `/improve_policy <manual|assisted|auto_low>` or `/improve_policy artifact_families_allowed <csv>`")
        return
    set_setting("improve_policy", pol)
    await reply(update, f"✅ improve_policy set to: {pol}")


async def cmd_improve_promote_impl(*, update, owner_only, reply, to_thread, promote_proposal_sync):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip().split()
    if not raw:
        await reply(update, "Use: `/improve_promote <proposal_id> [force <reason>]`")
        return
    try:
        pid = int(raw[0])
    except Exception:
        await reply(update, "Use: `/improve_promote <proposal_id> [force <reason>]`")
        return
    force = any(x.lower() == "force" for x in raw[1:])
    reason = ""
    if force:
        try:
            i = next(i for i, x in enumerate(raw[1:], start=1) if x.lower() == "force")
            reason = " ".join(raw[i+1:]).strip()
        except Exception:
            reason = ""
        if not reason:
            await reply(update, "Use: `/improve_promote <proposal_id> force <reason>`")
            return
    rep = await to_thread(promote_proposal_sync, pid, force, reason)
    await reply(update, rep)


async def cmd_improve_autopromote_impl(*, update, owner_only, reply, to_thread, auto_promote_low_sync):
    if not owner_only(update):
        return
    rep = await to_thread(auto_promote_low_sync)
    await reply(update, rep)


async def cmd_improve_funnel_impl(*, update, owner_only, reply, improve_funnel_report):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        days = int(raw) if raw else 30
    except Exception:
        days = 30
    await reply(update, improve_funnel_report(days))


async def cmd_improve_eval_report_impl(*, update, owner_only, reply, improve_eval_report):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        days = int(raw) if raw else 30
    except Exception:
        days = 30
    await reply(update, improve_eval_report(days))


async def cmd_improve_weekly_report_impl(*, update, owner_only, reply, improve_weekly_report):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        weeks = int(raw) if raw else 4
    except Exception:
        weeks = 4
    await reply(update, improve_weekly_report(weeks))


async def cmd_improve_phase_impl(*, update, owner_only, reply, get_eval_phase, set_setting):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip().lower()
    if not raw:
        await reply(update, f"improve_eval_phase: {get_eval_phase()}\nUse: `/improve_phase <baseline|improving>`")
        return
    if raw not in {"baseline", "improving"}:
        await reply(update, "Use: `/improve_phase <baseline|improving>`")
        return
    set_setting("improve_eval_phase", raw)
    await reply(update, f"✅ improve_eval_phase set to: {raw}")


async def cmd_improve_log_rollback_impl(*, update, owner_only, reply, record_op_event):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    parts = raw.split(" ", 1)
    if not parts or not parts[0].isdigit():
        await reply(update, "Use: `/improve_log_rollback <artifact_id> <reason>`")
        return
    artifact_id = int(parts[0])
    reason = parts[1].strip() if len(parts) > 1 else ""
    record_op_event("rollback_event", chat_id=update.effective_chat.id, route="manual", status="ok", metadata={"artifact_id": artifact_id, "reason": reason})
    await reply(update, f"✅ rollback event logged for artifact #{artifact_id}")

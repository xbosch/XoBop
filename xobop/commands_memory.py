from __future__ import annotations

_REMEMBER_BLOCKLIST = frozenset({
    "override", "disable", "bypass", "safety check", "ignore all",
    "forget all", "temp instruction", "always send", "send email",
    "system prompt", "jailbreak",
})


async def cmd_remember_impl(*, update, owner_only, reply, save_fact, save_fact_memory):
    if not owner_only(update):
        return
    body = update.message.text.partition(" ")[2].strip()
    parts = body.split(":", 1)
    if len(parts) == 2:
        key, value = parts[0], parts[1]
        combined = (key + " " + value).lower()
        if any(term in combined for term in _REMEMBER_BLOCKLIST):
            await reply(update, "⛔ Memory content rejected: safety-override language detected.")
            return
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


async def cmd_evolve_capture_impl(
    *, update, owner_only, reply, to_thread, capture_evolution_memory_sync,
    embed_evolution_entry_async=None, embed_fn=None, update_query_vector_async=None,
):
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        pid = int(raw)
    except Exception:
        await reply(update, "Use: `/evolve_capture <proposal_id>`")
        return
    rep = await to_thread(capture_evolution_memory_sync, pid)
    if embed_evolution_entry_async and embed_fn:
        try:
            await embed_evolution_entry_async(pid=pid, embed_fn=embed_fn)
        except Exception:
            pass  # embedding is best-effort; don't fail the capture
    if update_query_vector_async and embed_fn:
        try:
            await update_query_vector_async(pid=pid, embed_fn=embed_fn)
        except Exception:
            pass  # vector update is best-effort
    await reply(update, rep)


async def cmd_evolve_recapture_all_impl(*, update, owner_only, reply, to_thread, recapture_all_evolution_sync):
    if not owner_only(update):
        return
    rep = await to_thread(recapture_all_evolution_sync)
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


async def cmd_evolve_status_impl(*, update, owner_only, reply, get_db):
    """Report learned query vector state per failure class."""
    if not owner_only(update):
        return
    import json, math
    lines = ["*Evolution memory status*"]
    with get_db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        if "evolution_query_vectors" in tables:
            qv_rows = conn.execute(
                "SELECT failure_class, update_count, ema_alpha, last_updated_ts FROM evolution_query_vectors ORDER BY update_count DESC"
            ).fetchall()
            if qv_rows:
                lines.append("\n*Learned query vectors:*")
                for r in qv_rows:
                    active = "✓ active" if int(r["update_count"] or 0) >= 2 else "⏳ warming up"
                    lines.append(f"  `{r['failure_class']}` n={r['update_count']} α={r['ema_alpha']} {active} ({str(r['last_updated_ts'] or '')[:10]})")
            else:
                lines.append("\nNo learned vectors yet — run /evolve_capture on promoted proposals.")
        else:
            lines.append("\nevolution_query_vectors table missing — restart to apply migrations.")

        if "evolution_memory_blocks" in tables:
            blk_rows = conn.execute(
                "SELECT block_index, entry_count, dominant_failure_class, avg_metric_delta, date_from, date_to, block_embedding_json FROM evolution_memory_blocks ORDER BY block_index"
            ).fetchall()
            if blk_rows:
                lines.append(f"\n*Block compression (N={len(blk_rows)}):*")
                for b in blk_rows:
                    has_centroid = "✓" if b["block_embedding_json"] else "⚠ no centroid"
                    delta = f"Δ={b['avg_metric_delta']:+.2f}" if b["avg_metric_delta"] is not None else "Δ=?"
                    lines.append(f"  B{b['block_index']} [{b['date_from']}→{b['date_to']}] n={b['entry_count']} fc={b['dominant_failure_class']} {delta} {has_centroid}")
            else:
                lines.append("\nNo blocks yet — runs nightly at 02:00 UTC or use /evolve_blocks_rebuild.")

        em_count = conn.execute("SELECT COUNT(*) FROM evolution_memory").fetchone()[0] if "evolution_memory" in tables else 0
        lines.append(f"\nTotal evolution entries: {em_count}")

    await reply(update, "\n".join(lines))


async def cmd_evolve_blocks_rebuild_impl(*, update, owner_only, reply, to_thread, recompute_evolution_blocks, get_db):
    """Manually trigger block recomputation with optional N."""
    if not owner_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    try:
        n = max(2, min(16, int(raw))) if raw.isdigit() else 4
    except Exception:
        n = 4
    n_written = await recompute_evolution_blocks(get_db=get_db, n_blocks=n)
    await reply(update, f"Evolution blocks rebuilt: {n_written} blocks (N={n}).")


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


async def cmd_memdiag_impl(*, update, owner_only, reply, memory_diag):
    if not owner_only(update):
        return
    await reply(update, memory_diag())

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class MemoryItem:
    id: int
    text: str
    mem_type: str
    importance: float
    created_ts: str
    last_access_ts: str


async def embed_text_impl(*, text: str, max_memory_text: int, client, embedding_model: str, embedding_fallback: bool, fallback_embedding, log):
    safe_text = text[:max_memory_text]
    try:
        resp = await client.embeddings.create(model=embedding_model, input=safe_text)
        data = resp.data[0].embedding
        return [float(x) for x in data], embedding_model
    except Exception as exc:
        if not embedding_fallback:
            raise RuntimeError(f"Embedding request failed: {exc}") from exc
        log.warning("Embedding API unavailable, using fallback embedding: %s", exc)
        return fallback_embedding(safe_text), "fallback-hash-256"


def insert_memory_item_sync_impl(*, get_db, text: str, mem_type: str, importance: float, source: str, pinned: bool, max_memory_text: int) -> int:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO memory_items (text,mem_type,importance,source,pinned)
            VALUES (?,?,?,?,?)
            """,
            (text[:max_memory_text], mem_type, importance, source, 1 if pinned else 0),
        )
        row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
        memory_id = int(row["id"])
        conn.execute("INSERT INTO memory_items_fts(rowid,text) VALUES (?,?)", (memory_id, text[:max_memory_text]))
        return memory_id


def save_memory_vector_sync_impl(*, get_db, memory_id: int, embedding: list[float], model_name: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_vectors (memory_id,embedding_json,model,created_ts)
            VALUES (?,?,?,CURRENT_TIMESTAMP)
            """,
            (memory_id, json.dumps(embedding), model_name),
        )


def delete_memory_sync_impl(*, get_db, memory_id: int) -> str:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE memory_items SET is_deleted=1 WHERE id=? AND is_deleted=0",
            (memory_id,),
        )
        if cur.rowcount:
            conn.execute("DELETE FROM memory_items_fts WHERE rowid=?", (memory_id,))
    return f"✓ Memory {memory_id} deleted." if cur.rowcount else f"Memory not found: {memory_id}"


async def add_memory_impl(*, text: str, mem_type: str, importance: float, source: str, pinned: bool, max_memory_text: int, insert_memory_item_sync, embed_text, save_memory_vector_sync) -> int:
    clean_text = text.strip()[:max_memory_text]
    if not clean_text:
        raise RuntimeError("empty memory")
    memory_id = insert_memory_item_sync(clean_text, mem_type, importance, source, pinned)
    embedding, model_name = await embed_text(clean_text)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: save_memory_vector_sync(memory_id, embedding, model_name))
    return memory_id


def promote_fact_to_memory_sync_impl(*, get_db, key: str, value: str) -> None:
    text = f"Fact: {key} = {value}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_items WHERE text=? AND is_deleted=0 LIMIT 1",
            (text,),
        ).fetchone()
    if row:
        with get_db() as conn:
            conn.execute(
                "UPDATE memory_items SET importance=MAX(importance, 0.95), pinned=1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )


async def save_fact_memory_impl(*, get_db, key: str, value: str, add_memory) -> None:
    text = f"Fact: {key} = {value}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_items WHERE text=? AND is_deleted=0 LIMIT 1",
            (text,),
        ).fetchone()
    if row:
        with get_db() as conn:
            conn.execute(
                "UPDATE memory_items SET importance=MAX(importance, 0.95), pinned=1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
        return
    await add_memory(text, mem_type="fact", importance=0.95, source="fact", pinned=True)


def memory_rows_impl(*, get_db):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT mi.id, mi.text, mi.mem_type, mi.importance, mi.created_ts, mi.last_access_ts, mv.embedding_json
            FROM memory_items mi
            JOIN memory_vectors mv ON mv.memory_id = mi.id
            WHERE mi.is_deleted=0
            ORDER BY mi.id DESC
            """
        ).fetchall()
    return rows


def retrieve_memory_lexical_impl(*, get_db, query: str, k: int, max_memory_results: int, split_words):
    terms = [w for w in split_words(query) if len(w) >= 3][:6]
    if not terms:
        return []

    match_q = " OR ".join(terms)
    sql = """
        SELECT mi.id,mi.text,mi.mem_type,mi.importance,mi.created_ts,mi.last_access_ts
        FROM memory_items_fts f
        JOIN memory_items mi ON mi.id = f.rowid
        WHERE f.text MATCH ? AND mi.is_deleted=0
        ORDER BY mi.pinned DESC, mi.importance DESC, mi.last_access_ts DESC, mi.id DESC
        LIMIT ?
    """
    with get_db() as conn:
        rows = conn.execute(sql, (match_q, max(1, min(k, max_memory_results)))).fetchall()
        ids = [int(r["id"]) for r in rows]
        if ids:
            conn.executemany(
                "UPDATE memory_items SET access_count = access_count + 1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                [(i,) for i in ids],
            )

    return [
        MemoryItem(
            id=int(r["id"]),
            text=r["text"],
            mem_type=r["mem_type"],
            importance=float(r["importance"]),
            created_ts=r["created_ts"],
            last_access_ts=r["last_access_ts"],
        )
        for r in rows
    ]


async def retrieve_memory_impl(*, get_db, query: str, k: int, max_memory_results: int, embed_text, retrieve_memory_lexical, memory_rows, cosine, recency_score):
    q_emb, model_name = await embed_text(query)
    if str(model_name).startswith("fallback"):
        return retrieve_memory_lexical(query, k=k)

    rows = memory_rows()
    if not rows:
        return retrieve_memory_lexical(query, k=k)

    q_dim = len(q_emb) if isinstance(q_emb, list) else 0
    scored: list[tuple[float, MemoryItem]] = []
    valid_vectors = 0
    for r in rows:
        try:
            emb = json.loads(r["embedding_json"])
            if not isinstance(emb, list):
                continue
            if q_dim and len(emb) != q_dim:
                continue
        except Exception:
            continue
        valid_vectors += 1
        sim = cosine(q_emb, emb)
        rec = recency_score(r["last_access_ts"])
        score = 0.7 * sim + 0.2 * float(r["importance"]) + 0.1 * rec
        item = MemoryItem(
            id=int(r["id"]),
            text=r["text"],
            mem_type=r["mem_type"],
            importance=float(r["importance"]),
            created_ts=r["created_ts"],
            last_access_ts=r["last_access_ts"],
        )
        scored.append((score, item))

    # If vectors are missing/stale/mismatched, fall back to lexical retrieval.
    need = max(1, min(k, max_memory_results))
    if valid_vectors < need or not scored:
        return retrieve_memory_lexical(query, k=k)

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [item for _score, item in scored[:need]]
    ids = [item.id for item in top]
    if ids:
        with get_db() as conn:
            conn.executemany(
                "UPDATE memory_items SET access_count = access_count + 1, last_access_ts=CURRENT_TIMESTAMP WHERE id=?",
                [(i,) for i in ids],
            )
    return top


def list_memories_impl(*, get_db, limit: int = 20) -> str:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, mem_type, importance, text, last_access_ts, access_count
            FROM memory_items
            WHERE is_deleted=0
            ORDER BY pinned DESC, importance DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
    if not rows:
        return "No memories."
    return "\n".join(
        f"#{r['id']} [{r['mem_type']}] imp={r['importance']:.2f} hits={r['access_count']} — {r['text'][:140]}"
        for r in rows
    )


def memory_count_impl(*, get_db) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()
    return int(row["n"])


def prune_memory_sync_impl(*, get_db, get_policy, max_memory_items: int) -> None:
    keep = int(get_policy("memory_cleanup_keep", str(max_memory_items)))
    with get_db() as conn:
        conn.execute(
            """
            UPDATE memory_items
            SET is_deleted=1
            WHERE id IN (
                SELECT id FROM memory_items
                WHERE is_deleted=0 AND pinned=0
                ORDER BY importance DESC, last_access_ts DESC, id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (keep,),
        )


def today_daily_path_impl(*, daily_memory_dir: Path) -> Path:
    return daily_memory_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def yesterday_daily_path_impl(*, daily_memory_dir: Path) -> Path:
    return daily_memory_dir / f"{(datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')}.md"


def append_daily_memory_impl(*, daily_memory_dir: Path, text: str) -> None:
    p = today_daily_path_impl(daily_memory_dir=daily_memory_dir)
    if not p.exists():
        p.write_text(f"# {datetime.now().strftime('%Y-%m-%d')}\n\n", encoding="utf-8")
    clean = text.strip().replace("\n", " ")
    line = f"- {clean}\n"
    existing = p.read_text(encoding="utf-8", errors="ignore")
    if clean and clean not in existing:
        with p.open("a", encoding="utf-8") as f:
            f.write(line)


def promote_long_memory_impl(*, long_memory_file: Path, text: str) -> None:
    clean = text.strip().replace("\n", " ")
    if not clean:
        return
    if not long_memory_file.exists():
        long_memory_file.write_text("# MEMORY.md\n\n## Promoted notes\n", encoding="utf-8")
    existing = long_memory_file.read_text(encoding="utf-8", errors="ignore")
    if clean in existing:
        return
    with long_memory_file.open("a", encoding="utf-8") as f:
        f.write(f"- {clean}\n")


def build_file_memory_block_impl(*, long_memory_file: Path, daily_memory_dir: Path) -> str:
    chunks: list[str] = []
    if long_memory_file.exists():
        txt = long_memory_file.read_text(encoding="utf-8", errors="ignore").strip()
        if txt:
            chunks.append("Long-term file memory:\n" + txt[:4000])
    for p in (yesterday_daily_path_impl(daily_memory_dir=daily_memory_dir), today_daily_path_impl(daily_memory_dir=daily_memory_dir)):
        if p.exists():
            txt = p.read_text(encoding="utf-8", errors="ignore").strip()
            if txt:
                chunks.append(f"Daily memory ({p.name}):\n" + txt[:2000])
    return ("\n\n" + "\n\n".join(chunks)) if chunks else ""


def _looks_like_promotion_junk(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True

    # Strong instruction / script / artifact signals should never be promoted as memory.
    imperative_hits = sum(1 for k in ["step 1", "step 2", "return exactly", "execute", "create file", "create folder", "run this"] if k in t)
    shellish_hits = sum(1 for k in ["#!/", "chmod", "pip install", "python3 ", "bash ", "&&", "||", "./run", "plot.svg", "agent-test/"] if k in t)
    if imperative_hits >= 2 or shellish_hits >= 2:
        return True

    # Ignore machine-output style key/value dumps.
    if "run_status=" in t or "error=" in t or "unknown_keys=" in t:
        return True

    # Ignore low-information repeated character blobs.
    if len(set(t)) <= 3 and len(t) > 40:
        return True

    return False


async def maybe_promote_memory_impl(*, text: str, source: str, add_memory, append_daily_memory, promote_long_memory, log) -> None:
    clean = text.strip()
    if len(clean) < 24:
        return
    if _looks_like_promotion_junk(clean):
        return

    lowered = clean.lower()
    importance = 0.45
    mem_type = "note"
    if any(w in lowered for w in ["prefer", "always", "never", "remember", "important", "from now on"]):
        importance = 0.85
        mem_type = "preference"
    elif any(w in lowered for w in ["task", "todo", "deadline", "meeting"]):
        importance = 0.7
        mem_type = "observation"
    elif len(clean) > 400:
        importance = 0.55
    else:
        return
    try:
        await add_memory(clean, mem_type=mem_type, importance=importance, source=source, pinned=False)
        append_daily_memory(clean)
        if mem_type in {"preference", "observation"} and importance >= 0.8:
            promote_long_memory(clean)
    except Exception as exc:
        log.warning("Could not promote memory: %s", exc)


def build_structured_memory_block_impl(*, query: str, get_facts, get_tasks) -> str:
    q = query.lower()
    parts = []
    if any(w in q for w in ("remember", "fact", "know", "status")):
        parts.append("Saved facts:\n" + get_facts())
    if any(w in q for w in ("task", "done", "todo")):
        parts.append("Open tasks:\n" + get_tasks())
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def memory_item_is_prompt_polluting_impl(*, text: str, query: str) -> bool:
    t = (text or "").lower()
    q = (query or "").lower()

    # Generic prompt/instruction-shape detector (avoid hardcoded project artifacts).
    imperative_hits = sum(
        1 for k in ["return exactly", "you must", "do not", "step 1", "step 2", "create ", "run ", "execute "] if k in t
    )
    shellish_hits = sum(1 for k in ["chmod", "pip install", "python ", "bash ", "&&", "#!/bin/"] if k in t)
    looks_like_instruction_blob = imperative_hits >= 2 or shellish_hits >= 2

    query_is_asking_for_execution = any(k in q for k in ["code", "script", "python", "bash", "chmod", "file", "folder", "run", "execute"])
    return looks_like_instruction_blob and not query_is_asking_for_execution


async def build_memory_block_impl(*, query: str, k: int, retrieve_memory, memory_item_is_prompt_polluting, build_file_memory_block, build_structured_memory_block, log) -> str:
    semantic = await retrieve_memory(query, k=k)
    semantic = [m for m in semantic if not memory_item_is_prompt_polluting(getattr(m, "text", ""), query)]

    sem_block = ""
    if semantic:
        sem_block = "\n\nRelevant memory:\n" + "\n".join(f"- {m.text}" for m in semantic)

    try:
        ids = [m.id for m in semantic]
        log.info("memory_read query=%r hits=%d ids=%s", query[:80], len(semantic), ids)
    except Exception:
        pass

    file_block = build_file_memory_block()
    return build_structured_memory_block(query) + sem_block + file_block

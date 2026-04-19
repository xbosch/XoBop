from __future__ import annotations

from typing import Callable


def initialize_database(
    *,
    get_db: Callable,
    ensure_core_schema: Callable,
    apply_migrations: Callable,
    bootstrap_default_skills: Callable,
    bootstrap_default_policies: Callable,
    bootstrap_default_prompts: Callable,
    ensure_default_cron_jobs: Callable,
    repo_cwd: str,
) -> None:
    with get_db() as conn:
        ensure_core_schema(conn)
        apply_migrations(conn)

    with get_db() as conn:
        conn.execute("INSERT INTO memory_items_fts(memory_items_fts) VALUES('rebuild')")

    bootstrap_default_skills()
    bootstrap_default_policies()
    bootstrap_default_prompts()

    with get_db() as conn:
        ensure_default_cron_jobs(conn, repo_cwd)

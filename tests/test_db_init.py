import sqlite3

from xobop.db_init import initialize_database


def test_initialize_database_calls_bootstrap_steps():
    conn = sqlite3.connect(":memory:")

    calls = {"schema": 0, "migrations": 0, "skills": 0, "policies": 0, "prompts": 0, "cron": 0}

    def get_db():
        class Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, exc_type, exc, tb):
                return False

        return Ctx()

    def ensure_core_schema(c):
        calls["schema"] += 1
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(text)")

    def apply_migrations(c):
        calls["migrations"] += 1

    def bootstrap_default_skills():
        calls["skills"] += 1

    def bootstrap_default_policies():
        calls["policies"] += 1

    def bootstrap_default_prompts():
        calls["prompts"] += 1

    def ensure_default_cron_jobs(c, cwd):
        calls["cron"] += 1

    initialize_database(
        get_db=get_db,
        ensure_core_schema=ensure_core_schema,
        apply_migrations=apply_migrations,
        bootstrap_default_skills=bootstrap_default_skills,
        bootstrap_default_policies=bootstrap_default_policies,
        bootstrap_default_prompts=bootstrap_default_prompts,
        ensure_default_cron_jobs=ensure_default_cron_jobs,
        repo_cwd=".",
    )

    assert calls == {"schema": 1, "migrations": 1, "skills": 1, "policies": 1, "prompts": 1, "cron": 1}

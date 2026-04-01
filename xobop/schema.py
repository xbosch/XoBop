from __future__ import annotations

import sqlite3

SCHEMA_SQL = r"""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_chat_id_id ON conversations(chat_id, id);

            CREATE TABLE IF NOT EXISTS facts (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                source TEXT DEFAULT '',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                source TEXT DEFAULT '',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                done_ts DATETIME
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                chat_id INTEGER NOT NULL,
                turn_id TEXT DEFAULT '',
                user_query TEXT NOT NULL,
                mode TEXT NOT NULL,
                tool_name TEXT,
                tool_args TEXT,
                result TEXT,
                exit_code INTEGER,
                duration_ms INTEGER,
                status TEXT NOT NULL DEFAULT 'ok',
                artifact_path TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_chat_id_id ON runs(chat_id, id);
            CREATE INDEX IF NOT EXISTS idx_runs_turn_id ON runs(turn_id, id);
            CREATE INDEX IF NOT EXISTS idx_runs_status_ts ON runs(status, timestamp);

            CREATE TABLE IF NOT EXISTS op_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                chat_id INTEGER NOT NULL DEFAULT 0,
                event_type TEXT NOT NULL,
                route TEXT DEFAULT '',
                command TEXT DEFAULT '',
                status TEXT DEFAULT 'ok',
                latency_ms INTEGER,
                retries INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_op_events_ts ON op_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_op_events_type_ts ON op_events(event_type, timestamp);

            CREATE TABLE IF NOT EXISTS op_signals_daily (
                day TEXT PRIMARY KEY,
                events_total INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                command_unknown INTEGER NOT NULL DEFAULT 0,
                retries_total INTEGER NOT NULL DEFAULT 0,
                median_latency_ms REAL NOT NULL DEFAULT 0,
                p95_latency_ms REAL NOT NULL DEFAULT 0,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS improvement_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'proposed',
                risk_level TEXT NOT NULL DEFAULT 'low',
                title TEXT NOT NULL,
                problem_statement TEXT NOT NULL,
                suspected_cause TEXT NOT NULL,
                proposed_change TEXT NOT NULL,
                expected_benefit TEXT NOT NULL,
                metric_target TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS improvement_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                promotion_mode TEXT DEFAULT '',
                eval_phase TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                before_json TEXT DEFAULT '{}',
                after_json TEXT DEFAULT '{}',
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS simulation_case_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                proposal_id INTEGER NOT NULL,
                case_id TEXT NOT NULL,
                case_class TEXT NOT NULL,
                expected TEXT DEFAULT '',
                actual TEXT DEFAULT '',
                improved INTEGER NOT NULL DEFAULT 0,
                regressed INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                policy_ok INTEGER NOT NULL DEFAULT 1,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_sim_case_results_run ON simulation_case_results(run_id);
            CREATE INDEX IF NOT EXISTS idx_sim_case_results_proposal ON simulation_case_results(proposal_id, created_ts);

            CREATE TABLE IF NOT EXISTS change_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                artifact_type TEXT NOT NULL,
                target TEXT NOT NULL,
                artifact_json TEXT NOT NULL,
                expected_metric TEXT NOT NULL DEFAULT '',
                expected_delta REAL,
                status TEXT NOT NULL DEFAULT 'compiled',
                promotion_mode TEXT DEFAULT '',
                eval_phase TEXT DEFAULT '',
                simulated_ts DATETIME,
                applied_ts DATETIME,
                commit_hash TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_change_artifacts_proposal ON change_artifacts(proposal_id, id);

            CREATE TABLE IF NOT EXISTS evolution_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                proposal_id INTEGER,
                signature TEXT NOT NULL,
                outcome TEXT NOT NULL,
                lesson TEXT NOT NULL,
                evidence_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_evolution_memory_sig ON evolution_memory(signature, created_ts);

            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                schema_json TEXT NOT NULL,
                handler_type TEXT NOT NULL,
                risk_level TEXT NOT NULL DEFAULT 'normal',
                timeout_sec INTEGER NOT NULL DEFAULT 30,
                requires_force INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                visible_in_strict INTEGER NOT NULL DEFAULT 1,
                visible_in_normal INTEGER NOT NULL DEFAULT 1,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS skill_policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                policy_key TEXT NOT NULL,
                policy_value TEXT NOT NULL,
                UNIQUE(skill_name, policy_key)
            );

            CREATE TABLE IF NOT EXISTS skill_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                skill_name TEXT NOT NULL,
                action TEXT NOT NULL,
                note TEXT DEFAULT '',
                owner_id INTEGER,
                cmd_input TEXT,
                exit_code INTEGER
            );

            CREATE TABLE IF NOT EXISTS skill_usage_stats (
                skill_name TEXT PRIMARY KEY,
                call_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                total_duration_ms INTEGER NOT NULL DEFAULT 0,
                last_used_ts DATETIME
            );

            CREATE TABLE IF NOT EXISTS cron_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                cron_expr TEXT NOT NULL,
                skill_type TEXT NOT NULL,
                skill_args TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                alert_owner INTEGER NOT NULL DEFAULT 1,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_run_ts DATETIME,
                last_status TEXT DEFAULT 'never'
            );

            CREATE TABLE IF NOT EXISTS cron_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                output TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                mem_type TEXT NOT NULL DEFAULT 'note',
                importance REAL NOT NULL DEFAULT 0.5,
                source TEXT DEFAULT '',
                pinned INTEGER NOT NULL DEFAULT 0,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_access_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                access_count INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_memory_items_active ON memory_items(is_deleted, pinned, created_ts);

            CREATE TABLE IF NOT EXISTS memory_vectors (
                memory_id INTEGER PRIMARY KEY,
                embedding_json TEXT NOT NULL,
                model TEXT NOT NULL,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS global_policies (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS runtime_prompts (
                mode TEXT PRIMARY KEY,
                prompt_text TEXT NOT NULL,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                bucket TEXT NOT NULL,
                weight INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_rate_events_bucket_ts ON rate_events(bucket, timestamp);

            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notification_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                delivered INTEGER NOT NULL DEFAULT 0,
                delivered_ts DATETIME
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
                text,
                content='memory_items',
                content_rowid='id'
            );
        """


def ensure_core_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)

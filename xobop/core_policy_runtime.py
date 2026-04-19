from __future__ import annotations

import os


def default_skills_impl(*, shell_timeout_write: int, allow_raw_shell: bool, request_timeout: int, max_browse_bytes: int, read_root, max_read_bytes: int, allowed_write_root):
    return [
        {
            "name": "run_shell",
            "description": (
                "Run a shell command. Uses argv with shell=False by default. "
                "Set raw=true to use shell=True if explicitly enabled. "
                "Supports optional cwd and timeout_override."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "raw": {"type": "boolean"},
                    "timeout_override": {"type": "integer"},
                },
                "required": ["command"],
            },
            "handler_type": "shell_command",
            "risk_level": "high",
            "timeout_sec": shell_timeout_write,
            "requires_force": 0,
            "enabled": 1,
            "visible_in_strict": 1,
            "visible_in_normal": 1,
            "policies": {
                "default_cwd": os.getcwd(),
                "allow_raw_shell": "1" if allow_raw_shell else "0",
                "allowed_commands": "",
                "write_root": str(allowed_write_root),
            },
        },
        {
            "name": "browse_page",
            "description": "Fetch a URL and return plain text content with a byte cap.",
            "schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            "handler_type": "http_get",
            "risk_level": "low",
            "timeout_sec": request_timeout,
            "requires_force": 0,
            "enabled": 1,
            "visible_in_strict": 1,
            "visible_in_normal": 1,
            "policies": {"max_bytes": str(max_browse_bytes)},
        },
        {
            "name": "read_file",
            "description": "Read a file under READ_ROOT with a byte cap.",
            "schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            "handler_type": "file_read",
            "risk_level": "low",
            "timeout_sec": 15,
            "requires_force": 0,
            "enabled": 1,
            "visible_in_strict": 1,
            "visible_in_normal": 1,
            "policies": {"read_root": str(read_root), "max_bytes": str(max_read_bytes)},
        },
        {
            "name": "write_file",
            "description": "Write content to a file under ALLOWED_WRITE_ROOT. Set overwrite=true to replace.",
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            "handler_type": "file_write",
            "risk_level": "high",
            "timeout_sec": 30,
            "requires_force": 0,
            "enabled": 1,
            "visible_in_strict": 1,
            "visible_in_normal": 1,
            "policies": {"write_root": str(allowed_write_root)},
        },
        {
            "name": "append_file",
            "description": "Append content to a file under ALLOWED_WRITE_ROOT.",
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            "handler_type": "file_append",
            "risk_level": "high",
            "timeout_sec": 30,
            "requires_force": 0,
            "enabled": 1,
            "visible_in_strict": 1,
            "visible_in_normal": 1,
            "policies": {"write_root": str(allowed_write_root)},
        },
    ]


def get_policy_impl(*, get_db, key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM global_policies WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_policy_impl(*, get_db, key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO global_policies (key,value,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, value),
        )


def bootstrap_default_policies_impl(*, get_db, defaults: dict[str, str], set_policy) -> None:
    with get_db() as conn:
        rows = conn.execute("SELECT key FROM global_policies").fetchall()
    existing = {r["key"] for r in rows}
    for k, v in defaults.items():
        if k not in existing:
            set_policy(k, v)


def bootstrap_default_prompts_impl(*, get_db, strict_prompt: str, normal_prompt: str) -> None:
    with get_db() as conn:
        rows = conn.execute("SELECT mode FROM runtime_prompts").fetchall()
    existing = {r["mode"] for r in rows}
    defaults = {
        "strict": strict_prompt,
        "normal": normal_prompt,
    }
    with get_db() as conn:
        for mode, prompt in defaults.items():
            if mode not in existing:
                conn.execute(
                    "INSERT INTO runtime_prompts (mode,prompt_text,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
                    (mode, prompt),
                )


def get_runtime_prompt_impl(*, get_db, mode: str, strict_prompt: str, normal_prompt: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT prompt_text FROM runtime_prompts WHERE mode=?", (mode,)).fetchone()
    if row and row["prompt_text"].strip():
        return row["prompt_text"]
    return strict_prompt if mode == "strict" else normal_prompt


def set_runtime_prompt_impl(*, get_db, mode: str, prompt_text: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runtime_prompts (mode,prompt_text,updated_ts) VALUES (?,?,CURRENT_TIMESTAMP)",
            (mode, prompt_text),
        )

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional


async def exec_file_read(*, skill, policies: dict, args: dict, read_root: Path, max_read_bytes: int, is_under_root, to_thread, read_text_limited, shorten_or_artifact):
    root = Path(policies.get("read_root", str(read_root))).resolve()
    max_bytes = int(policies.get("max_bytes", str(max_read_bytes)))
    path = Path(args["path"]).expanduser().resolve()
    if not is_under_root(path, root):
        return f"⛔ Path outside read root: {root}", None, "blocked", None
    full = await to_thread(read_text_limited, path, max_bytes)
    result, artifact = shorten_or_artifact(skill.name, full)
    return result, None, "ok", artifact


async def exec_http_get(*, skill, policies: dict, args: dict, domain_allowed, max_browse_bytes: int, to_thread, fetch_text_limited, shorten_or_artifact):
    url = args["url"]
    if not domain_allowed(url):
        return "⛔ URL domain blocked by global policy.", None, "blocked", None
    max_bytes = int(policies.get("max_bytes", str(max_browse_bytes)))
    full = await to_thread(fetch_text_limited, url, max_bytes)
    result, artifact = shorten_or_artifact(skill.name, full)
    return result, None, "ok", artifact


async def exec_file_write(*, skill, policies: dict, args: dict, allowed_write_root: Path, is_under_root, blocked_suffix, get_policy, default_max_write_size: int, to_thread):
    root = Path(policies.get("write_root", str(allowed_write_root))).resolve()
    path = Path(args["path"]).expanduser().resolve()
    overwrite = bool(args.get("overwrite", False))
    content = args["content"]
    if not is_under_root(path, root):
        return f"⛔ Write blocked outside root: {root}", None, "blocked", None
    blocked = blocked_suffix(path)
    if blocked:
        return f"⛔ Writing files with suffix {blocked} is blocked.", None, "blocked", None
    if len(content.encode("utf-8")) > int(get_policy("max_write_size", default_max_write_size)):
        return "⛔ Write size exceeds global policy.", None, "blocked", None
    if path.exists() and not overwrite:
        return f"⛔ Blocked overwrite without overwrite=true: {path}", None, "blocked", None

    def _do_write():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    await to_thread(_do_write)
    return f"Written {len(content):,} chars → {path}", None, "ok", None


async def exec_file_append(*, skill, policies: dict, args: dict, allowed_write_root: Path, is_under_root, blocked_suffix, get_policy, default_max_write_size: int, to_thread):
    root = Path(policies.get("write_root", str(allowed_write_root))).resolve()
    path = Path(args["path"]).expanduser().resolve()
    content = args["content"]
    if not is_under_root(path, root):
        return f"⛔ Write blocked outside root: {root}", None, "blocked", None
    blocked = blocked_suffix(path)
    if blocked:
        return f"⛔ Writing files with suffix {blocked} is blocked.", None, "blocked", None
    if len(content.encode("utf-8")) > int(get_policy("max_write_size", default_max_write_size)):
        return "⛔ Write size exceeds global policy.", None, "blocked", None

    def _do_append():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

    await to_thread(_do_append)
    return f"Appended {len(content):,} chars → {path}", None, "ok", None


async def exec_shell_command(*, skill, policies: dict, user_query: str, args: dict, use_capability_facade_shell: bool, get_shell_executor, exec_shell_command_capability):
    async def _legacy_runner(command: str, cwd: Optional[str], timeout: Optional[int], raw: bool):
        run_args = dict(args)
        run_args["command"] = command
        if cwd:
            run_args["cwd"] = cwd
        if timeout is not None:
            run_args["timeout_override"] = int(timeout)
        run_args["raw"] = bool(raw)
        return await exec_shell_command_capability(
            skill_name=skill.name,
            requires_force=skill.requires_force,
            policies=policies,
            user_query=user_query,
            args=run_args,
        )

    if not use_capability_facade_shell:
        return await _legacy_runner(
            str(args.get("command", "")),
            args.get("cwd"),
            args.get("timeout_override"),
            bool(args.get("raw", False)),
        )

    shell = get_shell_executor(mode="live", legacy_runner=_legacy_runner)
    cap = await shell.run(
        command=str(args.get("command", "")),
        cwd=args.get("cwd"),
        timeout=(int(args.get("timeout_override")) if args.get("timeout_override") is not None else None),
        raw=bool(args.get("raw", False)),
    )
    return cap.output


async def call_skill_impl(*, chat_id: int, user_query: str, mode: str, skill_name: str, args: dict, panic_locked, record_op_event, get_policy, default_global_policies: dict, enforce_rate_limit, get_skill, load_skill_policies, allowed_write_root: Path, project_root: Path, allow_raw_shell: bool, user_forced, handlers: dict, log, to_thread, record_run, update_skill_usage, audit_skill, fire_and_forget):
    start = time.monotonic()
    if panic_locked() and skill_name not in {"read_file", "browse_page"}:
        return "⛔ Panic-lock active: execution skills are disabled. Use /panicstatus or /panicunlock <code>."
    exit_code = None
    status = "ok"
    artifact_path = None
    result = f"Unknown skill: {skill_name}"

    tool_limit = int(get_policy("tool_calls_per_minute", default_global_policies["tool_calls_per_minute"]))
    ok, msg = enforce_rate_limit("tool_calls", tool_limit)
    if not ok:
        return f"{msg}\nRATE_LIMIT_BUCKET=tool_calls\nSUGGESTED_ACTION=retry_with_backoff"

    try:
        skill = get_skill(skill_name)
        policies = load_skill_policies(skill.name)

        if skill.name == "run_shell":
            policies["write_root"] = str(allowed_write_root)
            policies["default_cwd"] = str(project_root)
            policies["allow_raw_shell"] = "1" if allow_raw_shell else "0"

        if skill.handler_type == "shell_command":
            shell_limit = int(get_policy("shell_calls_per_minute", default_global_policies["shell_calls_per_minute"]))
            ok, msg = enforce_rate_limit("shell_calls", shell_limit)
            if not ok:
                return f"{msg}\nRUN_STATUS=FAIL\nERROR=Shell rate limit active; execution blocked\nRATE_LIMIT_BUCKET=shell_calls"

        if user_forced(user_query):
            record_op_event("manual_override", chat_id=chat_id, route=skill_name, status="ok")

        if skill.requires_force and not user_forced(user_query):
            result, status = f"⛔ Skill '{skill.name}' requires force.", "blocked"
        else:
            fn = handlers.get(skill.handler_type)
            if not fn:
                result, status = f"Unknown handler_type: {skill.handler_type}", "error"
            else:
                result, exit_code, status, artifact_path = await fn(skill=skill, policies=policies, args=args, user_query=user_query)

    except asyncio.TimeoutError as exc:
        result, status = f"⏱ Timeout: {exc}", "timeout"
    except Exception as exc:
        log.exception("Skill %s failed", skill_name)
        result, status = f"Error: {exc}", "error"

    duration_ms = int((time.monotonic() - start) * 1000)
    loop = asyncio.get_running_loop()
    fire_and_forget(loop, lambda: record_run(chat_id, user_query, mode, skill_name, args, result, exit_code, duration_ms, status, artifact_path), label=f"record_run:{skill_name}")
    fire_and_forget(loop, lambda: update_skill_usage(skill_name, duration_ms, status), label=f"skill_usage:{skill_name}")
    fire_and_forget(loop, lambda: audit_skill(skill_name, "call", note=status, owner_id=(chat_id if chat_id != 0 else None), cmd_input=(args.get("command", user_query) if isinstance(args, dict) else user_query), exit_code=exit_code), label=f"skill_audit:{skill_name}")
    return result

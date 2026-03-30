from __future__ import annotations

import re
import shlex
from typing import Any, Awaitable, Callable, Optional


async def exec_shell_command(
    *,
    skill_name: str,
    requires_force: bool,
    policies: dict,
    user_query: str,
    args: dict,
    classify_shell: Callable[[str], Any],
    user_forced: Callable[[str], bool],
    run_shell_async: Callable[[str, str, int, bool], Awaitable[tuple[str, str, int]]],
    shorten_or_artifact: Callable[[str, str], tuple[str, Optional[str]]],
) -> tuple[str, Optional[int], str, Optional[str]]:
    command = args["command"].strip()
    cwd = str(args.get("cwd") or policies.get("default_cwd") or ".")
    raw = bool(args.get("raw", False))
    timeout = int(args.get("timeout_override") or 60)

    # Normalize common shorthand
    command = re.sub(r"\bgit\s+(.+?)\s+push\s+force\b", r"git \1 push --force", command)

    policy = classify_shell(command)
    allowed_commands = [x.strip() for x in policies.get("allowed_commands", "").split(",") if x.strip()]

    raw_allowed = policies.get("allow_raw_shell", "0") == "1"
    if raw and not raw_allowed:
        return "⛔ Raw shell disabled by skill policy.", None, "blocked", None
    if (requires_force or policy.requires_force or raw) and not user_forced(user_query):
        return f"⛔ Blocked ({policy.kind}, raw={raw}). Add 'force' to proceed.\nCommand: {command}", None, "blocked", None

    segments = [s.strip() for s in command.split("&&") if s.strip()]
    if not segments:
        return "⛔ Empty command.", None, "blocked", None

    outputs = [f"Kind: {policy.kind}", f"CWD: {cwd}"]
    last_rc: Optional[int] = 0
    for i, seg in enumerate(segments, start=1):
        argv = shlex.split(seg) if seg else []
        if allowed_commands and argv and argv[0] not in allowed_commands:
            return f"⛔ Command '{argv[0]}' blocked by skill allowlist.", None, "blocked", None

        stdout, stderr, rc = await run_shell_async(seg, cwd, timeout, raw=False)
        outputs.append(f"[step {i}] $ {seg}")
        if stdout.strip():
            outputs.append(f"STDOUT:\n{stdout.strip()}")
        if stderr.strip():
            outputs.append(f"STDERR:\n{stderr.strip()}")
        outputs.append(f"Exit: {rc}")
        last_rc = rc
        if rc != 0:
            break

    result, artifact = shorten_or_artifact(skill_name, "\n".join(outputs))
    status = "ok" if (last_rc == 0) else "error"
    return result, last_rc, status, artifact

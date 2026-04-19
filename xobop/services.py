from __future__ import annotations

import os
import re
from typing import Awaitable, Callable

from .capabilities_fs import count_entries
from .intents import extract_git_dir


async def run_download_count(reply: Callable[[str], Awaitable[None]]) -> None:
    n = count_entries('/home/xavi/Downloads')
    await reply(f"You have {n} files in Downloads.")


async def run_git_commit_push(
    *,
    text: str,
    chat_id: int,
    state,
    call_skill: Callable[[int, str, str, str, dict], Awaitable[str]],
    mode_getter: Callable[[], str],
    reply: Callable[[str], Awaitable[None]],
) -> None:
    prev = state.last_exec_by_chat[chat_id]
    git_dir = extract_git_dir(prev) or os.getcwd()
    cmd = f'git -C {git_dir} add -A && git -C {git_dir} commit -m "Update via XoBop" && git -C {git_dir} push'
    state.last_exec_by_chat[chat_id] = cmd
    result = await call_skill(chat_id, text + " force", mode_getter(), "run_shell", {"command": cmd})
    await reply(result)


async def run_execute_last(
    *,
    text: str,
    chat_id: int,
    state,
    call_skill: Callable[[int, str, str, str, dict], Awaitable[str]],
    mode_getter: Callable[[], str],
    reply: Callable[[str], Awaitable[None]],
) -> None:
    cmd = state.last_exec_by_chat[chat_id]
    result = await call_skill(chat_id, text + " force", mode_getter(), "run_shell", {"command": cmd})
    await reply(result)


async def run_shell_with_autovenv(
    *,
    user_text: str,
    command: str,
    chat_id: int,
    call_skill: Callable[[int, str, str, str, dict], Awaitable[str]],
    mode_getter: Callable[[], str],
) -> str:
    result = await call_skill(chat_id, user_text, mode_getter(), "run_shell", {"command": command})

    m = re.search(r"ModuleNotFoundError:\s+No module named '([^']+)'", result or "")
    is_python_cmd = bool(re.search(r"\bpython(3)?\b", command))
    if not (m and is_python_cmd):
        return result

    mod = m.group(1).strip()
    if not mod or not re.fullmatch(r"[A-Za-z0-9_.-]+", mod):
        return result

    recovery_cmd = (
        "mkdir -p agent-test/runtime-venv && "
        "python3 -m venv agent-test/runtime-venv && "
        "source agent-test/runtime-venv/bin/activate && "
        "python -m pip install -U pip >/dev/null 2>&1 && "
        f"python -m pip install {mod} && "
        f"{command}"
    )
    retried = await call_skill(chat_id, user_text + " force", mode_getter(), "run_shell", {"command": recovery_cmd})
    return (
        result
        + "\n\n--- AUTO-RECOVERY ATTEMPT (venv + pip install) ---\n"
        + retried
    )


async def run_direct_shell(
    *,
    text: str,
    chat_id: int,
    state,
    call_skill: Callable[[int, str, str, str, dict], Awaitable[str]],
    mode_getter: Callable[[], str],
    reply: Callable[[str], Awaitable[None]],
) -> None:
    state.last_exec_by_chat[chat_id] = text
    result = await run_shell_with_autovenv(
        user_text=text,
        command=text,
        chat_id=chat_id,
        call_skill=call_skill,
        mode_getter=mode_getter,
    )
    await reply(result)

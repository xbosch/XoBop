from __future__ import annotations

import os
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
    result = await call_skill(chat_id, text, mode_getter(), "run_shell", {"command": text})
    await reply(result)

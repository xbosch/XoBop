from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

from openai import RateLimitError

_PENDING_BG_TASKS: set[asyncio.Task] = set()


def _track_bg_task(task: asyncio.Task, *, label: str, log) -> None:
    _PENDING_BG_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _PENDING_BG_TASKS.discard(t)
        try:
            exc = t.exception()
            if exc:
                log.warning("Background task [%s] failed: %s", label, exc)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("Background task [%s] completion check failed: %s", label, e)

    task.add_done_callback(_done)


async def run_agent_loop(
    *,
    chat_id: int,
    query: str,
    force: bool,
    mode: str,
    client,
    model: str,
    history_load_limit: int,
    max_memory_results: int,
    max_tool_rounds: int,
    history_max: int,
    history_trim_to: int,
    get_history: Callable[[int], list[dict[str, str]]],
    build_memory_block: Callable[[str, int], Awaitable[str]],
    build_tools_from_db: Callable[[str], list[dict[str, Any]]],
    user_forced: Callable[[str], bool],
    get_runtime_prompt: Callable[[str], str],
    save_message: Callable[[int, str, str], None],
    maybe_promote_memory: Callable[[str], Awaitable[None]],
    call_skill: Callable[[int, str, str, str, dict], Awaitable[str]],
    redact: Callable[[str], str],
    normalize_machine_kv_output: Callable[[str], str],
    fire_and_forget: Callable[[asyncio.AbstractEventLoop, Callable, str], None],
    log,
    set_last_openai_latency_ms: Callable[[float], None],
    temperature_for_mode: Callable[[str], float],
) -> str:
    hist = get_history(chat_id)

    prev_user = [m.get("content", "") for m in hist if m.get("role") == "user"][-3:]
    memory_query = "\n".join([*prev_user, query])
    memory_block = await build_memory_block(memory_query, k=min(3, max_memory_results))

    tools = build_tools_from_db(mode)

    effective_query = query + (" force" if force and not user_forced(query) else "")
    user_msg = {"role": "user", "content": effective_query}
    base_prompt = get_runtime_prompt(mode)
    messages: list[dict[str, Any]] = (
        [{"role": "system", "content": base_prompt + memory_block}] + hist[-history_load_limit:] + [user_msg]
    )

    if chat_id != 0:
        loop = asyncio.get_running_loop()
        fire_and_forget(loop, lambda: save_message(chat_id, "user", query), label="save_user_msg")
        hist.append(user_msg)
        _track_bg_task(asyncio.create_task(maybe_promote_memory(query, source="user")), label="maybe_promote_memory", log=log)

    final_text = "✓"
    round_count = 0

    while round_count < max_tool_rounds:
        try:
            t0 = time.monotonic()
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature_for_mode(mode),
                max_tokens=4096,
            )
            set_last_openai_latency_ms((time.monotonic() - t0) * 1000.0)
        except RateLimitError as exc:
            retry_after = 5
            try:
                retry_after = int(exc.response.headers.get("retry-after", 5))
            except Exception:
                pass
            wait = min(retry_after + round_count * 2, 60)
            log.warning("Rate limit — sleeping %ds", wait)
            await asyncio.sleep(wait)
            round_count += 1
            continue
        except Exception as exc:
            log.exception("LLM call failed")
            return f"LLM error: {str(exc)[:200]}"

        msg = response.choices[0].message
        final_text = msg.content or ""

        if not msg.tool_calls:
            break

        messages.append(msg.model_dump(exclude_none=True))

        for tc in msg.tool_calls:
            tool_name = tc.function.name
            tool_args: dict[str, Any] = {}
            try:
                import json as _json
                tool_args = _json.loads(tc.function.arguments)
            except Exception as exc:
                result = f"Error: could not parse tool arguments — {exc}"
                log.warning("Tool call %s had bad JSON args: %s", tool_name, exc)
            else:
                result = await call_skill(chat_id, effective_query, mode, tool_name, tool_args)

            if chat_id != 0:
                loop = asyncio.get_running_loop()
                fire_and_forget(
                    loop,
                    lambda r=result, n=tool_name, a=tool_args: save_message(chat_id, "tool", f"[{n}] {__import__('json').dumps(a)}\n{r}"),
                    label=f"save_tool_msg:{tool_name}",
                )
            messages.append({"role": "tool", "content": result, "tool_call_id": tc.id})

        round_count += 1
    else:
        log.warning("Hit MAX_TOOL_ROUNDS (%d), forcing final reply", max_tool_rounds)
        try:
            t1 = time.monotonic()
            final_resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature_for_mode(mode),
                max_tokens=512,
            )
            set_last_openai_latency_ms((time.monotonic() - t1) * 1000.0)
            final_text = final_resp.choices[0].message.content or "✓"
        except Exception:
            pass

    final_text = (final_text or "✓").strip()
    final_text = normalize_machine_kv_output(final_text)
    final_text = redact(final_text)
    if chat_id != 0:
        loop = asyncio.get_running_loop()
        fire_and_forget(loop, lambda: save_message(chat_id, "assistant", final_text), label="save_assistant_msg")
        hist.append({"role": "assistant", "content": final_text})
        if len(hist) > max(1, int(history_max)):
            trimmed = hist[-max(1, int(history_trim_to)):]
            hist.clear()
            hist.extend(trimmed)
    return final_text

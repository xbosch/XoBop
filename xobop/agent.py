from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from openai import RateLimitError

# Tools whose output is externally-sourced content (a webpage, a file) that
# the model didn't author -- used to tag subsequent tool calls in the same
# turn as "came after untrusted content was read," for audit purposes only
# (see call_skill_impl's _prior_content_read handling). Pure logging, no
# blocking behavior.
CONTENT_READ_TOOLS = frozenset({"browse_page", "read_file"})

_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("permission denied", "permission_denied — ask user; do not retry same command"),
    ("operation not permitted", "permission_denied — ask user; do not retry same command"),
    ("blocked by policy", "policy_block — action not allowed; suggest a compliant alternative"),
    ("blocked: write outside", "policy_block — write outside allowed root; use allowed write root"),
    ("rate limit", "rate_limited — wait before retrying"),
    ("no such file", "not_found — verify path exists before retrying"),
    ("not found", "not_found — verify path or resource before retrying"),
    ("timed out", "timeout — operation too slow; try a simpler command or smaller scope"),
    ("timeout", "timeout — operation too slow; try a simpler command or smaller scope"),
    ("connection refused", "network_error — service may be down; check connectivity"),
]


def _classify_tool_result(result: str) -> str:
    """Prefix failing tool results with a structured error tag the LLM can act on."""
    rl = (result or "").lower()
    for pattern, label in _ERROR_PATTERNS:
        if pattern in rl:
            return f"[ERR:{label}]\n{result}"
    return result

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


@dataclass(frozen=True)
class AgentDeps:
    get_history: Callable[[int], list[dict[str, str]]]
    build_memory_block: Callable[[str, int], Awaitable[str]]
    build_tools_from_db: Callable[[str], list[dict[str, Any]]]
    user_forced: Callable[[str], bool]
    get_runtime_prompt: Callable[[str], str]
    get_allowed_write_root: Callable[[], str]
    save_message: Callable[[int, str, str], None]
    maybe_promote_memory: Callable[[str, str], Awaitable[None]]
    call_skill: Callable[[int, str, str, str, dict], Awaitable[str]]
    redact: Callable[[str], str]
    normalize_machine_kv_output: Callable[[str], str]
    fire_and_forget: Callable[[asyncio.AbstractEventLoop, Callable, str], None]
    set_last_openai_latency_ms: Callable[[float], None]
    temperature_for_mode: Callable[[str], float]


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
    deps: AgentDeps,
    log,
) -> str:
    hist = deps.get_history(chat_id)

    prev_user = [m.get("content", "") for m in hist if m.get("role") == "user"][-3:]
    memory_query = "\n".join([*prev_user, query])
    memory_block = await deps.build_memory_block(memory_query, k=max_memory_results)

    tools = deps.build_tools_from_db(mode)

    effective_query = query + (" force" if force and not deps.user_forced(query) else "")

    # Hard anti-confirmation shim for common codegen requests:
    # when user asks to write/run code (e.g., plot script), proceed inside allowed root
    # without asking redundant folder confirmation.
    ql = (query or "").lower()
    if (("write" in ql or "create" in ql or "build" in ql) and ("script" in ql or "program" in ql or "code" in ql) and ("run" in ql or "execute" in ql or "compile" in ql)):
        effective_query += (
            "\n\nExecution policy for code tasks: "
            "(1) PLAN first with concrete steps, "
            "(2) create workspace files under allowed root (prefer subfolder agent-test/codegen-check when no path is given), "
            "(3) set up environment/toolchain as needed (venv/pip, cargo, gcc, etc.), "
            "(4) compile/run, "
            "(5) if failure, iterate once with concrete fix and rerun, "
            "(6) report structured result with outputs/artifacts. "
            "Do not ask folder/path confirmation when operating inside allowed root."
        )

    user_msg = {"role": "user", "content": effective_query}
    base_prompt = deps.get_runtime_prompt(mode)
    write_root = deps.get_allowed_write_root()
    execution_defaults = (
        "\nExecution defaults:\n"
        f"- Allowed write root is exactly: {write_root}\n"
        f"- If user asks to write/run code without specifying a path, default to {write_root}/agent-test/codegen-check.\n"
        "- Do not ask for folder clarification when the request can be executed inside allowed root.\n"
        "- Ask for confirmation only if user explicitly asks to write outside allowed root.\n"
    )
    messages: list[dict[str, Any]] = (
        [{"role": "system", "content": base_prompt + execution_defaults + memory_block}] + hist[-history_load_limit:] + [user_msg]
    )

    if chat_id != 0:
        loop = asyncio.get_running_loop()
        deps.fire_and_forget(loop, lambda: deps.save_message(chat_id, "user", query), label="save_user_msg")
        hist.append(user_msg)
        # Schedule directly on the active loop (same-loop safe semantics).
        task = loop.create_task(deps.maybe_promote_memory(query, source="user"))
        _track_bg_task(task, label="maybe_promote_memory", log=log)

    final_text = "✓"
    round_count = 0
    content_tools_seen: set[str] = set()

    while round_count < max_tool_rounds:
        try:
            t0 = time.monotonic()
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=deps.temperature_for_mode(mode),
                max_tokens=4096,
            )
            deps.set_last_openai_latency_ms((time.monotonic() - t0) * 1000.0)
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
                if content_tools_seen and isinstance(tool_args, dict):
                    tool_args["_prior_content_read"] = True
                result = await deps.call_skill(chat_id, effective_query, mode, tool_name, tool_args)
                result = _classify_tool_result(result)
                if tool_name in CONTENT_READ_TOOLS:
                    content_tools_seen.add(tool_name)

            if chat_id != 0:
                loop = asyncio.get_running_loop()
                deps.fire_and_forget(
                    loop,
                    lambda r=result, n=tool_name, a=tool_args: deps.save_message(chat_id, "tool", f"[{n}] {__import__('json').dumps(a)}\n{r}"),
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
                temperature=deps.temperature_for_mode(mode),
                max_tokens=512,
            )
            deps.set_last_openai_latency_ms((time.monotonic() - t1) * 1000.0)
            final_text = final_resp.choices[0].message.content or "✓"
        except Exception:
            pass

    # If the model returned no content and made no tool calls, force a text-only reply.
    # This happens with instruction-following models that see nothing to act on (e.g. "Hello").
    if not final_text:
        try:
            t2 = time.monotonic()
            fallback_resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=deps.temperature_for_mode(mode),
                max_tokens=512,
            )
            deps.set_last_openai_latency_ms((time.monotonic() - t2) * 1000.0)
            final_text = (fallback_resp.choices[0].message.content or "").strip()
        except Exception:
            pass

    final_text = (final_text or "✓").strip()
    final_text = deps.normalize_machine_kv_output(final_text)
    final_text = deps.redact(final_text)
    if chat_id != 0:
        loop = asyncio.get_running_loop()
        deps.fire_and_forget(loop, lambda: deps.save_message(chat_id, "assistant", final_text), label="save_assistant_msg")
        hist.append({"role": "assistant", "content": final_text})
        if len(hist) > max(1, int(history_max)):
            trimmed = hist[-max(1, int(history_trim_to)):]
            hist.clear()
            hist.extend(trimmed)
    return final_text

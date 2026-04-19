from __future__ import annotations

import uuid

from .agent import AgentDeps, run_agent_loop


async def run_agent_impl(*, chat_id: int, query: str, force: bool, get_mode, agent_turn_id, get_history, build_memory_block, build_tools_from_db, user_forced, get_runtime_prompt, get_allowed_write_root, save_message, maybe_promote_memory, call_skill, redact, normalize_machine_kv_output, fire_and_forget, set_last_latency_ref, temperature_for_mode, client, model, history_load_limit, max_memory_results, max_tool_rounds, history_max, history_trim_to, log):
    mode = get_mode()

    def _set_last_latency(ms: float) -> None:
        set_last_latency_ref(ms)

    token = agent_turn_id.set(f"{chat_id}-{uuid.uuid4().hex[:12]}")
    try:
        deps = AgentDeps(
            get_history=get_history,
            build_memory_block=build_memory_block,
            build_tools_from_db=build_tools_from_db,
            user_forced=user_forced,
            get_runtime_prompt=get_runtime_prompt,
            get_allowed_write_root=get_allowed_write_root,
            save_message=save_message,
            maybe_promote_memory=maybe_promote_memory,
            call_skill=call_skill,
            redact=redact,
            normalize_machine_kv_output=normalize_machine_kv_output,
            fire_and_forget=fire_and_forget,
            set_last_openai_latency_ms=_set_last_latency,
            temperature_for_mode=temperature_for_mode,
        )
        return await run_agent_loop(
            chat_id=chat_id,
            query=query,
            force=force,
            mode=mode,
            client=client,
            model=model,
            history_load_limit=history_load_limit,
            max_memory_results=max_memory_results,
            max_tool_rounds=max_tool_rounds,
            history_max=history_max,
            history_trim_to=history_trim_to,
            deps=deps,
            log=log,
        )
    finally:
        agent_turn_id.reset(token)

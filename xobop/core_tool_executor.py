from __future__ import annotations


async def call_tool_skill(*, chat_id: int, text: str, mode: str, capability: str, payload: dict, call_skill, log) -> str:
    try:
        return await call_skill(chat_id, text, mode, capability, payload)
    except Exception as exc:
        log.exception("Tool capability call failed: %s", capability)
        return f"⚠️ Tool call failed ({capability}): {str(exc)[:180]}"

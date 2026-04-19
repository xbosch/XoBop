import asyncio

from xobop.handlers_runtime import _dispatch_intent_if_any


class _Msg:
    class _Chat:
        async def send_action(self, *_args, **_kwargs):
            return None

    chat = _Chat()


class _Update:
    message = _Msg()


class _State:
    last_exec_by_chat = {}


def test_pipeline_contract_forces_execution_hint_and_kv_fallback():
    seen = {"text": None, "reply": None}

    async def run_agent(_chat_id, text):
        seen["text"] = text
        return "Template response without KV"

    async def reply(_u, text):
        seen["reply"] = text

    def op_event(*_a, **_k):
        return None

    async def call_skill(*_a, **_k):
        return ""

    req = """
Final output ONLY (exact keys):
RUN_STATUS=<PASS|WARN|FAIL>
RAW_ROWS=<n>
CLEAN_ROWS=<n>
REJECTED_ROWS=<n>
OUTLIER_ROWS=<n>
SCHEMA_VALID=<0|1>
CONSISTENCY=<CONSISTENT|DRIFT>
RECOVERY_OK=<0|1>
PROOF_FILES_OK=<0|1>
OUTPUT_DIR=~/agent-test/stress-lab
ERROR=<NONE or short reason>
"""

    async def _run():
        return await _dispatch_intent_if_any(
            update=_Update(),
            text=req,
            chat_id=1,
            state=_State(),
            reply=reply,
            run_agent=run_agent,
            list_cron_jobs=lambda: "",
            call_skill=call_skill,
            mode_getter=lambda: "normal",
            op_event=op_event,
            log=type("L", (), {"exception": lambda *a, **k: None})(),
        )

    handled = asyncio.run(_run())

    assert handled is True
    assert "Execution policy: You MUST execute" in (seen["text"] or "")
    assert "RUN_STATUS=FAIL" in (seen["reply"] or "")
    assert "ERROR=execution_contract_not_satisfied" in (seen["reply"] or "")

import asyncio
from types import SimpleNamespace

from xobop.handlers_runtime import process_text_message


class DummyChat:
    async def send_action(self, *_args, **_kwargs):
        return None


class DummyMessage:
    def __init__(self):
        self.chat = DummyChat()


class DummyUpdate:
    def __init__(self):
        self.message = DummyMessage()
        self.effective_chat = SimpleNamespace(id=1)


def test_status_command_dispatched_via_registry():
    called = {"status": 0}

    async def cmd_status(_u):
        called["status"] += 1

    async def _noop(*_args, **_kwargs):
        return None

    update = DummyUpdate()
    state = SimpleNamespace(
        last_attachment_by_chat={},
        pending_action_by_chat={},
        last_email_status_by_chat={},
        last_exec_by_chat={},
    )

    async def run():
        await process_text_message(
            update=update,
            text="/status",
            chat_id=1,
            state=state,
            reply=_noop,
            call_skill=_noop,
            mode_getter=lambda: "strict",
            list_cron_jobs=lambda: "",
            run_agent=lambda *_: _noop(),
            owner_only=lambda _u: True,
            cmd_cronadd=_noop,
            cmd_cronlist=_noop,
            cmd_cronrun=_noop,
            cmd_cronruns=_noop,
            cmd_status=cmd_status,
            cmd_health=_noop,
            cmd_checkup=_noop,
            cmd_state_check=_noop,
            cmd_state_repair=_noop,
            cmd_op_sanity=_noop,
            cmd_diagnose=_noop,
            cmd_improve_propose=_noop,
            cmd_improve_list=_noop,
            cmd_improve_compile=_noop,
            cmd_improve_artifacts=_noop,
            cmd_improve_simulate=_noop,
            cmd_improve_runs=_noop,
            cmd_improve_compare=_noop,
            cmd_improve_policy=_noop,
            cmd_improve_promote=_noop,
            cmd_improve_autopromote=_noop,
            cmd_improve_funnel=_noop,
            cmd_improve_eval_report=_noop,
            cmd_improve_weekly_report=_noop,
            cmd_improve_phase=_noop,
            cmd_improve_log_rollback=_noop,
            cmd_evolve_capture=_noop,
            cmd_evolve_memory=_noop,
            cmd_panicstatus=_noop,
            cmd_paniclock=_noop,
            cmd_panicunlock=_noop,
            panic_locked=lambda: False,
            panic_bypass_text=lambda _t: False,
            command_aliases_getter=lambda: {},
            op_event=lambda *a, **k: None,
            log=SimpleNamespace(exception=lambda *a, **k: None),
        )

    asyncio.run(run())
    assert called["status"] == 1

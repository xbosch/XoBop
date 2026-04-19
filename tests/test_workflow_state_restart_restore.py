import asyncio
from types import SimpleNamespace

from xobop.app_lifecycle import post_init_impl


class _DummyScheduler:
    def __init__(self):
        self._jobs = [1]

    def start(self):
        return None

    def get_jobs(self):
        return self._jobs


async def _noop_loop():
    await asyncio.sleep(0)


def test_post_init_restores_workflow_state_from_db_loader():
    app = SimpleNamespace(bot_data={})
    state = SimpleNamespace(
        pending_action_by_chat={},
        last_email_payload_by_chat={},
        email_workflow_state_by_chat={},
    )

    restored_messages = []

    async def _notify_owner(msg: str):
        restored_messages.append(msg)

    tasks = []

    def _set_tasks(t1, t2, t3, t4):
        tasks.extend([t1, t2, t3, t4])

    def _load_session_state(_key: str):
        # stale snapshot values should be overridden by DB-backed workflow_state
        return {
            "pending_action_by_chat": {"1": {"action": "stale"}},
            "last_email_payload_by_chat": {"1": {"to": "stale@example.com"}},
            "email_workflow_state_by_chat": {"1": "DRAFT_READY"},
        }

    def _load_workflow_state(key: str):
        if key == "pending_action":
            return {1: {"action": "send_email", "to": "neri1982@gmail.com"}}
        if key == "last_email_payload":
            return {1: {"to": "neri1982@gmail.com", "subject": "x", "body": "y", "attachment": None}}
        if key == "email_workflow_state":
            return {1: "AWAITING_ATTACHMENT"}
        return {}

    async def _run():
        await post_init_impl(
            app=app,
            build_scheduler=lambda: _DummyScheduler(),
            sync_scheduler=lambda _s: None,
            log=SimpleNamespace(info=lambda *a, **k: None),
            load_session_state=_load_session_state,
            load_workflow_state=_load_workflow_state,
            state=state,
            notify_owner=_notify_owner,
            state_persist_loop=_noop_loop,
            artifact_watch_loop=_noop_loop,
            notification_flush_loop=_noop_loop,
            memory_promotion_loop=_noop_loop,
            set_tasks=_set_tasks,
        )

    asyncio.run(_run())

    assert state.pending_action_by_chat[1]["action"] == "send_email"
    assert state.last_email_payload_by_chat[1]["to"] == "neri1982@gmail.com"
    assert state.email_workflow_state_by_chat[1] == "AWAITING_ATTACHMENT"
    assert restored_messages and "Resuming pending task state" in restored_messages[0]

    for t in tasks:
        t.cancel()

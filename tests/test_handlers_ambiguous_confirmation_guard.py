import asyncio
from types import SimpleNamespace

from xobop.handlers_runtime import ProcessTextDeps, process_text_message


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


def test_ambiguous_confirmation_tokens_request_clarification_and_do_not_run_agent():
    update = DummyUpdate()
    replies = []
    calls = {"agent": 0}

    async def _reply(_u, text):
        replies.append(text)

    async def _run_agent(_chat_id, _text):
        calls["agent"] += 1
        return "agent-result"

    async def _noop(*_a, **_k):
        return None

    state = SimpleNamespace(
        last_attachment_by_chat={},
        pending_action_by_chat={},
        last_email_status_by_chat={},
        last_exec_by_chat={},
    )

    deps = ProcessTextDeps(
        state=state,
        reply=_reply,
        call_skill=_noop,
        mode_getter=lambda: "normal",
        list_cron_jobs=lambda: "",
        run_agent=_run_agent,
        owner_only=lambda _u: True,
        panic_locked=lambda: False,
        panic_bypass_text=lambda _t: False,
        command_aliases_getter=lambda: {},
        analyze_attachment=lambda *_a, **_k: _noop(),
        op_event=lambda *a, **k: None,
        log=SimpleNamespace(exception=lambda *a, **k: None),
        command_registry={},
    )

    for t in ["Do it", "Do", "It", "Yes"]:
        asyncio.run(process_text_message(update=update, text=t, chat_id=1, deps=deps))

    assert calls["agent"] == 0
    assert replies and all("what should i do" in r.lower() for r in replies)

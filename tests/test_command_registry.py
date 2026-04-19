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


def _deps(*, state, run_agent, command_registry, aliases, events):
    async def _noop(*_args, **_kwargs):
        return None

    return ProcessTextDeps(
        state=state,
        reply=_noop,
        call_skill=_noop,
        mode_getter=lambda: "strict",
        list_cron_jobs=lambda: "",
        run_agent=run_agent,
        owner_only=lambda _u: True,
        panic_locked=lambda: False,
        panic_bypass_text=lambda _t: False,
        command_aliases_getter=lambda: aliases,
        analyze_attachment=lambda *_a, **_k: _noop(),
        op_event=lambda *a, **k: events.append((a, k)),
        log=SimpleNamespace(exception=lambda *a, **k: None),
        command_registry=command_registry,
    )


def test_status_command_dispatched_via_registry():
    called = {"status": 0}

    async def cmd_status(_u):
        called["status"] += 1

    update = DummyUpdate()
    state = SimpleNamespace(
        last_attachment_by_chat={},
        pending_action_by_chat={},
        last_email_status_by_chat={},
        last_exec_by_chat={},
    )

    command_registry = {
        "/status": (cmd_status, "route_selected", "cmd_status"),
    }

    async def run_agent(_chat_id, _text):
        return "ok"

    async def run():
        await process_text_message(
            update=update,
            text="/status",
            chat_id=1,
            deps=_deps(state=state, run_agent=run_agent, command_registry=command_registry, aliases={}, events=[]),
        )

    asyncio.run(run())
    assert called["status"] == 1


def test_alias_resolves_before_dispatch_and_preserves_args():
    called = {"status": 0, "cronrun": 0}

    async def cmd_status(_u):
        called["status"] += 1

    async def cmd_cronrun(_u):
        called["cronrun"] += 1

    update = DummyUpdate()
    state = SimpleNamespace(
        last_attachment_by_chat={},
        pending_action_by_chat={},
        last_email_status_by_chat={},
        last_exec_by_chat={},
    )

    command_registry = {
        "/status": (cmd_status, "route_selected", "cmd_status"),
        "/cronrun": (cmd_cronrun, None, None),
    }

    async def run_agent(_chat_id, _text):
        return "ok"

    events = []

    async def run():
        deps = _deps(
            state=state,
            run_agent=run_agent,
            command_registry=command_registry,
            aliases={"statu": "status", "cronru": "cronrun"},
            events=events,
        )
        await process_text_message(update=update, text="/statu full", chat_id=1, deps=deps)
        await process_text_message(update=update, text="/cronru load-heartbeat", chat_id=1, deps=deps)

    asyncio.run(run())
    assert called["status"] == 1
    assert called["cronrun"] == 1
    assert any(args and args[0] == "command_alias_applied" for args, _kwargs in events)

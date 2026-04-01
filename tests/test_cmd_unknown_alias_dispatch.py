import asyncio
from types import SimpleNamespace

import XoBop as xb


class DummyMessage:
    def __init__(self, text: str):
        self.text = text
        self.replies = []

    async def reply_text(self, text, parse_mode=None, disable_web_page_preview=True):
        self.replies.append(text)


class DummyUpdate:
    def __init__(self, text: str):
        self.message = DummyMessage(text)
        self.effective_chat = SimpleNamespace(id=xb.OWNER_ID)
        self.effective_user = SimpleNamespace(id=xb.OWNER_ID)


class DummyCtx:
    pass


def test_cmd_unknown_dispatches_alias_to_real_handler():
    called = {"status": 0}

    async def _status_handler(_update, _ctx):
        called["status"] += 1

    orig_dispatch = xb._COMMAND_DISPATCH
    orig_alias_getter = xb._get_command_aliases
    orig_record = xb._record_op_event

    xb._COMMAND_DISPATCH = {"status": _status_handler}
    xb._get_command_aliases = lambda: {"statu": "status"}
    xb._record_op_event = lambda *a, **k: None

    try:
        u = DummyUpdate("/statu")
        asyncio.run(xb.cmd_unknown(u, DummyCtx()))
        assert called["status"] == 1
        assert u.message.text == "/status"
    finally:
        xb._COMMAND_DISPATCH = orig_dispatch
        xb._get_command_aliases = orig_alias_getter
        xb._record_op_event = orig_record

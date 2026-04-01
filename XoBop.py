#!/usr/bin/env python3
"""Compatibility launcher.

Runtime implementation moved to xobop_core.py to keep this entrypoint thin.
"""

import xobop_core as _core

globals().update(vars(_core))


async def cmd_unknown(update, ctx):
    # test/compat monkeypatch bridge
    _core._COMMAND_DISPATCH = globals().get("_COMMAND_DISPATCH", _core._COMMAND_DISPATCH)
    _core._get_command_aliases = globals().get("_get_command_aliases", _core._get_command_aliases)
    _core._record_op_event = globals().get("_record_op_event", _core._record_op_event)
    return await _core.cmd_unknown(update, ctx)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

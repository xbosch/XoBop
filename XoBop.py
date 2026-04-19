#!/usr/bin/env python3
"""Compatibility launcher.

Runtime implementation moved to xobop_core.py to keep this entrypoint thin.
"""

import xobop_core as _core

globals().update(vars(_core))


async def cmd_unknown(update, ctx):
    # test/compat monkeypatch bridge
    new_dispatch = globals().get("_COMMAND_DISPATCH", _core._COMMAND_DISPATCH)
    new_alias = globals().get("_get_command_aliases", _core._get_command_aliases)
    new_record = globals().get("_record_op_event", _core._record_op_event)
    _core._COMMAND_DISPATCH = new_dispatch
    _core._get_command_aliases = new_alias
    _core._record_op_event = new_record
    # Also patch function-global runtime module where _core.cmd_unknown is defined.
    g = getattr(_core.cmd_unknown, "__globals__", {})
    g["_COMMAND_DISPATCH"] = new_dispatch
    g["_get_command_aliases"] = new_alias
    g["_record_op_event"] = new_record
    return await _core.cmd_unknown(update, ctx)


if __name__ == "__main__":
    main()

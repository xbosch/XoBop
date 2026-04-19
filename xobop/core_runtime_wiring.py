from __future__ import annotations

import asyncio
import os

from .core_message_dispatch import build_command_dispatch
from .core_runtime_signals import install_shutdown_signal_handlers


def run_main(ns: dict) -> None:
    ns["_init_db"]()
    ns["_init_extended_skills"]()
    ok, note = ns["_acquire_runtime_lock"]()
    if not ok:
        ns["log"].error("Runtime lock denied: %s", note)
        return
    ns["_startup_reconcile_runtime_state"]()
    if ns["_get_setting"]("mode") not in ns["MODES"]:
        ns["_set_setting"]("mode", ns["DEFAULT_MODE"] if ns["DEFAULT_MODE"] in ns["MODES"] else "strict")

    unlock_code = (os.getenv("PANIC_UNLOCK_CODE", "") or "").strip()
    if unlock_code:
        ns["_set_setting"]("panic_unlock_code", unlock_code)

    ns["log"].info(
        "XaviExecutor V5 | Owner: %d | Model: %s | Embeddings: %s | Mode: %s | Write root: %s",
        ns["OWNER_ID"],
        ns["MODEL"],
        ns["EMBEDDING_MODEL"],
        ns["_get_mode"](),
        ns["ALLOWED_WRITE_ROOT"],
    )

    app = ns["build_app"](token=ns["TOKEN"], post_init=ns["_post_init"], post_shutdown=ns["_post_shutdown"])
    commands = build_command_dispatch(ns)
    ns["_COMMAND_DISPATCH"] = dict(commands)

    ns["register_handlers"](
        app=app,
        commands=commands,
        cmd_unknown=ns["cmd_unknown"],
        handle_media_message=ns["handle_media_message"],
        handle_message=ns["handle_message"],
        on_error=ns["_on_error"],
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    install_shutdown_signal_handlers(loop=loop, graceful_shutdown_coro_factory=lambda: ns["_graceful_shutdown"](app))

    app.run_polling(drop_pending_updates=True, allowed_updates=["message"], close_loop=False)

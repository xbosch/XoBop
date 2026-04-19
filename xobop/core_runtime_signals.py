from __future__ import annotations

import asyncio
import signal


def install_shutdown_signal_handlers(*, loop, graceful_shutdown_coro_factory) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(graceful_shutdown_coro_factory()))

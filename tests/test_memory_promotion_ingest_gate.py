import asyncio
from types import SimpleNamespace

from xobop.core_memory_runtime import maybe_promote_memory_impl


def test_maybe_promote_ignores_instruction_blob():
    calls = {"add": 0}

    async def add_memory(*_a, **_k):
        calls["add"] += 1

    asyncio.run(
        maybe_promote_memory_impl(
            text="Step 1 create folder agent-test/codegen-check && chmod +x run.sh",
            source="user",
            add_memory=add_memory,
            append_daily_memory=lambda _t: None,
            promote_long_memory=lambda _t: None,
            log=SimpleNamespace(warning=lambda *a, **k: None),
        )
    )

    assert calls["add"] == 0


def test_maybe_promote_long_note_threshold_raised():
    calls = {"add": 0}

    async def add_memory(*_a, **_k):
        calls["add"] += 1

    text = "x" * 220
    asyncio.run(
        maybe_promote_memory_impl(
            text=text,
            source="user",
            add_memory=add_memory,
            append_daily_memory=lambda _t: None,
            promote_long_memory=lambda _t: None,
            log=SimpleNamespace(warning=lambda *a, **k: None),
        )
    )

    assert calls["add"] == 0

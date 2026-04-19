import asyncio
from pathlib import Path
from types import SimpleNamespace

from xobop.skills_runtime import call_skill_impl


def test_write_file_policy_write_root_is_clamped_to_allowed_root():
    captured = {}

    async def handler(**kwargs):
        captured["policies"] = dict(kwargs["policies"])
        return "ok", 0, "ok", None

    async def run():
        await call_skill_impl(
            chat_id=1,
            user_query="write file",
            mode="normal",
            skill_name="write_file",
            args={"path": "agent-test/codegen-check/plot.py", "content": "print(1)"},
            panic_locked=lambda: False,
            record_op_event=lambda *a, **k: None,
            get_policy=lambda k, d=None: d,
            default_global_policies={"tool_calls_per_minute": "999", "shell_calls_per_minute": "999"},
            enforce_rate_limit=lambda *_: (True, "ok"),
            get_skill=lambda _: SimpleNamespace(name="write_file", handler_type="write_file", requires_force=False),
            load_skill_policies=lambda _: {"write_root": "/home/xavi/.openclaw/workspace/xobops-test"},
            allowed_write_root=Path("/home/xavi/.openclaw/workspace/xobops-dev-clean"),
            project_root=Path("/home/xavi/.openclaw/workspace/xobops-dev-clean"),
            allow_raw_shell=False,
            user_forced=lambda _: False,
            handlers={"write_file": handler},
            log=SimpleNamespace(exception=lambda *a, **k: None),
            to_thread=lambda fn, *a, **k: fn(*a, **k),
            record_run=lambda *a, **k: None,
            update_skill_usage=lambda *a, **k: None,
            audit_skill=lambda *a, **k: None,
            fire_and_forget=lambda *_args, **_kwargs: None,
        )

    asyncio.run(run())
    assert captured["policies"]["write_root"] == "/home/xavi/.openclaw/workspace/xobops-dev-clean"

import asyncio
from pathlib import Path
from types import SimpleNamespace

from xobop.skills_runtime import call_skill_impl


def _base_kwargs(**overrides):
    kwargs = dict(
        chat_id=1,
        user_query="run something",
        mode="normal",
        panic_locked=lambda: False,
        record_op_event=lambda *a, **k: None,
        get_policy=lambda k, d=None: d,
        default_global_policies={"tool_calls_per_minute": "999", "shell_calls_per_minute": "999"},
        enforce_rate_limit=lambda *_a, **_k: (True, "ok"),
        allowed_write_root=Path("/tmp"),
        project_root=Path("/tmp"),
        allow_raw_shell=False,
        user_forced=lambda _: False,
        log=SimpleNamespace(exception=lambda *a, **k: None),
        to_thread=lambda fn, *a, **k: fn(*a, **k),
        record_run=lambda *a, **k: None,
        update_skill_usage=lambda *a, **k: None,
        fire_and_forget=lambda loop, fn, **_k: fn(),  # run synchronously so audit_skill fires before assertions
    )
    kwargs.update(overrides)
    return kwargs


def test_prior_content_read_marker_is_stripped_before_handler_and_recorded_in_audit():
    captured_args = {}
    captured_audit = {}

    async def handler(**kwargs):
        captured_args.update(kwargs["args"])
        return "ok", 0, "ok", None

    def audit_skill(skill_name, action, note="", **_k):
        captured_audit["note"] = note

    async def run():
        await call_skill_impl(
            **_base_kwargs(
                skill_name="run_shell",
                args={"command": "cat notes.txt", "_prior_content_read": True},
                get_skill=lambda _: SimpleNamespace(name="run_shell", handler_type="shell_command", requires_force=False),
                load_skill_policies=lambda _: {},
                handlers={"shell_command": handler},
                audit_skill=audit_skill,
            )
        )

    asyncio.run(run())
    assert "_prior_content_read" not in captured_args  # never reaches the real handler
    assert captured_args["command"] == "cat notes.txt"
    assert captured_audit["note"] == "ok;prior_content_read=1"


def test_no_prior_content_read_marker_leaves_audit_note_unchanged():
    captured_audit = {}

    async def handler(**kwargs):
        return "ok", 0, "ok", None

    def audit_skill(skill_name, action, note="", **_k):
        captured_audit["note"] = note

    async def run():
        await call_skill_impl(
            **_base_kwargs(
                skill_name="run_shell",
                args={"command": "ls"},
                get_skill=lambda _: SimpleNamespace(name="run_shell", handler_type="shell_command", requires_force=False),
                load_skill_policies=lambda _: {},
                handlers={"shell_command": handler},
                audit_skill=audit_skill,
            )
        )

    asyncio.run(run())
    assert captured_audit["note"] == "ok"


def test_max_calls_per_hour_policy_triggers_hourly_bucket_check():
    seen_calls = []

    def enforce_rate_limit(bucket, limit, weight=1, lookback_minutes=1):
        seen_calls.append((bucket, limit, lookback_minutes))
        if bucket.startswith("skill_hourly:"):
            return False, "⛔ Rate limit exceeded"
        return True, "ok"

    async def handler(**kwargs):
        return "should not be reached", 0, "ok", None

    async def run():
        return await call_skill_impl(
            **_base_kwargs(
                skill_name="browse_page",
                args={"url": "https://example.com"},
                enforce_rate_limit=enforce_rate_limit,
                get_skill=lambda _: SimpleNamespace(name="browse_page", handler_type="http_get", requires_force=False),
                load_skill_policies=lambda _: {"max_calls_per_hour": "5"},
                handlers={"http_get": handler},
                audit_skill=lambda *a, **k: None,
            )
        )

    result = asyncio.run(run())
    assert "Rate limit exceeded" in result
    assert any(bucket == "skill_hourly:browse_page" and lookback == 60 for bucket, _limit, lookback in seen_calls)


def test_no_max_calls_per_hour_policy_skips_hourly_check():
    seen_buckets = []

    def enforce_rate_limit(bucket, limit, weight=1, lookback_minutes=1):
        seen_buckets.append(bucket)
        return True, "ok"

    async def handler(**kwargs):
        return "ok", 0, "ok", None

    async def run():
        await call_skill_impl(
            **_base_kwargs(
                skill_name="browse_page",
                args={"url": "https://example.com"},
                enforce_rate_limit=enforce_rate_limit,
                get_skill=lambda _: SimpleNamespace(name="browse_page", handler_type="http_get", requires_force=False),
                load_skill_policies=lambda _: {},  # unset, matches production default today
                handlers={"http_get": handler},
                audit_skill=lambda *a, **k: None,
            )
        )

    asyncio.run(run())
    assert not any(b.startswith("skill_hourly:") for b in seen_buckets)

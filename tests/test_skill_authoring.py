import asyncio
import json

import pytest

from xobop.skill_authoring import (
    SkillAuthoringError,
    _make_author_skill,
    _validate_code,
    _validate_name,
    load_persisted_custom_skills,
)
import xobop.skill_authoring as skill_authoring


def _run(coro):
    return asyncio.run(coro)


def test_validate_name_rejects_non_identifier():
    with pytest.raises(SkillAuthoringError):
        _validate_name("not a valid name!", reserved=set())


def test_validate_name_rejects_reserved_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_authoring, "_CUSTOM_SKILLS_DIR", tmp_path)
    with pytest.raises(SkillAuthoringError):
        _validate_name("run_shell", reserved={"run_shell"})


def test_validate_name_allows_overwriting_own_custom_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_authoring, "_CUSTOM_SKILLS_DIR", tmp_path)
    (tmp_path / "my_skill.py").write_text("def my_skill(args, **_): return 'x'")
    _validate_name("my_skill", reserved={"my_skill"})  # must not raise


def test_validate_code_rejects_syntax_error():
    with pytest.raises(SkillAuthoringError):
        _validate_code("def broken(:\n  pass", "broken")


def test_validate_code_requires_matching_top_level_function():
    with pytest.raises(SkillAuthoringError):
        _validate_code("def other_name(args, **_):\n    return 'x'", "expected_name")


def test_validate_code_accepts_async_def():
    _validate_code("async def my_fn(args, **_):\n    return 'x'", "my_fn")  # must not raise


def test_author_skill_writes_registers_and_is_immediately_callable(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_authoring, "_CUSTOM_SKILLS_DIR", tmp_path)

    registered = {}
    upserted = []

    def fake_register(name, fn):
        registered[name] = fn

    def fake_upsert(**kw):
        upserted.append(kw)

    author_skill = _make_author_skill(
        register_python_fn=fake_register, upsert_skill=fake_upsert, reserved_names=set(),
    )

    result = _run(author_skill({
        "name": "double_it",
        "description": "doubles a number",
        "schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        "code": "def double_it(args: dict, **_) -> str:\n    return str(args['n'] * 2)\n",
    }))

    assert "double_it" in result
    assert "double_it" in registered
    assert registered["double_it"]({"n": 21}) == "42"
    assert upserted[0]["name"] == "double_it"
    assert upserted[0]["handler_type"] == "python_fn"
    assert (tmp_path / "double_it.py").exists()
    meta = json.loads((tmp_path / "double_it.json").read_text())
    assert meta["description"] == "doubles a number"


def test_author_skill_rejects_reserved_name(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_authoring, "_CUSTOM_SKILLS_DIR", tmp_path)
    author_skill = _make_author_skill(
        register_python_fn=lambda *a: None, upsert_skill=lambda **k: None, reserved_names={"run_shell"},
    )
    with pytest.raises(SkillAuthoringError):
        _run(author_skill({
            "name": "run_shell", "description": "x", "schema": {"type": "object"},
            "code": "def run_shell(args, **_): return 'x'",
        }))


def test_load_persisted_custom_skills_reloads_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_authoring, "_CUSTOM_SKILLS_DIR", tmp_path)
    (tmp_path / "triple_it.py").write_text("def triple_it(args: dict, **_) -> str:\n    return str(args['n'] * 3)\n")
    (tmp_path / "triple_it.json").write_text(json.dumps({
        "description": "triples a number",
        "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
        "risk_level": "normal",
        "requires_force": False,
        "timeout_sec": 30,
    }))

    registered = {}
    upserted = []
    count = load_persisted_custom_skills(
        register_python_fn=lambda name, fn: registered.__setitem__(name, fn),
        upsert_skill=lambda **kw: upserted.append(kw),
    )

    assert count == 1
    assert registered["triple_it"]({"n": 10}) == "30"
    assert upserted[0]["name"] == "triple_it"


def test_load_persisted_custom_skills_skips_missing_metadata(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(skill_authoring, "_CUSTOM_SKILLS_DIR", tmp_path)
    (tmp_path / "orphan.py").write_text("def orphan(args, **_): return 'x'")

    count = load_persisted_custom_skills(register_python_fn=lambda *a: None, upsert_skill=lambda **k: None)
    assert count == 0
